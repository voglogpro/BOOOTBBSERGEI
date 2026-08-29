from __future__ import annotations

import hashlib
import hmac
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from journal.app import LOGIN_STATE_COOKIE, SESSION_COOKIE, create_app
from journal.config import Settings


TOKEN = "123456:website-test-token"


def signed_login(user_id: int, auth_date: int) -> dict[str, str]:
    fields = {
        "id": str(user_id),
        "first_name": "Анна",
        "username": "journal_owner",
        "auth_date": str(auth_date),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hashlib.sha256(TOKEN.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


def signed_mini_app(user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-sync-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Анна", "username": "journal_owner"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class WebsiteLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        settings = Settings(
            bot_token=TOKEN,
            bot_username="JournalTestBot",
            database_path=Path(self.temp.name) / "website.sqlite3",
        )
        self.client = TestClient(TestServer(create_app(settings)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_website_and_mini_app_use_the_same_user_and_data(self) -> None:
        login = await self.client.get("/login")
        self.assertEqual(login.status, 200)
        self.assertIn("JournalTestBot", await login.text())
        state = login.cookies[LOGIN_STATE_COOKIE].value

        now = int(time.time())
        query = {"state": state, **signed_login(777, now)}
        callback = await self.client.get(
            "/auth/telegram",
            params=query,
            headers={"Cookie": f"{LOGIN_STATE_COOKIE}={state}"},
            allow_redirects=False,
        )
        self.assertEqual(callback.status, 303)
        session = callback.cookies[SESSION_COOKIE].value

        website_headers = {"Cookie": f"{SESSION_COOKIE}={session}"}
        website = await self.client.get("/api/bootstrap", headers=website_headers)
        self.assertEqual(website.status, 200)
        website_data = await website.json()
        self.assertEqual(website_data["user"]["telegram_id"], 777)

        origin = str(self.client.make_url("/")).rstrip("/")
        mood = await self.client.put(
            f"/api/moods/{time.strftime('%Y-%m-%d')}",
            headers={**website_headers, "Origin": origin},
            json={
                "mood": 5,
                "energy": 4,
                "confidence": 4,
                "discipline": 5,
                "focus": "Один подтверждённый сетап",
                "visibility": "private",
            },
        )
        self.assertEqual(mood.status, 200)

        mini_app = await self.client.get(
            f"/api/bootstrap?date={time.strftime('%Y-%m-%d')}",
            headers={"X-Telegram-Init-Data": signed_mini_app(777, now)},
        )
        self.assertEqual(mini_app.status, 200)
        mini_data = await mini_app.json()
        self.assertEqual(mini_data["user"]["id"], website_data["user"]["id"])
        self.assertEqual(mini_data["today_mood"]["focus"], "Один подтверждённый сетап")

    async def test_cookie_writes_require_same_origin(self) -> None:
        login = await self.client.get("/login")
        state = login.cookies[LOGIN_STATE_COOKIE].value
        callback = await self.client.get(
            "/auth/telegram",
            params={"state": state, **signed_login(777, int(time.time()))},
            headers={"Cookie": f"{LOGIN_STATE_COOKIE}={state}"},
            allow_redirects=False,
        )
        session = callback.cookies[SESSION_COOKIE].value
        response = await self.client.put(
            f"/api/moods/{time.strftime('%Y-%m-%d')}",
            headers={"Cookie": f"{SESSION_COOKIE}={session}"},
            json={"mood": 3, "energy": 3, "confidence": 3, "discipline": 3},
        )
        self.assertEqual(response.status, 401)


if __name__ == "__main__":
    unittest.main()
