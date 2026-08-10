from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from dataclasses import asdict

from app.config import Settings
from app.db import init_db, utc_now
from app.ingest import ingest_public_message
from app.models import PublicMessageEvent
from app.repository import (
    list_sources,
    purge_expired_rejections,
    register_verified_source,
    set_source_enabled,
    upsert_city,
    upsert_source,
)
from app.rules import classify_intent
from app.source_candidates import load_candidate_catalog
from app.source_verification import load_ready_source_verification


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BibiBike Leads stage-1 tools")
    parser.add_argument("--env-file", default=".env", help="path to .env")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create or update the local database")
    sub.add_parser("seed-pilot", help="create the Yekaterinburg pilot city")
    sub.add_parser("list-sources", help="show the current Telegram allowlist")
    candidates = sub.add_parser(
        "list-candidates",
        help="validate and show researched A/B source candidates",
    )
    candidates.add_argument(
        "--catalog",
        help="path to source-candidates.json (uses research catalog by default)",
    )
    candidates.add_argument(
        "--priority",
        choices=("A", "B"),
        help="show only one priority (shows A and B by default)",
    )
    sub.add_parser("purge", help="remove expired rejected observations")

    classify = sub.add_parser("classify", help="run the deterministic intent rules")
    classify.add_argument("text")

    source = sub.add_parser(
        "add-source",
        help="register one public source in disabled state",
    )
    source.add_argument("--chat-id", type=int, required=True)
    source.add_argument("--handle", required=True)
    source.add_argument("--title", required=True)
    source.add_argument("--city")
    source.add_argument(
        "--kind",
        choices=("group", "supergroup", "channel"),
        default="group",
    )

    verified = sub.add_parser(
        "register-ready-source",
        help="register one ready resolver result in disabled state",
    )
    verified.add_argument("--handle", required=True)
    verified.add_argument(
        "--report",
        default="./data/source-resolution.json",
        help="path to the fresh resolver report",
    )
    verified.add_argument("--city")

    toggle = sub.add_parser("set-source", help="enable or disable one source")
    toggle.add_argument("--chat-id", type=int, required=True)
    state = toggle.add_mutually_exclusive_group(required=True)
    state.add_argument("--enable", action="store_true")
    state.add_argument("--disable", action="store_true")

    ingest = sub.add_parser(
        "simulate-ingest", help="test ingestion without connecting Telegram"
    )
    ingest.add_argument("--chat-id", type=int, required=True)
    ingest.add_argument("--message-id", type=int, required=True)
    ingest.add_argument("--text", required=True)
    ingest.add_argument("--request-id")
    ingest.add_argument("--published-at")
    ingest.add_argument("--edited-at")
    ingest.add_argument("--event-type", choices=("new", "edited"), default="new")
    return parser


async def _run(args: argparse.Namespace) -> int:
    if args.command == "list-candidates":
        catalog = load_candidate_catalog(args.catalog)
        selected = (
            catalog.by_priority(args.priority)
            if args.priority
            else catalog.by_priority()
        )
        print(
            f"source candidates: {len(selected)} "
            f"(researched {catalog.researched_at.isoformat()})"
        )
        if not selected:
            print("no candidates")
            return 0
        print("PRI  HANDLE                         NOISE   GEO                  TITLE")
        for candidate in selected:
            print(
                f"{candidate.priority:<4} "
                f"@{candidate.handle:<29} "
                f"{candidate.noise_risk:<7} "
                f"{candidate.geo:<20} "
                f"{candidate.title}"
            )
        return 0

    settings = Settings.from_env(args.env_file)

    if args.command == "init-db":
        await init_db(settings)
        print(f"database ready: {settings.database_path}")
        return 0

    if args.command == "seed-pilot":
        await init_db(settings)
        city_id = await upsert_city(
            settings,
            slug="ekaterinburg",
            name="Екатеринбург",
            region="Свердловская область",
            aliases=("Екатеринбург", "Екб"),
        )
        print(f"pilot city ready: id={city_id}, slug=ekaterinburg")
        return 0

    if args.command == "classify":
        result = classify_intent(args.text)
        print(
            json.dumps(
                {
                    "score": result.score,
                    "decision": result.decision,
                    "rules_version": result.rules_version,
                    "matches": [asdict(match) for match in result.matches],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.command == "add-source":
        await init_db(settings)
        source_id = await upsert_source(
            settings,
            telegram_chat_id=args.chat_id,
            public_handle=args.handle,
            title=args.title,
            city_slug=args.city,
            source_kind=args.kind,
        )
        print(
            f"source registered disabled: id={source_id}, chat_id={args.chat_id}"
        )
        return 0

    if args.command == "register-ready-source":
        expected_user_raw = os.getenv("TELEGRAM_EXPECTED_USER_ID", "").strip()
        if not expected_user_raw:
            raise ValueError(
                "TELEGRAM_EXPECTED_USER_ID is required to import a resolver report"
            )
        try:
            expected_user_id = int(expected_user_raw)
        except ValueError as exc:
            raise ValueError("TELEGRAM_EXPECTED_USER_ID must be an integer") from exc
        if expected_user_id <= 0:
            raise ValueError("TELEGRAM_EXPECTED_USER_ID must be positive")
        verification = load_ready_source_verification(
            args.report,
            handle=args.handle,
        )
        if verification.account_user_id != expected_user_id:
            raise ValueError(
                "resolver report belongs to a different Telegram account"
            )
        await init_db(settings)
        source_id = await register_verified_source(
            settings,
            verification=verification,
            city_slug=args.city,
        )
        print(
            "verified source registered disabled: "
            f"id={source_id}, chat_id={verification.telegram_chat_id}, "
            f"handle=@{verification.handle}"
        )
        return 0

    if args.command == "set-source":
        await init_db(settings)
        enabled = bool(args.enable)
        await set_source_enabled(
            settings, telegram_chat_id=args.chat_id, enabled=enabled
        )
        print(f"source {args.chat_id}: {'enabled' if enabled else 'disabled'}")
        return 0

    if args.command == "list-sources":
        await init_db(settings)
        rows = await list_sources(settings)
        if not rows:
            print("allowlist is empty")
            return 0
        for row in rows:
            print(
                f"{row['telegram_chat_id']}  @{row['public_handle']}  "
                f"verified={bool(row['verified_at'])}  "
                f"enabled={bool(row['enabled'])}  city={row['city_slug'] or '-'}  "
                f"reader={row['reader_status'] or 'paused'}  {row['title']}"
            )
        return 0

    if args.command == "simulate-ingest":
        await init_db(settings)
        event = PublicMessageEvent(
            request_id=args.request_id
            or f"manual:{args.chat_id}:{args.message_id}:{uuid.uuid4().hex}",
            telegram_chat_id=args.chat_id,
            telegram_message_id=args.message_id,
            text=args.text,
            published_at=args.published_at or utc_now(),
            edited_at=args.edited_at,
            event_type=args.event_type,
        )
        result = await ingest_public_message(settings, event)
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
        return 0

    if args.command == "purge":
        await init_db(settings)
        deleted = await purge_expired_rejections(settings)
        print(f"purged observations: {deleted}")
        return 0

    raise AssertionError(f"unsupported command: {args.command}")


def main() -> None:
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
