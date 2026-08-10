from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.config import Settings
from app.db import connect_db, init_db
from app.ingest import (
    CollectorDisabled,
    IdempotencyConflict,
    SourceNotAllowed,
    ingest_public_message,
)
from app.models import PublicMessageEvent
from app.repository import (
    purge_expired_rejections,
    register_verified_source,
    set_source_enabled,
    upsert_city,
)
from app.source_verification import ReadySourceVerification


CHAT_ID = -1001234567890
PUBLISHED_AT = "2026-08-09T12:00:00Z"


class IngestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            app_env="test",
            data_dir=root,
            database_path=root / "leads.sqlite3",
            timezone="Europe/Moscow",
            collector_enabled=True,
            rejected_message_retention_days=7,
            reader_catchup_limit=0,
            reader_queue_max=100,
        )
        await init_db(self.settings)
        await upsert_city(
            self.settings,
            slug="ekaterinburg",
            name="Екатеринбург",
            region="Свердловская область",
            aliases=("Екатеринбург", "Екб"),
        )
        await register_verified_source(
            self.settings,
            verification=ReadySourceVerification(
                handle="ekb_business",
                title="Бизнес Екатеринбурга",
                telegram_chat_id=CHAT_ID,
                account_user_id=777,
                checked_at="2026-08-09T12:00:00+00:00",
                report_schema_version=1,
            ),
            city_slug="ekaterinburg",
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def event(
        self,
        *,
        request_id: str,
        text: str,
        message_id: int = 100,
        chat_id: int = CHAT_ID,
        event_type: str = "new",
        edited_at: str | None = None,
    ) -> PublicMessageEvent:
        return PublicMessageEvent(
            request_id=request_id,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
            text=text,
            published_at=PUBLISHED_AT,
            edited_at=edited_at,
            event_type=event_type,
        )

    async def test_allowlisted_lead_is_created_with_canonical_link(self) -> None:
        result = await ingest_public_message(
            self.settings,
            self.event(
                request_id="req-1",
                text="Ищу франшизу, готов вложить 7 млн в Екатеринбурге",
            ),
        )
        self.assertEqual(result.result, "created")
        self.assertEqual(result.decision, "lead")
        self.assertIsNotNone(result.lead_id)
        self.assertEqual(result.message_url, "https://t.me/ekb_business/100")

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                """
                SELECT c.slug
                FROM franchise_leads AS l
                JOIN market_cities AS c ON c.id = l.detected_city_id
                WHERE l.id = ?
                """,
                (result.lead_id,),
            )
            row = await cursor.fetchone()
            self.assertEqual(row["slug"], "ekaterinburg")
        finally:
            await db.close()

    async def test_unknown_source_is_rejected(self) -> None:
        with self.assertRaises(SourceNotAllowed):
            await ingest_public_message(
                self.settings,
                self.event(
                    request_id="req-unknown",
                    chat_id=-1009999999999,
                    text="Ищу франшизу",
                ),
            )

    async def test_global_collector_switch_fails_closed(self) -> None:
        disabled = replace(self.settings, collector_enabled=False)

        with self.assertRaises(CollectorDisabled):
            await ingest_public_message(
                disabled,
                self.event(
                    request_id="collector-off",
                    text="Ищу франшизу",
                ),
            )

    async def test_same_request_is_idempotent(self) -> None:
        event = self.event(request_id="same-request", text="Какую франшизу выбрать?")
        first = await ingest_public_message(self.settings, event)
        second = await ingest_public_message(self.settings, event)
        self.assertEqual(first.observation_id, second.observation_id)
        self.assertEqual(first.lead_id, second.lead_id)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM franchise_leads")
            self.assertEqual((await cursor.fetchone())["n"], 1)
            cursor = await db.execute("SELECT COUNT(*) AS n FROM lead_events")
            self.assertEqual((await cursor.fetchone())["n"], 1)
        finally:
            await db.close()

    async def test_same_message_with_new_request_is_duplicate(self) -> None:
        first = await ingest_public_message(
            self.settings,
            self.event(request_id="delivery-1", text="Какую франшизу выбрать?"),
        )
        second = await ingest_public_message(
            self.settings,
            self.event(request_id="delivery-2", text="Какую франшизу выбрать?"),
        )
        self.assertEqual(second.result, "duplicate")
        self.assertEqual(first.observation_id, second.observation_id)
        self.assertEqual(first.lead_id, second.lead_id)

    async def test_request_id_cannot_be_reused_for_other_payload(self) -> None:
        await ingest_public_message(
            self.settings,
            self.event(request_id="fixed-id", text="Какую франшизу выбрать?"),
        )
        with self.assertRaises(IdempotencyConflict):
            await ingest_public_message(
                self.settings,
                self.event(request_id="fixed-id", text="Во что вложить 5 млн?"),
            )

    async def test_edit_reclassifies_without_deleting_existing_lead(self) -> None:
        first = await ingest_public_message(
            self.settings,
            self.event(request_id="before-edit", text="Какую франшизу выбрать?"),
        )
        edited = await ingest_public_message(
            self.settings,
            self.event(
                request_id="after-edit",
                text="Продаём курс и приглашаем на вебинар",
                event_type="edited",
                edited_at="2026-08-09T12:05:00Z",
            ),
        )
        self.assertEqual(edited.result, "updated")
        self.assertEqual(edited.revision, 2)
        self.assertEqual(edited.decision, "rejected")
        self.assertEqual(edited.lead_id, first.lead_id)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                "SELECT status, needs_review FROM franchise_leads WHERE id = ?",
                (first.lead_id,),
            )
            row = await cursor.fetchone()
            self.assertEqual(row["status"], "new")
            self.assertEqual(row["needs_review"], 1)
        finally:
            await db.close()

    async def test_expired_rejected_observation_is_purged(self) -> None:
        rejected = await ingest_public_message(
            self.settings,
            self.event(request_id="reject", text="Обычная новость рынка"),
        )
        self.assertIsNone(rejected.lead_id)
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE message_observations SET purge_after = ? WHERE id = ?",
                ("2020-01-01T00:00:00Z", rejected.observation_id),
            )
            await db.commit()
        finally:
            await db.close()

        deleted = await purge_expired_rejections(
            self.settings, now="2026-08-09T13:00:00Z"
        )
        self.assertEqual(deleted, 1)


if __name__ == "__main__":
    unittest.main()
