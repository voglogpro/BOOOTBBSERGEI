from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from aiohttp.test_utils import unused_port
from cryptography.fernet import Fernet

from server.parser_login import LoginStatus, ParserLoginError
from server.settings import ServerSettings
from server.web_app import (
    DIAGNOSTICS_TASK_KEY,
    LOGIN_SERVICE_KEY,
    _cleanup_login_service,
    create_app,
)


BOT_TOKEN = "123456:web-test-secret"


def signed_header(*, user_id: int = 111, tamper: bool = False) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "web-test-query",
        "user": json.dumps({"id": user_id}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        fields["query_id"] = "tampered"
    return f"tma {urlencode(fields)}"


def settings(path: Path, *, port: int = 8080) -> ServerSettings:
    return ServerSettings(
        bot_token=BOT_TOKEN,
        admin_telegram_ids=frozenset({111}),
        telegram_api_id=123456,
        telegram_api_hash="0123456789abcdef0123456789abcdef",
        telegram_expected_user_id=777,
        session_encryption_key=Fernet.generate_key().decode("ascii"),
        encrypted_session_path=path,
        host="0.0.0.0",
        port=port,
        init_data_max_age_seconds=300,
        login_challenge_ttl_seconds=300,
    )


class FakeLoginService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        self.status_result = LoginStatus(state="phone_required")
        self.error: ParserLoginError | None = None

    async def status(self, admin_id: int) -> LoginStatus:
        self.calls.append(("status", admin_id))
        if self.error:
            raise self.error
        return self.status_result

    async def request_code(
        self,
        admin_id: int,
        phone: object,
        *,
        replace_existing: object = False,
    ) -> LoginStatus:
        self.calls.append(("phone", admin_id, phone, replace_existing))
        if self.error:
            raise self.error
        return LoginStatus(state="code_required", flow_id="flow-1")

    async def confirm_code(
        self, admin_id: int, flow_id: object, code: object
    ) -> LoginStatus:
        self.calls.append(("code", admin_id, flow_id, code))
        return LoginStatus(state="password_required", flow_id=str(flow_id))

    async def confirm_password(
        self, admin_id: int, flow_id: object, password: object
    ) -> LoginStatus:
        self.calls.append(("password", admin_id, flow_id, password))
        return LoginStatus(state="authorized", account_user_id=777)

    async def cancel(self, admin_id: int, flow_id: object) -> LoginStatus:
        self.calls.append(("cancel", admin_id, flow_id))
        return LoginStatus(state="phone_required")

    async def close(self) -> None:
        self.closed = True


class WebAppTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.service = FakeLoginService()
        app = create_app(
            settings(Path(self.temp_dir.name) / "reader.enc"),
            login_service=self.service,  # type: ignore[arg-type]
            runtime_diagnostics=False,
        )
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp_dir.cleanup()

    async def test_status_requires_valid_telegram_init_data(self) -> None:
        response = await self.client.get("/api/telegram/auth/status")
        self.assertEqual(response.status, 401)
        payload = await response.json()
        self.assertEqual(payload["state"], "unauthorized")
        self.assertEqual(self.service.calls, [])

        response = await self.client.get(
            "/api/telegram/auth/status",
            headers={"Authorization": signed_header(tamper=True)},
        )
        self.assertEqual(response.status, 401)
        self.assertEqual(self.service.calls, [])

    async def test_status_is_bound_to_allowlisted_admin(self) -> None:
        response = await self.client.get(
            "/api/telegram/auth/status",
            headers={"Authorization": signed_header()},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["state"], "phone_required")
        self.assertEqual(self.service.calls, [("status", 111)])
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors", response.headers["Content-Security-Policy"])

    async def test_phone_route_accepts_only_exact_json_shape(self) -> None:
        headers = {"Authorization": signed_header()}
        response = await self.client.post(
            "/api/telegram/auth/phone",
            headers=headers,
            json={"phone": "+79991234567", "replace": False, "extra": "not-allowed"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(self.service.calls, [])

        response = await self.client.post(
            "/api/telegram/auth/phone",
            headers=headers,
            json={"phone": "+79991234567", "replace": False},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload, {"state": "code_required", "flow_id": "flow-1"})
        self.assertEqual(
            self.service.calls,
            [("phone", 111, "+79991234567", False)],
        )

    async def test_rate_limit_has_safe_message_and_retry_header(self) -> None:
        self.service.error = ParserLoginError(
            "telegram_rate_limited",
            "Нужно подождать.",
            http_status=429,
            retry_after=91,
        )
        response = await self.client.get(
            "/api/telegram/auth/status",
            headers={"Authorization": signed_header()},
        )
        self.assertEqual(response.status, 429)
        payload = await response.json()
        self.assertEqual(payload["state"], "locked")
        self.assertEqual(payload["retry_after"], 91)
        self.assertEqual(response.headers["Retry-After"], "91")

    async def test_static_app_is_served_without_session_material(self) -> None:
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        html = await response.text()
        self.assertIn("Telegram Reader", html)
        self.assertNotIn("SESSION_STRING", html)
        self.assertNotIn("./app.js", html)
        self.assertNotIn("./app.css", html)
        self.assertNotIn("__CSP_NONCE__", html)
        nonce_match = re.search(r'<script nonce="([A-Za-z0-9_-]+)">', html)
        style_nonce_match = re.search(r'<style nonce="([A-Za-z0-9_-]+)">', html)
        self.assertIsNotNone(nonce_match)
        self.assertIsNotNone(style_nonce_match)
        assert nonce_match is not None
        assert style_nonce_match is not None
        self.assertEqual(style_nonce_match.group(1), nonce_match.group(1))
        csp = response.headers["Content-Security-Policy"]
        self.assertIn(
            f"'nonce-{nonce_match.group(1)}'",
            csp,
        )
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertEqual(response.headers["Referrer-Policy"], "no-referrer")

        javascript = await self.client.get("/app.js")
        self.assertEqual(javascript.status, 404)
        stylesheet = await self.client.get("/app.css")
        self.assertEqual(stylesheet.status, 404)

        second_response = await self.client.get("/")
        second_html = await second_response.text()
        second_nonce_match = re.search(
            r'<script nonce="([A-Za-z0-9_-]+)">',
            second_html,
        )
        self.assertIsNotNone(second_nonce_match)
        assert second_nonce_match is not None
        self.assertNotEqual(second_nonce_match.group(1), nonce_match.group(1))

    async def test_diagnostic_log_never_includes_query_string(self) -> None:
        captured = io.StringIO()
        with redirect_stdout(captured):
            response = await self.client.get(
                "/health?phone=%2B79991234567&code=12345"
            )
            unmatched = await self.client.get("/private/67890?password=hidden")
        self.assertEqual(response.status, 200)
        self.assertEqual(unmatched.status, 404)
        diagnostic_log = captured.getvalue()
        self.assertIn("[http] GET /health -> 200", diagnostic_log)
        self.assertIn("[http] GET <unmatched> -> 404", diagnostic_log)
        self.assertNotIn("79991234567", diagnostic_log)
        self.assertNotIn("12345", diagnostic_log)
        self.assertNotIn("67890", diagnostic_log)
        self.assertNotIn("hidden", diagnostic_log)

    async def test_runtime_selfcheck_reaches_local_health_endpoint(self) -> None:
        port = unused_port()
        service = FakeLoginService()
        app = create_app(
            settings(Path(self.temp_dir.name) / "selfcheck.enc", port=port),
            login_service=service,  # type: ignore[arg-type]
        )
        runner = web.AppRunner(app)
        captured = io.StringIO()
        with (
            patch(
                "server.web_app.SELF_CHECK_INITIAL_DELAY_SECONDS",
                0.05,
            ),
            redirect_stdout(captured),
        ):
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", port)
            await site.start()
            await asyncio.sleep(0.2)
            await runner.cleanup()

        diagnostic_log = captured.getvalue()
        self.assertIn(
            f"[selfcheck] GET /health -> 200; local_port={port}",
            diagnostic_log,
        )
        self.assertTrue(service.closed)

    async def test_failed_diagnostics_task_does_not_skip_login_cleanup(self) -> None:
        service = FakeLoginService()
        app = web.Application()
        app[LOGIN_SERVICE_KEY] = service  # type: ignore[assignment]

        async def fail_diagnostics() -> None:
            raise RuntimeError("diagnostic failure detail")

        task = asyncio.create_task(fail_diagnostics())
        await asyncio.sleep(0)
        app[DIAGNOSTICS_TASK_KEY] = task
        captured = io.StringIO()
        with redirect_stdout(captured):
            await _cleanup_login_service(app)

        self.assertTrue(service.closed)
        output = captured.getvalue()
        self.assertIn(
            "[lifecycle] diagnostics task failed; error=RuntimeError",
            output,
        )
        self.assertNotIn("diagnostic failure detail", output)


if __name__ == "__main__":
    unittest.main()
