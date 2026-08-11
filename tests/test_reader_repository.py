from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import aiosqlite.core

from app.config import Settings
from app.db import connect_db, init_db
from app.models import PublicMessageEvent
from app.repository import (
    cancel_pending_reader_events,
    claim_next_reader_event,
    complete_reader_event,
    enqueue_reader_event,
    fail_closed_invalid_enabled_sources,
    get_reader_runtime,
    purge_completed_reader_events,
    recover_reader_inbox,
    register_verified_source,
    set_reader_runtime,
    set_source_enabled,
)
from app.source_verification import ReadySourceVerification


CHAT_ID = -1001234567890
ACCOUNT_ID = 777


class ConnectDbCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_during_initial_connect_does_not_leak_sqlite_file(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        root = Path(temp_dir.name)
        settings = Settings(
            app_env="test",
            data_dir=root,
            database_path=root / "cancelled.sqlite3",
            timezone="Europe/Moscow",
            collector_enabled=False,
            rejected_message_retention_days=7,
            reader_catchup_limit=0,
            reader_queue_max=100,
        )
        real_connect = aiosqlite.core.sqlite3.connect

        def slow_connect(*args: object, **kwargs: object) -> object:
            time.sleep(0.15)
            return real_connect(*args, **kwargs)

        with patch("aiosqlite.core.sqlite3.connect", side_effect=slow_connect):
            task = asyncio.create_task(connect_db(settings))
            await asyncio.sleep(0.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        # This is the regression assertion on Windows: cleanup raises WinError
        # 32 if aiosqlite's connector thread or SQLite handle leaked.
        temp_dir.cleanup()


class ReaderRepositoryTests(unittest.IsolatedAsyncioTestCase):
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
        self.source_id = await register_verified_source(
            self.settings,
            verification=self._verification(),
            expected_account_user_id=ACCOUNT_ID,
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
            expected_account_user_id=ACCOUNT_ID,
        )

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    def _verification(
        self,
        *,
        checked_at: datetime | None = None,
        schema: int = 1,
        account_user_id: int = ACCOUNT_ID,
    ) -> ReadySourceVerification:
        return ReadySourceVerification(
            handle="public_group",
            title="Public group",
            telegram_chat_id=CHAT_ID,
            account_user_id=account_user_id,
            checked_at=(checked_at or datetime.now(UTC))
            .replace(microsecond=0)
            .isoformat(),
            report_schema_version=schema,
        )

    @staticmethod
    def _event() -> PublicMessageEvent:
        published_at = datetime.now(UTC).replace(microsecond=0).isoformat()
        payload = {
            "chat_id": CHAT_ID,
            "message_id": 42,
            "event_type": "new",
            "text": "Ищу франшизу с бюджетом 5 млн",
            "published_at": published_at.replace("+00:00", "Z"),
            "edited_at": None,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return PublicMessageEvent(
            request_id=f"tg:{digest}",
            telegram_chat_id=CHAT_ID,
            telegram_message_id=42,
            text=payload["text"],
            published_at=published_at,
            event_type="new",
        )

    async def test_repository_rejects_stale_or_untrusted_verification_objects(self) -> None:
        with self.assertRaisesRegex(ValueError, "older than 24 hours"):
            await register_verified_source(
                self.settings,
                verification=self._verification(
                    checked_at=datetime.now(UTC) - timedelta(days=2)
                ),
                expected_account_user_id=ACCOUNT_ID,
            )
        with self.assertRaisesRegex(ValueError, "schema"):
            await register_verified_source(
                self.settings,
                verification=self._verification(schema=999),
                expected_account_user_id=ACCOUNT_ID,
            )
        with self.assertRaisesRegex(ValueError, "unexpected"):
            await register_verified_source(
                self.settings,
                verification=self._verification(account_user_id=999),
                expected_account_user_id=ACCOUNT_ID,
            )

    async def test_inbox_is_durable_idempotent_and_scrubbed_after_ingest(self) -> None:
        event = self._event()
        payload_hash = event.request_id.removeprefix("tg:")
        first = await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=payload_hash,
            expected_account_user_id=ACCOUNT_ID,
        )
        duplicate = await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=payload_hash,
            expected_account_user_id=ACCOUNT_ID,
        )
        self.assertTrue(first.inserted)
        self.assertFalse(duplicate.inserted)
        self.assertEqual(duplicate.pending_count, 1)

        claimed = await claim_next_reader_event(self.settings)
        assert claimed is not None
        self.assertEqual(claimed.event.text, event.text)
        self.assertEqual(claimed.attempt_count, 1)

        self.assertEqual(await recover_reader_inbox(self.settings), 1)
        reclaimed = await claim_next_reader_event(self.settings)
        assert reclaimed is not None
        self.assertEqual(reclaimed.inbox_id, claimed.inbox_id)
        self.assertEqual(reclaimed.attempt_count, 2)
        await complete_reader_event(self.settings, item=reclaimed)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                "SELECT status, message_text FROM reader_inbox WHERE id = ?",
                (reclaimed.inbox_id,),
            )
            inbox = await cursor.fetchone()
            cursor = await db.execute(
                """
                SELECT last_message_id, reader_status
                FROM source_checkpoints WHERE source_id = ?
                """,
                (self.source_id,),
            )
            checkpoint = await cursor.fetchone()
        finally:
            await db.close()
        self.assertEqual(inbox["status"], "done")
        self.assertIsNone(inbox["message_text"])
        self.assertEqual(checkpoint["last_message_id"], 42)
        self.assertEqual(checkpoint["reader_status"], "ok")

    async def test_inbox_second_gate_rejects_other_account(self) -> None:
        event = self._event()
        result = await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=event.request_id.removeprefix("tg:"),
            expected_account_user_id=999,
        )
        self.assertFalse(result.inserted)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute("SELECT COUNT(*) AS n FROM reader_inbox")
            self.assertEqual((await cursor.fetchone())["n"], 0)
        finally:
            await db.close()

    async def test_removed_catalog_source_is_disabled_and_pending_text_scrubbed(self) -> None:
        event = self._event()
        await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=event.request_id.removeprefix("tg:"),
            expected_account_user_id=ACCOUNT_ID,
        )

        disabled = await fail_closed_invalid_enabled_sources(
            self.settings,
            expected_account_user_id=ACCOUNT_ID,
            allowed_public_handles={"another_reviewed_source"},
        )

        self.assertEqual(disabled, 1)
        db = await connect_db(self.settings)
        try:
            source = await (
                await db.execute(
                    "SELECT enabled FROM lead_sources WHERE id = ?",
                    (self.source_id,),
                )
            ).fetchone()
            inbox = await (
                await db.execute(
                    "SELECT status, message_text FROM reader_inbox"
                )
            ).fetchone()
        finally:
            await db.close()
        self.assertEqual(source["enabled"], 0)
        self.assertEqual(inbox["status"], "dead")
        self.assertIsNone(inbox["message_text"])

    async def test_runtime_status_is_persisted_with_pending_count(self) -> None:
        event = self._event()
        await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=event.request_id.removeprefix("tg:"),
            expected_account_user_id=ACCOUNT_ID,
        )
        snapshot = await set_reader_runtime(
            self.settings,
            state="running",
            account_user_id=ACCOUNT_ID,
            active_source_count=1,
            connected=True,
        )
        self.assertEqual(snapshot.pending_event_count, 1)
        self.assertEqual(snapshot.active_source_count, 1)
        self.assertIsNotNone(snapshot.connected_at)
        self.assertEqual(await get_reader_runtime(self.settings), snapshot)

    async def test_completed_inbox_metadata_is_purged_after_retention(self) -> None:
        event = self._event()
        await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=event.request_id.removeprefix("tg:"),
            expected_account_user_id=ACCOUNT_ID,
        )
        item = await claim_next_reader_event(self.settings)
        assert item is not None
        await complete_reader_event(self.settings, item=item)
        db = await connect_db(self.settings)
        try:
            await db.execute(
                """
                UPDATE reader_inbox
                SET completed_at = '2026-08-01T00:00:00Z',
                    updated_at = '2026-08-01T00:00:00Z'
                WHERE id = ?
                """,
                (item.inbox_id,),
            )
            await db.commit()
        finally:
            await db.close()

        deleted = await purge_completed_reader_events(
            self.settings,
            now="2026-08-11T00:00:00Z",
        )
        self.assertEqual(deleted, 1)

    async def test_disabling_source_scrubs_an_inflight_claim_idempotently(self) -> None:
        event = self._event()
        await enqueue_reader_event(
            self.settings,
            event=event,
            payload_hash=event.request_id.removeprefix("tg:"),
            expected_account_user_id=ACCOUNT_ID,
        )
        item = await claim_next_reader_event(self.settings)
        assert item is not None

        scrubbed = await cancel_pending_reader_events(
            self.settings,
            source_id=self.source_id,
        )
        self.assertEqual(scrubbed, 1)
        # A worker that already owns the in-memory item may finish after the
        # source was disabled; completion must not resurrect or fail the row.
        await complete_reader_event(self.settings, item=item)
        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                "SELECT status, message_text FROM reader_inbox WHERE id = ?",
                (item.inbox_id,),
            )
            row = await cursor.fetchone()
        finally:
            await db.close()
        assert row is not None
        self.assertEqual(str(row["status"]), "dead")
        self.assertIsNone(row["message_text"])

    async def test_v2_volume_gets_additive_reader_schema_migration(self) -> None:
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE app_meta SET value = '2' WHERE key = 'schema_version'"
            )
            await db.execute("DROP TABLE source_audit_events")
            await db.execute("DROP TABLE reader_inbox")
            await db.execute("DROP TABLE reader_runtime")
            await db.commit()
        finally:
            await db.close()

        await init_db(self.settings)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name IN (
                      'source_audit_events', 'reader_inbox', 'reader_runtime'
                  )
                ORDER BY name
                """
            )
            names = [str(row["name"]) for row in await cursor.fetchall()]
            cursor = await db.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            )
            version = str((await cursor.fetchone())["value"])
        finally:
            await db.close()
        self.assertEqual(
            names,
            ["reader_inbox", "reader_runtime", "source_audit_events"],
        )
        self.assertEqual(version, "3")

    async def test_newer_database_schema_is_rejected_before_migration(self) -> None:
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE app_meta SET value = '99' WHERE key = 'schema_version'"
            )
            await db.commit()
        finally:
            await db.close()

        with self.assertRaisesRegex(RuntimeError, "newer"):
            await init_db(self.settings)


if __name__ == "__main__":
    unittest.main()
