from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.source_verification import (
    ResolutionReportError,
    load_ready_source_verification,
)
from reader.resolver import (
    SourceResolution,
    build_resolution_report,
    write_resolution_report,
)


NOW = datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc)


def _source(**overrides: object) -> SourceResolution:
    values: dict[str, object] = {
        "handle": "public_group",
        "title": "Catalog title",
        "priority": "A",
        "status": "ready",
        "reason": "verified",
        "telegram_chat_id": -1001234567890,
        "canonical_handle": "public_group",
        "telegram_title": "Telegram title",
    }
    values.update(overrides)
    return SourceResolution(**values)


class SourceVerificationReportTests(unittest.TestCase):
    def _report_path(
        self,
        root: str,
        source: SourceResolution,
        *,
        checked_at: datetime = NOW,
    ) -> Path:
        report = build_resolution_report(
            account_user_id=777,
            sources=[source],
            checked_at=checked_at,
        )
        return write_resolution_report(report, Path(root) / "resolution.json")

    def test_ready_source_can_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._report_path(temp_dir, _source())

            verification = load_ready_source_verification(
                path,
                handle="@PUBLIC_GROUP",
                now=NOW,
            )

        self.assertEqual(verification.telegram_chat_id, -1001234567890)
        self.assertEqual(verification.account_user_id, 777)
        self.assertEqual(verification.title, "Telegram title")

    def test_non_ready_source_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._report_path(
                temp_dir,
                _source(status="not_joined", telegram_chat_id=None),
            )

            with self.assertRaisesRegex(ResolutionReportError, "ready status"):
                load_ready_source_verification(path, handle="public_group", now=NOW)

    def test_stale_report_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._report_path(
                temp_dir,
                _source(),
                checked_at=NOW - timedelta(hours=25),
            )

            with self.assertRaisesRegex(ResolutionReportError, "older than 24"):
                load_ready_source_verification(path, handle="public_group", now=NOW)

    def test_changed_canonical_handle_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._report_path(
                temp_dir,
                _source(canonical_handle="other_group"),
            )

            with self.assertRaisesRegex(ResolutionReportError, "canonical_handle"):
                load_ready_source_verification(path, handle="public_group", now=NOW)

    def test_non_marked_chat_id_cannot_be_imported(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self._report_path(
                temp_dir,
                _source(telegram_chat_id=-123456789),
            )

            with self.assertRaisesRegex(ResolutionReportError, "marked -100"):
                load_ready_source_verification(path, handle="public_group", now=NOW)


if __name__ == "__main__":
    unittest.main()
