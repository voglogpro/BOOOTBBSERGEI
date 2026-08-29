from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from journal.app import create_app
from journal.config import Settings


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            bot_token="",
            database_path=Path(self.temp.name) / "api.sqlite3",
            dev_mode=True,
            dev_user_id=999,
            dev_user_name="Локальный трейдер",
        )
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_bootstrap_and_trade_flow(self) -> None:
        response = await self.client.get("/api/bootstrap")
        self.assertEqual(response.status, 200)
        bootstrap = await response.json()
        self.assertEqual(bootstrap["user"]["telegram_id"], 999)
        self.assertEqual(bootstrap["settings"]["balance"], 50000)

        response = await self.client.post(
            "/api/trades",
            json={
                "traded_at": "2026-08-28T10:30:00", "symbol": "XAUUSD",
                "direction": "BUY", "timeframe": "M15", "pnl": 250,
                "plan_followed": True, "visibility": "team",
            },
        )
        self.assertEqual(response.status, 201)
        trade = (await response.json())["trade"]
        self.assertEqual(trade["symbol"], "XAUUSD")

        response = await self.client.get(
            "/api/trades?from=2026-08-28&to=2026-08-28"
        )
        self.assertEqual(len((await response.json())["trades"]), 1)

    async def test_backtest_plan_outcome_and_ai_export(self) -> None:
        response = await self.client.put("/api/moods/2026-08-28", json={
            "mood": 2, "energy": 3, "confidence": 2, "discipline": 4,
            "emotion": "Сомнение", "visibility": "private",
            "journal_mode": "backtest", "market_bias": "LONG",
            "day_idea": "Возврат выше дневного уровня", "key_levels": "4500",
            "day_invalidation": "Закрепление ниже 4490", "news_context": "Без новостей",
        })
        self.assertEqual(response.status, 200)
        response = await self.client.post("/api/trades", json={
            "traded_at": "2026-08-28T10:30:00", "symbol": "XAUUSD",
            "direction": "BUY", "status": "closed", "journal_mode": "backtest",
            "confidence_before": 2, "trade_plan": "Лонг после возврата уровня",
            "entry_trigger": "Закрытие M15 выше 4500", "trade_invalidation": "Ниже 4490",
            "outcome_type": "stop", "risk_amount": 100, "pnl": -100,
            "plan_followed": True, "visibility": "private",
        })
        self.assertEqual(response.status, 201)
        trade = (await response.json())["trade"]
        self.assertEqual(trade["outcome_type"], "stop")
        self.assertEqual(trade["confidence_before"], 2)

        response = await self.client.get("/api/export")
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertIn("day_idea", body)
        self.assertIn("Возврат выше дневного уровня", body)
        self.assertIn("Лонг после возврата уровня", body)

    async def test_trade_creation_is_idempotent_and_validated(self) -> None:
        payload = {
            "client_entry_id": "entry_12345678", "traded_at": "2026-08-28T10:30:00",
            "symbol": "XAUUSD", "direction": "BUY", "status": "closed",
            "risk_amount": 100, "pnl": 250, "plan_followed": True,
            "visibility": "private",
        }
        first = await self.client.post("/api/trades", json=payload)
        second = await self.client.post("/api/trades", json=payload)
        self.assertEqual(first.status, 201)
        self.assertEqual(second.status, 201)
        self.assertEqual((await first.json())["trade"]["id"], (await second.json())["trade"]["id"])

        response = await self.client.post("/api/trades", json={**payload, "client_entry_id": "entry_bad_123", "risk_amount": -1})
        self.assertEqual(response.status, 400)

    async def test_bootstrap_uses_client_local_date(self) -> None:
        await self.client.put("/api/moods/2030-01-02", json={
            "mood": 4, "energy": 4, "confidence": 3, "discipline": 5,
            "emotion": "Спокойствие", "visibility": "private",
        })
        response = await self.client.get("/api/bootstrap?date=2030-01-02")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["today_mood"]["mood"], 4)

    async def test_health_does_not_require_telegram_auth(self) -> None:
        response = await self.client.get("/health")
        self.assertEqual(response.status, 200)

    async def test_index_is_served_from_project_root(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn('src="/client"', html)
        self.assertNotIn("/static/app.js", html)

        response = await self.client.get("/client")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "application/javascript")
        self.assertIn("window.Telegram", await response.text())


if __name__ == "__main__":
    unittest.main()
