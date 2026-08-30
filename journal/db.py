from __future__ import annotations

from pathlib import Path

import aiosqlite


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL UNIQUE,
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    username TEXT NOT NULL DEFAULT '',
    photo_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS account_settings (
    user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    account_name TEXT NOT NULL DEFAULT 'Funded account',
    balance REAL NOT NULL DEFAULT 50000,
    currency TEXT NOT NULL DEFAULT 'USD',
    profit_target_pct REAL NOT NULL DEFAULT 10,
    daily_loss_limit_pct REAL NOT NULL DEFAULT 3,
    max_loss_limit_pct REAL NOT NULL DEFAULT 6,
    risk_per_trade_pct REAL NOT NULL DEFAULT 0.5,
    max_trades_day INTEGER NOT NULL DEFAULT 3,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS circles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    invite_code TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS circle_members (
    circle_id INTEGER NOT NULL REFERENCES circles(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (circle_id, user_id),
    UNIQUE (user_id)
);

CREATE TABLE IF NOT EXISTS moods (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    entry_date TEXT NOT NULL,
    mood INTEGER NOT NULL CHECK (mood BETWEEN 1 AND 5),
    energy INTEGER NOT NULL CHECK (energy BETWEEN 1 AND 5),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 1 AND 5),
    discipline INTEGER NOT NULL CHECK (discipline BETWEEN 1 AND 5),
    emotion TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    focus TEXT NOT NULL DEFAULT '',
    lesson TEXT NOT NULL DEFAULT '',
    journal_mode TEXT NOT NULL DEFAULT 'backtest' CHECK (journal_mode IN ('backtest', 'demo', 'live')),
    market_bias TEXT NOT NULL DEFAULT 'NEUTRAL' CHECK (market_bias IN ('LONG', 'SHORT', 'NEUTRAL')),
    day_idea TEXT NOT NULL DEFAULT '',
    key_levels TEXT NOT NULL DEFAULT '',
    day_invalidation TEXT NOT NULL DEFAULT '',
    news_context TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('private', 'team')),
    circle_id INTEGER REFERENCES circles(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, entry_date)
);

CREATE TABLE IF NOT EXISTS weekly_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    week_start TEXT NOT NULL,
    symbol TEXT NOT NULL,
    bias TEXT NOT NULL DEFAULT 'NEUTRAL' CHECK (bias IN ('LONG', 'SHORT', 'NEUTRAL')),
    title TEXT NOT NULL DEFAULT '',
    idea TEXT NOT NULL DEFAULT '',
    trade_plan TEXT NOT NULL DEFAULT '',
    invalidation TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'reviewed')),
    week_summary TEXT NOT NULL DEFAULT '',
    week_lesson TEXT NOT NULL DEFAULT '',
    rating INTEGER CHECK (rating IS NULL OR rating BETWEEN 1 AND 5),
    visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('private', 'team')),
    circle_id INTEGER REFERENCES circles(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, week_start, symbol)
);

CREATE TABLE IF NOT EXISTS weekly_plan_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES weekly_plans(id) ON DELETE CASCADE,
    storage_name TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    traded_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    status TEXT NOT NULL DEFAULT 'closed',
    client_entry_id TEXT NOT NULL DEFAULT '',
    timeframe TEXT NOT NULL DEFAULT '',
    session TEXT NOT NULL DEFAULT '',
    setup TEXT NOT NULL DEFAULT '',
    grade TEXT NOT NULL DEFAULT '',
    market_context TEXT NOT NULL DEFAULT '',
    journal_mode TEXT NOT NULL DEFAULT 'backtest' CHECK (journal_mode IN ('backtest', 'demo', 'live')),
    confidence_before INTEGER CHECK (confidence_before IS NULL OR confidence_before BETWEEN 1 AND 5),
    trade_plan TEXT NOT NULL DEFAULT '',
    entry_trigger TEXT NOT NULL DEFAULT '',
    trade_invalidation TEXT NOT NULL DEFAULT '',
    outcome_type TEXT NOT NULL DEFAULT '' CHECK (outcome_type IN ('', 'take', 'stop', 'breakeven', 'manual', 'cancelled')),
    weekly_plan_id INTEGER REFERENCES weekly_plans(id) ON DELETE SET NULL,
    idea_followed INTEGER CHECK (idea_followed IS NULL OR idea_followed IN (0, 1)),
    countertrend_confirmed INTEGER CHECK (countertrend_confirmed IS NULL OR countertrend_confirmed IN (0, 1)),
    entry_price REAL,
    stop_loss REAL,
    take_profit REAL,
    volume REAL,
    risk_amount REAL,
    pnl REAL NOT NULL DEFAULT 0,
    r_multiple REAL,
    emotion_before TEXT NOT NULL DEFAULT '',
    emotion_after TEXT NOT NULL DEFAULT '',
    plan_followed INTEGER NOT NULL DEFAULT 1 CHECK (plan_followed IN (0, 1)),
    mistake TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    screenshot_url TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('private', 'team')),
    circle_id INTEGER REFERENCES circles(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_moods_user_date ON moods(user_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_trades_user_date ON trades(user_id, traded_at);
CREATE INDEX IF NOT EXISTS idx_circle_members_circle ON circle_members(circle_id);
CREATE INDEX IF NOT EXISTS idx_weekly_plans_user_week ON weekly_plans(user_id, week_start);
CREATE INDEX IF NOT EXISTS idx_weekly_plan_images_plan ON weekly_plan_images(plan_id);
"""


MIGRATION_COLUMNS = {
    "moods": {
        "focus": "TEXT NOT NULL DEFAULT ''",
        "lesson": "TEXT NOT NULL DEFAULT ''",
        "journal_mode": "TEXT NOT NULL DEFAULT 'backtest' CHECK (journal_mode IN ('backtest', 'demo', 'live'))",
        "market_bias": "TEXT NOT NULL DEFAULT 'NEUTRAL' CHECK (market_bias IN ('LONG', 'SHORT', 'NEUTRAL'))",
        "day_idea": "TEXT NOT NULL DEFAULT ''",
        "key_levels": "TEXT NOT NULL DEFAULT ''",
        "day_invalidation": "TEXT NOT NULL DEFAULT ''",
        "news_context": "TEXT NOT NULL DEFAULT ''",
        "circle_id": "INTEGER",
    },
    "trades": {
        "status": "TEXT NOT NULL DEFAULT 'closed'",
        "client_entry_id": "TEXT NOT NULL DEFAULT ''",
        "session": "TEXT NOT NULL DEFAULT ''",
        "grade": "TEXT NOT NULL DEFAULT ''",
        "market_context": "TEXT NOT NULL DEFAULT ''",
        "journal_mode": "TEXT NOT NULL DEFAULT 'backtest' CHECK (journal_mode IN ('backtest', 'demo', 'live'))",
        "confidence_before": "INTEGER CHECK (confidence_before IS NULL OR confidence_before BETWEEN 1 AND 5)",
        "trade_plan": "TEXT NOT NULL DEFAULT ''",
        "entry_trigger": "TEXT NOT NULL DEFAULT ''",
        "trade_invalidation": "TEXT NOT NULL DEFAULT ''",
        "outcome_type": "TEXT NOT NULL DEFAULT '' CHECK (outcome_type IN ('', 'take', 'stop', 'breakeven', 'manual', 'cancelled'))",
        "weekly_plan_id": "INTEGER",
        "idea_followed": "INTEGER CHECK (idea_followed IS NULL OR idea_followed IN (0, 1))",
        "countertrend_confirmed": "INTEGER CHECK (countertrend_confirmed IS NULL OR countertrend_confirmed IN (0, 1))",
        "circle_id": "INTEGER",
    },
}


async def _migrate_columns(connection: aiosqlite.Connection) -> None:
    for table, columns in MIGRATION_COLUMNS.items():
        cursor = await connection.execute(f"PRAGMA table_info({table})")
        existing = {str(row[1]) for row in await cursor.fetchall()}
        for name, definition in columns.items():
            if name not in existing:
                await connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )


async def connect(path: Path) -> aiosqlite.Connection:
    connection = await aiosqlite.connect(path)
    connection.row_factory = aiosqlite.Row
    await connection.execute("PRAGMA foreign_keys = ON")
    await connection.execute("PRAGMA busy_timeout = 5000")
    return connection


async def initialize_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = await connect(path)
    try:
        await connection.execute("PRAGMA journal_mode = WAL")
        await connection.executescript(SCHEMA)
        await _migrate_columns(connection)
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_moods_circle_date ON moods(circle_id, entry_date)"
        )
        await connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_trades_circle_date ON trades(circle_id, traded_at)"
        )
        await connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_trades_client_entry "
            "ON trades(user_id, client_entry_id) WHERE client_entry_id != ''"
        )
        await connection.commit()
    finally:
        await connection.close()
