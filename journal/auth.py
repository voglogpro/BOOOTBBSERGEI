from __future__ import annotations

import hashlib
import hmac
import base64
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


HASH_RE = re.compile(r"^[0-9a-fA-F]{64}$")
MAX_INIT_DATA_BYTES = 8192
MAX_FUTURE_CLOCK_SKEW_SECONDS = 30
MAX_SESSION_BYTES = 4096


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


def validate_login_data(
    values: dict[str, str],
    *,
    bot_token: str,
    allowed_user_ids: frozenset[int] | set[int] = frozenset(),
    max_age_seconds: int = 86400,
    now: int | None = None,
) -> Principal:
    """Validate data returned by the Telegram website login widget."""
    fields = {key: str(value) for key, value in values.items() if value is not None}
    received_hash = fields.pop("hash", "")
    if not bot_token or not HASH_RE.fullmatch(received_hash):
        raise AuthorizationError("invalid Telegram login data")
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items())
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected_hash = hmac.new(
        secret_key, data_check_string.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected_hash, received_hash.lower()):
        raise AuthorizationError("invalid Telegram login data")

    current_time = int(time.time()) if now is None else int(now)
    try:
        telegram_id = int(fields["id"])
        auth_date = int(fields["auth_date"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthorizationError("invalid Telegram login data") from exc
    if (
        telegram_id <= 0
        or auth_date <= 0
        or auth_date > current_time + MAX_FUTURE_CLOCK_SKEW_SECONDS
        or current_time - auth_date > max_age_seconds
    ):
        raise AuthorizationError("Telegram login data has expired or is invalid")
    if allowed_user_ids and telegram_id not in allowed_user_ids:
        raise AuthorizationError("this Telegram account is not allowed")

    def clean(key: str, limit: int) -> str:
        return str(fields.get(key, "")).strip()[:limit]

    return Principal(
        telegram_id=telegram_id,
        first_name=clean("first_name", 128),
        last_name=clean("last_name", 128),
        username=clean("username", 64),
        photo_url=clean("photo_url", 1024),
        auth_date=auth_date,
    )


def _session_key(bot_token: str) -> bytes:
    return hmac.new(
        bot_token.encode("utf-8"), b"trader-journal-web-session-v1", hashlib.sha256
    ).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_web_session(
    principal: Principal,
    *,
    bot_token: str,
    lifetime_seconds: int = 30 * 86400,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    payload = {
        "id": principal.telegram_id,
        "first_name": principal.first_name,
        "last_name": principal.last_name,
        "username": principal.username,
        "photo_url": principal.photo_url,
        "iat": issued_at,
        "exp": issued_at + lifetime_seconds,
    }
    encoded = _b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    signature = _b64encode(
        hmac.new(_session_key(bot_token), encoded.encode("ascii"), hashlib.sha256).digest()
    )
    return f"{encoded}.{signature}"


def validate_web_session(
    token: str,
    *,
    bot_token: str,
    allowed_user_ids: frozenset[int] | set[int] = frozenset(),
    now: int | None = None,
) -> Principal:
    if not token or not bot_token or len(token.encode("utf-8")) > MAX_SESSION_BYTES:
        raise AuthorizationError("invalid web session")
    try:
        encoded, received_signature = token.split(".", 1)
        expected_signature = _b64encode(
            hmac.new(
                _session_key(bot_token), encoded.encode("ascii"), hashlib.sha256
            ).digest()
        )
        if not hmac.compare_digest(expected_signature, received_signature):
            raise AuthorizationError("invalid web session")
        payload = json.loads(_b64decode(encoded))
        telegram_id = int(payload["id"])
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
    except AuthorizationError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError) as exc:
        raise AuthorizationError("invalid web session") from exc

    current_time = int(time.time()) if now is None else int(now)
    if (
        telegram_id <= 0
        or issued_at <= 0
        or issued_at > current_time + MAX_FUTURE_CLOCK_SKEW_SECONDS
        or expires_at <= current_time
        or expires_at - issued_at > 31 * 86400
    ):
        raise AuthorizationError("web session has expired or is invalid")
    if allowed_user_ids and telegram_id not in allowed_user_ids:
        raise AuthorizationError("this Telegram account is not allowed")

    def clean(key: str, limit: int) -> str:
        return str(payload.get(key, "")).strip()[:limit]

    return Principal(
        telegram_id=telegram_id,
        first_name=clean("first_name", 128),
        last_name=clean("last_name", 128),
        username=clean("username", 64),
        photo_url=clean("photo_url", 1024),
        auth_date=issued_at,
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
