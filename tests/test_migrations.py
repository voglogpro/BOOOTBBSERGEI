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
            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            db.close()
            self.assertTrue(
                {"focus", "lesson", "journal_mode", "market_bias", "day_idea",
                 "key_levels", "day_invalidation", "news_context", "circle_id"}
                <= mood_columns
            )
            self.assertTrue(
                {"status", "client_entry_id", "session", "grade", "market_context",
                 "journal_mode", "confidence_before", "trade_plan", "entry_trigger",
                 "trade_invalidation", "outcome_type", "weekly_plan_id",
                 "idea_followed", "circle_id"}
                <= trade_columns
            )
            self.assertTrue({"weekly_plans", "weekly_plan_images"} <= tables)


if __name__ == "__main__":
    unittest.main()
