from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import secrets
import sys
from pathlib import Path
from typing import Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .errors import ConfigurationError


MASTER_KEY_SERVICE = "hh-mcp/master-key/v1"
MASTER_KEY_ACCOUNT = "local-installation"
_ENVELOPE_PREFIX = b"HHM1"
_NONCE_BYTES = 12


class SecretStore(Protocol):
    def read(self, name: str) -> dict[str, object] | None: ...
    def write(self, name: str, value: dict[str, object]) -> None: ...
    def delete(self, name: str) -> None: ...


class Protector(Protocol):
    def protect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes: ...
    def unprotect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes: ...


class CredentialBackend(Protocol):
    priority: object

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def master_key_account(home: Path) -> str:
    """Return a stable, non-secret credential account for one data directory."""
    canonical = os.path.normcase(os.path.abspath(os.fspath(home)))
    digest = hashlib.sha256(os.fsencode(canonical)).hexdigest()[:32]
    return f"installation-{digest}"


def derive_environment_draft_key(token: str, account_id: str) -> bytes:
    """Derive a state-only key; changing the access token intentionally changes the key."""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=b"hh-mcp/environment-draft/salt/v1",
        info=b"hh-mcp/environment-draft/key/v1\x00" + account_id.encode("utf-8"),
    ).derive(token.encode("utf-8"))


def validate_credential_backend(backend: CredentialBackend, *, platform_name: str | None = None) -> str:
    """Accept only an explicitly known OS credential backend."""
    platform_name = platform_name or sys.platform
    backend_type = type(backend)
    identity = f"{backend_type.__module__}.{backend_type.__qualname__}"
    lowered = identity.lower()
    try:
        priority = float(backend.priority)
    except Exception as exc:
        raise ConfigurationError("The selected credential backend has no usable priority") from exc
    if not math.isfinite(priority) or priority < 1 or any(
        word in lowered for word in ("null", "fail", "plaintext", "chainer")
    ):
        raise ConfigurationError("A secure system credential backend is required")

    if platform_name == "win32":
        allowed = ("keyring.backends.Windows.",)
    elif platform_name == "darwin":
        allowed = ("keyring.backends.macOS.",)
    elif platform_name.startswith("linux"):
        allowed = ("keyring.backends.SecretService.", "keyring.backends.kwallet.")
    else:
        raise ConfigurationError(f"Unsupported platform for system credential storage: {platform_name}")
    if not identity.startswith(allowed):
        raise ConfigurationError(
            "The selected keyring backend is not an approved system credential backend for this platform"
        )
    return identity


class KeyringCredentialBackend:
    """Fail-closed adapter over the backend selected by the keyring package."""

    def __init__(self, backend: CredentialBackend, *, platform_name: str | None = None) -> None:
        self._backend = backend
        self.identity = validate_credential_backend(backend, platform_name=platform_name)

    @classmethod
    def from_system(cls, *, platform_name: str | None = None) -> "KeyringCredentialBackend":
        try:
            import keyring

            backend = keyring.get_keyring()
        except Exception as exc:
            raise ConfigurationError("Could not initialize the system credential backend") from exc
        return cls(backend, platform_name=platform_name)

    def get_password(self, service: str, username: str) -> str | None:
        try:
            return self._backend.get_password(service, username)
        except Exception as exc:
            raise ConfigurationError("Could not read the encryption key from system credentials") from exc

    def set_password(self, service: str, username: str, password: str) -> None:
        try:
            self._backend.set_password(service, username, password)
        except Exception as exc:
            raise ConfigurationError("Could not save the encryption key in system credentials") from exc

    def delete_password(self, service: str, username: str) -> None:
        try:
            self._backend.delete_password(service, username)
        except Exception as exc:
            raise ConfigurationError("Could not delete the encryption key from system credentials") from exc


class MasterKeyProvider:
    def __init__(self, backend: CredentialBackend, *, account: str = MASTER_KEY_ACCOUNT) -> None:
        self._backend = backend
        self._account = account

    def get_or_create(self) -> bytes:
        encoded = self._backend.get_password(MASTER_KEY_SERVICE, self._account)
        if encoded is None:
            candidate = secrets.token_bytes(32)
            self._backend.set_password(
                MASTER_KEY_SERVICE,
                self._account,
                base64.urlsafe_b64encode(candidate).decode("ascii"),
            )
            encoded = self._backend.get_password(MASTER_KEY_SERVICE, self._account)
            if encoded is None:
                raise ConfigurationError("System credentials did not retain the encryption key")
        try:
            if not isinstance(encoded, str):
                raise TypeError("credential value is not text")
            key = base64.b64decode(encoded.encode("ascii"), altchars=b"-_", validate=True)
        except (TypeError, UnicodeEncodeError, ValueError) as exc:
            raise ConfigurationError("The stored HH MCP encryption key is invalid") from exc
        if len(key) != 32:
            raise ConfigurationError("The stored HH MCP encryption key has an invalid length")
        return key


class AESGCMProtector:
    """Versioned AES-256-GCM envelope using a key held by the OS credential service."""

    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ConfigurationError("AES-256-GCM requires a 32-byte encryption key")
        self._cipher = AESGCM(key)

    @staticmethod
    def _aad(entropy: bytes) -> bytes:
        return b"hh-mcp/aes-gcm/v1\x00" + entropy

    def protect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        return _ENVELOPE_PREFIX + nonce + self._cipher.encrypt(nonce, data, self._aad(entropy))

    def unprotect(self, data: bytes, *, entropy: bytes = b"hh-mcp-v1") -> bytes:
        if not data.startswith(_ENVELOPE_PREFIX) or len(data) < len(_ENVELOPE_PREFIX) + _NONCE_BYTES + 16:
            raise ConfigurationError("Encrypted HH MCP data has an unsupported or invalid format")
        nonce_start = len(_ENVELOPE_PREFIX)
        nonce = data[nonce_start : nonce_start + _NONCE_BYTES]
        ciphertext = data[nonce_start + _NONCE_BYTES :]
        try:
            return self._cipher.decrypt(nonce, ciphertext, self._aad(entropy))
        except InvalidTag as exc:
            raise ConfigurationError("Encrypted HH MCP data could not be authenticated") from exc


class EncryptedFileSecretStore:
    def __init__(self, directory: Path, protector: Protector) -> None:
        self._directory = directory
        self._protector = protector

    def _path(self, name: str) -> Path:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError("invalid secret name")
        return self._directory / f"{name}.bin"

    def read(self, name: str) -> dict[str, object] | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            raw = self._protector.unprotect(path.read_bytes(), entropy=name.encode("ascii"))
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"Secret record {name} is invalid") from exc
        if not isinstance(value, dict):
            raise ConfigurationError(f"Secret record {name} is invalid")
        return value

    def write(self, name: str, value: dict[str, object]) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path(name)
        temporary = path.with_suffix(".tmp")
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        try:
            temporary.write_bytes(self._protector.protect(raw, entropy=name.encode("ascii")))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def delete(self, name: str) -> None:
        self._path(name).unlink(missing_ok=True)


class MemorySecretStore:
    """Test-only secret store."""

    def __init__(self) -> None:
        self.values: dict[str, dict[str, object]] = {}

    def read(self, name: str) -> dict[str, object] | None:
        value = self.values.get(name)
        return dict(value) if value is not None else None

    def write(self, name: str, value: dict[str, object]) -> None:
        self.values[name] = dict(value)

    def delete(self, name: str) -> None:
        self.values.pop(name, None)
