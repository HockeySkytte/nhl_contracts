"""Build the full 32-team Open NHL Contracts & Cap Penalties dataset (2026-27).

Combines:
  - 4 curated stress-test teams (EDM, TOR, SJS, NYI)
  - 28 newly backfilled teams (Phases A + B + C: NHL API + NHL.com Forge + editorial news)
  - Official NHL API bios for deterministic CBA Article 10.1 signing/expiry statuses
  - Verified 2026-27 cap penalties (buyouts, retained salary, bonus overages)
  - Strict zero-cap-site guardrail assertion across every URL in the dataset

Outputs (CC0-1.0 Public Domain):
  - data/teams/{team}_contracts.json & .csv (all 32 teams)
  - data/all_contracts.json & .csv (full NHL active roster)
  - data/all_cap_penalties.csv (league-wide verified 2026-27 cap penalties)
  - data/league_summary.json (32-team coverage & cap scoreboard)
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import discover_v3 as dv3

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TEAMS_DIR = DATA_DIR / "teams"
PB_DIR = ROOT / "cache" / "phase_b"
PC_DIR = ROOT / "cache" / "phase_c"
NHLPA_HIST_PATH = ROOT / "cache" / "nhlpa" / "compensation_history.json"

CURRENT_SEASON = "2026-27"
CURRENT_SEASON_INT = 20262027

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

# Verified editorial overrides for a few high-profile FLA stars & minor audit cleanups
EXTRA_OVERRIDES = {
    # Florida Panthers stars verified via Sportsnet / TSN / Daily Faceoff
    "8477933": {  # Sam Reinhart
        "signing_team": "FLA", "signing_status": "UFA", "term_years": 8, "aav_usd": 8_625_000,
        "start_season": "2024-25", "end_season": "2031-32", "signed_date": "2024-07-01",
        "source_url": "https://www.nhl.com/panthers/news/florida-panthers-agree-to-terms-with-forward-sam-reinhart-on-an-eight-year-contract",
    },
    "8477409": {  # Carter Verhaeghe
        "signing_team": "FLA", "signing_status": "UFA", "term_years": 8, "aav_usd": 7_000_000,
        "start_season": "2025-26", "end_season": "2032-33", "signed_date": "2024-10-08",
        "source_url": "https://www.nhl.com/panthers/news/florida-panthers-agree-to-terms-with-forward-carter-verhaeghe-on-an-eight-year-contract-extension",
    },
    "8475462": {  # Radko Gudas
        "signing_team": "FLA", "signing_status": "UFA", "term_years": 6, "aav_usd": 1_500_000,
        "start_season": "2026-27", "end_season": "2031-32", "signed_date": "2026-07-01",
        "source_url": "https://www.nhl.com/news/radko-gudas-signs-6-year-contract-with-florida-panthers",
    },
    "8478859": {  # Niko Mikkola
        "signing_team": "FLA", "signing_status": "UFA", "term_years": 8, "aav_usd": 5_000_000,
        "start_season": "2026-27", "end_season": "2033-34", "signed_date": "2025-10-02",
        "source_url": "https://www.nhl.com/panthers/news/florida-panthers-agree-to-terms-with-defenseman-niko-mikkola-on-an-eight-year-contract-extension",
    },
    # Audit cleanups on previously built teams (TOR/SJS/NYI)
    "8475200": {"two_way": False},  # Dmitry Orlov (2y $6.5M is one-way)
    "8478057": {"signing_team": "VAN"},  # Dakota Joshua signed with VAN
    "8480893": {"signing_team": "CBJ"},  # Kirill Marchenko active 3y deal signed with CBJ
    "8477426": {"signing_team": "TBL"},  # Nick Paul signed with TBL
    "8476624": {"signing_team": "NYR"},  # Barclay Goodrow signed with NYR
    "8477498": {"signing_team": "EDM"},  # Darnell Nurse signed with EDM
    "8476292": {"signing_team": "NJD"},  # Ondrej Palat signed with NJD
    "8484221": {"signing_team": "COL"},  # Calum Ritchie ELC signed with COL
    "8475170": {"signing_team": "STL"},  # Brayden Schenn signed with STL
}

# Load verified league-wide audit overrides (Edge Cases 1, 2, 3)
_AUDIT_FIXES_PATH = DATA_DIR / "overrides" / "league_audit_fixes.json"
if _AUDIT_FIXES_PATH.exists():
    _audit_fixes = json.loads(_AUDIT_FIXES_PATH.read_text(encoding="utf-8"))
    for _pid, _ov in _audit_fixes.items():
        if _pid.startswith("_"):
            continue
        EXTRA_OVERRIDES.setdefault(_pid, {}).update({k: v for k, v in _ov.items() if not k.startswith("_")})

# Verified 2026-27 league-wide cap penalties (buyouts, retained salary, bonus overages)
# All sourced from NHL.com / Sportsnet / ProHockeyRumors / Daily Faceoff — zero cap database sites.
LEAGUE_CAP_PENALTIES = {
    "BOS": [
        {
            "penalty_type": "retained_salary", "player_id": 8478443, "player_name": "Brandon Carlo",
            "event_date": "2025-03-07", "start_season": "2024-25", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 615_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-boston-bruins-10.html",
            "notes": "Retained salary from March 2025 trade; final season in 2026-27.",
        },
    ],
    "BUF": [
        {
            "penalty_type": "buyout", "player_id": 8475784, "player_name": "Jeff Skinner",
            "event_date": "2024-06-30", "start_season": "2024-25", "end_season": "2029-30",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 6_444_444,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-buffalo-sabres-10.html",
            "notes": "Peak year ($6.44M) of 6-year buyout; drops to $2,444,444/yr for 2027-28 through 2029-30.",
        },
    ],
    "DAL": [
        {
            "penalty_type": "bonus_overage", "player_id": None, "player_name": "Team Performance Bonus Overage (Jamie Benn / Justin Hryckowian)",
            "event_date": "2026-04-20", "start_season": "2026-27", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 2_080_000,
            "source_url": "https://www.prohockeyrumors.com/2026/04/at-least-eight-teams-to-carry-overage-penalty-into-2026-27.html",
            "notes": "2025-26 performance bonus carryover ($2M Jamie Benn + $80K Justin Hryckowian).",
        },
    ],
    "FLA": [
        {
            "penalty_type": "bonus_overage", "player_id": 8473507, "player_name": "Team Performance Bonus Overage (Jeff Petry)",
            "event_date": "2026-04-20", "start_season": "2026-27", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 150_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-florida-panthers-10.html",
            "notes": "2025-26 performance bonus carryover from Jeff Petry's 50-GP milestone.",
        },
    ],
    "MIN": [
        {
            "penalty_type": "buyout", "player_id": 8470610, "player_name": "Zach Parise",
            "event_date": "2021-07-13", "start_season": "2021-22", "end_season": "2028-29",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 833_333,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-minnesota-wild-10.html",
            "notes": "Reduced tail of 2021 buyout ($833,333/yr through 2028-29).",
        },
        {
            "penalty_type": "buyout", "player_id": 8470600, "player_name": "Ryan Suter",
            "event_date": "2021-07-13", "start_season": "2021-22", "end_season": "2028-29",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 833_333,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-minnesota-wild-10.html",
            "notes": "Reduced tail of 2021 buyout ($833,333/yr through 2028-29).",
        },
    ],
    "MTL": [
        {
            "penalty_type": "retained_salary", "player_id": 8475848, "player_name": "Brendan Gallagher",
            "event_date": "2026-07-01", "start_season": "2026-27", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 3_250_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-montreal-canadiens-10.html",
            "notes": "50% retained salary on Brendan Gallagher's final contract year ($6.5M AAV).",
        },
        {
            "penalty_type": "bonus_overage", "player_id": None, "player_name": "Team Performance Bonus Overage (2025-26 carryover)",
            "event_date": "2026-08-01", "start_season": "2026-27", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 1_934_410,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-montreal-canadiens-10.html",
            "notes": "2025-26 performance bonus carryover (including Ivan Demidov and Oliver Kapanen ELC bonuses).",
        },
    ],
    "NSH": [
        {
            "penalty_type": "buyout", "player_id": 8474068, "player_name": "Kyle Turris",
            "event_date": "2020-10-07", "start_season": "2020-21", "end_season": "2027-28",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 2_000_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-nashville-predators-10.html",
            "notes": "Ongoing 8-year buyout penalty ($2.0M/yr through 2027-28).",
        },
        {
            "penalty_type": "buyout", "player_id": 8475168, "player_name": "Matt Duchene",
            "event_date": "2023-06-30", "start_season": "2023-24", "end_season": "2028-29",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 1_555_556,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-nashville-predators-10.html",
            "notes": "Reduced tail of 2023 buyout ($1,555,556/yr from 2026-27 through 2028-29).",
        },
    ],
    "OTT": [
        {
            "penalty_type": "buyout", "player_id": 8478400, "player_name": "Colin White",
            "event_date": "2022-07-06", "start_season": "2022-23", "end_season": "2027-28",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 875_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-ottawa-senators-10.html",
            "notes": "6-year buyout penalty ($875K/yr through 2027-28).",
        },
    ],
    "SEA": [
        {
            "penalty_type": "buyout", "player_id": 8480813, "player_name": "Joe Veleno",
            "event_date": "2025-06-30", "start_season": "2025-26", "end_season": "2026-27",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 379_147,
            "source_url": "https://www.prohockeyrumors.com/2026/10/salary-cap-deep-dive-seattle-kraken-6.html",
            "notes": "Final year of buyout penalty.",
        },
    ],
    "STL": [
        {
            "penalty_type": "buyout", "player_id": 8477494, "player_name": "Jonathan Drouin",
            "event_date": "2026-06-30", "start_season": "2026-27", "end_season": "2027-28",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 1_333_333,
            "source_url": "https://www.prohockeyrumors.com/2026/09/salary-cap-deep-dive-st-louis-blues-10.html",
            "notes": "2026 buyout cap penalty ($1,333,333 in 2026-27).",
        },
    ],
    "UTA": [
        {
            "penalty_type": "retained_buyout", "player_id": 8475171, "player_name": "Oliver Ekman-Larsson",
            "event_date": "2023-06-16", "start_season": "2023-24", "end_season": "2030-31",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 650_000,
            "source_url": "https://www.prohockeyrumors.com/2026/08/salary-cap-deep-dive-utah-mammoth-2.html",
            "notes": "Inherited retention/buyout share from ARI trade to VAN ($650K in 2026-27; drops to $290K/yr for 2027-31).",
        },
    ],
    "VAN": [
        {
            "penalty_type": "buyout", "player_id": 8475171, "player_name": "Oliver Ekman-Larsson",
            "event_date": "2023-06-16", "start_season": "2023-24", "end_season": "2030-31",
            "current_season": CURRENT_SEASON, "current_season_cap_hit_usd": 4_766_667,
            "source_url": "https://www.nhl.com/news/oliver-ekman-larsson-bought-out-by-canucks-344911236",
            "notes": "Peak buyout year ($4,766,667 in 2025-26 and 2026-27); drops to $2,126,667/yr for 2027-28 through 2030-31.",
        },
    ],
}


def compute_expiry_status(bio: dict, position: str, end_season: str | None) -> tuple[str | None, str | None]:
    if not end_season or not bio.get("birthDate"):
        return None, None
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


def infer_signing_status(bio: dict, start_season: str | None, position: str, terms_hint: str | None, is_elc: bool, aav_usd: int | None = None) -> str | None:
    # CBA guard: an ELC can never exceed ~$1.15M AAV or be signed by a player older than 25
    age_june_30 = None
    sy = None
    if start_season and bio.get("birthDate"):
        sy = int(start_season.split("-")[0])
        by, bm, bd = map(int, bio["birthDate"].split("-"))
        age_june_30 = sy - by - ((6, 30) < (bm, bd))
    elc_plausible = (aav_usd is None or aav_usd <= 1_150_000) and (age_june_30 is None or age_june_30 <= 25)

    if (is_elc or terms_hint == "ELC") and elc_plausible:
        return "ELC"
    if terms_hint in ("RFA", "UFA"):
        return terms_hint
    if sy is None or age_june_30 is None:
        return None
    if age_june_30 >= 27:
        return "UFA"
    gp_thresh = 30 if position == "G" else 40
    nhl_by_season: dict[int, int] = {}
    for st in bio.get("seasonTotals", []):
        if st.get("gameTypeId") == 2 and st.get("leagueAbbrev") == "NHL":
            nhl_by_season[st["season"]] = nhl_by_season.get(st["season"], 0) + (st.get("gamesPlayed", 0) or 0)
    cutoff = sy * 10000 + (sy + 1)
    accrued = sum(1 for s, gp in nhl_by_season.items() if s < cutoff and gp >= gp_thresh)
    if accrued >= 7:
        return "UFA"
    if age_june_30 <= 21 and accrued == 0 and elc_plausible:
        return "ELC"
    return "RFA"


def assert_no_cap_site(url: str | None) -> None:
    if not url:
        return
    low = url.lower()
    for bad in FORBIDDEN_CAP_DOMAINS:
        if bad in low:
            raise RuntimeError(f"Forbidden cap-site URL detected: {url}")


def build_new_team(team_code: str, pc_hits: dict) -> dict:
    fast = json.loads((PB_DIR / f"{team_code.lower()}_fast.json").read_text(encoding="utf-8"))
    enr_p = PB_DIR / f"{team_code.lower()}_enriched.json"
    enr_map = {}
    if enr_p.exists():
        enr = json.loads(enr_p.read_text(encoding="utf-8"))
        enr_map = {r["player"]["id"]: r for r in enr["results"]}

    records = []
    for r in fast["results"]:
        p = r["player"]
        pid = p["id"]
        bio = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/player/{pid}/landing") or {}

        a_f = r.get("active")
        e_r = enr_map.get(pid, {})
        a_e = e_r.get("active")
        ddh = e_r.get("deep_dive_hit")
        c_hit = (pc_hits.get(str(pid)) or {}).get("contract")
        ov = EXTRA_OVERRIDES.get(str(pid), {})

        # Pick best candidate: prefer direct URL w/ AAV > any URL w/ AAV > direct URL w/o AAV
        best = None
        for cand in (a_e, a_f, c_hit):
            if not cand:
                continue
            if best is None:
                best = cand
            else:
                def score(c):
                    u = c.get("url") or ""
                    is_direct = "news.google.com" not in u
                    return (c["terms"]["aav_usd"] is not None, is_direct, c.get("source") == "nhl.com")
                if score(cand) > score(best):
                    best = cand

        # Assemble fields (explicit null in override intentionally clears bad inherited values)
        term_years = ov["term_years"] if "term_years" in ov else (best["terms"]["term_years"] if best else None)
        aav_usd = ov["aav_usd"] if "aav_usd" in ov else (best["terms"]["aav_usd"] if best else (ddh["aav_usd"] if ddh else None))
        start_season = ov["start_season"] if "start_season" in ov else (best["start_season"] if best else None)
        end_season = ov["end_season"] if "end_season" in ov else (best["end_season"] if best else (ddh["end_season"] if ddh else None))
        signed_date = ov["signed_date"] if "signed_date" in ov else (best["date"] if best else "")
        signing_team = ov["signing_team"] if "signing_team" in ov else (best.get("signing_team", team_code) if best else team_code)
        two_way = ov["two_way"] if "two_way" in ov else (best["terms"]["two_way"] if best else False)
        is_elc = (ov.get("signing_status") == "ELC") or (best["terms"]["is_elc"] if best else False)
        status_hint = best["terms"].get("status_hint") if best else None

        # Prefer direct official NHL.com URL as source_url or secondary_source_url
        source_url = ov["source_url"] if "source_url" in ov else (best["url"] if best else (ddh["url"] if ddh else None))
        secondary_url = ov["secondary_source_url"] if "secondary_source_url" in ov else (
            r.get("official_nhl_url") or e_r.get("official_nhl_url") or (best.get("deep_dive_url") if best else None)
        )
        if secondary_url == source_url:
            secondary_url = None
        # If source_url is a Google News RSS link and we have a direct official NHL.com URL, make NHL.com primary!
        if source_url and "news.google.com" in source_url and secondary_url and "nhl.com" in secondary_url:
            source_url, secondary_url = secondary_url, source_url

        total_value_usd = (aav_usd * term_years) if (aav_usd and term_years) else None
        signing_status = ov.get("signing_status") or infer_signing_status(bio, start_season, p["pos"], status_hint, is_elc, aav_usd)
        expiry_status, expiry_note = compute_expiry_status(bio, p["pos"], end_season)

        if "future_extension" in ov:
            future_ext = ov["future_extension"]
        else:
            f_ext = r.get("future_extension") or e_r.get("future_extension")
            future_ext = None
            if f_ext and f_ext["terms"].get("aav_usd"):
                cand_fe = {
                    "term_years": f_ext["terms"]["term_years"],
                    "aav_usd": f_ext["terms"]["aav_usd"],
                    "total_value_usd": f_ext["terms"]["total_value_usd"],
                    "start_season": f_ext["start_season"],
                    "end_season": f_ext["end_season"],
                    "signed_date": f_ext["date"],
                    "source_url": f_ext["url"],
                }
                # Never duplicate the active contract inside future_extension
                if not (cand_fe["aav_usd"] == aav_usd and cand_fe["term_years"] == term_years):
                    future_ext = cand_fe

        tr = r.get("trade") or e_r.get("trade")
        acq_url = tr["url"] if tr else None
        acq_method = "trade" if acq_url else (
            "draft" if r.get("draft_team") == team_code else "free_agency"
        )

        for u in (source_url, secondary_url, acq_url, future_ext["source_url"] if future_ext else None):
            assert_no_cap_site(u)

        records.append({
            "player_id": pid,
            "player_name": p["name"],
            "position": p["pos"],
            "sweater_number": p.get("sweater"),
            "birth_date": bio.get("birthDate"),
            "current_team": team_code,
            "signing_team": signing_team,
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
            "acquisition": {"method": acq_method, "url": acq_url},
        })

    penalties = LEAGUE_CAP_PENALTIES.get(team_code, [])
    for pen in penalties:
        assert_no_cap_site(pen.get("source_url"))
        assert_no_cap_site(pen.get("secondary_source_url"))

    known_aav = [x for x in records if x["aav_usd"] is not None]
    total_aav = sum(x["aav_usd"] for x in known_aav)
    total_pen = sum(p["current_season_cap_hit_usd"] for p in penalties)

    return {
        "license": "CC0-1.0",
        "season": CURRENT_SEASON,
        "team": team_code,
        "team_name": dv3.TEAM_INFO[team_code][3],
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


def apply_nhlpa_primary(record: dict, nhlpa_players: dict) -> None:
    """Attach official NHLPA Compensation data (https://www.nhlpa.com/compensation/{team})
    as the primary financial source: official AAV, 2026-27 base salary + signing bonus +
    minor-league salary, deterministic two-way flag, and year-by-year JSON compensation history."""
    pid_str = str(record["player_id"])
    nh = nhlpa_players.get(pid_str)
    if not nh or not nh.get("current_2026_27"):
        record.setdefault("aav_source", "editorial" if record.get("aav_usd") is not None else None)
        record.setdefault("nhlpa_url", None)
        record.setdefault("current_season_salary_usd", None)
        record.setdefault("current_season_base_salary_usd", None)
        record.setdefault("current_season_signing_bonus_usd", None)
        record.setdefault("minor_league_salary_usd", None)
        record.setdefault("yearly_compensation", nh.get("yearly_compensation", {}) if nh else {})
        return

    cur = nh["current_2026_27"]
    nhlpa_url = nh.get("nhlpa_compensation_url")
    assert_no_cap_site(nhlpa_url)

    # NHLPA official AAV & current-season cash breakdown are authoritative
    record["aav_usd"] = cur["aav_usd"]
    record["aav_source"] = "nhlpa"
    record["current_season_salary_usd"] = cur["total_salary_usd"]
    record["current_season_base_salary_usd"] = cur["base_salary_usd"]
    record["current_season_signing_bonus_usd"] = cur["signing_bonus_usd"]
    record["minor_league_salary_usd"] = cur["minor_league_salary_usd"]
    record["two_way"] = bool(cur["two_way"])
    record["nhlpa_url"] = nhlpa_url
    record["yearly_compensation"] = nh.get("yearly_compensation") or {}

    # Fill missing start_season from NHLPA contiguous AAV history
    if not record.get("start_season") and nh.get("inferred_contract_start_season"):
        record["start_season"] = nh["inferred_contract_start_season"]

    # Recompute total_value_usd cleanly from authoritative AAV * term_years
    if record.get("term_years") and record.get("aav_usd") is not None:
        record["total_value_usd"] = record["aav_usd"] * record["term_years"]

    # If no signing article URL was found, use official NHLPA compensation page as source_url
    if not record.get("source_url") and nhlpa_url:
        record["source_url"] = nhlpa_url

    # CBA sanity guard: never allow ELC status on deals > $1.15M AAV or players older than 25 at start
    if record.get("signing_status") == "ELC":
        bad_aav = (record.get("aav_usd") or 0) > 1_150_000
        bad_age = False
        if record.get("birth_date") and record.get("start_season"):
            sy = int(record["start_season"].split("-")[0])
            by, bm, bd = map(int, record["birth_date"].split("-"))
            bad_age = (sy - by - ((6, 30) < (bm, bd))) > 25
        if bad_aav or bad_age:
            bio = dv3.fetch_json_cached(f"https://api-web.nhle.com/v1/player/{record['player_id']}/landing") or {}
            record["signing_status"] = infer_signing_status(
                bio, record.get("start_season"), record.get("position", ""), None, False, record.get("aav_usd")
            )


def recompute_team_summary(td: dict) -> None:
    records = td["contracts"]
    penalties = td["cap_penalties"]
    known_aav = [x for x in records if x.get("aav_usd") is not None]
    nhlpa_matched = sum(1 for x in records if x.get("aav_source") == "nhlpa")
    total_aav = sum(x["aav_usd"] for x in known_aav)
    total_pen = sum(p["current_season_cap_hit_usd"] for p in penalties)
    td["summary"] = {
        "roster_players": len(records),
        "contracts_with_verified_source_url": sum(1 for x in records if x.get("source_url")),
        "contracts_matched_nhlpa": nhlpa_matched,
        "contracts_with_public_aav": len(known_aav),
        "contracts_missing_public_aav": len(records) - len(known_aav),
        "total_active_roster_aav_usd": total_aav,
        "cap_penalties_count": len(penalties),
        "total_cap_penalties_usd": total_pen,
        "combined_roster_plus_penalties_usd": total_aav + total_pen,
    }


def write_contracts_csv(records: list[dict], path: Path) -> None:
    fields = [
        "current_team", "player_id", "player_name", "position", "signing_team",
        "signed_date", "signing_status", "expiry_status", "term_years",
        "aav_usd", "aav_source", "total_value_usd",
        "current_season_salary_usd", "current_season_base_salary_usd",
        "current_season_signing_bonus_usd", "minor_league_salary_usd",
        "start_season", "end_season", "two_way",
        "nhlpa_url", "source_url", "secondary_source_url",
        "future_extension_summary", "acquisition_method", "acquisition_url",
        "yearly_compensation_json",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for r in records:
            fe = r.get("future_extension")
            fe_str = f"{fe['term_years']}y ${fe['aav_usd']:,} ({fe['start_season']}..{fe['end_season']})" if fe else ""
            yc = r.get("yearly_compensation") or {}
            w.writerow({
                "current_team": r["current_team"],
                "player_id": r["player_id"],
                "player_name": r["player_name"],
                "position": r["position"],
                "signing_team": r["signing_team"],
                "signed_date": r["signed_date"],
                "signing_status": r.get("signing_status") or "",
                "expiry_status": r.get("expiry_status") or "",
                "term_years": r["term_years"] if r["term_years"] is not None else "",
                "aav_usd": r["aav_usd"] if r["aav_usd"] is not None else "",
                "aav_source": r.get("aav_source") or "",
                "total_value_usd": r["total_value_usd"] if r["total_value_usd"] is not None else "",
                "current_season_salary_usd": r["current_season_salary_usd"] if r.get("current_season_salary_usd") is not None else "",
                "current_season_base_salary_usd": r["current_season_base_salary_usd"] if r.get("current_season_base_salary_usd") is not None else "",
                "current_season_signing_bonus_usd": r["current_season_signing_bonus_usd"] if r.get("current_season_signing_bonus_usd") is not None else "",
                "minor_league_salary_usd": r["minor_league_salary_usd"] if r.get("minor_league_salary_usd") is not None else "",
                "start_season": r.get("start_season") or "",
                "end_season": r.get("end_season") or "",
                "two_way": r.get("two_way", False),
                "nhlpa_url": r.get("nhlpa_url") or "",
                "source_url": r.get("source_url") or "",
                "secondary_source_url": r.get("secondary_source_url") or "",
                "future_extension_summary": fe_str,
                "acquisition_method": (r.get("acquisition") or {}).get("method", ""),
                "acquisition_url": (r.get("acquisition") or {}).get("url") or "",
                "yearly_compensation_json": json.dumps(yc, separators=(",", ":")) if yc else "",
            })


def main() -> None:
    TEAMS_DIR.mkdir(parents=True, exist_ok=True)
    pc_hits = json.loads((PC_DIR / "dollar_rss_hits.json").read_text(encoding="utf-8")) if (PC_DIR / "dollar_rss_hits.json").exists() else {}
    nhlpa_players = (
        json.loads(NHLPA_HIST_PATH.read_text(encoding="utf-8")).get("players", {})
        if NHLPA_HIST_PATH.exists() else {}
    )

    all_teams_data = []
    for tm in sorted(dv3.TEAM_INFO.keys()):
        if tm in ("EDM", "TOR", "SJS", "NYI"):
            td = json.loads((TEAMS_DIR / f"{tm.lower()}_contracts.json").read_text(encoding="utf-8"))
            # Apply audit cleanups to previously built teams
            for r in td["contracts"]:
                ov = EXTRA_OVERRIDES.get(str(r["player_id"]), {})
                for k, v in ov.items():
                    r[k] = v
                apply_nhlpa_primary(r, nhlpa_players)
                for u in (r.get("nhlpa_url"), r.get("source_url"), r.get("secondary_source_url"), (r.get("acquisition") or {}).get("url")):
                    assert_no_cap_site(u)
            for p in td["cap_penalties"]:
                assert_no_cap_site(p.get("source_url"))
                assert_no_cap_site(p.get("secondary_source_url"))
            recompute_team_summary(td)
        else:
            td = build_new_team(tm, pc_hits)
            for r in td["contracts"]:
                apply_nhlpa_primary(r, nhlpa_players)
                for u in (r.get("nhlpa_url"), r.get("source_url"), r.get("secondary_source_url"), (r.get("acquisition") or {}).get("url")):
                    assert_no_cap_site(u)
            recompute_team_summary(td)
        all_teams_data.append(td)

    all_contracts = []
    all_penalties = []
    summaries = []
    unique_urls = set()

    for td in all_teams_data:
        tm_low = td["team"].lower()
        (TEAMS_DIR / f"{tm_low}_contracts.json").write_text(json.dumps(td, indent=2), encoding="utf-8")
        write_contracts_csv(td["contracts"], TEAMS_DIR / f"{tm_low}_contracts.csv")
        all_contracts.extend(td["contracts"])
        for c in td["contracts"]:
            for u in (c.get("nhlpa_url"), c.get("source_url"), c.get("secondary_source_url"), (c.get("acquisition") or {}).get("url"), (c.get("future_extension") or {}).get("source_url")):
                if u:
                    unique_urls.add(u)
        for p in td["cap_penalties"]:
            all_penalties.append({"team": td["team"], **p})
            for u in (p.get("source_url"), p.get("secondary_source_url")):
                if u:
                    unique_urls.add(u)
        summaries.append({"team": td["team"], "team_name": td["team_name"], **td["summary"]})

    tot_p = len(all_contracts)
    tot_u = sum(1 for c in all_contracts if c.get("source_url"))
    tot_nhlpa = sum(1 for c in all_contracts if c.get("aav_source") == "nhlpa")
    tot_aav = sum(1 for c in all_contracts if c.get("aav_usd") is not None)
    tot_yc = sum(1 for c in all_contracts if c.get("yearly_compensation"))
    tot_ext = sum(1 for c in all_contracts if c.get("future_extension"))
    tot_pen_usd = sum(p["current_season_cap_hit_usd"] for p in all_penalties)

    league = {
        "license": "CC0-1.0",
        "season": CURRENT_SEASON,
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "totals": {
            "teams": len(all_teams_data),
            "players": tot_p,
            "players_with_source_url": tot_u,
            "url_coverage_pct": round(100.0 * tot_u / tot_p, 1),
            "players_matched_nhlpa": tot_nhlpa,
            "nhlpa_coverage_pct": round(100.0 * tot_nhlpa / tot_p, 1),
            "players_with_public_aav": tot_aav,
            "aav_coverage_pct": round(100.0 * tot_aav / tot_p, 1),
            "players_missing_public_aav": tot_p - tot_aav,
            "players_with_yearly_compensation_history": tot_yc,
            "signed_future_extensions": tot_ext,
            "cap_penalties_count": len(all_penalties),
            "total_cap_penalties_usd": tot_pen_usd,
            "unique_source_urls": len(unique_urls),
            "cap_site_urls": 0,
        },
        "team_summaries": summaries,
        "contracts": all_contracts,
        "cap_penalties": all_penalties,
    }

    (DATA_DIR / "all_contracts.json").write_text(json.dumps(league, indent=2), encoding="utf-8")
    (DATA_DIR / "league_summary.json").write_text(json.dumps({"totals": league["totals"], "team_summaries": summaries}, indent=2), encoding="utf-8")
    write_contracts_csv(all_contracts, DATA_DIR / "all_contracts.csv")

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

    print("=== FULL 32-TEAM NHL LEAGUE BUILD COMPLETE ===")
    print(json.dumps(league["totals"], indent=2))
    print("\n32-Team Scoreboard:")
    for s in summaries:
        print(
            f"  {s['team']} | {s['team_name']:22} | "
            f"URL {s['contracts_with_verified_source_url']:2}/{s['roster_players']:2} | "
            f"AAV {s['contracts_with_public_aav']:2}/{s['roster_players']:2} | "
            f"Roster ${s['total_active_roster_aav_usd']:>11,} | "
            f"Dead Cap ${s['total_cap_penalties_usd']:>9,}"
        )


if __name__ == "__main__":
    main()
