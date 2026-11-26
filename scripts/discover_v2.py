"""Two-layer automated NHL contract & trade discovery engine.

Layer 1: Official NHL Web API (api-web.nhle.com) + NHL.com Forge API
Layer 2: ProHockeyRumors Player RSS feeds (/players/{slug}/feed?paged=N) for
         insider-reported terms when clubs omit dollars (e.g. TOR, NYI) or ELCs.

Strictly zero cap/contract database sites (PuckPedia, CapWages, Spotrac, etc.).
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

CURRENT_START_YEAR = 2026
CURRENT_SEASON = "2026-27"
CURRENT_SEASON_INT = 20262027

from discover_team import TEAM_META, forge_web_url, fetch_forge_pages, fetch_story_full

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}

BUYOUT_RE = re.compile(r"\b(bought out|buyout|buy out|termination|terminated)\b", re.I)
UNDISCLOSED_RE = re.compile(
    r"(?:financial\s+)?terms\s+(?:of\s+the\s+(?:contract|deal)\s+)?were\s+not\s+(?:disclosed|announced|released)",
    re.I,
)
SIGN_HL_RE = re.compile(
    r"\b(sign|signs|signed|re-sign|re-signs|re-signed|extend|extends|extended|extension|agree|agrees|agreed|entry-level|elc)\b",
    re.I,
)
EXCLUDE_SIGN_HL_RE = re.compile(
    r"\b(bought out|buyout|buy out|recall|recalled|loan|loaned|assign|assigned|reassign|reassigned|activate|activated|place.*on ir|waivers|pto|professional tryout|notes:|snapshots:|rumor|rumour|interested|open to|talks|could|might|projected|lineup|recap|deep dive|captain|suspension|hearing|injury|out with|out one|awards|finalists|declines)\b",
    re.I,
)
TRADE_HL_RE = re.compile(r"\b(trade|traded|acquire|acquires|acquired|claim|claimed|waivers)\b", re.I)


def slugify_name(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = s.lower().replace("'", "").replace(".", "")
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def fetch_raw(url: str) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=18) as r:
        return r.read().decode("utf-8", errors="replace")


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<forge-entity[^>]*>(.*?)</forge-entity>", r"\1", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    raw = raw.replace("&#8217;", "'").replace("&#8211;", "-").replace("&#8212;", "-").replace("&amp;", "&").replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", raw).strip()


def parse_money(val_str: str, unit: str | None) -> int:
    num = float(val_str.replace(",", ""))
    if unit:
        u = unit.lower()
        if u.startswith("m"):
            return int(round(num * 1_000_000))
        if u.startswith("k"):
            return int(round(num * 1_000))
    return int(round(num))


def parse_contract_text(body: str, player_last: str, is_nhl_official: bool = False) -> dict:
    idx = body.lower().find(player_last.lower())
    window = body[max(0, idx - 120) : idx + 1400] if idx != -1 else body[:1500]
    lead = window[:450]

    # Term years
    term_years = None
    m_term = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|[1-8])[\s-]year\b",
        window[:800],
        re.I,
    )
    if m_term:
        tok = m_term.group(1).lower()
        term_years = WORD_NUMS.get(tok, int(tok) if tok.isdigit() else None)

    is_elc = bool(re.search(r"\bentry[\s-]level\b|\bELC\b", window[:800]))
    two_way = bool(re.search(r"\btwo[\s-]way\s+(?:contract|deal|agreement|extension)\b", window[:900], re.I))

    aav = None
    total_value = None

    # If an official NHL.com release explicitly says financial terms weren't disclosed,
    # do NOT accidentally grab a previous contract's dollar value from later paragraphs!
    if not (is_nhl_official and UNDISCLOSED_RE.search(window[:650])):
        # Pattern 1: explicit AAV / cap hit ("AAV of $X.YM", "$X.YMM AAV", "cap hit of $X.YM", "carries a $X.YM AAV")
        aav_pats = [
            r"(?:average annual value(?:\s*\(AAV\))?|annual average value|AAV|cap hit)\s+(?:of\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
            r"\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?\s*(?:average annual value|AAV|cap hit|per season|per year|annually)",
        ]
        for pat in aav_pats:
            m = re.search(pat, window, re.I)
            if m:
                val = parse_money(m.group(1), m.group(2))
                if 700_000 <= val <= 25_000_000:
                    aav = val
                    break

        # Pattern 2: total value ("eight-year, $68M", "two-year, $5 million", "worth $X.YMM")
        tot_pats = [
            r"(?:one|two|three|four|five|six|seven|eight|[1-8])[\s-]year,\s+(?:two-way\s+|one-way\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
            r"(?:worth|totaling|valued at)\s+(?:a total of\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?",
        ]
        for pat in tot_pats:
            m = re.search(pat, window[:900], re.I)
            if m:
                val = parse_money(m.group(1), m.group(2))
                if term_years and val >= 700_000 * term_years and val <= 25_000_000 * term_years:
                    total_value = val
                    break
                elif term_years == 1 and 700_000 <= val <= 25_000_000:
                    total_value = val
                    break

        # Pattern 3: league minimum ("league minimum" or "$775K" / "$850K" / "$950K" in 1-year or two-way text)
        if not aav and not total_value:
            m_k = re.search(r"\$([\d,]+(?:\.\d+)?)\s*(K\b|million\b|MM\b|M\b)?\s*(?:at the NHL level|in the NHL|base salary)", window, re.I)
            if m_k:
                val = parse_money(m_k.group(1), m_k.group(2))
                if 700_000 <= val <= 25_000_000:
                    aav = val

        if term_years and total_value and not aav:
            aav = int(round(total_value / term_years))
        elif term_years and aav and not total_value:
            total_value = aav * term_years

        if aav is not None and not (700_000 <= aav <= 25_000_000):
            aav = None
            total_value = None

    # Explicit start season detection
    explicit_start = None
    m_start = re.search(
        r"(?:begins|begin|starts|starting|commencing|kicks in|takes effect)\s+(?:with\s+the\s+|in\s+(?:the\s+)?)?(20\d{2}-\d{2})",
        window[:1000],
        re.I,
    )
    if m_start:
        explicit_start = m_start.group(1)
    elif re.search(r"(?:begins|starts|starting|start)\s+(?:next\s+season|after\s+this\s+season|in\s+the\s+20\d{2})", window[:1000], re.I):
        m_yr = re.search(r"in\s+the\s+(20\d{2}-\d{2})", window[:1000], re.I)
        explicit_start = m_yr.group(1) if m_yr else "NEXT_SEASON"

    # Also detect explicit expiry ("through the 2029-30 season" / "through 2027-28")
    explicit_end = None
    m_end = re.search(r"through\s+(?:the\s+)?(20\d{2}-\d{2})", window[:1000], re.I)
    if m_end:
        explicit_end = m_end.group(1)

    status_hint = None
    if is_elc:
        status_hint = "ELC"
    elif re.search(r"\brestricted free agent\b|\bRFA\b", window[:1000], re.I):
        status_hint = "RFA"
    elif re.search(r"\bunrestricted free agent\b|\bUFA\b", window[:1000], re.I):
        status_hint = "UFA"

    sentences = re.split(r"(?<=[.!?])\s+", window)
    quote = " ".join(sentences[:2]).strip()[:320]
    return {
        "term_years": term_years,
        "aav_usd": aav,
        "total_value_usd": total_value,
        "is_elc": is_elc,
        "two_way": two_way,
        "explicit_start": explicit_start,
        "explicit_end": explicit_end,
        "status_hint": status_hint,
        "quote": quote,
    }


def resolve_seasons(
    signed_date: str,
    term_years: int | None,
    explicit_start: str | None,
    explicit_end: str | None,
    is_elc: bool,
    is_extension_hl: bool,
    bio: dict,
) -> tuple[str | None, str | None]:
    if not signed_date or not term_years:
        return None, None
    sy, sm, _ = map(int, signed_date.split("-"))

    if explicit_end:
        end_start = int(explicit_end.split("-")[0])
        start_yr = end_start - term_years + 1
    elif explicit_start and explicit_start != "NEXT_SEASON":
        start_yr = int(explicit_start.split("-")[0])
    elif explicit_start == "NEXT_SEASON":
        start_yr = sy + 1 if sm >= 7 else sy
    elif is_elc:
        by, bm, bd = map(int, bio["birthDate"].split("-"))
        nhl_gp: dict[int, int] = {}
        for st in bio.get("seasonTotals", []):
            if st.get("gameTypeId") == 2 and st.get("leagueAbbrev") == "NHL":
                nhl_gp[st["season"]] = nhl_gp.get(st["season"], 0) + (st.get("gamesPlayed", 0) or 0)
        cand = sy if sm >= 6 else (sy - 1 if nhl_gp.get((sy - 1) * 10000 + sy, 0) > 0 else sy)
        slides = 0
        while slides < 2 and cand < CURRENT_START_YEAR:
            age_sep15 = cand - by - ((9, 15) < (bm, bd))
            if age_sep15 <= 19 and nhl_gp.get(cand * 10000 + (cand + 1), 0) < 10:
                cand += 1
                slides += 1
            else:
                break
        start_yr = cand
    else:
        # Extension signed July..Dec while player already had a contract for sy-(sy+1) starts sy+1
        # Regular FA/RFA signing July..Dec starts sy
        if is_extension_hl and sm >= 7 and not (sm == 7 and int(signed_date.split("-")[2]) <= 15):
            start_yr = sy + 1
        else:
            start_yr = sy

    end_yr = start_yr + term_years - 1
    return f"{start_yr}-{str(start_yr + 1)[-2:]}", f"{end_yr}-{str(end_yr + 1)[-2:]}"


def fetch_phr_player_articles(player_name: str, max_pages: int = 4) -> list[dict]:
    slug = slugify_name(player_name)
    items = []
    seen = set()
    for page in range(1, max_pages + 1):
        u = f"https://www.prohockeyrumors.com/players/{slug}/feed" + (f"?paged={page}" if page > 1 else "")
        try:
            xml_txt = fetch_raw(u)
            # Strip invalid XML control chars
            xml_txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_txt)
            root = ET.fromstring(xml_txt)
        except Exception:
            break
        batch = root.findall(".//item")
        if not batch:
            break
        for it in batch:
            link = it.findtext("link") or ""
            if not link or link in seen:
                continue
            seen.add(link)
            title = it.findtext("title") or ""
            pub = it.findtext("pubDate") or ""
            # Parse RFC822 date "Sat, 01 Jul 2023 ..."
            dt = ""
            m_d = re.search(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
            if m_d:
                months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
                dt = f"{m_d.group(3)}-{months.get(m_d.group(2),'01')}-{m_d.group(1)}"
            # Check content:encoded or description right inside RSS!
            desc = ""
            for child in it:
                if child.tag.endswith("encoded") and child.text:
                    desc = clean_html(child.text)
                    break
            if not desc:
                desc = clean_html(it.findtext("description") or "")
            items.append({"date": dt, "title": title, "url": link, "rss_body": desc})
        if len(batch) < 15:
            break
    return items


def fetch_phr_full_body(url: str) -> str:
    html = fetch_raw(url)
    ps = [clean_html(p) for p in re.findall(r"<p[^>]*>(.*?)</p>", html, re.I | re.S)]
    ps = [p for p in ps if len(p) > 35 and "Pro Hockey Rumors" not in p[:40]]
    return " ".join(ps[:12])


def run_team(team_code: str) -> dict:
    team_code = team_code.upper()
    tid, _, tag_prefix = TEAM_META[team_code]
    roster = json.loads(fetch_raw(f"https://api-web.nhle.com/v1/roster/{team_code}/current"))
    players = [
        {
            "id": p["id"],
            "name": f"{p['firstName']['default']} {p['lastName']['default']}",
            "first": p["firstName"]["default"],
            "last": p["lastName"]["default"],
            "pos": p["positionCode"],
            "sweater": p.get("sweaterNumber"),
        }
        for grp in ("forwards", "defensemen", "goalies")
        for p in roster[grp]
    ]

    # 1. Fetch official NHL API bios
    with ThreadPoolExecutor(max_workers=10) as ex:
        bios = dict(
            ex.map(
                lambda p: (p["id"], json.loads(fetch_raw(f"https://api-web.nhle.com/v1/player/{p['id']}/landing"))),
                players,
            )
        )

    # 2. Fetch NHL.com Forge pool (team + player)
    pool: dict[str, dict] = {}
    for expr, pages in [
        (f"{tag_prefix}-created,transactions", 10),
        (f"teamid-{tid},free-agency", 8),
        (f"teamid-{tid},transactions", 8),
    ]:
        for it in fetch_forge_pages(expr, max_pages=pages):
            pool[it["slug"]] = it

    def scan_pid(p):
        pid = p["id"]
        out = []
        for expr, pg in [(f"playerid-{pid},free-agency", 2), (f"playerid-{pid},transactions", 2), (f"playerid-{pid}", 2)]:
            out.extend(fetch_forge_pages(expr, max_pages=pg))
        return out

    with ThreadPoolExecutor(max_workers=10) as ex:
        for batch in ex.map(scan_pid, players):
            for it in batch:
                pool[it["slug"]] = it

    # 3. For each player, evaluate NHL.com Forge candidates + ProHockeyRumors player RSS
    def resolve_player(p):
        pid = p["id"]
        bio = bios[pid]
        last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)

        nhl_signings = []
        nhl_trades = []
        for sl, it in pool.items():
            hl = it.get("headline") or it.get("title") or ""
            tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
            if "game-recap" in tags or BUYOUT_RE.search(hl):
                continue
            if not (f"playerid-{pid}" in tags or last_re.search(hl)):
                continue
            dt = (it.get("contentDate") or "")[:10]
            if SIGN_HL_RE.search(hl) and not EXCLUDE_SIGN_HL_RE.search(hl):
                nhl_signings.append((dt, sl, hl))
            if TRADE_HL_RE.search(hl) and not re.search(r"recall|loan|assign|activate|ir\b", hl, re.I):
                nhl_trades.append((dt, sl, hl))

        nhl_signings.sort(reverse=True)
        seen_s = set()
        parsed_contracts = []

        for dt, sl, hl in nhl_signings:
            if sl in seen_s or len(seen_s) >= 4:
                continue
            seen_s.add(sl)
            try:
                st = fetch_story_full(sl)
            except Exception:
                continue
            if not last_re.search(st["body"]):
                continue
            if p["first"].lower() not in st["body"].lower() and f"playerid-{pid}" not in st["tags"]:
                continue
            terms = parse_contract_text(st["body"], p["last"], is_nhl_official=True)
            if not terms["term_years"]:
                continue
            start_s, end_s = resolve_seasons(
                st["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
                terms["is_elc"], "extension" in hl.lower(), bio
            )
            if not start_s:
                continue
            parsed_contracts.append({
                "source": "nhl.com",
                "url": st["url"],
                "date": st["date"],
                "headline": st["headline"],
                "start_season": start_s,
                "end_season": end_s,
                "start_yr": int(start_s.split("-")[0]),
                "end_yr": int(end_s.split("-")[0]),
                "terms": terms,
            })

        # Check if we already have an active 2026-27 contract WITH AAV from NHL.com
        has_active_nhl_aav = any(
            c["start_yr"] <= CURRENT_START_YEAR <= c["end_yr"] and c["terms"]["aav_usd"] is not None
            for c in parsed_contracts
        )

        # Layer 2: Query ProHockeyRumors player RSS if missing active contract OR missing AAV
        if not has_active_nhl_aav:
            phr_items = fetch_phr_player_articles(p["name"], max_pages=4)
            phr_sign_cands = [
                it for it in phr_items
                if SIGN_HL_RE.search(it["title"])
                and last_re.search(it["title"])
                and not EXCLUDE_SIGN_HL_RE.search(it["title"])
                and not BUYOUT_RE.search(it["title"])
            ]
            for it in phr_sign_cands[:4]:
                try:
                    body = fetch_phr_full_body(it["url"])
                except Exception:
                    continue
                if not last_re.search(body):
                    continue
                terms = parse_contract_text(body, p["last"], is_nhl_official=False)
                if not terms["term_years"]:
                    continue
                start_s, end_s = resolve_seasons(
                    it["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
                    terms["is_elc"], "extension" in it["title"].lower() or "extend" in it["title"].lower(), bio
                )
                if not start_s:
                    continue
                parsed_contracts.append({
                    "source": "prohockeyrumors.com",
                    "url": it["url"],
                    "date": it["date"],
                    "headline": it["title"],
                    "start_season": start_s,
                    "end_season": end_s,
                    "start_yr": int(start_s.split("-")[0]),
                    "end_yr": int(end_s.split("-")[0]),
                    "terms": terms,
                })

        # Pick best ACTIVE 2026-27 contract (start_yr <= 2026 <= end_yr) and optional FUTURE extension (start_yr >= 2027)
        active_cands = [c for c in parsed_contracts if c["start_yr"] <= CURRENT_START_YEAR <= c["end_yr"]]
        future_cands = [c for c in parsed_contracts if c["start_yr"] > CURRENT_START_YEAR]

        # Sort preference: has AAV > official nhl.com > most recent date
        def rank_cand(c):
            return (c["terms"]["aav_usd"] is not None, c["source"] == "nhl.com", c["date"])

        active_cands.sort(key=rank_cand, reverse=True)
        future_cands.sort(key=rank_cand, reverse=True)

        best_active = active_cands[0] if active_cands else None
        best_future = future_cands[0] if future_cands else None

        # Attach official NHL.com URL as secondary if best_active came from PHR
        official_url = None
        if best_active and best_active["source"] != "nhl.com":
            nhl_same = [c for c in active_cands if c["source"] == "nhl.com"]
            if nhl_same:
                official_url = nhl_same[0]["url"]

        nhl_trades.sort(reverse=True)
        best_trade = None
        if nhl_trades:
            try:
                st = fetch_story_full(nhl_trades[0][1])
                if last_re.search(st["body"]):
                    best_trade = {"date": st["date"], "headline": st["headline"], "url": st["url"]}
            except Exception:
                pass

        return {
            "player": p,
            "birth_date": bio.get("birthDate"),
            "active": best_active,
            "official_nhl_url": official_url,
            "future_extension": best_future,
            "trade": best_trade,
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(resolve_player, players))

    return {"team": team_code, "roster_count": len(players), "results": results}


def main() -> None:
    teams = sys.argv[1:] if len(sys.argv) > 1 else ["TOR", "SJS", "NYI"]
    out_dir = Path(r"C:\Users\larss\.gemini\antigravity\brain\c2554015-4821-428d-a6a2-f59d894018e7\scratch\discovery_v2")
    out_dir.mkdir(parents=True, exist_ok=True)

    for tm in teams:
        print(f"\n================ {tm} (2-LAYER AUTO) ================")
        res = run_team(tm)
        (out_dir / f"{tm.lower()}_v2.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

        n = res["roster_count"]
        ok_aav = 0
        ok_elc = 0
        gaps = []
        for r in res["results"]:
            p = r["player"]
            a = r["active"]
            f = r["future_extension"]
            ext_note = f" [+EXT {f['terms']['term_years']}y ${f['terms']['aav_usd']:,} {f['start_season']}]" if (f and f["terms"]["aav_usd"]) else ""
            if a and a["terms"]["aav_usd"]:
                ok_aav += 1
                src_tag = "NHL" if a["source"] == "nhl.com" else "PHR"
                print(
                    f"  [OK  {src_tag}] {p['name']:22} {p['pos']:1} | {a['terms']['term_years']}y | "
                    f"${a['terms']['aav_usd']:>10,} | {a['start_season']}..{a['end_season']}{ext_note}"
                )
            elif a and a["terms"]["is_elc"]:
                ok_elc += 1
                print(
                    f"  [ELC    ] {p['name']:22} {p['pos']:1} | {a['terms']['term_years']}y | "
                    f"  [ELC n/a] | {a['start_season']}..{a['end_season']}{ext_note}"
                )
            else:
                gaps.append(p["name"])
                print(f"  [GAP    ] {p['name']:22} {p['pos']:1} ({p['id']}){ext_note}")

        print(f"--> {tm}: {ok_aav}/{n} active w/ AAV, {ok_elc}/{n} ELC w/o $, {len(gaps)}/{n} gaps")


if __name__ == "__main__":
    main()
