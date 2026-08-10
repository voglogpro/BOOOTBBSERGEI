from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parents[1] / "research" / "source-candidates.json"
)

_HANDLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
_PRIORITIES = frozenset({"A", "B"})
_NOISE_RISKS = frozenset({"low", "medium", "high"})


class CandidateCatalogError(ValueError):
    """The source-candidate catalog is missing required or safe values."""


@dataclass(frozen=True, slots=True)
class CandidatePolicy:
    public_username_required: bool
    member_list_collection: bool
    history_collection: bool
    automatic_join: bool
    automatic_messages: bool
    note: str


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    priority: str
    handle: str
    title: str
    category: str
    geo: str
    public_url: str
    public_preview_verified: bool
    history_verified: bool
    telegram_chat_id: int | None
    enabled: bool
    noise_risk: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExcludedSource:
    handle: str
    reason: str


@dataclass(frozen=True, slots=True)
class CandidateCatalog:
    schema_version: int
    researched_at: date
    policy: CandidatePolicy
    candidates: tuple[SourceCandidate, ...]
    excluded: tuple[ExcludedSource, ...]

    def by_priority(self, *priorities: str) -> tuple[SourceCandidate, ...]:
        selected = tuple(priority.upper() for priority in priorities) or ("A", "B")
        unknown = sorted(set(selected) - _PRIORITIES)
        if unknown:
            raise CandidateCatalogError(
                f"unknown candidate priority: {', '.join(unknown)}"
            )
        return tuple(
            candidate
            for candidate in self.candidates
            if candidate.priority in selected
        )


def load_candidate_catalog(
    path: str | Path | None = None,
) -> CandidateCatalog:
    """Load and strictly validate a discovery catalog without changing runtime state."""

    catalog_path = Path(path) if path is not None else DEFAULT_CATALOG_PATH
    try:
        raw = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CandidateCatalogError(
            f"candidate catalog not found: {catalog_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CandidateCatalogError(
            f"invalid JSON in {catalog_path} at line {exc.lineno}, column {exc.colno}"
        ) from exc
    except UnicodeDecodeError as exc:
        raise CandidateCatalogError(
            f"candidate catalog must be UTF-8: {catalog_path}"
        ) from exc

    root = _mapping(raw, "catalog")
    schema_version = _integer(root.get("schema_version"), "schema_version")
    if schema_version != CATALOG_SCHEMA_VERSION:
        raise CandidateCatalogError(
            f"unsupported schema_version {schema_version}; "
            f"expected {CATALOG_SCHEMA_VERSION}"
        )

    researched_at_raw = _text(root.get("researched_at"), "researched_at")
    try:
        researched_at = date.fromisoformat(researched_at_raw)
    except ValueError as exc:
        raise CandidateCatalogError(
            "researched_at must be an ISO date in YYYY-MM-DD format"
        ) from exc

    policy = _parse_policy(root.get("policy"))
    candidates_raw = _list(root.get("candidates"), "candidates")
    excluded_raw = _list(root.get("excluded"), "excluded")

    candidates = tuple(
        _parse_candidate(item, index)
        for index, item in enumerate(candidates_raw)
    )
    excluded = tuple(
        _parse_excluded(item, index) for index, item in enumerate(excluded_raw)
    )
    _validate_unique_handles(candidates, excluded)

    return CandidateCatalog(
        schema_version=schema_version,
        researched_at=researched_at,
        policy=policy,
        candidates=candidates,
        excluded=excluded,
    )


def _parse_policy(value: Any) -> CandidatePolicy:
    raw = _mapping(value, "policy")
    policy = CandidatePolicy(
        public_username_required=_boolean(
            raw.get("public_username_required"),
            "policy.public_username_required",
        ),
        member_list_collection=_boolean(
            raw.get("member_list_collection"),
            "policy.member_list_collection",
        ),
        history_collection=_boolean(
            raw.get("history_collection"), "policy.history_collection"
        ),
        automatic_join=_boolean(
            raw.get("automatic_join"), "policy.automatic_join"
        ),
        automatic_messages=_boolean(
            raw.get("automatic_messages"), "policy.automatic_messages"
        ),
        note=_text(raw.get("note"), "policy.note"),
    )
    if not policy.public_username_required:
        raise CandidateCatalogError(
            "policy.public_username_required must be true for this catalog"
        )
    if any(
        (
            policy.member_list_collection,
            policy.history_collection,
            policy.automatic_join,
            policy.automatic_messages,
        )
    ):
        raise CandidateCatalogError(
            "candidate catalog policy cannot enable collection, joining, or messages"
        )
    return policy


def _parse_candidate(value: Any, index: int) -> SourceCandidate:
    prefix = f"candidates[{index}]"
    raw = _mapping(value, prefix)
    priority = _text(raw.get("priority"), f"{prefix}.priority").upper()
    if priority not in _PRIORITIES:
        raise CandidateCatalogError(f"{prefix}.priority must be A or B")

    handle = _handle(raw.get("handle"), f"{prefix}.handle")
    public_url = _text(raw.get("public_url"), f"{prefix}.public_url")
    _validate_public_url(public_url, handle, f"{prefix}.public_url")

    telegram_chat_id = raw.get("telegram_chat_id")
    if telegram_chat_id is not None and (
        isinstance(telegram_chat_id, bool) or not isinstance(telegram_chat_id, int)
    ):
        raise CandidateCatalogError(f"{prefix}.telegram_chat_id must be integer or null")

    enabled = _boolean(raw.get("enabled"), f"{prefix}.enabled")
    if enabled:
        raise CandidateCatalogError(
            f"{prefix}.enabled must be false; discovery candidates cannot be activated"
        )

    noise_risk = _text(raw.get("noise_risk"), f"{prefix}.noise_risk").lower()
    if noise_risk not in _NOISE_RISKS:
        raise CandidateCatalogError(
            f"{prefix}.noise_risk must be low, medium, or high"
        )

    return SourceCandidate(
        priority=priority,
        handle=handle,
        title=_text(raw.get("title"), f"{prefix}.title"),
        category=_text(raw.get("category"), f"{prefix}.category"),
        geo=_text(raw.get("geo"), f"{prefix}.geo"),
        public_url=public_url,
        public_preview_verified=_boolean(
            raw.get("public_preview_verified"),
            f"{prefix}.public_preview_verified",
        ),
        history_verified=_boolean(
            raw.get("history_verified"), f"{prefix}.history_verified"
        ),
        telegram_chat_id=telegram_chat_id,
        enabled=enabled,
        noise_risk=noise_risk,
        reason=_text(raw.get("reason"), f"{prefix}.reason"),
    )


def _parse_excluded(value: Any, index: int) -> ExcludedSource:
    prefix = f"excluded[{index}]"
    raw = _mapping(value, prefix)
    return ExcludedSource(
        handle=_handle(raw.get("handle"), f"{prefix}.handle"),
        reason=_text(raw.get("reason"), f"{prefix}.reason"),
    )


def _validate_public_url(url: str, handle: str, field: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "t.me"
        or parsed.query
        or parsed.fragment
        or parsed.path.strip("/").lower() != handle.lower()
    ):
        raise CandidateCatalogError(
            f"{field} must be the canonical https://t.me/{handle} URL"
        )


def _validate_unique_handles(
    candidates: tuple[SourceCandidate, ...],
    excluded: tuple[ExcludedSource, ...],
) -> None:
    seen: dict[str, str] = {}
    for position, source in enumerate((*candidates, *excluded)):
        key = source.handle.casefold()
        location = (
            f"candidates[{position}]"
            if position < len(candidates)
            else f"excluded[{position - len(candidates)}]"
        )
        if key in seen:
            raise CandidateCatalogError(
                f"duplicate handle @{source.handle}: {seen[key]} and {location}"
            )
        seen[key] = location


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CandidateCatalogError(f"{field} must be an object")
    return value


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise CandidateCatalogError(f"{field} must be an array")
    return value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CandidateCatalogError(f"{field} must be a non-empty string")
    return value.strip()


def _handle(value: Any, field: str) -> str:
    handle = _text(value, field)
    if not _HANDLE_RE.fullmatch(handle):
        raise CandidateCatalogError(
            f"{field} must be a Telegram username without @ "
            "(5-32 Latin letters, digits, or underscores)"
        )
    return handle


def _boolean(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise CandidateCatalogError(f"{field} must be true or false")
    return value


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CandidateCatalogError(f"{field} must be an integer")
    return value
