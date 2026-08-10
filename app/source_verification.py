from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


RESOLUTION_REPORT_SCHEMA_VERSION = 1
MAX_REPORT_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)


class ResolutionReportError(ValueError):
    """A resolver report cannot authorize a runtime source."""


@dataclass(frozen=True, slots=True)
class ReadySourceVerification:
    handle: str
    title: str
    telegram_chat_id: int
    account_user_id: int
    checked_at: str
    report_schema_version: int


def load_ready_source_verification(
    path: str | Path,
    *,
    handle: str,
    now: datetime | None = None,
) -> ReadySourceVerification:
    report_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ResolutionReportError(f"resolution report not found: {report_path}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResolutionReportError("resolution report must be valid UTF-8 JSON") from exc

    report = _mapping(raw, "report")
    schema_version = _integer(report.get("schema_version"), "schema_version")
    if schema_version != RESOLUTION_REPORT_SCHEMA_VERSION:
        raise ResolutionReportError(
            f"unsupported resolution report schema: {schema_version}"
        )

    account_user_id = _integer(report.get("account_user_id"), "account_user_id")
    if account_user_id <= 0:
        raise ResolutionReportError("account_user_id must be positive")

    checked_at = _timestamp(report.get("checked_at"), "checked_at")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if checked_at > current + MAX_FUTURE_SKEW:
        raise ResolutionReportError("resolution report timestamp is in the future")
    if current - checked_at > MAX_REPORT_AGE:
        raise ResolutionReportError("resolution report is older than 24 hours")

    requested = handle.strip().removeprefix("@").casefold()
    if not requested:
        raise ResolutionReportError("handle is required")
    sources = _list(report.get("sources"), "sources")
    matches = [
        _mapping(item, f"sources[{index}]")
        for index, item in enumerate(sources)
        if isinstance(item, dict)
        and str(item.get("handle", "")).strip().casefold() == requested
    ]
    if len(matches) != 1:
        raise ResolutionReportError(
            f"report must contain exactly one source for @{requested}"
        )

    source = matches[0]
    if source.get("status") != "ready":
        raise ResolutionReportError(f"@{requested} does not have ready status")
    canonical_handle = _text(source.get("canonical_handle"), "canonical_handle")
    if canonical_handle.casefold() != requested:
        raise ResolutionReportError("canonical_handle does not match requested handle")
    telegram_chat_id = _integer(
        source.get("telegram_chat_id"), "telegram_chat_id"
    )
    if telegram_chat_id >= -1_000_000_000_000:
        raise ResolutionReportError(
            "telegram_chat_id must be a marked -100... supergroup ID"
        )
    title = _text(
        source.get("telegram_title") or source.get("title"),
        "telegram_title",
    )

    return ReadySourceVerification(
        handle=canonical_handle,
        title=title,
        telegram_chat_id=telegram_chat_id,
        account_user_id=account_user_id,
        checked_at=checked_at.isoformat(timespec="seconds"),
        report_schema_version=schema_version,
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ResolutionReportError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ResolutionReportError(f"{field} must be an array")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResolutionReportError(f"{field} must be a non-empty string")
    return value.strip()


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ResolutionReportError(f"{field} must be an integer")
    return value


def _timestamp(value: Any, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ResolutionReportError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ResolutionReportError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)
