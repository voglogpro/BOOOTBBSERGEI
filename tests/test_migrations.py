from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from journal.db import initialize_database


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_trade_and_mood_tables_are_upgraded_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript(
                """
                CREATE TABLE users (id INTEGER PRIMARY KEY);
                CREATE TABLE moods (
                    id INTEGER PRIMARY KEY, user_id INTEGER, entry_date TEXT,
                    mood INTEGER, energy INTEGER, confidence INTEGER, discipline INTEGER,
                    emotion TEXT, note TEXT, visibility TEXT
                );
                CREATE TABLE trades (
                    id INTEGER PRIMARY KEY, user_id INTEGER, traded_at TEXT,
                    symbol TEXT, direction TEXT, timeframe TEXT, setup TEXT,
                    entry_price REAL, stop_loss REAL, take_profit REAL, volume REAL,
                    risk_amount REAL, pnl REAL, r_multiple REAL, emotion_before TEXT,
                    emotion_after TEXT, plan_followed INTEGER, mistake TEXT, note TEXT,
                    screenshot_url TEXT, visibility TEXT
                );
                """
            )
            db.close()

            await initialize_database(path)
            await initialize_database(path)

            db = sqlite3.connect(path)
            mood_columns = {row[1] for row in db.execute("PRAGMA table_info(moods)")}
            trade_columns = {row[1] for row in db.execute("PRAGMA table_info(trades)")}
            db.close()
            self.assertTrue({"focus", "lesson", "circle_id"} <= mood_columns)
            self.assertTrue(
                {"status", "client_entry_id", "session", "grade", "market_context", "circle_id"}
                <= trade_columns
            )


if __name__ == "__main__":
    unittest.main()
