from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon import TelegramClient, errors, events, types, utils
from telethon.sessions import StringSession

from app.config import Settings
from app.db import init_db
from app.ingest import (
    CollectorDisabled,
    IdempotencyConflict,
    IngestionError,
    SourceNotAllowed,
    ingest_public_message,
)
from app.models import IngestResult, PublicMessageEvent
from app.repository import (
    ReaderRuntimeSnapshot,
    claim_next_reader_event,
    complete_reader_event,
    enqueue_reader_event,
    fail_closed_invalid_enabled_sources,
    fail_reader_event,
    get_reader_runtime,
    list_enabled_reader_sources,
    purge_completed_reader_events,
    purge_expired_rejections,
    recover_reader_inbox,
    refresh_source_verification,
    revoke_reader_source_verification,
    set_reader_runtime,
    set_reader_source_statuses,
    touch_reader_heartbeat,
)
from app.source_candidates import SourceCandidate, load_candidate_catalog
from reader.cooldown import (
    TelegramCooldownError,
    cooldown_remaining_seconds,
    record_cooldown,
)
from reader.identity import TelegramAuthorizationError, verify_authorized_identity
from server.session_store import (
    EncryptedSessionStore,
    SessionNotFoundError,
    SessionStoreError,
)
from server.settings import ServerSettings
from server.source_service import ResolvedReviewedSource, SourceResolutionError


LOGGER = logging.getLogger(__name__)
TELEGRAM_OPERATION_TIMEOUT_SECONDS = 45
WORKER_IDLE_SECONDS = 1.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
INBOX_PRUNE_INTERVAL_SECONDS = 60 * 60.0
MAX_DELIVERY_ATTEMPTS = 5
SUPERVISOR_RETRY_SECONDS = 15.0
FULL_REFRESH_SECONDS = 300.0
VERIFICATION_RECHECK_SECONDS = 24 * 60 * 60
PERMANENT_SOURCE_VALIDATION_ERRORS = frozenset(
    {
        "invalid_chat_id",
        "source_identity_changed",
        "source_not_joined",
        "source_not_public",
        "source_not_reviewed",
        "source_unavailable",
        "unsupported_forum",
        "unsupported_source_type",
        "username_mismatch",
    }
)


class ReaderLeaseError(RuntimeError):
    """Another process currently owns the encrypted Telegram session."""


class ReaderDisconnectError(RuntimeError):
    """The existing Telethon client could not be confirmed disconnected."""


class _ProcessLease:
    """Cross-platform advisory lock held while a user session is connected."""

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self._path = os.fspath(path)
        self._file: Any | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        if self._file is not None:
            return
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            raise ReaderLeaseError("reader session lease is unavailable") from exc
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    handle.write(b"\0")
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError) as exc:
            handle.close()
            raise ReaderLeaseError(
                "reader session is already owned by another process"
            ) from exc
        self._file = handle

    def release(self) -> None:
        handle = self._file
        self._file = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            handle.close()


ClientFactory = Callable[[StringSession], Any]
IngestFunction = Callable[
    [Settings, PublicMessageEvent],
    Awaitable[IngestResult],
]


def build_live_reader_client(
    settings: ServerSettings,
    session: StringSession,
) -> TelegramClient:
    """Build the only live Telethon client; the session never touches plaintext disk."""

    client = TelegramClient(
        session,
        settings.telegram_api_id,
        settings.telegram_api_hash,
        receive_updates=True,
        catch_up=False,
        sequential_updates=True,
        flood_sleep_threshold=0,
        request_retries=5,
        connection_retries=5,
        auto_reconnect=True,
        device_model="BibiBike Lead Reader",
        app_version="0.4",
        lang_code="ru",
        system_lang_code="ru",
    )
    client.session.save_entities = False
    return client


def _canonical_event_time(value: object, *, field: str) -> str:
    if not isinstance(value, datetime):
        raise ValueError(f"Telegram event {field} is missing")
    if value.tzinfo is None:
        raise ValueError(f"Telegram event {field} has no timezone")
    return (
        value.astimezone(UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


class LiveReaderService:
    """Single-session, allowlist-only Telethon runtime with a durable inbox."""

    def __init__(
        self,
        *,
        app_settings: Settings,
        server_settings: ServerSettings,
        session_store: EncryptedSessionStore | None = None,
        client_factory: ClientFactory | None = None,
        ingest: IngestFunction = ingest_public_message,
        is_channel: Callable[[Any], bool] | None = None,
        peer_id: Callable[[Any], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app_settings = app_settings
        self._server_settings = server_settings
        self._session_store = session_store or EncryptedSessionStore(
            encryption_key=server_settings.session_encryption_key,
            path=server_settings.encrypted_session_path,
        )
        self._client_factory = client_factory or (
            lambda session: build_live_reader_client(server_settings, session)
        )
        self._ingest = ingest
        self._is_channel = is_channel or (
            lambda entity: isinstance(entity, types.Channel)
        )
        self._peer_id = peer_id or utils.get_peer_id
        self._monotonic = monotonic
        reviewed_catalog = load_candidate_catalog()
        self._reviewed_candidates = {
            candidate.handle.casefold(): candidate
            for candidate in reviewed_catalog.candidates
            if candidate.public_preview_verified
        }
        self._reviewed_handles = frozenset(self._reviewed_candidates)
        self._cooldown_path = self._session_store.path.with_name(
            f"{self._session_store.path.name}.reader-cooldown.json"
        )
        self._lease = _ProcessLease(
            self._session_store.path.with_name(
                f"{self._session_store.path.name}.reader.lock"
            )
        )

        self._lifecycle_lock = asyncio.Lock()
        self._client: Any | None = None
        self._client_connected = False
        self._account_user_id: int | None = None
        self._handlers: list[tuple[Any, Any]] = []
        self._active_chat_ids: frozenset[int] = frozenset()
        self._active_source_ids: frozenset[int] = frozenset()
        self._worker_task: asyncio.Task[None] | None = None
        self._supervisor_task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._maintenance_tasks: set[asyncio.Task[None]] = set()
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._started = False
        self._last_heartbeat = 0.0
        self._last_inbox_prune = 0.0
        self._last_full_refresh = 0.0
        self._generation = 0
        self._needs_retry = False
        self._retry_error_code: str | None = None
        self._inbox_recovered = False

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._started:
                return
            await init_db(self._app_settings)
            self._stop_event.clear()
            self._started = True
            self._generation += 1
            self._needs_retry = False
            self._retry_error_code = None
            try:
                self._lease.acquire()
                self._inbox_recovered = False
                await recover_reader_inbox(self._app_settings)
                self._inbox_recovered = True
                await purge_completed_reader_events(self._app_settings)
                await purge_expired_rejections(self._app_settings)
                self._last_inbox_prune = self._monotonic()
                await set_reader_runtime(
                    self._app_settings,
                    state="starting",
                    account_user_id=None,
                )
                await self._refresh_locked()
            except asyncio.CancelledError as cancellation:
                try:
                    await self._abort_cancelled_start_locked()
                except Exception as cleanup_error:
                    raise cleanup_error from cancellation
                raise
            except Exception as exc:
                await self._degrade_locked(self._safe_error_code(exc))
            self._ensure_supervisor_locked()

    async def _abort_cancelled_start_locked(self) -> None:
        """Clear every partial runtime artifact left by a cancelled start."""

        self._started = False
        self._generation += 1
        self._needs_retry = False
        self._retry_error_code = None
        self._stop_event.set()
        self._wake_event.set()
        active_source_ids = self._active_source_ids
        await self._remove_handlers_locked()
        worker = self._worker_task
        self._worker_task = None
        supervisor = self._supervisor_task
        self._supervisor_task = None
        maintenance = tuple(self._maintenance_tasks)
        self._maintenance_tasks.clear()
        for task in (supervisor, worker, *maintenance):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.warning(
                    "Partial reader task stopped with %s",
                    type(exc).__name__,
                )

        if not self._lease.held:
            return
        disconnected = False
        try:
            try:
                await recover_reader_inbox(self._app_settings)
                self._inbox_recovered = True
            finally:
                await self._disconnect_locked(release_lease=False)
                disconnected = True
            await set_reader_source_statuses(
                self._app_settings,
                source_ids=active_source_ids,
                status="paused",
            )
            await set_reader_runtime(
                self._app_settings,
                state="stopped",
                account_user_id=self._account_user_id,
            )
        finally:
            if disconnected:
                self._lease.release()
                self._inbox_recovered = False

    async def stop(self) -> None:
        """Finish teardown even if the caller/request is cancelled midway."""

        task = self._stop_task
        if task is None or task.done():
            task = asyncio.create_task(
                self._stop_impl(),
                name="bb-bike-reader-stop",
            )
            self._stop_task = task

        cancellation_received = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancellation_received = True
                continue

        try:
            task.result()
        finally:
            if self._stop_task is task:
                self._stop_task = None
        if cancellation_received:
            raise asyncio.CancelledError

    async def _stop_impl(self) -> None:
        async with self._lifecycle_lock:
            owned_lease = self._lease.held
            self._started = False
            self._generation += 1
            self._needs_retry = False
            self._retry_error_code = None
            self._stop_event.set()
            self._wake_event.set()
            active_source_ids = self._active_source_ids
            await self._remove_handlers_locked()
            worker = self._worker_task
            self._worker_task = None
            supervisor = self._supervisor_task
            self._supervisor_task = None
            maintenance = tuple(self._maintenance_tasks)
            self._maintenance_tasks.clear()

        for task in (supervisor, worker, *maintenance):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                LOGGER.warning(
                    "Live reader background task stopped with %s",
                    type(exc).__name__,
                )

        if not owned_lease:
            return

        async with self._lifecycle_lock:
            disconnected = False
            try:
                try:
                    await recover_reader_inbox(self._app_settings)
                    self._inbox_recovered = True
                finally:
                    await self._disconnect_locked(release_lease=False)
                    disconnected = True
                await set_reader_source_statuses(
                    self._app_settings,
                    source_ids=active_source_ids,
                    status="paused",
                )
                await set_reader_runtime(
                    self._app_settings,
                    state="stopped",
                    account_user_id=self._account_user_id,
                )
            except ReaderDisconnectError:
                await set_reader_source_statuses(
                    self._app_settings,
                    source_ids=active_source_ids,
                    status="degraded",
                    error_code="disconnect_failed",
                )
                await set_reader_runtime(
                    self._app_settings,
                    state="degraded",
                    account_user_id=self._account_user_id,
                    error_code="disconnect_failed",
                )
                raise
            except Exception as exc:
                error_code = self._safe_error_code(exc)
                try:
                    await set_reader_source_statuses(
                        self._app_settings,
                        source_ids=active_source_ids,
                        status="degraded",
                        error_code=error_code,
                    )
                    await set_reader_runtime(
                        self._app_settings,
                        state="degraded",
                        account_user_id=self._account_user_id,
                        error_code=error_code,
                    )
                except Exception as status_error:
                    LOGGER.warning(
                        "Could not persist failed reader stop: %s",
                        type(status_error).__name__,
                    )
                raise
            finally:
                if disconnected:
                    self._lease.release()
                    self._inbox_recovered = False

    async def status(self) -> ReaderRuntimeSnapshot | None:
        return await get_reader_runtime(self._app_settings)

    async def refresh_allowlist(self) -> None:
        async with self._lifecycle_lock:
            if not self._started:
                return
            try:
                await self._refresh_locked()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._degrade_locked(self._safe_error_code(exc))

    async def resolve_reviewed_source(
        self,
        candidate: SourceCandidate,
    ) -> ResolvedReviewedSource:
        async with self._lifecycle_lock:
            if not self._started:
                raise SourceResolutionError(
                    "reader_not_running",
                    "Telegram Reader временно остановлен.",
                    http_status=409,
                )
            cooldown = self._cooldown_remaining()
            if cooldown > 0:
                raise SourceResolutionError(
                    "telegram_rate_limited",
                    "Telegram временно ограничил проверку источников.",
                    http_status=429,
                    retry_after=cooldown,
                )
            try:
                await self._ensure_connected_locked()
                assert self._client is not None
                async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                    entity = await self._client.get_entity(candidate.handle)
                resolved = self._validate_resolved_entity(candidate, entity)
            except errors.FloodError as exc:
                retry_after = self._record_flood_wait(exc)
                raise SourceResolutionError(
                    "telegram_rate_limited",
                    "Telegram временно ограничил проверку источников.",
                    http_status=429,
                    retry_after=retry_after,
                ) from None
            except SourceResolutionError:
                raise
            except (SessionStoreError, TelegramAuthorizationError) as exc:
                raise SourceResolutionError(
                    "session_unavailable",
                    "Сессия Telegram Reader недоступна или больше не авторизована.",
                    http_status=409,
                ) from exc
            except ReaderLeaseError as exc:
                raise SourceResolutionError(
                    "reader_session_busy",
                    "Сессия Telegram Reader уже используется другим процессом.",
                    http_status=409,
                ) from exc
            except (errors.RPCError, TimeoutError, OSError, ValueError, TypeError) as exc:
                raise SourceResolutionError(
                    "telegram_unavailable",
                    "Не удалось проверить источник через Telegram.",
                    http_status=502,
                ) from exc
            finally:
                if not self._handlers:
                    try:
                        await self._disconnect_locked()
                    except ReaderDisconnectError as exc:
                        raise SourceResolutionError(
                            "telegram_disconnect_failed",
                            "Не удалось безопасно завершить проверку Telegram.",
                            http_status=503,
                        ) from exc
            return resolved

    async def _refresh_locked(self) -> None:
        if not self._lease.held:
            self._lease.acquire()
            self._inbox_recovered = False
        if not self._inbox_recovered:
            await recover_reader_inbox(self._app_settings)
            self._inbox_recovered = True
        await fail_closed_invalid_enabled_sources(
            self._app_settings,
            expected_account_user_id=(
                self._server_settings.telegram_expected_user_id
            ),
            allowed_public_handles=self._reviewed_handles,
        )
        sources = await list_enabled_reader_sources(
            self._app_settings,
            expected_account_user_id=(
                self._server_settings.telegram_expected_user_id
            ),
        )
        if not self._app_settings.collector_enabled:
            await self._remove_handlers_locked()
            disconnected = False
            try:
                await self._disconnect_locked(release_lease=False)
                disconnected = True
                await set_reader_source_statuses(
                    self._app_settings,
                    source_ids=(source.source_id for source in sources),
                    status="paused",
                )
                await set_reader_runtime(
                    self._app_settings,
                    state="paused",
                    account_user_id=self._account_user_id,
                    error_code="collector_disabled",
                )
                self._needs_retry = False
                self._retry_error_code = None
            finally:
                if disconnected:
                    self._lease.release()
                    self._inbox_recovered = False
            return
        if not sources:
            await self._remove_handlers_locked()
            disconnected = False
            try:
                await self._disconnect_locked(release_lease=False)
                disconnected = True
                await set_reader_runtime(
                    self._app_settings,
                    state="paused",
                    account_user_id=self._account_user_id,
                    active_source_count=0,
                )
                self._needs_retry = False
                self._retry_error_code = None
            finally:
                if disconnected:
                    self._lease.release()
                    self._inbox_recovered = False
            return

        await self._ensure_connected_locked()
        assert self._client is not None
        await self._revalidate_stale_sources_locked(sources)
        await self._replace_handlers_locked(
            tuple(source.telegram_chat_id for source in sources)
        )
        self._active_source_ids = frozenset(source.source_id for source in sources)
        await set_reader_source_statuses(
            self._app_settings,
            source_ids=self._active_source_ids,
            status="ok",
        )
        self._ensure_worker_locked()
        await set_reader_runtime(
            self._app_settings,
            state="running",
            account_user_id=self._account_user_id,
            active_source_count=len(sources),
            connected=True,
        )
        self._needs_retry = False
        self._retry_error_code = None
        self._last_full_refresh = self._monotonic()

    async def _revalidate_stale_sources_locked(
        self,
        sources: tuple[Any, ...],
    ) -> None:
        assert self._client is not None
        now = datetime.now(UTC)
        for source in sources:
            try:
                verified_at = datetime.fromisoformat(
                    source.verified_at.replace("Z", "+00:00")
                )
            except (AttributeError, ValueError):
                verified_at = datetime.min.replace(tzinfo=UTC)
            if verified_at.tzinfo is None:
                verified_at = verified_at.replace(tzinfo=UTC)
            if (
                now - verified_at.astimezone(UTC)
                < timedelta(seconds=VERIFICATION_RECHECK_SECONDS)
            ):
                continue
            try:
                candidate = self._reviewed_candidates.get(
                    source.public_handle.casefold()
                )
                if candidate is None:
                    raise SourceResolutionError(
                        "source_not_reviewed",
                        "Источник больше не входит в проверенный каталог.",
                        http_status=409,
                    )
                async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                    entity = await self._client.get_entity(source.public_handle)
            except errors.FloodError as exc:
                retry_after = self._record_flood_wait(exc)
                raise SourceResolutionError(
                    "telegram_rate_limited",
                    "Telegram временно ограничил повторную проверку источников.",
                    http_status=429,
                    retry_after=retry_after,
                ) from None
            except SourceResolutionError as exc:
                if exc.code in PERMANENT_SOURCE_VALIDATION_ERRORS:
                    await revoke_reader_source_verification(
                        self._app_settings,
                        source_id=source.source_id,
                        error_code=exc.code,
                    )
                raise
            try:
                resolved = self._validate_resolved_entity(candidate, entity)
                if (
                    resolved.telegram_chat_id != source.telegram_chat_id
                    or resolved.source_kind != source.source_kind
                ):
                    raise SourceResolutionError(
                        "source_identity_changed",
                        "Telegram-источник изменился и требует ручной проверки.",
                        http_status=409,
                    )
            except SourceResolutionError as exc:
                if exc.code in PERMANENT_SOURCE_VALIDATION_ERRORS:
                    await revoke_reader_source_verification(
                        self._app_settings,
                        source_id=source.source_id,
                        error_code=exc.code,
                    )
                raise
            await refresh_source_verification(
                self._app_settings,
                source_id=source.source_id,
                telegram_chat_id=source.telegram_chat_id,
                public_handle=source.public_handle,
                expected_account_user_id=(
                    self._server_settings.telegram_expected_user_id
                ),
                checked_at=resolved.checked_at,
            )

    async def _ensure_connected_locked(self) -> None:
        cooldown = self._cooldown_remaining()
        if cooldown > 0:
            raise SourceResolutionError(
                "telegram_rate_limited",
                "Telegram Reader ожидает окончания обязательной паузы.",
                http_status=429,
                retry_after=cooldown,
            )
        if self._client is None:
            self._lease.acquire()
            session = self._session_store.load()
            self._client = self._client_factory(session)
            if hasattr(self._client, "session"):
                self._client.session.save_entities = False
        if not self._client_connected:
            async with asyncio.timeout(TELEGRAM_OPERATION_TIMEOUT_SECONDS):
                await self._client.connect()
                identity = await verify_authorized_identity(
                    self._client,
                    expected_user_id=(
                        self._server_settings.telegram_expected_user_id
                    ),
                )
            self._client_connected = True
            self._account_user_id = identity.user_id

    async def _replace_handlers_locked(self, chat_ids: tuple[int, ...]) -> None:
        unique_ids = tuple(sorted(set(chat_ids)))
        if (
            self._active_chat_ids == frozenset(unique_ids)
            and len(self._handlers) == 2
        ):
            return
        await self._remove_handlers_locked()
        assert self._client is not None
        new_builder = events.NewMessage(
            chats=unique_ids,
            incoming=True,
            forwards=False,
        )
        edited_builder = events.MessageEdited(
            chats=unique_ids,
            incoming=True,
            forwards=False,
        )
        self._client.add_event_handler(self._on_new_message, new_builder)
        self._client.add_event_handler(self._on_edited_message, edited_builder)
        self._handlers = [
            (self._on_new_message, new_builder),
            (self._on_edited_message, edited_builder),
        ]
        self._active_chat_ids = frozenset(unique_ids)

    async def _remove_handlers_locked(self) -> None:
        client = self._client
        if client is not None:
            for callback, builder in self._handlers:
                try:
                    client.remove_event_handler(callback, builder)
                except Exception as exc:
                    LOGGER.warning(
                        "Could not remove Telethon handler: %s",
                        type(exc).__name__,
                    )
        self._handlers.clear()
        self._active_chat_ids = frozenset()
        self._active_source_ids = frozenset()

    async def _disconnect_locked(self, *, release_lease: bool = True) -> None:
        client = self._client
        if client is None:
            self._client_connected = False
            if release_lease:
                self._lease.release()
                self._inbox_recovered = False
            return
        try:
            async with asyncio.timeout(10):
                await client.disconnect()
        except Exception as exc:
            LOGGER.warning(
                "Could not disconnect Telethon cleanly: %s",
                type(exc).__name__,
            )
            raise ReaderDisconnectError(
                "Telethon disconnect could not be confirmed"
            ) from exc
        self._client = None
        self._client_connected = False
        if release_lease:
            self._lease.release()
            self._inbox_recovered = False

    def _validate_resolved_entity(
        self,
        candidate: SourceCandidate,
        entity: Any,
    ) -> ResolvedReviewedSource:
        if not self._is_channel(entity):
            raise SourceResolutionError(
                "unsupported_source_type",
                "Telegram-источник не является публичной группой или каналом.",
                http_status=409,
            )
        canonical_raw = getattr(entity, "username", None)
        canonical_handle = str(canonical_raw).strip() if canonical_raw else ""
        if not canonical_handle:
            raise SourceResolutionError(
                "source_not_public",
                "У источника нет публичного Telegram username.",
                http_status=409,
            )
        if canonical_handle.casefold() != candidate.handle.casefold():
            raise SourceResolutionError(
                "username_mismatch",
                "Telegram username больше не совпадает с каталогом.",
                http_status=409,
            )
        if bool(getattr(entity, "forum", False)):
            raise SourceResolutionError(
                "unsupported_forum",
                "Форумы пока не поддерживаются без корректных topic-ссылок.",
                http_status=409,
            )
        if bool(getattr(entity, "kicked", False)):
            raise SourceResolutionError(
                "source_unavailable",
                "Аккаунт Reader заблокирован в этом источнике.",
                http_status=409,
            )
        if bool(getattr(entity, "left", False)):
            raise SourceResolutionError(
                "source_not_joined",
                "Сначала вступите или подпишитесь вручную в официальном Telegram.",
                http_status=409,
            )

        if bool(getattr(entity, "megagroup", False)):
            source_kind = "supergroup"
        elif bool(getattr(entity, "broadcast", False)):
            source_kind = "channel"
        else:
            raise SourceResolutionError(
                "unsupported_source_type",
                "Поддерживаются только публичные супергруппы и каналы.",
                http_status=409,
            )
        marked_id = int(self._peer_id(entity))
        if marked_id >= -1_000_000_000_000:
            raise SourceResolutionError(
                "invalid_chat_id",
                "Telegram вернул неподдерживаемый ID источника.",
                http_status=409,
            )
        title_raw = getattr(entity, "title", None)
        title = str(title_raw).strip() if title_raw else candidate.title
        if self._account_user_id is None:
            raise SourceResolutionError(
                "session_unavailable",
                "Не удалось подтвердить Telegram-аккаунт Reader.",
                http_status=409,
            )
        return ResolvedReviewedSource(
            handle=canonical_handle,
            title=title,
            telegram_chat_id=marked_id,
            source_kind=source_kind,
            account_user_id=self._account_user_id,
            checked_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
        )

    async def _on_new_message(self, event: Any) -> None:
        await self._persist_event(event, event_type="new")

    async def _on_edited_message(self, event: Any) -> None:
        await self._persist_event(event, event_type="edited")

    async def _persist_event(self, telegram_event: Any, *, event_type: str) -> None:
        generation = self._generation
        if not self._started:
            return
        try:
            chat_id = int(telegram_event.chat_id)
            message_id = int(telegram_event.id)
            if chat_id not in self._active_chat_ids:
                return
            raw_text = telegram_event.raw_text
            if not isinstance(raw_text, str) or not raw_text.strip():
                return
            published_at = _canonical_event_time(
                telegram_event.date,
                field="date",
            )
            edited_at = None
            if event_type == "edited":
                edited_at = _canonical_event_time(
                    telegram_event.edit_date,
                    field="edit_date",
                )
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "event_type": event_type,
                "text": raw_text.strip(),
                "published_at": published_at,
                "edited_at": edited_at,
            }
            digest = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            result = await enqueue_reader_event(
                self._app_settings,
                event=PublicMessageEvent(
                    request_id=f"tg:{digest}",
                    telegram_chat_id=chat_id,
                    telegram_message_id=message_id,
                    text=raw_text,
                    published_at=published_at,
                    edited_at=edited_at,
                    event_type=event_type,
                ),
                payload_hash=digest,
                expected_account_user_id=(
                    self._server_settings.telegram_expected_user_id
                ),
            )
            if result.inserted:
                self._wake_event.set()
            if result.pending_count > self._app_settings.reader_queue_max:
                self._schedule_maintenance(
                    self._degrade_for_backpressure(generation),
                    name="bb-bike-reader-backpressure",
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOGGER.error(
                "Could not durably persist Telegram update: %s",
                type(exc).__name__,
            )
            self._schedule_maintenance(
                self._degrade_after_callback_failure(exc, generation),
                name="bb-bike-reader-callback-failure",
            )

    def _ensure_worker_locked(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = asyncio.create_task(
            self._worker_loop(),
            name="bb-bike-reader-inbox-worker",
        )
        generation = self._generation
        self._worker_task.add_done_callback(
            lambda task: self._worker_finished(task, generation)
        )

    def _worker_finished(
        self,
        task: asyncio.Task[None],
        generation: int,
    ) -> None:
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if (
            exc is None
            or self._stop_event.is_set()
            or generation != self._generation
        ):
            return
        LOGGER.error("Live reader worker failed: %s", type(exc).__name__)
        self._schedule_maintenance(
            self._degrade_after_callback_failure(exc, generation),
            name="bb-bike-reader-worker-failure",
        )

    def _schedule_maintenance(
        self,
        operation: Awaitable[None],
        *,
        name: str,
    ) -> None:
        task = asyncio.create_task(operation, name=name)
        self._maintenance_tasks.add(task)
        task.add_done_callback(self._maintenance_finished)

    def _maintenance_finished(self, task: asyncio.Task[None]) -> None:
        self._maintenance_tasks.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            LOGGER.warning(
                "Live reader maintenance task failed with %s",
                type(exc).__name__,
            )

    def _ensure_supervisor_locked(self) -> None:
        if self._supervisor_task is not None and not self._supervisor_task.done():
            return
        self._supervisor_task = asyncio.create_task(
            self._supervisor_loop(),
            name="bb-bike-reader-supervisor",
        )

    async def _supervisor_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=SUPERVISOR_RETRY_SECONDS,
                )
                continue
            except TimeoutError:
                pass
            try:
                async with self._lifecycle_lock:
                    if not self._started:
                        continue
                    disconnected = False
                    if self._client_connected:
                        checker = getattr(self._client, "is_connected", None)
                        if callable(checker):
                            try:
                                disconnected = not bool(checker())
                            except Exception:
                                disconnected = True
                    if disconnected:
                        await self._degrade_locked("telegram_disconnected")
                        continue
                    needs_refresh = self._needs_retry or (
                        self._monotonic() - self._last_full_refresh
                        >= FULL_REFRESH_SECONDS
                    )
                    retry_error_code = self._retry_error_code
                if not needs_refresh:
                    continue
                if retry_error_code == "inbox_backpressure":
                    snapshot = await self.status()
                    if (
                        snapshot is not None
                        and snapshot.pending_event_count
                        >= self._app_settings.reader_queue_max
                    ):
                        continue
                await self.refresh_allowlist()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.warning(
                    "Live reader supervisor retry failed with %s",
                    type(exc).__name__,
                )

    async def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            item = await claim_next_reader_event(self._app_settings)
            if item is None:
                await self._heartbeat_if_due()
                self._wake_event.clear()
                try:
                    await asyncio.wait_for(
                        self._wake_event.wait(),
                        timeout=WORKER_IDLE_SECONDS,
                    )
                except TimeoutError:
                    pass
                continue

            try:
                await self._ingest(self._app_settings, item.event)
                await complete_reader_event(self._app_settings, item=item)
            except asyncio.CancelledError:
                retry_at = datetime.now(UTC).replace(microsecond=0).isoformat()
                try:
                    await asyncio.shield(
                        fail_reader_event(
                            self._app_settings,
                            item=item,
                            error_code="shutdown_interrupted",
                            retry_at=retry_at,
                            permanent=False,
                        )
                    )
                except Exception:
                    pass
                raise
            except (SourceNotAllowed, CollectorDisabled):
                await fail_reader_event(
                    self._app_settings,
                    item=item,
                    error_code="source_not_allowed",
                    retry_at=None,
                    permanent=True,
                )
            except (IdempotencyConflict, IngestionError):
                await fail_reader_event(
                    self._app_settings,
                    item=item,
                    error_code="invalid_ingest_event",
                    retry_at=None,
                    permanent=True,
                )
            except Exception as exc:
                permanent = item.attempt_count >= MAX_DELIVERY_ATTEMPTS
                delay = min(60, 2 ** max(0, item.attempt_count - 1))
                retry_at = (
                    datetime.now(UTC) + timedelta(seconds=delay)
                ).replace(microsecond=0).isoformat()
                await fail_reader_event(
                    self._app_settings,
                    item=item,
                    error_code=(
                        "delivery_failed"
                        if permanent
                        else self._safe_error_code(exc)
                    ),
                    retry_at=None if permanent else retry_at,
                    permanent=permanent,
                )
            await self._heartbeat_if_due()

    async def _heartbeat_if_due(self) -> None:
        now = self._monotonic()
        if now - self._last_heartbeat < HEARTBEAT_INTERVAL_SECONDS:
            return
        await touch_reader_heartbeat(self._app_settings)
        self._last_heartbeat = now
        if now - self._last_inbox_prune >= INBOX_PRUNE_INTERVAL_SECONDS:
            try:
                await purge_completed_reader_events(self._app_settings)
                await purge_expired_rejections(self._app_settings)
                self._last_inbox_prune = now
            except Exception as exc:
                LOGGER.warning(
                    "Could not prune completed reader inbox metadata: %s",
                    type(exc).__name__,
                )

    async def _degrade_for_backpressure(self, generation: int) -> None:
        async with self._lifecycle_lock:
            if not self._started or generation != self._generation:
                return
            await self._degrade_locked("inbox_backpressure")

    async def _degrade_after_callback_failure(
        self,
        exc: Exception,
        generation: int,
    ) -> None:
        async with self._lifecycle_lock:
            if not self._started or generation != self._generation:
                return
            await self._degrade_locked(self._safe_error_code(exc))

    async def _degrade_locked(self, error_code: str) -> None:
        self._needs_retry = True
        self._retry_error_code = error_code
        if error_code == "readerleaseerror" and not self._lease.held:
            return
        active_source_ids = self._active_source_ids
        await self._remove_handlers_locked()
        await self._disconnect_locked(release_lease=False)
        await set_reader_source_statuses(
            self._app_settings,
            source_ids=active_source_ids,
            status="degraded",
            error_code=error_code,
        )
        await set_reader_runtime(
            self._app_settings,
            state="degraded",
            account_user_id=self._account_user_id,
            active_source_count=0,
            error_code=error_code,
        )

    def _cooldown_remaining(self) -> int:
        try:
            return cooldown_remaining_seconds(self._cooldown_path)
        except TelegramCooldownError as exc:
            raise SourceResolutionError(
                "cooldown_locked",
                "Файл обязательной паузы Telegram повреждён.",
                http_status=429,
                retry_after=3600,
            ) from exc

    def _record_flood_wait(self, exc: errors.FloodError) -> int:
        seconds = getattr(exc, "seconds", None)
        retry_after = (
            int(seconds)
            if isinstance(seconds, (int, float)) and seconds > 0
            else 3600
        )
        try:
            record_cooldown(self._cooldown_path, seconds=retry_after)
        except (OSError, ValueError):
            LOGGER.error("Could not persist Telegram FloodWait cooldown")
        return retry_after

    @staticmethod
    def _safe_error_code(exc: Exception) -> str:
        explicit = getattr(exc, "code", None)
        if isinstance(explicit, str) and explicit:
            candidate = explicit.lower()
            if all(character.isalnum() or character == "_" for character in candidate):
                return candidate[:64]
        name = type(exc).__name__.lower()
        normalized = "".join(character if character.isalnum() else "_" for character in name)
        return (normalized.strip("_") or "reader_error")[:64]
