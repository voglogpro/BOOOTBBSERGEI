from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable, Mapping

from app.rules import normalize_text


@dataclass(frozen=True, slots=True)
class CityDetection:
    city_id: int
    name: str
    confidence: float
    source: str


def detect_city(
    text: str,
    cities: Iterable[Mapping[str, object]],
    *,
    default_city_id: int | None = None,
) -> CityDetection | None:
    normalized = normalize_text(text)
    city_rows = list(cities)

    for row in city_rows:
        aliases = json.loads(str(row["aliases_json"]))
        candidates = [str(row["name"]), *[str(alias) for alias in aliases]]
        candidates = sorted(
            {normalize_text(alias) for alias in candidates if str(alias).strip()},
            key=len,
            reverse=True,
        )
        for alias in candidates:
            pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
            if re.search(pattern, normalized, flags=re.IGNORECASE):
                return CityDetection(
                    city_id=int(row["id"]),
                    name=str(row["name"]),
                    confidence=0.95,
                    source="message",
                )

    if default_city_id is not None:
        for row in city_rows:
            if int(row["id"]) == int(default_city_id):
                return CityDetection(
                    city_id=int(row["id"]),
                    name=str(row["name"]),
                    confidence=0.65,
                    source="source",
                )
    return None
