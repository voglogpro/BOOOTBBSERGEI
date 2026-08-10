from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    value = default if raw is None or not raw.strip() else int(raw)
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    app_env: str
    data_dir: Path
    database_path: Path
    timezone: str
    collector_enabled: bool
    rejected_message_retention_days: int
    reader_catchup_limit: int
    reader_queue_max: int

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)

        data_dir = Path(os.getenv("DATA_DIR", "./data")).expanduser().resolve()
        database_raw = os.getenv("DATABASE_PATH", "").strip()
        database_path = (
            Path(database_raw).expanduser().resolve()
            if database_raw
            else data_dir / "leads.sqlite3"
        )

        return cls(
            app_env=os.getenv("APP_ENV", "development").strip() or "development",
            data_dir=data_dir,
            database_path=database_path,
            timezone=os.getenv("TIMEZONE", "Europe/Moscow").strip()
            or "Europe/Moscow",
            collector_enabled=_env_bool("COLLECTOR_ENABLED", False),
            rejected_message_retention_days=_env_int(
                "REJECTED_MESSAGE_RETENTION_DAYS",
                7,
                minimum=1,
                maximum=365,
            ),
            reader_catchup_limit=_env_int(
                "READER_CATCHUP_LIMIT", 0, minimum=0, maximum=0
            ),
            reader_queue_max=_env_int(
                "READER_QUEUE_MAX", 10_000, minimum=100, maximum=100_000
            ),
        )

    def ensure_runtime_directories(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
