from __future__ import annotations

import hashlib
import hmac
import json
import unittest
from urllib.parse import urlencode

from server.miniapp_auth import (
    MiniAppAuthorizationError,
    validate_miniapp_init_data,
)


BOT_TOKEN = "123456:unit-test-secret"
NOW = 1_800_000_000


def signed_init_data(*, user_id: int = 777, auth_date: int = NOW) -> str:
    fields = {
        "auth_date": str(auth_date),
        "query_id": "AAExample",
        "user": json.dumps(
            {"id": user_id, "first_name": "Кирилл"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(
        b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret, check.encode(), hashlib.sha256
    ).hexdigest()
    return urlencode(fields)


class MiniAppAuthTests(unittest.TestCase):
    def test_valid_allowlisted_user_is_accepted(self) -> None:
        principal = validate_miniapp_init_data(
            signed_init_data(),
            bot_token=BOT_TOKEN,
            allowed_user_ids={777},
            now=NOW,
        )
        self.assertEqual(principal.user_id, 777)

    def test_tampering_is_rejected(self) -> None:
        raw = signed_init_data().replace("AAExample", "Changed")
        with self.assertRaises(MiniAppAuthorizationError):
            validate_miniapp_init_data(
                raw,
                bot_token=BOT_TOKEN,
                allowed_user_ids={777},
                now=NOW,
            )

    def test_expired_data_is_rejected(self) -> None:
        with self.assertRaisesRegex(MiniAppAuthorizationError, "expired"):
            validate_miniapp_init_data(
                signed_init_data(auth_date=NOW - 301),
                bot_token=BOT_TOKEN,
                allowed_user_ids={777},
                now=NOW,
            )

    def test_non_admin_is_rejected(self) -> None:
        with self.assertRaisesRegex(MiniAppAuthorizationError, "administrator"):
            validate_miniapp_init_data(
                signed_init_data(user_id=888),
                bot_token=BOT_TOKEN,
                allowed_user_ids={777},
                now=NOW,
            )

    def test_duplicate_field_is_rejected(self) -> None:
        with self.assertRaises(MiniAppAuthorizationError):
            validate_miniapp_init_data(
                signed_init_data() + "&auth_date=1",
                bot_token=BOT_TOKEN,
                allowed_user_ids={777},
                now=NOW,
            )


if __name__ == "__main__":
    unittest.main()
