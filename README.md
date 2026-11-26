# Open NHL Contracts (17-Season Historical & Active 32-Team Database — 2010-11 to 2026-27)

An open, public-domain (`CC0-1.0`), source-linked NHL contract, compensation, trade, and cap-penalty database covering **17 full seasons (`2010-11` through `2026-27`)** and **all 32 active NHL rosters** — built **without scraping PuckPedia, CapWages, Spotrac, CapFriendly, or any third-party contract database**.

Uses **official primary sources**: the **NHL Players' Association (NHLPA)** Compensation by Team registry (`nhlpa.com/compensation/{team}`) for authoritative year-by-year base salary, signing bonus, minor-league salary, and AAV data, combined with the **official NHL Web API** (`api-web.nhle.com`) for deterministic CBA Article 10.1 signing/expiry statuses, and **NHL.com Forge** + editorial hockey news for contract terms, future extensions, trades, and active cap penalties.

---

## Database Snapshot

### 1. 17-Season Historical Contracts Database (`2010-11` through `2026-27`)
- **17 seasons** (`2010-11`..`2026-27`), **4,691 unique NHL-contracted players** (`4,691 / 4,691` matched to official NHL Web API bios)
- **25,712 player-season records** (`nhlId`, team, official `aav_usd`, `base_salary_usd`, `signing_bonus_usd`, `total_salary_usd`, `minor_league_salary_usd`, deterministic `two_way`)
- **12,095 segmented contracts** — each storing its complete season-by-season breakdown in a structured JSON column (`yearly_compensation_json` in CSV / `yearly_compensation` in JSON), alongside both official NHLPA `aav_usd` and arithmetic mean `calculated_aav_usd` (**97.2% exact match** across all 9,225 complete uncensored contracts)
- **12,095 / 12,095 (100.0%)** contracts with deterministic CBA Article 10.1 **`signing_status`**: **4,501 UFA**, **3,826 RFA**, **3,768 ELC**
- **11,492 / 12,095 (95.0%)** contracts with deterministic CBA Article 10.1 **`expiry_status`** (accounting for shortened seasons `2012-13`, `2019-20`, `2020-21` and Group 6 UFA rules): **6,384 UFA**, **5,108 RFA** *(remaining 603 are active `2026-27` non-NHL-roster/AHL deals whose end year beyond `2026-27` is not yet observed, flagged via `is_right_censored_2026`)*
- **0 cap-database URLs** (enforced by assertion in code)

### 2. 12-Season Official NHL Trades Database (`2015-16` through `2026-27`)
- **12 consecutive seasons** (`2015-16`..`2026-27`), **1,319 completed NHL trades** sourced directly from official **NHL.com Trade Trackers** (Forge API + March 2018 Wayback snapshot for `2017-18`)
- **1,319 / 1,319 (100.0%)** trades parsed cleanly with **full both-ways (and 3-team) compensation** (`assets_by_team_json`) and linked to an official NHL.com source URL (**0 cap-site URLs**)
- **3,812 structured trade asset rows** (`trade_assets_2015_2027.csv`): **2,352** player / signing-rights / unsigned-draft-choice assets (**87.5%** matched to canonical `nhlId`), **1,359** draft picks (year, round, conditional flag, original team), and **100** future considerations deals
- **1,135 / 1,205 (94.2%)** completed `2015–2026` traded draft picks resolved via official **NHL Draft API** (`records.nhl.com/site/api/draft` + `api-web.nhle.com/v1/draft/picks`, using official `teamPickHistory` trade chains!) to their exact **`pick_overall`**, **`pick_in_round`**, **`drafted_by_team`**, **`drafted_player_id`**, and **`drafted_player_name`** (with `134` `2027+` picks cleanly tagged `upcoming`)
- **18 three-team trades** parsed with per-team asset distributions, plus **89 trades enriched with retained salary %** extracted from **935** linked official NHL.com trade articles

### 3. Active `2026-27` 32-Team NHL Roster Dataset
- **32 / 32 NHL teams** (**804 active roster players**)
- **804 / 804 (100.0%)** players linked to a verified primary source URL (**1,175 unique URLs**, **0 cap-site URLs**)
- **802 / 804 (99.8%)** players matched directly by `nhlId` to official **NHLPA Compensation** — **30 of 32 teams at 100% AAV coverage** (including **100%** across all 5 non-disclosure clubs: `ANA`, `COL`, `NYR`, `UTA`, `CGY`)
- **30** signed future extensions tracked separately (`2027-28+`) without overwriting active `2026-27` cap hits
- **23** active `2026-27` cap penalties tracked (**$40.58M** total across buyouts, retained salary, and performance bonus overages)

### 4. Live Current NHL Injuries (`2026-27` Snapshot)
- **118 currently injured/unavailable NHL players** across **31 teams** from the live **ESPN Public NHL Injuries API** cross-linked with recent official **NHL.com** injury articles
- **118 / 118 (100.0%)** matched to canonical NHL `player_id` (`nhlId`) and **117 / 118 (99.2%)** joined to exact `2026-27` AAV
- Includes `injury_status`, `injury_type`, `injury_detail`, `estimated_return_date`, `updated_at_utc`, editorial `short_comment`, and direct source URLs (**0 cap-site URLs**)

---

## Pipeline Architecture

1. **Official NHLPA Compensation Registry (`scripts/fetch_nhlpa.py`):** Pulls all 17 seasons (`2010-11`..`2026-27`) from `nhlpa.com/api/compensation/{year}`, joining deterministically on `nhlId` (`cache/nhlpa/compensation_history.json`).
2. **Official NHL Web API Bios (`scripts/fetch_nhl_bios.py`):** Pulls compact career bios (`birthDate`, `position`, `draftDetails`, regular-season NHL & pro GP per season) for all 4,691 players (`cache/nhl/player_bios_2010_2026.json`) to compute exact CBA Article 10.1 `signing_status` and `expiry_status`.
3. **Active 32-Team Roster Builder (`scripts/build_league.py`, `data/overrides/league_audit_fixes.json`):** Combines NHLPA financial data with official NHL.com Forge signing/trade articles (`scripts/phase_a_nhl_all.py`, `scripts/discover_v3.py`, `scripts/phase_b_fast.py`, `scripts/phase_c_dollar_rss.py`) and verified 2026-27 cap penalties.
4. **17-Season Historical Contracts Builder (`scripts/build_historical_contracts.py`):** Segments all 25,712 player-season rows into 12,095 discrete contracts, attaches verified 2026-27 multi-year end seasons/URLs, computes `calculated_aav_usd` + CBA `signing_status`/`expiry_status`, and exports CSV/JSON outputs.
5. **12-Season Official Trades & Draft-Pick Resolver (`scripts/fetch_nhl_drafts.py`, `scripts/build_trades.py`):** Pulls all 2,636 official NHL Draft picks (`2015–2026`) with official `teamPickHistory` trade chains, parses all 12 official NHL.com Trade Trackers (`2015-16`..`2026-27`) into full two-way and three-team compensation tables, resolves traded draft picks to exact overall pick number + drafted player (`player_id` & `player_name`), and extracts retained salary % from linked official NHL.com trade articles.
6. **Live Current NHL Injuries Fetcher (`scripts/fetch_injuries.py`):** Pulls current injuries from the live ESPN NHL Injuries JSON API, matches 100% of injured players to canonical `nhlId` + current AAV, and attaches recent official NHL.com injury/IR articles.
7. **Supabase / PostgreSQL Loader (`sql/schema.sql`, `scripts/load_supabase.py`):** Creates and populates 5 normalized PostgreSQL tables (`public.contracts`, `public.contract_years`, `public.contract_teams_years`, `public.trades`, `public.injuries`) — including committed future seasons (`2027-28`..`2034-35`), signed future extensions, multi-year buyout/retention schedules, bonus overages, and official NHL cap ceilings/floors — with foreign keys, JSONB columns, GIN/B-tree indexes, and idempotent upsert/refresh logic.
8. **Incremental Daily Updater (`scripts/daily_update.py`, `.github/workflows/daily_update.yml`):** Runs daily via GitHub Actions at **11:00 UTC** (plus manual `workflow_dispatch`). Checks live current-season NHLPA compensation (1 request), official NHL.com Trade Tracker (1 request), and live ESPN/NHL.com injuries, diffs each feed against the repo baseline, upserts **only** changed tables to Supabase (`DATABASE_HS_URL` repo secret), and commits updated snapshots back to `main`.

---

## Key Output Files

- `sql/schema.sql` & `scripts/load_supabase.py` — PostgreSQL DDL and idempotent Supabase loader for `contracts`, `contract_years`, `contract_teams_years`, `trades`, and `injuries`
- `data/contract_teams_years_2010_2035.csv` — Local CSV mirror of team-level cap sheets & penalties (`2010-11`..`2034-35`, 751 team-season rows, normalized `UTH` → `UTA`)

- `data/historical_contracts_2010_2027.csv` & `.json` — All **12,095** NHL contracts (`2010-11`..`2026-27`) with `signing_status`, `expiry_status`, `aav_usd`, `calculated_aav_usd`, and `yearly_compensation_json`
- `data/historical_player_seasons_2010_2027.csv` — Tidy **25,712-row** player-season table (`2010-11`..`2026-27`) with base salary, signing bonus, total salary, minor-league salary, and AAV
- `data/historical_summary.json` — Season-by-season payroll, player counts, and CBA status totals (`2010-11`..`2026-27`)
- `data/trades_2015_2027.csv` & `.json` — All **1,319** official NHL trades (`2015-16`..`2026-27`) with both-ways/3-team `assets_by_team_json`, `retained_salary_json`, and official NHL.com URLs
- `data/trade_assets_2015_2027.csv` & `data/trades_summary.json` — Tidy **3,812-row** trade-assets table (including resolved `pick_overall`, `drafted_player_id`, `drafted_player_name`, and `pick_history`) and season-by-season summary
- `data/current_injuries.csv` & `.json` — Live snapshot of all currently injured NHL players (**118 players**, 100% matched to `nhlId` + AAV + status/type/return date + source URLs)
- `data/all_contracts.csv` & `.json` — Active `2026-27` 32-team NHL rosters (**804 players**) with future extensions and acquisition/trade links
- `data/all_cap_penalties.csv` — Verified `2026-27` buyouts, retained salary, and performance bonus overages
- `data/league_summary.json` & `data/teams/{team}_contracts.{csv,json}` — Per-team `2026-27` files for all 32 NHL clubs (`ana`..`wsh`)

---

## License & Source Note

This repository's code and factual dataset are released under [CC0 1.0 Universal](LICENSE) (Public Domain — free to use for any purpose, with no attribution required). Every record links directly to its underlying official source (`nhlpa.com/compensation/{team}`, `nhl.com`, or editorial report). Note that NHLPA.com's website Terms of Use specify personal, non-commercial use of site pages, while underlying player contract facts themselves are factual public figures.
