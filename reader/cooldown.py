from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path


COOLDOWN_SCHEMA_VERSION = 1


class TelegramCooldownError(RuntimeError):
    """Telegram requests are blocked until the recorded FloodWait expires."""


def _read_retry_not_before(path: str | Path) -> datetime | None:
    cooldown_path = Path(path).expanduser().resolve()
    if not cooldown_path.exists():
        return None
    try:
        payload = json.loads(cooldown_path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != COOLDOWN_SCHEMA_VERSION:
            raise ValueError("unsupported schema")
        raw = payload["retry_not_before"]
        retry_not_before = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if retry_not_before.tzinfo is None:
            raise ValueError("timezone is missing")
        return retry_not_before.astimezone(timezone.utc)
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TelegramCooldownError(
            "Telegram cooldown file is invalid; inspect it before making requests"
        ) from exc


def cooldown_remaining_seconds(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> int:
    retry_not_before = _read_retry_not_before(path)
    if retry_not_before is None:
        return 0
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0, math.ceil((retry_not_before - current).total_seconds()))


def record_cooldown(
    path: str | Path,
    *,
    seconds: int,
    now: datetime | None = None,
) -> datetime:
    if seconds <= 0:
        raise ValueError("cooldown seconds must be positive")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retry_not_before = current + timedelta(seconds=seconds)
    output = Path(path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": COOLDOWN_SCHEMA_VERSION,
        "recorded_at": current.isoformat(timespec="seconds"),
        "retry_not_before": retry_not_before.isoformat(timespec="seconds"),
    }
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(output)
    return retry_not_before


def enforce_cooldown(
    path: str | Path,
    *,
    now: datetime | None = None,
) -> None:
    retry_not_before = _read_retry_not_before(path)
    if retry_not_before is None:
        return
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current < retry_not_before:
        raise TelegramCooldownError(
            "Telegram requests are paused until "
            + retry_not_before.isoformat(timespec="seconds")
        )
