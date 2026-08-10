from __future__ import annotations

import asyncio
import re
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable

from telethon import TelegramClient, errors
from telethon.sessions import StringSession

from reader.cooldown import (
    TelegramCooldownError,
    cooldown_remaining_seconds,
    record_cooldown,
)
from reader.identity import TelegramAuthorizationError, account_identity_from_user
from server.session_store import EncryptedSessionStore, SessionStoreError
from server.settings import ServerSettings


PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
CODE_RE = re.compile(r"^[0-9]{5,8}$")
MAX_SECRET_LENGTH = 256
MAX_LOGIN_ATTEMPTS = 5
SEND_CODE_COOLDOWN_SECONDS = 60
SEND_CODE_WINDOW_SECONDS = 3600
MAX_SEND_CODE_PER_WINDOW = 3
TELEGRAM_OPERATION_TIMEOUT_SECONDS = 45
CLEANUP_TIMEOUT_SECONDS = 10


class ParserLoginError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 400,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message
        self.http_status = http_status
        self.retry_after = retry_after


@dataclass(frozen=True, slots=True)
class LoginStatus:
    state: str
    flow_id: str | None = None
    account_user_id: int | None = None
    retry_after: int | None = None
    message: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"state": self.state}
        if self.flow_id is not None:
            payload["flow_id"] = self.flow_id
        if self.account_user_id is not None:
            payload["account_user_id"] = self.account_user_id
        if self.retry_after is not None:
            payload["retry_after"] = self.retry_after
        if self.message is not None:
            payload["message"] = self.message
        return payload


@dataclass(slots=True)
class _LoginFlow:
    flow_id: str
    admin_user_id: int
    client: Any
    phone: str
    phone_code_hash: str
    state: str
    expires_at: float
    attempts: int = 0


ClientFactory = Callable[[StringSession], Any]


def build_web_login_client(settings: ServerSettings, session: StringSession) -> Any:
    """Build a no-updates client whose session never touches plaintext disk."""
    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        receive_updates=False,
        catch_up=False,
        sequential_updates=True,
        flood_sleep_threshold=0,
        request_retries=3,
        connection_retries=3,
        auto_reconnect=False,
        device_model="BibiBike Lead Reader",
        app_version="0.3",
        lang_code="ru",
        system_lang_code="ru",
    )
    client.session.save_entities = False
    return client


def normalize_phone(raw: object) -> str:
    if not isinstance(raw, str):
        raise ParserLoginError("invalid_phone", "Введите номер телефона.")
    phone = re.sub(r"[\s()\-]", "", raw.strip())
    if not PHONE_RE.fullmatch(phone):
        raise ParserLoginError(
            "invalid_phone",
            "Введите номер в международном формате, например +79991234567.",
        )
    return phone


class ParserLoginService:
    """One-worker, admin-bound Telethon login flow for the BotHost Mini App."""

    def __init__(
        self,
        *,
        settings: ServerSettings,
        session_store: EncryptedSessionStore,
        client_factory: ClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._store = session_store
        self._client_factory = client_factory or (
            lambda session: build_web_login_client(settings, session)
        )
        self._monotonic = monotonic
        self._lock = asyncio.Lock()
        self._flow: _LoginFlow | None = None
        self._send_history: dict[int, deque[float]] = defaultdict(deque)
        self._flood_retry_until = 0.0
        self._cooldown_write_failed = False
        self._cooldown_path = self._store.path.with_name(
            f"{self._store.path.name}.login-cooldown.json"
        )

    async def status(self, admin_user_id: int) -> LoginStatus:
        async with self._lock:
            await self._expire_flow_locked()
            if self._flow is not None:
                if self._flow.admin_user_id != admin_user_id:
                    return LoginStatus(state="locked")
                return LoginStatus(
                    state=self._flow.state,
                    flow_id=self._flow.flow_id,
                )
            stored = self._stored_session_state()
            if stored == "authorized":
                return LoginStatus(state="authorized")
            if stored == "locked":
                return LoginStatus(
                    state="locked",
                    message=(
                        "Сохранённая сессия не открывается. Проверьте ключ "
                        "шифрования в BotHost; не перезаписывайте файл вслепую."
                    ),
                )
            cooldown = self._login_cooldown_remaining()
            if cooldown > 0:
                return LoginStatus(
                    state="locked",
                    retry_after=cooldown,
                    message=(
                        "Telegram ограничил попытки входа. Сервис сохранит "
                        "паузу даже после перезапуска."
                    ),
                )
            if self._flow is None:
                return LoginStatus(state="phone_required")
            raise AssertionError("unreachable flow state")

    async def request_code(
        self,
        admin_user_id: int,
        raw_phone: object,
        *,
        replace_existing: object = False,
    ) -> LoginStatus:
        phone = normalize_phone(raw_phone)
        if not isinstance(replace_existing, bool):
            raise ParserLoginError(
                "invalid_replace_flag",
                "Некорректный режим переподключения.",
            )
        async with self._lock:
            await self._expire_flow_locked()
            stored = self._stored_session_state()
            if stored == "authorized" and not replace_existing:
                raise ParserLoginError(
                    "already_authorized",
                    "Аккаунт парсера уже подключён.",
                    http_status=409,
                )
            if stored == "locked":
                raise ParserLoginError(
                    "session_locked",
                    "Сохранённая сессия не открывается. Проверьте ключ шифрования.",
                    http_status=409,
                )
            if self._flow is not None:
                raise ParserLoginError(
                    "login_in_progress",
                    "Вход уже начат. Завершите или отмените текущую попытку.",
                    http_status=409,
                )

            self._enforce_send_limit(admin_user_id)
            self._send_history[admin_user_id].append(self._monotonic())
            client = self._client_factory(StringSession())
            try:
                async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                    await client.connect()
                    sent_code = await client.send_code_request(phone)
                phone_code_hash = getattr(sent_code, "phone_code_hash", None)
                if not isinstance(phone_code_hash, str) or not phone_code_hash:
                    raise ParserLoginError(
                        "telegram_unavailable",
                        "Telegram не вернул данные для подтверждения входа.",
                        http_status=502,
                    )
            except (errors.PhoneNumberInvalidError, errors.PhoneNumberUnoccupiedError):
                await self._disconnect_quietly(client)
                raise ParserLoginError(
                    "phone_rejected",
                    "Telegram не принял этот номер телефона.",
                ) from None
            except errors.PhoneNumberBannedError:
                await self._disconnect_quietly(client)
                raise ParserLoginError(
                    "phone_banned",
                    "Telegram ограничил вход для этого номера.",
                    http_status=403,
                ) from None
            except errors.FloodError as exc:
                await self._disconnect_quietly(client)
                raise self._flood_error(exc) from None
            except asyncio.CancelledError:
                await self._disconnect_quietly(client)
                raise
            except ParserLoginError:
                await self._disconnect_quietly(client)
                raise
            except errors.RPCError:
                await self._disconnect_quietly(client)
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Telegram временно отклонил запрос входа.",
                    http_status=502,
                ) from None
            except Exception:
                await self._disconnect_quietly(client)
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Не удалось связаться с Telegram.",
                    http_status=502,
                ) from None

            flow_id = secrets.token_urlsafe(32)
            self._flow = _LoginFlow(
                flow_id=flow_id,
                admin_user_id=admin_user_id,
                client=client,
                phone=phone,
                phone_code_hash=phone_code_hash,
                state="code_required",
                expires_at=(
                    self._monotonic()
                    + self._settings.login_challenge_ttl_seconds
                ),
            )
            return LoginStatus(state="code_required", flow_id=flow_id)

    async def confirm_code(
        self,
        admin_user_id: int,
        flow_id: object,
        raw_code: object,
    ) -> LoginStatus:
        if not isinstance(raw_code, str) or not CODE_RE.fullmatch(raw_code.strip()):
            raise ParserLoginError(
                "invalid_code",
                "Введите код из Telegram только цифрами.",
            )
        code = raw_code.strip()
        async with self._lock:
            await self._expire_flow_locked()
            flow = self._require_flow(admin_user_id, flow_id, "code_required")
            try:
                async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                    signed_in_user = await flow.client.sign_in(
                        phone=flow.phone,
                        code=code,
                        phone_code_hash=flow.phone_code_hash,
                    )
            except errors.SessionPasswordNeededError:
                flow.state = "password_required"
                flow.phone = ""
                flow.phone_code_hash = ""
                flow.attempts = 0
                return LoginStatus(state="password_required", flow_id=flow.flow_id)
            except (errors.PhoneCodeInvalidError, errors.PhoneCodeEmptyError):
                await self._record_failed_attempt_locked(flow)
                raise ParserLoginError(
                    "invalid_code",
                    "Код не подошёл. Проверьте его и попробуйте ещё раз.",
                ) from None
            except (errors.PhoneCodeExpiredError, errors.PhoneCodeHashEmptyError):
                await self._destroy_flow_locked()
                raise ParserLoginError(
                    "code_expired",
                    "Код истёк. Начните вход заново.",
                ) from None
            except errors.FloodError as exc:
                await self._destroy_flow_locked()
                raise self._flood_error(exc) from None
            except asyncio.CancelledError:
                await self._reject_authorized_flow_locked()
                raise
            except errors.RPCError:
                await self._reject_authorized_flow_locked()
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Telegram отклонил подтверждение входа.",
                    http_status=502,
                ) from None
            except Exception:
                await self._reject_authorized_flow_locked()
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Не удалось подтвердить вход в Telegram.",
                    http_status=502,
                ) from None
            return await self._finish_login_locked(flow, signed_in_user)

    async def confirm_password(
        self,
        admin_user_id: int,
        flow_id: object,
        password: object,
    ) -> LoginStatus:
        if (
            not isinstance(password, str)
            or not password
            or len(password) > MAX_SECRET_LENGTH
        ):
            raise ParserLoginError(
                "invalid_password",
                "Введите пароль двухэтапной аутентификации.",
            )
        async with self._lock:
            await self._expire_flow_locked()
            flow = self._require_flow(admin_user_id, flow_id, "password_required")
            try:
                async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                    signed_in_user = await flow.client.sign_in(password=password)
            except errors.PasswordHashInvalidError:
                await self._record_failed_attempt_locked(flow)
                raise ParserLoginError(
                    "invalid_password",
                    "Пароль не подошёл. Попробуйте ещё раз.",
                ) from None
            except errors.FloodError as exc:
                await self._destroy_flow_locked()
                raise self._flood_error(exc) from None
            except asyncio.CancelledError:
                await self._reject_authorized_flow_locked()
                raise
            except errors.RPCError:
                await self._reject_authorized_flow_locked()
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Telegram отклонил пароль входа.",
                    http_status=502,
                ) from None
            except Exception:
                await self._reject_authorized_flow_locked()
                raise ParserLoginError(
                    "telegram_unavailable",
                    "Не удалось подтвердить пароль Telegram.",
                    http_status=502,
                ) from None
            return await self._finish_login_locked(flow, signed_in_user)

    async def cancel(self, admin_user_id: int, flow_id: object) -> LoginStatus:
        async with self._lock:
            await self._expire_flow_locked()
            self._require_flow(admin_user_id, flow_id)
            await self._destroy_flow_locked()
            stored = self._stored_session_state()
            if stored == "authorized":
                return LoginStatus(state="authorized")
            if stored == "locked":
                return LoginStatus(
                    state="locked",
                    message="Сохранённая сессия не открывается.",
                )
            return LoginStatus(state="phone_required")

    async def close(self) -> None:
        async with self._lock:
            await self._destroy_flow_locked()

    def _stored_session_state(self) -> str:
        try:
            metadata = self._store.metadata()
            if not metadata.exists:
                return "missing"
            self._store.load()
            return "authorized"
        except SessionStoreError:
            return "locked"

    def _enforce_send_limit(self, admin_user_id: int) -> None:
        now = self._monotonic()
        persistent_remaining = self._login_cooldown_remaining()
        in_memory_remaining = max(0, int(self._flood_retry_until - now + 0.999))
        retry_after = max(persistent_remaining, in_memory_remaining)
        if self._cooldown_write_failed or retry_after > 0:
            raise ParserLoginError(
                "telegram_rate_limited",
                "Telegram временно ограничил попытки входа. Дождитесь окончания паузы.",
                http_status=429,
                retry_after=retry_after or 3600,
            )
        history = self._send_history[admin_user_id]
        while history and now - history[0] >= SEND_CODE_WINDOW_SECONDS:
            history.popleft()
        if history:
            cooldown_left = SEND_CODE_COOLDOWN_SECONDS - (now - history[-1])
            if cooldown_left > 0:
                retry_after = max(1, int(cooldown_left + 0.999))
                raise ParserLoginError(
                    "send_code_cooldown",
                    "Код уже запрошен. Подождите перед новой попыткой.",
                    http_status=429,
                    retry_after=retry_after,
                )
        if len(history) >= MAX_SEND_CODE_PER_WINDOW:
            retry_after = max(
                1,
                int(SEND_CODE_WINDOW_SECONDS - (now - history[0]) + 0.999),
            )
            raise ParserLoginError(
                "send_code_limit",
                "Слишком много запросов кода. Попробуйте позже.",
                http_status=429,
                retry_after=retry_after,
            )

    def _require_flow(
        self,
        admin_user_id: int,
        flow_id: object,
        required_state: str | None = None,
    ) -> _LoginFlow:
        if (
            self._flow is None
            or not isinstance(flow_id, str)
            or not secrets.compare_digest(self._flow.flow_id, flow_id)
            or self._flow.admin_user_id != admin_user_id
        ):
            raise ParserLoginError(
                "invalid_flow",
                "Попытка входа не найдена. Начните заново.",
            )
        if required_state is not None and self._flow.state != required_state:
            raise ParserLoginError(
                "invalid_flow_state",
                "Этот шаг входа уже завершён. Обновите экран.",
                http_status=409,
            )
        return self._flow

    async def _record_failed_attempt_locked(self, flow: _LoginFlow) -> None:
        flow.attempts += 1
        if flow.attempts >= MAX_LOGIN_ATTEMPTS:
            await self._destroy_flow_locked()
            raise ParserLoginError(
                "attempts_exceeded",
                "Слишком много неверных попыток. Начните вход заново.",
                http_status=429,
                retry_after=SEND_CODE_COOLDOWN_SECONDS,
            )

    async def _finish_login_locked(
        self,
        flow: _LoginFlow,
        signed_in_user: Any,
    ) -> LoginStatus:
        try:
            identity = account_identity_from_user(
                signed_in_user,
                expected_user_id=self._settings.telegram_expected_user_id,
            )
            if not isinstance(flow.client.session, StringSession):
                raise ParserLoginError(
                    "invalid_session",
                    "Telegram создал неподдерживаемый формат сессии.",
                    http_status=500,
                )
            self._store.save(flow.client.session)
        except asyncio.CancelledError:
            await self._reject_authorized_flow_locked()
            raise
        except TelegramAuthorizationError:
            await self._reject_authorized_flow_locked()
            raise ParserLoginError(
                "unexpected_account",
                "Выполнен вход не в тот аккаунт парсера.",
                http_status=403,
            ) from None
        except SessionStoreError:
            await self._reject_authorized_flow_locked()
            raise ParserLoginError(
                "session_store_failed",
                "Не удалось безопасно сохранить сессию на сервере.",
                http_status=500,
            ) from None
        except ParserLoginError:
            await self._reject_authorized_flow_locked()
            raise
        except Exception:
            await self._reject_authorized_flow_locked()
            raise ParserLoginError(
                "telegram_unavailable",
                "Не удалось проверить подключённый аккаунт.",
                http_status=502,
            ) from None

        self._flow = None
        await self._disconnect_quietly(flow.client)
        flow.phone = ""
        flow.phone_code_hash = ""
        return LoginStatus(state="authorized", account_user_id=identity.user_id)

    async def _expire_flow_locked(self) -> None:
        if self._flow is not None and self._monotonic() >= self._flow.expires_at:
            await self._destroy_flow_locked()

    async def _destroy_flow_locked(self) -> None:
        flow = self._flow
        self._flow = None
        if flow is None:
            return
        flow.phone = ""
        flow.phone_code_hash = ""
        await self._disconnect_quietly(flow.client)

    async def _reject_authorized_flow_locked(self) -> None:
        flow = self._flow
        self._flow = None
        if flow is None:
            return
        flow.phone = ""
        flow.phone_code_hash = ""
        await self._revoke_quietly(flow.client)
        await self._disconnect_quietly(flow.client)

    @staticmethod
    async def _disconnect_quietly(client: Any) -> None:
        await ParserLoginService._run_cleanup(client.disconnect())

    @staticmethod
    async def _revoke_quietly(client: Any) -> None:
        # This is only for a newly-created login that failed identity/storage
        # checks. A persisted or accepted reader session is never logged out.
        try:
            operation = client.log_out()
        except Exception:
            return
        await ParserLoginService._run_cleanup(operation)

    @staticmethod
    async def _run_cleanup(operation: Any) -> None:
        # Telethon 1.44 disconnect() may return an asyncio Future rather than
        # a coroutine while its event loop is running.
        task = asyncio.ensure_future(operation)
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=CLEANUP_TIMEOUT_SECONDS,
            )
        except asyncio.CancelledError:
            task.add_done_callback(ParserLoginService._consume_task_result)
        except Exception:
            if not task.done():
                task.cancel()
            task.add_done_callback(ParserLoginService._consume_task_result)

    @staticmethod
    def _consume_task_result(task: asyncio.Future[Any]) -> None:
        if task.cancelled():
            return
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass

    def _login_cooldown_remaining(self) -> int:
        try:
            return cooldown_remaining_seconds(self._cooldown_path)
        except TelegramCooldownError:
            self._cooldown_write_failed = True
            raise ParserLoginError(
                "cooldown_locked",
                "Файл паузы Telegram повреждён. Проверьте серверное хранилище.",
                http_status=429,
                retry_after=3600,
            ) from None

    def _flood_error(self, exc: errors.FloodError) -> ParserLoginError:
        seconds = getattr(exc, "seconds", None)
        retry_after = int(seconds) if isinstance(seconds, int) and seconds > 0 else 3600
        self._flood_retry_until = max(
            self._flood_retry_until,
            self._monotonic() + retry_after,
        )
        try:
            record_cooldown(self._cooldown_path, seconds=retry_after)
        except (OSError, ValueError):
            self._cooldown_write_failed = True
        return ParserLoginError(
            "telegram_rate_limited",
            "Telegram временно ограничил попытки входа. Дождитесь окончания паузы.",
            http_status=429,
            retry_after=retry_after,
        )
