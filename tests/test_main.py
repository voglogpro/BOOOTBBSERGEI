from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import Mock, patch

import main


class MainEntrypointTests(unittest.TestCase):
    def test_bind_failure_is_logged_without_exception_message(self) -> None:
        settings = SimpleNamespace(
            host="0.0.0.0",
            port=8080,
            session_encryption_key="test-key",
            encrypted_session_path="reader.enc",
            telegram_expected_user_id=777,
        )
        app_settings = Mock()
        bind_error = OSError(98, "sensitive operating-system detail")
        captured = io.StringIO()

        with (
            patch.object(main.ServerSettings, "from_env", return_value=settings),
            patch.object(main.Settings, "from_env", return_value=app_settings),
            patch.object(main, "EncryptedSessionStore", return_value=object()),
            patch.object(main, "ParserLoginService", return_value=object()),
            patch.object(main, "LiveReaderService", return_value=object()),
            patch.object(main, "SourceManagementService", return_value=object()),
            patch.object(main, "create_app", return_value=object()),
            patch.object(main.web, "run_app", side_effect=bind_error),
            redirect_stdout(captured),
            self.assertRaises(OSError),
        ):
            main.main()

        output = captured.getvalue()
        self.assertIn(
            "[startup] configuration OK; starting listener on 0.0.0.0:8080",
            output,
        )
        self.assertIn("[runtime] FAILED: OSError errno=98", output)
        self.assertNotIn("sensitive operating-system detail", output)

    def test_clean_shutdown_is_logged(self) -> None:
        settings = SimpleNamespace(
            host="0.0.0.0",
            port=8080,
            session_encryption_key="test-key",
            encrypted_session_path="reader.enc",
            telegram_expected_user_id=777,
        )
        app_settings = Mock()
        captured = io.StringIO()

        with (
            patch.object(main.ServerSettings, "from_env", return_value=settings),
            patch.object(main.Settings, "from_env", return_value=app_settings),
            patch.object(main, "EncryptedSessionStore", return_value=object()),
            patch.object(main, "ParserLoginService", return_value=object()),
            patch.object(main, "LiveReaderService", return_value=object()),
            patch.object(main, "SourceManagementService", return_value=object()),
            patch.object(main, "create_app", return_value=object()),
            patch.object(main.web, "run_app", return_value=None),
            redirect_stdout(captured),
        ):
            main.main()

        self.assertIn("[shutdown] aiohttp server stopped", captured.getvalue())


if __name__ == "__main__":
    unittest.main()
