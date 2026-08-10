from __future__ import annotations

import unittest
from types import SimpleNamespace

from reader.identity import (
    TelegramAuthorizationError,
    account_identity_from_user,
    verify_authorized_identity,
)


class FakeClient:
    def __init__(self, *, authorized: bool, user: object | None) -> None:
        self.authorized = authorized
        self.user = user

    async def is_user_authorized(self) -> bool:
        return self.authorized

    async def get_me(self) -> object | None:
        return self.user


class ReaderIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_returned_sign_in_user_can_be_checked_without_an_extra_rpc(self) -> None:
        identity = account_identity_from_user(
            SimpleNamespace(id=777, username="reader", bot=False),
            expected_user_id=777,
        )
        self.assertEqual(identity.user_id, 777)

    async def test_expected_user_is_accepted(self) -> None:
        client = FakeClient(
            authorized=True,
            user=SimpleNamespace(id=777, username="reader", bot=False),
        )
        identity = await verify_authorized_identity(client, expected_user_id=777)
        self.assertEqual(identity.user_id, 777)
        self.assertEqual(identity.username, "reader")

    async def test_wrong_user_is_rejected(self) -> None:
        client = FakeClient(
            authorized=True,
            user=SimpleNamespace(id=888, username=None, bot=False),
        )
        with self.assertRaisesRegex(TelegramAuthorizationError, "expected 777"):
            await verify_authorized_identity(client, expected_user_id=777)

    async def test_bot_session_is_rejected(self) -> None:
        client = FakeClient(
            authorized=True,
            user=SimpleNamespace(id=777, username="bot", bot=True),
        )
        with self.assertRaisesRegex(TelegramAuthorizationError, "not a bot"):
            await verify_authorized_identity(client, expected_user_id=None)

    async def test_unauthorized_session_is_rejected(self) -> None:
        client = FakeClient(authorized=False, user=None)
        with self.assertRaisesRegex(TelegramAuthorizationError, "not authorized"):
            await verify_authorized_identity(client, expected_user_id=None)


if __name__ == "__main__":
    unittest.main()
