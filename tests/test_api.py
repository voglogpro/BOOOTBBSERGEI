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
