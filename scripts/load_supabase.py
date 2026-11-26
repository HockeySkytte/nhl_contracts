r"""Create and populate the 5 Supabase (PostgreSQL) tables:
  1. public.contracts            (12,095 historical/active contracts + 30 signed 2027-28+ extensions)
  2. public.contract_years       (25,712 observed NHLPA years 2010-27 + ~1,400 projected future years 2027-35)
  3. public.contract_teams_years (team-level cap sheets 2010-11..2034-35 incl. buyouts, retained salary & bonus overages)
  4. public.trades               (1,319 official NHL trades, 2015-16..2026-27)
  5. public.injuries             (live current NHL injuries snapshot)

Reads DATABASE_HS_URL from C:\Apps\NHL\.env (or DATABASE_HS_URL env var).
Idempotent: uses UPSERT for contracts/contract_years/contract_teams_years/trades and atomic refresh for injuries.
Also exports data/contract_teams_years_2010_2035.csv for local analysis.
"""

from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_historical_contracts as bhc

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
BIOS_PATH = ROOT / "cache" / "nhl" / "player_bios_2010_2026.json"
SCHEMA_PATH = ROOT / "sql" / "schema.sql"
DEFAULT_ENV_PATH = Path(r"C:\Apps\NHL\.env")

# Official NHL Salary Cap Upper & Lower Limits (2010-11 through 2027-28; 2028-29+ NULL/unannounced)
NHL_SALARY_CAP: dict[int, tuple[int, int]] = {
    2010: (59_400_000, 43_400_000),
    2011: (64_300_000, 48_300_000),
    2012: (60_000_000, 44_000_000),  # 2012-13 lockout prorated / $70.2M transitional ceiling
    2013: (64_300_000, 44_000_000),
    2014: (69_000_000, 51_000_000),
    2015: (71_400_000, 52_800_000),
    2016: (73_000_000, 54_000_000),
    2017: (75_000_000, 55_400_000),
    2018: (79_500_000, 58_800_000),
    2019: (81_500_000, 60_200_000),
    2020: (81_500_000, 60_200_000),
    2021: (81_500_000, 60_200_000),
    2022: (82_500_000, 61_000_000),
    2023: (83_500_000, 61_700_000),
    2024: (88_000_000, 65_000_000),
    2025: (95_500_000, 70_600_000),
    2026: (104_000_000, 76_900_000),
    2027: (113_500_000, 83_900_000),  # Official NHL/NHLPA 3-year cap agreement
}

# Verified year-by-year buyout step-downs for multi-year buyouts extending past 2026-27
# (keyed by (team, player_name) -> {season_year: cap_hit_usd})
MULTIYEAR_PENALTY_OVERRIDES: dict[tuple[str, str], dict[int, int]] = {
    ("BUF", "Jeff Skinner"): {2026: 6_444_444, 2027: 2_444_444, 2028: 2_444_444, 2029: 2_444_444},
    ("EDM", "Jack Campbell"): {2026: 2_600_000, 2027: 1_500_000, 2028: 1_500_000, 2029: 1_500_000},
    ("UTA", "Oliver Ekman-Larsson"): {2026: 650_000, 2027: 290_000, 2028: 290_000, 2029: 290_000, 2030: 290_000},
    ("VAN", "Oliver Ekman-Larsson"): {2026: 4_766_667, 2027: 2_126_667, 2028: 2_126_667, 2029: 2_126_667, 2030: 2_126_667},
}


def load_database_url() -> str:
    if os.environ.get("DATABASE_HS_URL"):
        return os.environ["DATABASE_HS_URL"]
    if DEFAULT_ENV_PATH.exists():
        for line in DEFAULT_ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                if k.strip() == "DATABASE_HS_URL":
                    return v.strip()
    raise RuntimeError("DATABASE_HS_URL not found in environment or C:\\Apps\\NHL\\.env")


def parse_date(val: str | None) -> str | None:
    if not val:
        return None
    s = str(val).strip()
    return s[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", s) else None


def parse_season_start(season: str | None) -> int | None:
    if not season:
        return None
    m = re.match(r"^(\d{4})-", str(season))
    return int(m.group(1)) if m else None


def season_str(yr: int) -> str:
    return f"{yr}-{str(yr + 1)[-2:]}"


def season_to_id(yr: int) -> int:
    return yr * 10000 + (yr + 1)


def expand_penalties_by_team_year() -> dict[tuple[str, int], list[dict]]:
    """Expand all verified cap penalties across 2026-27 AND future seasons (2027-28..2030-31)."""
    by_tm_yr: dict[tuple[str, int], list[dict]] = defaultdict(list)
    csv_p = DATA_DIR / "all_cap_penalties.csv"
    if not csv_p.exists():
        return by_tm_yr

    with csv_p.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            tm = row["team"].strip()
            ptype = row["penalty_type"].strip()
            pname = row["player_name"].strip()
            s_yr = max(2026, parse_season_start(row["start_season"]) or 2026)
            e_yr = parse_season_start(row["end_season"]) or s_yr
            cur_hit = int(row["current_season_cap_hit_usd"])
            sched = MULTIYEAR_PENALTY_OVERRIDES.get((tm, pname))

            for yr in range(s_yr, e_yr + 1):
                hit = sched.get(yr, cur_hit) if sched else cur_hit
                by_tm_yr[(tm, yr)].append({
                    "penalty_type": ptype,
                    "player_name": pname,
                    "cap_hit_usd": hit,
                    "start_season": row["start_season"],
                    "end_season": row["end_season"],
                    "source_url": row["source_url"],
                    "notes": row.get("notes") or "",
                })
    return by_tm_yr


def load_contracts_years_and_teams(cur) -> tuple[int, int, int, int]:
    hist = json.loads((DATA_DIR / "historical_contracts_2010_2027.json").read_text(encoding="utf-8"))
    active_league = json.loads((DATA_DIR / "all_contracts.json").read_text(encoding="utf-8"))
    bios = json.loads(BIOS_PATH.read_text(encoding="utf-8")) if BIOS_PATH.exists() else {}
    active_by_pid = {c["player_id"]: c for c in active_league["contracts"]}
    contracts = hist["contracts"]

    contract_rows = []
    year_rows = []
    seen_contract_ids: set[str] = set()
    projected_year_count = 0
    ext_contract_count = 0

    for c in contracts:
        cid = c["contract_id"]
        seen_contract_ids.add(cid)
        pid = int(c["player_id"])
        s_yr = parse_season_start(c["start_season"])
        e_yr = parse_season_start(c.get("end_season"))
        obs_e_yr = parse_season_start(c["observed_end_season"])
        is_active = bool(c.get("is_active_2026_27", False))
        active_rec = active_by_pid.get(pid) if is_active else None
        cur_team = (active_rec["current_team"] if active_rec else (c.get("teams") or [c.get("signing_team")])[-1])

        contract_rows.append((
            cid,
            pid,
            c["player_name"],
            c.get("position") or None,
            parse_date(c.get("birth_date")),
            int(c["draft_year"]) if c.get("draft_year") else None,
            c.get("signing_team") or None,
            c.get("teams") or [],
            s_yr,
            e_yr,
            obs_e_yr,
            c["start_season"],
            c.get("end_season") or None,
            c["observed_end_season"],
            int(c["term_years"]) if c.get("term_years") is not None else None,
            int(c["observed_years"]),
            parse_date(c.get("signed_date")),
            c.get("signing_status") or None,
            c.get("signing_status_note") or None,
            c.get("expiry_status") or None,
            c.get("expiry_status_note") or None,
            int(c["aav_usd"]),
            int(c["calculated_aav_usd"]) if c.get("calculated_aav_usd") is not None else None,
            int(c["total_value_usd"]) if c.get("total_value_usd") is not None else None,
            bool(c.get("two_way", False)),
            is_active,
            False,  # is_future_extension
            bool(c.get("is_left_censored_2010", False)),
            bool(c.get("is_right_censored_2026", False)),
            c.get("nhlpa_url") or None,
            c.get("source_url") or None,
            c.get("secondary_source_url") or None,
            Json(c["future_extension"]) if c.get("future_extension") else None,
        ))

        # 1. Observed NHLPA yearly rows (2010-11..2026-27)
        obs_years_set: set[int] = set()
        for s_lbl, yrec in sorted((c.get("yearly_compensation") or {}).items()):
            yr = parse_season_start(s_lbl)
            if yr is None:
                continue
            obs_years_set.add(yr)
            minor = int(yrec.get("minor_league_salary_usd") or 0)
            year_rows.append((
                cid,
                pid,
                c["player_name"],
                c.get("position") or None,
                yr,
                season_to_id(yr),
                s_lbl,
                yrec.get("team") or cur_team,
                int(yrec["aav_usd"]),
                int(yrec["total_salary_usd"]),
                int(yrec["base_salary_usd"]),
                int(yrec["signing_bonus_usd"]),
                minor,
                minor > 0,
                False,  # is_projected
                c.get("nhlpa_url") or None,
            ))

        # 2. Projected remaining future years (2027-28..end_year) of active 2026-27 multi-year contracts
        if is_active and e_yr and e_yr >= 2027:
            for yr in range(2027, e_yr + 1):
                if yr in obs_years_set:
                    continue
                projected_year_count += 1
                year_rows.append((
                    cid,
                    pid,
                    c["player_name"],
                    c.get("position") or None,
                    yr,
                    season_to_id(yr),
                    season_str(yr),
                    cur_team,
                    int(c["aav_usd"]),
                    None,  # total_salary_usd not yet published by NHLPA
                    None,  # base_salary_usd
                    None,  # signing_bonus_usd
                    None,  # minor_league_salary_usd
                    bool(c.get("two_way", False)),
                    True,  # is_projected
                    c.get("nhlpa_url") or None,
                ))

        # 3. Signed future extensions (2027-28+) as first-class rows in contracts + contract_years!
        fe = c.get("future_extension")
        if is_active and fe and fe.get("start_season") and fe.get("end_season") and fe.get("aav_usd"):
            fe_s_yr = parse_season_start(fe["start_season"])
            fe_e_yr = parse_season_start(fe["end_season"])
            if fe_s_yr and fe_e_yr and fe_s_yr >= 2027:
                fe_cid = f"{pid}_{fe['start_season']}"
                if fe_cid not in seen_contract_ids:
                    seen_contract_ids.add(fe_cid)
                    ext_contract_count += 1
                    fe_term = int(fe.get("term_years") or (fe_e_yr - fe_s_yr + 1))
                    fe_aav = int(fe["aav_usd"])
                    fe_tot = int(fe.get("total_value_usd") or (fe_aav * fe_term))
                    bio = bios.get(str(pid))
                    pos = c.get("position") or ""
                    # Signing status of extension = expiry status of the preceding active contract!
                    fe_sign_status = c.get("expiry_status") or "RFA"
                    fe_sign_note = f"Signed extension commencing {fe['start_season']} (preceding contract expires as {fe_sign_status})"
                    fe_exp_status, fe_exp_note = bhc.compute_historical_expiry_status(bio, pos, fe_e_yr, False)

                    contract_rows.append((
                        fe_cid,
                        pid,
                        c["player_name"],
                        pos or None,
                        parse_date(c.get("birth_date")),
                        int(c["draft_year"]) if c.get("draft_year") else None,
                        cur_team,
                        [cur_team] if cur_team else [],
                        fe_s_yr,
                        fe_e_yr,
                        fe_e_yr,
                        fe["start_season"],
                        fe["end_season"],
                        fe["end_season"],
                        fe_term,
                        0,  # observed_years (not yet played)
                        parse_date(fe.get("signed_date")),
                        fe_sign_status,
                        fe_sign_note,
                        fe_exp_status,
                        fe_exp_note,
                        fe_aav,
                        None,  # calculated_aav_usd (projected)
                        fe_tot,
                        False,  # extensions of multi-million deals are one-way
                        False,  # is_active_2026_27
                        True,   # is_future_extension
                        False,
                        False,
                        c.get("nhlpa_url") or None,
                        fe.get("source_url") or None,
                        None,
                        None,
                    ))

                    for yr in range(fe_s_yr, fe_e_yr + 1):
                        projected_year_count += 1
                        year_rows.append((
                            fe_cid,
                            pid,
                            c["player_name"],
                            pos or None,
                            yr,
                            season_to_id(yr),
                            season_str(yr),
                            cur_team,
                            fe_aav,
                            None,
                            None,
                            None,
                            None,
                            False,
                            True,  # is_projected
                            c.get("nhlpa_url") or None,
                        ))

    # Build contract_teams_years aggregates (2010-11..2034-35) directly from year_rows + expanded cap penalties
    penalties_by_tm_yr = expand_penalties_by_team_year()
    agg: dict[tuple[str, int], dict] = {}

    def _get_bucket(tm: str, yr: int) -> dict:
        k = (tm, yr)
        if k not in agg:
            agg[k] = {
                "team": tm,
                "season_year": yr,
                "is_projected": yr >= 2027,
                "contracts_count": 0,
                "one_way_contracts_count": 0,
                "two_way_contracts_count": 0,
                "total_aav_usd": 0,
                "one_way_aav_usd": 0,
                "total_salary_usd": 0,
                "total_base_salary_usd": 0,
                "total_signing_bonus_usd": 0,
                "has_cash": False,
            }
        return agg[k]

    for row in year_rows:
        tm = row[7]
        yr = row[4]
        if not tm:
            continue
        b = _get_bucket(tm, yr)
        aav = row[8]
        tot_sal, base_sal, sb_sal = row[9], row[10], row[11]
        is_2w = bool(row[13])
        b["contracts_count"] += 1
        if is_2w:
            b["two_way_contracts_count"] += 1
        else:
            b["one_way_contracts_count"] += 1
            b["one_way_aav_usd"] += aav
        b["total_aav_usd"] += aav
        if tot_sal is not None:
            b["has_cash"] = True
            b["total_salary_usd"] += tot_sal
            b["total_base_salary_usd"] += (base_sal or 0)
            b["total_signing_bonus_usd"] += (sb_sal or 0)

    # Ensure any (team, yr) with a future cap penalty also exists in contract_teams_years
    for (tm, yr) in penalties_by_tm_yr:
        _get_bucket(tm, yr)

    team_year_rows = []
    team_year_csv = []
    for (tm, yr) in sorted(agg.keys(), key=lambda x: (x[1], x[0])):
        b = agg[(tm, yr)]
        pens = penalties_by_tm_yr.get((tm, yr), [])
        buyout_usd = sum(p["cap_hit_usd"] for p in pens if p["penalty_type"] in ("buyout", "retained_buyout"))
        ret_usd = sum(p["cap_hit_usd"] for p in pens if p["penalty_type"] == "retained_salary")
        bonus_usd = sum(p["cap_hit_usd"] for p in pens if p["penalty_type"] == "bonus_overage")
        tot_pen = buyout_usd + ret_usd + bonus_usd
        comb_all = b["total_aav_usd"] + tot_pen
        comb_1w = b["one_way_aav_usd"] + tot_pen
        cap_ceil, cap_floor = NHL_SALARY_CAP.get(yr, (None, None))
        cap_space = (cap_ceil - comb_1w) if cap_ceil is not None else None

        tot_sal = b["total_salary_usd"] if b["has_cash"] else None
        base_sal = b["total_base_salary_usd"] if b["has_cash"] else None
        sb_sal = b["total_signing_bonus_usd"] if b["has_cash"] else None

        team_year_rows.append((
            tm, yr, season_to_id(yr), season_str(yr), b["is_projected"],
            b["contracts_count"], b["one_way_contracts_count"], b["two_way_contracts_count"],
            b["total_aav_usd"], b["one_way_aav_usd"],
            tot_sal, base_sal, sb_sal,
            buyout_usd, ret_usd, bonus_usd, tot_pen,
            comb_all, comb_1w,
            cap_ceil, cap_floor, cap_space,
            Json(pens) if pens else None,
        ))
        team_year_csv.append({
            "team": tm,
            "season_year": yr,
            "season_id": season_to_id(yr),
            "season": season_str(yr),
            "is_projected": b["is_projected"],
            "contracts_count": b["contracts_count"],
            "one_way_contracts_count": b["one_way_contracts_count"],
            "two_way_contracts_count": b["two_way_contracts_count"],
            "total_aav_usd": b["total_aav_usd"],
            "one_way_aav_usd": b["one_way_aav_usd"],
            "total_salary_usd": tot_sal if tot_sal is not None else "",
            "total_base_salary_usd": base_sal if base_sal is not None else "",
            "total_signing_bonus_usd": sb_sal if sb_sal is not None else "",
            "buyout_penalty_usd": buyout_usd,
            "retained_salary_penalty_usd": ret_usd,
            "bonus_overage_penalty_usd": bonus_usd,
            "total_cap_penalties_usd": tot_pen,
            "combined_aav_plus_penalties_usd": comb_all,
            "combined_one_way_plus_penalties_usd": comb_1w,
            "cap_ceiling_usd": cap_ceil if cap_ceil is not None else "",
            "cap_floor_usd": cap_floor if cap_floor is not None else "",
            "projected_cap_space_usd": cap_space if cap_space is not None else "",
            "penalties_detail_json": json.dumps(pens, separators=(",", ":")) if pens else "",
        })

    # Export local CSV mirror of contract_teams_years
    cty_csv = DATA_DIR / "contract_teams_years_2010_2035.csv"
    with cty_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(team_year_csv[0].keys()))
        w.writeheader()
        w.writerows(team_year_csv)

    # Clear stale projected/future rows before upsert so removed extensions never linger
    cur.execute("DELETE FROM public.contract_years WHERE is_projected = TRUE;")
    cur.execute("DELETE FROM public.contracts WHERE is_future_extension = TRUE;")
    cur.execute("TRUNCATE TABLE public.contract_teams_years;")

    execute_values(
        cur,
        """
        INSERT INTO public.contracts (
            contract_id, player_id, player_name, position, birth_date, draft_year,
            signing_team, teams, start_year, end_year, observed_end_year,
            start_season, end_season, observed_end_season, term_years, observed_years,
            signed_date, contract_status, contract_status_note, expiry_status, expiry_status_note,
            aav_usd, calculated_aav_usd, total_value_usd, two_way,
            is_active_2026_27, is_future_extension, is_left_censored_2010, is_right_censored_2026,
            nhlpa_url, source_url, secondary_source_url, future_extension
        ) VALUES %s
        ON CONFLICT (contract_id) DO UPDATE SET
            player_name = EXCLUDED.player_name,
            position = EXCLUDED.position,
            birth_date = EXCLUDED.birth_date,
            draft_year = EXCLUDED.draft_year,
            signing_team = EXCLUDED.signing_team,
            teams = EXCLUDED.teams,
            end_year = EXCLUDED.end_year,
            observed_end_year = EXCLUDED.observed_end_year,
            end_season = EXCLUDED.end_season,
            observed_end_season = EXCLUDED.observed_end_season,
            term_years = EXCLUDED.term_years,
            observed_years = EXCLUDED.observed_years,
            signed_date = EXCLUDED.signed_date,
            contract_status = EXCLUDED.contract_status,
            contract_status_note = EXCLUDED.contract_status_note,
            expiry_status = EXCLUDED.expiry_status,
            expiry_status_note = EXCLUDED.expiry_status_note,
            aav_usd = EXCLUDED.aav_usd,
            calculated_aav_usd = EXCLUDED.calculated_aav_usd,
            total_value_usd = EXCLUDED.total_value_usd,
            two_way = EXCLUDED.two_way,
            is_active_2026_27 = EXCLUDED.is_active_2026_27,
            is_future_extension = EXCLUDED.is_future_extension,
            is_left_censored_2010 = EXCLUDED.is_left_censored_2010,
            is_right_censored_2026 = EXCLUDED.is_right_censored_2026,
            nhlpa_url = EXCLUDED.nhlpa_url,
            source_url = EXCLUDED.source_url,
            secondary_source_url = EXCLUDED.secondary_source_url,
            future_extension = EXCLUDED.future_extension,
            updated_at = NOW()
        """,
        contract_rows,
        page_size=500,
    )

    execute_values(
        cur,
        """
        INSERT INTO public.contract_years (
            contract_id, player_id, player_name, position,
            season_year, season_id, season, team,
            aav_usd, total_salary_usd, base_salary_usd, signing_bonus_usd,
            minor_league_salary_usd, two_way, is_projected, nhlpa_url
        ) VALUES %s
        ON CONFLICT (contract_id, season_year) DO UPDATE SET
            player_name = EXCLUDED.player_name,
            position = EXCLUDED.position,
            team = EXCLUDED.team,
            aav_usd = EXCLUDED.aav_usd,
            total_salary_usd = EXCLUDED.total_salary_usd,
            base_salary_usd = EXCLUDED.base_salary_usd,
            signing_bonus_usd = EXCLUDED.signing_bonus_usd,
            minor_league_salary_usd = EXCLUDED.minor_league_salary_usd,
            two_way = EXCLUDED.two_way,
            is_projected = EXCLUDED.is_projected,
            nhlpa_url = EXCLUDED.nhlpa_url
        """,
        year_rows,
        page_size=1000,
    )

    execute_values(
        cur,
        """
        INSERT INTO public.contract_teams_years (
            team, season_year, season_id, season, is_projected,
            contracts_count, one_way_contracts_count, two_way_contracts_count,
            total_aav_usd, one_way_aav_usd,
            total_salary_usd, total_base_salary_usd, total_signing_bonus_usd,
            buyout_penalty_usd, retained_salary_penalty_usd, bonus_overage_penalty_usd,
            total_cap_penalties_usd, combined_aav_plus_penalties_usd, combined_one_way_plus_penalties_usd,
            cap_ceiling_usd, cap_floor_usd, projected_cap_space_usd, penalties_detail
        ) VALUES %s
        """,
        team_year_rows,
        page_size=500,
    )

    return len(contract_rows), len(year_rows), projected_year_count, len(team_year_rows)


def load_trades(cur) -> int:
    tdb = json.loads((DATA_DIR / "trades_2015_2027.json").read_text(encoding="utf-8"))
    trades = tdb["trades"]

    rows = []
    for t in trades:
        s_yr = parse_season_start(t["season"]) or int(t["trade_date"][:4])
        tms = t.get("teams") or []
        rows.append((
            t["trade_id"],
            t["season"],
            s_yr,
            season_to_id(s_yr),
            parse_date(t["trade_date"]),
            int(t["num_teams"]),
            bool(t.get("is_three_team", False)),
            tms,
            tms[0] if len(tms) > 0 else None,
            tms[1] if len(tms) > 1 else None,
            tms[2] if len(tms) > 2 else None,
            t["summary"],
            Json(t.get("assets_by_team") or {}),
            Json(t.get("players_traded") or []),
            Json(t.get("draft_picks_traded") or []),
            bool(t.get("has_retained_salary", False)),
            Json(t["retained_salary"]) if t.get("retained_salary") else None,
            bool(t.get("has_future_considerations", False)),
            t.get("trade_article_url") or None,
            t.get("tracker_url") or None,
        ))

    execute_values(
        cur,
        """
        INSERT INTO public.trades (
            trade_id, season, season_year, season_id, trade_date,
            num_teams, is_three_team, teams, team_a, team_b, team_c,
            summary, assets_by_team, players_traded, draft_picks_traded,
            has_retained_salary, retained_salary, has_future_considerations,
            trade_article_url, tracker_url
        ) VALUES %s
        ON CONFLICT (trade_id) DO UPDATE SET
            season = EXCLUDED.season,
            season_year = EXCLUDED.season_year,
            season_id = EXCLUDED.season_id,
            trade_date = EXCLUDED.trade_date,
            num_teams = EXCLUDED.num_teams,
            is_three_team = EXCLUDED.is_three_team,
            teams = EXCLUDED.teams,
            team_a = EXCLUDED.team_a,
            team_b = EXCLUDED.team_b,
            team_c = EXCLUDED.team_c,
            summary = EXCLUDED.summary,
            assets_by_team = EXCLUDED.assets_by_team,
            players_traded = EXCLUDED.players_traded,
            draft_picks_traded = EXCLUDED.draft_picks_traded,
            has_retained_salary = EXCLUDED.has_retained_salary,
            retained_salary = EXCLUDED.retained_salary,
            has_future_considerations = EXCLUDED.has_future_considerations,
            trade_article_url = EXCLUDED.trade_article_url,
            tracker_url = EXCLUDED.tracker_url,
            updated_at = NOW()
        """,
        rows,
        page_size=250,
    )
    return len(rows)


def load_injuries(cur) -> int:
    idb = json.loads((DATA_DIR / "current_injuries.json").read_text(encoding="utf-8"))
    injuries = idb["injuries"]
    snap_ts = idb.get("snapshot_at_utc")

    cur.execute("TRUNCATE TABLE public.injuries RESTART IDENTITY;")

    rows = []
    for r in injuries:
        rows.append((
            int(r["player_id"]) if r.get("player_id") else None,
            r["player_name"],
            r["team"],
            r.get("team_name") or None,
            r.get("position") or None,
            int(r["aav_usd"]) if r.get("aav_usd") is not None else None,
            r["injury_status"],
            r.get("injury_type") or None,
            r.get("injury_detail") or None,
            r.get("injury_side") or None,
            parse_date(r.get("estimated_return_date")),
            r.get("updated_at_utc") or None,
            r.get("short_comment") or None,
            r.get("long_comment") or None,
            r.get("source_url") or None,
            r.get("nhl_article_url") or None,
            parse_date(r.get("nhl_article_date")),
            r.get("espn_player_url") or None,
            snap_ts,
        ))

    execute_values(
        cur,
        """
        INSERT INTO public.injuries (
            player_id, player_name, team, team_name, position, aav_usd,
            injury_status, injury_type, injury_detail, injury_side,
            estimated_return_date, updated_at_utc, short_comment, long_comment,
            source_url, nhl_article_url, nhl_article_date, espn_player_url,
            snapshot_at_utc
        ) VALUES %s
        """,
        rows,
        page_size=200,
    )
    return len(rows)


def main() -> None:
    db_url = load_database_url()
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    conn = psycopg2.connect(db_url)
    try:
        with conn:
            with conn.cursor() as cur:
                print("Applying DDL from sql/schema.sql...", flush=True)
                cur.execute(schema_sql)

                print("Loading contracts, contract_years (incl. future 2027-35), and contract_teams_years...", flush=True)
                n_c, n_cy, n_proj, n_cty = load_contracts_years_and_teams(cur)
                print(f"  Upserted {n_c:,} contracts, {n_cy:,} contract_years ({n_proj:,} projected future years), and {n_cty:,} contract_teams_years.", flush=True)

                print("Loading trades...", flush=True)
                n_t = load_trades(cur)
                print(f"  Upserted {n_t:,} trades.", flush=True)

                print("Loading current injuries snapshot...", flush=True)
                n_i = load_injuries(cur)
                print(f"  Inserted {n_i:,} current injuries.", flush=True)

        # Verification queries
        with conn.cursor() as cur:
            print("\n=== SUPABASE 5-TABLE VERIFICATION SUMMARY ===")
            for tbl in ("contracts", "contract_years", "contract_teams_years", "trades", "injuries"):
                cur.execute(f"SELECT COUNT(*) FROM public.{tbl};")
                print(f"  public.{tbl:22}: {cur.fetchone()[0]:>8,} rows")

            cur.execute("""
                SELECT is_projected, MIN(season), MAX(season), COUNT(*)
                FROM public.contract_years
                GROUP BY is_projected ORDER BY is_projected;
            """)
            print("  contract_years observed vs projected:", cur.fetchall())

            cur.execute("""
                SELECT season, COUNT(*) AS teams,
                       SUM(contracts_count) AS signed_players,
                       SUM(total_aav_usd) AS committed_aav,
                       SUM(buyout_penalty_usd) AS buyouts,
                       SUM(retained_salary_penalty_usd) AS retained,
                       SUM(bonus_overage_penalty_usd) AS overages
                FROM public.contract_teams_years
                WHERE season_year >= 2026
                GROUP BY season, season_year
                ORDER BY season_year;
            """)
            print("\n  League-Wide Future Cap Commitments & Penalties (2026-27..2034-35):")
            for s, tms, pl, aav, bo, ret, ov in cur.fetchall():
                print(f"    {s} | {tms:2} teams | {pl:4} contracts | AAV ${aav:>13,} | Buyouts ${bo:>10,} | Retained ${ret:>9,} | Overages ${ov:>9,}")

            # Spot-check EDM & BUF multi-year team cap sheets (2026-27..2030-31)
            for tm in ("EDM", "BUF", "SJS", "COL"):
                cur.execute("""
                    SELECT season, contracts_count, one_way_aav_usd, buyout_penalty_usd,
                           retained_salary_penalty_usd, bonus_overage_penalty_usd,
                           combined_one_way_plus_penalties_usd, cap_ceiling_usd, projected_cap_space_usd
                    FROM public.contract_teams_years
                    WHERE team = %s AND season_year BETWEEN 2026 AND 2030
                    ORDER BY season_year;
                """, (tm,))
                print(f"\n  Spot-Check {tm} Team Cap Sheet (2026-27..2030-31):")
                for r in cur.fetchall():
                    ceil_s = f"${r[7]:>11,}" if r[7] else "        TBD"
                    sp_s = f"${r[8]:>11,}" if r[8] is not None else "        TBD"
                    print(
                        f"    {r[0]} | {r[1]:2} signed | 1W AAV ${r[2]:>11,} | "
                        f"BO ${r[3]:>9,} | Ret ${r[4]:>9,} | Ov ${r[5]:>9,} | "
                        f"Cap {ceil_s} | Space {sp_s}"
                    )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
