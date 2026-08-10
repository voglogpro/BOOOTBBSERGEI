from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class TelegramAuthorizationError(RuntimeError):
    """The session is missing, belongs to the wrong account or is a bot."""


@dataclass(frozen=True, slots=True)
class AccountIdentity:
    user_id: int
    username: str | None


def account_identity_from_user(
    user: Any,
    *,
    expected_user_id: int | None,
) -> AccountIdentity:
    if user is None or not getattr(user, "id", None):
        raise TelegramAuthorizationError("Telegram did not return the account identity")
    if bool(getattr(user, "bot", False)):
        raise TelegramAuthorizationError(
            "The reader requires a separate user account, not a bot session"
        )

    user_id = int(user.id)
    if expected_user_id is not None and user_id != expected_user_id:
        raise TelegramAuthorizationError(
            "Telegram session belongs to an unexpected account: "
            f"expected {expected_user_id}, got {user_id}"
        )

    username_raw = getattr(user, "username", None)
    username = str(username_raw) if username_raw else None
    return AccountIdentity(user_id=user_id, username=username)


async def verify_authorized_identity(
    client: Any,
    *,
    expected_user_id: int | None,
) -> AccountIdentity:
    if not await client.is_user_authorized():
        raise TelegramAuthorizationError(
            "Telegram session is not authorized; run the authorize command locally"
        )

    me = await client.get_me()
    return account_identity_from_user(me, expected_user_id=expected_user_id)
