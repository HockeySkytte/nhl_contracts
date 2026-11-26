"""Phase A: Fast, 100% official NHL Web API + NHL.com Forge API backfill across all 28 remaining teams.

Touches ONLY:
  - https://api-web.nhle.com/v1/roster/{TEAM}/current
  - https://api-web.nhle.com/v1/player/{ID}/landing
  - https://forge-dapi.d3.nhle.com/v2/content/en-us/stories

Zero ProHockeyRumors calls (zero rate-limit risk). Uses persistent cache/http/.
Also reads NHL.com's 2026-27 and 2025-26 Free Agent Tracker articles once to capture
direct team press release links for recent signings.
"""

from __future__ import annotations

import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discover_v3 import (
    BUYOUT_RE,
    CURRENT_START_YEAR,
    EXCLUDE_SIGN_HL_RE,
    SIGN_HL_RE,
    TEAM_INFO,
    TRADE_HL_RE,
    fetch_forge_pages,
    fetch_json_cached,
    fetch_story_full,
    infer_signing_team_from_text,
    parse_contract_text,
    resolve_seasons,
)

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "cache" / "phase_a"

DONE_TEAMS = {"EDM", "TOR", "SJS", "NYI"}
REMAINING_28 = [t for t in sorted(TEAM_INFO.keys()) if t not in DONE_TEAMS]


def load_nhl_fa_trackers() -> list[tuple[str, str]]:
    """Pull direct nhl.com signing links from NHL.com's 2026-27 and 2025-26 Free Agent Trackers."""
    links = []
    for slug in ("free-agency-signings-nhl-2026-27", "free-agency-signings-nhl-2025-26"):
        d = fetch_json_cached(f"https://forge-dapi.d3.nhle.com/v2/content/en-us/stories/{slug}")
        if not d:
            continue
        md = "\n".join(p.get("content", "") for p in d.get("parts", []) if p.get("type") == "markdown")
        for m in re.finditer(r"\[([^\]]+)\]\((https://www\.nhl\.com/[^)]+)\)", md):
            links.append((m.group(1), m.group(2)))
    return links


def run_team_nhl_only(team_code: str, tracker_links: list[tuple[str, str]]) -> dict:
    tid, _, tag_prefix, team_name, team_pat = TEAM_INFO[team_code]
    team_re = re.compile(rf"\b({team_pat})\b", re.I)

    roster = fetch_json_cached(f"https://api-web.nhle.com/v1/roster/{team_code}/current")
    if not roster:
        raise RuntimeError(f"Roster fetch failed for {team_code}")

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

    with ThreadPoolExecutor(max_workers=12) as ex:
        bios = dict(
            ex.map(
                lambda p: (p["id"], fetch_json_cached(f"https://api-web.nhle.com/v1/player/{p['id']}/landing") or {}),
                players,
            )
        )

    # Build team + player Forge pool
    pool: dict[str, dict] = {}
    for expr, pages in [
        (f"{tag_prefix}-created,transactions", 12),
        (f"teamid-{tid},free-agency", 10),
        (f"teamid-{tid},transactions", 10),
    ]:
        for it in fetch_forge_pages(expr, max_pages=pages):
            pool[it["slug"]] = it

    def scan_pid(p):
        pid = p["id"]
        out = []
        for expr, pg in [
            (f"playerid-{pid},free-agency", 2),
            (f"playerid-{pid},transactions", 2),
            (f"playerid-{pid}", 2),
        ]:
            out.extend(fetch_forge_pages(expr, max_pages=pg))
        return out

    with ThreadPoolExecutor(max_workers=12) as ex:
        for batch in ex.map(scan_pid, players):
            for it in batch:
                pool[it["slug"]] = it

    # Add relevant tracker links
    for p in players:
        last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)
        for label, url in tracker_links:
            if last_re.search(label) and "sign" in label.lower():
                m_sl = re.search(r"/news/(?:topic/[^/]+/)?([^/?#]+)$", url)
                if m_sl and m_sl.group(1) not in pool:
                    pool[m_sl.group(1)] = {
                        "slug": m_sl.group(1),
                        "headline": label,
                        "contentDate": "2026-07-01",
                        "tags": ["free-agency"],
                    }

    def resolve_player(p):
        pid = p["id"]
        bio = bios[pid]
        last_re = re.compile(r"\b" + re.escape(p["last"]) + r"\b", re.I)
        first_re = re.compile(r"\b" + re.escape(p["first"]) + r"\b", re.I)

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
                # Rank dedicated single-player articles higher than roundup/multi-player articles
                dedicated = bool(last_re.search(hl)) and not re.search(r";|,.*sign|each sign|four players|three players", hl, re.I)
                nhl_signings.append((dt[:4], dedicated, dt, sl, hl))
            if (
                TRADE_HL_RE.search(hl)
                and not re.search(r"recall|loan|assign|activate|ir\b|buzz|rumor|preview|edge stats|takeaways|adjusting|watch", hl, re.I)
                and (f"teamid-{tid}" in tags or f"{tag_prefix}-created" in tags or team_re.search(hl))
            ):
                nhl_trades.append((dt, sl, hl))

        nhl_signings.sort(reverse=True)
        seen_s = set()
        parsed_contracts = []

        for _, dedicated, dt, sl, hl in nhl_signings:
            if sl in seen_s or len(seen_s) >= 5:
                continue
            seen_s.add(sl)
            st = fetch_story_full(sl)
            if not st or not last_re.search(st["body"]):
                continue
            if not first_re.search(st["body"]) and f"playerid-{pid}" not in st["tags"]:
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
            signing_team = infer_signing_team_from_text(st["headline"], st["body"], st["tags"], team_code)
            parsed_contracts.append({
                "source": "nhl.com",
                "dedicated": dedicated,
                "url": st["url"],
                "date": st["date"],
                "headline": st["headline"],
                "signing_team": signing_team,
                "start_season": start_s,
                "end_season": end_s,
                "start_yr": int(start_s.split("-")[0]),
                "end_yr": int(end_s.split("-")[0]),
                "terms": terms,
            })

        active_cands = [c for c in parsed_contracts if c["start_yr"] <= CURRENT_START_YEAR <= c["end_yr"]]
        future_cands = [c for c in parsed_contracts if c["start_yr"] > CURRENT_START_YEAR]

        def rank_c(c):
            return (c["terms"]["aav_usd"] is not None, c["dedicated"], c["date"])

        active_cands.sort(key=rank_c, reverse=True)
        future_cands.sort(key=rank_c, reverse=True)

        nhl_trades.sort(reverse=True)
        best_trade = None
        for dt, sl, hl in nhl_trades[:3]:
            st = fetch_story_full(sl)
            if st and last_re.search(st["body"]) and team_re.search(st["headline"] + " " + st["body"][:350]):
                best_trade = {"date": st["date"], "headline": st["headline"], "url": st["url"]}
                break

        return {
            "player": p,
            "birth_date": bio.get("birthDate"),
            "draft_team": (bio.get("draftDetails") or {}).get("teamAbbrev"),
            "active": active_cands[0] if active_cands else None,
            "future_extension": future_cands[0] if future_cands else None,
            "trade": best_trade,
        }

    with ThreadPoolExecutor(max_workers=10) as ex:
        results = list(ex.map(resolve_player, players))

    return {
        "team": team_code,
        "team_name": team_name,
        "roster_count": len(players),
        "results": results,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    teams = [t.upper() for t in sys.argv[1:]] if len(sys.argv) > 1 else REMAINING_28
    tracker_links = load_nhl_fa_trackers()
    print(f"Loaded {len(tracker_links)} links from NHL.com 2025-26 & 2026-27 Free Agent Trackers.", flush=True)

    tot_players = 0
    tot_url = 0
    tot_aav = 0

    for tm in teams:
        res = run_team_nhl_only(tm, tracker_links)
        (OUT_DIR / f"{tm.lower()}_nhl.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
        n = res["roster_count"]
        got_url = sum(1 for r in res["results"] if r["active"])
        got_aav = sum(1 for r in res["results"] if r["active"] and r["active"]["terms"]["aav_usd"])
        tot_players += n
        tot_url += got_url
        tot_aav += got_aav
        print(f"  {tm} ({res['team_name']:22}): {n:2} players | {got_url:2}/{n:2} NHL.com URL | {got_aav:2}/{n:2} NHL.com AAV", flush=True)

    print(
        f"\nPHASE A TOTAL ({len(teams)} teams): {tot_players} players | "
        f"{tot_url}/{tot_players} ({100*tot_url/tot_players:.1f}%) NHL.com URL | "
        f"{tot_aav}/{tot_players} ({100*tot_aav/tot_players:.1f}%) NHL.com AAV",
        flush=True,
    )


if __name__ == "__main__":
    main()
