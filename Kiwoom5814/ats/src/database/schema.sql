CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    event_key TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS strategy_state (
    symbol TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK(mode IN ('DAILY','WEEKLY')),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    order_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
    fill_id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(order_id),
    quantity INTEGER NOT NULL CHECK(quantity > 0),
    price TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS positions (
    symbol TEXT PRIMARY KEY,
    quantity INTEGER NOT NULL CHECK(quantity >= 0),
    cost_basis TEXT NOT NULL,
    state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notification_outbox (
    event_key TEXT PRIMARY KEY,
    message TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','SENT')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL
);
INSERT OR IGNORE INTO system_state VALUES ('control', 'STOP');
CREATE INDEX IF NOT EXISTS events_category_time ON events(category, occurred_at);
