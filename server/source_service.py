from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from app.config import Settings
from app.db import init_db
from app.repository import (
    cancel_pending_reader_events,
    fail_closed_invalid_enabled_sources,
    get_source,
    list_sources,
    normalize_public_handle,
    register_verified_source,
    set_source_enabled_by_id,
)
from app.source_candidates import (
    CandidateCatalog,
    SourceCandidate,
    load_candidate_catalog,
)
from app.source_verification import (
    MAX_FUTURE_SKEW,
    MAX_REPORT_AGE,
    RESOLUTION_REPORT_SCHEMA_VERSION,
    ReadySourceVerification,
)


class SourceServiceError(RuntimeError):
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


class SourceResolutionError(SourceServiceError):
    """A reviewed source could not be safely resolved by Telegram."""


@dataclass(frozen=True, slots=True)
class ResolvedReviewedSource:
    handle: str
    title: str
    telegram_chat_id: int
    source_kind: str
    account_user_id: int
    checked_at: str


class SourceReader(Protocol):
    async def resolve_reviewed_source(
        self,
        candidate: SourceCandidate,
    ) -> ResolvedReviewedSource: ...

    async def refresh_allowlist(self) -> None: ...


def _source_payload(row: object) -> dict[str, object]:
    return {
        "id": int(row["id"]),
        "telegram_chat_id": int(row["telegram_chat_id"]),
        "public_handle": str(row["public_handle"]),
        "title": str(row["title"]),
        "source_kind": str(row["source_kind"]),
        "enabled": bool(row["enabled"]),
        "city_slug": str(row["city_slug"]) if row["city_slug"] else None,
        "city_name": str(row["city_name"]) if row["city_name"] else None,
        "reader_status": (
            str(row["reader_status"]) if row["reader_status"] else "paused"
        ),
        "last_event_at": (
            str(row["last_event_at"]) if row["last_event_at"] else None
        ),
        "last_error_code": (
            str(row["last_error_code"]) if row["last_error_code"] else None
        ),
        "verified_at": (
            str(row["verified_at"]) if row["verified_at"] else None
        ),
        "verified_account_user_id": (
            int(row["verified_account_user_id"])
            if row["verified_account_user_id"] is not None
            else None
        ),
    }


def _verification_is_current(row: object, *, expected_account_user_id: int) -> bool:
    raw_verified_at = row["verified_at"]
    if raw_verified_at is None:
        return False
    try:
        verified_at = datetime.fromisoformat(
            str(raw_verified_at).replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if verified_at.tzinfo is None:
        return False
    now = datetime.now(UTC)
    checked = verified_at.astimezone(UTC)
    account_id = row["verified_account_user_id"]
    return (
        account_id is not None
        and int(account_id) == expected_account_user_id
        and checked <= now + MAX_FUTURE_SKEW
        and now - checked <= MAX_REPORT_AGE
    )


class SourceManagementService:
    """Server-side source lifecycle used by authenticated HTTP routes.

    Browser callers select only a reviewed catalog handle or an existing DB
    source ID. Telegram identity fields always come from ``SourceReader``.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        expected_account_user_id: int,
        reader: SourceReader,
        catalog_path: str | Path | None = None,
    ) -> None:
        if expected_account_user_id <= 0:
            raise ValueError("expected_account_user_id must be positive")
        self._settings = settings
        self._expected_account_user_id = expected_account_user_id
        self._reader = reader
        self._schema_lock = asyncio.Lock()
        self._schema_ready = False
        self._catalog: CandidateCatalog = load_candidate_catalog(catalog_path)
        self._candidates = {
            candidate.handle.casefold(): candidate
            for candidate in self._catalog.candidates
        }

    async def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        async with self._schema_lock:
            if self._schema_ready:
                return
            await init_db(self._settings)
            await fail_closed_invalid_enabled_sources(
                self._settings,
                expected_account_user_id=self._expected_account_user_id,
                allowed_public_handles=(
                    candidate.handle
                    for candidate in self._candidates.values()
                    if candidate.public_preview_verified
                ),
            )
            self._schema_ready = True

    async def catalog(self) -> dict[str, object]:
        await self._ensure_schema()
        runtime_rows = await list_sources(self._settings)
        runtime_by_handle = {
            str(row["public_handle"]).casefold(): row for row in runtime_rows
        }
        candidates: list[dict[str, object]] = []
        for candidate in self._catalog.candidates:
            source = runtime_by_handle.get(candidate.handle.casefold())
            if source is None:
                state = "candidate"
                source_id = None
                reader_status = None
            elif bool(source["enabled"]):
                state = "enabled"
                source_id = int(source["id"])
                reader_status = str(source["reader_status"] or "paused")
            elif _verification_is_current(
                source,
                expected_account_user_id=self._expected_account_user_id,
            ):
                state = "verified_disabled"
                source_id = int(source["id"])
                reader_status = str(source["reader_status"] or "paused")
            else:
                state = "unverified"
                source_id = int(source["id"])
                reader_status = str(source["reader_status"] or "paused")
            candidates.append(
                {
                    "priority": candidate.priority,
                    "handle": candidate.handle,
                    "title": candidate.title,
                    "category": candidate.category,
                    "geo": candidate.geo,
                    "public_url": candidate.public_url,
                    "noise_risk": candidate.noise_risk,
                    "reason": candidate.reason,
                    "state": state,
                    "source_id": source_id,
                    "reader_status": reader_status,
                }
            )
        return {
            "schema_version": self._catalog.schema_version,
            "researched_at": self._catalog.researched_at.isoformat(),
            "candidates": candidates,
        }

    async def sources(self) -> list[dict[str, object]]:
        await self._ensure_schema()
        return [_source_payload(row) for row in await list_sources(self._settings)]

    async def verify(
        self,
        *,
        handle: str,
        actor_telegram_id: int,
        city_slug: str | None = None,
    ) -> dict[str, object]:
        await self._ensure_schema()
        try:
            normalized = normalize_public_handle(handle)
        except ValueError as exc:
            raise SourceServiceError("invalid_handle", str(exc)) from exc
        candidate = self._candidates.get(normalized.casefold())
        if candidate is None or not candidate.public_preview_verified:
            raise SourceServiceError(
                "source_not_reviewed",
                "Источник отсутствует в проверенном каталоге.",
                http_status=404,
            )

        resolved = await self._reader.resolve_reviewed_source(candidate)
        if resolved.account_user_id != self._expected_account_user_id:
            raise SourceServiceError(
                "unexpected_account",
                "Сессия Reader принадлежит другому Telegram-аккаунту.",
                http_status=403,
            )
        if resolved.handle.casefold() != candidate.handle.casefold():
            raise SourceServiceError(
                "username_mismatch",
                "Telegram username больше не совпадает с каталогом.",
                http_status=409,
            )
        if resolved.source_kind not in {"supergroup", "channel"}:
            raise SourceServiceError(
                "unsupported_source_type",
                "Этот тип Telegram-источника пока не поддерживается.",
                http_status=409,
            )

        try:
            source_id = await register_verified_source(
                self._settings,
                verification=ReadySourceVerification(
                    handle=resolved.handle,
                    title=resolved.title,
                    telegram_chat_id=resolved.telegram_chat_id,
                    account_user_id=resolved.account_user_id,
                    checked_at=resolved.checked_at,
                    report_schema_version=RESOLUTION_REPORT_SCHEMA_VERSION,
                ),
                city_slug=city_slug,
                source_kind=resolved.source_kind,
                expected_account_user_id=self._expected_account_user_id,
                actor_telegram_id=actor_telegram_id,
            )
        except ValueError as exc:
            raise SourceServiceError(
                "verification_rejected",
                str(exc),
                http_status=409,
            ) from exc
        await self._reader.refresh_allowlist()
        row = await get_source(self._settings, source_id=source_id)
        assert row is not None
        return _source_payload(row)

    async def enable(
        self,
        *,
        source_id: int,
        actor_telegram_id: int,
    ) -> dict[str, object]:
        await self._ensure_schema()
        existing = await get_source(self._settings, source_id=source_id)
        if existing is None:
            raise SourceServiceError(
                "unknown_source",
                "Источник не найден.",
                http_status=404,
            )
        reviewed = self._candidates.get(
            str(existing["public_handle"]).casefold()
        )
        if reviewed is None or not reviewed.public_preview_verified:
            raise SourceServiceError(
                "source_not_reviewed",
                "Источник отсутствует в проверенном каталоге.",
                http_status=409,
            )
        try:
            await set_source_enabled_by_id(
                self._settings,
                source_id=source_id,
                enabled=True,
                expected_account_user_id=self._expected_account_user_id,
                actor_telegram_id=actor_telegram_id,
            )
        except ValueError as exc:
            raise SourceServiceError(
                "source_not_ready",
                str(exc),
                http_status=409,
            ) from exc
        await self._reader.refresh_allowlist()
        row = await get_source(self._settings, source_id=source_id)
        assert row is not None
        return _source_payload(row)

    async def disable(
        self,
        *,
        source_id: int,
        actor_telegram_id: int,
    ) -> dict[str, object]:
        await self._ensure_schema()
        try:
            await set_source_enabled_by_id(
                self._settings,
                source_id=source_id,
                enabled=False,
                expected_account_user_id=self._expected_account_user_id,
                actor_telegram_id=actor_telegram_id,
            )
            await cancel_pending_reader_events(
                self._settings,
                source_id=source_id,
            )
        except ValueError as exc:
            raise SourceServiceError(
                "unknown_source",
                str(exc),
                http_status=404,
            ) from exc
        await self._reader.refresh_allowlist()
        row = await get_source(self._settings, source_id=source_id)
        assert row is not None
        return _source_payload(row)
