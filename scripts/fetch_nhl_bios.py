"""Fetch & compact official NHL Web API bios (https://api-web.nhle.com/v1/player/{id}/landing)
for every unique player in the 2010-11 through 2026-27 NHLPA compensation archive (~4,691 players).

Stores only the minimal fields needed for deterministic CBA Article 10.1 signing_status &
expiry_status calculations:
  - birth_date, position, draft_year, draft_team
  - nhl_gp_by_season: {20152016: 45, ...} (regular season NHL GP)
  - pro_seasons: [20142015, 20152016, ...] (NHL/AHL/ECHL/KHL/SHL/Liiga/NL/NLA regular seasons)

Disk-cached in cache/http/ via discover_v3; saves compact index to cache/nhl/player_bios_2010_2026.json.
"""

from __future__ import annotations

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
NHLPA_HIST = ROOT / "cache" / "nhlpa" / "compensation_history.json"
OUT_PATH = ROOT / "cache" / "nhl" / "player_bios_2010_2026.json"

PRO_LEAGUES = {"NHL", "AHL", "ECHL", "KHL", "SHL", "Liiga", "NL", "NLA", "DEL", "Czechia", "Extraliga"}


def compact_bio(pid: int, raw: dict | None) -> dict | None:
    if not raw:
        return None
    nhl_gp: dict[str, int] = {}
    pro_seasons: set[int] = set()
    for st in raw.get("seasonTotals") or []:
        if st.get("gameTypeId") != 2:
            continue
        lg = st.get("leagueAbbrev") or ""
        season = st.get("season")
        gp = int(st.get("gamesPlayed") or 0)
        if not season:
            continue
        if lg == "NHL":
            sk = str(season)
            nhl_gp[sk] = nhl_gp.get(sk, 0) + gp
            if gp > 0:
                pro_seasons.add(int(season))
        elif lg in PRO_LEAGUES and gp > 0:
            pro_seasons.add(int(season))
    dd = raw.get("draftDetails") or {}
    return {
        "player_id": pid,
        "birth_date": raw.get("birthDate"),
        "position": raw.get("position"),
        "draft_year": dd.get("year"),
        "draft_team": dd.get("teamAbbrev"),
        "draft_round": dd.get("round"),
        "nhl_gp_by_season": dict(sorted(nhl_gp.items())),
        "pro_seasons": sorted(pro_seasons),
    }


def fetch_one(pid: int) -> tuple[int, dict | None]:
    url = f"https://api-web.nhle.com/v1/player/{pid}/landing"
    raw = dv3.fetch_json_cached(url)
    if raw is None:
        # One gentle retry on transient timeout/5xx
        time.sleep(0.3)
        raw = dv3.fetch_json_cached(url)
    return pid, compact_bio(pid, raw)


def main() -> None:
    hist = json.loads(NHLPA_HIST.read_text(encoding="utf-8"))
    pids = sorted(int(k) for k in hist["players"].keys())
    print(f"Fetching/loading official NHL API bios for {len(pids):,} players...", flush=True)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() else {}
    todo = [pid for pid in pids if str(pid) not in existing or not existing[str(pid)].get("birth_date")]
    print(f"  Already indexed: {len(pids) - len(todo):,} | To fetch/parse: {len(todo):,}", flush=True)

    t0 = time.monotonic()
    ok = 0
    miss = 0
    if todo:
        with ThreadPoolExecutor(max_workers=20) as ex:
            futs = {ex.submit(fetch_one, pid): pid for pid in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                pid, bio = fut.result()
                if bio and bio.get("birth_date"):
                    existing[str(pid)] = bio
                    ok += 1
                else:
                    miss += 1
                if i % 500 == 0 or i == len(todo):
                    dt = time.monotonic() - t0
                    print(f"  Progress: {i:,}/{len(todo):,} ({ok:,} ok, {miss:,} miss) in {dt:.1f}s", flush=True)

    OUT_PATH.write_text(json.dumps(existing, separators=(",", ":")), encoding="utf-8")
    print(f"Saved {OUT_PATH} ({len(existing):,} player bios, {OUT_PATH.stat().st_size / 1_048_576:.2f} MB)", flush=True)


if __name__ == "__main__":
    main()
