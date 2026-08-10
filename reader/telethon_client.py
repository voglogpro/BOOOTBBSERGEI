from __future__ import annotations

import os
from pathlib import Path

from telethon import TelegramClient

from reader.settings import ReaderSettings


def build_telegram_client(
    settings: ReaderSettings,
    *,
    receive_updates: bool = False,
) -> TelegramClient:
    """Build the one allowed user client without connecting it."""
    settings.ensure_session_directory()
    client = TelegramClient(
        str(settings.telegram_session_path),
        settings.telegram_api_id,
        settings.telegram_api_hash,
        receive_updates=receive_updates,
        catch_up=False,
        sequential_updates=True,
        flood_sleep_threshold=0,
        request_retries=5,
        connection_retries=5,
        auto_reconnect=True,
        device_model="BibiBike Lead Reader",
        app_version="0.2",
        lang_code="ru",
        system_lang_code="ru",
    )
    client.session.save_entities = False
    if settings.telegram_session_path.exists() and not harden_session_permissions(
        settings.telegram_session_path
    ):
        client.session.close()
        raise PermissionError("could not restrict the Telegram session file")
    return client


def harden_session_permissions(session_path: Path) -> bool:
    """Best-effort owner-only permissions; Windows applies its supported subset."""
    changed = False
    for suffix in ("", "-journal", "-wal", "-shm"):
        path = Path(f"{session_path}{suffix}")
        if not path.exists():
            continue
        try:
            os.chmod(path, 0o600)
            changed = True
        except OSError:
            return False
    return changed
