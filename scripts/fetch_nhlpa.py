"""Fetch official NHLPA Compensation by Team data (https://www.nhlpa.com/api/compensation/{year})
and build a clean year-by-year compensation history indexed by canonical NHL player ID (nhlId).

Why NHLPA?
  - Official primary source published by the National Hockey League Players' Association
    (https://www.nhlpa.com/compensation/{team-slug}, robots.txt Allow: /).
  - Covers 99.8% of active NHL roster players (802/804) including all 5 non-disclosure clubs
    (ANA, COL, NYR, UTA, CGY) with exact nhlId joins.
  - Provides official AAV, baseSalary, signingBonus, totalSalary (base + signing bonus),
    and minorLeagueSalary (deterministically identifying two-way contracts) year-by-year
    from 2018-19 through 2026-27 (and archivable going forward).

Outputs:
  - cache/nhlpa/compensation_history.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "cache" / "nhlpa"

# Pull all 17 available seasons: 2010-11 through 2026-27
START_YEAR = 2010
END_YEAR = 2026

# Normalize franchise tricode aliases to canonical NHL / public.teams codes
TRICODE_NORM = {
    "UTH": "UTA",  # Utah Hockey Club (2024-25) / NHLPA residual code -> Utah Mammoth (UTA)
}


def season_label(yr: int) -> str:
    return f"{yr}-{str(yr + 1)[-2:]}"


def infer_contract_start_from_history(yearly: dict[str, dict], current_season: str = "2026-27") -> str | None:
    """Infer the earliest contiguous season sharing the current season's official AAV."""
    cur = yearly.get(current_season)
    if not cur or not cur.get("aav_usd"):
        return None
    target_aav = cur["aav_usd"]
    seasons_desc = sorted(yearly.keys(), reverse=True)
    start_s = current_season
    cur_yr = int(current_season.split("-")[0])
    for s in seasons_desc:
        sy = int(s.split("-")[0])
        if sy >= cur_yr:
            continue
        # Must be contiguous (no gap > 1 season, or immediate prior season with same AAV)
        prev_yr = int(start_s.split("-")[0])
        if prev_yr - sy != 1:
            break
        rec = yearly[s]
        if rec.get("aav_usd") is not None and abs(rec["aav_usd"] - target_aav) <= 2_500:
            start_s = s
        else:
            break
    return start_s


def build_nhlpa_history(start_year: int = START_YEAR, end_year: int = END_YEAR) -> dict:
    players: dict[str, dict] = {}
    season_counts: dict[str, int] = {}

    for yr in range(start_year, end_year + 1):
        s_lbl = season_label(yr)
        data = dv3.fetch_json_cached(f"https://www.nhlpa.com/api/compensation/{yr}")
        if not isinstance(data, list):
            season_counts[s_lbl] = 0
            continue

        cnt = 0
        for team_block in data:
            team = team_block.get("team") or {}
            raw_tricode = team.get("tricode")
            tricode = TRICODE_NORM.get(raw_tricode, raw_tricode)
            team_slug = team.get("slug")
            for row in team_block.get("players") or []:
                pl = row.get("player") or {}
                comp = row.get("compensation") or {}
                nhl_id = pl.get("nhlId")
                if not nhl_id:
                    continue
                cnt += 1
                pid_str = str(int(nhl_id))
                entry = players.setdefault(pid_str, {
                    "nhl_id": int(nhl_id),
                    "nhlpa_player_id": pl.get("playerId"),
                    "name": f"{pl.get('firstName', '')} {pl.get('lastName', '')}".strip(),
                    "position": pl.get("positionCode"),
                    "slug": pl.get("slug"),
                    "current_team_tricode": None,
                    "current_team_slug": None,
                    "nhlpa_compensation_url": None,
                    "nhlpa_player_url": (
                        f"https://www.nhlpa.com/player/{pl.get('playerId')}/{pl.get('slug')}"
                        if pl.get("playerId") and pl.get("slug") else None
                    ),
                    "yearly_compensation": {},
                })
                if yr == end_year:
                    entry["current_team_tricode"] = tricode
                    entry["current_team_slug"] = team_slug
                    if team_slug:
                        entry["nhlpa_compensation_url"] = f"https://www.nhlpa.com/compensation/{team_slug}"

                base = int(comp.get("baseSalary") or 0)
                sb = int(comp.get("signingBonus") or 0)
                tot = int(comp.get("totalSalary") or (base + sb))
                aav = int(comp.get("aav") or tot)
                minor = int(comp.get("minorLeagueSalary") or 0)

                entry["yearly_compensation"][s_lbl] = {
                    "team": tricode,
                    "aav_usd": aav,
                    "total_salary_usd": tot,
                    "base_salary_usd": base,
                    "signing_bonus_usd": sb,
                    "minor_league_salary_usd": minor,
                }
        season_counts[s_lbl] = cnt

    # Compute inferred contract start season + observed multi-year averages
    cur_label = season_label(end_year)
    for pid_str, entry in players.items():
        yc = entry["yearly_compensation"]
        entry["yearly_compensation"] = dict(sorted(yc.items()))
        if cur_label in yc:
            inferred_start = infer_contract_start_from_history(yc, cur_label)
            entry["inferred_contract_start_season"] = inferred_start
            cur_rec = yc[cur_label]
            entry["current_2026_27"] = {
                **cur_rec,
                "two_way": cur_rec["minor_league_salary_usd"] > 0,
            }
            # Also compute observed average total_salary across the contiguous contract years so far
            if inferred_start:
                s0 = int(inferred_start.split("-")[0])
                seg = [v["total_salary_usd"] for k, v in yc.items() if int(k.split("-")[0]) >= s0]
                entry["observed_contract_years"] = len(seg)
                entry["observed_mean_salary_usd"] = int(round(sum(seg) / len(seg))) if seg else cur_rec["aav_usd"]

    return {
        "source": "NHLPA Official Compensation by Team (https://www.nhlpa.com/compensation-by-team)",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "seasons": season_counts,
        "total_unique_players": len(players),
        "players": players,
    }


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    res = build_nhlpa_history()
    out_path = OUT_DIR / "compensation_history.json"
    out_path.write_text(json.dumps(res, indent=2), encoding="utf-8")
    print(f"Saved {out_path} ({res['total_unique_players']} unique players across {len(res['seasons'])} seasons)")
    for s, c in res["seasons"].items():
        print(f"  {s}: {c} players")


if __name__ == "__main__":
    main()
