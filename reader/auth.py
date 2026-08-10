from __future__ import annotations

import getpass
import re

from reader.identity import AccountIdentity, verify_authorized_identity
from reader.settings import ReaderSettings
from reader.telethon_client import build_telegram_client, harden_session_permissions


PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")


def _prompt_phone() -> str:
    raw = input("Номер отдельного Telegram-аккаунта в формате +79991234567: ")
    phone = re.sub(r"[\s()\-]", "", raw.strip())
    if not PHONE_RE.fullmatch(phone):
        raise ValueError("phone must use international format: + and 8-15 digits")
    return phone


def _prompt_code() -> str:
    return getpass.getpass("Код входа из Telegram: ").strip()


def _prompt_password() -> str:
    return getpass.getpass("Пароль двухэтапной аутентификации: ")


async def authorize_interactively(settings: ReaderSettings) -> AccountIdentity:
    """Create or verify a local user session without persisting login prompts."""
    client = build_telegram_client(settings, receive_updates=False)
    try:
        await client.start(
            phone=_prompt_phone,
            code_callback=_prompt_code,
            password=_prompt_password,
        )
        return await verify_authorized_identity(
            client,
            expected_user_id=settings.telegram_expected_user_id,
        )
    finally:
        try:
            await client.disconnect()
        finally:
            hardened = harden_session_permissions(settings.telegram_session_path)
            if settings.telegram_session_path.exists() and not hardened:
                raise PermissionError("could not restrict the Telegram session file")
