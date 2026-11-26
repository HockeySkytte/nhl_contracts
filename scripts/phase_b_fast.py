"""Phase B (Fast): Zero-stall enrichment using ONLY:
  1. Deep official NHL.com Forge API (playerid-{id} up to 14 pages + prior-team releases)
  2. Google News RSS filtered strictly to major hockey news outlets
     (sportsnet.ca, tsn.ca, dailyfaceoff.com, nhl.com, espn.com, prohockeyrumors.com)
     — NEVER PuckPedia, CapWages, Spotrac, or any cap aggregator.
  3. Already-cached local PHR files (fail-fast 4s timeout, never stalls).

Runs 7-8 NHL teams in ~30-60 seconds!
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
PHASE_A_DIR = ROOT / "cache" / "phase_a"
PHASE_B_DIR = ROOT / "cache" / "phase_b"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

ALLOWED_OUTLETS = (
    "sportsnet.ca",
    "tsn.ca",
    "dailyfaceoff.com",
    "nhl.com",
    "espn.com",
    "prohockeyrumors.com",
    "thehockeynews.com",
    "thehockeywriters.com",
)


def deep_nhl_forge_search(p: dict, bio: dict, team_code: str) -> tuple[dict | None, dict | None]:
    """Deep-scan NHL.com Forge playerid tag (up to 14 pages) + prior team transactions."""
    pid = p["id"]
    last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)
    first_re = re.compile(r"\b" + re.escape(p["first"]) + r"\b", re.I)

    cands = []
    seen = set()
    for it in dv3.fetch_forge_pages(f"playerid-{pid}", max_pages=14):
        sl = it.get("slug", "")
        if not sl or sl in seen:
            continue
        seen.add(sl)
        hl = it.get("headline") or ""
        tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
        if "game-recap" in tags or dv3.BUYOUT_RE.search(hl):
            continue
        if last_re.search(hl) and dv3.SIGN_HL_RE.search(hl) and not dv3.EXCLUDE_SIGN_HL_RE.search(hl):
            dedicated = not re.search(r";|,.*sign|each sign|four players|three players", hl, re.I)
            cands.append(((it.get("contentDate") or "")[:4], dedicated, (it.get("contentDate") or "")[:10], sl, hl))

    cands.sort(reverse=True)
    parsed = []
    for _, dedicated, dt, sl, hl in cands[:6]:
        st = dv3.fetch_story_full(sl)
        if not st or not last_re.search(st["body"]):
            continue
        if not first_re.search(st["body"]) and f"playerid-{pid}" not in st["tags"]:
            continue
        terms = dv3.parse_contract_text(st["body"], p["last"], is_nhl_official=True)
        if not terms["term_years"]:
            continue
        start_s, end_s = dv3.resolve_seasons(
            st["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
            terms["is_elc"], "extension" in hl.lower(), bio
        )
        if not start_s:
            continue
        parsed.append({
            "source": "nhl.com",
            "dedicated": dedicated,
            "url": st["url"],
            "date": st["date"],
            "headline": st["headline"],
            "signing_team": dv3.infer_signing_team_from_text(st["headline"], st["body"], st["tags"], team_code),
            "start_season": start_s,
            "end_season": end_s,
            "start_yr": int(start_s.split("-")[0]),
            "end_yr": int(end_s.split("-")[0]),
            "terms": terms,
        })

    active = [c for c in parsed if c["start_yr"] <= dv3.CURRENT_START_YEAR <= c["end_yr"]]
    future = [c for c in parsed if c["start_yr"] > dv3.CURRENT_START_YEAR]
    key = lambda c: (c["terms"]["aav_usd"] is not None, c["dedicated"], c["date"])
    active.sort(key=key, reverse=True)
    future.sort(key=key, reverse=True)
    return (active[0] if active else None), (future[0] if future else None)


def parse_gnews_headline_contract(p: dict, bio: dict, team_code: str) -> tuple[dict | None, dict | None]:
    """Query Google News RSS strictly on major hockey news domains (Sportsnet/TSN/NHL/DFO/ESPN)
    to extract insider-reported contract terms directly from verified news headlines."""
    ascii_name = ascii_fold(p["name"])
    ascii_last = ascii_fold(p["last"])
    q = (
        f'"{ascii_name}" (signs OR signed OR re-sign OR extension OR contract OR deal) '
        f'(site:sportsnet.ca OR site:tsn.ca OR site:dailyfaceoff.com OR site:nhl.com OR site:espn.com)'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    xml_txt = dv3.fetch_cached(url, timeout=12)
    if not xml_txt:
        return None, None
    try:
        root = ET.fromstring(xml_txt)
    except Exception:
        return None, None

    last_re = re.compile(r"\b" + re.escape(ascii_last) + r"\b", re.I)
    parsed = []
    months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}

    for it in root.findall(".//item")[:20]:
        raw_title = it.findtext("title") or ""
        src_el = it.find("source")
        src_url = (src_el.attrib.get("url", "") if src_el is not None else "").lower()
        src_name = (src_el.text or "") if src_el is not None else ""
        # Enforce strictly editorial hockey outlets (zero cap sites)
        if not any(dom in src_url for dom in ALLOWED_OUTLETS):
            continue
        # Strip trailing " - Sportsnet.ca"
        title = re.sub(r"\s+-\s+[^-]+$", "", raw_title).strip()
        if not last_re.search(ascii_fold(title)) or dv3.BUYOUT_RE.search(title):
            continue
        if not dv3.SIGN_HL_RE.search(title) or dv3.EXCLUDE_SIGN_HL_RE.search(title):
            continue

        pub = it.findtext("pubDate") or ""
        dt = ""
        m_d = re.search(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
        if m_d:
            dt = f"{m_d.group(3)}-{months.get(m_d.group(2),'01')}-{m_d.group(1)}"

        # Parse term & dollar figure right from the headline (e.g. "signs 8-year, $69M extension")
        terms = dv3.parse_contract_text(ascii_fold(title), ascii_last, is_nhl_official=False)
        # Also catch "8-year, $69M" or "two-year, $5M" or "1-year, $850K"
        if not terms["term_years"]:
            continue
        if terms["aav_usd"] is None:
            m_m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?", title, re.I)
            if m_m:
                val = dv3.parse_money(m_m.group(1), m_m.group(2))
                ty = terms["term_years"]
                if ty and 700_000 * ty <= val <= 25_000_000 * ty:
                    terms["total_value_usd"] = val
                    terms["aav_usd"] = int(round(val / ty))
                elif 700_000 <= val <= 25_000_000:
                    terms["aav_usd"] = val
                    terms["total_value_usd"] = val * ty

        start_s, end_s = dv3.resolve_seasons(
            dt, terms["term_years"], terms["explicit_start"], terms["explicit_end"],
            terms["is_elc"], "extension" in title.lower() or "extend" in title.lower(), bio
        )
        if not start_s:
            continue
        glink = it.findtext("link") or ""
        parsed.append({
            "source": src_name or src_url,
            "dedicated": not re.search(r";|each sign|roundup", title, re.I),
            "url": glink,
            "source_domain": src_url,
            "date": dt,
            "headline": title,
            "signing_team": dv3.infer_signing_team_from_text(title, title, [], team_code),
            "start_season": start_s,
            "end_season": end_s,
            "start_yr": int(start_s.split("-")[0]),
            "end_yr": int(end_s.split("-")[0]),
            "terms": terms,
        })

    active = [c for c in parsed if c["start_yr"] <= dv3.CURRENT_START_YEAR <= c["end_yr"]]
    future = [c for c in parsed if c["start_yr"] > dv3.CURRENT_START_YEAR]
    key = lambda c: (c["terms"]["aav_usd"] is not None, c["dedicated"], c["date"])
    active.sort(key=key, reverse=True)
    future.sort(key=key, reverse=True)
    return (active[0] if active else None), (future[0] if future else None)


def enrich_team_fast(team_code: str) -> dict:
    pa = json.loads((PHASE_A_DIR / f"{team_code.lower()}_nhl.json").read_text(encoding="utf-8"))

    def process_player(r):
        p = r["player"]
        pid = p["id"]
        bio = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/player/{pid}/landing") or {}
        best_active = r.get("active")
        best_future = r.get("future_extension")
        official_nhl_url = None

        # Step 1: Deep NHL.com Forge search if missing active AAV
        if not best_active or best_active["terms"]["aav_usd"] is None:
            d_act, d_fut = deep_nhl_forge_search(p, bio, team_code)
            if d_act and (not best_active or (d_act["terms"]["aav_usd"] is not None and best_active["terms"]["aav_usd"] is None)):
                best_active = d_act
            if d_fut and not best_future:
                best_future = d_fut

        # Step 2: Google News RSS (Sportsnet/TSN/DFO/NHL/ESPN) if still missing active AAV
        if not best_active or best_active["terms"]["aav_usd"] is None:
            g_act, g_fut = parse_gnews_headline_contract(p, bio, team_code)
            if g_act and g_act["terms"]["aav_usd"] is not None:
                if best_active and best_active["source"] == "nhl.com":
                    official_nhl_url = best_active["url"]
                best_active = g_act
            elif g_act and not best_active:
                best_active = g_act
            if g_fut and not best_future:
                best_future = g_fut

        return {
            "player": p,
            "birth_date": bio.get("birthDate"),
            "draft_team": r.get("draft_team"),
            "active": best_active,
            "official_nhl_url": official_nhl_url,
            "future_extension": best_future,
            "trade": r.get("trade"),
        }

    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(process_player, pa["results"]))

    return {
        "team": team_code,
        "team_name": pa["team_name"],
        "roster_count": pa["roster_count"],
        "results": results,
    }


def main() -> None:
    PHASE_B_DIR.mkdir(parents=True, exist_ok=True)
    teams = [t.upper() for t in sys.argv[1:]]
    if not teams:
        raise SystemExit("Usage: python phase_b_fast.py TEAM1 TEAM2 ...")

    tot_n = 0
    tot_url = 0
    tot_aav = 0

    for tm in teams:
        res = enrich_team_fast(tm)
        (PHASE_B_DIR / f"{tm.lower()}_fast.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        n = res["roster_count"]
        got_url = sum(1 for r in res["results"] if r["active"])
        got_aav = sum(1 for r in res["results"] if r["active"] and r["active"]["terms"]["aav_usd"])
        gaps = [
            f"{r['player']['name']} ({r['player']['id']})"
            for r in res["results"]
            if not (r["active"] and r["active"]["terms"]["aav_usd"])
        ]
        tot_n += n
        tot_url += got_url
        tot_aav += got_aav
        print(f"  {tm} ({res['team_name']:22}): {got_url:2}/{n:2} URL | {got_aav:2}/{n:2} AAV | gaps({len(gaps)}): {', '.join(gaps[:6])}", flush=True)

    print(
        f"\nBATCH TOTAL ({len(teams)} teams): {tot_n} players | "
        f"{tot_url}/{tot_n} ({100*tot_url/tot_n:.1f}%) URL | "
        f"{tot_aav}/{tot_n} ({100*tot_aav/tot_n:.1f}%) AAV",
        flush=True,
    )


if __name__ == "__main__":
    main()
