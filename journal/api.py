from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from aiohttp import web

from journal.keys import REPOSITORY_KEY, USER_KEY
from journal.repository import JournalRepository, RepositoryError


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{2,20}$")


class ApiError(ValueError):
    def __init__(self, message: str, *, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status = status
        self.code = code


def _repo(request: web.Request) -> JournalRepository:
    return request.app[REPOSITORY_KEY]


def _user(request: web.Request) -> dict[str, Any]:
    return request[USER_KEY]


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        payload = await request.json()
    except Exception as exc:
        raise ApiError("Некорректный JSON") from exc
    if not isinstance(payload, dict):
        raise ApiError("Ожидается JSON-объект")
    return payload


def _text(payload: dict[str, Any], key: str, limit: int, default: str = "") -> str:
    value = payload.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ApiError(f"Поле {key} должно быть текстом")
    return value.strip()[:limit]


def _number(
    payload: dict[str, Any], key: str, *, required: bool = False, default: float | None = None
) -> float | None:
    value = payload.get(key, default)
    if value in (None, ""):
        if required:
            raise ApiError(f"Заполните поле {key}")
        return None
    if isinstance(value, bool):
        raise ApiError(f"Поле {key} должно быть числом")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"Поле {key} должно быть числом") from exc
    if not -1_000_000_000 <= result <= 1_000_000_000:
        raise ApiError(f"Значение {key} вне допустимого диапазона")
    return result


def _date(value: str) -> str:
    if not DATE_RE.fullmatch(value):
        raise ApiError("Дата должна быть в формате YYYY-MM-DD")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise ApiError("Некорректная дата") from exc


def _scope(request: web.Request) -> str:
    return "team" if request.query.get("scope") == "team" else "me"


def _period(request: web.Request) -> tuple[str, str]:
    try:
        days = min(max(int(request.query.get("days", "30")), 1), 365)
    except ValueError as exc:
        raise ApiError("Некорректный период") from exc
    end = date.today()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _trade_payload(payload: dict[str, Any]) -> dict[str, Any]:
    traded_at = _text(payload, "traded_at", 32)
    if not traded_at:
        traded_at = datetime.now().replace(microsecond=0).isoformat()
    try:
        datetime.fromisoformat(traded_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError("Некорректные дата и время сделки") from exc

    symbol = _text(payload, "symbol", 20).upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ApiError("Укажите корректный торговый символ")
    direction = _text(payload, "direction", 4).upper()
    if direction not in {"BUY", "SELL"}:
        raise ApiError("Направление должно быть BUY или SELL")
    visibility = _text(payload, "visibility", 10, "team")
    if visibility not in {"private", "team"}:
        raise ApiError("Некорректная видимость")
    plan_followed = payload.get("plan_followed", True)
    if not isinstance(plan_followed, bool):
        raise ApiError("Некорректное значение соблюдения плана")

    return {
        "traded_at": traded_at,
        "symbol": symbol,
        "direction": direction,
        "timeframe": _text(payload, "timeframe", 10),
        "setup": _text(payload, "setup", 160),
        "entry_price": _number(payload, "entry_price"),
        "stop_loss": _number(payload, "stop_loss"),
        "take_profit": _number(payload, "take_profit"),
        "volume": _number(payload, "volume"),
        "risk_amount": _number(payload, "risk_amount"),
        "pnl": _number(payload, "pnl", required=True),
        "r_multiple": _number(payload, "r_multiple"),
        "emotion_before": _text(payload, "emotion_before", 80),
        "emotion_after": _text(payload, "emotion_after", 80),
        "plan_followed": 1 if plan_followed else 0,
        "mistake": _text(payload, "mistake", 160),
        "note": _text(payload, "note", 1200),
        "screenshot_url": _text(payload, "screenshot_url", 1000),
        "visibility": visibility,
    }


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "trader-journal"})


async def bootstrap(request: web.Request) -> web.Response:
    repo = _repo(request)
    user = _user(request)
    today = date.today().isoformat()
    start = (date.today() - timedelta(days=29)).isoformat()
    circle = await repo.get_circle(user["id"])
    mood = await repo.get_mood(user["id"], today)
    trades = await repo.list_trades(
        user["id"], start_date=today, end_date=today, scope="team"
    )
    stats = await repo.stats(
        user["id"], start_date=start, end_date=today, scope="me"
    )
    return web.json_response(
        {"user": user, "circle": circle, "today_mood": mood, "today_trades": trades, "stats": stats}
    )


async def save_mood(request: web.Request) -> web.Response:
    payload = await _json(request)
    entry_date = _date(request.match_info["entry_date"])
    visibility = _text(payload, "visibility", 10, "team")
    if visibility not in {"private", "team"}:
        raise ApiError("Некорректная видимость")

    values: dict[str, Any] = {"entry_date": entry_date, "visibility": visibility}
    for key in ("mood", "energy", "confidence", "discipline"):
        number = _number(payload, key, required=True)
        if number is None or not number.is_integer() or not 1 <= number <= 5:
            raise ApiError(f"{key}: выберите значение от 1 до 5")
        values[key] = int(number)
    values["emotion"] = _text(payload, "emotion", 80)
    values["note"] = _text(payload, "note", 1200)
    result = await _repo(request).upsert_mood(_user(request)["id"], values)
    return web.json_response({"mood": result})


async def list_trades(request: web.Request) -> web.Response:
    today = date.today()
    start = _date(request.query.get("from", (today - timedelta(days=30)).isoformat()))
    end = _date(request.query.get("to", today.isoformat()))
    if start > end:
        raise ApiError("Начальная дата позже конечной")
    trades = await _repo(request).list_trades(
        _user(request)["id"], start_date=start, end_date=end, scope=_scope(request)
    )
    return web.json_response({"trades": trades})


async def create_trade(request: web.Request) -> web.Response:
    trade = await _repo(request).create_trade(
        _user(request)["id"], _trade_payload(await _json(request))
    )
    return web.json_response({"trade": trade}, status=201)


async def update_trade(request: web.Request) -> web.Response:
    trade = await _repo(request).update_trade(
        _user(request)["id"],
        int(request.match_info["trade_id"]),
        _trade_payload(await _json(request)),
    )
    return web.json_response({"trade": trade})


async def delete_trade(request: web.Request) -> web.Response:
    await _repo(request).delete_trade(
        _user(request)["id"], int(request.match_info["trade_id"])
    )
    return web.json_response({"deleted": True})


async def calendar(request: web.Request) -> web.Response:
    month = request.query.get("month", date.today().strftime("%Y-%m"))
    if not MONTH_RE.fullmatch(month):
        raise ApiError("Месяц должен быть в формате YYYY-MM")
    try:
        first = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise ApiError("Некорректный месяц") from exc
    if first.month == 12:
        next_month = date(first.year + 1, 1, 1)
    else:
        next_month = date(first.year, first.month + 1, 1)
    last = next_month - timedelta(days=1)
    days = await _repo(request).calendar(
        _user(request)["id"],
        start_date=first.isoformat(),
        end_date=last.isoformat(),
        scope=_scope(request),
    )
    return web.json_response({"month": month, "days": days})


async def stats(request: web.Request) -> web.Response:
    start, end = _period(request)
    result = await _repo(request).stats(
        _user(request)["id"], start_date=start, end_date=end, scope=_scope(request)
    )
    return web.json_response({"from": start, "to": end, "stats": result})


async def create_circle(request: web.Request) -> web.Response:
    name = _text(await _json(request), "name", 40, "Команда трейдеров")
    circle = await _repo(request).create_circle(_user(request)["id"], name)
    return web.json_response({"circle": circle}, status=201)


async def join_circle(request: web.Request) -> web.Response:
    code = _text(await _json(request), "invite_code", 16)
    circle = await _repo(request).join_circle(_user(request)["id"], code)
    return web.json_response({"circle": circle})


async def leave_circle(request: web.Request) -> web.Response:
    await _repo(request).leave_circle(_user(request)["id"])
    return web.json_response({"left": True})


def routes() -> list[web.RouteDef]:
    return [
        web.get("/health", health),
        web.get("/api/bootstrap", bootstrap),
        web.put("/api/moods/{entry_date}", save_mood),
        web.get("/api/trades", list_trades),
        web.post("/api/trades", create_trade),
        web.put("/api/trades/{trade_id}", update_trade),
        web.delete("/api/trades/{trade_id}", delete_trade),
        web.get("/api/calendar", calendar),
        web.get("/api/stats", stats),
        web.post("/api/circles", create_circle),
        web.post("/api/circles/join", join_circle),
        web.post("/api/circles/leave", leave_circle),
    ]
