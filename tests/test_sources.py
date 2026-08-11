from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.db import connect_db, init_db
from app.repository import (
    list_sources,
    register_verified_source,
    set_source_enabled,
    upsert_source,
)
from app.source_verification import ReadySourceVerification


CHAT_ID = -1001234567890


class SourceRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.settings = Settings(
            app_env="test",
            data_dir=root,
            database_path=root / "leads.sqlite3",
            timezone="Europe/Moscow",
            collector_enabled=False,
            rejected_message_retention_days=7,
            reader_catchup_limit=0,
            reader_queue_max=100,
        )
        await init_db(self.settings)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _verification(self, title: str = "Public group") -> ReadySourceVerification:
        return ReadySourceVerification(
            handle="public_group",
            title=title,
            telegram_chat_id=CHAT_ID,
            account_user_id=777,
            checked_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
            report_schema_version=1,
        )

    async def test_new_source_is_registered_disabled(self) -> None:
        await upsert_source(
            self.settings,
            telegram_chat_id=CHAT_ID,
            public_handle="public_group",
            title="Public group",
            source_kind="supergroup",
        )

        sources = await list_sources(self.settings)

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["enabled"], 0)
        self.assertIsNone(sources[0]["verified_at"])
        self.assertEqual(sources[0]["reader_status"], "paused")

    async def test_unverified_source_cannot_be_enabled(self) -> None:
        await upsert_source(
            self.settings,
            telegram_chat_id=CHAT_ID,
            public_handle="public_group",
            title="Public group",
        )

        with self.assertRaisesRegex(ValueError, "ready verification"):
            await set_source_enabled(
                self.settings,
                telegram_chat_id=CHAT_ID,
                enabled=True,
            )

    async def test_ready_source_can_be_enabled_explicitly(self) -> None:
        await register_verified_source(
            self.settings,
            verification=self._verification(),
        )

        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
        )

        source = (await list_sources(self.settings))[0]
        self.assertEqual(source["enabled"], 1)
        self.assertIsNotNone(source["verified_at"])

    async def test_upsert_does_not_reenable_manually_disabled_source(self) -> None:
        await register_verified_source(
            self.settings,
            verification=self._verification("Original title"),
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=False,
        )

        await upsert_source(
            self.settings,
            telegram_chat_id=CHAT_ID,
            public_handle="public_group",
            title="Updated title",
        )

        source = (await list_sources(self.settings))[0]
        self.assertEqual(source["enabled"], 0)
        self.assertEqual(source["title"], "Updated title")

    async def test_reregistering_active_source_fails_closed_to_disabled(self) -> None:
        await register_verified_source(
            self.settings,
            verification=self._verification("Original title"),
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
        )

        await upsert_source(
            self.settings,
            telegram_chat_id=CHAT_ID,
            public_handle="public_group",
            title="Reverified title",
        )

        source = (await list_sources(self.settings))[0]
        self.assertEqual(source["enabled"], 0)
        self.assertIsNone(source["verified_at"])
        self.assertEqual(source["reader_status"], "paused")

    async def test_init_db_disables_legacy_unverified_active_source(self) -> None:
        await upsert_source(
            self.settings,
            telegram_chat_id=CHAT_ID,
            public_handle="public_group",
            title="Legacy source",
        )
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE lead_sources SET enabled = 1 WHERE telegram_chat_id = ?",
                (CHAT_ID,),
            )
            await db.commit()
        finally:
            await db.close()

        await init_db(self.settings)

        source = (await list_sources(self.settings))[0]
        self.assertEqual(source["enabled"], 0)
        self.assertIsNone(source["verified_at"])


if __name__ == "__main__":
    unittest.main()
