from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import aiosqlite

from app.cities import detect_city
from app.config import Settings
from app.db import connect_db, utc_now
from app.models import IngestResult, PublicMessageEvent
from app.rules import classify_intent


class IngestionError(ValueError):
    """Base error for a rejected ingestion request."""


class SourceNotAllowed(IngestionError):
    """The event did not come from an enabled allowlisted source."""


class IdempotencyConflict(IngestionError):
    """One request ID was reused for a different payload."""


class CollectorDisabled(IngestionError):
    """The global collector kill switch is off."""


def _canonical_time(value: str, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise IngestionError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise IngestionError(f"{field} must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _validate_event(event: PublicMessageEvent) -> PublicMessageEvent:
    request_id = event.request_id.strip()
    if not request_id or len(request_id) > 200:
        raise IngestionError("request_id must contain 1-200 characters")
    if event.telegram_chat_id >= -1_000_000_000_000:
        raise IngestionError("telegram_chat_id must be a marked -100... ID")
    if event.telegram_message_id <= 0:
        raise IngestionError("telegram_message_id must be positive")
    text = event.text.strip()
    if not 1 <= len(text) <= 8192:
        raise IngestionError("text must contain 1-8192 characters")
    if event.event_type not in {"new", "edited"}:
        raise IngestionError("event_type must be new or edited")
    return PublicMessageEvent(
        request_id=request_id,
        telegram_chat_id=event.telegram_chat_id,
        telegram_message_id=event.telegram_message_id,
        text=text,
        published_at=_canonical_time(event.published_at, "published_at"),
        edited_at=(
            _canonical_time(event.edited_at, "edited_at") if event.edited_at else None
        ),
        event_type=event.event_type,
    )


def _payload_hash(event: PublicMessageEvent) -> str:
    payload = json.dumps(
        {
            "chat_id": event.telegram_chat_id,
            "message_id": event.telegram_message_id,
            "text": event.text,
            "published_at": event.published_at,
            "edited_at": event.edited_at,
            "event_type": event.event_type,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _content_hash(event: PublicMessageEvent) -> str:
    return hashlib.sha256(event.text.encode("utf-8")).hexdigest()


async def _result_for_request(
    db: aiosqlite.Connection, request_id: str, payload_hash: str
) -> IngestResult | None:
    cursor = await db.execute(
        """
        SELECT ir.payload_hash, ir.result, ir.observation_id, ir.lead_id,
               o.decision, o.intent_score, o.revision, o.message_url
        FROM ingest_requests AS ir
        JOIN message_observations AS o ON o.id = ir.observation_id
        WHERE ir.request_id = ?
        """,
        (request_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return None
    if row["payload_hash"] != payload_hash:
        raise IdempotencyConflict("request_id was already used for another payload")
    return IngestResult(
        result=str(row["result"]),
        observation_id=int(row["observation_id"]),
        lead_id=int(row["lead_id"]) if row["lead_id"] is not None else None,
        decision=str(row["decision"]),
        intent_score=int(row["intent_score"]),
        revision=int(row["revision"]),
        message_url=str(row["message_url"]),
    )


async def ingest_public_message(
    settings: Settings, event: PublicMessageEvent
) -> IngestResult:
    if not settings.collector_enabled:
        raise CollectorDisabled("collector is disabled by COLLECTOR_ENABLED=false")
    event = _validate_event(event)
    payload_hash = _payload_hash(event)
    content_hash = _content_hash(event)
    intent = classify_intent(event.text)
    now = utc_now()

    db = await connect_db(settings)
    try:
        await db.execute("BEGIN IMMEDIATE")
        prior_request = await _result_for_request(db, event.request_id, payload_hash)
        if prior_request:
            await db.commit()
            return prior_request

        cursor = await db.execute(
            """
            SELECT id, public_handle, default_city_id
            FROM lead_sources
            WHERE telegram_chat_id = ? AND enabled = 1
            """,
            (event.telegram_chat_id,),
        )
        source = await cursor.fetchone()
        if not source:
            raise SourceNotAllowed("source is not present in the enabled allowlist")

        cursor = await db.execute(
            """
            SELECT id, name, aliases_json
            FROM market_cities
            WHERE enabled = 1
            ORDER BY length(name) DESC
            """
        )
        city_rows = await cursor.fetchall()
        city = detect_city(
            event.text,
            city_rows,
            default_city_id=(
                int(source["default_city_id"])
                if source["default_city_id"] is not None
                else None
            ),
        )
        city_id = city.city_id if city else None
        city_confidence = city.confidence if city else None
        message_url = (
            f"https://t.me/{source['public_handle']}/{event.telegram_message_id}"
        )
        purge_after = None
        if intent.decision == "rejected":
            purge_after = (
                datetime.now(UTC)
                + timedelta(days=settings.rejected_message_retention_days)
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

        cursor = await db.execute(
            """
            SELECT * FROM message_observations
            WHERE source_id = ? AND telegram_message_id = ?
            """,
            (int(source["id"]), event.telegram_message_id),
        )
        existing = await cursor.fetchone()

        if existing and existing["content_hash"] == content_hash:
            observation_id = int(existing["id"])
            cursor = await db.execute(
                "SELECT id FROM franchise_leads WHERE observation_id = ?",
                (observation_id,),
            )
            lead_row = await cursor.fetchone()
            lead_id = int(lead_row["id"]) if lead_row else None
            await db.execute(
                """
                INSERT INTO ingest_requests(
                    request_id, payload_hash, observation_id, lead_id, result, created_at
                ) VALUES(?, ?, ?, ?, 'duplicate', ?)
                """,
                (event.request_id, payload_hash, observation_id, lead_id, now),
            )
            await db.commit()
            return IngestResult(
                result="duplicate",
                observation_id=observation_id,
                lead_id=lead_id,
                decision=str(existing["decision"]),
                intent_score=int(existing["intent_score"]),
                revision=int(existing["revision"]),
                message_url=str(existing["message_url"]),
            )

        if existing:
            observation_id = int(existing["id"])
            revision = int(existing["revision"]) + 1
            previous_decision = str(existing["decision"])
            await db.execute(
                """
                UPDATE message_observations SET
                    message_url = ?, message_text = ?, published_at = ?,
                    edited_at = ?, observed_at = ?, content_hash = ?,
                    revision = ?, decision = ?, intent_score = ?,
                    matched_rules_json = ?, rule_version = ?,
                    detected_city_id = ?, city_confidence = ?, purge_after = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    message_url,
                    event.text,
                    event.published_at,
                    event.edited_at,
                    now,
                    content_hash,
                    revision,
                    intent.decision,
                    intent.score,
                    intent.matched_rules_json,
                    intent.rules_version,
                    city_id,
                    city_confidence,
                    purge_after,
                    now,
                    observation_id,
                ),
            )
            result_kind = "updated"
        else:
            revision = 1
            previous_decision = None
            cursor = await db.execute(
                """
                INSERT INTO message_observations(
                    source_id, telegram_message_id, message_url, message_text,
                    published_at, edited_at, observed_at, content_hash, revision,
                    decision, intent_score, matched_rules_json, rule_version,
                    detected_city_id, city_confidence, purge_after,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(source["id"]),
                    event.telegram_message_id,
                    message_url,
                    event.text,
                    event.published_at,
                    event.edited_at,
                    now,
                    content_hash,
                    revision,
                    intent.decision,
                    intent.score,
                    intent.matched_rules_json,
                    intent.rules_version,
                    city_id,
                    city_confidence,
                    purge_after,
                    now,
                    now,
                ),
            )
            observation_id = int(cursor.lastrowid)
            result_kind = "created"

        cursor = await db.execute(
            "SELECT id, status FROM franchise_leads WHERE observation_id = ?",
            (observation_id,),
        )
        existing_lead = await cursor.fetchone()
        lead_id: int | None = None

        if existing_lead:
            lead_id = int(existing_lead["id"])
            await db.execute(
                """
                UPDATE franchise_leads SET
                    detected_city_id = ?, intent_score = ?, matched_rules_json = ?,
                    rule_version = ?, needs_review = 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    city_id,
                    intent.score,
                    intent.matched_rules_json,
                    intent.rules_version,
                    now,
                    lead_id,
                ),
            )
            await db.execute(
                """
                INSERT INTO lead_events(
                    lead_id, event_type, actor_kind, details_json, created_at
                ) VALUES(?, 'message_reclassified', 'reader', ?, ?)
                """,
                (
                    lead_id,
                    json.dumps(
                        {
                            "previous_decision": previous_decision,
                            "decision": intent.decision,
                            "score": intent.score,
                            "revision": revision,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )
        elif intent.decision in {"lead", "review"}:
            initial_status = "new" if intent.decision == "lead" else "reviewing"
            cursor = await db.execute(
                """
                INSERT INTO franchise_leads(
                    observation_id, status, detected_city_id, intent_score,
                    matched_rules_json, rule_version, needs_review,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?)
                """,
                (
                    observation_id,
                    initial_status,
                    city_id,
                    intent.score,
                    intent.matched_rules_json,
                    intent.rules_version,
                    now,
                    now,
                ),
            )
            lead_id = int(cursor.lastrowid)
            await db.execute(
                """
                INSERT INTO lead_events(
                    lead_id, event_type, to_status, actor_kind,
                    details_json, created_at
                ) VALUES(?, 'lead_created', ?, 'system', ?, ?)
                """,
                (
                    lead_id,
                    initial_status,
                    json.dumps(
                        {"decision": intent.decision, "score": intent.score},
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    now,
                ),
            )

        await db.execute(
            """
            INSERT INTO ingest_requests(
                request_id, payload_hash, observation_id, lead_id, result, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                event.request_id,
                payload_hash,
                observation_id,
                lead_id,
                result_kind,
                now,
            ),
        )
        await db.commit()
        return IngestResult(
            result=result_kind,
            observation_id=observation_id,
            lead_id=lead_id,
            decision=intent.decision,
            intent_score=intent.score,
            revision=revision,
            message_url=message_url,
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
