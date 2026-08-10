from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from telethon import errors

from app.source_candidates import SourceCandidate
from reader.resolver import (
    MAX_RESOLVE_BATCH,
    SourceResolution,
    build_resolution_report,
    resolve_source_candidates,
    write_resolution_report,
)


def _candidate(handle: str = "public_group") -> SourceCandidate:
    return SourceCandidate(
        priority="A",
        handle=handle,
        title=f"Candidate {handle}",
        category="entrepreneurs",
        geo="federal",
        public_url=f"https://t.me/{handle}",
        public_preview_verified=True,
        history_verified=False,
        telegram_chat_id=None,
        enabled=False,
        noise_risk="low",
        reason="test",
    )


class FakeClient:
    def __init__(self, results: dict[str, object]) -> None:
        self.results = results
        self.requests: list[str] = []

    async def get_entity(self, handle: str) -> object:
        self.requests.append(handle)
        result = self.results[handle]
        if isinstance(result, BaseException):
            raise result
        return result


def _group(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "megagroup": True,
        "username": "public_group",
        "title": "Public Group",
        "left": False,
        "kicked": False,
        "id": 123456789,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_joined_public_group_is_ready(self) -> None:
        client = FakeClient({"public_group": _group()})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(result[0].status, "ready")
        self.assertEqual(result[0].telegram_chat_id, -1001234567890)
        self.assertEqual(result[0].canonical_handle, "public_group")

    async def test_non_marked_chat_id_is_rejected(self) -> None:
        client = FakeClient({"public_group": _group()})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -123456789,
        )

        self.assertEqual(result[0].status, "unavailable")
        self.assertEqual(result[0].telegram_chat_id, None)

    async def test_group_is_never_joined_automatically(self) -> None:
        client = FakeClient({"public_group": _group(left=True)})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(result[0].status, "not_joined")
        self.assertEqual(result[0].telegram_chat_id, None)
        self.assertEqual(client.requests, ["public_group"])

    async def test_broadcast_channel_is_rejected(self) -> None:
        client = FakeClient({"public_group": _group(megagroup=False)})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(result[0].status, "not_public_group")

    async def test_changed_username_is_not_accepted(self) -> None:
        client = FakeClient({"public_group": _group(username="other_group")})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(result[0].status, "username_mismatch")
        self.assertEqual(result[0].telegram_chat_id, None)

    async def test_forum_group_is_deferred_until_topic_links_exist(self) -> None:
        client = FakeClient({"public_group": _group(forum=True)})

        result = await resolve_source_candidates(
            client,
            [_candidate()],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(result[0].status, "unsupported_forum")
        self.assertEqual(result[0].telegram_chat_id, None)

    async def test_batch_is_capped_before_telegram_requests(self) -> None:
        candidates = [_candidate(f"group_{index}") for index in range(11)]
        client = FakeClient({})

        with self.assertRaisesRegex(ValueError, str(MAX_RESOLVE_BATCH)):
            await resolve_source_candidates(client, candidates)

        self.assertEqual(client.requests, [])

    async def test_rate_limit_stops_all_further_requests(self) -> None:
        flood_wait = errors.FloodWaitError(request=None, capture=91)
        client = FakeClient(
            {
                "first_group": flood_wait,
                "second_group": _group(username="second_group"),
            }
        )

        result = await resolve_source_candidates(
            client,
            [_candidate("first_group"), _candidate("second_group")],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(client.requests, ["first_group"])
        self.assertEqual(result[0].status, "rate_limited")
        self.assertEqual(result[0].retry_after_seconds, 91)
        self.assertEqual(result[1].status, "skipped_after_rate_limit")

    async def test_premium_flood_wait_also_stops_all_requests(self) -> None:
        flood_wait = errors.FloodPremiumWaitError(request=None, capture=91)
        client = FakeClient(
            {
                "first_group": flood_wait,
                "second_group": _group(username="second_group"),
            }
        )

        result = await resolve_source_candidates(
            client,
            [_candidate("first_group"), _candidate("second_group")],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual(client.requests, ["first_group"])
        self.assertEqual(result[0].status, "rate_limited")
        self.assertEqual(result[0].retry_after_seconds, 91)
        self.assertEqual(result[1].status, "skipped_after_rate_limit")

    async def test_unavailable_username_does_not_abort_report(self) -> None:
        client = FakeClient(
            {
                "first_group": ValueError("not found"),
                "second_group": _group(username="second_group"),
            }
        )

        result = await resolve_source_candidates(
            client,
            [_candidate("first_group"), _candidate("second_group")],
            _is_channel=lambda entity: True,
            _peer_id=lambda entity: -1001234567890,
        )

        self.assertEqual([item.status for item in result], ["unavailable", "ready"])


class ResolutionReportTests(unittest.TestCase):
    def test_report_is_utf8_json_and_does_not_activate_sources(self) -> None:
        source = SourceResolution(
            handle="public_group",
            title="Предприниматели",
            priority="A",
            status="not_joined",
            reason="manual join required",
            telegram_chat_id=None,
            canonical_handle="public_group",
            telegram_title="Предприниматели",
        )
        report = build_resolution_report(
            account_user_id=42,
            sources=[source],
            checked_at=datetime(2026, 8, 9, 12, 0, tzinfo=timezone.utc),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_resolution_report(report, Path(temp_dir) / "report.json")
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["account_user_id"], 42)
        self.assertEqual(payload["sources"][0]["title"], "Предприниматели")
        self.assertNotIn("enabled", payload["sources"][0])
        self.assertNotIn("participants", payload["sources"][0])


if __name__ == "__main__":
    unittest.main()
