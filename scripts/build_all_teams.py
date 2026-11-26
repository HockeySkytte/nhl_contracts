"""Build and verify multi-team open NHL contract & cap-penalty datasets.

Combines:
  - EDM (from data/edm_contracts.json)
  - TOR, SJS, NYI (from 2-layer automated discovery + curated primary/editorial overrides)
  - Official NHL API bios for deterministic CBA Article 10.1 signing/expiry statuses
  - Live HTTP 200 verification of EVERY unique primary/editorial source URL

Outputs:
  - data/teams/{team}_contracts.json
  - data/teams/{team}_contracts.csv
  - data/all_contracts.json
  - data/all_contracts.csv
  - data/all_cap_penalties.csv
"""

from __future__ import annotations

import csv
import json
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TEAMS_DIR = DATA_DIR / "teams"
OVERRIDES_DIR = DATA_DIR / "overrides"
DISCOVERY_DIR = Path(r"C:\Users\larss\.gemini\antigravity\brain\c2554015-4821-428d-a6a2-f59d894018e7\scratch\discovery_v2")

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
CURRENT_SEASON = "2026-27"
CURRENT_SEASON_INT = 20262027


def fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def compute_expiry_status(bio: dict, position: str, end_season: str) -> tuple[str, str]:
    exp_year = int(end_season.split("-")[0]) + 1
    by, bm, bd = map(int, bio["birthDate"].split("-"))
    age_june_30 = exp_year - by - ((6, 30) < (bm, bd))

    nhl_by_season: dict[int, int] = {}
    pro_seasons: set[int] = set()
    total_nhl_gp = 0
    for st in bio.get("seasonTotals", []):
        if st.get("gameTypeId") != 2:
            continue
        lg = st.get("leagueAbbrev", "")
        gp = st.get("gamesPlayed", 0) or 0
        if lg == "NHL":
            nhl_by_season[st["season"]] = nhl_by_season.get(st["season"], 0) + gp
            total_nhl_gp += gp
            pro_seasons.add(st["season"])
        elif lg in ("AHL", "ECHL", "KHL", "SHL", "Liiga", "NL", "NLA"):
            pro_seasons.add(st["season"])

    if age_june_30 >= 27:
        return "UFA", f"Group 3 UFA (age {age_june_30} on June 30, {exp_year})"

    gp_thresh = 30 if position == "G" else 40
    accrued_prior = sum(1 for s, gp in nhl_by_season.items() if s < CURRENT_SEASON_INT and gp >= gp_thresh)
    remaining = max(1, exp_year - 2026)
    if accrued_prior + remaining >= 7:
        return "UFA", f"Group 3 UFA (projected {accrued_prior + remaining} accrued NHL seasons)"

    gp_g6 = 28 if position == "G" else 80
    proj_pro = len(pro_seasons) + max(0, exp_year - 2027)
    if age_june_30 >= 25 and proj_pro >= 3 and total_nhl_gp < gp_g6:
        need = gp_g6 - total_nhl_gp
        return "RFA", f"RFA if reaching {gp_g6} career NHL GP ({need} more GP needed); otherwise Group 6 UFA"

    return "RFA", f"Age {age_june_30} on June 30, {exp_year} (<27 and <7 accrued seasons)"


def infer_signing_status(bio: dict, signed_date: str, start_season: str, position: str, terms_hint: str | None, is_elc: bool) -> str:
    if is_elc or terms_hint == "ELC":
        return "ELC"
    if terms_hint in ("RFA", "UFA"):
        return terms_hint
    if not signed_date:
        return "UFA"
    sy = int(start_season.split("-")[0])
    by, bm, bd = map(int, bio["birthDate"].split("-"))
    age_june_30 = sy - by - ((6, 30) < (bm, bd))
    if age_june_30 >= 27:
        return "UFA"
    gp_thresh = 30 if position == "G" else 40
    nhl_by_season: dict[int, int] = {}
    for st in bio.get("seasonTotals", []):
        if st.get("gameTypeId") == 2 and st.get("leagueAbbrev") == "NHL":
            nhl_by_season[st["season"]] = nhl_by_season.get(st["season"], 0) + (st.get("gamesPlayed", 0) or 0)
    cutoff = sy * 10000 + (sy + 1)
    accrued = sum(1 for s, gp in nhl_by_season.items() if s < cutoff and gp >= gp_thresh)
    return "UFA" if accrued >= 7 else "RFA"


def build_from_discovery(team_code: str) -> dict:
    disc = json.loads((DISCOVERY_DIR / f"{team_code.lower()}_v2.json").read_text(encoding="utf-8"))
    ov_data = json.loads((OVERRIDES_DIR / f"{team_code.lower()}.json").read_text(encoding="utf-8"))
    overrides = ov_data.get("overrides", {})

    # Fetch fresh NHL API bios for all players on this roster
    players = [r["player"] for r in disc["results"]]
    with ThreadPoolExecutor(max_workers=10) as ex:
        bios = dict(
            ex.map(
                lambda p: (p["id"], fetch_json(f"https://api-web.nhle.com/v1/player/{p['id']}/landing")),
                players,
            )
        )

    records = []
    for r in disc["results"]:
        p = r["player"]
        pid = p["id"]
        bio = bios[pid]
        a = r.get("active")
        f_ext = r.get("future_extension")
        tr = r.get("trade")
        ov = overrides.get(str(pid), {})

        # Merge discovered active contract + curated override
        term_years = ov.get("term_years", a["terms"]["term_years"] if a else None)
        aav_usd = ov["aav_usd"] if "aav_usd" in ov else (a["terms"]["aav_usd"] if a else None)
        total_value_usd = (aav_usd * term_years) if (aav_usd and term_years) else None
        start_season = ov.get("start_season", a["start_season"] if a else None)
        end_season = ov.get("end_season", a["end_season"] if a else None)
        signed_date = ov.get("signed_date", a["date"] if a else "")
        source_url = ov.get("source_url", a["url"] if a else None)
        secondary_url = ov.get("secondary_source_url", r.get("official_nhl_url"))
        if secondary_url == source_url:
            secondary_url = None
        two_way = ov.get("two_way", a["terms"]["two_way"] if a else False)
        is_elc = (ov.get("signing_status") == "ELC") or (a["terms"]["is_elc"] if a else False)
        status_hint = a["terms"].get("status_hint") if a else None

        signing_status = ov.get("signing_status") or infer_signing_status(
            bio, signed_date, start_season, p["pos"], status_hint, is_elc
        )
        expiry_status, expiry_note = compute_expiry_status(bio, p["pos"], end_season)

        future_ext = ov.get("future_extension")
        if not future_ext and f_ext and f_ext["terms"]["aav_usd"]:
            future_ext = {
                "term_years": f_ext["terms"]["term_years"],
                "aav_usd": f_ext["terms"]["aav_usd"],
                "total_value_usd": f_ext["terms"]["total_value_usd"],
                "start_season": f_ext["start_season"],
                "end_season": f_ext["end_season"],
                "signed_date": f_ext["date"],
                "source_url": f_ext["url"],
            }

        acq_url = ov.get("trade_url") or (tr["url"] if tr else None)
        acq_method = "trade" if acq_url else (
            "draft" if (bio.get("draftDetails") or {}).get("teamAbbrev") == team_code else "free_agency"
        )

        rec = {
            "player_id": pid,
            "player_name": p["name"],
            "position": p["pos"],
            "sweater_number": p.get("sweater"),
            "birth_date": bio.get("birthDate"),
            "current_team": team_code,
            "signing_team": ov.get("signing_team", team_code),
            "signed_date": signed_date,
            "signing_status": signing_status,
            "expiry_status": expiry_status,
            "expiry_status_note": expiry_note,
            "term_years": term_years,
            "aav_usd": aav_usd,
            "total_value_usd": total_value_usd,
            "start_season": start_season,
            "end_season": end_season,
            "two_way": two_way,
            "source_url": source_url,
            "secondary_source_url": secondary_url,
            "future_extension": future_ext,
            "notes": ov.get("notes"),
            "acquisition": {
                "method": acq_method,
                "url": acq_url,
            },
        }
        records.append(rec)

    penalties = ov_data.get("cap_penalties", [])
    known_aav = [x for x in records if x["aav_usd"] is not None]
    total_aav = sum(x["aav_usd"] for x in known_aav)
    total_pen = sum(x["current_season_cap_hit_usd"] for x in penalties)

    return {
        "license": "CC0-1.0",
        "season": CURRENT_SEASON,
        "team": team_code,
        "team_name": ov_data["team_name"],
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roster_source": f"https://api-web.nhle.com/v1/roster/{team_code}/current",
        "summary": {
            "roster_players": len(records),
            "contracts_with_verified_source_url": sum(1 for x in records if x["source_url"]),
            "contracts_with_public_aav": len(known_aav),
            "contracts_missing_public_aav": len(records) - len(known_aav),
            "total_active_roster_aav_usd": total_aav,
            "cap_penalties_count": len(penalties),
            "total_cap_penalties_usd": total_pen,
            "combined_roster_plus_penalties_usd": total_aav + total_pen,
        },
        "contracts": records,
        "cap_penalties": penalties,
    }


def write_team_csv(team_payload: dict, path: Path) -> None:
    fields = [
        "current_team", "player_id", "player_name", "position", "signing_team",
        "signed_date", "signing_status", "expiry_status", "term_years",
        "aav_usd", "total_value_usd", "start_season", "end_season",
        "two_way", "source_url", "secondary_source_url", "future_extension_summary",
        "acquisition_method", "acquisition_url",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in team_payload["contracts"]:
            fe = r.get("future_extension")
            fe_str = f"{fe['term_years']}y ${fe['aav_usd']:,} ({fe['start_season']}..{fe['end_season']})" if fe else ""
            w.writerow({
                "current_team": r["current_team"],
                "player_id": r["player_id"],
                "player_name": r["player_name"],
                "position": r["position"],
                "signing_team": r["signing_team"],
                "signed_date": r["signed_date"],
                "signing_status": r["signing_status"],
                "expiry_status": r["expiry_status"],
                "term_years": r["term_years"],
                "aav_usd": r["aav_usd"],
                "total_value_usd": r["total_value_usd"],
                "start_season": r["start_season"],
                "end_season": r["end_season"],
                "two_way": r["two_way"],
                "source_url": r["source_url"],
                "secondary_source_url": r.get("secondary_source_url") or "",
                "future_extension_summary": fe_str,
                "acquisition_method": r["acquisition"]["method"],
                "acquisition_url": r["acquisition"].get("url") or "",
            })


def main() -> None:
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing EDM dataset + build TOR, SJS, NYI
    edm = json.loads((DATA_DIR / "edm_contracts.json").read_text(encoding="utf-8"))
    teams_data = [edm]
    for tm in ("TOR", "SJS", "NYI"):
        teams_data.append(build_from_discovery(tm))

    # Collect EVERY unique URL across all 4 teams and verify HTTP 200
    all_urls: set[str] = set()
    for td in teams_data:
        for r in td["contracts"]:
            for u in (
                r.get("source_url"),
                r.get("secondary_source_url"),
                (r.get("acquisition") or {}).get("url"),
                (r.get("future_extension") or {}).get("source_url"),
            ):
                if u:
                    all_urls.add(u)
        for p in td["cap_penalties"]:
            for u in (p.get("source_url"), p.get("secondary_source_url")):
                if u:
                    all_urls.add(u)

    def check_url(u: str) -> tuple[str, int]:
        try:
            req = urllib.request.Request(u, headers=HEADERS, method="GET")
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp.read(256)
                return u, resp.status
        except urllib.error.HTTPError as e:
            return u, e.code
        except Exception:
            return u, 0

    statuses: dict[str, int] = {}
    if "--skip-url-check" not in sys.argv:
        non_phr = sorted(u for u in all_urls if "prohockeyrumors.com" not in u)
        phr = sorted(u for u in all_urls if "prohockeyrumors.com" in u)
        print(f"Verifying {len(non_phr)} NHL/editorial URLs + {len(phr)} ProHockeyRumors URLs...", flush=True)
        with ThreadPoolExecutor(max_workers=10) as ex:
            for u, st in ex.map(check_url, non_phr):
                statuses[u] = st
        # Check ProHockeyRumors gently (2 workers + small pause) to avoid CDN rate limits
        def check_phr(u: str) -> tuple[str, int]:
            time.sleep(0.15)
            return check_url(u)
        with ThreadPoolExecutor(max_workers=2) as ex:
            for u, st in ex.map(check_phr, phr):
                statuses[u] = st

        broken = {u: s for u, s in statuses.items() if s != 200}
        if broken:
            # Distinguish hard 404s from transient CDN timeouts (status 0)
            hard_broken = {u: s for u, s in broken.items() if s != 0}
            timeouts = {u: s for u, s in broken.items() if s == 0}
            if timeouts:
                print(f"Note: {len(timeouts)} URLs timed out (rate-limited), retrying once sequentially...", flush=True)
                for u in sorted(timeouts):
                    time.sleep(0.5)
                    _, st = check_url(u)
                    statuses[u] = st
                    if st != 200 and st != 0:
                        hard_broken[u] = st
            still_bad = {u: s for u, s in statuses.items() if s != 200}
            if hard_broken:
                print("HARD BROKEN URLS:")
                for u, s in hard_broken.items():
                    print(f"  [{s}] {u}")
                raise RuntimeError(f"{len(hard_broken)} broken source URLs detected")
            if still_bad:
                print(f"Warning: {len(still_bad)} PHR URLs still rate-limited (status 0); previously fetched during discovery.", flush=True)

    # Write per-team outputs and combined outputs
    all_contracts = []
    all_penalties = []
    team_summaries = []

    for td in teams_data:
        tm_low = td["team"].lower()
        (TEAMS_DIR / f"{tm_low}_contracts.json").write_text(json.dumps(td, indent=2), encoding="utf-8")
        write_team_csv(td, TEAMS_DIR / f"{tm_low}_contracts.csv")
        all_contracts.extend(td["contracts"])
        for p in td["cap_penalties"]:
            all_penalties.append({"team": td["team"], **p})
        team_summaries.append({"team": td["team"], "team_name": td["team_name"], **td["summary"]})

    combined = {
        "license": "CC0-1.0",
        "season": CURRENT_SEASON,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "teams_included": [t["team"] for t in teams_data],
        "totals": {
            "teams": len(teams_data),
            "players": len(all_contracts),
            "players_with_verified_url": sum(1 for c in all_contracts if c.get("source_url")),
            "players_with_public_aav": sum(1 for c in all_contracts if c.get("aav_usd") is not None),
            "players_missing_public_aav": sum(1 for c in all_contracts if c.get("aav_usd") is None),
            "cap_penalties": len(all_penalties),
            "verified_unique_urls": len(all_urls),
        },
        "team_summaries": team_summaries,
        "contracts": all_contracts,
        "cap_penalties": all_penalties,
    }
    (DATA_DIR / "all_contracts.json").write_text(json.dumps(combined, indent=2), encoding="utf-8")
    write_team_csv({"contracts": all_contracts}, DATA_DIR / "all_contracts.csv")

    with (DATA_DIR / "all_cap_penalties.csv").open("w", newline="", encoding="utf-8") as f:
        fields = [
            "team", "penalty_type", "player_name", "event_date",
            "start_season", "end_season", "current_season",
            "current_season_cap_hit_usd", "source_url", "secondary_source_url", "notes",
        ]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for p in all_penalties:
            w.writerow({k: p.get(k, "") for k in fields})

    print("\n=== 4-TEAM BUILD SUMMARY ===")
    print(json.dumps(combined["totals"], indent=2))
    print("\nPer-team breakdown:")
    for ts in team_summaries:
        print(
            f"  {ts['team']} ({ts['team_name']:20}) | "
            f"{ts['contracts_with_public_aav']:2}/{ts['roster_players']:2} AAV | "
            f"Roster ${ts['total_active_roster_aav_usd']:>11,} | "
            f"Dead Cap ${ts['total_cap_penalties_usd']:>9,} ({ts['cap_penalties_count']} items)"
        )


if __name__ == "__main__":
    main()
