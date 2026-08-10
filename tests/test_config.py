from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class CoreSettingsTests(unittest.TestCase):
    def test_pilot_defaults_to_live_only(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env(None)

        self.assertEqual(settings.reader_catchup_limit, 0)

    def test_pilot_rejects_catchup(self) -> None:
        with patch.dict(
            os.environ,
            {"READER_CATCHUP_LIMIT": "1"},
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "between 0 and 0"):
                Settings.from_env(None)


if __name__ == "__main__":
    unittest.main()
