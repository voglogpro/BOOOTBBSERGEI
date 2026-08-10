from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublicMessageEvent:
    request_id: str
    telegram_chat_id: int
    telegram_message_id: int
    text: str
    published_at: str
    edited_at: str | None = None
    event_type: str = "new"


@dataclass(frozen=True, slots=True)
class IngestResult:
    result: str
    observation_id: int
    lead_id: int | None
    decision: str
    intent_score: int
    revision: int
    message_url: str
