from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from journal.config import _bot_token


class ConfigTests(unittest.TestCase):
    def test_primary_bot_token_is_used(self) -> None:
        with patch.dict(os.environ, {"BOT_TOKEN": "primary-token"}, clear=True):
            self.assertEqual(_bot_token(), "primary-token")

    def test_bothost_token_aliases_are_supported(self) -> None:
        for name in ("TELEGRAM_BOT_TOKEN", "API_TOKEN", "TOKEN"):
            with self.subTest(name=name):
                with patch.dict(os.environ, {name: "alias-token"}, clear=True):
                    self.assertEqual(_bot_token(), "alias-token")


if __name__ == "__main__":
    unittest.main()
