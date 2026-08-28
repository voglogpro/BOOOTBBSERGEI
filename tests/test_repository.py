from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from journal.auth import dev_principal
from journal.db import initialize_database
from journal.repository import JournalRepository


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        await initialize_database(self.path)
        self.repo = JournalRepository(self.path)
        self.owner = await self.repo.upsert_user(dev_principal(101, "Сергей"))
        self.friend = await self.repo.upsert_user(dev_principal(202, "Друг"))

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_circle_is_limited_and_shared_entries_are_visible(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        joined = await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        self.assertEqual(len(joined["members"]), 2)

        base = {
            "traded_at": "2026-08-28T12:00:00", "symbol": "XAUUSD",
            "direction": "BUY", "timeframe": "M15", "setup": "Ретест",
            "entry_price": 4500.0, "stop_loss": 4490.0, "take_profit": 4520.0,
            "volume": 0.2, "risk_amount": 200.0, "pnl": 400.0, "r_multiple": 2.0,
            "emotion_before": "Спокойствие", "emotion_after": "Спокойствие",
            "plan_followed": 1, "mistake": "", "note": "", "screenshot_url": "",
            "visibility": "team",
        }
        await self.repo.create_trade(self.owner["id"], base)
        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["pnl"], 400.0)

    async def test_private_trade_is_not_visible_to_friend(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        trade = {
            "traded_at": "2026-08-28T12:00:00", "symbol": "EURUSD",
            "direction": "SELL", "timeframe": "H1", "setup": "",
            "entry_price": None, "stop_loss": None, "take_profit": None,
            "volume": None, "risk_amount": None, "pnl": -50.0, "r_multiple": None,
            "emotion_before": "", "emotion_after": "", "plan_followed": 0,
            "mistake": "FOMO", "note": "", "screenshot_url": "", "visibility": "private",
        }
        await self.repo.create_trade(self.owner["id"], trade)
        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(visible, [])

    async def test_mood_upsert_and_stats(self) -> None:
        mood = {
            "entry_date": "2026-08-28", "mood": 4, "energy": 3,
            "confidence": 4, "discipline": 5, "emotion": "Спокойствие",
            "note": "По плану", "visibility": "team",
        }
        await self.repo.upsert_mood(self.owner["id"], mood)
        mood["discipline"] = 4
        await self.repo.upsert_mood(self.owner["id"], mood)
        saved = await self.repo.get_mood(self.owner["id"], "2026-08-28")
        self.assertEqual(saved["discipline"], 4)


if __name__ == "__main__":
    unittest.main()

