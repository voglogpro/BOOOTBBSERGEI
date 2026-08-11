from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.config import Settings
from app.db import connect_db, init_db
from app.source_candidates import SourceCandidate, load_candidate_catalog
from server.source_service import (
    ResolvedReviewedSource,
    SourceManagementService,
    SourceServiceError,
)


ACCOUNT_ID = 777
ADMIN_ID = 888
CHAT_ID = -1001234567890


class FakeSourceReader:
    def __init__(self, *, account_user_id: int = ACCOUNT_ID) -> None:
        self.account_user_id = account_user_id
        self.resolved: list[str] = []
        self.refreshes = 0

    async def resolve_reviewed_source(
        self,
        candidate: SourceCandidate,
    ) -> ResolvedReviewedSource:
        self.resolved.append(candidate.handle)
        return ResolvedReviewedSource(
            handle=candidate.handle,
            title=f"Telegram {candidate.title}",
            telegram_chat_id=CHAT_ID,
            source_kind="channel",
            account_user_id=self.account_user_id,
            checked_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )

    async def refresh_allowlist(self) -> None:
        self.refreshes += 1


class SourceManagementServiceTests(unittest.IsolatedAsyncioTestCase):
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
        self.reader = FakeSourceReader()
        self.service = SourceManagementService(
            settings=self.settings,
            expected_account_user_id=ACCOUNT_ID,
            reader=self.reader,
        )
        self.candidate = load_candidate_catalog().candidates[0]

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_only_reviewed_catalog_handle_can_be_resolved(self) -> None:
        with self.assertRaisesRegex(SourceServiceError, "проверенном каталоге"):
            await self.service.verify(
                handle="unknown_public_group",
                actor_telegram_id=ADMIN_ID,
            )

        self.assertEqual(self.reader.resolved, [])

    async def test_verify_enable_disable_is_explicit_and_audited(self) -> None:
        verified = await self.service.verify(
            handle=self.candidate.handle,
            actor_telegram_id=ADMIN_ID,
        )
        self.assertFalse(verified["enabled"])
        self.assertEqual(verified["source_kind"], "channel")
        self.assertEqual(self.reader.refreshes, 1)

        enabled = await self.service.enable(
            source_id=int(verified["id"]),
            actor_telegram_id=ADMIN_ID,
        )
        self.assertTrue(enabled["enabled"])
        self.assertEqual(self.reader.refreshes, 2)

        disabled = await self.service.disable(
            source_id=int(verified["id"]),
            actor_telegram_id=ADMIN_ID,
        )
        self.assertFalse(disabled["enabled"])
        self.assertEqual(self.reader.refreshes, 3)

        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                """
                SELECT event_type, actor_kind, actor_telegram_id
                FROM source_audit_events ORDER BY id
                """
            )
            events = [tuple(row) for row in await cursor.fetchall()]
        finally:
            await db.close()
        self.assertEqual(
            events,
            [
                ("verified", "admin", ADMIN_ID),
                ("enabled", "admin", ADMIN_ID),
                ("disabled", "admin", ADMIN_ID),
            ],
        )

    async def test_reader_account_mismatch_is_rejected_before_repository(self) -> None:
        service = SourceManagementService(
            settings=self.settings,
            expected_account_user_id=ACCOUNT_ID,
            reader=FakeSourceReader(account_user_id=999),
        )

        with self.assertRaisesRegex(SourceServiceError, "другому"):
            await service.verify(
                handle=self.candidate.handle,
                actor_telegram_id=ADMIN_ID,
            )

        self.assertEqual(await service.sources(), [])

    async def test_catalog_overlays_runtime_state_without_session_material(self) -> None:
        before = await self.service.catalog()
        entry = next(
            item
            for item in before["candidates"]
            if item["handle"] == self.candidate.handle
        )
        self.assertEqual(entry["state"], "candidate")

        await self.service.verify(
            handle=self.candidate.handle,
            actor_telegram_id=ADMIN_ID,
        )
        after = await self.service.catalog()
        entry = next(
            item
            for item in after["candidates"]
            if item["handle"] == self.candidate.handle
        )
        self.assertEqual(entry["state"], "verified_disabled")
        self.assertNotIn("session", entry)

    async def test_stale_disabled_verification_can_be_checked_again(self) -> None:
        await self.service.verify(
            handle=self.candidate.handle,
            actor_telegram_id=ADMIN_ID,
        )
        stale = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE source_verifications SET verified_at = ?",
                (stale,),
            )
            await db.commit()
        finally:
            await db.close()

        payload = await self.service.catalog()
        entry = next(
            item
            for item in payload["candidates"]
            if item["handle"] == self.candidate.handle
        )
        self.assertEqual(entry["state"], "unverified")

    async def test_future_database_schema_blocks_service_operations(self) -> None:
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE app_meta SET value = '99' WHERE key = 'schema_version'"
            )
            await db.commit()
        finally:
            await db.close()
        service = SourceManagementService(
            settings=self.settings,
            expected_account_user_id=ACCOUNT_ID,
            reader=self.reader,
        )

        with self.assertRaisesRegex(RuntimeError, "newer"):
            await service.sources()

    async def test_removed_public_preview_verification_disables_source(self) -> None:
        verified = await self.service.verify(
            handle=self.candidate.handle,
            actor_telegram_id=ADMIN_ID,
        )
        await self.service.enable(
            source_id=int(verified["id"]),
            actor_telegram_id=ADMIN_ID,
        )

        repository_catalog = (
            Path(__file__).resolve().parent.parent
            / "research"
            / "source-candidates.json"
        )
        catalog_data = json.loads(repository_catalog.read_text(encoding="utf-8"))
        for candidate in catalog_data["candidates"]:
            if candidate["handle"].casefold() == self.candidate.handle.casefold():
                candidate["public_preview_verified"] = False
                break
        catalog_path = self.settings.data_dir / "catalog.json"
        catalog_path.write_text(
            json.dumps(catalog_data, ensure_ascii=False),
            encoding="utf-8",
        )
        service = SourceManagementService(
            settings=self.settings,
            expected_account_user_id=ACCOUNT_ID,
            reader=self.reader,
            catalog_path=catalog_path,
        )

        sources = await service.sources()
        source = next(item for item in sources if item["id"] == verified["id"])
        self.assertFalse(source["enabled"])


if __name__ == "__main__":
    unittest.main()
