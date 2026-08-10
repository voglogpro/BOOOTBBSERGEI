from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_INIT_DATA_BYTES = 8192
MAX_FUTURE_CLOCK_SKEW_SECONDS = 30


class MiniAppAuthorizationError(ValueError):
    """The Telegram Mini App identity could not be trusted or is not allowed."""


@dataclass(frozen=True, slots=True)
class MiniAppPrincipal:
    user_id: int
    auth_date: int


def validate_miniapp_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    allowed_user_ids: frozenset[int] | set[int],
    max_age_seconds: int = 300,
    now: int | None = None,
) -> MiniAppPrincipal:
    """Validate Telegram.WebApp.initData and return an allowlisted identity."""
    if not raw_init_data or len(raw_init_data.encode("utf-8")) > MAX_INIT_DATA_BYTES:
        raise MiniAppAuthorizationError("invalid Telegram authorization data")
    if not bot_token:
        raise MiniAppAuthorizationError("server authorization is not configured")

    try:
        pairs = parse_qsl(
            raw_init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as exc:
        raise MiniAppAuthorizationError(
            "invalid Telegram authorization data"
        ) from exc

    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise MiniAppAuthorizationError("invalid Telegram authorization data")
    fields = dict(pairs)

    received_hash = fields.pop("hash", "")
    if not HASH_RE.fullmatch(received_hash):
        raise MiniAppAuthorizationError("invalid Telegram authorization data")

    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    secret_key = hmac.new(
        b"WebAppData",
        bot_token.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise MiniAppAuthorizationError("invalid Telegram authorization data")

    try:
        auth_date = int(fields["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MiniAppAuthorizationError(
            "invalid Telegram authorization data"
        ) from exc

    current_time = int(time.time()) if now is None else int(now)
    if (
        auth_date <= 0
        or auth_date > current_time + MAX_FUTURE_CLOCK_SKEW_SECONDS
        or current_time - auth_date > max_age_seconds
    ):
        raise MiniAppAuthorizationError("Telegram authorization data has expired")

    try:
        user = json.loads(fields["user"])
        user_id = user["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MiniAppAuthorizationError(
            "invalid Telegram authorization data"
        ) from exc
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        raise MiniAppAuthorizationError("invalid Telegram authorization data")
    if user_id not in allowed_user_ids:
        raise MiniAppAuthorizationError("administrator access is required")

    return MiniAppPrincipal(user_id=user_id, auth_date=auth_date)
