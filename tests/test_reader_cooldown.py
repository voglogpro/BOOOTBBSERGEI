from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from reader.cooldown import (
    TelegramCooldownError,
    cooldown_remaining_seconds,
    enforce_cooldown,
    record_cooldown,
)


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


class ReaderCooldownTests(unittest.TestCase):
    def test_recorded_flood_wait_blocks_early_retry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reader.session.cooldown.json"
            retry_at = record_cooldown(path, seconds=90, now=NOW)

            with self.assertRaisesRegex(TelegramCooldownError, "paused until"):
                enforce_cooldown(path, now=NOW + timedelta(seconds=89))
            self.assertEqual(
                cooldown_remaining_seconds(path, now=NOW + timedelta(seconds=89)),
                1,
            )

            enforce_cooldown(path, now=retry_at)
            self.assertEqual(cooldown_remaining_seconds(path, now=retry_at), 0)

    def test_invalid_cooldown_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "reader.session.cooldown.json"
            path.write_text("not JSON", encoding="utf-8")

            with self.assertRaisesRegex(TelegramCooldownError, "invalid"):
                enforce_cooldown(path, now=NOW)


if __name__ == "__main__":
    unittest.main()
