from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from filelock import FileLock

from .applications import ApplicationService
from .auth import AuthManager
from .client import HHClient
from .config import Settings, load_settings
from .secrets import (
    AESGCMProtector,
    CredentialBackend,
    EncryptedFileSecretStore,
    KeyringCredentialBackend,
    MasterKeyProvider,
    master_key_account,
)
from .security import ensure_private_directory
from .service import HHService
from .state import StateStore


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        credential_backend: CredentialBackend | None = None,
        secure_directory: Callable[[Path], None] = ensure_private_directory,
    ) -> None:
        self.settings = settings or load_settings()
        self.settings.validate()
        secure_directory(self.settings.home)
        secure_directory(self.settings.secrets_dir)
        if credential_backend is None:
            system_backend = KeyringCredentialBackend.from_system()
            credential_backend = system_backend
            self.credential_backend_name = system_backend.identity
        else:
            self.credential_backend_name = "injected"
        self.credential_account = master_key_account(self.settings.home)
        with FileLock(self.settings.auth_lock, timeout=30):
            key = MasterKeyProvider(credential_backend, account=self.credential_account).get_or_create()
        protector = AESGCMProtector(key)
        self.state = StateStore(self.settings.state_db, protector)
        self.secrets = EncryptedFileSecretStore(self.settings.secrets_dir, protector)
        self.auth = AuthManager(self.settings, self.state, self.secrets)
        self.client = HHClient(self.settings)
        self.applications = ApplicationService(self.client, self.auth, self.state)
        self.service = HHService(self.client, self.auth, self.applications)

    async def close(self) -> None:
        await self.client.aclose()
