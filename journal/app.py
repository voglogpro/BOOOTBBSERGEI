from __future__ import annotations

from pathlib import Path
from typing import Any

from aiohttp import web

from journal.api import ApiError, routes
from journal.auth import AuthorizationError, dev_principal, validate_init_data
from journal.config import Settings
from journal.db import initialize_database
from journal.keys import PRINCIPAL_KEY, REPOSITORY_KEY, SETTINGS_KEY, USER_KEY
from journal.repository import JournalRepository, RepositoryError


ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
@web.middleware
async def error_middleware(request: web.Request, handler: Any) -> web.StreamResponse:
    try:
        return await handler(request)
    except AuthorizationError:
        return web.json_response(
            {"error": "unauthorized", "message": "Откройте приложение заново из Telegram."},
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
        "img-src 'self' data: https:; "
        "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
    )
    if request.path.startswith("/api/") or request.path == "/":
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
    if settings.dev_mode and not raw_init_data:
        principal = dev_principal(settings.dev_user_id, settings.dev_user_name)
    else:
        principal = validate_init_data(
            raw_init_data,
            bot_token=settings.bot_token,
            allowed_user_ids=settings.allowed_user_ids,
            max_age_seconds=settings.max_auth_age_seconds,
        )
    request[PRINCIPAL_KEY] = principal
    request[USER_KEY] = await request.app[REPOSITORY_KEY].upsert_user(principal)
    return await handler(request)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def startup(app: web.Application) -> None:
    await initialize_database(app[SETTINGS_KEY].database_path)


def create_app(settings: Settings) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, security_headers_middleware, telegram_auth_middleware],
        client_max_size=64 * 1024,
    )
    app[SETTINGS_KEY] = settings
    app[REPOSITORY_KEY] = JournalRepository(settings.database_path)
    app.on_startup.append(startup)
    app.add_routes(routes())
    app.router.add_get("/", index)
    app.router.add_static("/static", STATIC, show_index=False, append_version=True)
    return app
