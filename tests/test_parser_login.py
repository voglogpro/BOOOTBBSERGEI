from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from cryptography.fernet import Fernet
from telethon import errors
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from server.parser_login import (
    ParserLoginError,
    ParserLoginService,
    build_web_login_client,
)
from server.session_store import EncryptedSessionStore
from server.settings import ServerSettings


class FakeLoginClient:
    def __init__(
        self,
        session: StringSession,
        *,
        user_id: int = 777,
        bot: bool = False,
        require_password: bool = False,
        send_code_error: Exception | None = None,
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.bot = bot
        self.require_password = require_password
        self.send_code_error = send_code_error
        self.connected = False
        self.disconnected = False
        self.authorized = False
        self.log_out_calls = 0
        self.sign_in_calls: list[dict[str, object]] = []

    async def connect(self) -> None:
        self.connected = True
        self.session.set_dc(2, "149.154.167.51", 443)
        self.session.auth_key = AuthKey(bytes(range(256)))

    async def disconnect(self) -> None:
        self.disconnected = True

    async def send_code_request(self, phone: str) -> object:
        if self.send_code_error is not None:
            raise self.send_code_error
        return SimpleNamespace(phone_code_hash="server-side-hash")

    async def sign_in(self, **kwargs: object) -> object:
        self.sign_in_calls.append(kwargs)
        if "code" in kwargs:
            if kwargs["code"] == "00000":
                raise errors.PhoneCodeInvalidError(request=None)
            if self.require_password:
                raise errors.SessionPasswordNeededError(request=None)
            self.authorized = True
        elif "password" in kwargs:
            if kwargs["password"] == "wrong":
                raise errors.PasswordHashInvalidError(request=None)
            self.authorized = True
        return SimpleNamespace(id=self.user_id, username="reader", bot=self.bot)

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> object:
        return SimpleNamespace(id=self.user_id, username="reader", bot=self.bot)

    async def log_out(self) -> bool:
        self.log_out_calls += 1
        self.authorized = False
        return True


class SlowCodeClient(FakeLoginClient):
    def __init__(self, session: StringSession) -> None:
        super().__init__(session)
        self.code_request_started = asyncio.Event()

    async def send_code_request(self, phone: str) -> object:
        self.code_request_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class FutureDisconnectClient(FakeLoginClient):
    def disconnect(self) -> asyncio.Future[None]:  # type: ignore[override]
        self.disconnected = True
        future = asyncio.get_running_loop().create_future()
        future.set_result(None)
        return future


def make_settings(path: Path, key: bytes, *, ttl: int = 300) -> ServerSettings:
    return ServerSettings(
        bot_token="123456:test",
        admin_telegram_ids=frozenset({111, 222}),
        telegram_api_id=123456,
        telegram_api_hash="0123456789abcdef0123456789abcdef",
        telegram_expected_user_id=777,
        session_encryption_key=key.decode("ascii"),
        encrypted_session_path=path,
        host="0.0.0.0",
        port=8080,
        init_data_max_age_seconds=300,
        login_challenge_ttl_seconds=ttl,
    )


class ParserLoginTests(unittest.IsolatedAsyncioTestCase):
    def test_real_client_is_memory_only_and_receives_no_updates(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            settings = make_settings(Path(temp_dir) / "reader.enc", key)
            client = build_web_login_client(settings, StringSession())
            try:
                self.assertFalse(client.session.save_entities)
                self.assertTrue(client._no_updates)
                self.assertFalse(client._catch_up)
                self.assertEqual(client._flood_sleep_threshold, 0)
            finally:
                client.session.close()

    async def test_code_login_saves_only_encrypted_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)
            clients: list[FakeLoginClient] = []

            def factory(session: StringSession) -> FakeLoginClient:
                client = FakeLoginClient(session)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=factory,
            )
            requested = await service.request_code(111, "+7 (999) 123-45-67")
            result = await service.confirm_code(111, requested.flow_id, "12345")

            self.assertEqual(result.state, "authorized")
            self.assertEqual(result.account_user_id, 777)
            self.assertTrue(store.metadata().exists)
            self.assertTrue(clients[0].disconnected)
            self.assertEqual(clients[0].log_out_calls, 0)
            encrypted = path.read_text(encoding="ascii")
            self.assertNotIn("+79991234567", encrypted)
            self.assertNotIn("12345", encrypted)
            self.assertNotIn("server-side-hash", encrypted)

    async def test_real_telethon_style_future_disconnect_is_supported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            clients: list[FutureDisconnectClient] = []

            def factory(session: StringSession) -> FutureDisconnectClient:
                client = FutureDisconnectClient(session)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=factory,
            )
            flow = await service.request_code(111, "+79991234567")
            result = await service.confirm_code(111, flow.flow_id, "12345")

            self.assertEqual(result.state, "authorized")
            self.assertTrue(clients[0].disconnected)

    async def test_two_factor_password_is_a_separate_step(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)
            client: FakeLoginClient | None = None

            def factory(session: StringSession) -> FakeLoginClient:
                nonlocal client
                client = FakeLoginClient(session, require_password=True)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=factory,
            )
            requested = await service.request_code(111, "+79991234567")
            code_result = await service.confirm_code(111, requested.flow_id, "12345")
            self.assertEqual(code_result.state, "password_required")
            self.assertFalse(store.metadata().exists)

            result = await service.confirm_password(
                111, requested.flow_id, "correct password"
            )
            self.assertEqual(result.state, "authorized")
            self.assertIsNotNone(client)
            assert client is not None
            self.assertEqual(client.sign_in_calls[-1], {"password": "correct password"})
            self.assertTrue(client.disconnected)

    async def test_unexpected_account_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)
            clients: list[FakeLoginClient] = []

            def factory(session: StringSession) -> FakeLoginClient:
                client = FakeLoginClient(session, user_id=888)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=factory,
            )
            requested = await service.request_code(111, "+79991234567")
            with self.assertRaisesRegex(ParserLoginError, "не в тот аккаунт"):
                await service.confirm_code(111, requested.flow_id, "12345")

            self.assertFalse(store.metadata().exists)
            self.assertTrue(clients[0].disconnected)
            self.assertEqual(clients[0].log_out_calls, 1)

    async def test_bot_account_is_never_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)
            service = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=lambda session: FakeLoginClient(session, bot=True),
            )
            requested = await service.request_code(111, "+79991234567")
            with self.assertRaises(ParserLoginError):
                await service.confirm_code(111, requested.flow_id, "12345")
            self.assertFalse(store.metadata().exists)

    async def test_flow_is_bound_to_admin(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=lambda session: FakeLoginClient(session),
            )
            requested = await service.request_code(111, "+79991234567")
            with self.assertRaisesRegex(ParserLoginError, "не найдена"):
                await service.confirm_code(222, requested.flow_id, "12345")
            self.assertEqual((await service.status(111)).state, "code_required")

    async def test_five_invalid_codes_destroy_flow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            clients: list[FakeLoginClient] = []

            def factory(session: StringSession) -> FakeLoginClient:
                client = FakeLoginClient(session)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=factory,
            )
            requested = await service.request_code(111, "+79991234567")
            for _ in range(4):
                with self.assertRaisesRegex(ParserLoginError, "не подошёл"):
                    await service.confirm_code(111, requested.flow_id, "00000")
            with self.assertRaisesRegex(ParserLoginError, "Слишком много"):
                await service.confirm_code(111, requested.flow_id, "00000")

            self.assertEqual((await service.status(111)).state, "phone_required")
            self.assertTrue(clients[0].disconnected)

    async def test_expired_flow_is_removed_and_disconnected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            clock = [1000.0]
            settings = make_settings(path, key, ttl=60)
            clients: list[FakeLoginClient] = []

            def factory(session: StringSession) -> FakeLoginClient:
                client = FakeLoginClient(session)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=factory,
                monotonic=lambda: clock[0],
            )
            await service.request_code(111, "+79991234567")
            clock[0] += 61

            self.assertEqual((await service.status(111)).state, "phone_required")
            self.assertTrue(clients[0].disconnected)

    async def test_send_code_has_local_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            clock = [1000.0]
            settings = make_settings(path, key)
            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=lambda session: FakeLoginClient(session),
                monotonic=lambda: clock[0],
            )
            requested = await service.request_code(111, "+79991234567")
            await service.cancel(111, requested.flow_id)

            with self.assertRaises(ParserLoginError) as raised:
                await service.request_code(111, "+79991234567")
            self.assertEqual(raised.exception.code, "send_code_cooldown")
            self.assertEqual(raised.exception.retry_after, 60)

    async def test_flood_wait_is_persisted_across_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)
            first = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=lambda session: FakeLoginClient(
                    session,
                    send_code_error=errors.FloodWaitError(
                        request=None,
                        capture=91,
                    ),
                ),
            )
            with self.assertRaises(ParserLoginError) as raised:
                await first.request_code(111, "+79991234567")
            self.assertEqual(raised.exception.retry_after, 91)

            restarted = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=lambda session: FakeLoginClient(session),
            )
            status = await restarted.status(111)
            self.assertEqual(status.state, "locked")
            self.assertGreater(status.retry_after or 0, 0)

    async def test_replacement_keeps_old_session_until_new_login_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            store = EncryptedSessionStore(encryption_key=key, path=path)

            initial = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=lambda session: FakeLoginClient(session),
            )
            first_flow = await initial.request_code(111, "+79991234567")
            await initial.confirm_code(111, first_flow.flow_id, "12345")
            original_ciphertext = path.read_bytes()

            replacement = ParserLoginService(
                settings=settings,
                session_store=store,
                client_factory=lambda session: FakeLoginClient(session),
            )
            new_flow = await replacement.request_code(
                111,
                "+79991234567",
                replace_existing=True,
            )
            self.assertEqual(path.read_bytes(), original_ciphertext)
            cancelled = await replacement.cancel(111, new_flow.flow_id)

            self.assertEqual(cancelled.state, "authorized")
            self.assertEqual(path.read_bytes(), original_ciphertext)

    async def test_cancelled_http_task_still_disconnects_transient_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            key = Fernet.generate_key()
            path = Path(temp_dir) / "reader.enc"
            settings = make_settings(path, key)
            clients: list[SlowCodeClient] = []

            def factory(session: StringSession) -> SlowCodeClient:
                client = SlowCodeClient(session)
                clients.append(client)
                return client

            service = ParserLoginService(
                settings=settings,
                session_store=EncryptedSessionStore(encryption_key=key, path=path),
                client_factory=factory,
            )
            task = asyncio.create_task(
                service.request_code(111, "+79991234567")
            )
            while not clients:
                await asyncio.sleep(0)
            await clients[0].code_request_started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            self.assertTrue(clients[0].disconnected)
            self.assertEqual((await service.status(111)).state, "phone_required")


if __name__ == "__main__":
    unittest.main()
