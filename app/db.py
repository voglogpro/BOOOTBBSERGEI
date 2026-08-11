from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import aiosqlite

from app.config import Settings
from app.rules import (
    DEFAULT_RULESET_VERSION,
    LEAD_THRESHOLD,
    REVIEW_THRESHOLD,
    ruleset_json,
)


SCHEMA_VERSION = 3


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_cities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL COLLATE NOCASE UNIQUE,
    name TEXT NOT NULL,
    region TEXT,
    aliases_json TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS intent_rule_sets (
    version TEXT PRIMARY KEY,
    rules_json TEXT NOT NULL,
    review_threshold INTEGER NOT NULL CHECK (review_threshold BETWEEN 0 AND 100),
    lead_threshold INTEGER NOT NULL CHECK (lead_threshold BETWEEN 0 AND 100),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lead_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_chat_id INTEGER NOT NULL UNIQUE,
    public_handle TEXT NOT NULL COLLATE NOCASE UNIQUE,
    title TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT 'group'
        CHECK (source_kind IN ('group', 'supergroup', 'channel')),
    default_city_id INTEGER REFERENCES market_cities(id) ON DELETE SET NULL,
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (length(public_handle) BETWEEN 5 AND 32),
    CHECK (public_handle NOT LIKE '@%')
);

CREATE TABLE IF NOT EXISTS source_checkpoints (
    source_id INTEGER PRIMARY KEY REFERENCES lead_sources(id) ON DELETE CASCADE,
    last_message_id INTEGER,
    last_event_at TEXT,
    last_error_code TEXT,
    last_error_at TEXT,
    reader_status TEXT NOT NULL DEFAULT 'paused'
        CHECK (reader_status IN ('paused', 'ok', 'degraded')),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_verifications (
    source_id INTEGER PRIMARY KEY REFERENCES lead_sources(id) ON DELETE CASCADE,
    verified_handle TEXT NOT NULL COLLATE NOCASE,
    verified_chat_id INTEGER NOT NULL CHECK (verified_chat_id < -1000000000000),
    account_user_id INTEGER NOT NULL CHECK (account_user_id > 0),
    report_schema_version INTEGER NOT NULL CHECK (report_schema_version > 0),
    verified_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER REFERENCES lead_sources(id) ON DELETE SET NULL,
    telegram_chat_id INTEGER,
    public_handle TEXT COLLATE NOCASE,
    event_type TEXT NOT NULL
        CHECK (event_type IN (
            'verified', 'enabled', 'disabled', 'verification_revoked',
            'reader_degraded'
        )),
    actor_kind TEXT NOT NULL DEFAULT 'system'
        CHECK (actor_kind IN ('system', 'admin', 'reader')),
    actor_telegram_id INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    CHECK (
        (actor_kind = 'admin' AND actor_telegram_id IS NOT NULL)
        OR actor_kind != 'admin'
    )
);

CREATE TABLE IF NOT EXISTS reader_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    source_id INTEGER NOT NULL REFERENCES lead_sources(id) ON DELETE RESTRICT,
    telegram_chat_id INTEGER NOT NULL CHECK (telegram_chat_id < -1000000000000),
    telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
    event_type TEXT NOT NULL CHECK (event_type IN ('new', 'edited')),
    message_text TEXT CHECK (
        message_text IS NULL OR length(message_text) BETWEEN 1 AND 8192
    ),
    published_at TEXT NOT NULL,
    edited_at TEXT,
    payload_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'dead')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at TEXT,
    last_error_code TEXT,
    last_error_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK (status IN ('done', 'dead') OR message_text IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS reader_runtime (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    account_user_id INTEGER,
    state TEXT NOT NULL
        CHECK (state IN ('stopped', 'starting', 'paused', 'running', 'degraded')),
    active_source_count INTEGER NOT NULL DEFAULT 0
        CHECK (active_source_count >= 0),
    pending_event_count INTEGER NOT NULL DEFAULT 0
        CHECK (pending_event_count >= 0),
    connected_at TEXT,
    heartbeat_at TEXT,
    last_error_code TEXT,
    last_error_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS message_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES lead_sources(id) ON DELETE RESTRICT,
    telegram_message_id INTEGER NOT NULL CHECK (telegram_message_id > 0),
    message_url TEXT NOT NULL,
    message_text TEXT NOT NULL CHECK (length(message_text) BETWEEN 1 AND 8192),
    published_at TEXT NOT NULL,
    edited_at TEXT,
    observed_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    decision TEXT NOT NULL CHECK (decision IN ('lead', 'review', 'rejected')),
    intent_score INTEGER NOT NULL CHECK (intent_score BETWEEN 0 AND 100),
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    rule_version TEXT NOT NULL REFERENCES intent_rule_sets(version),
    detected_city_id INTEGER REFERENCES market_cities(id) ON DELETE SET NULL,
    city_confidence REAL CHECK (
        city_confidence IS NULL OR city_confidence BETWEEN 0.0 AND 1.0
    ),
    purge_after TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (source_id, telegram_message_id)
);

CREATE TABLE IF NOT EXISTS franchise_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    observation_id INTEGER NOT NULL UNIQUE
        REFERENCES message_observations(id) ON DELETE RESTRICT,
    status TEXT NOT NULL
        CHECK (status IN (
            'new', 'reviewing', 'contacted', 'qualified',
            'won', 'lost', 'disqualified', 'archived'
        )),
    detected_city_id INTEGER REFERENCES market_cities(id) ON DELETE SET NULL,
    intent_score INTEGER NOT NULL CHECK (intent_score BETWEEN 0 AND 100),
    matched_rules_json TEXT NOT NULL DEFAULT '[]',
    rule_version TEXT NOT NULL REFERENCES intent_rule_sets(version),
    assigned_manager_telegram_id INTEGER,
    disqualification_reason TEXT,
    needs_review INTEGER NOT NULL DEFAULT 0 CHECK (needs_review IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        status NOT IN ('lost', 'disqualified')
        OR (disqualification_reason IS NOT NULL AND length(trim(disqualification_reason)) > 0)
    )
);

CREATE TABLE IF NOT EXISTS lead_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id INTEGER NOT NULL REFERENCES franchise_leads(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor_kind TEXT NOT NULL DEFAULT 'system'
        CHECK (actor_kind IN ('system', 'manager', 'reader')),
    actor_telegram_id INTEGER,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_requests (
    request_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    observation_id INTEGER NOT NULL
        REFERENCES message_observations(id) ON DELETE CASCADE,
    lead_id INTEGER REFERENCES franchise_leads(id) ON DELETE SET NULL,
    result TEXT NOT NULL CHECK (result IN ('created', 'duplicate', 'updated')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS city_score_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    city_id INTEGER NOT NULL REFERENCES market_cities(id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    scenario TEXT NOT NULL CHECK (scenario IN ('low', 'base', 'high')),
    inputs_json TEXT NOT NULL,
    outputs_json TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (city_id, model_version, scenario, calculated_at)
);

CREATE INDEX IF NOT EXISTS idx_sources_enabled
    ON lead_sources(enabled, id);
CREATE INDEX IF NOT EXISTS idx_source_audit_date
    ON source_audit_events(source_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reader_inbox_delivery
    ON reader_inbox(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_reader_inbox_source
    ON reader_inbox(source_id, status, id);
CREATE INDEX IF NOT EXISTS idx_reader_inbox_retention
    ON reader_inbox(status, completed_at)
    WHERE status IN ('done', 'dead');
CREATE INDEX IF NOT EXISTS idx_observations_decision_date
    ON message_observations(decision, published_at DESC);
CREATE INDEX IF NOT EXISTS idx_observations_purge
    ON message_observations(purge_after)
    WHERE purge_after IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_leads_status_date
    ON franchise_leads(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_leads_city_status
    ON franchise_leads(detected_city_id, status);
CREATE INDEX IF NOT EXISTS idx_lead_events_lead_date
    ON lead_events(lead_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_city_scores_latest
    ON city_score_snapshots(city_id, scenario, calculated_at DESC);
"""


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def connect_db(settings: Settings) -> aiosqlite.Connection:
    settings.ensure_runtime_directories()
    db = aiosqlite.connect(settings.database_path)
    # ``aiosqlite.Connection.__await__`` starts a worker thread.  If the
    # caller is cancelled while that thread is still opening SQLite, directly
    # cancelling the await can return before ``_connection`` is assigned; at
    # that point ``close()`` is a no-op and the worker may later leave the
    # database file open.  Keep the connect operation alive under a shield,
    # then wait for it before closing on every failure/cancellation path.
    connect_task = asyncio.ensure_future(db)
    try:
        await asyncio.shield(connect_task)
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute("PRAGMA synchronous = NORMAL")
        await db.execute("PRAGMA busy_timeout = 5000")
        return db
    except BaseException:
        try:
            await asyncio.shield(connect_task)
        except BaseException:
            # A failed connector already stops its own worker thread.
            pass
        try:
            await asyncio.shield(db.close())
        except BaseException:
            pass
        raise


async def init_db(settings: Settings) -> None:
    now = utc_now()
    db = await connect_db(settings)
    try:
        cursor = await db.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'app_meta'
            """
        )
        meta_exists = await cursor.fetchone() is not None
        existing_version = 0
        if meta_exists:
            cursor = await db.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            )
            version_row = await cursor.fetchone()
        else:
            version_row = None
        if version_row is not None:
            try:
                existing_version = int(version_row["value"])
            except (TypeError, ValueError) as exc:
                raise RuntimeError("database schema_version is invalid") from exc
        if existing_version > SCHEMA_VERSION:
            raise RuntimeError(
                "database schema is newer than this application: "
                f"{existing_version} > {SCHEMA_VERSION}"
            )

        # Version 3 is an additive migration: the schema script creates the
        # durable reader and source-audit tables without rewriting lead data.
        await db.executescript(SCHEMA_SQL)
        freshness_clause = ""
        if existing_version < 3:
            # Older code could import a stale ReadySourceVerification object
            # directly. Fail closed once while crossing the v3 boundary.
            freshness_clause = """
                    AND datetime(v.verified_at) >= datetime('now', '-24 hours')
                    AND datetime(v.verified_at) <= datetime('now', '+5 minutes')
            """
        await db.execute(
            f"""
            UPDATE lead_sources
            SET enabled = 0, updated_at = ?
            WHERE enabled = 1
              AND NOT EXISTS (
                  SELECT 1 FROM source_verifications AS v
                  WHERE v.source_id = lead_sources.id
                    AND v.verified_chat_id = lead_sources.telegram_chat_id
                    AND v.verified_handle = lead_sources.public_handle COLLATE NOCASE
                    AND v.report_schema_version = 1
                    {freshness_clause}
              )
            """,
            (now,),
        )
        await db.execute(
            """
            UPDATE source_checkpoints
            SET reader_status = 'paused', updated_at = ?
            WHERE source_id IN (
                SELECT id FROM lead_sources WHERE enabled = 0
            )
            """,
            (now,),
        )
        await db.execute(
            """
            INSERT INTO app_meta(key, value) VALUES('schema_version', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(SCHEMA_VERSION),),
        )
        await db.execute(
            """
            INSERT INTO intent_rule_sets(
                version, rules_json, review_threshold, lead_threshold,
                enabled, created_at
            ) VALUES(?, ?, ?, ?, 1, ?)
            ON CONFLICT(version) DO UPDATE SET
                rules_json = excluded.rules_json,
                review_threshold = excluded.review_threshold,
                lead_threshold = excluded.lead_threshold,
                enabled = 1
            """,
            (
                DEFAULT_RULESET_VERSION,
                ruleset_json(),
                REVIEW_THRESHOLD,
                LEAD_THRESHOLD,
                now,
            ),
        )
        await db.commit()
    finally:
        await db.close()
