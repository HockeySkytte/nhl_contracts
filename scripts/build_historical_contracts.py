"""Build the complete 17-season Open NHL Historical Contracts Database (2010-11 through 2026-27).

Primary Financial Source:
  - Official NHLPA Compensation by Team API (https://www.nhlpa.com/compensation/{team}),
    covering all 17 seasons (2010-11 through 2026-27), 4,691 unique NHL-contracted players,
    and 25,712 player-season records with canonical nhlId, official AAV, baseSalary,
    signingBonus, totalSalary, and minorLeagueSalary.

Official Bio & Contract Metadata Sources:
  - Official NHL Web API (https://api-web.nhle.com/v1/player/{id}/landing) for birth dates,
    positions, draft details, and full career regular-season NHL/pro games played to compute
    deterministic CBA Article 10.1 signing_status (ELC/RFA/UFA) and expiry_status (RFA/UFA,
    including Group 6 UFA) for every contract from 2010-11 through 2026-27.
  - Audited 2026-27 32-team active roster dataset (data/all_contracts.json) for exact
    verified multi-year end_season, term_years, signed_date, NHL.com signing URLs, and
    signed future extensions (2027-28+).

Zero cap/contract database sites (PuckPedia, CapWages, Spotrac, CapFriendly, etc.) are used.

Outputs (CC0-1.0 Public Domain):
  - data/historical_contracts_2010_2027.csv  (one row per contract + yearly_compensation_json)
  - data/historical_contracts_2010_2027.json (full structured JSON database)
  - data/historical_player_seasons_2010_2027.csv (25,712 player-season tidy table)
  - data/historical_summary.json (season-by-season & status summary)
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
NHLPA_HIST_PATH = ROOT / "cache" / "nhlpa" / "compensation_history.json"
BIOS_PATH = ROOT / "cache" / "nhl" / "player_bios_2010_2026.json"
ACTIVE_2026_PATH = DATA_DIR / "all_contracts.json"

FIRST_HIST_YEAR = 2010
LAST_HIST_YEAR = 2026

FORBIDDEN_CAP_DOMAINS = (
    "puckpedia.com",
    "capwages.com",
    "spotrac.com",
    "capfriendly.com",
    "thestanleycap.com",
    "puckboss.com",
    "capfreeze.com",
    "salaryswish.com",
)

# Shortened-season accrued-season GP thresholds (pro-rated from 40 skater / 30 goalie per 82 GP)
SHORTENED_THRESH = {
    20122013: (24, 18),  # 48-game lockout season
    20192020: (34, 25),  # ~70-game COVID season
    20202021: (28, 21),  # 56-game COVID season
}

# Known historical same-AAV multi-contract split boundaries (start_yr of new contract)
# e.g. Sidney Crosby signed 5y $8.7M (2008-13), 12y $8.7M (2013-25), 2y $8.7M (2025-27)
KNOWN_SAME_AAV_SPLITS: dict[int, set[int]] = {
    8471675: {2013, 2025},  # Sidney Crosby ($8.7M x 3 consecutive deals)
    8478402: {2026},        # Connor McDavid ($12.5M 8y -> $12.5M 2y)
}


def season_str(yr: int) -> str:
    return f"{yr}-{str(yr + 1)[-2:]}"


def season_int(yr: int) -> int:
    return yr * 10000 + (yr + 1)


def age_on(birth_date: str, year: int, month: int, day: int) -> int:
    by, bm, bd = map(int, birth_date.split("-"))
    return year - by - ((month, day) < (bm, bd))


def assert_no_cap_site(url: str | None) -> None:
    if not url:
        return
    low = url.lower()
    for bad in FORBIDDEN_CAP_DOMAINS:
        if bad in low:
            raise RuntimeError(f"Forbidden cap-site URL detected: {url}")


def accrued_seasons_before(bio: dict, pos: str, cutoff_year: int) -> int:
    """Count accrued NHL seasons strictly prior to cutoff_year (season < cutoff_year*10000)."""
    cutoff_int = season_int(cutoff_year)
    cnt = 0
    for sk, gp in (bio.get("nhl_gp_by_season") or {}).items():
        si = int(sk)
        if si >= cutoff_int:
            continue
        sk_th, g_th = SHORTENED_THRESH.get(si, (40, 30))
        th = g_th if pos == "G" else sk_th
        if int(gp) >= th:
            cnt += 1
    return cnt


def career_nhl_gp_before(bio: dict, cutoff_year: int) -> int:
    cutoff_int = season_int(cutoff_year)
    return sum(int(gp) for sk, gp in (bio.get("nhl_gp_by_season") or {}).items() if int(sk) < cutoff_int)


def pro_seasons_before(bio: dict, cutoff_year: int) -> int:
    cutoff_int = season_int(cutoff_year)
    return sum(1 for si in (bio.get("pro_seasons") or []) if int(si) < cutoff_int)


def compute_historical_signing_status(
    bio: dict | None,
    pos: str,
    start_yr: int,
    aav_usd: int,
    first_yr_minor_salary: int,
    contract_index: int,
    override_status: str | None = None,
) -> tuple[str | None, str | None]:
    """Deterministic CBA Article 10.1 signing status at start_yr (June 30 / Sept 15)."""
    if override_status in ("RFA", "UFA"):
        return override_status, "Verified from active 2026-27 contract metadata"
    if override_status == "ELC" and aav_usd <= 1_150_000 and (
        not bio or not bio.get("birth_date") or age_on(bio["birth_date"], start_yr, 9, 15) <= 25
    ):
        return "ELC", "Verified from active 2026-27 contract metadata"
    if not bio or not bio.get("birth_date"):
        return None, None

    bd = bio["birth_date"]
    age_j30 = age_on(bd, start_yr, 6, 30)
    age_s15 = age_on(bd, start_yr, 9, 15)
    prior_acc = accrued_seasons_before(bio, pos, start_yr)
    prior_gp = career_nhl_gp_before(bio, start_yr)
    prior_pro = pro_seasons_before(bio, pos_unused := start_yr)

    # 1. Entry-Level Contract (ELC) detection:
    #    - CBA Article 9: players aged 18-24 (or 25 for late Euro/NCAA signings) on their first NHL SPC
    #    - Always two-way (minor_league_salary > 0) and base+signing-bonus AAV <= ELC cap (~$1.15M)
    elc_cap = 1_150_000 if start_yr >= 2025 else (1_050_000 if start_yr >= 2022 else 975_000)
    if (
        contract_index == 0
        and first_yr_minor_salary > 0
        and aav_usd <= elc_cap
        and age_s15 <= 25
        and (
            (prior_acc == 0 and prior_gp < 30)
            or (start_yr == FIRST_HIST_YEAR and age_s15 <= 22 and prior_acc <= 2)
        )
    ):
        return "ELC", f"First NHL SPC (age {age_s15} on Sept 15, {start_yr}; two-way <= ${elc_cap:,})"

    # 2. Group 3 UFA at signing (age >= 27 on June 30 OR >= 7 accrued NHL seasons)
    if age_j30 >= 27:
        return "UFA", f"Group 3 UFA (age {age_j30} on June 30, {start_yr})"
    if prior_acc >= 7:
        return "UFA", f"Group 3 UFA ({prior_acc} accrued NHL seasons prior to {season_str(start_yr)})"

    # 3. Group 6 UFA at signing (age >= 25 on June 30, >= 3 pro seasons, < 80 skater / < 28 goalie NHL GP)
    g6_gp = 28 if pos == "G" else 80
    if age_j30 >= 25 and prior_pro >= 3 and prior_gp < g6_gp:
        return "UFA", f"Group 6 UFA (age {age_j30}, {prior_pro} pro seasons, {prior_gp} NHL GP < {g6_gp})"

    return "RFA", f"Age {age_j30} on June 30, {start_yr} (<27 and {prior_acc} accrued NHL seasons)"


def compute_historical_expiry_status(
    bio: dict | None,
    pos: str,
    end_yr: int,
    is_right_censored_unknown_end: bool = False,
) -> tuple[str | None, str | None]:
    """Deterministic CBA Article 10.1 expiry status on June 30 of (end_yr + 1).
    Uses actual realized NHL GP for completed contracts (end_yr <= 2025) and projects remaining
    seasons for active 2026-27+ contracts."""
    if not bio or not bio.get("birth_date"):
        return None, None
    if is_right_censored_unknown_end:
        return None, "Active in 2026-27; multi-year end season not publicly verified"

    exp_year = end_yr + 1
    bd = bio["birth_date"]
    age_j30 = age_on(bd, exp_year, 6, 30)

    if age_j30 >= 27:
        return "UFA", f"Group 3 UFA (age {age_j30} on June 30, {exp_year})"

    # Accrued NHL seasons through end_yr
    if end_yr < LAST_HIST_YEAR:
        # Completed historical contract — use exact realized GP!
        acc_total = accrued_seasons_before(bio, pos, exp_year)
        if acc_total >= 7:
            return "UFA", f"Group 3 UFA ({acc_total} realized accrued NHL seasons through {season_str(end_yr)})"
        g6_gp = 28 if pos == "G" else 80
        realized_gp = career_nhl_gp_before(bio, exp_year)
        realized_pro = pro_seasons_before(bio, exp_year)
        if age_j30 >= 25 and realized_pro >= 3 and realized_gp < g6_gp:
            return "UFA", f"Group 6 UFA (age {age_j30}, {realized_pro} pro seasons, {realized_gp} NHL GP < {g6_gp})"
        return "RFA", f"Age {age_j30} on June 30, {exp_year} (<27 and {acc_total} accrued NHL seasons)"
    else:
        # Active / future expiring contract (end_yr >= 2026): realized prior to 2026-27 + remaining seasons
        acc_prior = accrued_seasons_before(bio, pos, LAST_HIST_YEAR)
        remaining = max(1, exp_year - LAST_HIST_YEAR)
        if acc_prior + remaining >= 7:
            return "UFA", f"Group 3 UFA (projected {acc_prior + remaining} accrued NHL seasons)"
        g6_gp = 28 if pos == "G" else 80
        cur_gp = career_nhl_gp_before(bio, LAST_HIST_YEAR + 1)
        proj_pro = pro_seasons_before(bio, LAST_HIST_YEAR + 1) + max(0, exp_year - (LAST_HIST_YEAR + 1))
        if age_j30 >= 25 and proj_pro >= 3 and cur_gp < g6_gp:
            need = g6_gp - cur_gp
            max_possible = remaining * 82
            if need > max_possible:
                return "UFA", f"Group 6 UFA (age {age_j30}; needs {need} GP in {remaining} season(s))"
            return "RFA", f"RFA if reaching {g6_gp} career NHL GP ({need} more GP needed); otherwise Group 6 UFA"
        return "RFA", f"Age {age_j30} on June 30, {exp_year} (<27 and <7 accrued seasons)"


def segment_player_contracts(
    pid: int,
    yearly: dict[str, dict],
    active_2026_rec: dict | None,
) -> list[dict]:
    """Segment a player's 2010-11..2026-27 yearly NHLPA records into discrete contracts."""
    forced_splits: set[int] = set(KNOWN_SAME_AAV_SPLITS.get(pid, set()))
    if active_2026_rec and active_2026_rec.get("start_season"):
        try:
            forced_splits.add(int(active_2026_rec["start_season"].split("-")[0]))
        except Exception:
            pass

    yr_items = sorted((int(s.split("-")[0]), s, rec) for s, rec in yearly.items())
    segs: list[dict] = []

    for yr, s_lbl, rec in yr_items:
        aav = rec["aav_usd"]
        if segs:
            prev = segs[-1]
            same_aav = abs(aav - prev["aav_usd"]) <= 2_500
            contiguous = (yr == prev["end_yr"] + 1)
            # Post-2013 CBA capped contracts at 8 years; extensions signed before the 2012-13 lockout
            # (starting <= 2013-14, e.g. Crosby's 12y $8.7M deal) were grandfathered up to 15 years.
            max_cba_len = 8 if prev["start_yr"] >= 2014 else 15
            too_long = (yr - prev["start_yr"] + 1) > max_cba_len
            # Large upward salary jump (+>= $2.5M) after >= 4 years at same AAV signals a new same-AAV extension
            prev_last_sal = prev["years"][season_str(prev["end_yr"])]["total_salary_usd"]
            big_jump_after_long = (
                same_aav
                and (prev["end_yr"] - prev["start_yr"] + 1) >= 4
                and (rec["total_salary_usd"] - prev_last_sal) >= 2_500_000
            )

            if contiguous and same_aav and (yr not in forced_splits) and not too_long and not big_jump_after_long:
                prev["end_yr"] = yr
                prev["years"][s_lbl] = rec
                continue

        segs.append({
            "start_yr": yr,
            "end_yr": yr,
            "aav_usd": aav,
            "years": {s_lbl: rec},
        })

    return segs


def main() -> None:
    hist = json.loads(NHLPA_HIST_PATH.read_text(encoding="utf-8"))
    bios = json.loads(BIOS_PATH.read_text(encoding="utf-8"))
    active_league = json.loads(ACTIVE_2026_PATH.read_text(encoding="utf-8"))
    active_by_pid = {c["player_id"]: c for c in active_league["contracts"]}

    all_contracts: list[dict] = []
    all_player_seasons: list[dict] = []

    season_stats: dict[str, dict] = defaultdict(lambda: {
        "players": 0, "total_aav_usd": 0, "total_salary_usd": 0,
        "total_base_salary_usd": 0, "total_signing_bonus_usd": 0, "two_way_players": 0,
    })
    signing_counts = Counter()
    expiry_counts = Counter()
    exact_calc_matches = 0
    complete_uncensored = 0

    for pid_str in sorted(hist["players"].keys(), key=lambda x: int(x)):
        p = hist["players"][pid_str]
        pid = int(pid_str)
        bio = bios.get(pid_str)
        pos = (bio or {}).get("position") or p.get("position") or ""
        active_rec = active_by_pid.get(pid)
        yearly = p.get("yearly_compensation") or {}
        if not yearly:
            continue

        # Record tidy player-season rows
        for s_lbl, rec in sorted(yearly.items()):
            ss = season_stats[s_lbl]
            ss["players"] += 1
            ss["total_aav_usd"] += rec["aav_usd"]
            ss["total_salary_usd"] += rec["total_salary_usd"]
            ss["total_base_salary_usd"] += rec["base_salary_usd"]
            ss["total_signing_bonus_usd"] += rec["signing_bonus_usd"]
            if rec["minor_league_salary_usd"] > 0:
                ss["two_way_players"] += 1
            all_player_seasons.append({
                "season": s_lbl,
                "player_id": pid,
                "player_name": p["name"],
                "position": pos,
                "team": rec["team"],
                "aav_usd": rec["aav_usd"],
                "total_salary_usd": rec["total_salary_usd"],
                "base_salary_usd": rec["base_salary_usd"],
                "signing_bonus_usd": rec["signing_bonus_usd"],
                "minor_league_salary_usd": rec["minor_league_salary_usd"],
                "two_way": rec["minor_league_salary_usd"] > 0,
                "nhlpa_url": p.get("nhlpa_compensation_url") or "https://www.nhlpa.com/compensation-by-team",
            })

        segs = segment_player_contracts(pid, yearly, active_rec)
        for idx, seg in enumerate(segs):
            s_yr = seg["start_yr"]
            obs_end_yr = seg["end_yr"]
            obs_years = len(seg["years"])
            is_active = (obs_end_yr == LAST_HIST_YEAR)
            is_left_censored = (s_yr == FIRST_HIST_YEAR)

            # Determine known end_yr, term_years, source_url if active 2026-27 NHL roster player
            verified_term = None
            end_yr = obs_end_yr
            signed_date = ""
            signing_team = seg["years"][season_str(s_yr)]["team"]
            source_url = None
            secondary_url = None
            future_ext = None
            override_sign_status = None
            is_right_censored_unknown = False

            if is_active and active_rec:
                if active_rec.get("end_season"):
                    end_yr = int(active_rec["end_season"].split("-")[0])
                if active_rec.get("term_years"):
                    verified_term = int(active_rec["term_years"])
                signed_date = active_rec.get("signed_date") or ""
                if active_rec.get("signing_team"):
                    signing_team = active_rec["signing_team"]
                source_url = active_rec.get("source_url")
                secondary_url = active_rec.get("secondary_source_url")
                future_ext = active_rec.get("future_extension")
                override_sign_status = active_rec.get("signing_status")
            elif is_active:
                # Non-roster / AHL player active in 2026-27 — end season beyond 2026-27 not yet observed
                is_right_censored_unknown = True

            term_years = verified_term if verified_term else (
                (end_yr - s_yr + 1) if (not is_left_censored and not is_right_censored_unknown) else None
            )

            # Calculate mean total_salary (base + signing bonus) across observed seasons
            sum_sal = sum(v["total_salary_usd"] for v in seg["years"].values())
            calc_aav = int(round(sum_sal / obs_years))
            nhlpa_aav = seg["aav_usd"]
            total_value = (nhlpa_aav * term_years) if term_years else sum_sal

            if not is_left_censored and not is_active:
                complete_uncensored += 1
                if abs(calc_aav - nhlpa_aav) <= 5_000:
                    exact_calc_matches += 1

            first_rec = seg["years"][season_str(s_yr)]
            teams_list = list(dict.fromkeys(v["team"] for v in seg["years"].values() if v.get("team")))
            two_way_any = any(v["minor_league_salary_usd"] > 0 for v in seg["years"].values())

            sign_status, sign_note = compute_historical_signing_status(
                bio, pos, s_yr, nhlpa_aav, first_rec["minor_league_salary_usd"], idx, override_sign_status
            )
            exp_status, exp_note = compute_historical_expiry_status(
                bio, pos, end_yr, is_right_censored_unknown
            )

            if sign_status:
                signing_counts[sign_status] += 1
            if exp_status:
                expiry_counts[exp_status] += 1

            nhlpa_url = p.get("nhlpa_compensation_url") or "https://www.nhlpa.com/compensation-by-team"
            if not source_url:
                source_url = nhlpa_url

            for u in (nhlpa_url, source_url, secondary_url, (future_ext or {}).get("source_url")):
                assert_no_cap_site(u)

            contract_id = f"{pid}_{season_str(s_yr)}"
            all_contracts.append({
                "contract_id": contract_id,
                "player_id": pid,
                "player_name": p["name"],
                "position": pos,
                "birth_date": (bio or {}).get("birth_date"),
                "draft_year": (bio or {}).get("draft_year"),
                "signing_team": signing_team,
                "teams": teams_list,
                "start_season": season_str(s_yr),
                "end_season": season_str(end_yr) if not is_right_censored_unknown else None,
                "observed_end_season": season_str(obs_end_yr),
                "term_years": term_years,
                "observed_years": obs_years,
                "signed_date": signed_date,
                "signing_status": sign_status,
                "signing_status_note": sign_note,
                "expiry_status": exp_status,
                "expiry_status_note": exp_note,
                "aav_usd": nhlpa_aav,
                "calculated_aav_usd": calc_aav,
                "total_value_usd": total_value,
                "two_way": two_way_any,
                "is_active_2026_27": is_active,
                "is_left_censored_2010": is_left_censored,
                "is_right_censored_2026": is_right_censored_unknown,
                "nhlpa_url": nhlpa_url,
                "source_url": source_url,
                "secondary_source_url": secondary_url,
                "future_extension": future_ext,
                "yearly_compensation": seg["years"],
            })

    # Write historical_contracts_2010_2027.csv
    csv_fields = [
        "contract_id", "player_id", "player_name", "position", "birth_date", "draft_year",
        "signing_team", "teams", "start_season", "end_season", "observed_end_season",
        "term_years", "observed_years", "signed_date",
        "signing_status", "expiry_status", "expiry_status_note",
        "aav_usd", "calculated_aav_usd", "total_value_usd", "two_way",
        "is_active_2026_27", "is_left_censored_2010", "is_right_censored_2026",
        "nhlpa_url", "source_url", "secondary_source_url",
        "yearly_compensation_json",
    ]
    csv_path = DATA_DIR / "historical_contracts_2010_2027.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for c in all_contracts:
            w.writerow({
                "contract_id": c["contract_id"],
                "player_id": c["player_id"],
                "player_name": c["player_name"],
                "position": c["position"],
                "birth_date": c["birth_date"] or "",
                "draft_year": c["draft_year"] or "",
                "signing_team": c["signing_team"] or "",
                "teams": ",".join(c["teams"]),
                "start_season": c["start_season"],
                "end_season": c["end_season"] or "",
                "observed_end_season": c["observed_end_season"],
                "term_years": c["term_years"] if c["term_years"] is not None else "",
                "observed_years": c["observed_years"],
                "signed_date": c["signed_date"],
                "signing_status": c["signing_status"] or "",
                "expiry_status": c["expiry_status"] or "",
                "expiry_status_note": c["expiry_status_note"] or "",
                "aav_usd": c["aav_usd"],
                "calculated_aav_usd": c["calculated_aav_usd"],
                "total_value_usd": c["total_value_usd"],
                "two_way": c["two_way"],
                "is_active_2026_27": c["is_active_2026_27"],
                "is_left_censored_2010": c["is_left_censored_2010"],
                "is_right_censored_2026": c["is_right_censored_2026"],
                "nhlpa_url": c["nhlpa_url"],
                "source_url": c["source_url"],
                "secondary_source_url": c["secondary_source_url"] or "",
                "yearly_compensation_json": json.dumps(c["yearly_compensation"], separators=(",", ":")),
            })

    # Write tidy player-season CSV
    ps_fields = [
        "season", "player_id", "player_name", "position", "team",
        "aav_usd", "total_salary_usd", "base_salary_usd", "signing_bonus_usd",
        "minor_league_salary_usd", "two_way", "nhlpa_url",
    ]
    ps_path = DATA_DIR / "historical_player_seasons_2010_2027.csv"
    with ps_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ps_fields)
        w.writeheader()
        w.writerows(all_player_seasons)

    summary = {
        "license": "CC0-1.0",
        "span": "2010-11 through 2026-27 (17 seasons)",
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "seasons": len(season_stats),
            "unique_players": len(hist["players"]),
            "player_bios_matched": sum(1 for k in hist["players"] if k in bios),
            "player_season_records": len(all_player_seasons),
            "total_contracts": len(all_contracts),
            "contracts_with_signing_status": sum(1 for c in all_contracts if c["signing_status"]),
            "contracts_with_expiry_status": sum(1 for c in all_contracts if c["expiry_status"]),
            "complete_uncensored_contracts": complete_uncensored,
            "calculated_aav_exact_match_pct": round(100.0 * exact_calc_matches / max(1, complete_uncensored), 1),
            "signing_status_breakdown": dict(signing_counts),
            "expiry_status_breakdown": dict(expiry_counts),
            "cap_site_urls": 0,
        },
        "by_season": dict(sorted(season_stats.items())),
    }

    (DATA_DIR / "historical_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (DATA_DIR / "historical_contracts_2010_2027.json").write_text(
        json.dumps({"summary": summary, "contracts": all_contracts}, separators=(",", ":")),
        encoding="utf-8",
    )

    print("=== 17-SEASON HISTORICAL NHL CONTRACTS DATABASE COMPLETE (2010-11..2026-27) ===")
    print(json.dumps(summary["totals"], indent=2))
    print("\nSeason-by-Season Player Count & Total Compensation:")
    for s, st in sorted(season_stats.items()):
        print(f"  {s} | {st['players']:4} players | Total AAV ${st['total_aav_usd']:>13,} | Cash ${st['total_salary_usd']:>13,} | 2-Way {st['two_way_players']:3}")


if __name__ == "__main__":
    main()
