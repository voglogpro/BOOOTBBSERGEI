from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path

from aiohttp import FormData
from aiohttp.test_utils import TestClient, TestServer

from journal.app import create_app
from journal.config import Settings


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp.name) / "api.sqlite3"
        settings = Settings(
            bot_token="",
            database_path=self.database_path,
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

    async def test_weekly_plan_image_trade_link_and_daily_result(self) -> None:
        response = await self.client.post("/api/weekly-plans", json={
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "Золото от поддержки", "idea": "Ищу продолжение роста",
            "trade_plan": "Вход после возврата уровня", "invalidation": "Ниже 4400",
            "visibility": "private",
        })
        self.assertEqual(response.status, 201)
        plan = (await response.json())["plan"]

        form = FormData()
        form.add_field(
            "image", b"\x89PNG\r\n\x1a\nchart",
            filename="plan.png", content_type="image/png",
        )
        response = await self.client.post(
            f"/api/weekly-plans/{plan['id']}/images", data=form
        )
        self.assertEqual(response.status, 201)
        image = (await response.json())["image"]
        response = await self.client.get(image["url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/png")

        response = await self.client.post("/api/trades", json={
            "traded_at": "2026-08-28T10:30:00", "symbol": "XAUUSD",
            "direction": "BUY", "status": "closed", "outcome_type": "take",
            "weekly_plan_id": plan["id"], "idea_followed": True,
            "risk_amount": 100, "pnl": 200, "plan_followed": True,
            "visibility": "private",
        })
        self.assertEqual(response.status, 201)

        response = await self.client.get("/api/weekly-plans?week=2026-08-27")
        weekly = (await response.json())["plans"][0]
        self.assertEqual(weekly["week_start"], "2026-08-24")
        self.assertEqual(weekly["pnl"], 200.0)
        self.assertEqual(weekly["idea_rate"], 100.0)
        self.assertEqual(weekly["days"][0]["day"], "2026-08-28")

        response = await self.client.get(
            "/api/calendar?from=2026-08-24&to=2026-08-30"
        )
        day = (await response.json())["days"][0]
        self.assertEqual(day["idea_followed"], 1)
        self.assertEqual(day["idea_broken"], 0)

    async def test_trade_image_upload_replace_list_delete_and_trade_cleanup(self) -> None:
        response = await self.client.post("/api/trades", json={
            "traded_at": "2026-08-28T10:30:00", "symbol": "XAUUSD",
            "direction": "BUY", "status": "closed", "pnl": 0,
            "plan_followed": True, "visibility": "private",
        })
        trade = (await response.json())["trade"]
        directory = self.database_path.parent / "uploads" / "trades"

        form = FormData()
        form.add_field(
            "image", b"\x89PNG\r\n\x1a\nentry", filename="entry.png",
            content_type="image/png",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/entry", data=form
        )
        self.assertEqual(response.status, 200)
        entry = (await response.json())["image"]
        self.assertEqual(entry["kind"], "entry")
        self.assertNotIn("storage_name", entry)
        first_files = list(directory.iterdir())
        self.assertEqual(len(first_files), 1)

        response = await self.client.get(entry["url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/png")

        form = FormData()
        form.add_field(
            "image", b"\xff\xd8\xffreplacement", filename="replacement.jpg",
            content_type="image/jpeg",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/entry", data=form
        )
        self.assertEqual(response.status, 200)
        replaced = (await response.json())["image"]
        self.assertEqual(replaced["id"], entry["id"])
        self.assertFalse(first_files[0].exists())
        self.assertEqual(len(list(directory.iterdir())), 1)

        response = await self.client.get(
            "/api/trades?from=2026-08-28&to=2026-08-28"
        )
        listed = (await response.json())["trades"][0]
        self.assertEqual(listed["images"][0]["url"], replaced["url"])
        self.assertNotIn("storage_name", listed["images"][0])

        response = await self.client.delete(replaced["url"])
        self.assertEqual(response.status, 200)
        self.assertEqual(list(directory.iterdir()), [])

        form = FormData()
        form.add_field(
            "image", b"RIFF\x04\x00\x00\x00WEBPresult", filename="result.webp",
            content_type="image/webp",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/result", data=form
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(len(list(directory.iterdir())), 1)
        response = await self.client.delete(f"/api/trades/{trade['id']}")
        self.assertEqual(response.status, 200)
        self.assertEqual(list(directory.iterdir()), [])

    async def test_trade_image_rejects_invalid_input_and_cleans_failed_upload(self) -> None:
        response = await self.client.post("/api/trades", json={
            "traded_at": "2026-08-28T10:30:00", "symbol": "XAUUSD",
            "direction": "BUY", "status": "closed", "pnl": 0,
            "plan_followed": True, "visibility": "private",
        })
        trade = (await response.json())["trade"]

        form = FormData()
        form.add_field(
            "image", b"not-an-image", filename="chart.svg",
            content_type="image/svg+xml",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/entry", data=form
        )
        self.assertEqual(response.status, 400)

        form = FormData()
        form.add_field(
            "image", b"\x89PNG\r\n\x1a\nchart", filename="chart.png",
            content_type="image/png",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/other", data=form
        )
        self.assertEqual(response.status, 400)

        form = FormData()
        form.add_field(
            "image", io.BytesIO(b"\x89PNG\r\n\x1a\n" + b"x" * (6 * 1024 * 1024)),
            filename="too-large.png", content_type="image/png",
        )
        response = await self.client.put(
            f"/api/trades/{trade['id']}/images/entry", data=form
        )
        self.assertEqual(response.status, 400)

        form = FormData()
        form.add_field(
            "image", b"\x89PNG\r\n\x1a\norphan", filename="chart.png",
            content_type="image/png",
        )
        response = await self.client.put("/api/trades/999999/images/entry", data=form)
        self.assertEqual(response.status, 400)
        directory = self.database_path.parent / "uploads" / "trades"
        self.assertEqual(list(directory.iterdir()), [])

    async def test_bootstrap_uses_client_local_date(self) -> None:
        await self.client.put("/api/moods/2030-01-02", json={
            "mood": 4, "energy": 4, "confidence": 3, "discipline": 5,
            "emotion": "Спокойствие", "visibility": "private",
        })
        response = await self.client.get("/api/bootstrap?date=2030-01-02")
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["today_mood"]["mood"], 4)

    async def test_entry_without_journal_permit_is_recorded_as_violation(self) -> None:
        response = await self.client.post("/api/trades", json={
            "traded_at": "2026-08-31T09:15:00", "symbol": "XAUUSD",
            "direction": "SELL", "status": "open", "journal_mode": "live",
            "entered_without_plan": True, "pnl": 0,
            "plan_followed": True, "visibility": "private",
        })
        self.assertEqual(response.status, 201)
        trade = (await response.json())["trade"]
        self.assertEqual(trade["entered_without_plan"], 1)
        self.assertEqual(trade["plan_followed"], 0)
        self.assertEqual(trade["grade"], "D")
        self.assertEqual(trade["mistake"], "Вход без допуска журнала")

    async def test_calendar_accepts_a_bounded_period_for_week_navigation(self) -> None:
        response = await self.client.get(
            "/api/calendar?from=2026-08-31&to=2026-10-04"
        )
        payload = await response.json()
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["from"], "2026-08-31")
        self.assertEqual(payload["to"], "2026-10-04")

        response = await self.client.get(
            "/api/calendar?from=2026-01-01&to=2026-04-01"
        )
        self.assertEqual(response.status, 400)

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
