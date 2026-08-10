from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from telethon import errors, types, utils

from app.source_candidates import SourceCandidate


RESOLUTION_SCHEMA_VERSION = 1
MAX_RESOLVE_BATCH = 10


@dataclass(frozen=True, slots=True)
class SourceResolution:
    handle: str
    title: str
    priority: str
    status: str
    reason: str
    telegram_chat_id: int | None = None
    canonical_handle: str | None = None
    telegram_title: str | None = None
    retry_after_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class SourceResolutionReport:
    schema_version: int
    checked_at: str
    account_user_id: int
    sources: tuple[SourceResolution, ...]


def _resolve_entity(
    candidate: SourceCandidate,
    entity: Any,
    *,
    is_channel: Callable[[Any], bool],
    peer_id: Callable[[Any], int],
) -> SourceResolution:
    common = {
        "handle": candidate.handle,
        "title": candidate.title,
        "priority": candidate.priority,
    }
    if not is_channel(entity) or not bool(getattr(entity, "megagroup", False)):
        return SourceResolution(
            **common,
            status="not_public_group",
            reason="Telegram entity is not a public discussion group",
        )

    canonical_raw = getattr(entity, "username", None)
    canonical_handle = str(canonical_raw).strip() if canonical_raw else None
    if not canonical_handle:
        return SourceResolution(
            **common,
            status="not_public_group",
            reason="Group does not have a public username",
        )
    if canonical_handle.casefold() != candidate.handle.casefold():
        return SourceResolution(
            **common,
            status="username_mismatch",
            reason="Resolved username does not match the reviewed catalog entry",
            canonical_handle=canonical_handle,
        )

    telegram_title_raw = getattr(entity, "title", None)
    telegram_title = str(telegram_title_raw).strip() if telegram_title_raw else None
    if bool(getattr(entity, "forum", False)):
        return SourceResolution(
            **common,
            status="unsupported_forum",
            reason="Forum topics are excluded until topic links are implemented",
            canonical_handle=canonical_handle,
            telegram_title=telegram_title,
        )
    if bool(getattr(entity, "kicked", False)):
        return SourceResolution(
            **common,
            status="unavailable",
            reason="Account is banned from this group",
            canonical_handle=canonical_handle,
            telegram_title=telegram_title,
        )
    if bool(getattr(entity, "left", False)):
        return SourceResolution(
            **common,
            status="not_joined",
            reason="Join this public group manually in the official Telegram app",
            canonical_handle=canonical_handle,
            telegram_title=telegram_title,
        )

    marked_id = int(peer_id(entity))
    if marked_id >= -1_000_000_000_000:
        return SourceResolution(
            **common,
            status="unavailable",
            reason="Telegram returned an invalid marked supergroup ID",
            canonical_handle=canonical_handle,
            telegram_title=telegram_title,
        )
    return SourceResolution(
        **common,
        status="ready",
        reason="Public group is already joined; ID resolved without activation",
        telegram_chat_id=marked_id,
        canonical_handle=canonical_handle,
        telegram_title=telegram_title,
    )


async def resolve_source_candidates(
    client: Any,
    candidates: Iterable[SourceCandidate],
    *,
    _is_channel: Callable[[Any], bool] | None = None,
    _peer_id: Callable[[Any], int] | None = None,
) -> tuple[SourceResolution, ...]:
    """Resolve public groups sequentially without joining or reading messages."""

    selected = tuple(candidates)
    if len(selected) > MAX_RESOLVE_BATCH:
        raise ValueError(
            f"resolve batch is limited to {MAX_RESOLVE_BATCH} reviewed usernames"
        )
    is_channel = _is_channel or (lambda entity: isinstance(entity, types.Channel))
    peer_id = _peer_id or utils.get_peer_id
    results: list[SourceResolution] = []

    for position, candidate in enumerate(selected):
        try:
            entity = await client.get_entity(candidate.handle)
        except errors.FloodError as exc:
            seconds_raw = getattr(exc, "seconds", None)
            seconds = (
                int(seconds_raw)
                if isinstance(seconds_raw, (int, float)) and seconds_raw > 0
                else 3600
            )
            pause = f"{seconds} seconds"
            results.append(
                SourceResolution(
                    handle=candidate.handle,
                    title=candidate.title,
                    priority=candidate.priority,
                    status="rate_limited",
                    reason=f"Telegram requested a pause of {pause}",
                    retry_after_seconds=seconds,
                )
            )
            for skipped in selected[position + 1 :]:
                results.append(
                    SourceResolution(
                        handle=skipped.handle,
                        title=skipped.title,
                        priority=skipped.priority,
                        status="skipped_after_rate_limit",
                        reason="No further Telegram requests were made",
                    )
                )
            break
        except (errors.RPCError, ValueError, TypeError, TimeoutError, OSError) as exc:
            results.append(
                SourceResolution(
                    handle=candidate.handle,
                    title=candidate.title,
                    priority=candidate.priority,
                    status="unavailable",
                    reason=f"{type(exc).__name__}: source could not be resolved",
                )
            )
            continue

        results.append(
            _resolve_entity(
                candidate,
                entity,
                is_channel=is_channel,
                peer_id=peer_id,
            )
        )

    return tuple(results)


def build_resolution_report(
    *,
    account_user_id: int,
    sources: Iterable[SourceResolution],
    checked_at: datetime | None = None,
) -> SourceResolutionReport:
    timestamp = checked_at or datetime.now(timezone.utc)
    return SourceResolutionReport(
        schema_version=RESOLUTION_SCHEMA_VERSION,
        checked_at=timestamp.astimezone(timezone.utc).isoformat(timespec="seconds"),
        account_user_id=account_user_id,
        sources=tuple(sources),
    )


def write_resolution_report(
    report: SourceResolutionReport,
    path: str | Path,
) -> Path:
    output_path = Path(path).expanduser().resolve()
    if output_path.suffix.lower() != ".json":
        raise ValueError("resolution report path must end with .json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = asdict(report)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return output_path
