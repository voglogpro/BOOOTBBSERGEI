from __future__ import annotations

import html
import secrets
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from aiohttp import web

from journal.api import ApiError, routes
from journal.auth import (
    AuthorizationError,
    create_web_session,
    dev_principal,
    validate_init_data,
    validate_login_data,
    validate_web_session,
)
from journal.config import Settings
from journal.db import initialize_database
from journal.keys import PRINCIPAL_KEY, REPOSITORY_KEY, SETTINGS_KEY, USER_KEY
from journal.repository import JournalRepository, RepositoryError


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
INDEX = ROOT / "index.html"
CLIENT = STATIC / "client.browser"
LOGIN_CLIENT = STATIC / "login.browser"
WEB_LOGIN = ROOT / "web_login.html"
SESSION_COOKIE = "journal_session"
LOGIN_STATE_COOKIE = "journal_login_state"
SESSION_LIFETIME_SECONDS = 30 * 86400


def _external_host(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-Host", "").split(",", 1)[0].strip()
    return forwarded or request.host


@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except AuthorizationError:
        return web.json_response(
            {"error": "unauthorized", "message": "Войдите через Telegram или откройте Mini App заново."},
            status=401,
        )
    except (ApiError, RepositoryError) as exc:
        status = exc.status if isinstance(exc, ApiError) else 400
        code = exc.code if isinstance(exc, ApiError) else "invalid_operation"
        return web.json_response({"error": code, "message": str(exc)}, status=status)
    except (ValueError, TypeError):
        return web.json_response(
            {"error": "bad_request", "message": "Некорректный запрос"}, status=400
        )


@web.middleware
async def security_headers_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' https://telegram.org; "
        "style-src 'self'; "
        "img-src 'self' data: blob: https:; "
        "connect-src 'self'; frame-src https://oauth.telegram.org https://telegram.org; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    if request.path.startswith(("/api/", "/auth/")) or request.path in {"/", "/login"}:
        response.headers["Cache-Control"] = "no-store"
    return response


@web.middleware
async def telegram_auth_middleware(
    request: web.Request, handler: Any
) -> web.StreamResponse:
    if not request.path.startswith("/api/"):
        return await handler(request)
    settings = request.app[SETTINGS_KEY]
    raw_init_data = request.headers.get("X-Telegram-Init-Data", "")
    session_token = request.cookies.get(SESSION_COOKIE, "")
    if raw_init_data:
        principal = validate_init_data(
            raw_init_data,
            bot_token=settings.bot_token,
            allowed_user_ids=settings.allowed_user_ids,
            max_age_seconds=settings.max_auth_age_seconds,
        )
    elif session_token:
        principal = validate_web_session(
            session_token,
            bot_token=settings.bot_token,
            allowed_user_ids=settings.allowed_user_ids,
        )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin", "")
            parsed = urlparse(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.netloc.lower() != _external_host(request).lower()
            ):
                raise AuthorizationError("invalid website request origin")
    elif settings.dev_mode:
        principal = dev_principal(settings.dev_user_id, settings.dev_user_name)
    else:
        raise AuthorizationError("website login is required")
    request[PRINCIPAL_KEY] = principal
    request[USER_KEY] = await request.app[REPOSITORY_KEY].upsert_user(principal)
    return await handler(request)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(INDEX)


async def client_script(_: web.Request) -> web.Response:
    return web.Response(
        body=CLIENT.read_bytes(),
        content_type="application/javascript",
        charset="utf-8",
    )


async def login_client_script(_: web.Request) -> web.Response:
    return web.Response(
        body=LOGIN_CLIENT.read_bytes(),
        content_type="application/javascript",
        charset="utf-8",
    )


def _secure_cookie(request: web.Request) -> bool:
    settings = request.app[SETTINGS_KEY]
    forwarded = request.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip()
    return not settings.dev_mode or request.secure or forwarded == "https"


def _redirect(location: str) -> web.Response:
    return web.Response(status=303, headers={"Location": location})


async def website_login(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    existing = request.cookies.get(SESSION_COOKIE, "")
    if existing:
        try:
            validate_web_session(
                existing,
                bot_token=settings.bot_token,
                allowed_user_ids=settings.allowed_user_ids,
            )
            return _redirect("/")
        except AuthorizationError:
            pass

    state = secrets.token_urlsafe(24)
    proto = request.headers.get("X-Forwarded-Proto", request.scheme).split(",", 1)[0].strip()
    callback = f"{proto}://{_external_host(request)}/auth/telegram"
    auth_url = f"{callback}?{urlencode({'state': state})}"
    if settings.bot_username:
        widget = (
            '<script async src="https://telegram.org/js/telegram-widget.js?22" '
            f'data-telegram-login="{html.escape(settings.bot_username, quote=True)}" '
            'data-size="large" data-radius="10" data-userpic="false" '
            f'data-auth-url="{html.escape(auth_url, quote=True)}"></script>'
        )
        setup_copy = "Войдите тем же Telegram-аккаунтом, который используете в Mini App."
    else:
        widget = '<div class="login-notice">В переменных BotHost не указан BOT_USERNAME.</div>'
        setup_copy = "Добавьте имя бота без символа @ и выполните новый деплой."
    template = WEB_LOGIN.read_text(encoding="utf-8")
    body = (
        template.replace("{{TELEGRAM_WIDGET}}", widget)
        .replace("{{SETUP_COPY}}", html.escape(setup_copy))
        .replace(
            "{{ERROR_COPY}}",
            "Не удалось подтвердить вход. Попробуйте ещё раз."
            if request.query.get("error")
            else "",
        )
    )
    response = web.Response(text=body, content_type="text/html", charset="utf-8")
    response.set_cookie(
        LOGIN_STATE_COOKIE,
        state,
        max_age=600,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="Lax",
        path="/",
    )
    return response


async def telegram_login_callback(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    state = request.query.get("state", "")
    expected_state = request.cookies.get(LOGIN_STATE_COOKIE, "")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return _redirect("/login?error=1")
    login_fields = {
        key: request.query[key]
        for key in (
            "id",
            "first_name",
            "last_name",
            "username",
            "photo_url",
            "auth_date",
            "hash",
        )
        if key in request.query
    }
    try:
        principal = validate_login_data(
            login_fields,
            bot_token=settings.bot_token,
            allowed_user_ids=settings.allowed_user_ids,
            max_age_seconds=settings.max_auth_age_seconds,
        )
    except AuthorizationError:
        return _redirect("/login?error=1")

    await request.app[REPOSITORY_KEY].upsert_user(principal)
    response = _redirect("/")
    response.set_cookie(
        SESSION_COOKIE,
        create_web_session(
            principal,
            bot_token=settings.bot_token,
            lifetime_seconds=SESSION_LIFETIME_SECONDS,
        ),
        max_age=SESSION_LIFETIME_SECONDS,
        httponly=True,
        secure=_secure_cookie(request),
        samesite="Lax",
        path="/",
    )
    response.del_cookie(LOGIN_STATE_COOKIE, path="/")
    return response


async def website_logout(request: web.Request) -> web.Response:
    origin = request.headers.get("Origin", "")
    if origin:
        parsed = urlparse(origin)
        if parsed.netloc.lower() != _external_host(request).lower():
            raise AuthorizationError("invalid website request origin")
    response = web.json_response({"status": "ok"})
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


async def startup(app: web.Application) -> None:
    await initialize_database(app[SETTINGS_KEY].database_path)


def create_app(settings: Settings) -> web.Application:
    app = web.Application(
        middlewares=[security_headers_middleware, error_middleware, telegram_auth_middleware],
        # JSON entries stay tiny; the larger limit is reserved for protected
        # weekly-plan chart images (the endpoint enforces a stricter 6 MB cap).
        client_max_size=8 * 1024 * 1024,
    )
    app[SETTINGS_KEY] = settings
    app[REPOSITORY_KEY] = JournalRepository(settings.database_path)
    app.on_startup.append(startup)
    app.add_routes(routes())
    app.router.add_get("/", index)
    app.router.add_get("/login", website_login)
    app.router.add_get("/auth/telegram", telegram_login_callback)
    app.router.add_post("/auth/logout", website_logout)
    app.router.add_get("/client", client_script)
    app.router.add_get("/login-client", login_client_script)
    app.router.add_static("/static", STATIC, show_index=False, append_version=True)
    return app
