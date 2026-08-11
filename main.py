from __future__ import annotations

import sys

from aiohttp import web

from server.settings import ServerSettings
from server.web_app import create_app


def _startup_print(message: object) -> None:
    print(message, flush=True)


def main() -> None:
    _startup_print("[startup] bb.bike Mini App: loading configuration")
    try:
        settings = ServerSettings.from_env()
        application = create_app(settings)
    except Exception as exc:
        _startup_print(
            f"[startup] FAILED: {type(exc).__name__}: {exc}"
        )
        raise

    _startup_print(
        f"[startup] configuration OK; listening on "
        f"{settings.host}:{settings.port}"
    )
    web.run_app(
        application,
        host=settings.host,
        port=settings.port,
        print=_startup_print,
        access_log=None,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _startup_print("[shutdown] stopped")
    except Exception:
        sys.exit(1)
