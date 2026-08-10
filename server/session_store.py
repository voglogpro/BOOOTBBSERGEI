"""Encrypted, file-backed storage for a Telethon ``StringSession``.

The encryption key is intentionally never generated or persisted by this
module.  It must be supplied explicitly or read from a secret environment
variable by :meth:`EncryptedSessionStore.from_env`.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

from cryptography.fernet import Fernet, InvalidToken
from telethon.sessions import StringSession


DEFAULT_DATA_DIR = Path("/app/data")
DEFAULT_RELATIVE_PATH = Path("telegram") / "reader.session.enc"
DEFAULT_KEY_ENV = "SESSION_ENCRYPTION_KEY"
PATH_ENV = "ENCRYPTED_SESSION_PATH"
LEGACY_PATH_ENV = "TELETHON_SESSION_STORE_PATH"
DATA_DIR_ENV = "DATA_DIR"

_FORMAT_NAME = "bibibike.telethon-string-session"
_FORMAT_VERSION = 1
_PLAINTEXT_MAGIC = b"bibibike-telethon-session-v1\x00"
_MAX_FILE_SIZE = 64 * 1024


class SessionStoreError(RuntimeError):
    """Base exception for encrypted session storage failures."""


class SessionKeyError(SessionStoreError):
    """Raised when the encryption key is missing or invalid."""


class SessionNotFoundError(SessionStoreError):
    """Raised when no encrypted session has been saved yet."""


class SessionIntegrityError(SessionStoreError):
    """Raised when encrypted data cannot be authenticated."""


class SessionFormatError(SessionStoreError):
    """Raised when a file or Telethon session has an invalid format."""


class SessionPermissionError(SessionStoreError):
    """Raised for unsafe file types such as symbolic links."""


@dataclass(frozen=True, slots=True)
class SessionMetadata:
    """Non-sensitive state safe to expose to an authenticated admin UI."""

    exists: bool
    encrypted_size: int = 0
    updated_at: datetime | None = None


class EncryptedSessionStore:
    """Persist a Telethon session using Fernet authenticated encryption."""

    def __init__(
        self,
        *,
        encryption_key: str | bytes,
        path: str | os.PathLike[str] = DEFAULT_DATA_DIR / DEFAULT_RELATIVE_PATH,
    ) -> None:
        self._path = Path(path)
        self._fernet = self._build_fernet(encryption_key)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        key_env: str = DEFAULT_KEY_ENV,
    ) -> "EncryptedSessionStore":
        """Build a store from BotHost-style environment variables.

        ``SESSION_ENCRYPTION_KEY`` is required.  The file defaults to
        ``/app/data/telegram/reader.session.enc`` and can be changed using
        either ``DATA_DIR`` or ``ENCRYPTED_SESSION_PATH``. The old
        ``TELETHON_SESSION_STORE_PATH`` name remains a compatibility fallback.
        """

        values = os.environ if environ is None else environ
        key = values.get(key_env, "").strip()
        if not key:
            raise SessionKeyError(f"Required secret environment variable {key_env} is missing.")

        configured_path = (
            values.get(PATH_ENV, "").strip()
            or values.get(LEGACY_PATH_ENV, "").strip()
        )
        if configured_path:
            path = Path(configured_path)
        else:
            data_dir = Path(values.get(DATA_DIR_ENV, str(DEFAULT_DATA_DIR)).strip())
            path = data_dir / DEFAULT_RELATIVE_PATH
        return cls(encryption_key=key, path=path)

    @property
    def path(self) -> Path:
        """Return the encrypted file path; never session contents or keys."""

        return self._path

    def save(self, session: str | StringSession) -> SessionMetadata:
        """Encrypt and atomically persist a valid authorized StringSession.

        The returned metadata contains neither the session nor account data.
        """

        serialized = session.save() if isinstance(session, StringSession) else session
        if not isinstance(serialized, str) or not serialized:
            raise SessionFormatError("A non-empty Telethon StringSession is required.")

        try:
            # Validate before replacing a previously working encrypted session.
            StringSession(serialized)
        except (TypeError, ValueError) as exc:
            raise SessionFormatError("The Telethon StringSession is invalid.") from exc

        plaintext = bytearray(_PLAINTEXT_MAGIC)
        plaintext.extend(serialized.encode("ascii"))
        try:
            token = self._fernet.encrypt(bytes(plaintext))
        except (TypeError, ValueError) as exc:
            raise SessionFormatError("The Telethon StringSession is invalid.") from exc
        finally:
            plaintext[:] = b"\x00" * len(plaintext)

        envelope = json.dumps(
            {
                "format": _FORMAT_NAME,
                "version": _FORMAT_VERSION,
                "ciphertext": token.decode("ascii"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        self._atomic_write(envelope)
        return self.metadata()

    def load(self) -> StringSession:
        """Authenticate, decrypt, and return a Telethon session object.

        Raw session text is deliberately not part of this API.
        """

        envelope = self._read_envelope()
        try:
            token = envelope["ciphertext"].encode("ascii")
            decrypted = self._fernet.decrypt(token)
        except (InvalidToken, KeyError, TypeError, ValueError, UnicodeError) as exc:
            # Wrong keys and tampering intentionally have the same public error.
            raise SessionIntegrityError("Encrypted session authentication failed.") from exc

        if not decrypted.startswith(_PLAINTEXT_MAGIC):
            raise SessionIntegrityError("Encrypted session authentication failed.")

        serialized_bytes = bytearray(decrypted[len(_PLAINTEXT_MAGIC) :])
        try:
            serialized = serialized_bytes.decode("ascii")
            return StringSession(serialized)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise SessionIntegrityError("Encrypted session authentication failed.") from exc
        finally:
            serialized_bytes[:] = b"\x00" * len(serialized_bytes)

    def metadata(self) -> SessionMetadata:
        """Return only non-sensitive filesystem metadata."""

        file_stat = self._safe_stat(missing_ok=True)
        if file_stat is None:
            return SessionMetadata(exists=False)
        return SessionMetadata(
            exists=True,
            encrypted_size=file_stat.st_size,
            updated_at=datetime.fromtimestamp(file_stat.st_mtime, timezone.utc),
        )

    def delete(self) -> bool:
        """Delete the exact encrypted file; intended for server-side recovery only."""

        if self._safe_stat(missing_ok=True) is None:
            return False
        self._path.unlink()
        self._fsync_directory(self._path.parent)
        return True

    @staticmethod
    def _build_fernet(encryption_key: str | bytes) -> Fernet:
        try:
            if isinstance(encryption_key, str):
                key_bytes = encryption_key.strip().encode("ascii", errors="strict")
            elif isinstance(encryption_key, bytes):
                key_bytes = encryption_key.strip()
            else:
                raise TypeError
            return Fernet(key_bytes)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise SessionKeyError("The session encryption key is invalid.") from exc

    def _read_envelope(self) -> dict[str, object]:
        file_stat = self._safe_stat(missing_ok=False)
        assert file_stat is not None
        if file_stat.st_size > _MAX_FILE_SIZE:
            raise SessionFormatError("Encrypted session file is invalid.")

        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self._path, flags)
            with os.fdopen(descriptor, "rb") as encrypted_file:
                payload = encrypted_file.read(_MAX_FILE_SIZE + 1)
        except FileNotFoundError as exc:
            raise SessionNotFoundError("No encrypted Telegram session is stored.") from exc
        except OSError as exc:
            raise SessionPermissionError("Encrypted session file cannot be read safely.") from exc

        if len(payload) > _MAX_FILE_SIZE:
            raise SessionFormatError("Encrypted session file is invalid.")
        try:
            envelope = json.loads(payload.decode("ascii"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SessionFormatError("Encrypted session file is invalid.") from exc
        if not isinstance(envelope, dict):
            raise SessionFormatError("Encrypted session file is invalid.")
        if envelope.get("format") != _FORMAT_NAME or envelope.get("version") != _FORMAT_VERSION:
            raise SessionFormatError("Encrypted session file is invalid.")
        if set(envelope) != {"format", "version", "ciphertext"}:
            raise SessionFormatError("Encrypted session file is invalid.")
        if not isinstance(envelope.get("ciphertext"), str):
            raise SessionFormatError("Encrypted session file is invalid.")
        return envelope

    def _atomic_write(self, payload: bytes) -> None:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._safe_stat(missing_ok=True)

        descriptor = -1
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self._path.name}.",
                suffix=".tmp",
                dir=parent,
            )
            self._best_effort_chmod(Path(temporary_name), 0o600)
            with os.fdopen(descriptor, "wb") as temporary_file:
                descriptor = -1
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_name, self._path)
            temporary_name = None
            self._best_effort_chmod(self._path, 0o600)
            self._fsync_directory(parent)
        except OSError as exc:
            raise SessionStoreError("Encrypted session could not be stored atomically.") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    Path(temporary_name).unlink(missing_ok=True)
                except OSError:
                    pass

    def _safe_stat(self, *, missing_ok: bool) -> os.stat_result | None:
        try:
            file_stat = self._path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise SessionNotFoundError("No encrypted Telegram session is stored.") from None
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise SessionPermissionError("Encrypted session path must be a regular file.")
        return file_stat

    @staticmethod
    def _best_effort_chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            # Windows and some mounted BotHost volumes may not expose POSIX modes.
            pass

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(directory, flags)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            # Directory fsync is unavailable on some filesystems.
            pass
