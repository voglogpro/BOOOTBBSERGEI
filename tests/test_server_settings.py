from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.settings import ServerSettings


BASE_ENV = {
    "BOT_TOKEN": "123456:secret-token",
    "ADMIN_TELEGRAM_IDS": "111, 222",
    "TELEGRAM_API_ID": "123456",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELEGRAM_EXPECTED_USER_ID": "777",
    "SESSION_ENCRYPTION_KEY": "test-key",
}


class ServerSettingsTests(unittest.TestCase):
    def test_valid_settings_use_bothost_port(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env = {
                **BASE_ENV,
                "PORT": "9000",
                "APP_PORT": "8000",
                "ENCRYPTED_SESSION_PATH": str(Path(temp_dir) / "session.enc"),
            }
            with patch.dict(os.environ, env, clear=True):
                settings = ServerSettings.from_env(None)

        self.assertEqual(settings.port, 9000)
        self.assertEqual(settings.admin_telegram_ids, frozenset({111, 222}))
        self.assertEqual(settings.telegram_expected_user_id, 777)

    def test_expected_reader_account_is_required(self) -> None:
        env = dict(BASE_ENV)
        env.pop("TELEGRAM_EXPECTED_USER_ID")
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "TELEGRAM_EXPECTED_USER_ID"):
                ServerSettings.from_env(None)

    def test_encrypted_session_path_must_not_be_plain_session(self) -> None:
        env = {**BASE_ENV, "ENCRYPTED_SESSION_PATH": "./data/reader.session"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(ValueError, "must end with .enc"):
                ServerSettings.from_env(None)


if __name__ == "__main__":
    unittest.main()
