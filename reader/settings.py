from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


API_HASH_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class ReaderSettings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_expected_user_id: int | None
    telegram_session_path: Path

    @classmethod
    def from_env(
        cls,
        env_file: str | Path | None = ".env",
        *,
        require_expected_user_id: bool = False,
    ) -> "ReaderSettings":
        if env_file:
            load_dotenv(dotenv_path=env_file, override=False)

        api_id_raw = _required_env("TELEGRAM_API_ID")
        try:
            api_id = int(api_id_raw)
        except ValueError as exc:
            raise ValueError("TELEGRAM_API_ID must be a positive integer") from exc
        if api_id <= 0:
            raise ValueError("TELEGRAM_API_ID must be a positive integer")

        api_hash = _required_env("TELEGRAM_API_HASH")
        if not API_HASH_RE.fullmatch(api_hash):
            raise ValueError("TELEGRAM_API_HASH must contain 32 hexadecimal characters")

        expected_raw = os.getenv("TELEGRAM_EXPECTED_USER_ID", "").strip()
        expected_user_id = int(expected_raw) if expected_raw else None
        if expected_user_id is not None and expected_user_id <= 0:
            raise ValueError("TELEGRAM_EXPECTED_USER_ID must be a positive integer")
        if require_expected_user_id and expected_user_id is None:
            raise ValueError(
                "TELEGRAM_EXPECTED_USER_ID is required after the first authorization"
            )

        session_raw = os.getenv(
            "TELEGRAM_SESSION_PATH", "./data/telegram/reader.session"
        ).strip()
        session_path = Path(session_raw).expanduser().resolve()
        if session_path.suffix != ".session":
            raise ValueError("TELEGRAM_SESSION_PATH must end with .session in lowercase")

        return cls(
            telegram_api_id=api_id,
            telegram_api_hash=api_hash.lower(),
            telegram_expected_user_id=expected_user_id,
            telegram_session_path=session_path,
        )

    def ensure_session_directory(self) -> None:
        directory = self.telegram_session_path.parent
        created = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        if created:
            try:
                os.chmod(directory, 0o700)
            except OSError as exc:
                raise PermissionError(
                    "could not restrict the Telegram session directory"
                ) from exc
        elif os.name != "nt":
            mode = stat.S_IMODE(directory.stat().st_mode)
            if mode & 0o077:
                raise PermissionError(
                    "Telegram session directory must not be accessible to group/others"
                )

    @property
    def telegram_cooldown_path(self) -> Path:
        return Path(f"{self.telegram_session_path}.cooldown.json")
