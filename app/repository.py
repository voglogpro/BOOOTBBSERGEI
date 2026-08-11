from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import aiosqlite

from app.config import Settings
from app.db import connect_db, utc_now
from app.models import PublicMessageEvent
from app.rules import normalize_text
from app.source_verification import (
    MAX_FUTURE_SKEW,
    MAX_REPORT_AGE,
    RESOLUTION_REPORT_SCHEMA_VERSION,
    ReadySourceVerification,
)


HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
READER_RUNTIME_STATES = frozenset(
    {"stopped", "starting", "paused", "running", "degraded"}
)


@dataclass(frozen=True, slots=True)
class ReaderSource:
    source_id: int
    telegram_chat_id: int
    public_handle: str
    title: str
    source_kind: str
    verified_at: str


@dataclass(frozen=True, slots=True)
class InboxEnqueueResult:
    inserted: bool
    pending_count: int


@dataclass(frozen=True, slots=True)
class ReaderInboxItem:
    inbox_id: int
    source_id: int
    event: PublicMessageEvent
    attempt_count: int


@dataclass(frozen=True, slots=True)
class ReaderRuntimeSnapshot:
    state: str
    account_user_id: int | None
    active_source_count: int
    pending_event_count: int
    connected_at: str | None
    heartbeat_at: str | None
    last_error_code: str | None
    last_error_at: str | None
    updated_at: str


def normalize_public_handle(value: str) -> str:
    handle = value.strip().removeprefix("@").lower()
    if not HANDLE_RE.fullmatch(handle):
        raise ValueError("public handle must contain 5-32 letters, digits or underscores")
    return handle


def _parse_utc_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(UTC)


def _canonical_utc_timestamp(value: str, *, field: str) -> str:
    return (
        _parse_utc_timestamp(value, field=field)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _validate_ready_verification(
    verification: ReadySourceVerification,
    *,
    expected_account_user_id: int | None,
    now: datetime | None = None,
) -> ReadySourceVerification:
    handle = normalize_public_handle(verification.handle)
    title = verification.title.strip()
    if not title:
        raise ValueError("verification title is required")
    if verification.telegram_chat_id >= -1_000_000_000_000:
        raise ValueError("verification requires a marked -100... Telegram ID")
    if verification.account_user_id <= 0:
        raise ValueError("verification account_user_id must be positive")
    if (
        expected_account_user_id is not None
        and verification.account_user_id != expected_account_user_id
    ):
        raise ValueError("verification belongs to an unexpected Telegram account")
    if verification.report_schema_version != RESOLUTION_REPORT_SCHEMA_VERSION:
        raise ValueError("verification report schema is not supported")

    checked_at = _parse_utc_timestamp(
        verification.checked_at,
        field="verification.checked_at",
    )
    current = (now or datetime.now(UTC)).astimezone(UTC)
    if checked_at > current + MAX_FUTURE_SKEW:
        raise ValueError("verification timestamp is in the future")
    if current - checked_at > MAX_REPORT_AGE:
        raise ValueError("verification is older than 24 hours")

    return ReadySourceVerification(
        handle=handle,
        title=title,
        telegram_chat_id=verification.telegram_chat_id,
        account_user_id=verification.account_user_id,
        checked_at=checked_at.replace(microsecond=0).isoformat(),
        report_schema_version=verification.report_schema_version,
    )


def _audit_actor(actor_telegram_id: int | None) -> tuple[str, int | None]:
    if actor_telegram_id is None:
        return "system", None
    if isinstance(actor_telegram_id, bool) or actor_telegram_id <= 0:
        raise ValueError("actor_telegram_id must be a positive integer")
    return "admin", actor_telegram_id


async def _insert_source_audit_event(
    db: aiosqlite.Connection,
    *,
    source_id: int | None,
    telegram_chat_id: int | None,
    public_handle: str | None,
    event_type: str,
    actor_kind: str,
    actor_telegram_id: int | None,
    details: dict[str, object] | None = None,
    created_at: str | None = None,
) -> None:
    if event_type not in {
        "verified",
        "enabled",
        "disabled",
        "verification_revoked",
        "reader_degraded",
    }:
        raise ValueError("unsupported source audit event_type")
    if actor_kind not in {"system", "admin", "reader"}:
        raise ValueError("unsupported source audit actor_kind")
    if actor_kind == "admin" and (
        actor_telegram_id is None or actor_telegram_id <= 0
    ):
        raise ValueError("admin audit events require actor_telegram_id")
    await db.execute(
        """
        INSERT INTO source_audit_events(
            source_id, telegram_chat_id, public_handle, event_type,
            actor_kind, actor_telegram_id, details_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            telegram_chat_id,
            public_handle,
            event_type,
            actor_kind,
            actor_telegram_id,
            json.dumps(
                details or {},
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
            created_at or utc_now(),
        ),
    )


async def upsert_city(
    settings: Settings,
    *,
    slug: str,
    name: str,
    region: str | None = None,
    aliases: Iterable[str] = (),
) -> int:
    slug = slug.strip().lower()
    if not SLUG_RE.fullmatch(slug):
        raise ValueError("city slug must use lowercase latin letters, digits and hyphens")
    name = name.strip()
    if not name:
        raise ValueError("city name is required")

    normalized_aliases = sorted(
        {
            normalize_text(alias)
            for alias in aliases
            if isinstance(alias, str) and alias.strip()
        }
    )
    now = utc_now()
    db = await connect_db(settings)
    try:
        await db.execute(
            """
            INSERT INTO market_cities(
                slug, name, region, aliases_json, enabled, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(slug) DO UPDATE SET
                name = excluded.name,
                region = excluded.region,
                aliases_json = excluded.aliases_json,
                enabled = 1,
                updated_at = excluded.updated_at
            """,
            (
                slug,
                name,
                region.strip() if region else None,
                json.dumps(normalized_aliases, ensure_ascii=False),
                now,
                now,
            ),
        )
        cursor = await db.execute(
            "SELECT id FROM market_cities WHERE slug = ? COLLATE NOCASE", (slug,)
        )
        row = await cursor.fetchone()
        await db.commit()
        return int(row["id"])
    finally:
        await db.close()


async def upsert_source(
    settings: Settings,
    *,
    telegram_chat_id: int,
    public_handle: str,
    title: str,
    city_slug: str | None = None,
    source_kind: str = "group",
) -> int:
    if telegram_chat_id >= -1_000_000_000_000:
        raise ValueError("public Telegram sources require a marked -100... chat_id")
    handle = normalize_public_handle(public_handle)
    title = title.strip()
    if not title:
        raise ValueError("source title is required")
    if source_kind not in {"group", "supergroup", "channel"}:
        raise ValueError("source_kind must be group, supergroup or channel")

    db = await connect_db(settings)
    try:
        city_id = None
        if city_slug:
            cursor = await db.execute(
                "SELECT id FROM market_cities WHERE slug = ? AND enabled = 1",
                (city_slug.strip().lower(),),
            )
            city_row = await cursor.fetchone()
            if not city_row:
                raise ValueError(f"unknown or disabled city: {city_slug}")
            city_id = int(city_row["id"])

        now = utc_now()
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """
            INSERT INTO lead_sources(
                telegram_chat_id, public_handle, title, source_kind,
                default_city_id, enabled, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET
                public_handle = excluded.public_handle,
                title = excluded.title,
                source_kind = excluded.source_kind,
                default_city_id = excluded.default_city_id,
                enabled = 0,
                updated_at = excluded.updated_at
            """,
            (
                telegram_chat_id,
                handle,
                title,
                source_kind,
                city_id,
                now,
                now,
            ),
        )
        cursor = await db.execute(
            "SELECT id FROM lead_sources WHERE telegram_chat_id = ?",
            (telegram_chat_id,),
        )
        source_row = await cursor.fetchone()
        source_id = int(source_row["id"])
        await db.execute(
            "DELETE FROM source_verifications WHERE source_id = ?",
            (source_id,),
        )
        await db.execute(
            """
            INSERT INTO source_checkpoints(source_id, reader_status, updated_at)
            VALUES(?, 'paused', ?)
            ON CONFLICT(source_id) DO UPDATE SET
                reader_status = 'paused',
                updated_at = excluded.updated_at
            """,
            (source_id, now),
        )
        await db.commit()
        return source_id
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def register_verified_source(
    settings: Settings,
    *,
    verification: ReadySourceVerification,
    city_slug: str | None = None,
    source_kind: str = "supergroup",
    expected_account_user_id: int | None = None,
    actor_telegram_id: int | None = None,
) -> int:
    """Atomically store a server-issued verification, always disabled.

    The repository repeats freshness, schema and expected-account checks so a
    future HTTP handler cannot accidentally treat browser-supplied identity
    fields as trusted resolver output.
    """

    verified = _validate_ready_verification(
        verification,
        expected_account_user_id=expected_account_user_id,
    )
    if source_kind not in {"supergroup", "channel"}:
        raise ValueError("verified source_kind must be supergroup or channel")
    actor_kind, actor_id = _audit_actor(actor_telegram_id)

    db = await connect_db(settings)
    try:
        now = utc_now()
        await db.execute("BEGIN IMMEDIATE")

        city_id = None
        if city_slug:
            cursor = await db.execute(
                "SELECT id FROM market_cities WHERE slug = ? AND enabled = 1",
                (city_slug.strip().lower(),),
            )
            city_row = await cursor.fetchone()
            if not city_row:
                raise ValueError(f"unknown or disabled city: {city_slug}")
            city_id = int(city_row["id"])

        cursor = await db.execute(
            """
            SELECT id, telegram_chat_id
            FROM lead_sources
            WHERE public_handle = ? COLLATE NOCASE
            """,
            (verified.handle,),
        )
        handle_owner = await cursor.fetchone()
        if (
            handle_owner is not None
            and int(handle_owner["telegram_chat_id"]) != verified.telegram_chat_id
        ):
            raise ValueError(
                "reviewed public handle is already bound to another Telegram ID"
            )

        await db.execute(
            """
            INSERT INTO lead_sources(
                telegram_chat_id, public_handle, title, source_kind,
                default_city_id, enabled, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(telegram_chat_id) DO UPDATE SET
                public_handle = excluded.public_handle,
                title = excluded.title,
                source_kind = excluded.source_kind,
                default_city_id = excluded.default_city_id,
                enabled = 0,
                updated_at = excluded.updated_at
            """,
            (
                verified.telegram_chat_id,
                verified.handle,
                verified.title,
                source_kind,
                city_id,
                now,
                now,
            ),
        )
        cursor = await db.execute(
            """
            SELECT id, telegram_chat_id, public_handle, enabled
            FROM lead_sources
            WHERE telegram_chat_id = ?
            """,
            (verified.telegram_chat_id,),
        )
        source = await cursor.fetchone()
        if not source or int(source["enabled"]) != 0:
            raise ValueError("verified source must remain disabled during registration")
        source_id = int(source["id"])
        if int(source["telegram_chat_id"]) != verified.telegram_chat_id:
            raise ValueError("verification chat_id does not match registered source")
        if str(source["public_handle"]).casefold() != verified.handle.casefold():
            raise ValueError("verification handle does not match registered source")

        await db.execute(
            """
            INSERT INTO source_checkpoints(source_id, reader_status, updated_at)
            VALUES(?, 'paused', ?)
            ON CONFLICT(source_id) DO UPDATE SET
                reader_status = 'paused',
                last_error_code = NULL,
                last_error_at = NULL,
                updated_at = excluded.updated_at
            """,
            (source_id, now),
        )
        await db.execute(
            """
            INSERT INTO source_verifications(
                source_id, verified_handle, verified_chat_id, account_user_id,
                report_schema_version, verified_at, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                verified_handle = excluded.verified_handle,
                verified_chat_id = excluded.verified_chat_id,
                account_user_id = excluded.account_user_id,
                report_schema_version = excluded.report_schema_version,
                verified_at = excluded.verified_at,
                created_at = excluded.created_at
            """,
            (
                source_id,
                verified.handle,
                verified.telegram_chat_id,
                verified.account_user_id,
                verified.report_schema_version,
                verified.checked_at,
                now,
            ),
        )
        await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'dead', message_text = NULL, next_attempt_at = NULL,
                last_error_code = 'source_reverified', last_error_at = ?,
                completed_at = ?, updated_at = ?
            WHERE source_id = ? AND status IN ('pending', 'processing')
            """,
            (now, now, now, source_id),
        )
        await _insert_source_audit_event(
            db,
            source_id=source_id,
            telegram_chat_id=verified.telegram_chat_id,
            public_handle=verified.handle,
            event_type="verified",
            actor_kind=actor_kind,
            actor_telegram_id=actor_id,
            details={
                "account_user_id": verified.account_user_id,
                "checked_at": verified.checked_at,
                "source_kind": source_kind,
            },
            created_at=now,
        )
        await db.commit()
        return source_id
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def set_source_enabled(
    settings: Settings,
    *,
    telegram_chat_id: int,
    enabled: bool,
    expected_account_user_id: int | None = None,
    actor_telegram_id: int | None = None,
) -> None:
    await _set_source_enabled(
        settings,
        telegram_chat_id=telegram_chat_id,
        source_id=None,
        enabled=enabled,
        expected_account_user_id=expected_account_user_id,
        actor_telegram_id=actor_telegram_id,
    )


async def set_source_enabled_by_id(
    settings: Settings,
    *,
    source_id: int,
    enabled: bool,
    expected_account_user_id: int | None = None,
    actor_telegram_id: int | None = None,
) -> None:
    if isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    await _set_source_enabled(
        settings,
        telegram_chat_id=None,
        source_id=source_id,
        enabled=enabled,
        expected_account_user_id=expected_account_user_id,
        actor_telegram_id=actor_telegram_id,
    )


async def _set_source_enabled(
    settings: Settings,
    *,
    telegram_chat_id: int | None,
    source_id: int | None,
    enabled: bool,
    expected_account_user_id: int | None,
    actor_telegram_id: int | None,
) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be true or false")
    if (telegram_chat_id is None) == (source_id is None):
        raise ValueError("select a source by exactly one identifier")
    actor_kind, actor_id = _audit_actor(actor_telegram_id)
    selector = "s.telegram_chat_id = ?" if telegram_chat_id is not None else "s.id = ?"
    selector_value = telegram_chat_id if telegram_chat_id is not None else source_id

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            f"""
            SELECT s.id, s.telegram_chat_id, s.public_handle, s.enabled,
                   v.verified_chat_id, v.verified_handle, v.account_user_id,
                   v.report_schema_version, v.verified_at
            FROM lead_sources AS s
            LEFT JOIN source_verifications AS v ON v.source_id = s.id
            WHERE {selector}
            """,
            (selector_value,),
        )
        source = await cursor.fetchone()
        if source is None:
            identity = (
                f"chat_id: {telegram_chat_id}"
                if telegram_chat_id is not None
                else f"id: {source_id}"
            )
            raise ValueError(f"unknown source {identity}")

        if enabled:
            matching = (
                source["verified_chat_id"] is not None
                and int(source["verified_chat_id"]) == int(source["telegram_chat_id"])
                and str(source["verified_handle"]).casefold()
                == str(source["public_handle"]).casefold()
                and int(source["report_schema_version"])
                == RESOLUTION_REPORT_SCHEMA_VERSION
            )
            if not matching:
                raise ValueError(
                    "source cannot be enabled without a matching ready verification"
                )
            if (
                expected_account_user_id is not None
                and int(source["account_user_id"]) != expected_account_user_id
            ):
                raise ValueError("source verification belongs to another account")
            verified_at = _parse_utc_timestamp(
                str(source["verified_at"]),
                field="source verification timestamp",
            )
            current = datetime.now(UTC)
            if verified_at > current + MAX_FUTURE_SKEW:
                raise ValueError("source verification timestamp is in the future")
            if current - verified_at > MAX_REPORT_AGE:
                raise ValueError("source verification is older than 24 hours")

        if bool(source["enabled"]) == enabled:
            await db.commit()
            return

        now = utc_now()
        cursor = await db.execute(
            """
            UPDATE lead_sources
            SET enabled = ?, updated_at = ?
            WHERE id = ?
            """,
            (1 if enabled else 0, now, int(source["id"])),
        )
        if cursor.rowcount != 1:
            raise ValueError("source state changed concurrently")
        await db.execute(
            """
            INSERT INTO source_checkpoints(source_id, reader_status, updated_at)
            VALUES(?, 'paused', ?)
            ON CONFLICT(source_id) DO UPDATE SET
                reader_status = 'paused',
                updated_at = excluded.updated_at
            """,
            (int(source["id"]), now),
        )
        await _insert_source_audit_event(
            db,
            source_id=int(source["id"]),
            telegram_chat_id=int(source["telegram_chat_id"]),
            public_handle=str(source["public_handle"]),
            event_type="enabled" if enabled else "disabled",
            actor_kind=actor_kind,
            actor_telegram_id=actor_id,
            created_at=now,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def list_sources(settings: Settings) -> list[aiosqlite.Row]:
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            SELECT s.id, s.telegram_chat_id, s.public_handle, s.title,
                   s.source_kind, s.enabled, c.slug AS city_slug,
                   c.name AS city_name, cp.reader_status, cp.last_event_at,
                   cp.last_error_code, v.verified_at,
                   v.account_user_id AS verified_account_user_id
            FROM lead_sources AS s
            LEFT JOIN market_cities AS c ON c.id = s.default_city_id
            LEFT JOIN source_checkpoints AS cp ON cp.source_id = s.id
            LEFT JOIN source_verifications AS v ON v.source_id = s.id
            ORDER BY s.enabled DESC, s.title COLLATE NOCASE
            """
        )
        return list(await cursor.fetchall())
    finally:
        await db.close()


async def get_source(settings: Settings, *, source_id: int) -> aiosqlite.Row | None:
    if isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            SELECT s.id, s.telegram_chat_id, s.public_handle, s.title,
                   s.source_kind, s.enabled, c.slug AS city_slug,
                   c.name AS city_name, cp.reader_status, cp.last_message_id,
                   cp.last_event_at, cp.last_error_code, cp.last_error_at,
                   v.verified_at, v.account_user_id AS verified_account_user_id
            FROM lead_sources AS s
            LEFT JOIN market_cities AS c ON c.id = s.default_city_id
            LEFT JOIN source_checkpoints AS cp ON cp.source_id = s.id
            LEFT JOIN source_verifications AS v ON v.source_id = s.id
            WHERE s.id = ?
            """,
            (source_id,),
        )
        return await cursor.fetchone()
    finally:
        await db.close()


async def list_enabled_reader_sources(
    settings: Settings,
    *,
    expected_account_user_id: int,
) -> tuple[ReaderSource, ...]:
    if expected_account_user_id <= 0:
        raise ValueError("expected_account_user_id must be positive")
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            SELECT s.id, s.telegram_chat_id, s.public_handle, s.title,
                   s.source_kind, v.verified_at
            FROM lead_sources AS s
            JOIN source_verifications AS v ON v.source_id = s.id
            WHERE s.enabled = 1
              AND v.verified_chat_id = s.telegram_chat_id
              AND v.verified_handle = s.public_handle COLLATE NOCASE
              AND v.account_user_id = ?
              AND v.report_schema_version = ?
              AND s.source_kind IN ('supergroup', 'channel')
            ORDER BY s.id
            """,
            (expected_account_user_id, RESOLUTION_REPORT_SCHEMA_VERSION),
        )
        rows = await cursor.fetchall()
        return tuple(
            ReaderSource(
                source_id=int(row["id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                public_handle=str(row["public_handle"]),
                title=str(row["title"]),
                source_kind=str(row["source_kind"]),
                verified_at=str(row["verified_at"]),
            )
            for row in rows
        )
    finally:
        await db.close()


async def refresh_source_verification(
    settings: Settings,
    *,
    source_id: int,
    telegram_chat_id: int,
    public_handle: str,
    expected_account_user_id: int,
    checked_at: str,
) -> None:
    if isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    handle = normalize_public_handle(public_handle)
    canonical_checked_at = _canonical_utc_timestamp(
        checked_at,
        field="verification.checked_at",
    )
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            UPDATE source_verifications
            SET verified_at = ?, created_at = ?
            WHERE source_id = ?
              AND verified_chat_id = ?
              AND verified_handle = ? COLLATE NOCASE
              AND account_user_id = ?
              AND report_schema_version = ?
            """,
            (
                canonical_checked_at,
                utc_now(),
                source_id,
                telegram_chat_id,
                handle,
                expected_account_user_id,
                RESOLUTION_REPORT_SCHEMA_VERSION,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("source verification identity changed during refresh")
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def fail_closed_invalid_enabled_sources(
    settings: Settings,
    *,
    expected_account_user_id: int,
    allowed_public_handles: Iterable[str] | None = None,
) -> int:
    """Disable enabled rows that cannot belong to this reader account."""

    if expected_account_user_id <= 0:
        raise ValueError("expected_account_user_id must be positive")
    allowed_handles = (
        tuple(
            sorted(
                {
                    normalize_public_handle(handle)
                    for handle in allowed_public_handles
                }
            )
        )
        if allowed_public_handles is not None
        else None
    )
    catalog_clause = ""
    catalog_parameters: tuple[object, ...] = ()
    if allowed_handles is not None:
        if allowed_handles:
            placeholders = ",".join("?" for _ in allowed_handles)
            catalog_clause = (
                f" OR lower(s.public_handle) NOT IN ({placeholders})"
            )
            catalog_parameters = tuple(allowed_handles)
        else:
            catalog_clause = " OR 1 = 1"
    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            f"""
            SELECT s.id, s.telegram_chat_id, s.public_handle
            FROM lead_sources AS s
            LEFT JOIN source_verifications AS v ON v.source_id = s.id
            WHERE s.enabled = 1
              AND (
                    v.source_id IS NULL
                 OR v.verified_chat_id != s.telegram_chat_id
                 OR v.verified_handle != s.public_handle COLLATE NOCASE
                 OR v.account_user_id != ?
                 OR v.report_schema_version != ?
                 OR s.source_kind NOT IN ('supergroup', 'channel')
                 {catalog_clause}
              )
            """,
            (
                expected_account_user_id,
                RESOLUTION_REPORT_SCHEMA_VERSION,
                *catalog_parameters,
            ),
        )
        invalid = list(await cursor.fetchall())
        if not invalid:
            await db.commit()
            return 0

        now = utc_now()
        source_ids = [int(row["id"]) for row in invalid]
        placeholders = ",".join("?" for _ in source_ids)
        await db.execute(
            f"UPDATE lead_sources SET enabled = 0, updated_at = ? "
            f"WHERE id IN ({placeholders})",
            (now, *source_ids),
        )
        await db.execute(
            f"""
            UPDATE source_checkpoints
            SET reader_status = 'paused',
                last_error_code = 'verification_invalid',
                last_error_at = ?, updated_at = ?
            WHERE source_id IN ({placeholders})
            """,
            (now, now, *source_ids),
        )
        await db.execute(
            f"""
            UPDATE reader_inbox
            SET status = 'dead', message_text = NULL, next_attempt_at = NULL,
                last_error_code = 'verification_invalid', last_error_at = ?,
                completed_at = ?, updated_at = ?
            WHERE source_id IN ({placeholders})
              AND status IN ('pending', 'processing')
            """,
            (now, now, now, *source_ids),
        )
        for row in invalid:
            await _insert_source_audit_event(
                db,
                source_id=int(row["id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                public_handle=str(row["public_handle"]),
                event_type="verification_revoked",
                actor_kind="system",
                actor_telegram_id=None,
                details={"reason": "account_identity_or_catalog_mismatch"},
                created_at=now,
            )
        await db.commit()
        return len(invalid)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def revoke_reader_source_verification(
    settings: Settings,
    *,
    source_id: int,
    error_code: str,
) -> bool:
    """Disable a source whose public Telegram identity no longer verifies.

    The verification row is removed so the source cannot be re-enabled until
    an administrator explicitly verifies it again. Pending plaintext is
    scrubbed in the same transaction.
    """

    if isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    normalized_error = error_code.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", normalized_error):
        raise ValueError("reader error_code is invalid")

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT id, telegram_chat_id, public_handle, enabled
            FROM lead_sources
            WHERE id = ?
            """,
            (source_id,),
        )
        source = await cursor.fetchone()
        if source is None:
            await db.commit()
            return False

        now = utc_now()
        await db.execute(
            """
            UPDATE lead_sources
            SET enabled = 0, updated_at = ?
            WHERE id = ?
            """,
            (now, source_id),
        )
        await db.execute(
            "DELETE FROM source_verifications WHERE source_id = ?",
            (source_id,),
        )
        await db.execute(
            """
            INSERT INTO source_checkpoints(
                source_id, reader_status, last_error_code,
                last_error_at, updated_at
            ) VALUES(?, 'paused', ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                reader_status = 'paused',
                last_error_code = excluded.last_error_code,
                last_error_at = excluded.last_error_at,
                updated_at = excluded.updated_at
            """,
            (source_id, normalized_error, now, now),
        )
        await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'dead', message_text = NULL, next_attempt_at = NULL,
                last_error_code = ?, last_error_at = ?,
                completed_at = ?, updated_at = ?
            WHERE source_id = ? AND status IN ('pending', 'processing')
            """,
            (normalized_error, now, now, now, source_id),
        )
        await _insert_source_audit_event(
            db,
            source_id=source_id,
            telegram_chat_id=int(source["telegram_chat_id"]),
            public_handle=str(source["public_handle"]),
            event_type="verification_revoked",
            actor_kind="reader",
            actor_telegram_id=None,
            details={"reason": normalized_error},
            created_at=now,
        )
        await db.commit()
        return bool(source["enabled"])
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def set_reader_source_statuses(
    settings: Settings,
    *,
    source_ids: Iterable[int],
    status: str,
    error_code: str | None = None,
) -> None:
    selected = tuple(sorted(set(source_ids)))
    if not selected:
        return
    if any(isinstance(source_id, bool) or source_id <= 0 for source_id in selected):
        raise ValueError("source_ids must contain positive integers")
    if status not in {"paused", "ok", "degraded"}:
        raise ValueError("unsupported source reader status")
    normalized_error = error_code.strip().lower() if error_code else None
    if normalized_error and not re.fullmatch(r"[a-z0-9_]{1,64}", normalized_error):
        raise ValueError("source reader error_code is invalid")
    if status == "degraded" and normalized_error is None:
        raise ValueError("degraded source status requires error_code")

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        for source_id in selected:
            await db.execute(
                """
                INSERT INTO source_checkpoints(
                    source_id, last_error_code, last_error_at,
                    reader_status, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET
                    last_error_code = excluded.last_error_code,
                    last_error_at = excluded.last_error_at,
                    reader_status = excluded.reader_status,
                    updated_at = excluded.updated_at
                """,
                (
                    source_id,
                    normalized_error,
                    now if normalized_error else None,
                    status,
                    now,
                ),
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def _validate_reader_event(event: PublicMessageEvent) -> PublicMessageEvent:
    request_id = event.request_id.strip()
    if not request_id or len(request_id) > 200:
        raise ValueError("reader event_id must contain 1-200 characters")
    if event.telegram_chat_id >= -1_000_000_000_000:
        raise ValueError("reader event requires a marked -100... Telegram ID")
    if event.telegram_message_id <= 0:
        raise ValueError("reader message_id must be positive")
    text = event.text.strip()
    if not 1 <= len(text) <= 8192:
        raise ValueError("reader message text must contain 1-8192 characters")
    if event.event_type not in {"new", "edited"}:
        raise ValueError("reader event_type must be new or edited")
    return PublicMessageEvent(
        request_id=request_id,
        telegram_chat_id=event.telegram_chat_id,
        telegram_message_id=event.telegram_message_id,
        text=text,
        published_at=_canonical_utc_timestamp(
            event.published_at,
            field="reader published_at",
        ),
        edited_at=(
            _canonical_utc_timestamp(event.edited_at, field="reader edited_at")
            if event.edited_at
            else None
        ),
        event_type=event.event_type,
    )


async def enqueue_reader_event(
    settings: Settings,
    *,
    event: PublicMessageEvent,
    payload_hash: str,
    expected_account_user_id: int,
) -> InboxEnqueueResult:
    """Durably accept one allowlisted update before any classification."""

    validated = _validate_reader_event(event)
    if not re.fullmatch(r"[0-9a-f]{64}", payload_hash):
        raise ValueError("reader payload_hash must be lowercase SHA-256")
    canonical_payload = json.dumps(
        {
            "chat_id": validated.telegram_chat_id,
            "message_id": validated.telegram_message_id,
            "event_type": validated.event_type,
            "text": validated.text,
            "published_at": validated.published_at,
            "edited_at": validated.edited_at,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    expected_hash = hashlib.sha256(canonical_payload).hexdigest()
    if payload_hash != expected_hash or validated.request_id != f"tg:{expected_hash}":
        raise ValueError("reader event_id or payload_hash does not match payload")
    if expected_account_user_id <= 0:
        raise ValueError("expected_account_user_id must be positive")

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT s.id
            FROM lead_sources AS s
            JOIN source_verifications AS v ON v.source_id = s.id
            WHERE s.telegram_chat_id = ?
              AND s.enabled = 1
              AND v.verified_chat_id = s.telegram_chat_id
              AND v.verified_handle = s.public_handle COLLATE NOCASE
              AND v.account_user_id = ?
              AND v.report_schema_version = ?
            """,
            (
                validated.telegram_chat_id,
                expected_account_user_id,
                RESOLUTION_REPORT_SCHEMA_VERSION,
            ),
        )
        source = await cursor.fetchone()
        if source is None:
            await db.rollback()
            return InboxEnqueueResult(inserted=False, pending_count=0)

        now = utc_now()
        cursor = await db.execute(
            """
            INSERT INTO reader_inbox(
                event_id, source_id, telegram_chat_id, telegram_message_id,
                event_type, message_text, published_at, edited_at, payload_hash,
                status, attempt_count, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (
                validated.request_id,
                int(source["id"]),
                validated.telegram_chat_id,
                validated.telegram_message_id,
                validated.event_type,
                validated.text,
                validated.published_at,
                validated.edited_at,
                payload_hash,
                now,
                now,
            ),
        )
        inserted = cursor.rowcount == 1
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS n
            FROM reader_inbox
            WHERE status IN ('pending', 'processing')
            """
        )
        pending_count = int((await cursor.fetchone())["n"])
        await db.commit()
        return InboxEnqueueResult(
            inserted=inserted,
            pending_count=pending_count,
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def recover_reader_inbox(settings: Settings) -> int:
    db = await connect_db(settings)
    try:
        now = utc_now()
        cursor = await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'pending', next_attempt_at = NULL, updated_at = ?
            WHERE status = 'processing'
            """,
            (now,),
        )
        await db.commit()
        return max(0, cursor.rowcount)
    finally:
        await db.close()


async def claim_next_reader_event(
    settings: Settings,
    *,
    now: str | None = None,
) -> ReaderInboxItem | None:
    current = now or utc_now()
    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT id, event_id, source_id, telegram_chat_id,
                   telegram_message_id, event_type, message_text,
                   published_at, edited_at, attempt_count
            FROM reader_inbox
            WHERE status = 'pending'
              AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
            ORDER BY id
            LIMIT 1
            """,
            (current,),
        )
        row = await cursor.fetchone()
        if row is None:
            await db.commit()
            return None
        if row["message_text"] is None:
            raise ValueError("pending reader inbox item has no message text")
        attempt_count = int(row["attempt_count"]) + 1
        cursor = await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'processing', attempt_count = ?, updated_at = ?
            WHERE id = ? AND status = 'pending'
            """,
            (attempt_count, current, int(row["id"])),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("reader inbox claim changed concurrently")
        await db.commit()
        return ReaderInboxItem(
            inbox_id=int(row["id"]),
            source_id=int(row["source_id"]),
            event=PublicMessageEvent(
                request_id=str(row["event_id"]),
                telegram_chat_id=int(row["telegram_chat_id"]),
                telegram_message_id=int(row["telegram_message_id"]),
                text=str(row["message_text"]),
                published_at=str(row["published_at"]),
                edited_at=(
                    str(row["edited_at"]) if row["edited_at"] is not None else None
                ),
                event_type=str(row["event_type"]),
            ),
            attempt_count=attempt_count,
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def complete_reader_event(
    settings: Settings,
    *,
    item: ReaderInboxItem,
) -> None:
    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        cursor = await db.execute(
            "SELECT enabled FROM lead_sources WHERE id = ?",
            (item.source_id,),
        )
        source = await cursor.fetchone()
        if source is None:
            raise RuntimeError("reader inbox source no longer exists")
        reader_status = "ok" if bool(source["enabled"]) else "paused"
        cursor = await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'done', message_text = NULL, next_attempt_at = NULL,
                last_error_code = NULL, last_error_at = NULL,
                completed_at = ?, updated_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (now, now, item.inbox_id),
        )
        if cursor.rowcount != 1:
            cursor = await db.execute(
                "SELECT status FROM reader_inbox WHERE id = ?",
                (item.inbox_id,),
            )
            inbox = await cursor.fetchone()
            if inbox is not None and str(inbox["status"]) == "dead":
                await db.commit()
                return
            raise RuntimeError("reader inbox completion requires a claimed event")
        event_at = item.event.edited_at or item.event.published_at
        await db.execute(
            """
            INSERT INTO source_checkpoints(
                source_id, last_message_id, last_event_at,
                reader_status, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                last_message_id = CASE
                    WHEN source_checkpoints.last_message_id IS NULL
                      OR source_checkpoints.last_message_id < excluded.last_message_id
                    THEN excluded.last_message_id
                    ELSE source_checkpoints.last_message_id
                END,
                last_event_at = excluded.last_event_at,
                last_error_code = NULL,
                last_error_at = NULL,
                reader_status = excluded.reader_status,
                updated_at = excluded.updated_at
            """,
            (
                item.source_id,
                item.event.telegram_message_id,
                event_at,
                reader_status,
                now,
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def fail_reader_event(
    settings: Settings,
    *,
    item: ReaderInboxItem,
    error_code: str,
    retry_at: str | None,
    permanent: bool,
) -> None:
    error = error_code.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", error):
        raise ValueError("reader error_code is invalid")
    canonical_retry = (
        _canonical_utc_timestamp(retry_at, field="reader retry_at")
        if retry_at
        else None
    )
    if not permanent and canonical_retry is None:
        raise ValueError("retryable reader failures require retry_at")

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        cursor = await db.execute(
            """
            SELECT telegram_chat_id, public_handle, enabled
            FROM lead_sources WHERE id = ?
            """,
            (item.source_id,),
        )
        source = await cursor.fetchone()
        if source is None:
            raise RuntimeError("reader inbox source no longer exists")
        checkpoint_status = "degraded" if bool(source["enabled"]) else "paused"
        status = "dead" if permanent else "pending"
        cursor = await db.execute(
            """
            UPDATE reader_inbox
            SET status = ?,
                message_text = CASE WHEN ? = 'dead' THEN NULL ELSE message_text END,
                next_attempt_at = ?, last_error_code = ?, last_error_at = ?,
                completed_at = CASE WHEN ? = 'dead' THEN ? ELSE NULL END,
                updated_at = ?
            WHERE id = ? AND status = 'processing'
            """,
            (
                status,
                status,
                canonical_retry,
                error,
                now,
                status,
                now,
                now,
                item.inbox_id,
            ),
        )
        if cursor.rowcount != 1:
            cursor = await db.execute(
                "SELECT status FROM reader_inbox WHERE id = ?",
                (item.inbox_id,),
            )
            inbox = await cursor.fetchone()
            if inbox is not None and str(inbox["status"]) == "dead":
                await db.commit()
                return
            raise RuntimeError("reader inbox failure requires a claimed event")
        await db.execute(
            """
            INSERT INTO source_checkpoints(
                source_id, last_error_code, last_error_at,
                reader_status, updated_at
            ) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                last_error_code = excluded.last_error_code,
                last_error_at = excluded.last_error_at,
                reader_status = excluded.reader_status,
                updated_at = excluded.updated_at
            """,
            (item.source_id, error, now, checkpoint_status, now),
        )
        if permanent and bool(source["enabled"]):
            await _insert_source_audit_event(
                db,
                source_id=item.source_id,
                telegram_chat_id=int(source["telegram_chat_id"]),
                public_handle=str(source["public_handle"]),
                event_type="reader_degraded",
                actor_kind="reader",
                actor_telegram_id=None,
                details={
                    "error_code": error,
                    "event_id": item.event.request_id,
                },
                created_at=now,
            )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def cancel_pending_reader_events(
    settings: Settings,
    *,
    source_id: int,
) -> int:
    if isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("source_id must be a positive integer")
    db = await connect_db(settings)
    try:
        now = utc_now()
        cursor = await db.execute(
            """
            UPDATE reader_inbox
            SET status = 'dead', message_text = NULL, next_attempt_at = NULL,
                last_error_code = 'source_disabled', last_error_at = ?,
                completed_at = ?, updated_at = ?
            WHERE source_id = ? AND status IN ('pending', 'processing')
            """,
            (now, now, now, source_id),
        )
        await db.commit()
        return max(0, cursor.rowcount)
    finally:
        await db.close()


async def purge_completed_reader_events(
    settings: Settings,
    *,
    now: str | None = None,
) -> int:
    """Bound durable-inbox metadata after plaintext has already been scrubbed."""

    current = (
        _parse_utc_timestamp(now, field="reader inbox purge time")
        if now is not None
        else datetime.now(UTC)
    )
    cutoff = (current - timedelta(days=settings.rejected_message_retention_days))
    cutoff_text = cutoff.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            DELETE FROM reader_inbox
            WHERE status IN ('done', 'dead')
              AND completed_at IS NOT NULL
              AND completed_at <= ?
            """,
            (cutoff_text,),
        )
        await db.commit()
        return max(0, cursor.rowcount)
    finally:
        await db.close()


async def set_reader_runtime(
    settings: Settings,
    *,
    state: str,
    account_user_id: int | None,
    active_source_count: int = 0,
    connected: bool = False,
    error_code: str | None = None,
) -> ReaderRuntimeSnapshot:
    if state not in READER_RUNTIME_STATES:
        raise ValueError("unsupported reader runtime state")
    if account_user_id is not None and account_user_id <= 0:
        raise ValueError("reader account_user_id must be positive")
    if active_source_count < 0:
        raise ValueError("active_source_count cannot be negative")
    normalized_error = error_code.strip().lower() if error_code else None
    if normalized_error and not re.fullmatch(r"[a-z0-9_]{1,64}", normalized_error):
        raise ValueError("reader runtime error_code is invalid")

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        now = utc_now()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS n FROM reader_inbox
            WHERE status IN ('pending', 'processing')
            """
        )
        pending = int((await cursor.fetchone())["n"])
        await db.execute(
            """
            INSERT INTO reader_runtime(
                singleton_id, account_user_id, state, active_source_count,
                pending_event_count, connected_at, heartbeat_at,
                last_error_code, last_error_at, updated_at
            ) VALUES(1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(singleton_id) DO UPDATE SET
                account_user_id = excluded.account_user_id,
                state = excluded.state,
                active_source_count = excluded.active_source_count,
                pending_event_count = excluded.pending_event_count,
                connected_at = excluded.connected_at,
                heartbeat_at = excluded.heartbeat_at,
                last_error_code = excluded.last_error_code,
                last_error_at = excluded.last_error_at,
                updated_at = excluded.updated_at
            """,
            (
                account_user_id,
                state,
                active_source_count,
                pending,
                now if connected else None,
                now,
                normalized_error,
                now if normalized_error else None,
                now,
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
    snapshot = await get_reader_runtime(settings)
    assert snapshot is not None
    return snapshot


async def touch_reader_heartbeat(settings: Settings) -> None:
    db = await connect_db(settings)
    try:
        now = utc_now()
        cursor = await db.execute(
            """
            SELECT COUNT(*) AS n FROM reader_inbox
            WHERE status IN ('pending', 'processing')
            """
        )
        pending = int((await cursor.fetchone())["n"])
        await db.execute(
            """
            UPDATE reader_runtime
            SET pending_event_count = ?, heartbeat_at = ?, updated_at = ?
            WHERE singleton_id = 1
            """,
            (pending, now, now),
        )
        await db.commit()
    finally:
        await db.close()


async def get_reader_runtime(
    settings: Settings,
) -> ReaderRuntimeSnapshot | None:
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            "SELECT * FROM reader_runtime WHERE singleton_id = 1"
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        state = str(row["state"])
        if state not in READER_RUNTIME_STATES:
            raise ValueError("stored reader runtime state is invalid")
        return ReaderRuntimeSnapshot(
            state=state,
            account_user_id=(
                int(row["account_user_id"])
                if row["account_user_id"] is not None
                else None
            ),
            active_source_count=int(row["active_source_count"]),
            pending_event_count=int(row["pending_event_count"]),
            connected_at=(
                str(row["connected_at"]) if row["connected_at"] is not None else None
            ),
            heartbeat_at=(
                str(row["heartbeat_at"]) if row["heartbeat_at"] is not None else None
            ),
            last_error_code=(
                str(row["last_error_code"])
                if row["last_error_code"] is not None
                else None
            ),
            last_error_at=(
                str(row["last_error_at"])
                if row["last_error_at"] is not None
                else None
            ),
            updated_at=str(row["updated_at"]),
        )
    finally:
        await db.close()


async def purge_expired_rejections(settings: Settings, *, now: str | None = None) -> int:
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            DELETE FROM message_observations
            WHERE decision = 'rejected'
              AND purge_after IS NOT NULL
              AND purge_after <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM franchise_leads AS l
                  WHERE l.observation_id = message_observations.id
              )
            """,
            (now or utc_now(),),
        )
        await db.commit()
        return max(0, cursor.rowcount)
    finally:
        await db.close()
