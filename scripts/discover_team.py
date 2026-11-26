"""Reusable automated NHL contract & trade discovery engine.

Discovers active contracts and acquisitions for any NHL team using ONLY:
  1. Official NHL Web API (api-web.nhle.com)
  2. Official NHL.com Forge API (forge-dapi.d3.nhle.com)

No cap/contract aggregator sites are queried.
Outputs raw automated discovery candidates + parsed contracts per team so
any remaining gaps (e.g. ELC dollars, undisclosed press releases, cap penalties)
can be supplemented cleanly via editorial articles (Sportsnet, TSN, PHR).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) OpenNHLContracts/0.3"}

CURRENT_SEASON = "2026-27"
CURRENT_START_YEAR = 2026
CURRENT_SEASON_INT = 20262027

# Official NHL teamId + nhl.com team path + Forge team-created tag prefix
TEAM_META = {
    "ANA": (24, "ducks", "ana"),
    "BOS": (6, "bruins", "bos"),
    "BUF": (7, "sabres", "buf"),
    "CGY": (20, "flames", "cgy"),
    "CAR": (12, "hurricanes", "car"),
    "CHI": (16, "blackhawks", "chi"),
    "COL": (21, "avalanche", "col"),
    "CBJ": (29, "bluejackets", "cbj"),
    "DAL": (25, "stars", "dal"),
    "DET": (17, "redwings", "det"),
    "EDM": (22, "oilers", "edm"),
    "FLA": (13, "panthers", "fla"),
    "LAK": (26, "kings", "lak"),
    "MIN": (30, "wild", "min"),
    "MTL": (8, "canadiens", "mtl"),
    "NSH": (18, "predators", "nsh"),
    "NJD": (1, "devils", "njd"),
    "NYI": (2, "islanders", "nyi"),
    "NYR": (3, "rangers", "nyr"),
    "OTT": (9, "senators", "ott"),
    "PHI": (4, "flyers", "phi"),
    "PIT": (5, "penguins", "pit"),
    "SJS": (28, "sharks", "sjs"),
    "SEA": (55, "kraken", "sea"),
    "STL": (19, "blues", "stl"),
    "TBL": (14, "lightning", "tbl"),
    "TOR": (10, "mapleleafs", "tor"),
    "UTA": (68, "utah", "uta"),
    "VAN": (23, "canucks", "van"),
    "VGK": (54, "goldenknights", "vgk"),
    "WSH": (15, "capitals", "wsh"),
    "WPG": (52, "jets", "wpg"),
}

TEAM_TAG_TO_PATH = {f"{tag}-created": path for _, path, tag in TEAM_META.values()}
TEAM_ID_TO_ABBREV = {tid: abbr for abbr, (tid, _, _) in TEAM_META.items()}

WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8,
}

SIGNING_RE = re.compile(
    r"\b(sign|signs|signed|signing|re-sign|re-signs|re-signed|agree|agrees|agreed|contract|extension|extends|extended|entry-level|elc|deal)\b",
    re.I,
)
TRADE_WAIVER_RE = re.compile(
    r"\b(trade|traded|trades|acquire|acquires|acquired|claim|claimed|waivers|offer sheet)\b",
    re.I,
)
EXCLUDE_HL_RE = re.compile(
    r"\b(recall|recalled|loan|loaned|assign|assigned|activate|activated|placed on ir|injured reserve|lineup|recap|preview|mailbag|fantasy|power rankings|projected|halloween|watch|podcast|buzz|rumor|rumour|could|might|open to|talks|top priority|no rush|hopes to|ready to|proud to|grateful)\b",
    re.I,
)


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def clean_html(raw: str) -> str:
    raw = re.sub(r"<script[^>]*>.*?</script>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<style[^>]*>.*?</style>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<forge-entity[^>]*>(.*?)</forge-entity>", r"\1", raw)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", raw).strip()


def parse_money(val_str: str, unit: str | None) -> int:
    num = float(val_str.replace(",", ""))
    if unit and unit.lower().startswith("m"):
        return int(round(num * 1_000_000))
    return int(round(num))


def forge_web_url(slug: str, tags: list[str]) -> str:
    if "nhl-created" not in tags:
        for t in tags:
            if t in TEAM_TAG_TO_PATH:
                return f"https://www.nhl.com/{TEAM_TAG_TO_PATH[t]}/news/{slug}"
    return f"https://www.nhl.com/news/{slug}"


def fetch_forge_pages(tag_expr: str, max_pages: int = 8) -> list[dict]:
    items: list[dict] = []
    seen: set[str] = set()
    for page in range(max_pages):
        url = (
            f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories"
            f"?tags.slug={tag_expr}&$skip={page * 25}&$limit=25"
        )
        try:
            d = fetch_json(url)
        except Exception:
            break
        batch = d.get("items", [])
        if not batch:
            break
        new_c = 0
        for it in batch:
            sl = it.get("slug", "")
            if sl and sl not in seen:
                seen.add(sl)
                items.append(it)
                new_c += 1
        if new_c == 0 or len(batch) < 25:
            break
    return items


def fetch_story_full(slug: str) -> dict:
    d = fetch_json(f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}")
    tags = [t.get("slug", "") for t in d.get("tags", [])]
    body = " ".join(
        clean_html(p["content"])
        for p in d.get("parts", [])
        if p.get("type") == "markdown" and p.get("content")
    )
    return {
        "slug": slug,
        "date": (d.get("contentDate") or "")[:10],
        "headline": d.get("headline") or d.get("title") or "",
        "tags": tags,
        "url": forge_web_url(slug, tags),
        "body": body,
    }


def extract_terms(body: str, player_last: str) -> dict:
    """Extract contract term, AAV, total value, ELC/two-way flags, and start hint."""
    # Focus around the first mention of the player's last name if multi-player article
    idx = body.lower().find(player_last.lower())
    window = body[max(0, idx - 120) : idx + 1100] if idx != -1 else body[:1200]

    term_years = None
    m_term = re.search(
        r"\b(one|two|three|four|five|six|seven|eight|[1-8])[\s-]year\b",
        window,
        re.I,
    )
    if m_term:
        tok = m_term.group(1).lower()
        term_years = WORD_NUMS.get(tok, int(tok) if tok.isdigit() else None)

    aav = None
    total_value = None
    m_aav = re.search(
        r"(?:average annual value(?:\s*\(AAV\))?|AAV|cap hit)\s+(?:of\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|M\b)?",
        window,
        re.I,
    )
    if m_aav:
        aav = parse_money(m_aav.group(1), m_aav.group(2))

    m_total = re.search(
        r"(?:one|two|three|four|five|six|seven|eight|[1-8])[\s-]year,\s+(?:two-way\s+|one-way\s+)?\$([\d,]+(?:\.\d+)?)\s*(million\b|M\b)?",
        window,
        re.I,
    )
    if m_total:
        total_value = parse_money(m_total.group(1), m_total.group(2))

    # Alternate pattern: "$X million contract" / "worth $X"
    if not aav and not total_value:
        m_worth = re.search(
            r"(?:worth|totaling|valued at)\s+\$([\d,]+(?:\.\d+)?)\s*(million\b|M\b)?",
            window,
            re.I,
        )
        if m_worth:
            val = parse_money(m_worth.group(1), m_worth.group(2))
            if term_years and val >= 700_000 * term_years:
                total_value = val
            elif 700_000 <= val <= 25_000_000:
                aav = val

    if term_years and total_value and not aav:
        aav = int(round(total_value / term_years))
    elif term_years and aav and not total_value:
        total_value = aav * term_years

    # Discard nonsensical parsed numbers
    if aav is not None and not (700_000 <= aav <= 25_000_000):
        aav = None
        total_value = None

    is_elc = bool(re.search(r"\bentry[\s-]level\b", window, re.I))
    two_way = bool(re.search(r"\btwo[\s-]way\b", window, re.I))

    # Detect explicit start season ("begins with the 2026-27 season" / "starting in 2025-26" / "begins next season")
    explicit_start = None
    m_start = re.search(
        r"(?:begins|begin|starts|starting|commencing|kicks in)\s+(?:with\s+the\s+|in\s+(?:the\s+)?)?(20\d{2}-\d{2})",
        window,
        re.I,
    )
    if m_start:
        explicit_start = m_start.group(1)
    elif re.search(r"(?:begins|starts|starting)\s+next\s+season", window, re.I):
        explicit_start = "NEXT_SEASON"

    # Signing status hints
    status_hint = None
    if is_elc:
        status_hint = "ELC"
    elif re.search(r"\brestricted free agent\b|\bRFA\b", window, re.I):
        status_hint = "RFA"
    elif re.search(r"\bunrestricted free agent\b|\bUFA\b", window, re.I):
        status_hint = "UFA"

    sentences = re.split(r"(?<=[.!?])\s+", window)
    quote = " ".join(sentences[:2]).strip()[:360]

    return {
        "term_years": term_years,
        "aav_usd": aav,
        "total_value_usd": total_value,
        "is_elc": is_elc,
        "two_way": two_way,
        "explicit_start": explicit_start,
        "status_hint": status_hint,
        "quote": quote,
    }


def infer_seasons(signed_date: str, term_years: int | None, explicit_start: str | None, is_elc: bool, bio: dict) -> tuple[str | None, str | None]:
    if not signed_date or not term_years:
        return None, None
    sy, sm, _ = map(int, signed_date.split("-"))

    if explicit_start and explicit_start != "NEXT_SEASON":
        start_yr = int(explicit_start.split("-")[0])
    elif explicit_start == "NEXT_SEASON":
        # Signed in-season (e.g. Oct 2025 -> starts 2026-27) or spring (e.g. Mar 2025 -> starts 2025-26)
        start_yr = sy + 1 if sm >= 7 else sy
    elif is_elc:
        # Check ELC slides using official NHL API birthDate + NHL GP
        by, bm, bd = map(int, bio["birthDate"].split("-"))
        nhl_gp: dict[int, int] = {}
        for st in bio.get("seasonTotals", []):
            if st.get("gameTypeId") == 2 and st.get("leagueAbbrev") == "NHL":
                nhl_gp[st["season"]] = nhl_gp.get(st["season"], 0) + (st.get("gamesPlayed", 0) or 0)
        # First eligible season
        cand = sy if sm >= 6 else sy - 1
        # If signed in March/April and played NHL games that spring, burn Year 1; otherwise starts next fall
        if sm <= 5:
            spring_s = (sy - 1) * 10000 + sy
            if nhl_gp.get(spring_s, 0) > 0:
                cand = sy - 1
            else:
                cand = sy
        # Apply up to 2 junior slides if age 18/19 on Sept 15 of start year and < 10 NHL GP
        slides = 0
        while slides < 2 and cand < CURRENT_START_YEAR:
            age_sep15 = cand - by - ((9, 15) < (bm, bd))
            season_int = cand * 10000 + (cand + 1)
            if age_sep15 <= 19 and nhl_gp.get(season_int, 0) < 10:
                cand += 1
                slides += 1
            else:
                break
        start_yr = cand
    else:
        # Standard signing/extension:
        # June 15..Dec 31 -> if signed June 15..Oct 15 as FA/RFA -> starts sy-(sy+1) unless extension
        # Jan..June 14 -> extension for upcoming season sy-(sy+1)
        start_yr = sy

    end_yr = start_yr + term_years - 1
    return f"{start_yr}-{str(start_yr + 1)[-2:]}", f"{end_yr}-{str(end_yr + 1)[-2:]}"


def discover_team(team_code: str) -> dict:
    team_code = team_code.upper()
    tid, team_path, tag_prefix = TEAM_META[team_code]
    roster_raw = fetch_json(f"https://api-web.nhle.com/v1/roster/{team_code}/current")

    players = []
    for grp in ("forwards", "defensemen", "goalies"):
        for p in roster_raw[grp]:
            players.append({
                "id": p["id"],
                "name": f"{p['firstName']['default']} {p['lastName']['default']}",
                "first": p["firstName"]["default"],
                "last": p["lastName"]["default"],
                "pos": p["positionCode"],
                "sweater": p.get("sweaterNumber"),
            })

    # 1. Fetch player bios in parallel
    def get_bio(p):
        return p["id"], fetch_json(f"https://api-web.nhle.com/v1/player/{p['id']}/landing")

    with ThreadPoolExecutor(max_workers=10) as ex:
        bios = dict(ex.map(get_bio, players))

    # 2. Build team-wide Forge story pool + NHL Free Agent Trackers (2026-27, 2025-26)
    pool: dict[str, dict] = {}
    tag_queries = [
        (f"{tag_prefix}-created,transactions", 12),
        (f"teamid-{tid},free-agency", 10),
        (f"teamid-{tid},transactions", 10),
    ]
    for expr, pages in tag_queries:
        for it in fetch_forge_pages(expr, max_pages=pages):
            pool[it["slug"]] = it

    # 3. Also pull player-specific Forge stories (free-agency, transactions, and general playerid up to 3 pages)
    def scan_pid(p):
        pid = p["id"]
        found = []
        for expr, pages in [
            (f"playerid-{pid},free-agency", 2),
            (f"playerid-{pid},transactions", 2),
            (f"playerid-{pid}", 3),
        ]:
            found.extend(fetch_forge_pages(expr, max_pages=pages))
        return found

    with ThreadPoolExecutor(max_workers=10) as ex:
        for batch in ex.map(scan_pid, players):
            for it in batch:
                pool[it["slug"]] = it

    # 4. Also parse NHL.com 2026-27 Free Agent Tracker for this team's signing links
    tracker_links: list[tuple[str, str]] = []
    try:
        tr = fetch_story_full("free-agency-signings-nhl-2026-27")
        # Extract raw markdown from Forge for link extraction
        tr_raw = fetch_json("https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/free-agency-signings-nhl-2026-27")
        md = "\n".join(p.get("content", "") for p in tr_raw.get("parts", []) if p.get("type") == "markdown")
        for m in re.finditer(r"\[([^\]]+)\]\((https://www\.nhl\.com/[^)]+)\)", md):
            tracker_links.append((m.group(1), m.group(2)))
    except Exception:
        pass

    # 5. Match candidate signing & trade stories to each player
    results = []
    for p in players:
        pid = p["id"]
        bio = bios[pid]
        last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)

        signing_cands = []
        trade_cands = []

        # Check tracker links first
        for label, url in tracker_links:
            if last_re.search(label) and "sign" in label.lower():
                m_sl = re.search(r"/news/(?:topic/[^/]+/)?([^/?#]+)$", url)
                if m_sl:
                    sl = m_sl.group(1)
                    if sl not in pool:
                        pool[sl] = {
                            "slug": sl,
                            "headline": label,
                            "contentDate": "2026-07-01",
                            "tags": ["free-agency"],
                        }

        for sl, it in pool.items():
            hl = it.get("headline") or it.get("title") or ""
            tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
            if "game-recap" in tags:
                continue
            has_pid = f"playerid-{pid}" in tags
            has_name_hl = bool(last_re.search(hl))
            if not (has_pid or has_name_hl):
                continue
            if EXCLUDE_HL_RE.search(hl) and not re.search(r"\bsign|re-sign|extension|entry-level|trade|acquir|claim", hl, re.I):
                continue
            dt = (it.get("contentDate") or "")[:10]
            score = 0
            if has_name_hl:
                score += 5
            if has_pid:
                score += 3
            if re.search(r"\b(signs|signed|re-sign|extension|entry-level)\b", hl, re.I):
                score += 6
            if re.search(r"\$|\bmillion\b|\byear\b", hl, re.I):
                score += 4
            if "release" in hl.lower():
                score += 2
            if SIGNING_RE.search(hl):
                signing_cands.append((dt, score, sl, hl))
            if TRADE_WAIVER_RE.search(hl):
                trade_cands.append((dt, score, sl, hl))

        # Prefer most recent strong signing candidate; fetch top 3 to find one with valid terms covering 2026-27
        signing_cands.sort(key=lambda x: (x[0][:4], x[1], x[0]), reverse=True)
        # Deduplicate by slug
        seen_s, top_s = set(), []
        for c in signing_cands:
            if c[2] not in seen_s:
                seen_s.add(c[2])
                top_s.append(c)
            if len(top_s) >= 4:
                break

        best_signing = None
        for dt, score, sl, hl in top_s:
            try:
                st = fetch_story_full(sl)
            except Exception:
                continue
            # Confirm player's full last name (and ideally first name) appears in body
            if not last_re.search(st["body"]):
                continue
            if p["first"].lower() not in st["body"].lower() and f"playerid-{pid}" not in st["tags"]:
                continue
            terms = extract_terms(st["body"], p["last"])
            if not terms["term_years"]:
                continue
            start_s, end_s = infer_seasons(st["date"], terms["term_years"], terms["explicit_start"], terms["is_elc"], bio)
            # Check if contract could plausibly be active in 2026-27 (end_yr >= 2026)
            end_yr = int(end_s.split("-")[0]) + 1 if end_s else 0
            cand_obj = {
                "slug": sl,
                "url": st["url"],
                "date": st["date"],
                "headline": st["headline"],
                "terms": terms,
                "start_season": start_s,
                "end_season": end_s,
                "active_2627": end_yr >= 2027,
            }
            if best_signing is None:
                best_signing = cand_obj
            else:
                # Prefer active_2627 + has AAV + more recent
                cur_key = (best_signing["active_2627"], best_signing["terms"]["aav_usd"] is not None, best_signing["date"])
                new_key = (cand_obj["active_2627"], cand_obj["terms"]["aav_usd"] is not None, cand_obj["date"])
                if new_key > cur_key:
                    best_signing = cand_obj

        trade_cands.sort(key=lambda x: (x[0], x[1]), reverse=True)
        best_trade = None
        if trade_cands:
            sl = trade_cands[0][2]
            try:
                st = fetch_story_full(sl)
                if last_re.search(st["body"]):
                    best_trade = {"slug": sl, "url": st["url"], "date": st["date"], "headline": st["headline"]}
            except Exception:
                pass

        results.append({
            "player": p,
            "birthDate": bio.get("birthDate"),
            "draftDetails": bio.get("draftDetails"),
            "signing": best_signing,
            "trade": best_trade,
            "signing_cands_count": len(signing_cands),
        })

    return {"team": team_code, "roster_count": len(players), "results": results}


def main() -> None:
    teams = sys.argv[1:] if len(sys.argv) > 1 else ["TOR", "SJS", "NYI"]
    out_dir = Path(r"C:\Users\larss\.gemini\antigravity\brain\c2554015-4821-428d-a6a2-f59d894018e7\scratch\discovery")
    out_dir.mkdir(parents=True, exist_ok=True)

    for tm in teams:
        print(f"\n================ {tm} ================")
        res = discover_team(tm)
        (out_dir / f"{tm.lower()}_auto.json").write_text(json.dumps(res, indent=2), encoding="utf-8")

        n = res["roster_count"]
        got_active_aav = 0
        got_active_elc = 0
        gaps = []
        for r in res["results"]:
            p = r["player"]
            s = r["signing"]
            if s and s["active_2627"] and s["terms"]["aav_usd"]:
                got_active_aav += 1
                print(
                    f"  [OK  ] {p['name']:22} {p['pos']:1} | {s['terms']['term_years']}y | "
                    f"${s['terms']['aav_usd']:>10,} | {s['start_season']}..{s['end_season']} | {s['slug'][:55]}"
                )
            elif s and s["active_2627"] and s["terms"]["is_elc"]:
                got_active_elc += 1
                print(
                    f"  [ELC ] {p['name']:22} {p['pos']:1} | {s['terms']['term_years']}y | "
                    f"  [ELC n/a] | {s['start_season']}..{s['end_season']} | {s['slug'][:55]}"
                )
            else:
                reason = "no_story" if not s else f"stale/no_aav({s['date']} {s['start_season']}..{s['end_season']} aav={s['terms']['aav_usd']})"
                gaps.append((p["name"], p["id"], reason))
                print(f"  [GAP ] {p['name']:22} {p['pos']:1} ({p['id']}) | {reason}")

        print(
            f"--> {tm} AUTO-DISCOVERY: {got_active_aav}/{n} active w/ AAV, "
            f"{got_active_elc}/{n} active ELCs (no $ in release), {len(gaps)}/{n} gaps"
        )


if __name__ == "__main__":
    main()
