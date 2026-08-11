from __future__ import annotations

import sys
import os
import platform
from pathlib import Path

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

    index_path = Path(__file__).resolve().with_name("index.html")
    _startup_print(
        f"[runtime] python={platform.python_version()} pid={os.getpid()} "
        f"cwd={Path.cwd()} index_bytes={index_path.stat().st_size}"
    )
    _startup_print(
        f"[startup] configuration OK; starting listener on "
        f"{settings.host}:{settings.port}"
    )
    try:
        web.run_app(
            application,
            host=settings.host,
            port=settings.port,
            print=_startup_print,
            access_log=None,
        )
    except Exception as exc:
        errno = getattr(exc, "errno", None)
        errno_suffix = f" errno={errno}" if isinstance(errno, int) else ""
        _startup_print(
            f"[runtime] FAILED: {type(exc).__name__}{errno_suffix}"
        )
        raise
    _startup_print("[shutdown] aiohttp server stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _startup_print("[shutdown] stopped")
    except Exception:
        sys.exit(1)
