from __future__ import annotations

from aiohttp import web

from server.settings import ServerSettings
from server.web_app import create_app


def main() -> None:
    settings = ServerSettings.from_env()
    web.run_app(
        create_app(settings),
        host=settings.host,
        port=settings.port,
        print=None,
        access_log=None,
    )


if __name__ == "__main__":
    main()
