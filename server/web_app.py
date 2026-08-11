from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, web

from server.miniapp_auth import (
    MAX_INIT_DATA_BYTES,
    MiniAppAuthorizationError,
    MiniAppPrincipal,
    validate_miniapp_init_data,
)
from server.live_reader import LiveReaderService
from server.parser_login import ParserLoginError, ParserLoginService
from server.session_store import EncryptedSessionStore
from server.settings import ServerSettings
from server.source_service import SourceManagementService, SourceServiceError


LOGGER = logging.getLogger(__name__)
WEB_INDEX = Path(__file__).resolve().parent.parent / "index.html"
MAX_JSON_BODY_BYTES = 4096
SELF_CHECK_INITIAL_DELAY_SECONDS = 2
SELF_CHECK_RETRY_SECONDS = 2
SELF_CHECK_INTERVAL_SECONDS = 60

SETTINGS_KEY = web.AppKey("settings", ServerSettings)
LOGIN_SERVICE_KEY = web.AppKey("login_service", ParserLoginService)
SOURCE_SERVICE_KEY = web.AppKey("source_service", SourceManagementService)
LIVE_READER_KEY = web.AppKey("live_reader", LiveReaderService)
CSP_NONCE_KEY = web.RequestKey("csp_nonce", str)
DIAGNOSTICS_TASK_KEY = web.AppKey("diagnostics_task", asyncio.Task)
SAFE_DIAGNOSTIC_PATHS = frozenset(
    {
        "/",
        "/health",
        "/api/telegram/auth/status",
        "/api/telegram/auth/phone",
        "/api/telegram/auth/code",
        "/api/telegram/auth/password",
        "/api/telegram/auth/cancel",
        "/api/sources/catalog",
        "/api/sources",
        "/api/reader/status",
    }
)


class ApiRequestError(ValueError):
    pass


def _diagnostic_print(message: str) -> None:
    print(message, flush=True)


def _safe_request_path(request: web.Request) -> str:
    if request.path in SAFE_DIAGNOSTIC_PATHS:
        return request.path
    return "<unmatched>"


def _json_response(
    payload: dict[str, object],
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
) -> web.Response:
    return web.json_response(payload, status=status, headers=headers)


@web.middleware
async def diagnostic_request_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    started_at = time.perf_counter()
    safe_path = _safe_request_path(request)
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        _diagnostic_print(
            f"[http] {request.method} {safe_path} -> {exc.status} "
            f"duration_ms={duration_ms}"
        )
        raise
    except Exception:
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        _diagnostic_print(
            f"[http] {request.method} {safe_path} -> 500 "
            f"duration_ms={duration_ms}"
        )
        raise
    duration_ms = round((time.perf_counter() - started_at) * 1000)
    _diagnostic_print(
        f"[http] {request.method} {safe_path} -> {response.status} "
        f"duration_ms={duration_ms}"
    )
    return response


@web.middleware
async def security_headers_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
    )
    nonce = request.get(CSP_NONCE_KEY)
    nonce_source = f" 'nonce-{nonce}'" if nonce else ""
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        f"script-src 'self' https://telegram.org{nonce_source}; "
        f"style-src 'self'{nonce_source}; "
        "connect-src 'self'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    return response


@web.middleware
async def api_error_middleware(
    request: web.Request,
    handler: Any,
) -> web.StreamResponse:
    try:
        return await handler(request)
    except MiniAppAuthorizationError:
        return _json_response(
            {
                "state": "unauthorized",
                "error": "unauthorized",
                "message": (
                    "Доступ Mini App недействителен. Полностью закройте и "
                    "снова откройте приложение из Telegram."
                ),
            },
            status=401,
        )
    except ParserLoginError as exc:
        payload: dict[str, object] = {
            "state": "locked" if exc.http_status == 429 else "unauthorized",
            "error": exc.code,
            "message": exc.public_message,
        }
        headers: dict[str, str] | None = None
        if exc.retry_after is not None:
            payload["retry_after"] = exc.retry_after
            headers = {"Retry-After": str(exc.retry_after)}
        return _json_response(
            payload,
            status=exc.http_status,
            headers=headers,
        )
    except SourceServiceError as exc:
        payload = {
            "error": exc.code,
            "message": exc.public_message,
        }
        headers = None
        if exc.retry_after is not None:
            payload["retry_after"] = exc.retry_after
            headers = {"Retry-After": str(exc.retry_after)}
        return _json_response(
            payload,
            status=exc.http_status,
            headers=headers,
        )
    except ApiRequestError as exc:
        return _json_response(
            {
                "state": "unauthorized",
                "error": "invalid_request",
                "message": str(exc),
            },
            status=400,
        )
    except web.HTTPRequestEntityTooLarge:
        return _json_response(
            {
                "state": "unauthorized",
                "error": "request_too_large",
                "message": "Запрос слишком большой.",
            },
            status=413,
        )
    except web.HTTPException:
        raise
    except Exception as exc:
        LOGGER.error("Unhandled web error: %s", type(exc).__name__)
        return _json_response(
            {
                "state": "unauthorized",
                "error": "server_error",
                "message": "Внутренняя ошибка сервера.",
            },
            status=500,
        )


def _authorize(request: web.Request) -> MiniAppPrincipal:
    authorization_values = request.headers.getall("Authorization", [])
    if len(authorization_values) != 1:
        raise MiniAppAuthorizationError("Telegram authorization is required")
    value = authorization_values[0]
    if not value.startswith("tma "):
        raise MiniAppAuthorizationError("Telegram authorization is required")
    raw_init_data = value[4:]
    if len(raw_init_data.encode("utf-8")) > MAX_INIT_DATA_BYTES:
        raise MiniAppAuthorizationError("Telegram authorization is invalid")
    settings = request.app[SETTINGS_KEY]
    return validate_miniapp_init_data(
        raw_init_data,
        bot_token=settings.bot_token,
        allowed_user_ids=settings.admin_telegram_ids,
        max_age_seconds=settings.init_data_max_age_seconds,
    )


async def _read_exact_json(
    request: web.Request,
    *,
    required_fields: frozenset[str],
) -> dict[str, object]:
    if request.query_string:
        raise ApiRequestError("Параметры запроса здесь не поддерживаются.")
    if request.content_length is not None and request.content_length > MAX_JSON_BODY_BYTES:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_JSON_BODY_BYTES,
            actual_size=request.content_length,
        )
    if request.content_type != "application/json":
        raise ApiRequestError("Ожидается JSON-запрос.")
    try:
        body = await request.json(loads=json.loads)
    except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
        raise ApiRequestError("Некорректный JSON-запрос.") from exc
    if not isinstance(body, dict) or set(body) != required_fields:
        raise ApiRequestError("Набор полей запроса не поддерживается.")
    return body


async def index(request: web.Request) -> web.Response:
    nonce = secrets.token_urlsafe(24)
    request[CSP_NONCE_KEY] = nonce
    document = WEB_INDEX.read_text(encoding="utf-8").replace(
        "__CSP_NONCE__",
        nonce,
    )
    return web.Response(text=document, content_type="text/html", charset="utf-8")


async def health(request: web.Request) -> web.Response:
    return _json_response({"ok": True})


async def auth_status(request: web.Request) -> web.Response:
    principal = _authorize(request)
    status = await request.app[LOGIN_SERVICE_KEY].status(principal.user_id)
    if status.state == "authorized":
        await _resume_live_reader_if_stopped(request.app)
    return _json_response(status.as_dict())


async def auth_phone(request: web.Request) -> web.Response:
    principal = _authorize(request)
    body = await _read_exact_json(
        request,
        required_fields=frozenset({"phone", "replace"}),
    )
    status = await request.app[LOGIN_SERVICE_KEY].request_code(
        principal.user_id,
        body["phone"],
        replace_existing=body["replace"],
    )
    return _json_response(status.as_dict())


async def auth_code(request: web.Request) -> web.Response:
    principal = _authorize(request)
    body = await _read_exact_json(
        request,
        required_fields=frozenset({"flow_id", "code"}),
    )
    status = await request.app[LOGIN_SERVICE_KEY].confirm_code(
        principal.user_id,
        body["flow_id"],
        body["code"],
    )
    if status.state == "authorized":
        await _restart_live_reader(request.app)
    return _json_response(status.as_dict())


async def auth_password(request: web.Request) -> web.Response:
    principal = _authorize(request)
    body = await _read_exact_json(
        request,
        required_fields=frozenset({"flow_id", "password"}),
    )
    status = await request.app[LOGIN_SERVICE_KEY].confirm_password(
        principal.user_id,
        body["flow_id"],
        body["password"],
    )
    if status.state == "authorized":
        await _restart_live_reader(request.app)
    return _json_response(status.as_dict())


async def auth_cancel(request: web.Request) -> web.Response:
    principal = _authorize(request)
    body = await _read_exact_json(
        request,
        required_fields=frozenset({"flow_id"}),
    )
    status = await request.app[LOGIN_SERVICE_KEY].cancel(
        principal.user_id,
        body["flow_id"],
    )
    return _json_response(status.as_dict())


def _source_id(request: web.Request) -> int:
    raw = request.match_info.get("source_id", "")
    try:
        source_id = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiRequestError("Некорректный идентификатор источника.") from exc
    if source_id <= 0:
        raise ApiRequestError("Некорректный идентификатор источника.")
    return source_id


async def sources_catalog(request: web.Request) -> web.Response:
    _authorize(request)
    if request.query_string:
        raise ApiRequestError("Параметры запроса здесь не поддерживаются.")
    payload = await request.app[SOURCE_SERVICE_KEY].catalog()
    return _json_response(payload)


async def sources_list(request: web.Request) -> web.Response:
    _authorize(request)
    if request.query_string:
        raise ApiRequestError("Параметры запроса здесь не поддерживаются.")
    sources = await request.app[SOURCE_SERVICE_KEY].sources()
    return _json_response({"sources": sources})


async def source_verify(request: web.Request) -> web.Response:
    principal = await _authorize_source_mutation(request)
    await _read_exact_json(request, required_fields=frozenset())
    source = await request.app[SOURCE_SERVICE_KEY].verify(
        handle=request.match_info.get("handle", ""),
        actor_telegram_id=principal.user_id,
    )
    return _json_response({"source": source})


async def source_enable(request: web.Request) -> web.Response:
    principal = await _authorize_source_mutation(request)
    await _read_exact_json(request, required_fields=frozenset())
    source = await request.app[SOURCE_SERVICE_KEY].enable(
        source_id=_source_id(request),
        actor_telegram_id=principal.user_id,
    )
    return _json_response({"source": source})


async def source_disable(request: web.Request) -> web.Response:
    principal = await _authorize_source_mutation(request)
    await _read_exact_json(request, required_fields=frozenset())
    source = await request.app[SOURCE_SERVICE_KEY].disable(
        source_id=_source_id(request),
        actor_telegram_id=principal.user_id,
    )
    return _json_response({"source": source})


async def reader_status(request: web.Request) -> web.Response:
    _authorize(request)
    if request.query_string:
        raise ApiRequestError("Параметры запроса здесь не поддерживаются.")
    snapshot = await request.app[LIVE_READER_KEY].status()
    if snapshot is None:
        return _json_response(
            {
                "state": "stopped",
                "active_source_count": 0,
                "pending_event_count": 0,
                "heartbeat_at": None,
                "last_error_code": None,
                "updated_at": None,
            }
        )
    return _json_response(
        {
            "state": snapshot.state,
            "active_source_count": snapshot.active_source_count,
            "pending_event_count": snapshot.pending_event_count,
            "heartbeat_at": snapshot.heartbeat_at,
            "last_error_code": snapshot.last_error_code,
            "updated_at": snapshot.updated_at,
        }
    )


async def _authorize_source_mutation(
    request: web.Request,
) -> MiniAppPrincipal:
    principal = _authorize(request)
    status = await request.app[LOGIN_SERVICE_KEY].status(principal.user_id)
    if status.state != "authorized":
        raise SourceServiceError(
            "login_in_progress",
            "Сначала завершите или отмените вход в Telegram Reader.",
            http_status=409,
        )
    return principal


async def _pause_live_reader(app: web.Application) -> bool:
    reader = app.get(LIVE_READER_KEY)
    if reader is None:
        return True
    try:
        await reader.stop()
        return True
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _diagnostic_print(
            f"[reader] pause failed; error={type(exc).__name__}"
        )
        return False


async def _restart_live_reader(app: web.Application) -> None:
    reader = app.get(LIVE_READER_KEY)
    if reader is None:
        return
    if not await _pause_live_reader(app):
        return
    try:
        await reader.start()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _diagnostic_print(
            f"[reader] restart failed; error={type(exc).__name__}"
        )


async def _resume_live_reader_if_stopped(app: web.Application) -> None:
    reader = app.get(LIVE_READER_KEY)
    if reader is None:
        return
    try:
        snapshot = await reader.status()
        if snapshot is None or snapshot.state == "stopped":
            await reader.start()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _diagnostic_print(
            f"[reader] resume failed; error={type(exc).__name__}"
        )


async def _start_live_reader(app: web.Application) -> None:
    reader = app.get(LIVE_READER_KEY)
    if reader is None:
        return
    try:
        await reader.start()
        snapshot = await reader.status()
        state = snapshot.state if snapshot is not None else "stopped"
        active = snapshot.active_source_count if snapshot is not None else 0
        _diagnostic_print(
            f"[reader] initialized; state={state} active_sources={active}"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _diagnostic_print(
            f"[reader] initialization failed; error={type(exc).__name__}"
        )
        raise


async def _runtime_diagnostics(app: web.Application) -> None:
    settings = app[SETTINGS_KEY]
    health_url = f"http://127.0.0.1:{settings.port}/health"
    timeout = ClientTimeout(total=5)
    await asyncio.sleep(SELF_CHECK_INITIAL_DELAY_SECONDS)
    first_success = False
    async with ClientSession(timeout=timeout, trust_env=False) as session:
        while True:
            try:
                async with session.get(
                    health_url,
                    allow_redirects=False,
                ) as response:
                    await response.read()
                    _diagnostic_print(
                        f"[selfcheck] GET /health -> {response.status}; "
                        f"local_port={settings.port}"
                    )
                    first_success = response.status == 200
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _diagnostic_print(
                    f"[selfcheck] GET /health -> FAILED; "
                    f"error={type(exc).__name__} local_port={settings.port}"
                )
            delay = (
                SELF_CHECK_INTERVAL_SECONDS
                if first_success
                else SELF_CHECK_RETRY_SECONDS
            )
            await asyncio.sleep(delay)


async def _start_runtime_diagnostics(app: web.Application) -> None:
    settings = app[SETTINGS_KEY]
    _diagnostic_print(
        f"[lifecycle] application initialized; "
        f"selfcheck_port={settings.port}"
    )
    app[DIAGNOSTICS_TASK_KEY] = asyncio.create_task(
        _runtime_diagnostics(app),
        name="bb-bike-runtime-diagnostics",
    )


async def _cleanup_login_service(app: web.Application) -> None:
    diagnostics_task = app.get(DIAGNOSTICS_TASK_KEY)
    try:
        if diagnostics_task is not None:
            diagnostics_task.cancel()
            try:
                await diagnostics_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _diagnostic_print(
                    f"[lifecycle] diagnostics task failed; "
                    f"error={type(exc).__name__}"
                )
    finally:
        _diagnostic_print("[lifecycle] diagnostics stopped")
        try:
            await _pause_live_reader(app)
        finally:
            await app[LOGIN_SERVICE_KEY].close()


def create_app(
    settings: ServerSettings,
    *,
    login_service: ParserLoginService | None = None,
    source_service: SourceManagementService | None = None,
    live_reader: LiveReaderService | None = None,
    runtime_diagnostics: bool = True,
) -> web.Application:
    if not WEB_INDEX.is_file():
        raise RuntimeError("index.html is missing")
    if login_service is None:
        store = EncryptedSessionStore(
            encryption_key=settings.session_encryption_key,
            path=settings.encrypted_session_path,
        )
        login_service = ParserLoginService(
            settings=settings,
            session_store=store,
        )

    app = web.Application(
        middlewares=[
            diagnostic_request_middleware,
            security_headers_middleware,
            api_error_middleware,
        ],
        client_max_size=MAX_JSON_BODY_BYTES,
    )
    app[SETTINGS_KEY] = settings
    app[LOGIN_SERVICE_KEY] = login_service
    if source_service is not None:
        app[SOURCE_SERVICE_KEY] = source_service
    if live_reader is not None:
        app[LIVE_READER_KEY] = live_reader
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/telegram/auth/status", auth_status)
    app.router.add_post("/api/telegram/auth/phone", auth_phone)
    app.router.add_post("/api/telegram/auth/code", auth_code)
    app.router.add_post("/api/telegram/auth/password", auth_password)
    app.router.add_post("/api/telegram/auth/cancel", auth_cancel)
    if source_service is not None:
        app.router.add_get("/api/sources/catalog", sources_catalog)
        app.router.add_get("/api/sources", sources_list)
        app.router.add_post("/api/sources/{handle}/verify", source_verify)
        app.router.add_post("/api/sources/{source_id}/enable", source_enable)
        app.router.add_post("/api/sources/{source_id}/disable", source_disable)
    if live_reader is not None:
        app.router.add_get("/api/reader/status", reader_status)
        app.on_startup.append(_start_live_reader)
    if runtime_diagnostics:
        app.on_startup.append(_start_runtime_diagnostics)
    app.on_cleanup.append(_cleanup_login_service)
    return app
