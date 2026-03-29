-- Schema version 1: Full schema for all phases
-- Timestamps: ISO 8601 UTC strings (TEXT)
-- Money: integer cents (INTEGER)
-- Booleans: integer 0/1 (INTEGER)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY,
    order_id        TEXT UNIQUE NOT NULL,
    ticker          TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('yes', 'no')),
    action          TEXT NOT NULL CHECK(action IN ('buy', 'sell')),
    price_cents     INTEGER NOT NULL,
    quantity        REAL NOT NULL,
    status          TEXT NOT NULL CHECK(status IN ('resting', 'executed', 'canceled', 'partial')),
    model_prob      REAL,
    market_price_cents INTEGER,
    edge_cents      INTEGER,
    confidence      REAL,
    created_at      TEXT NOT NULL,
    filled_at       TEXT,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS predictions (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    model_name      TEXT NOT NULL,
    model_version   INTEGER NOT NULL,
    predicted_prob  REAL NOT NULL,
    market_price_cents INTEGER NOT NULL,
    edge_cents      INTEGER NOT NULL,
    features_json   TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    result          TEXT NOT NULL CHECK(result IN ('yes', 'no')),
    settlement_price_cents INTEGER NOT NULL,
    trade_id        INTEGER REFERENCES trades(id),
    prediction_id   INTEGER REFERENCES predictions(id),
    pnl_cents       INTEGER NOT NULL,
    settled_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
    id              INTEGER PRIMARY KEY,
    date            TEXT NOT NULL UNIQUE,
    realized_pnl_cents INTEGER NOT NULL DEFAULT 0,
    unrealized_pnl_cents INTEGER NOT NULL DEFAULT 0,
    total_trades    INTEGER NOT NULL DEFAULT 0,
    winning_trades  INTEGER NOT NULL DEFAULT 0,
    losing_trades   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_versions (
    id              INTEGER PRIMARY KEY,
    model_name      TEXT NOT NULL,
    version         INTEGER NOT NULL,
    parameters_json TEXT NOT NULL,
    metrics_json    TEXT,
    training_data_hash TEXT,
    is_active       INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE(model_name, version)
);

CREATE TABLE IF NOT EXISTS improvements (
    id              INTEGER PRIMARY KEY,
    evaluator_run_id INTEGER REFERENCES evaluator_runs(id),
    type            TEXT NOT NULL,
    target          TEXT NOT NULL,
    description     TEXT NOT NULL,
    current_value   TEXT,
    proposed_value  TEXT,
    rationale       TEXT,
    risk_level      TEXT NOT NULL CHECK(risk_level IN ('low', 'medium', 'high')),
    auto_approvable INTEGER NOT NULL DEFAULT 0,
    validated_on_n_samples INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'applied', 'rejected', 'rolled_back')),
    applied_at      TEXT,
    post_apply_trades INTEGER NOT NULL DEFAULT 0,
    post_apply_pnl_cents INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluator_runs (
    id              INTEGER PRIMARY KEY,
    trigger_type    TEXT NOT NULL,
    snapshot_json   TEXT NOT NULL,
    diagnosis_json  TEXT,
    proposals_count INTEGER NOT NULL DEFAULT 0,
    llm_provider    TEXT NOT NULL,
    llm_model       TEXT NOT NULL,
    tokens_input    INTEGER NOT NULL DEFAULT 0,
    tokens_output   INTEGER NOT NULL DEFAULT 0,
    cost_cents      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed')),
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    completed_at    TEXT
);

CREATE TABLE IF NOT EXISTS holdout_results (
    id              INTEGER PRIMARY KEY,
    model_name      TEXT NOT NULL,
    model_version   INTEGER NOT NULL,
    holdout_size    INTEGER NOT NULL,
    brier_score     REAL NOT NULL,
    accuracy        REAL NOT NULL,
    edge_mean_cents REAL,
    pnl_cents       INTEGER,
    validated_at    TEXT NOT NULL
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_trades_ticker ON trades(ticker);
CREATE INDEX IF NOT EXISTS idx_trades_market_type ON trades(market_type);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_created_at ON trades(created_at);
CREATE INDEX IF NOT EXISTS idx_predictions_ticker ON predictions(ticker);
CREATE INDEX IF NOT EXISTS idx_outcomes_ticker ON outcomes(ticker);
CREATE INDEX IF NOT EXISTS idx_outcomes_settled_at ON outcomes(settled_at);
CREATE INDEX IF NOT EXISTS idx_improvements_status ON improvements(status);

-- Phase 2: NBA historical game results (populated on first run / --refresh-history)
CREATE TABLE IF NOT EXISTS nba_game_results (
    id              INTEGER PRIMARY KEY,
    game_id         TEXT NOT NULL UNIQUE,
    game_date       TEXT NOT NULL,
    home_team_id    INTEGER NOT NULL,
    away_team_id    INTEGER NOT NULL,
    home_pts        INTEGER NOT NULL,
    away_pts        INTEGER NOT NULL,
    home_win        INTEGER NOT NULL,
    season          TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_nba_results_date ON nba_game_results(game_date);
CREATE INDEX IF NOT EXISTS idx_nba_results_season ON nba_game_results(season);

-- Phase 2: NOAA historical daily weather (populated on first run / --refresh-history)
CREATE TABLE IF NOT EXISTS noaa_daily_weather (
    id              INTEGER PRIMARY KEY,
    station_id      TEXT NOT NULL,
    station_code    TEXT NOT NULL,
    date            TEXT NOT NULL,
    tmax_f          REAL,
    tmin_f          REAL,
    prcp_in         REAL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(station_id, date)
);
CREATE INDEX IF NOT EXISTS idx_noaa_station_date ON noaa_daily_weather(station_id, date);

INSERT OR IGNORE INTO schema_version (version) VALUES (1);
INSERT OR IGNORE INTO schema_version (version) VALUES (2);
