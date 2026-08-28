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


class AuthorizationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Principal:
    telegram_id: int
    first_name: str
    last_name: str
    username: str
    photo_url: str
    auth_date: int

    @property
    def display_name(self) -> str:
        full_name = " ".join(part for part in (self.first_name, self.last_name) if part)
        return full_name or self.username or f"Трейдер {self.telegram_id}"


def validate_init_data(
    raw_init_data: str,
    *,
    bot_token: str,
    allowed_user_ids: frozenset[int] | set[int] = frozenset(),
    max_age_seconds: int = 86400,
    now: int | None = None,
) -> Principal:
    if not raw_init_data or len(raw_init_data.encode("utf-8")) > MAX_INIT_DATA_BYTES:
        raise AuthorizationError("invalid Telegram authorization data")
    if not bot_token:
        raise AuthorizationError("server authorization is not configured")

    try:
        pairs = parse_qsl(
            raw_init_data,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as exc:
        raise AuthorizationError("invalid Telegram authorization data") from exc
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise AuthorizationError("invalid Telegram authorization data")

    fields = dict(pairs)
    received_hash = fields.pop("hash", "")
    if not HASH_RE.fullmatch(received_hash):
        raise AuthorizationError("invalid Telegram authorization data")
    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode("utf-8"), hashlib.sha256).digest()
    expected_hash = hmac.new(
        secret_key,
        data_check_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise AuthorizationError("invalid Telegram authorization data")

    current_time = int(time.time()) if now is None else int(now)
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        telegram_id = user["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AuthorizationError("invalid Telegram authorization data") from exc
    if (
        isinstance(telegram_id, bool)
        or not isinstance(telegram_id, int)
        or telegram_id <= 0
        or auth_date <= 0
        or auth_date > current_time + MAX_FUTURE_CLOCK_SKEW_SECONDS
        or current_time - auth_date > max_age_seconds
    ):
        raise AuthorizationError("Telegram authorization data has expired or is invalid")
    if allowed_user_ids and telegram_id not in allowed_user_ids:
        raise AuthorizationError("this Telegram account is not allowed")

    def clean(value: object, limit: int) -> str:
        return str(value or "").strip()[:limit]

    return Principal(
        telegram_id=telegram_id,
        first_name=clean(user.get("first_name"), 128),
        last_name=clean(user.get("last_name"), 128),
        username=clean(user.get("username"), 64),
        photo_url=clean(user.get("photo_url"), 1024),
        auth_date=auth_date,
    )


def dev_principal(user_id: int, name: str) -> Principal:
    return Principal(
        telegram_id=user_id,
        first_name=name,
        last_name="",
        username="local_preview",
        photo_url="",
        auth_date=int(time.time()),
    )

