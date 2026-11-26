"""Phase B: Polite, disk-cached editorial enrichment & Deep-Dive cross-checker.

Reads Phase A NHL.com outputs (cache/phase_a/{team}_nhl.json), enriches ONLY
players missing an active 2026-27 AAV (or flagged as a mismatch against the
team's 2026 editorial Salary Cap Deep Dive) using single-process, gently paced
ProHockeyRumors player RSS (/players/{slug}/feed) + Google News RSS fallback.

Strictly ZERO cap/contract database sites (PuckPedia, CapWages, Spotrac, etc.).
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

# Set polite single-process pacing for ProHockeyRumors
dv3.PHR_MIN_INTERVAL = 0.75

ROOT = Path(__file__).resolve().parent.parent
PHASE_A_DIR = ROOT / "cache" / "phase_a"
PHASE_B_DIR = ROOT / "cache" / "phase_b"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def find_or_fetch_deep_dive(team_code: str, cached_dd_map: dict[str, dict]) -> dict:
    """Return parsed 2026 PHR Salary Cap Deep Dive for a team (using cache first)."""
    if team_code in cached_dd_map and not cached_dd_map[team_code].get("error"):
        return cached_dd_map[team_code]
    full_name = dv3.TEAM_INFO[team_code][3]
    custom = {"STL": "st-louis-blues", "UTA": "utah-mammoth"}
    slug = custom.get(team_code, full_name.lower().replace(".", "").replace(" ", "-"))
    nums = (2, 3, 1) if team_code == "UTA" else ((6, 5, 7) if team_code == "SEA" else ((9, 10, 8) if team_code == "VGK" else (10, 9, 8)))
    for mo in ("08", "09", "10"):
        for num in nums:
            u = f"https://www.prohockeyrumors.com/2026/{mo}/salary-cap-deep-dive-{slug}-{num}.html"
            if dv3.fetch_cached(u):
                dd = dv3.parse_phr_deep_dive(u)
                cached_dd_map[team_code] = dd
                return dd
    return {"url": None, "players": {}, "dead_cap_lines": []}


def enrich_player_phr(p: dict, bio: dict, team_code: str, max_pages: int = 4) -> tuple[dict | None, dict | None]:
    """Return (best_active, best_future) parsed from ProHockeyRumors player RSS,
    stopping pagination early as soon as an active 2026-27 contract with AAV is found."""
    last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)
    slug = dv3.slugify_name(p["name"])
    parsed = []
    seen_urls = set()

    for page in range(1, max_pages + 1):
        u = f"https://www.prohockeyrumors.com/players/{slug}/feed" + (f"?paged={page}" if page > 1 else "")
        xml_txt = dv3.fetch_cached(u)
        if not xml_txt:
            break
        xml_txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", xml_txt)
        try:
            root = ET.fromstring(xml_txt)
        except Exception:
            break
        batch = root.findall(".//item")
        if not batch:
            break

        page_cands = []
        for it in batch:
            link = it.findtext("link") or ""
            if not link or link in seen_urls:
                continue
            seen_urls.add(link)
            title = it.findtext("title") or ""
            if (
                dv3.SIGN_HL_RE.search(title)
                and last_re.search(title)
                and not dv3.EXCLUDE_SIGN_HL_RE.search(title)
                and not dv3.BUYOUT_RE.search(title)
            ):
                pub = it.findtext("pubDate") or ""
                dt = ""
                m_d = re.search(r"(\d{2})\s+([A-Za-z]{3})\s+(\d{4})", pub)
                if m_d:
                    months = {"Jan":"01","Feb":"02","Mar":"03","Apr":"04","May":"05","Jun":"06","Jul":"07","Aug":"08","Sep":"09","Oct":"10","Nov":"11","Dec":"12"}
                    dt = f"{m_d.group(3)}-{months.get(m_d.group(2),'01')}-{m_d.group(1)}"
                page_cands.append({"date": dt, "title": title, "url": link})

        for it in page_cands[:3]:
            body = dv3.fetch_phr_full_body(it["url"])
            if not body or not last_re.search(body):
                continue
            terms = dv3.parse_contract_text(body, p["last"], is_nhl_official=False)
            if not terms["term_years"]:
                continue
            start_s, end_s = dv3.resolve_seasons(
                it["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
                terms["is_elc"], "extension" in it["title"].lower() or "extend" in it["title"].lower(), bio
            )
            if not start_s:
                continue
            signing_team = dv3.infer_signing_team_from_text(it["title"], body, [], team_code)
            dedicated = not re.search(r",|four players|three players|two-way signings|notes", it["title"], re.I)
            parsed.append({
                "source": "prohockeyrumors.com",
                "dedicated": dedicated,
                "url": it["url"],
                "date": it["date"],
                "headline": it["title"],
                "signing_team": signing_team,
                "start_season": start_s,
                "end_season": end_s,
                "start_yr": int(start_s.split("-")[0]),
                "end_yr": int(end_s.split("-")[0]),
                "terms": terms,
            })

        # Early stop once we have an active 2026-27 contract with AAV!
        if any(c["start_yr"] <= dv3.CURRENT_START_YEAR <= c["end_yr"] and c["terms"]["aav_usd"] for c in parsed):
            break
        if len(batch) < 15:
            break

    active = [c for c in parsed if c["start_yr"] <= dv3.CURRENT_START_YEAR <= c["end_yr"]]
    future = [c for c in parsed if c["start_yr"] > dv3.CURRENT_START_YEAR]
    key = lambda c: (c["terms"]["aav_usd"] is not None, c["dedicated"], c["date"])
    active.sort(key=key, reverse=True)
    future.sort(key=key, reverse=True)
    return (active[0] if active else None), (future[0] if future else None)


def deep_nhl_forge_fallback(p: dict, bio: dict, team_code: str) -> dict | None:
    """Deep-scan official NHL.com Forge playerid tag (pages 3..14) for older star extensions."""
    pid = p["id"]
    last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)
    cands = []
    for it in dv3.fetch_forge_pages(f"playerid-{pid}", max_pages=14):
        hl = it.get("headline") or ""
        tags = [t.get("slug", "") for t in it.get("tags", []) if isinstance(t, dict)]
        if "game-recap" in tags or dv3.BUYOUT_RE.search(hl):
            continue
        if last_re.search(hl) and dv3.SIGN_HL_RE.search(hl) and not dv3.EXCLUDE_SIGN_HL_RE.search(hl):
            cands.append(((it.get("contentDate") or "")[:10], it["slug"], hl))
    cands.sort(reverse=True)
    for dt, sl, hl in cands[:4]:
        st = dv3.fetch_story_full(sl)
        if not st or not last_re.search(st["body"]):
            continue
        terms = dv3.parse_contract_text(st["body"], p["last"], is_nhl_official=True)
        if not terms["term_years"] or not terms["aav_usd"]:
            continue
        start_s, end_s = dv3.resolve_seasons(
            st["date"], terms["term_years"], terms["explicit_start"], terms["explicit_end"],
            terms["is_elc"], "extension" in hl.lower(), bio
        )
        if not start_s:
            continue
        sy, ey = int(start_s.split("-")[0]), int(end_s.split("-")[0])
        if sy <= dv3.CURRENT_START_YEAR <= ey:
            return {
                "source": "nhl.com",
                "dedicated": True,
                "url": st["url"],
                "date": st["date"],
                "headline": st["headline"],
                "signing_team": dv3.infer_signing_team_from_text(st["headline"], st["body"], st["tags"], team_code),
                "start_season": start_s,
                "end_season": end_s,
                "start_yr": sy,
                "end_yr": ey,
                "terms": terms,
            }
    return None


def match_dd_player(p: dict, dd_players: dict) -> dict | None:
    slug = dv3.slugify_name(p["name"])
    if slug in dd_players:
        return dd_players[slug]
    last_slug = dv3.slugify_name(p["last"])
    hits = [v for k, v in dd_players.items() if k.endswith("-" + last_slug)]
    if len(hits) == 1:
        return hits[0]
    return None


def enrich_team(team_code: str, cached_dd_map: dict[str, dict]) -> dict:
    pa = json.loads((PHASE_A_DIR / f"{team_code.lower()}_nhl.json").read_text(encoding="utf-8"))
    dd = find_or_fetch_deep_dive(team_code, cached_dd_map)
    dd_players = dd.get("players", {})

    enriched_results = []
    audit_flags = []

    for r in pa["results"]:
        p = r["player"]
        pid = p["id"]
        bio = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/player/{pid}/landing") or {}
        nhl_active = r.get("active")
        nhl_future = r.get("future_extension")
        ddh = match_dd_player(p, dd_players)

        # Decide if we need PHR enrichment:
        # 1) No NHL.com active contract OR no AAV
        # 2) NHL.com AAV disagrees with Deep Dive by > $50,000 (catches multi-player article mis-parses!)
        need_phr = False
        if not nhl_active or nhl_active["terms"]["aav_usd"] is None:
            need_phr = True
        elif ddh and abs(nhl_active["terms"]["aav_usd"] - ddh["aav_usd"]) > 50_000:
            need_phr = True
            audit_flags.append({
                "player": p["name"],
                "reason": "nhl_vs_deepdive_mismatch",
                "nhl_aav": nhl_active["terms"]["aav_usd"],
                "dd_aav": ddh["aav_usd"],
                "nhl_url": nhl_active["url"],
            })

        phr_active, phr_future = (None, None)
        if need_phr:
            phr_active, phr_future = enrich_player_phr(p, bio, team_code, max_pages=4)

        # Choose best active contract:
        # If both exist and Deep Dive exists, prefer whichever matches Deep Dive closer!
        best_active = nhl_active
        official_nhl_url = None

        if nhl_active and phr_active:
            nhl_aav = nhl_active["terms"]["aav_usd"]
            phr_aav = phr_active["terms"]["aav_usd"]
            if nhl_aav is None and phr_aav is not None:
                best_active = phr_active
                official_nhl_url = nhl_active["url"]
            elif ddh and nhl_aav is not None and phr_aav is not None:
                if abs(phr_aav - ddh["aav_usd"]) < abs(nhl_aav - ddh["aav_usd"]):
                    best_active = phr_active
                    official_nhl_url = nhl_active["url"]
        elif phr_active and not nhl_active:
            best_active = phr_active

        # Deep official NHL.com Forge fallback if still missing active AAV (catches older star extensions!)
        if not best_active or best_active["terms"]["aav_usd"] is None:
            deep_nhl = deep_nhl_forge_fallback(p, bio, team_code)
            if deep_nhl:
                best_active = deep_nhl

        # Reconcile with Deep Dive if ELC or still missing AAV or still mismatched
        if ddh and best_active:
            cur_aav = best_active["terms"]["aav_usd"]
            if cur_aav is None:
                best_active["terms"]["aav_usd"] = ddh["aav_usd"]
                best_active["terms"]["total_value_usd"] = ddh["aav_usd"] * best_active["terms"]["term_years"]
                best_active["deep_dive_url"] = ddh["url"]
            elif best_active["terms"]["is_elc"] and abs(cur_aav - ddh["aav_usd"]) > 1_000:
                best_active["terms"]["aav_usd"] = ddh["aav_usd"]
                best_active["terms"]["total_value_usd"] = ddh["aav_usd"] * best_active["terms"]["term_years"]
                best_active["deep_dive_url"] = ddh["url"]
            elif abs(cur_aav - ddh["aav_usd"]) > 50_000:
                # Reconcile mismatch using Deep Dive exact cap hit while keeping signing URL + flagging
                audit_flags.append({
                    "player": p["name"],
                    "reason": "reconciled_to_deepdive",
                    "parsed_aav": cur_aav,
                    "dd_aav": ddh["aav_usd"],
                    "end_season_dd": ddh["end_season"],
                    "url": best_active["url"],
                })
                best_active["terms"]["aav_usd"] = ddh["aav_usd"]
                best_active["end_season"] = ddh["end_season"]
                best_active["deep_dive_url"] = ddh["url"]
                if best_active["terms"]["term_years"]:
                    best_active["terms"]["total_value_usd"] = ddh["aav_usd"] * best_active["terms"]["term_years"]

        # Choose best future extension
        best_future = nhl_future or phr_future

        enriched_results.append({
            "player": p,
            "birth_date": bio.get("birthDate"),
            "draft_team": r.get("draft_team"),
            "active": best_active,
            "official_nhl_url": official_nhl_url,
            "deep_dive_hit": ddh,
            "future_extension": best_future,
            "trade": r.get("trade"),
        })

    return {
        "team": team_code,
        "team_name": pa["team_name"],
        "roster_count": pa["roster_count"],
        "deep_dive_url": dd.get("url"),
        "dead_cap_lines": dd.get("dead_cap_lines", []),
        "audit_flags": audit_flags,
        "results": enriched_results,
    }


def main() -> None:
    PHASE_B_DIR.mkdir(parents=True, exist_ok=True)
    dd_cache_file = ROOT / "cache" / "all_deep_dives.json"
    cached_dd_map: dict[str, dict] = {}
    if dd_cache_file.exists():
        cached_dd_map = json.loads(dd_cache_file.read_text(encoding="utf-8"))
    # Also seed from atlantic_dd.json if present
    atl_f = ROOT / "cache" / "atlantic_dd.json"
    if atl_f.exists():
        for k, v in json.loads(atl_f.read_text(encoding="utf-8")).items():
            if k not in cached_dd_map and not v.get("error"):
                cached_dd_map[k] = v

    teams = [t.upper() for t in sys.argv[1:]]
    if not teams:
        raise SystemExit("Usage: python phase_b_enrich.py TEAM1 TEAM2 ...")

    for tm in teams:
        print(f"\n================ {tm} (Phase B) ================", flush=True)
        res = enrich_team(tm, cached_dd_map)
        (PHASE_B_DIR / f"{tm.lower()}_enriched.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        dd_cache_file.write_text(json.dumps(cached_dd_map, indent=2), encoding="utf-8")

        n = res["roster_count"]
        ok = 0
        dd_only = 0
        gaps = []
        for r in res["results"]:
            p = r["player"]
            a = r["active"]
            ddh = r["deep_dive_hit"]
            if a and a["terms"]["aav_usd"]:
                ok += 1
            elif ddh:
                dd_only += 1
                gaps.append((p["name"], p["id"], f"DD_ONLY ${ddh['aav_usd']:,} ..{ddh['end_season']}"))
            else:
                gaps.append((p["name"], p["id"], "NO_AAV"))
        dc = res["dead_cap_lines"][0][:140] if res["dead_cap_lines"] else "none"
        print(
            f"  {tm}: {ok}/{n} full w/ AAV | {dd_only}/{n} DD-only | {len(gaps) - dd_only}/{n} true gaps | "
            f"flags={len(res['audit_flags'])} | dead_cap={dc}",
            flush=True,
        )
        for g in gaps:
            print(f"    GAP: {g[0]:22} ({g[1]}) -> {g[2]}", flush=True)


if __name__ == "__main__":
    main()
