from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from journal.auth import (
    AuthorizationError,
    create_web_session,
    validate_init_data,
    validate_login_data,
    validate_web_session,
)


def signed_init_data(token: str, user_id: int, auth_date: int) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAE-test",
        "user": json.dumps(
            {"id": user_id, "first_name": "Анна", "username": "trader"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def signed_login_data(token: str, user_id: int, auth_date: int) -> dict[str, str]:
    fields = {
        "id": str(user_id),
        "first_name": "Анна",
        "username": "trader",
        "auth_date": str(auth_date),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hashlib.sha256(token.encode()).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return fields


class AuthTests(unittest.TestCase):
    def test_valid_signature_returns_telegram_identity(self) -> None:
        raw = signed_init_data("123:token", 42, 1_000)
        principal = validate_init_data(
            raw,
            bot_token="123:token",
            allowed_user_ids={42},
            max_age_seconds=300,
            now=1_100,
        )
        self.assertEqual(principal.telegram_id, 42)
        self.assertEqual(principal.first_name, "Анна")

    def test_modified_data_is_rejected(self) -> None:
        raw = signed_init_data("123:token", 42, 1_000).replace("trader", "intruder")
        with self.assertRaises(AuthorizationError):
            validate_init_data(raw, bot_token="123:token", now=1_100)

    def test_allowlist_is_enforced_when_configured(self) -> None:
        raw = signed_init_data("123:token", 42, 1_000)
        with self.assertRaises(AuthorizationError):
            validate_init_data(
                raw, bot_token="123:token", allowed_user_ids={7}, now=1_100
            )

    def test_expired_data_is_rejected(self) -> None:
        raw = signed_init_data("123:token", 42, 1_000)
        with self.assertRaises(AuthorizationError):
            validate_init_data(
                raw, bot_token="123:token", max_age_seconds=60, now=1_100
            )

    def test_website_login_and_session_preserve_telegram_identity(self) -> None:
        principal = validate_login_data(
            signed_login_data("123:token", 42, 1_000),
            bot_token="123:token",
            allowed_user_ids={42},
            now=1_100,
        )
        session = create_web_session(
            principal, bot_token="123:token", lifetime_seconds=600, now=1_100
        )
        restored = validate_web_session(
            session, bot_token="123:token", allowed_user_ids={42}, now=1_200
        )
        self.assertEqual(restored.telegram_id, 42)
        self.assertEqual(restored.username, "trader")

    def test_tampered_or_expired_website_session_is_rejected(self) -> None:
        principal = validate_login_data(
            signed_login_data("123:token", 42, 1_000),
            bot_token="123:token",
            now=1_100,
        )
        session = create_web_session(
            principal, bot_token="123:token", lifetime_seconds=60, now=1_100
        )
        with self.assertRaises(AuthorizationError):
            validate_web_session(session + "x", bot_token="123:token", now=1_120)
        with self.assertRaises(AuthorizationError):
            validate_web_session(session, bot_token="123:token", now=1_161)


if __name__ == "__main__":
    unittest.main()
