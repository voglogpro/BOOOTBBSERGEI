from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from reader.settings import ReaderSettings
from reader.telethon_client import build_telegram_client


VALID_ENV = {
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
}


class ReaderSettingsTests(unittest.TestCase):
    def test_first_authorization_does_not_require_expected_user(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                **VALID_ENV,
                "TELEGRAM_SESSION_PATH": str(Path(temp_dir) / "reader.session"),
                "TELEGRAM_EXPECTED_USER_ID": "",
            },
            clear=True,
        ):
            settings = ReaderSettings.from_env(None)
        self.assertIsNone(settings.telegram_expected_user_id)
        self.assertEqual(settings.telegram_session_path.suffix, ".session")

    def test_resolver_requires_expected_user(self) -> None:
        with patch.dict(os.environ, VALID_ENV, clear=True):
            with self.assertRaisesRegex(ValueError, "EXPECTED_USER_ID"):
                ReaderSettings.from_env(None, require_expected_user_id=True)

    def test_invalid_api_hash_is_rejected(self) -> None:
        with patch.dict(
            os.environ,
            {**VALID_ENV, "TELEGRAM_API_HASH": "not-a-secret"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "32 hexadecimal"):
                ReaderSettings.from_env(None)

    def test_session_extension_is_explicit(self) -> None:
        with patch.dict(
            os.environ,
            {**VALID_ENV, "TELEGRAM_SESSION_PATH": "./data/reader"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "end with .session"):
                ReaderSettings.from_env(None)

    def test_session_extension_must_be_lowercase(self) -> None:
        with patch.dict(
            os.environ,
            {**VALID_ENV, "TELEGRAM_SESSION_PATH": "./data/reader.SESSION"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "in lowercase"):
                ReaderSettings.from_env(None)

    def test_telethon_session_does_not_cache_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = ReaderSettings(
                telegram_api_id=123456,
                telegram_api_hash="0123456789abcdef0123456789abcdef",
                telegram_expected_user_id=777,
                telegram_session_path=Path(temp_dir) / "reader.session",
            )
            client = build_telegram_client(settings)
            try:
                self.assertFalse(client.session.save_entities)
                self.assertEqual(client.flood_sleep_threshold, 0)
            finally:
                client.session.close()

    def test_existing_session_parent_permissions_are_not_mutated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = ReaderSettings(
                telegram_api_id=123456,
                telegram_api_hash="0123456789abcdef0123456789abcdef",
                telegram_expected_user_id=777,
                telegram_session_path=Path(temp_dir) / "reader.session",
            )
            with patch("reader.settings.os.chmod") as chmod:
                settings.ensure_session_directory()

        chmod.assert_not_called()


if __name__ == "__main__":
    unittest.main()
