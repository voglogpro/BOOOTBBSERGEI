from aiohttp import web

from journal.auth import Principal
from journal.config import Settings
from journal.repository import JournalRepository


SETTINGS_KEY = web.AppKey("settings", Settings)
REPOSITORY_KEY = web.AppKey("repository", JournalRepository)
PRINCIPAL_KEY = web.RequestKey("principal", Principal)
USER_KEY = web.RequestKey("user", dict)
