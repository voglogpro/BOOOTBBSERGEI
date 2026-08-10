from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reader.auth import _prompt_phone, authorize_interactively
from reader.identity import TelegramAuthorizationError
from reader.settings import ReaderSettings


class FakeAuthClient:
    def __init__(self, user_id: int = 777) -> None:
        self.user_id = user_id
        self.disconnected = False
        self.start_callbacks: tuple[object, object, object] | None = None

    async def start(self, *, phone: object, code_callback: object, password: object) -> None:
        self.start_callbacks = (phone, code_callback, password)

    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self) -> object:
        return SimpleNamespace(id=self.user_id, username="reader", bot=False)

    async def disconnect(self) -> None:
        self.disconnected = True


class ReaderAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    def _settings(self, root: str, expected_user_id: int | None) -> ReaderSettings:
        return ReaderSettings(
            telegram_api_id=123456,
            telegram_api_hash="0123456789abcdef0123456789abcdef",
            telegram_expected_user_id=expected_user_id,
            telegram_session_path=Path(root) / "reader.session",
        )

    async def test_existing_authorized_session_does_not_prompt_in_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeAuthClient()
            with (
                patch("reader.auth.build_telegram_client", return_value=client),
                patch("reader.auth._prompt_phone", side_effect=AssertionError("prompted")),
                patch("reader.auth._prompt_code", side_effect=AssertionError("prompted")),
                patch("reader.auth._prompt_password", side_effect=AssertionError("prompted")),
                patch("reader.auth.harden_session_permissions"),
            ):
                identity = await authorize_interactively(
                    self._settings(temp_dir, expected_user_id=777)
                )

        self.assertEqual(identity.user_id, 777)
        self.assertTrue(client.disconnected)

    async def test_disconnects_and_hardens_when_identity_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = FakeAuthClient(user_id=888)
            with (
                patch("reader.auth.build_telegram_client", return_value=client),
                patch("reader.auth.harden_session_permissions") as harden,
            ):
                with self.assertRaises(TelegramAuthorizationError):
                    await authorize_interactively(
                        self._settings(temp_dir, expected_user_id=777)
                    )

        self.assertTrue(client.disconnected)
        harden.assert_called_once()


class PhonePromptTests(unittest.TestCase):
    def test_phone_is_normalized(self) -> None:
        with patch("builtins.input", return_value="+7 (999) 123-45-67"):
            self.assertEqual(_prompt_phone(), "+79991234567")

    def test_bot_token_shape_is_rejected(self) -> None:
        with patch("builtins.input", return_value="123456:bot-token"):
            with self.assertRaisesRegex(ValueError, "international format"):
                _prompt_phone()


if __name__ == "__main__":
    unittest.main()
