from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from journal.auth import dev_principal
from journal.db import initialize_database
from journal.repository import JournalRepository, RepositoryError


class RepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "journal.sqlite3"
        await initialize_database(self.path)
        self.repo = JournalRepository(self.path)
        self.owner = await self.repo.upsert_user(dev_principal(101, "Сергей"))
        self.friend = await self.repo.upsert_user(dev_principal(202, "Друг"))

    @staticmethod
    def trade(**overrides):
        values = {
            "traded_at": "2026-08-28T12:00:00", "symbol": "XAUUSD",
            "direction": "BUY", "timeframe": "M15", "setup": "Ретест",
            "entry_price": 4500.0, "stop_loss": 4490.0, "take_profit": 4520.0,
            "volume": 0.2, "risk_amount": 200.0, "pnl": 400.0, "r_multiple": 2.0,
            "emotion_before": "Спокойствие", "emotion_after": "Спокойствие",
            "plan_followed": 1, "mistake": "", "note": "", "screenshot_url": "",
            "visibility": "team",
        }
        values.update(overrides)
        return values

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    async def test_circle_is_limited_and_shared_entries_are_visible(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        joined = await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        self.assertEqual(len(joined["members"]), 2)

        await self.repo.create_trade(self.owner["id"], self.trade())
        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["pnl"], 400.0)

    async def test_private_trade_is_not_visible_to_friend(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        await self.repo.create_trade(self.owner["id"], self.trade(
            symbol="EURUSD", direction="SELL", timeframe="H1", setup="",
            entry_price=None, stop_loss=None, take_profit=None, volume=None,
            risk_amount=None, pnl=-50.0, r_multiple=None, plan_followed=0,
            mistake="FOMO", visibility="private",
        ))
        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(visible, [])

    async def test_trade_image_metadata_and_permissions_follow_trade(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        trade = await self.repo.create_trade(self.owner["id"], self.trade())
        image, previous = await self.repo.save_trade_image(
            self.owner["id"], trade["id"], "entry", {
                "storage_name": "owner_entry.png", "original_name": "chart.png",
                "mime_type": "image/png", "size_bytes": 12,
            },
        )
        self.assertIsNone(previous)

        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28",
            scope="team",
        )
        self.assertEqual(visible[0]["images"][0]["kind"], "entry")
        self.assertNotIn("storage_name", visible[0]["images"][0])
        shared = await self.repo.get_trade_image(self.friend["id"], image["id"])
        self.assertEqual(shared["storage_name"], "owner_entry.png")
        with self.assertRaises(RepositoryError):
            await self.repo.delete_trade_image(self.friend["id"], image["id"])

        private_trade = await self.repo.create_trade(
            self.owner["id"], self.trade(symbol="EURUSD", visibility="private")
        )
        private_image, _ = await self.repo.save_trade_image(
            self.owner["id"], private_trade["id"], "result", {
                "storage_name": "private_result.jpg", "original_name": "result.jpg",
                "mime_type": "image/jpeg", "size_bytes": 10,
            },
        )
        with self.assertRaises(RepositoryError):
            await self.repo.get_trade_image(self.friend["id"], private_image["id"])

    async def test_trade_image_slot_is_replaced_and_deleted_with_trade(self) -> None:
        trade = await self.repo.create_trade(
            self.owner["id"], self.trade(visibility="private")
        )
        first, previous = await self.repo.save_trade_image(
            self.owner["id"], trade["id"], "entry", {
                "storage_name": "first.png", "original_name": "first.png",
                "mime_type": "image/png", "size_bytes": 8,
            },
        )
        self.assertIsNone(previous)
        second, previous = await self.repo.save_trade_image(
            self.owner["id"], trade["id"], "entry", {
                "storage_name": "second.jpg", "original_name": "second.jpg",
                "mime_type": "image/jpeg", "size_bytes": 9,
            },
        )
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(previous, "first.png")
        files = await self.repo.delete_trade(self.owner["id"], trade["id"])
        self.assertEqual(files, ["second.jpg"])
        with self.assertRaises(RepositoryError):
            await self.repo.get_trade_image(self.owner["id"], second["id"])

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

    async def test_open_trades_are_visible_but_excluded_from_performance(self) -> None:
        await self.repo.create_trade(self.owner["id"], self.trade())
        await self.repo.create_trade(
            self.owner["id"],
            self.trade(status="open", client_entry_id="open_trade_001", pnl=0),
        )

        calendar = await self.repo.calendar(
            self.owner["id"], start_date="2026-08-28", end_date="2026-08-28"
        )
        stats = await self.repo.stats(
            self.owner["id"], start_date="2026-08-28", end_date="2026-08-28"
        )

        self.assertEqual(calendar[0]["trades"], 2)
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["pnl"], 400.0)

    async def test_old_team_entries_are_not_exposed_to_a_new_partner(self) -> None:
        first_circle = await self.repo.create_circle(self.owner["id"], "Первая пара")
        await self.repo.join_circle(self.friend["id"], first_circle["invite_code"])
        await self.repo.create_trade(self.owner["id"], self.trade())

        await self.repo.leave_circle(self.owner["id"])
        self.assertIsNone(await self.repo.get_circle(self.friend["id"]))
        newcomer = await self.repo.upsert_user(dev_principal(303, "Новый друг"))
        second_circle = await self.repo.create_circle(self.owner["id"], "Вторая пара")
        await self.repo.join_circle(newcomer["id"], second_circle["invite_code"])

        visible = await self.repo.list_trades(
            newcomer["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(visible, [])
        with self.assertRaises(RepositoryError):
            await self.repo.join_circle(self.friend["id"], first_circle["invite_code"])

    async def test_entry_created_before_circle_stays_private(self) -> None:
        await self.repo.create_trade(self.owner["id"], self.trade())
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28", scope="team"
        )
        self.assertEqual(visible, [])

    async def test_stats_connect_confidence_and_daily_mood_to_outcomes(self) -> None:
        for day, mood_value, confidence, outcome, pnl in (
            ("2026-08-27", 2, 2, "stop", -100.0),
            ("2026-08-28", 5, 5, "take", 200.0),
        ):
            await self.repo.upsert_mood(self.owner["id"], {
                "entry_date": day, "mood": mood_value, "energy": 3,
                "confidence": confidence, "discipline": 4, "emotion": "",
                "note": "", "visibility": "private", "journal_mode": "backtest",
                "market_bias": "LONG", "day_idea": "Тест идеи",
            })
            await self.repo.create_trade(self.owner["id"], self.trade(
                traded_at=f"{day}T12:00:00", confidence_before=confidence,
                journal_mode="backtest", outcome_type=outcome, pnl=pnl,
                r_multiple=pnl / 100, visibility="private",
            ))

        stats = await self.repo.stats(
            self.owner["id"], start_date="2026-08-27", end_date="2026-08-28"
        )
        confidence = {row["label"]: row for row in stats["confidence_patterns"]}
        moods = {row["label"]: row for row in stats["mood_patterns"]}
        self.assertEqual(confidence[2]["stops"], 1)
        self.assertEqual(confidence[5]["takes"], 1)
        self.assertEqual(moods[2]["pnl"], -100.0)
        self.assertEqual(moods[5]["pnl"], 200.0)

    async def test_team_stats_do_not_infer_a_private_daily_mood(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        await self.repo.upsert_mood(self.owner["id"], {
            "entry_date": "2026-08-28", "mood": 1, "energy": 1,
            "confidence": 1, "discipline": 1, "emotion": "Тревога",
            "note": "", "visibility": "private",
        })
        await self.repo.create_trade(self.owner["id"], self.trade(
            outcome_type="stop", pnl=-100, visibility="team"
        ))

        stats = await self.repo.stats(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28",
            scope="team",
        )
        self.assertEqual(stats["trades"], 1)
        self.assertEqual(stats["mood_patterns"], [])

    async def test_weekly_plan_rejects_trade_from_another_symbol(self) -> None:
        plan = await self.repo.save_weekly_plan(self.owner["id"], {
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "План", "idea": "Рост", "trade_plan": "После ретеста",
            "invalidation": "Ниже уровня", "status": "active", "week_summary": "",
            "week_lesson": "", "rating": None, "visibility": "private",
        })
        with self.assertRaises(RepositoryError):
            await self.repo.create_trade(self.owner["id"], self.trade(
                symbol="EURUSD", weekly_plan_id=plan["id"], idea_followed=1,
            ))

    async def test_countertrend_trade_is_saved_without_forced_confirmation(self) -> None:
        plan = await self.repo.save_weekly_plan(self.owner["id"], {
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "Тренд вверх", "idea": "Покупки", "trade_plan": "По тренду",
            "invalidation": "Смена структуры", "status": "active", "week_summary": "",
            "week_lesson": "", "rating": None, "visibility": "private",
        })
        unconfirmed = await self.repo.create_trade(self.owner["id"], self.trade(
            direction="SELL", weekly_plan_id=plan["id"],
            countertrend_confirmed=0,
        ))
        self.assertEqual(unconfirmed["idea_followed"], 0)

        trade = await self.repo.create_trade(self.owner["id"], self.trade(
            direction="SELL", weekly_plan_id=plan["id"],
            countertrend_confirmed=1,
        ))
        self.assertEqual(trade["countertrend_confirmed"], 1)

    async def test_live_trade_must_be_saved_as_plan_before_opening(self) -> None:
        plan = await self.repo.save_weekly_plan(self.owner["id"], {
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "Покупки после подтверждения", "idea": "Работаю вверх",
            "trade_plan": "Жду возврат уровня", "invalidation": "Ниже 4490",
            "status": "active", "week_summary": "", "week_lesson": "",
            "rating": None, "visibility": "private",
        })
        live_trade = self.trade(
            status="open", journal_mode="live", weekly_plan_id=plan["id"],
            trade_plan="Покупка после возврата", entry_trigger="Закрытие M15 выше 4500",
            trade_invalidation="Закрытие ниже 4490", trigger_confirmed=1,
            trigger_evidence="M15 закрылся выше 4500", pnl=0,
        )
        with self.assertRaisesRegex(RepositoryError, "Сначала сохраните план"):
            await self.repo.create_trade(self.owner["id"], live_trade)

        planned = await self.repo.create_trade(
            self.owner["id"], {**live_trade, "status": "planned", "trigger_confirmed": 0}
        )
        opened = await self.repo.update_trade(
            self.owner["id"], planned["id"], live_trade
        )
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["trigger_confirmed"], 1)

    async def test_live_saved_plan_allows_optional_journal_fields(self) -> None:
        planned_payload = self.trade(
            status="planned", journal_mode="live", weekly_plan_id=None,
            trade_plan="", entry_trigger="", trade_invalidation="",
            trigger_confirmed=0, trigger_evidence="", stop_loss=None,
            risk_amount=None, pnl=0,
        )
        planned = await self.repo.create_trade(self.owner["id"], planned_payload)
        opened = await self.repo.update_trade(
            self.owner["id"], planned["id"], {**planned_payload, "status": "open"}
        )
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["trade_plan"], "")
        self.assertIsNone(opened["risk_amount"])

    async def test_live_countertrend_evidence_is_optional(self) -> None:
        plan = await self.repo.save_weekly_plan(self.owner["id"], {
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "Покупки", "idea": "Основной сценарий вверх",
            "trade_plan": "Только покупки", "invalidation": "Смена структуры",
            "status": "active", "week_summary": "", "week_lesson": "",
            "rating": None, "visibility": "private",
        })
        planned = await self.repo.create_trade(self.owner["id"], self.trade(
            status="planned", journal_mode="live", direction="SELL",
            weekly_plan_id=plan["id"], trade_plan="Продажа после разворота",
            entry_trigger="Слом структуры M15", trade_invalidation="Возврат выше максимума",
            countertrend_confirmed=1, pnl=0,
        ))
        opened = await self.repo.update_trade(self.owner["id"], planned["id"], self.trade(
            status="open", journal_mode="live", direction="SELL",
            weekly_plan_id=plan["id"], trade_plan="Продажа после разворота",
            entry_trigger="Слом структуры M15", trade_invalidation="Возврат выше максимума",
            countertrend_confirmed=1, countertrend_evidence="мало",
            trigger_confirmed=1, trigger_evidence="M15 сломал структуру вниз", pnl=0,
        ))
        self.assertEqual(opened["status"], "open")
        self.assertEqual(opened["idea_followed"], 0)

    async def test_private_weekly_plan_details_are_not_exposed_with_shared_trade(self) -> None:
        circle = await self.repo.create_circle(self.owner["id"], "Дуэт")
        await self.repo.join_circle(self.friend["id"], circle["invite_code"])
        plan = await self.repo.save_weekly_plan(self.owner["id"], {
            "week_start": "2026-08-24", "symbol": "XAUUSD", "bias": "LONG",
            "title": "Приватная идея", "idea": "Рост", "trade_plan": "Ретест",
            "invalidation": "Ниже уровня", "status": "active", "week_summary": "",
            "week_lesson": "", "rating": None, "visibility": "private",
        })
        image = await self.repo.add_weekly_plan_image(self.owner["id"], plan["id"], {
            "storage_name": "private.jpg", "original_name": "chart.jpg",
            "mime_type": "image/jpeg", "size_bytes": 4,
        })
        await self.repo.create_trade(self.owner["id"], self.trade(
            weekly_plan_id=plan["id"], idea_followed=1, visibility="team",
        ))

        visible = await self.repo.list_trades(
            self.friend["id"], start_date="2026-08-28", end_date="2026-08-28",
            scope="team",
        )
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["weekly_plan_title"], "")
        self.assertEqual(visible[0]["weekly_plan_symbol"], "")
        self.assertEqual(visible[0]["weekly_plan_bias"], "")
        with self.assertRaises(RepositoryError):
            await self.repo.get_weekly_plan_image(self.friend["id"], image["id"])


if __name__ == "__main__":
    unittest.main()
