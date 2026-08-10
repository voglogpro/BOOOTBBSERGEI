from __future__ import annotations

import json
import re
from collections.abc import Iterable

import aiosqlite

from app.config import Settings
from app.db import connect_db, utc_now
from app.rules import normalize_text
from app.source_verification import ReadySourceVerification


HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def normalize_public_handle(value: str) -> str:
    handle = value.strip().removeprefix("@").lower()
    if not HANDLE_RE.fullmatch(handle):
        raise ValueError("public handle must contain 5-32 letters, digits or underscores")
    return handle


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
) -> int:
    if verification.telegram_chat_id >= -1_000_000_000_000:
        raise ValueError("verification requires a marked -100... supergroup ID")
    source_id = await upsert_source(
        settings,
        telegram_chat_id=verification.telegram_chat_id,
        public_handle=verification.handle,
        title=verification.title,
        city_slug=city_slug,
        source_kind="supergroup",
    )
    db = await connect_db(settings)
    try:
        now = utc_now()
        await db.execute("BEGIN IMMEDIATE")
        cursor = await db.execute(
            """
            SELECT telegram_chat_id, public_handle, enabled
            FROM lead_sources
            WHERE id = ?
            """,
            (source_id,),
        )
        source = await cursor.fetchone()
        if not source or int(source["enabled"]) != 0:
            raise ValueError("verified source must remain disabled during registration")
        if int(source["telegram_chat_id"]) != verification.telegram_chat_id:
            raise ValueError("verification chat_id does not match registered source")
        if str(source["public_handle"]).casefold() != verification.handle.casefold():
            raise ValueError("verification handle does not match registered source")
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
                verification.handle,
                verification.telegram_chat_id,
                verification.account_user_id,
                verification.report_schema_version,
                verification.checked_at,
                now,
            ),
        )
        await db.commit()
        return source_id
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def set_source_enabled(
    settings: Settings, *, telegram_chat_id: int, enabled: bool
) -> None:
    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        if enabled:
            cursor = await db.execute(
                """
                SELECT 1
                FROM lead_sources AS s
                JOIN source_verifications AS v ON v.source_id = s.id
                WHERE s.telegram_chat_id = ?
                  AND v.verified_chat_id = s.telegram_chat_id
                  AND v.verified_handle = s.public_handle COLLATE NOCASE
                  AND datetime(v.created_at) >= datetime('now', '-24 hours')
                """,
                (telegram_chat_id,),
            )
            if not await cursor.fetchone():
                raise ValueError(
                    "source cannot be enabled without a matching ready verification"
                )
        cursor = await db.execute(
            """
            UPDATE lead_sources
            SET enabled = ?, updated_at = ?
            WHERE telegram_chat_id = ?
            """,
            (1 if enabled else 0, utc_now(), telegram_chat_id),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"unknown source chat_id: {telegram_chat_id}")
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
