from __future__ import annotations

from journal.app import create_app
from journal.config import Settings


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    from aiohttp import web

    web.run_app(app, host="0.0.0.0", port=settings.port)


if __name__ == "__main__":
    main()
