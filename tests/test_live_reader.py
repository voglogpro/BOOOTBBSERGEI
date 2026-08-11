from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cryptography.fernet import Fernet
from telethon.sessions import StringSession

from app.config import Settings
from app.db import connect_db, init_db
from app.models import IngestResult
from app.repository import (
    register_verified_source,
    set_reader_runtime as repository_set_reader_runtime,
    set_source_enabled,
)
from app.source_candidates import load_candidate_catalog
from app.source_verification import ReadySourceVerification
from server.live_reader import (
    LiveReaderService,
    ReaderLeaseError,
    _ProcessLease,
    build_live_reader_client,
)
from server.settings import ServerSettings
from server.source_service import SourceResolutionError


CHAT_ID = -1001234567890
ACCOUNT_ID = 777


class FakeSessionStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.loads = 0

    def load(self) -> object:
        self.loads += 1
        return object()


class FakeClient:
    def __init__(self, entity: object | None = None) -> None:
        self.session = SimpleNamespace(save_entities=True)
        self.entity = entity
        self.connected = False
        self.connects = 0
        self.disconnects = 0
        self.entity_requests: list[str] = []
        self.handlers: list[tuple[object, object]] = []

    async def connect(self) -> None:
        self.connected = True
        self.connects += 1

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> object:
        return SimpleNamespace(id=ACCOUNT_ID, username="reader", bot=False)

    def is_connected(self) -> bool:
        return self.connected

    async def get_entity(self, handle: str) -> object:
        self.entity_requests.append(handle)
        if isinstance(self.entity, BaseException):
            raise self.entity
        assert self.entity is not None
        return self.entity

    def add_event_handler(self, callback: object, builder: object) -> None:
        self.handlers.append((callback, builder))

    def remove_event_handler(self, callback: object, builder: object) -> None:
        try:
            self.handlers.remove((callback, builder))
        except ValueError:
            pass

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnects += 1


def _entity(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "username": "businessclass_rbc",
        "title": "Business channel",
        "megagroup": False,
        "broadcast": True,
        "forum": False,
        "left": False,
        "kicked": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class LiveReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.root = root
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
        self.server_settings = ServerSettings(
            bot_token="123:token",
            admin_telegram_ids=frozenset({888}),
            telegram_api_id=12345,
            telegram_api_hash="a" * 32,
            telegram_expected_user_id=ACCOUNT_ID,
            session_encryption_key=Fernet.generate_key().decode("ascii"),
            encrypted_session_path=root / "reader.session.enc",
            host="127.0.0.1",
            port=3000,
            init_data_max_age_seconds=300,
            login_challenge_ttl_seconds=300,
        )
        await init_db(self.settings)

    async def asyncTearDown(self) -> None:
        self.temp_dir.cleanup()

    async def _enable_source(self) -> None:
        await register_verified_source(
            self.settings,
            verification=ReadySourceVerification(
                handle="businessclass_rbc",
                title="Business channel",
                telegram_chat_id=CHAT_ID,
                account_user_id=ACCOUNT_ID,
                checked_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                report_schema_version=1,
            ),
            source_kind="channel",
            expected_account_user_id=ACCOUNT_ID,
        )
        await set_source_enabled(
            self.settings,
            telegram_chat_id=CHAT_ID,
            enabled=True,
            expected_account_user_id=ACCOUNT_ID,
        )

    async def test_collector_kill_switch_stays_disconnected(self) -> None:
        client = FakeClient(_entity())
        service = LiveReaderService(
            app_settings=replace(self.settings, collector_enabled=False),
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )

        await service.start()
        status = await service.status()
        assert status is not None
        self.assertEqual(status.state, "paused")
        self.assertEqual(status.last_error_code, "collector_disabled")
        self.assertEqual(client.connects, 0)
        await service.stop()

    async def test_resolver_accepts_only_public_already_joined_channel(self) -> None:
        client = FakeClient(_entity())
        service = LiveReaderService(
            app_settings=replace(self.settings, collector_enabled=False),
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        await service.start()
        candidate = load_candidate_catalog().candidates[0]

        resolved = await service.resolve_reviewed_source(candidate)

        self.assertEqual(resolved.source_kind, "channel")
        self.assertEqual(resolved.telegram_chat_id, CHAT_ID)
        self.assertEqual(client.entity_requests, [candidate.handle])
        self.assertEqual(client.disconnects, 1)
        self.assertFalse(client.session.save_entities)

        client.entity = _entity(left=True)
        with self.assertRaisesRegex(SourceResolutionError, "официальном Telegram"):
            await service.resolve_reviewed_source(candidate)
        self.assertEqual(client.entity_requests, [candidate.handle, candidate.handle])
        await service.stop()

    async def test_live_callback_persists_before_worker_ingest_and_scrubs_text(self) -> None:
        await self._enable_source()
        client = FakeClient(_entity())
        inbox_was_durable = asyncio.Event()
        ingest_calls = 0

        async def fake_ingest(_settings: Settings, event: object) -> IngestResult:
            nonlocal ingest_calls
            ingest_calls += 1
            db = await connect_db(self.settings)
            try:
                cursor = await db.execute(
                    """
                    SELECT status, message_text
                    FROM reader_inbox WHERE event_id = ?
                    """,
                    (event.request_id,),
                )
                row = await cursor.fetchone()
            finally:
                await db.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "processing")
            self.assertIsNotNone(row["message_text"])
            inbox_was_durable.set()
            return IngestResult(
                result="created",
                observation_id=1,
                lead_id=None,
                decision="rejected",
                intent_score=0,
                revision=1,
                message_url="https://t.me/businessclass_rbc/42",
            )

        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            ingest=fake_ingest,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        await service.start()
        self.assertEqual(len(client.handlers), 2)
        self.assertEqual(
            {type(builder).__name__ for _, builder in client.handlers},
            {"NewMessage", "MessageEdited"},
        )

        telegram_event = SimpleNamespace(
            chat_id=CHAT_ID,
            id=42,
            raw_text="Ищу франшизу с бюджетом 5 млн",
            date=datetime.now(UTC),
            edit_date=None,
        )
        await service._on_new_message(telegram_event)
        await service._on_new_message(telegram_event)
        await asyncio.wait_for(inbox_was_durable.wait(), timeout=2)

        for _ in range(50):
            db = await connect_db(self.settings)
            try:
                cursor = await db.execute(
                    "SELECT status, message_text FROM reader_inbox"
                )
                row = await cursor.fetchone()
            finally:
                await db.close()
            if row is not None and row["status"] == "done":
                break
            await asyncio.sleep(0.01)
        assert row is not None
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["message_text"])
        self.assertEqual(ingest_calls, 1)

        await service.stop()
        self.assertGreaterEqual(client.disconnects, 1)
        self.assertFalse(hasattr(client, "log_out"))

    async def test_stop_disconnects_even_when_inbox_recovery_fails(self) -> None:
        await self._enable_source()
        client = FakeClient(_entity())
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        await service.start()
        self.assertTrue(client.connected)

        with (
            patch(
                "server.live_reader.recover_reader_inbox",
                side_effect=RuntimeError("database unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            await service.stop()

        self.assertFalse(client.connected)
        self.assertGreaterEqual(client.disconnects, 1)
        status = await service.status()
        assert status is not None
        self.assertEqual(status.state, "degraded")
        self.assertEqual(status.last_error_code, "runtimeerror")

    async def test_cancelled_stop_finishes_disconnect_and_releases_lease(self) -> None:
        await self._enable_source()
        disconnect_entered = asyncio.Event()
        allow_disconnect = asyncio.Event()

        class BlockingDisconnectClient(FakeClient):
            async def disconnect(self) -> None:
                disconnect_entered.set()
                await allow_disconnect.wait()
                await super().disconnect()

        client = BlockingDisconnectClient(_entity())
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        await service.start()

        stop_task = asyncio.create_task(service.stop())
        await asyncio.wait_for(disconnect_entered.wait(), timeout=1)
        stop_task.cancel()
        await asyncio.sleep(0.02)
        self.assertFalse(stop_task.done())
        allow_disconnect.set()
        with self.assertRaises(asyncio.CancelledError):
            await stop_task

        status = await service.status()
        assert status is not None
        self.assertEqual(status.state, "stopped")
        self.assertFalse(client.connected)
        self.assertFalse(service._lease.held)

    async def test_cancelled_late_start_clears_handlers_before_restart(self) -> None:
        await self._enable_source()
        first_client = FakeClient(_entity())
        second_client = FakeClient(_entity())
        clients = iter((first_client, second_client))
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: next(clients),
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        running_write_entered = asyncio.Event()

        async def block_running_state(
            settings: Settings,
            **kwargs: object,
        ) -> object:
            if kwargs.get("state") == "running":
                running_write_entered.set()
                await asyncio.Event().wait()
            return await repository_set_reader_runtime(settings, **kwargs)

        with patch(
            "server.live_reader.set_reader_runtime",
            side_effect=block_running_state,
        ):
            start_task = asyncio.create_task(service.start())
            await asyncio.wait_for(running_write_entered.wait(), timeout=1)
            start_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await start_task

        self.assertEqual(first_client.handlers, [])
        self.assertFalse(first_client.connected)
        self.assertFalse(service._lease.held)
        self.assertTrue(service._stop_event.is_set())
        self.assertTrue(
            service._worker_task is None or service._worker_task.done()
        )

        await service.start()
        self.assertEqual(len(second_client.handlers), 2)
        status = await service.status()
        assert status is not None
        self.assertEqual(status.state, "running")
        await service.stop()

    async def test_new_process_retries_after_old_process_releases_lease(self) -> None:
        await self._enable_source()
        first_client = FakeClient(_entity())
        second_client = FakeClient(_entity())
        session_path = self.root / "reader.session.enc"
        first = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(session_path),
            client_factory=lambda _session: first_client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        second = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(session_path),
            client_factory=lambda _session: second_client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )

        with patch("server.live_reader.SUPERVISOR_RETRY_SECONDS", 0.03):
            await first.start()
            await second.start()
            self.assertEqual(first_client.connects, 1)
            self.assertEqual(second_client.connects, 0)
            await first.stop()
            try:
                status = None
                for _ in range(50):
                    status = await second.status()
                    if (
                        second_client.connects == 1
                        and status is not None
                        and status.state == "running"
                    ):
                        break
                    await asyncio.sleep(0.02)

                self.assertEqual(second_client.connects, 1)
                assert status is not None
                self.assertEqual(status.state, "running")
            finally:
                await second.stop()

    async def test_supervisor_reconnects_after_terminal_disconnect(self) -> None:
        await self._enable_source()
        client = FakeClient(_entity())
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        with patch("server.live_reader.SUPERVISOR_RETRY_SECONDS", 0.03):
            await service.start()
            client.connected = False
            try:
                status = None
                for _ in range(60):
                    status = await service.status()
                    if (
                        client.connects >= 2
                        and status is not None
                        and status.state == "running"
                    ):
                        break
                    await asyncio.sleep(0.02)

                self.assertGreaterEqual(client.connects, 2)
                assert status is not None
                self.assertEqual(status.state, "running")
            finally:
                await service.stop()

    async def test_supervisor_retries_inbox_recovery_before_connecting(self) -> None:
        await self._enable_source()
        client = FakeClient(_entity())
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )
        recovery_calls = 0

        async def recover_with_one_failure(_settings: Settings) -> int:
            nonlocal recovery_calls
            recovery_calls += 1
            if recovery_calls == 1:
                raise RuntimeError("temporary database failure")
            return 0

        with (
            patch(
                "server.live_reader.recover_reader_inbox",
                side_effect=recover_with_one_failure,
            ),
            patch("server.live_reader.SUPERVISOR_RETRY_SECONDS", 0.03),
        ):
            await service.start()
            self.assertEqual(client.connects, 0)
            try:
                status = None
                for _ in range(60):
                    status = await service.status()
                    if (
                        client.connects == 1
                        and status is not None
                        and status.state == "running"
                    ):
                        break
                    await asyncio.sleep(0.02)
                self.assertGreaterEqual(recovery_calls, 2)
                self.assertEqual(client.connects, 1)
                assert status is not None
                self.assertEqual(status.state, "running")
            finally:
                await service.stop()

    async def test_stale_source_is_revalidated_before_handlers_attach(self) -> None:
        await self._enable_source()
        db = await connect_db(self.settings)
        try:
            await db.execute(
                "UPDATE source_verifications SET verified_at = ?",
                ((datetime.now(UTC) - timedelta(days=2)).isoformat(),),
            )
            await db.commit()
        finally:
            await db.close()
        client = FakeClient(_entity(left=True))
        service = LiveReaderService(
            app_settings=self.settings,
            server_settings=self.server_settings,
            session_store=FakeSessionStore(self.root / "reader.session.enc"),
            client_factory=lambda _session: client,
            is_channel=lambda _entity_value: True,
            peer_id=lambda _entity_value: CHAT_ID,
        )

        await service.start()
        status = await service.status()
        assert status is not None
        self.assertEqual(status.state, "degraded")
        self.assertEqual(client.entity_requests, ["businessclass_rbc"])
        self.assertEqual(client.handlers, [])
        db = await connect_db(self.settings)
        try:
            cursor = await db.execute(
                """
                SELECT s.enabled, cp.reader_status, cp.last_error_code,
                       v.source_id AS verification_id
                FROM lead_sources AS s
                LEFT JOIN source_checkpoints AS cp ON cp.source_id = s.id
                LEFT JOIN source_verifications AS v ON v.source_id = s.id
                WHERE s.telegram_chat_id = ?
                """,
                (CHAT_ID,),
            )
            source = await cursor.fetchone()
        finally:
            await db.close()
        assert source is not None
        self.assertEqual(int(source["enabled"]), 0)
        self.assertEqual(str(source["reader_status"]), "paused")
        self.assertEqual(str(source["last_error_code"]), "source_not_joined")
        self.assertIsNone(source["verification_id"])
        await service.stop()


class LiveReaderClientProfileTests(unittest.TestCase):
    def test_process_lease_allows_only_one_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "reader.lock"
            first = _ProcessLease(path)
            second = _ProcessLease(path)
            first.acquire()
            try:
                with self.assertRaises(ReaderLeaseError):
                    second.acquire()
            finally:
                first.release()
            second.acquire()
            second.release()

    def test_client_is_live_only_sequential_and_memory_backed(self) -> None:
        settings = ServerSettings(
            bot_token="123:token",
            admin_telegram_ids=frozenset({888}),
            telegram_api_id=12345,
            telegram_api_hash="a" * 32,
            telegram_expected_user_id=ACCOUNT_ID,
            session_encryption_key=Fernet.generate_key().decode("ascii"),
            encrypted_session_path=Path("reader.session.enc"),
            host="127.0.0.1",
            port=3000,
            init_data_max_age_seconds=300,
            login_challenge_ttl_seconds=300,
        )
        fake = Mock()
        fake.session = SimpleNamespace(save_entities=True)
        with patch("server.live_reader.TelegramClient", return_value=fake) as constructor:
            result = build_live_reader_client(settings, StringSession())

        self.assertIs(result, fake)
        kwargs = constructor.call_args.kwargs
        self.assertTrue(kwargs["receive_updates"])
        self.assertFalse(kwargs["catch_up"])
        self.assertTrue(kwargs["sequential_updates"])
        self.assertEqual(kwargs["flood_sleep_threshold"], 0)
        self.assertFalse(fake.session.save_entities)


if __name__ == "__main__":
    unittest.main()
