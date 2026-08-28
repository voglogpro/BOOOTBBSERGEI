from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from journal.auth import AuthorizationError, validate_init_data


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


if __name__ == "__main__":
    unittest.main()

