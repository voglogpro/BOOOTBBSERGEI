from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


TOKEN_ENV_NAMES = ("BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "API_TOKEN", "TOKEN")


def _bot_token() -> str:
    for name in TOKEN_ENV_NAMES:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _ids(value: str | None) -> frozenset[int]:
    if not value or not value.strip():
        return frozenset()
    result: set[int] = set()
    for item in value.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        parsed = int(cleaned)
        if parsed <= 0:
            raise ValueError("ALLOWED_USER_IDS must contain positive integers")
        result.add(parsed)
    return frozenset(result)


@dataclass(frozen=True, slots=True)
class Settings:
    bot_token: str
    database_path: Path
    port: int = 8080
    max_auth_age_seconds: int = 86400
    allowed_user_ids: frozenset[int] = frozenset()
    dev_mode: bool = False
    dev_user_id: int = 100001
    dev_user_name: str = "Тестовый трейдер"

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        dev_mode = _bool(os.getenv("DEV_MODE"))
        bot_token = _bot_token()
        if not dev_mode and not bot_token:
            names = ", ".join(TOKEN_ENV_NAMES)
            raise RuntimeError(
                f"Telegram bot token is required. Set one of: {names}"
            )

        database_value = os.getenv("DATABASE_PATH", "").strip()
        if database_value:
            database_path = Path(database_value)
        elif Path("/app/data").exists():
            database_path = Path("/app/data/trader_journal.sqlite3")
        else:
            database_path = Path("./data/trader_journal.sqlite3")

        port = int(os.getenv("PORT", "8080"))
        max_age = int(os.getenv("MAX_AUTH_AGE_SECONDS", "86400"))
        dev_user_id = int(os.getenv("DEV_USER_ID", "100001"))
        if not 1 <= port <= 65535:
            raise ValueError("PORT must be between 1 and 65535")
        if max_age < 60:
            raise ValueError("MAX_AUTH_AGE_SECONDS must be at least 60")
        if dev_user_id <= 0:
            raise ValueError("DEV_USER_ID must be positive")

        return cls(
            bot_token=bot_token,
            database_path=database_path,
            port=port,
            max_auth_age_seconds=max_age,
            allowed_user_ids=_ids(os.getenv("ALLOWED_USER_IDS")),
            dev_mode=dev_mode,
            dev_user_id=dev_user_id,
            dev_user_name=os.getenv("DEV_USER_NAME", "Тестовый трейдер").strip()
            or "Тестовый трейдер",
        )
