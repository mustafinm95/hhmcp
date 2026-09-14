from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import httpx
from filelock import FileLock

from .applications import ApplicationService
from .auth import AuthManager, EnvironmentAuth
from .client import HHClient
from .config import Settings, default_home, environment_access, load_settings
from .secrets import (
    AESGCMProtector,
    CredentialBackend,
    EncryptedFileSecretStore,
    KeyringCredentialBackend,
    MasterKeyProvider,
    derive_environment_draft_key,
    master_key_account,
)
from .security import ensure_private_directory
from .service import HHService
from .state import StateStore


class _EnvironmentApplications:
    def __init__(self, runtime: "Runtime", token: str, auth: EnvironmentAuth) -> None:
        self._runtime = runtime
        self._token = token
        self._auth = auth
        self._lock = asyncio.Lock()
        self._delegate: ApplicationService | None = None

    async def ready(self) -> ApplicationService:
        identity = await self._auth.ensure_identity()
        if self._delegate is not None:
            return self._delegate
        async with self._lock:
            if self._delegate is None:
                self._runtime._secure_directory(self._runtime.settings.home)
                account_id = str(identity.account_id)
                protector = AESGCMProtector(derive_environment_draft_key(self._token, account_id))
                state = StateStore(self._runtime.settings.environment_state_db, protector)
                self._runtime.state = state
                self._delegate = ApplicationService(self._runtime.client, self._auth, state)
        return self._delegate

    async def prepare(self, vacancy_id: str, resume_id: str, message: str) -> dict[str, object]:
        return await (await self.ready()).prepare(vacancy_id, resume_id, message)

    async def submit_from_cli(self, draft_id: str) -> dict[str, object]:
        return await (await self.ready()).submit_from_cli(draft_id)

    async def status(self, draft_id: str) -> dict[str, object]:
        return await (await self.ready()).status(draft_id)


class Runtime:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        credential_backend: CredentialBackend | None = None,
        secure_directory: Callable[[Path], None] = ensure_private_directory,
        environ: Mapping[str, str] | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        access = environment_access(environ)
        self._secure_directory = secure_directory
        self.state: StateStore | None = None
        if access is not None:
            base_settings = settings or Settings(home=default_home(environ=environ))
            self.settings = replace(base_settings, user_agent=access.user_agent)
            self.settings.validate()
            self.mode = "environment_access_token"
            self.credential_backend_name = "not_used"
            self.credential_account = None
            self.secrets = None
            self.client = HHClient(
                self.settings, http_transport, environment_access=True
            )
            env_auth = EnvironmentAuth(access.token, self.client.me)
            self.auth = env_auth
            env_applications = _EnvironmentApplications(self, access.token, env_auth)
            self.applications = env_applications
            self.service = HHService(self.client, self.auth, self.applications)
            return

        self.settings = settings or load_settings()
        self.settings.validate()
        self.mode = "configured_oauth"
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
        self.client = HHClient(self.settings, http_transport)
        self.applications = ApplicationService(self.client, self.auth, self.state)
        self.service = HHService(self.client, self.auth, self.applications)

    async def ready_applications(self) -> ApplicationService:
        if isinstance(self.applications, _EnvironmentApplications):
            return await self.applications.ready()
        return self.applications

    async def close(self) -> None:
        await self.client.aclose()
