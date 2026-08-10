from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def _positive_int(name: str, raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _bounded_int(name: str, raw: str, *, minimum: int, maximum: int) -> int:
    value = _positive_int(name, raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _admin_ids(raw: str) -> frozenset[int]:
    parts = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if not parts:
        raise ValueError("ADMIN_TELEGRAM_IDS must contain at least one user ID")
    values = frozenset(_positive_int("ADMIN_TELEGRAM_IDS", part) for part in parts)
    return values


@dataclass(frozen=True, slots=True)
class ServerSettings:
    bot_token: str
    admin_telegram_ids: frozenset[int]
    telegram_api_id: int
    telegram_api_hash: str
    telegram_expected_user_id: int
    session_encryption_key: str
    encrypted_session_path: Path
    host: str
    port: int
    init_data_max_age_seconds: int
    login_challenge_ttl_seconds: int

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "ServerSettings":
        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)

        api_hash = _required_env("TELEGRAM_API_HASH")
        if not API_HASH_RE.fullmatch(api_hash):
            raise ValueError(
                "TELEGRAM_API_HASH must contain 32 hexadecimal characters"
            )

        session_path = Path(
            os.getenv(
                "ENCRYPTED_SESSION_PATH",
                "./data/telegram/reader.session.enc",
            ).strip()
        ).expanduser().resolve()
        if session_path.suffix.lower() != ".enc":
            raise ValueError("ENCRYPTED_SESSION_PATH must end with .enc")

        host = os.getenv("APP_HOST", "0.0.0.0").strip()
        if not host:
            raise ValueError("APP_HOST cannot be empty")

        port_raw = os.getenv("PORT", os.getenv("APP_PORT", "8080")).strip()
        port = _bounded_int("PORT", port_raw, minimum=1, maximum=65535)

        return cls(
            bot_token=_required_env("BOT_TOKEN"),
            admin_telegram_ids=_admin_ids(_required_env("ADMIN_TELEGRAM_IDS")),
            telegram_api_id=_positive_int(
                "TELEGRAM_API_ID", _required_env("TELEGRAM_API_ID")
            ),
            telegram_api_hash=api_hash.lower(),
            telegram_expected_user_id=_positive_int(
                "TELEGRAM_EXPECTED_USER_ID",
                _required_env("TELEGRAM_EXPECTED_USER_ID"),
            ),
            session_encryption_key=_required_env("SESSION_ENCRYPTION_KEY"),
            encrypted_session_path=session_path,
            host=host,
            port=port,
            init_data_max_age_seconds=_bounded_int(
                "MINIAPP_INIT_DATA_MAX_AGE_SECONDS",
                os.getenv("MINIAPP_INIT_DATA_MAX_AGE_SECONDS", "300").strip(),
                minimum=60,
                maximum=3600,
            ),
            login_challenge_ttl_seconds=_bounded_int(
                "LOGIN_CHALLENGE_TTL_SECONDS",
                os.getenv("LOGIN_CHALLENGE_TTL_SECONDS", "300").strip(),
                minimum=60,
                maximum=900,
            ),
        )
