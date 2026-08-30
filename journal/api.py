from __future__ import annotations

import re
import math
import csv
import io
import secrets
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aiohttp import web

from journal.keys import REPOSITORY_KEY, USER_KEY
from journal.repository import JournalRepository, RepositoryError


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9._-]{2,20}$")
CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_PLAN_IMAGE_BYTES = 6 * 1024 * 1024


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
    if not math.isfinite(result) or not -1_000_000_000 <= result <= 1_000_000_000:
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
    end = date.fromisoformat(_date(request.query.get("end", date.today().isoformat())))
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _week_start(value: str) -> str:
    selected = date.fromisoformat(_date(value))
    return (selected - timedelta(days=selected.weekday())).isoformat()


def _weekly_plan_payload(payload: dict[str, Any]) -> dict[str, Any]:
    symbol = _text(payload, "symbol", 20).upper()
    if not SYMBOL_RE.fullmatch(symbol):
        raise ApiError("Укажите корректный торговый символ")
    bias = _text(payload, "bias", 10, "NEUTRAL").upper()
    if bias not in {"LONG", "SHORT", "NEUTRAL"}:
        raise ApiError("Некорректное направление недельной идеи")
    status = _text(payload, "status", 10, "active").lower()
    if status not in {"active", "reviewed"}:
        raise ApiError("Некорректный статус недельного плана")
    visibility = _text(payload, "visibility", 10, "team")
    if visibility not in {"private", "team"}:
        raise ApiError("Некорректная видимость")
    rating = _number(payload, "rating")
    if rating is not None and (not rating.is_integer() or not 1 <= rating <= 5):
        raise ApiError("Оценка недели должна быть от 1 до 5")
    idea = _text(payload, "idea", 2000)
    if not idea:
        raise ApiError("Опишите торговую идею на неделю")
    selected_week = _text(payload, "week_start", 10) or date.today().isoformat()
    return {
        "week_start": _week_start(selected_week),
        "symbol": symbol,
        "bias": bias,
        "title": _text(payload, "title", 120),
        "idea": idea,
        "trade_plan": _text(payload, "trade_plan", 2000),
        "invalidation": _text(payload, "invalidation", 1000),
        "status": status,
        "week_summary": _text(payload, "week_summary", 2000),
        "week_lesson": _text(payload, "week_lesson", 1000),
        "rating": int(rating) if rating is not None else None,
        "visibility": visibility,
    }


def _plan_image_type(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


def _plan_image_directory(repo: JournalRepository) -> Path:
    return repo.database_path.parent / "uploads" / "weekly_plans"


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

    status = _text(payload, "status", 12, "closed").lower()
    if status not in {"planned", "open", "closed", "cancelled"}:
        raise ApiError("Некорректный статус сделки")
    session = _text(payload, "session", 20)
    if session not in {"", "Asia", "London", "New York", "Overlap", "Other"}:
        raise ApiError("Некорректная торговая сессия")
    grade = _text(payload, "grade", 1).upper()
    if grade not in {"", "A", "B", "C", "D"}:
        raise ApiError("Оценка сделки должна быть A, B, C или D")
    client_entry_id = _text(payload, "client_entry_id", 64)
    if client_entry_id and not CLIENT_ID_RE.fullmatch(client_entry_id):
        raise ApiError("Некорректный идентификатор записи")

    journal_mode = _text(payload, "journal_mode", 12, "backtest").lower()
    if journal_mode not in {"backtest", "demo", "live"}:
        raise ApiError("Некорректный режим журнала")
    outcome_type = _text(payload, "outcome_type", 16).lower()
    if outcome_type not in {"", "take", "stop", "breakeven", "manual", "cancelled"}:
        raise ApiError("Некорректный исход сделки")
    if status == "cancelled":
        outcome_type = "cancelled"
    elif status != "closed":
        outcome_type = ""
    confidence_before = _number(payload, "confidence_before")
    if confidence_before is not None and (
        not confidence_before.is_integer() or not 1 <= confidence_before <= 5
    ):
        raise ApiError("Уверенность перед входом должна быть от 1 до 5")
    weekly_plan_id = _number(payload, "weekly_plan_id")
    if weekly_plan_id is not None and (
        not weekly_plan_id.is_integer() or weekly_plan_id <= 0
    ):
        raise ApiError("Некорректный недельный план")
    idea_followed_raw = payload.get("idea_followed")
    if idea_followed_raw in (None, ""):
        idea_followed = None
    elif isinstance(idea_followed_raw, bool):
        idea_followed = 1 if idea_followed_raw else 0
    else:
        raise ApiError("Некорректная отметка следования недельной идее")
    countertrend_raw = payload.get("countertrend_confirmed")
    if countertrend_raw in (None, ""):
        countertrend_confirmed = None
    elif isinstance(countertrend_raw, bool):
        countertrend_confirmed = 1 if countertrend_raw else 0
    else:
        raise ApiError("Некорректная отметка подтверждения разворота")

    positive_values = {}
    for key in ("entry_price", "stop_loss", "take_profit", "volume", "risk_amount"):
        value = _number(payload, key)
        if value is not None and value < 0:
            raise ApiError(f"Поле {key} не может быть отрицательным")
        positive_values[key] = value
    pnl = _number(payload, "pnl", required=status == "closed", default=0.0) or 0.0
    if outcome_type == "take" and pnl < 0:
        raise ApiError("Тейк не может иметь отрицательный результат")
    if outcome_type == "stop" and pnl > 0:
        raise ApiError("Стоп не может иметь положительный результат")
    r_multiple = _number(payload, "r_multiple")
    if r_multiple is None and positive_values["risk_amount"]:
        r_multiple = round(pnl / positive_values["risk_amount"], 4)
    screenshot_url = _text(payload, "screenshot_url", 1000)
    if screenshot_url and urlparse(screenshot_url).scheme not in {"http", "https"}:
        raise ApiError("Ссылка на скриншот должна начинаться с http:// или https://")

    return {
        "traded_at": traded_at,
        "symbol": symbol,
        "direction": direction,
        "status": status,
        "client_entry_id": client_entry_id,
        "timeframe": _text(payload, "timeframe", 10),
        "session": session,
        "setup": _text(payload, "setup", 160),
        "grade": grade,
        "market_context": _text(payload, "market_context", 240),
        "journal_mode": journal_mode,
        "confidence_before": int(confidence_before) if confidence_before is not None else None,
        "trade_plan": _text(payload, "trade_plan", 1200),
        "entry_trigger": _text(payload, "entry_trigger", 500),
        "trade_invalidation": _text(payload, "trade_invalidation", 500),
        "outcome_type": outcome_type,
        "weekly_plan_id": int(weekly_plan_id) if weekly_plan_id is not None else None,
        "idea_followed": idea_followed,
        "countertrend_confirmed": countertrend_confirmed,
        **positive_values,
        "pnl": pnl,
        "r_multiple": r_multiple,
        "emotion_before": _text(payload, "emotion_before", 80),
        "emotion_after": _text(payload, "emotion_after", 80),
        "plan_followed": 1 if plan_followed else 0,
        "mistake": _text(payload, "mistake", 160),
        "note": _text(payload, "note", 1200),
        "screenshot_url": screenshot_url,
        "visibility": visibility,
    }


async def health(request: web.Request) -> web.Response:
    await _repo(request).ping()
    return web.json_response({"status": "ok", "service": "trader-journal"})


async def bootstrap(request: web.Request) -> web.Response:
    repo = _repo(request)
    user = _user(request)
    today = _date(request.query.get("date", date.today().isoformat()))
    start = (date.fromisoformat(today) - timedelta(days=29)).isoformat()
    circle = await repo.get_circle(user["id"])
    mood = await repo.get_mood(user["id"], today)
    trades = await repo.list_trades(
        user["id"], start_date=today, end_date=today, scope="me"
    )
    stats = await repo.stats(
        user["id"], start_date=start, end_date=today, scope="me"
    )
    settings = await repo.get_settings(user["id"])
    weekly_plans = await repo.list_weekly_plans(
        user["id"], week_start=_week_start(today), scope="me"
    )
    return web.json_response(
        {"user": user, "circle": circle, "settings": settings,
         "today_mood": mood, "today_trades": trades, "stats": stats,
         "weekly_plans": weekly_plans}
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
    values["focus"] = _text(payload, "focus", 240)
    values["lesson"] = _text(payload, "lesson", 500)
    values["journal_mode"] = _text(payload, "journal_mode", 12, "backtest").lower()
    if values["journal_mode"] not in {"backtest", "demo", "live"}:
        raise ApiError("Некорректный режим журнала")
    values["market_bias"] = _text(payload, "market_bias", 10, "NEUTRAL").upper()
    if values["market_bias"] not in {"LONG", "SHORT", "NEUTRAL"}:
        raise ApiError("Некорректное направление идеи дня")
    values["day_idea"] = _text(payload, "day_idea", 1200)
    values["key_levels"] = _text(payload, "key_levels", 500)
    values["day_invalidation"] = _text(payload, "day_invalidation", 500)
    values["news_context"] = _text(payload, "news_context", 500)
    result = await _repo(request).upsert_mood(_user(request)["id"], values)
    return web.json_response({"mood": result})


async def list_trades(request: web.Request) -> web.Response:
    today = date.today()
    start = _date(request.query.get("from", (today - timedelta(days=30)).isoformat()))
    end = _date(request.query.get("to", today.isoformat()))
    if start > end:
        raise ApiError("Начальная дата позже конечной")
    if (date.fromisoformat(end) - date.fromisoformat(start)).days > 366:
        raise ApiError("Диапазон сделок не может превышать 367 дней")
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
    if "from" in request.query or "to" in request.query:
        start = date.fromisoformat(_date(request.query.get("from", "")))
        end = date.fromisoformat(_date(request.query.get("to", "")))
        if start > end:
            raise ApiError("Начальная дата позже конечной")
        if (end - start).days > 62:
            raise ApiError("Обзор календаря не может превышать 63 дня")
        month = start.strftime("%Y-%m")
    else:
        month = request.query.get("month", date.today().strftime("%Y-%m"))
        if not MONTH_RE.fullmatch(month):
            raise ApiError("Месяц должен быть в формате YYYY-MM")
        try:
            start = date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise ApiError("Некорректный месяц") from exc
        if start.month == 12:
            next_month = date(start.year + 1, 1, 1)
        else:
            next_month = date(start.year, start.month + 1, 1)
        end = next_month - timedelta(days=1)
    days = await _repo(request).calendar(
        _user(request)["id"],
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        scope=_scope(request),
    )
    return web.json_response(
        {"month": month, "from": start.isoformat(), "to": end.isoformat(), "days": days}
    )


async def stats(request: web.Request) -> web.Response:
    start, end = _period(request)
    result = await _repo(request).stats(
        _user(request)["id"], start_date=start, end_date=end, scope=_scope(request)
    )
    return web.json_response({"from": start, "to": end, "stats": result})


async def list_weekly_plans(request: web.Request) -> web.Response:
    week = _week_start(request.query.get("week", date.today().isoformat()))
    plans = await _repo(request).list_weekly_plans(
        _user(request)["id"], week_start=week, scope=_scope(request)
    )
    return web.json_response({"week_start": week, "plans": plans})


async def create_weekly_plan(request: web.Request) -> web.Response:
    plan = await _repo(request).save_weekly_plan(
        _user(request)["id"], _weekly_plan_payload(await _json(request))
    )
    return web.json_response({"plan": plan}, status=201)


async def update_weekly_plan(request: web.Request) -> web.Response:
    plan = await _repo(request).save_weekly_plan(
        _user(request)["id"],
        _weekly_plan_payload(await _json(request)),
        int(request.match_info["plan_id"]),
    )
    return web.json_response({"plan": plan})


def _remove_plan_image(repo: JournalRepository, storage_name: str) -> None:
    directory = _plan_image_directory(repo).resolve()
    target = (directory / storage_name).resolve()
    if target.parent == directory:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


async def delete_weekly_plan(request: web.Request) -> web.Response:
    repo = _repo(request)
    files = await repo.delete_weekly_plan(
        _user(request)["id"], int(request.match_info["plan_id"])
    )
    for storage_name in files:
        _remove_plan_image(repo, storage_name)
    return web.json_response({"deleted": True})


async def upload_weekly_plan_image(request: web.Request) -> web.Response:
    repo = _repo(request)
    plan_id = int(request.match_info["plan_id"])
    reader = await request.multipart()
    field = await reader.next()
    if field is None or field.name != "image" or not field.filename:
        raise ApiError("Выберите фотографию торгового плана")
    content = await field.read(decode=False)
    if not content:
        raise ApiError("Фотография пустая")
    if len(content) > MAX_PLAN_IMAGE_BYTES:
        raise ApiError("Фотография должна быть меньше 6 МБ")
    detected = _plan_image_type(content)
    if detected is None:
        raise ApiError("Поддерживаются JPG, PNG и WebP")
    mime_type, extension = detected
    directory = _plan_image_directory(repo)
    directory.mkdir(parents=True, exist_ok=True)
    storage_name = f"{_user(request)['id']}_{secrets.token_urlsafe(18)}{extension}"
    target = directory / storage_name
    target.write_bytes(content)
    try:
        image = await repo.add_weekly_plan_image(
            _user(request)["id"],
            plan_id,
            {
                "storage_name": storage_name,
                "original_name": Path(field.filename).name[:160],
                "mime_type": mime_type,
                "size_bytes": len(content),
            },
        )
    except Exception:
        target.unlink(missing_ok=True)
        raise
    image["url"] = f"/api/weekly-plan-images/{image['id']}"
    image.pop("storage_name", None)
    return web.json_response({"image": image}, status=201)


async def weekly_plan_image(request: web.Request) -> web.StreamResponse:
    repo = _repo(request)
    image = await repo.get_weekly_plan_image(
        _user(request)["id"], int(request.match_info["image_id"])
    )
    directory = _plan_image_directory(repo).resolve()
    target = (directory / str(image["storage_name"])).resolve()
    if target.parent != directory or not target.is_file():
        raise ApiError("Файл фотографии не найден", status=404, code="not_found")
    response = web.FileResponse(target)
    response.content_type = str(image["mime_type"])
    response.headers["Content-Disposition"] = "inline"
    return response


async def delete_weekly_plan_image(request: web.Request) -> web.Response:
    repo = _repo(request)
    storage_name = await repo.delete_weekly_plan_image(
        _user(request)["id"], int(request.match_info["image_id"])
    )
    _remove_plan_image(repo, storage_name)
    return web.json_response({"deleted": True})


async def save_settings(request: web.Request) -> web.Response:
    payload = await _json(request)
    balance = _number(payload, "balance", required=True)
    if balance is None or not 100 <= balance <= 100_000_000:
        raise ApiError("Баланс должен быть от 100 до 100 000 000")

    percentages: dict[str, float] = {}
    for key in (
        "profit_target_pct", "daily_loss_limit_pct", "max_loss_limit_pct",
        "risk_per_trade_pct",
    ):
        value = _number(payload, key, required=True)
        if value is None or not 0 < value <= 100:
            raise ApiError(f"Поле {key} должно быть больше 0 и не больше 100")
        percentages[key] = value
    if percentages["daily_loss_limit_pct"] > percentages["max_loss_limit_pct"]:
        raise ApiError("Дневной лимит не может превышать максимальный лимит")

    max_trades = _number(payload, "max_trades_day", required=True)
    if max_trades is None or not max_trades.is_integer() or not 1 <= max_trades <= 100:
        raise ApiError("Лимит сделок должен быть целым числом от 1 до 100")
    currency = _text(payload, "currency", 3, "USD").upper()
    if currency not in {"USD", "EUR", "RUB"}:
        raise ApiError("Поддерживаются USD, EUR и RUB")

    values = {
        "account_name": _text(payload, "account_name", 60, "Funded account"),
        "balance": balance,
        "currency": currency,
        **percentages,
        "max_trades_day": int(max_trades),
    }
    settings = await _repo(request).update_settings(_user(request)["id"], values)
    return web.json_response({"settings": settings})


async def export_trades(request: web.Request) -> web.Response:
    trades = await _repo(request).export_dataset(_user(request)["id"])
    fields = [
        "traded_at", "journal_mode", "symbol", "direction", "status", "timeframe",
        "session", "setup", "confidence_before", "trade_plan", "entry_trigger",
        "trade_invalidation", "market_context", "entry_price", "stop_loss",
        "take_profit", "volume", "risk_amount", "outcome_type", "pnl", "r_multiple",
        "plan_followed", "grade", "mistake", "emotion_before", "emotion_after",
        "note", "screenshot_url", "day_mode", "day_bias", "day_idea",
        "day_key_levels", "day_invalidation", "day_news_context", "day_mood",
        "day_energy", "day_confidence", "day_discipline", "day_emotion", "day_focus",
        "day_lesson",
        "weekly_plan_id", "weekly_plan_title", "weekly_plan_idea",
        "weekly_plan_trade_plan", "weekly_plan_invalidation", "idea_followed",
        "countertrend_confirmed",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(trades)
    return web.Response(
        text="\ufeff" + output.getvalue(),
        content_type="text/csv",
        charset="utf-8",
        headers={"Content-Disposition": "attachment; filename=trading-journal.csv"},
    )


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
        web.get("/api/weekly-plans", list_weekly_plans),
        web.post("/api/weekly-plans", create_weekly_plan),
        web.put("/api/weekly-plans/{plan_id}", update_weekly_plan),
        web.delete("/api/weekly-plans/{plan_id}", delete_weekly_plan),
        web.post("/api/weekly-plans/{plan_id}/images", upload_weekly_plan_image),
        web.get("/api/weekly-plan-images/{image_id}", weekly_plan_image),
        web.delete("/api/weekly-plan-images/{image_id}", delete_weekly_plan_image),
        web.put("/api/settings", save_settings),
        web.get("/api/export", export_trades),
        web.post("/api/circles", create_circle),
        web.post("/api/circles/join", join_circle),
        web.post("/api/circles/leave", leave_circle),
    ]
