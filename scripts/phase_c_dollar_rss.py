"""Phase C: Fast 15-second dollar-focused Google News RSS pass (Sportsnet/TSN/DFO/NHL/ESPN/THN)
ONLY for the remaining players missing a public AAV after Phases A & B.

Zero ProHockeyRumors calls. Zero cap database sites.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
PB = ROOT / "cache" / "phase_b"
PC = ROOT / "cache" / "phase_c"

ALLOWED_OUTLETS = (
    "sportsnet.ca",
    "tsn.ca",
    "dailyfaceoff.com",
    "nhl.com",
    "espn.com",
    "thehockeynews.com",
    "thehockeywriters.com",
    "prohockeyrumors.com",
    "cbssports.com",
    "nbcsports.com",
)


def ascii_fold(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")


def search_dollar_rss(p: dict, bio: dict, team_code: str) -> dict | None:
    ascii_name = ascii_fold(p["name"])
    ascii_last = ascii_fold(p["last"])
    q = (
        f'"{ascii_name}" ("million" OR "AAV" OR "cap hit" OR "entry-level" OR "M deal" OR "K deal") '
        f'(site:sportsnet.ca OR site:tsn.ca OR site:dailyfaceoff.com OR site:nhl.com OR site:espn.com OR site:thehockeynews.com)'
    )
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": q, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    )
    xml_txt = dv3.fetch_cached(url, timeout=12)
    if not xml_txt:
        return None
    try:
        root = ET.fromstring(xml_txt)
    except Exception:
        return None

    last_re = re.compile(r"\b" + re.escape(ascii_last) + r"\b", re.I)
    months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
    cands = []

    for it in root.findall(".//item")[:25]:
        raw_title = it.findtext("title") or ""
        src_el = it.find("source")
        src_url = (src_el.attrib.get("url", "") if src_el is not None else "").lower()
        src_name = (src_el.text or "") if src_el is not None else ""
        if not any(dom in src_url for dom in ALLOWED_OUTLETS):
            continue
        title = re.sub(r"\s+-\s+[^-]+$", "", raw_title).strip()
        ftitle = ascii_fold(title)
        if not last_re.search(ftitle) or dv3.BUYOUT_RE.search(ftitle):
            continue
        if not dv3.SIGN_HL_RE.search(ftitle) or dv3.EXCLUDE_SIGN_HL_RE.search(ftitle):
            continue

        pub = it.findtext("pubDate") or ""
        dt = ""
        m_d = re.search(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
        if m_d:
            dt = f"{m_d.group(3)}-{months.get(m_d.group(2),'01')}-{m_d.group(1)}"

        terms = dv3.parse_contract_text(ftitle, ascii_last, is_nhl_official=False)
        if not terms["term_years"]:
            continue
        if terms["aav_usd"] is None:
            m_m = re.search(r"\$([\d,]+(?:\.\d+)?)\s*(million\b|MM\b|M\b|K\b)?", ftitle, re.I)
            if m_m:
                val = dv3.parse_money(m_m.group(1), m_m.group(2))
                ty = terms["term_years"]
                if ty and 700_000 * ty <= val <= 25_000_000 * ty:
                    terms["total_value_usd"] = val
                    terms["aav_usd"] = int(round(val / ty))
                elif 700_000 <= val <= 25_000_000:
                    terms["aav_usd"] = val
                    terms["total_value_usd"] = val * ty

        if terms["aav_usd"] is None:
            continue

        start_s, end_s = dv3.resolve_seasons(
            dt, terms["term_years"], terms["explicit_start"], terms["explicit_end"],
            terms["is_elc"], "extension" in ftitle.lower() or "extend" in ftitle.lower(), bio
        )
        if not start_s:
            continue
        sy, ey = int(start_s.split("-")[0]), int(end_s.split("-")[0])
        if not (sy <= dv3.CURRENT_START_YEAR <= ey):
            continue

        cands.append({
            "source": src_name or src_url,
            "source_domain": src_url,
            "url": it.findtext("link") or "",
            "date": dt,
            "headline": title,
            "signing_team": dv3.infer_signing_team_from_text(ftitle, ftitle, [], team_code),
            "start_season": start_s,
            "end_season": end_s,
            "start_yr": sy,
            "end_yr": ey,
            "terms": terms,
        })

    if not cands:
        return None
    cands.sort(key=lambda c: (c["date"], "nhl.com" in c["source_domain"] or "sportsnet" in c["source_domain"]), reverse=True)
    return cands[0]


def main() -> None:
    PC.mkdir(parents=True, exist_ok=True)
    teams_28 = sorted(p.stem.replace("_fast", "").upper() for p in PB.glob("*_fast.json"))

    tasks = []
    for tm in teams_28:
        fast = json.loads((PB / f"{tm.lower()}_fast.json").read_text(encoding="utf-8"))
        enr_p = PB / f"{tm.lower()}_enriched.json"
        enr_map = {}
        if enr_p.exists():
            enr_map = {r["player"]["id"]: r for r in json.loads(enr_p.read_text(encoding="utf-8"))["results"]}
        for r in fast["results"]:
            pid = r["player"]["id"]
            a_f = r.get("active")
            e_r = enr_map.get(pid, {})
            a_e = e_r.get("active")
            ddh = e_r.get("deep_dive_hit")
            has_aav = bool((a_f and a_f["terms"]["aav_usd"]) or (a_e and a_e["terms"]["aav_usd"]) or ddh)
            if not has_aav:
                tasks.append((tm, r["player"]))

    print(f"Running dollar-focused editorial RSS pass on {len(tasks)} remaining players...", flush=True)

    def work(item):
        tm, p = item
        bio = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/player/{p['id']}/landing") or {}
        hit = search_dollar_rss(p, bio, tm)
        return tm, p["id"], p["name"], hit

    found = {}
    with ThreadPoolExecutor(max_workers=10) as ex:
        for tm, pid, name, hit in ex.map(work, tasks):
            if hit:
                found[str(pid)] = {"team": tm, "player_name": name, "contract": hit}
                print(
                    f"  [HIT] {tm} {name:22} | {hit['terms']['term_years']}y | "
                    f"${hit['terms']['aav_usd']:>10,} | {hit['start_season']}..{hit['end_season']} | {hit['source']}",
                    flush=True,
                )

    (PC / "dollar_rss_hits.json").write_text(json.dumps(found, indent=2), encoding="utf-8")
    print(f"\nPhase C resolved {len(found)}/{len(tasks)} additional players with public AAV!", flush=True)


if __name__ == "__main__":
    main()
