from __future__ import annotations

import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from app.cli import _run
from app.source_candidates import CandidateCatalogError, load_candidate_catalog


def _catalog_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "researched_at": "2026-08-09",
        "policy": {
            "public_username_required": True,
            "member_list_collection": False,
            "history_collection": False,
            "automatic_join": False,
            "automatic_messages": False,
            "note": "Discovery only.",
        },
        "candidates": [
            {
                "priority": "A",
                "handle": "alpha_chat",
                "title": "Alpha",
                "category": "entrepreneurs",
                "geo": "federal",
                "public_url": "https://t.me/alpha_chat",
                "public_preview_verified": True,
                "history_verified": False,
                "telegram_chat_id": None,
                "enabled": False,
                "noise_risk": "low",
                "reason": "Suitable for a pilot.",
            },
            {
                "priority": "B",
                "handle": "beta_chat",
                "title": "Beta",
                "category": "investments",
                "geo": "moscow",
                "public_url": "https://t.me/beta_chat",
                "public_preview_verified": True,
                "history_verified": False,
                "telegram_chat_id": None,
                "enabled": False,
                "noise_risk": "medium",
                "reason": "Reserve source.",
            },
        ],
        "excluded": [{"handle": "spam_chat", "reason": "Advertising board."}],
    }


class CandidateCatalogTests(unittest.TestCase):
    def _write(self, root: Path, data: dict[str, object]) -> Path:
        path = root / "source-candidates.json"
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_repository_catalog(self) -> None:
        catalog = load_candidate_catalog()
        self.assertGreater(len(catalog.by_priority("A")), 0)
        self.assertGreater(len(catalog.by_priority("B")), 0)
        self.assertTrue(all(not item.enabled for item in catalog.candidates))

    def test_filters_candidates_by_priority(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            catalog = load_candidate_catalog(
                self._write(Path(temp), _catalog_data())
            )
        self.assertEqual(
            [item.handle for item in catalog.by_priority("A")], ["alpha_chat"]
        )
        self.assertEqual(
            [item.handle for item in catalog.by_priority("B")], ["beta_chat"]
        )

    def test_rejects_enabled_discovery_candidate(self) -> None:
        data = _catalog_data()
        data["candidates"][0]["enabled"] = True  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CandidateCatalogError, "must be false"):
                load_candidate_catalog(self._write(Path(temp), data))

    def test_rejects_noncanonical_public_url(self) -> None:
        data = _catalog_data()
        data["candidates"][0]["public_url"] = "https://example.com/alpha_chat"  # type: ignore[index]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CandidateCatalogError, "canonical"):
                load_candidate_catalog(self._write(Path(temp), data))

    def test_rejects_duplicate_handle_case_insensitively(self) -> None:
        data = _catalog_data()
        data["excluded"] = [
            {"handle": "ALPHA_CHAT", "reason": "Duplicate."}
        ]
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(CandidateCatalogError, "duplicate handle"):
                load_candidate_catalog(self._write(Path(temp), data))


class CandidateCliTests(unittest.IsolatedAsyncioTestCase):
    async def test_cli_lists_both_priorities_without_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = CandidateCatalogTests()._write(Path(temp), _catalog_data())
            args = argparse.Namespace(
                command="list-candidates",
                catalog=str(path),
                priority=None,
                env_file=Path(temp) / "missing.env",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = await _run(args)

        self.assertEqual(result, 0)
        self.assertIn("@alpha_chat", output.getvalue())
        self.assertIn("@beta_chat", output.getvalue())

    async def test_cli_can_show_only_priority_a(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = CandidateCatalogTests()._write(Path(temp), _catalog_data())
            args = argparse.Namespace(
                command="list-candidates",
                catalog=str(path),
                priority="A",
                env_file=None,
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                result = await _run(args)

        self.assertEqual(result, 0)
        self.assertIn("@alpha_chat", output.getvalue())
        self.assertNotIn("@beta_chat", output.getvalue())


if __name__ == "__main__":
    unittest.main()
