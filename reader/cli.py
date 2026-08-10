from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from pathlib import Path
import sys

from telethon import errors

from app.source_candidates import (
    DEFAULT_CATALOG_PATH,
    CandidateCatalog,
    SourceCandidate,
    load_candidate_catalog,
)
from reader.auth import authorize_interactively
from reader.cooldown import TelegramCooldownError, enforce_cooldown, record_cooldown
from reader.identity import TelegramAuthorizationError, verify_authorized_identity
from reader.resolver import (
    MAX_RESOLVE_BATCH,
    build_resolution_report,
    resolve_source_candidates,
    write_resolution_report,
)
from reader.settings import ReaderSettings
from reader.telethon_client import build_telegram_client, harden_session_permissions


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="BibiBike read-only Telegram tools")
    parser.add_argument("--env-file", default=".env", help="path to local .env")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "authorize",
        help="create or verify the local user session interactively",
    )
    resolve = sub.add_parser(
        "resolve-sources",
        help="verify candidate groups without joining or reading messages",
    )
    selection = resolve.add_mutually_exclusive_group()
    selection.add_argument(
        "--priority",
        choices=("A", "B"),
        help="candidate priority to verify (default: A)",
    )
    selection.add_argument(
        "--handle",
        action="append",
        help="exact reviewed handle from the catalog; may be repeated",
    )
    resolve.add_argument(
        "--catalog",
        default=str(DEFAULT_CATALOG_PATH),
        help="path to the read-only candidate catalog",
    )
    resolve.add_argument(
        "--output",
        default="./data/source-resolution.json",
        help="where to write the verification report",
    )
    resolve.add_argument(
        "--limit",
        type=int,
        default=MAX_RESOLVE_BATCH,
        help=f"maximum candidates to check (1-{MAX_RESOLVE_BATCH})",
    )
    return parser


def _select_candidates(
    catalog: CandidateCatalog,
    args: argparse.Namespace,
) -> tuple[SourceCandidate, ...]:
    if args.handle:
        by_handle = {
            candidate.handle.casefold(): candidate for candidate in catalog.candidates
        }
        requested = [
            handle.strip().removeprefix("@").casefold() for handle in args.handle
        ]
        if len(set(requested)) != len(requested):
            raise ValueError("--handle values must be unique")
        if len(requested) > args.limit:
            raise ValueError("number of --handle values cannot exceed --limit")
        unknown = [handle for handle in requested if handle not in by_handle]
        if unknown:
            raise ValueError(
                "handles are not present in the reviewed catalog: "
                + ", ".join(f"@{handle}" for handle in unknown)
            )
        return tuple(by_handle[handle] for handle in requested)
    return tuple(catalog.by_priority(args.priority or "A")[: args.limit])


def _validated_report_output(
    value: str | Path,
    *,
    protected_paths: tuple[str | Path, ...],
) -> Path:
    output = Path(value).expanduser().resolve()
    protected = {Path(path).expanduser().resolve() for path in protected_paths}
    if output in protected:
        raise ValueError("resolution report cannot overwrite a protected project file")
    if output.suffix.lower() != ".json":
        raise ValueError("resolution report output must end with .json")
    return output


async def _resolve_sources(args: argparse.Namespace) -> int:
    settings = ReaderSettings.from_env(
        args.env_file,
        require_expected_user_id=True,
    )
    session_path = settings.telegram_session_path
    if not session_path.is_file():
        raise TelegramAuthorizationError(
            "Telegram session file is missing; run the authorize command locally"
        )
    enforce_cooldown(settings.telegram_cooldown_path)

    if not 1 <= args.limit <= MAX_RESOLVE_BATCH:
        raise ValueError(f"--limit must be between 1 and {MAX_RESOLVE_BATCH}")
    catalog = load_candidate_catalog(args.catalog)
    candidates = _select_candidates(catalog, args)
    output_path = _validated_report_output(
        args.output,
        protected_paths=(
            session_path,
            settings.telegram_cooldown_path,
            args.env_file,
            args.catalog,
        ),
    )
    client = build_telegram_client(settings, receive_updates=False)
    try:
        await client.connect()
        identity = await verify_authorized_identity(
            client,
            expected_user_id=settings.telegram_expected_user_id,
        )
        sources = await resolve_source_candidates(client, candidates)
        rate_limit = next(
            (source for source in sources if source.status == "rate_limited"),
            None,
        )
        if rate_limit is not None:
            record_cooldown(
                settings.telegram_cooldown_path,
                seconds=rate_limit.retry_after_seconds or 3600,
            )
    finally:
        try:
            await client.disconnect()
        finally:
            hardened = harden_session_permissions(session_path)
            if session_path.exists() and not hardened:
                raise PermissionError("could not restrict the Telegram session file")

    report = build_resolution_report(
        account_user_id=identity.user_id,
        sources=sources,
    )
    output_path = write_resolution_report(report, output_path)
    counts = Counter(source.status for source in sources)
    summary = ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    print(f"Source verification report: {output_path}")
    attempted = sum(
        source.status != "skipped_after_rate_limit" for source in sources
    )
    print(
        f"Telegram requests: {attempted}; "
        f"report entries: {len(sources)} of {len(candidates)} selected"
    )
    print(summary or "No candidates selected")
    print("No source was joined, enabled, or added to the runtime database.")
    return 3 if counts.get("rate_limited", 0) else 0


async def _run(args: argparse.Namespace) -> int:
    if args.command == "authorize":
        settings = ReaderSettings.from_env(args.env_file)
        identity = await authorize_interactively(settings)
        username = f"@{identity.username}" if identity.username else "без username"
        print(f"Telegram user session ready: id={identity.user_id}, {username}")
        if settings.telegram_expected_user_id is None:
            print(
                "Добавьте в локальный .env: "
                f"TELEGRAM_EXPECTED_USER_ID={identity.user_id}"
            )
        print(f"Session file: {settings.telegram_session_path}")
        return 0
    if args.command == "resolve-sources":
        return await _resolve_sources(args)
    raise AssertionError(f"unsupported command: {args.command}")


def main() -> None:
    args = _parser().parse_args()
    try:
        raise SystemExit(asyncio.run(_run(args)))
    except errors.RPCError as exc:
        print(
            f"error: Telegram rejected the request ({type(exc).__name__})",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    except (
        ValueError,
        OSError,
        RuntimeError,
        TelegramAuthorizationError,
        TelegramCooldownError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
