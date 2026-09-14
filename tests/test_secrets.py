from __future__ import annotations

import base64

import pytest

from hh_mcp.errors import ConfigurationError
from hh_mcp.secrets import (
    AESGCMProtector,
    EncryptedFileSecretStore,
    KeyringCredentialBackend,
    MasterKeyProvider,
    derive_environment_draft_key,
    master_key_account,
    validate_credential_backend,
)


class FakeCredentialBackend:
    priority = 10

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.fail_get = False
        self.fail_set = False

    def get_password(self, service: str, username: str) -> str | None:
        if self.fail_get:
            raise RuntimeError("backend marker secret")
        return self.values.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        if self.fail_set:
            raise RuntimeError("backend marker secret")
        self.values[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.values.pop((service, username), None)


def _backend_type(module: str, *, priority: object = 10) -> object:
    backend_type = type(
        "TestKeyring",
        (),
        {
            "__module__": module,
            "priority": priority,
            "get_password": lambda self, service, username: None,
            "set_password": lambda self, service, username, password: None,
            "delete_password": lambda self, service, username: None,
        },
    )
    return backend_type()


@pytest.mark.parametrize(
    ("platform_name", "module"),
    [
        ("win32", "keyring.backends.Windows"),
        ("darwin", "keyring.backends.macOS"),
        ("linux", "keyring.backends.SecretService"),
        ("linux", "keyring.backends.kwallet"),
    ],
)
def test_secure_system_backends_are_accepted(platform_name: str, module: str) -> None:
    identity = validate_credential_backend(_backend_type(module), platform_name=platform_name)
    assert identity.startswith(module)


@pytest.mark.parametrize(
    ("platform_name", "module", "priority"),
    [
        ("win32", "keyring.backends.null", 10),
        ("win32", "keyring.backends.fail", 10),
        ("linux", "keyrings.alt.file", 10),
        ("linux", "keyring.backends.chainer", 10),
        ("darwin", "vendor.unknown", 10),
        ("win32", "keyring.backends.Windows", 0),
        ("darwin", "keyring.backends.macOS", float("nan")),
    ],
)
def test_unsafe_or_unknown_backends_are_rejected(
    platform_name: str, module: str, priority: object
) -> None:
    with pytest.raises(ConfigurationError):
        validate_credential_backend(_backend_type(module, priority=priority), platform_name=platform_name)


def test_keyring_runtime_errors_fail_closed_without_leaking_backend_text() -> None:
    raw = _backend_type("keyring.backends.Windows")
    raw.get_password = lambda service, username: (_ for _ in ()).throw(RuntimeError("marker-secret"))
    backend = KeyringCredentialBackend(raw, platform_name="win32")
    with pytest.raises(ConfigurationError) as caught:
        backend.get_password("service", "account")
    assert "marker-secret" not in str(caught.value)


def test_backend_priority_error_fails_closed() -> None:
    backend_type = type(
        "BrokenKeyring",
        (),
        {"__module__": "keyring.backends.Windows", "priority": property(lambda self: 1 / 0)},
    )
    with pytest.raises(ConfigurationError):
        validate_credential_backend(backend_type(), platform_name="win32")


def test_master_key_account_is_stable_and_path_bound(workspace_tmp) -> None:
    assert master_key_account(workspace_tmp) == master_key_account(workspace_tmp)
    assert master_key_account(workspace_tmp) != master_key_account(workspace_tmp / "other")


def test_master_key_is_created_once_and_stored_as_base64() -> None:
    backend = FakeCredentialBackend()
    provider = MasterKeyProvider(backend)
    first = provider.get_or_create()
    second = provider.get_or_create()
    assert first == second
    assert len(first) == 32
    encoded = next(iter(backend.values.values()))
    assert base64.urlsafe_b64decode(encoded) == first


@pytest.mark.parametrize("failure", ["get", "set"])
def test_master_key_backend_failures_are_configuration_errors(failure: str) -> None:
    raw = FakeCredentialBackend()
    backend = KeyringCredentialBackend(_backend_type("keyring.backends.Windows"), platform_name="win32")
    backend._backend = raw
    setattr(raw, f"fail_{failure}", True)
    with pytest.raises(ConfigurationError):
        MasterKeyProvider(backend).get_or_create()


@pytest.mark.parametrize("encoded", ["not base64!", base64.urlsafe_b64encode(b"short").decode("ascii")])
def test_invalid_stored_master_key_is_rejected(encoded: str) -> None:
    backend = FakeCredentialBackend()
    backend.values[("hh-mcp/master-key/v1", "local-installation")] = encoded
    with pytest.raises(ConfigurationError):
        MasterKeyProvider(backend).get_or_create()


def test_aes_gcm_round_trip_entropy_and_tamper() -> None:
    marker = b"hh-mcp-secret-marker"
    protector = AESGCMProtector(b"k" * 32)
    encrypted = protector.protect(marker, entropy=b"test-a")
    assert marker not in encrypted
    assert protector.unprotect(encrypted, entropy=b"test-a") == marker
    with pytest.raises(ConfigurationError):
        protector.unprotect(encrypted, entropy=b"test-b")
    tampered = encrypted[:-1] + bytes([encrypted[-1] ^ 1])
    with pytest.raises(ConfigurationError):
        protector.unprotect(tampered, entropy=b"test-a")


def test_environment_draft_key_is_deterministic_and_token_bound() -> None:
    first = derive_environment_draft_key("token-a", "account")
    assert first == derive_environment_draft_key("token-a", "account")
    assert first != derive_environment_draft_key("token-b", "account")
    assert first != derive_environment_draft_key("token-a", "other-account")


def test_encrypted_file_store_never_writes_plaintext(workspace_tmp) -> None:
    store = EncryptedFileSecretStore(workspace_tmp, AESGCMProtector(b"s" * 32))
    store.write("oauth", {"token": "visible-marker"})
    on_disk = (workspace_tmp / "oauth.bin").read_bytes()
    assert b"visible-marker" not in on_disk
    assert store.read("oauth") == {"token": "visible-marker"}
