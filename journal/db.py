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
    visibility TEXT NOT NULL DEFAULT 'team' CHECK (visibility IN ('private', 'team')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, entry_date)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    traded_at TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('BUY', 'SELL')),
    timeframe TEXT NOT NULL DEFAULT '',
    setup TEXT NOT NULL DEFAULT '',
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
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_moods_user_date ON moods(user_id, entry_date);
CREATE INDEX IF NOT EXISTS idx_trades_user_date ON trades(user_id, traded_at);
CREATE INDEX IF NOT EXISTS idx_circle_members_circle ON circle_members(circle_id);
"""


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
        await connection.commit()
    finally:
        await connection.close()

