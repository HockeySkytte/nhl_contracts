"""Fetch all official NHL Draft picks (2015 through 2026) with exact overallPickNumber,
pickInRound, teamPickHistory (full chronological trade chain!), drafting team, and drafted
player (canonical playerId + playerName + position).

Sources (100% Official NHL APIs):
  - https://records.nhl.com/site/api/draft?cayenneExp=draftYear={yr}&limit=500 (includes playerId!)
  - https://api-web.nhle.com/v1/draft/picks/{yr}/all (fallback/supplement for latest draft year)

Outputs:
  - cache/nhl/draft_picks_2015_2026.json
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "cache" / "nhl" / "draft_picks_2015_2026.json"

# Normalize legacy tricodes in NHL teamPickHistory to match our trade database
TRICODE_NORM = {
    "PHX": "ARI",
    "LAK": "LAK",
    "SJS": "SJS",
    "TBL": "TBL",
    "NJD": "NJD",
    "VGK": "VGK",
    "SEA": "SEA",
    "UTA": "UTA",
}


def norm_tri(t: str | None) -> str | None:
    if not t:
        return None
    t = t.strip().upper()
    return TRICODE_NORM.get(t, t)


def parse_history_chain(hist: str | None, fallback_team: str | None) -> list[str]:
    if not hist:
        return [norm_tri(fallback_team)] if fallback_team else []
    parts = [norm_tri(x) for x in hist.replace("/", "-").split("-") if x.strip()]
    return [p for p in parts if p]


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    by_year: dict[str, list[dict]] = {}
    total = 0

    for yr in range(2015, 2027):
        rec = dv3.fetch_json_cached(f"https://records.nhl.com/site/api/draft?cayenneExp=draftYear={yr}&limit=500")
        rec_rows = (rec or {}).get("data") or []
        web = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/draft/picks/{yr}/all")
        web_rows = (web or {}).get("picks") or []

        # Index api-web rows by overallPick for supplemental fields (especially if records lags on 2026)
        web_by_ov = {int(w["overallPick"]): w for w in web_rows if w.get("overallPick")}
        rec_by_ov = {int(r["overallPickNumber"]): r for r in rec_rows if r.get("overallPickNumber")}

        all_ovs = sorted(set(web_by_ov.keys()) | set(rec_by_ov.keys()))
        picks = []
        for ov in all_ovs:
            r = rec_by_ov.get(ov) or {}
            w = web_by_ov.get(ov) or {}
            drafted_by = norm_tri(r.get("triCode") or w.get("teamAbbrev"))
            hist_raw = r.get("teamPickHistory") or w.get("teamPickHistory") or drafted_by
            chain = parse_history_chain(hist_raw, drafted_by)
            if r.get("playerName"):
                pname = r["playerName"]
            else:
                fn = (w.get("firstName") or {}).get("default", "")
                ln = (w.get("lastName") or {}).get("default", "")
                pname = f"{fn} {ln}".strip()
            picks.append({
                "draft_year": yr,
                "round": int(r.get("roundNumber") or w.get("round") or 0),
                "pick_in_round": int(r.get("pickInRound") or w.get("pickInRound") or 0),
                "overall_pick": ov,
                "drafted_by_team": drafted_by,
                "original_team": chain[0] if chain else drafted_by,
                "team_pick_history": "-".join(chain),
                "history_chain": chain,
                "player_id": r.get("playerId"),
                "player_name": pname,
                "position": r.get("position") or w.get("positionCode"),
                "amateur_club": r.get("amateurClubName") or w.get("amateurClubName"),
                "amateur_league": r.get("amateurLeague") or w.get("amateurLeague"),
                "removed_outright": r.get("removedOutright") == "Y",
            })
        by_year[str(yr)] = picks
        total += len(picks)
        with_hist = sum(1 for p in picks if len(p["history_chain"]) > 1)
        with_pid = sum(1 for p in picks if p["player_id"])
        print(f"  {yr} NHL Draft: {len(picks):3} picks | {with_hist:3} traded picks in history | {with_pid:3} with playerId")

    payload = {
        "source": "Official NHL Draft API (records.nhl.com/site/api/draft + api-web.nhle.com/v1/draft/picks)",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_picks": total,
        "by_year": by_year,
    }
    OUT_PATH.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    print(f"\nSaved {OUT_PATH} ({total:,} draft picks across 12 drafts, 2015-2026)")


if __name__ == "__main__":
    main()
