-- Open NHL Contracts, Team Cap Sheets, Trades & Current Injuries Schema (Supabase / PostgreSQL)
-- Designed to join cleanly with existing public.players (player_id BIGINT, season INT)
-- and public.teams (team TEXT).
-- License: CC0-1.0 (Public Domain)

CREATE TABLE IF NOT EXISTS public.contracts (
    contract_id             TEXT PRIMARY KEY,
    player_id               BIGINT NOT NULL,
    player_name             TEXT NOT NULL,
    position                TEXT,
    birth_date              DATE,
    draft_year              INTEGER,
    signing_team            TEXT,
    teams                   TEXT[],
    start_year              INTEGER NOT NULL,
    end_year                INTEGER,
    observed_end_year       INTEGER NOT NULL,
    start_season            TEXT NOT NULL,
    end_season              TEXT,
    observed_end_season     TEXT NOT NULL,
    term_years              INTEGER,
    observed_years          INTEGER NOT NULL,
    signed_date             DATE,
    contract_status         TEXT,       -- ELC, RFA, UFA (status at signing)
    contract_status_note    TEXT,
    expiry_status           TEXT,       -- RFA, UFA (CBA Article 10.1 status at expiry)
    expiry_status_note      TEXT,
    aav_usd                 BIGINT NOT NULL,
    calculated_aav_usd      BIGINT,
    total_value_usd         BIGINT,
    two_way                 BOOLEAN NOT NULL DEFAULT FALSE,
    is_active_2026_27       BOOLEAN NOT NULL DEFAULT FALSE,
    is_future_extension     BOOLEAN NOT NULL DEFAULT FALSE,
    is_left_censored_2010   BOOLEAN NOT NULL DEFAULT FALSE,
    is_right_censored_2026  BOOLEAN NOT NULL DEFAULT FALSE,
    nhlpa_url               TEXT,
    source_url              TEXT,
    secondary_source_url    TEXT,
    future_extension        JSONB,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE public.contracts
    ADD COLUMN IF NOT EXISTS is_future_extension BOOLEAN NOT NULL DEFAULT FALSE;

CREATE INDEX IF NOT EXISTS idx_contracts_player_id ON public.contracts (player_id);
CREATE INDEX IF NOT EXISTS idx_contracts_start_year ON public.contracts (start_year);
CREATE INDEX IF NOT EXISTS idx_contracts_end_year ON public.contracts (end_year);
CREATE INDEX IF NOT EXISTS idx_contracts_signing_team ON public.contracts (signing_team);
CREATE INDEX IF NOT EXISTS idx_contracts_active_2026 ON public.contracts (is_active_2026_27) WHERE is_active_2026_27 = TRUE;
CREATE INDEX IF NOT EXISTS idx_contracts_future_ext ON public.contracts (is_future_extension) WHERE is_future_extension = TRUE;
CREATE INDEX IF NOT EXISTS idx_contracts_status ON public.contracts (contract_status, expiry_status);

CREATE TABLE IF NOT EXISTS public.contract_years (
    contract_id             TEXT NOT NULL REFERENCES public.contracts(contract_id) ON DELETE CASCADE,
    player_id               BIGINT NOT NULL,
    player_name             TEXT NOT NULL,
    position                TEXT,
    season_year             INTEGER NOT NULL,
    season_id               INTEGER NOT NULL,  -- e.g. 20262027 (matches public.players.season)
    season                  TEXT NOT NULL,     -- e.g. '2026-27'
    team                    TEXT,
    aav_usd                 BIGINT NOT NULL,
    total_salary_usd        BIGINT,            -- NULL for projected 2027-28+ seasons
    base_salary_usd         BIGINT,
    signing_bonus_usd       BIGINT,
    minor_league_salary_usd BIGINT DEFAULT 0,
    two_way                 BOOLEAN NOT NULL DEFAULT FALSE,
    is_projected            BOOLEAN NOT NULL DEFAULT FALSE,
    nhlpa_url               TEXT,
    PRIMARY KEY (contract_id, season_year)
);

ALTER TABLE public.contract_years
    ADD COLUMN IF NOT EXISTS is_projected BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE public.contract_years ALTER COLUMN total_salary_usd DROP NOT NULL;
ALTER TABLE public.contract_years ALTER COLUMN base_salary_usd DROP NOT NULL;
ALTER TABLE public.contract_years ALTER COLUMN signing_bonus_usd DROP NOT NULL;
ALTER TABLE public.contract_years ALTER COLUMN minor_league_salary_usd DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_contract_years_player_season ON public.contract_years (player_id, season_year);
CREATE INDEX IF NOT EXISTS idx_contract_years_season_id_team ON public.contract_years (season_id, team);
CREATE INDEX IF NOT EXISTS idx_contract_years_team_year ON public.contract_years (team, season_year);
CREATE INDEX IF NOT EXISTS idx_contract_years_projected ON public.contract_years (is_projected, season_year);

CREATE TABLE IF NOT EXISTS public.contract_teams_years (
    team                                TEXT NOT NULL,
    season_year                         INTEGER NOT NULL,
    season_id                           INTEGER NOT NULL,  -- e.g. 20262027, 20272028
    season                              TEXT NOT NULL,     -- e.g. '2026-27', '2027-28'
    is_projected                        BOOLEAN NOT NULL DEFAULT FALSE,
    contracts_count                     INTEGER NOT NULL DEFAULT 0,
    one_way_contracts_count             INTEGER NOT NULL DEFAULT 0,
    two_way_contracts_count             INTEGER NOT NULL DEFAULT 0,
    total_aav_usd                       BIGINT NOT NULL DEFAULT 0,
    one_way_aav_usd                     BIGINT NOT NULL DEFAULT 0,
    total_salary_usd                    BIGINT,
    total_base_salary_usd               BIGINT,
    total_signing_bonus_usd             BIGINT,
    buyout_penalty_usd                  BIGINT NOT NULL DEFAULT 0,
    retained_salary_penalty_usd         BIGINT NOT NULL DEFAULT 0,
    bonus_overage_penalty_usd           BIGINT NOT NULL DEFAULT 0,
    total_cap_penalties_usd             BIGINT NOT NULL DEFAULT 0,
    combined_aav_plus_penalties_usd     BIGINT NOT NULL DEFAULT 0,
    combined_one_way_plus_penalties_usd BIGINT NOT NULL DEFAULT 0,
    cap_ceiling_usd                     BIGINT,
    cap_floor_usd                       BIGINT,
    projected_cap_space_usd             BIGINT,            -- cap_ceiling_usd - combined_one_way_plus_penalties_usd
    penalties_detail                    JSONB,
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (team, season_year)
);

CREATE INDEX IF NOT EXISTS idx_contract_teams_years_season ON public.contract_teams_years (season_year);
CREATE INDEX IF NOT EXISTS idx_contract_teams_years_season_id ON public.contract_teams_years (season_id);

CREATE TABLE IF NOT EXISTS public.trades (
    trade_id                    TEXT PRIMARY KEY,
    season                      TEXT NOT NULL,
    season_year                 INTEGER NOT NULL,
    season_id                   INTEGER NOT NULL,
    trade_date                  DATE NOT NULL,
    num_teams                   INTEGER NOT NULL,
    is_three_team               BOOLEAN NOT NULL DEFAULT FALSE,
    teams                       TEXT[] NOT NULL,
    team_a                      TEXT,
    team_b                      TEXT,
    team_c                      TEXT,
    summary                     TEXT NOT NULL,
    assets_by_team              JSONB NOT NULL,
    players_traded              JSONB NOT NULL,
    draft_picks_traded          JSONB NOT NULL,
    has_retained_salary         BOOLEAN NOT NULL DEFAULT FALSE,
    retained_salary             JSONB,
    has_future_considerations   BOOLEAN NOT NULL DEFAULT FALSE,
    trade_article_url           TEXT,
    tracker_url                 TEXT,
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trades_date ON public.trades (trade_date DESC);
CREATE INDEX IF NOT EXISTS idx_trades_season_year ON public.trades (season_year);
CREATE INDEX IF NOT EXISTS idx_trades_teams_gin ON public.trades USING GIN (teams);

CREATE TABLE IF NOT EXISTS public.injuries (
    id                      BIGSERIAL PRIMARY KEY,
    player_id               BIGINT UNIQUE,
    player_name             TEXT NOT NULL,
    team                    TEXT NOT NULL,
    team_name               TEXT,
    position                TEXT,
    aav_usd                 BIGINT,
    injury_status           TEXT NOT NULL,
    injury_type             TEXT,
    injury_detail           TEXT,
    injury_side             TEXT,
    estimated_return_date   DATE,
    updated_at_utc          TIMESTAMPTZ,
    short_comment           TEXT,
    long_comment            TEXT,
    source_url              TEXT,
    nhl_article_url         TEXT,
    nhl_article_date        DATE,
    espn_player_url         TEXT,
    snapshot_at_utc         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_injuries_team ON public.injuries (team);
CREATE INDEX IF NOT EXISTS idx_injuries_status ON public.injuries (injury_status);
