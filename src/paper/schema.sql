CREATE TABLE IF NOT EXISTS paper_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    config_json TEXT NOT NULL,
    cash_cents INTEGER NOT NULL CHECK (cash_cents >= 0),
    halted INTEGER NOT NULL DEFAULT 0,
    last_event_at TEXT
);
CREATE TABLE IF NOT EXISTS paper_events (
    event_id TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    result TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_markets (
    ticker TEXT PRIMARY KEY,
    market_type TEXT NOT NULL,
    event_key TEXT NOT NULL,
    close_at TEXT NOT NULL,
    quote_json TEXT NOT NULL,
    quote_at TEXT NOT NULL,
    result TEXT CHECK (result IN ('yes', 'no'))
);
CREATE TABLE IF NOT EXISTS paper_predictions (
    prediction_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL REFERENCES paper_markets(ticker),
    model_name TEXT NOT NULL,
    model_version TEXT NOT NULL,
    yes_probability REAL NOT NULL,
    market_yes_probability REAL,
    created_at TEXT NOT NULL,
    decision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL REFERENCES paper_markets(ticker),
    side TEXT NOT NULL CHECK (side IN ('yes', 'no')),
    limit_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    remaining INTEGER NOT NULL CHECK (remaining >= 0),
    status TEXT NOT NULL CHECK (status IN ('resting', 'partial', 'filled', 'canceled')),
    created_at TEXT NOT NULL,
    prediction_id TEXT REFERENCES paper_predictions(prediction_id)
);
CREATE TABLE IF NOT EXISTS paper_fills (
    id INTEGER PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES paper_orders(order_id),
    quote_id TEXT NOT NULL,
    price_cents INTEGER NOT NULL,
    quantity INTEGER NOT NULL,
    fee_cents INTEGER NOT NULL,
    filled_at TEXT NOT NULL,
    UNIQUE(order_id, quote_id, price_cents)
);
CREATE TABLE IF NOT EXISTS paper_settlements (
    ticker TEXT PRIMARY KEY REFERENCES paper_markets(ticker),
    payout_cents INTEGER NOT NULL,
    cost_cents INTEGER NOT NULL,
    pnl_cents INTEGER NOT NULL,
    settled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS paper_orders_ticker ON paper_orders(ticker);
