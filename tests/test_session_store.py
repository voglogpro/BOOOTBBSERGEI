from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from server.session_store import (
    EncryptedSessionStore,
    SessionIntegrityError,
    SessionKeyError,
    SessionStoreError,
)


def make_valid_session() -> str:
    session = StringSession()
    session.set_dc(2, "149.154.167.51", 443)
    session.auth_key = AuthKey(bytes(range(256)))
    return session.save()


class EncryptedSessionStoreTests(unittest.TestCase):
    def test_round_trip_returns_session_object_and_safe_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            serialized = make_valid_session()
            store = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)

            saved = store.save(serialized)
            restored = store.load()

            self.assertIsInstance(restored, StringSession)
            self.assertEqual(restored.save(), serialized)
            self.assertTrue(saved.exists)
            self.assertGreater(saved.encrypted_size, 0)
            self.assertNotIn(serialized, path.read_text(encoding="ascii"))
            self.assertEqual(set(json.loads(path.read_text(encoding="ascii"))), {
                "ciphertext",
                "format",
                "version",
            })

    def test_tampered_ciphertext_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            store = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)
            store.save(make_valid_session())
            envelope = json.loads(path.read_text(encoding="ascii"))
            token = envelope["ciphertext"]
            envelope["ciphertext"] = token[:-2] + ("AA" if token[-2:] != "AA" else "BB")
            path.write_text(json.dumps(envelope), encoding="ascii")

            with self.assertRaisesRegex(SessionIntegrityError, "authentication failed"):
                store.load()

    def test_wrong_key_is_rejected_without_secret_in_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            first = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)
            first.save(make_valid_session())
            second = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)

            with self.assertRaisesRegex(SessionIntegrityError, "authentication failed"):
                second.load()

    def test_failed_replace_preserves_old_file_and_removes_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            store = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)
            store.save(make_valid_session())
            original = path.read_bytes()

            with patch("server.session_store.os.replace", side_effect=OSError("simulated")):
                with self.assertRaisesRegex(SessionStoreError, "atomically"):
                    store.save(make_valid_session())

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    @unittest.skipIf(os.name == "nt", "Windows does not expose complete POSIX mode bits")
    def test_saved_file_is_owner_only_on_posix(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            store = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)
            store.save(make_valid_session())

            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_from_env_requires_key_and_uses_configured_data_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(SessionKeyError):
                EncryptedSessionStore.from_env({"DATA_DIR": temp_dir})

            store = EncryptedSessionStore.from_env(
                {
                    "SESSION_ENCRYPTION_KEY": Fernet.generate_key().decode("ascii"),
                    "DATA_DIR": temp_dir,
                }
            )
            self.assertEqual(store.path, Path(temp_dir) / "telegram" / "reader.session.enc")

    def test_delete_is_server_side_and_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "telegram.session.enc"
            store = EncryptedSessionStore(encryption_key=Fernet.generate_key(), path=path)
            store.save(make_valid_session())

            self.assertTrue(store.delete())
            self.assertFalse(store.delete())
            self.assertFalse(store.metadata().exists)


if __name__ == "__main__":
    unittest.main()
