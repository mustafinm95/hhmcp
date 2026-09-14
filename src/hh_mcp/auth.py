from __future__ import annotations

import base64
import hashlib
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from urllib.parse import urlencode

from filelock import AsyncFileLock, FileLock

from .config import Settings
from .errors import AuthenticationError, ConfigurationError, StateConflictError
from .secrets import SecretStore
from .state import AuthSnapshot, StateStore


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str
    token_type: str
    expires_at: datetime | None
    refresh_token: str | None

    @classmethod
    def from_response(cls, value: dict[str, object], now: datetime | None = None) -> "OAuthTokens":
        access = value.get("access_token")
        token_type = value.get("token_type", "bearer")
        if not isinstance(access, str) or not access:
            raise AuthenticationError("HH token response did not contain access_token")
        expires_in = value.get("expires_in")
        expires_at = None
        if isinstance(expires_in, (int, float)):
            expires_at = (now or datetime.now(timezone.utc)) + timedelta(seconds=float(expires_in))
        refresh = value.get("refresh_token")
        return cls(access, str(token_type), expires_at, refresh if isinstance(refresh, str) else None)

    def expired(self, now: datetime | None = None) -> bool:
        return self.expires_at is not None and (now or datetime.now(timezone.utc)) >= self.expires_at

    def as_secret(
        self, *, generation: int, credential_revision: int = 0, account_id: str | None = None
    ) -> dict[str, object]:
        return {
            "generation": generation,
            "credential_revision": credential_revision,
            "account_id": account_id,
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "refresh_token": self.refresh_token,
        }

    @classmethod
    def from_secret(cls, value: dict[str, object]) -> "OAuthTokens":
        raw_expiry = value.get("expires_at")
        return cls(
            access_token=str(value["access_token"]),
            token_type=str(value.get("token_type", "bearer")),
            expires_at=datetime.fromisoformat(str(raw_expiry)) if raw_expiry else None,
            refresh_token=str(value["refresh_token"]) if value.get("refresh_token") else None,
        )


RefreshCall = Callable[[str], Awaitable[dict[str, object]]]


@dataclass(frozen=True, slots=True)
class BoundUser:
    token: str
    identity: AuthSnapshot


class AuthManager:
    """Coordinates secrets and a durable auth generation under one process-shared lock."""

    def __init__(self, settings: Settings, state: StateStore, secrets_store: SecretStore) -> None:
        self.settings = settings
        self.state = state
        self.secrets = secrets_store
        self.lock_path = settings.auth_lock

    def _lock(self) -> FileLock:
        return FileLock(self.lock_path, timeout=30)

    def _async_lock(self) -> AsyncFileLock:
        return AsyncFileLock(self.lock_path, timeout=30)

    def configure_application(self, client_id: str, client_secret: str) -> None:
        if not client_id or not client_secret:
            raise ConfigurationError("client_id and client_secret are required")
        with self._lock():
            self.secrets.write(
                "application", {"client_id": client_id, "client_secret": client_secret}
            )
            self.secrets.delete("application_token")

    def application_credentials(self) -> tuple[str, str]:
        value = self.secrets.read("application")
        if not value:
            raise ConfigurationError("Application credentials are not configured")
        return str(value["client_id"]), str(value["client_secret"])

    def commit_login(self, account_id: str, tokens: OAuthTokens) -> int:
        if not account_id:
            raise AuthenticationError("HH /me response did not identify the account")
        with self._lock():
            next_generation = self.state.auth_snapshot().generation + 1
            self.secrets.write(
                "user_token",
                tokens.as_secret(generation=next_generation, credential_revision=0, account_id=account_id),
            )
            generation = self.state.replace_auth_state(account_id=account_id, logged_in=True)
            if generation != next_generation:
                self.secrets.delete("user_token")
                raise StateConflictError("Authentication generation changed unexpectedly")
        return generation

    def logout(self) -> int:
        with self._lock():
            generation = self.state.replace_auth_state(account_id=None, logged_in=False)
            self.secrets.delete("user_token")
        return generation

    def status(self) -> dict[str, object]:
        snapshot = self.state.auth_snapshot()
        record = self.secrets.read("user_token") if snapshot.logged_in else None
        consistent = bool(
            record
            and record.get("generation") == snapshot.generation
            and record.get("account_id") == snapshot.account_id
            and record.get("credential_revision", 0) == snapshot.credential_revision
            and not snapshot.refresh_inflight
        )
        return {
            "logged_in": snapshot.logged_in and consistent,
            "account_id": snapshot.account_id if consistent else None,
            "generation": snapshot.generation,
            "credential_revision": snapshot.credential_revision,
            "refresh_inflight": snapshot.refresh_inflight,
            "requires_login": snapshot.logged_in and not consistent,
            "application_configured": self.secrets.read("application") is not None,
        }

    def _current_identity_unlocked(self) -> tuple[AuthSnapshot, dict[str, object]]:
        snapshot = self.state.auth_snapshot()
        record = self.secrets.read("user_token")
        if (
            not snapshot.logged_in
            or snapshot.refresh_inflight
            or not record
            or record.get("generation") != snapshot.generation
            or record.get("credential_revision", 0) != snapshot.credential_revision
            or record.get("account_id") != snapshot.account_id
        ):
            raise AuthenticationError("User login is missing or inconsistent; run `hh-mcp auth login`")
        return snapshot, record

    def current_identity(self) -> AuthSnapshot:
        with self._lock():
            snapshot, _ = self._current_identity_unlocked()
            return snapshot

    async def user_access_token(self, refresh_call: RefreshCall) -> str:
        return (await self.user_session(refresh_call)).token

    async def user_session(self, refresh_call: RefreshCall) -> BoundUser:
        async with self._async_lock():
            return await self._user_session_unlocked(refresh_call)

    @asynccontextmanager
    async def bound_user(self, refresh_call: RefreshCall):
        """Hold the lifecycle lock when account identity must not change mid-operation."""
        async with self._async_lock():
            yield await self._user_session_unlocked(refresh_call)

    async def _user_session_unlocked(self, refresh_call: RefreshCall) -> BoundUser:
        snapshot, record = self._current_identity_unlocked()
        tokens = OAuthTokens.from_secret(record)
        if not tokens.expired():
            return BoundUser(tokens.access_token, snapshot)
        token = await self._refresh_locked(snapshot, tokens, refresh_call)
        refreshed, _ = self._current_identity_unlocked()
        return BoundUser(token, refreshed)

    async def _refresh_locked(
        self, snapshot: AuthSnapshot, tokens: OAuthTokens, refresh_call: RefreshCall
    ) -> str:
        if not tokens.refresh_token:
            raise AuthenticationError("Access token expired and no refresh token is available")
        self.state.set_refresh_inflight(snapshot.generation, snapshot.credential_revision)
        try:
            response = await refresh_call(str(tokens.refresh_token))
            refreshed = OAuthTokens.from_response(response)
            final_revision = snapshot.credential_revision + 1
            self.secrets.write(
                "user_token",
                refreshed.as_secret(
                    generation=snapshot.generation,
                    credential_revision=final_revision,
                    account_id=str(snapshot.account_id),
                ),
            )
            committed = self.state.finish_refresh(snapshot.generation, snapshot.credential_revision)
            if committed != final_revision:
                raise StateConflictError("Refresh revision changed unexpectedly")
            return refreshed.access_token
        except Exception:
            # refresh_inflight deliberately remains durable. A one-time refresh token
            # must never be retried automatically after an uncertain outcome.
            raise

    def save_application_token(self, tokens: OAuthTokens) -> None:
        with self._lock():
            self.secrets.write("application_token", tokens.as_secret(generation=0))

    def application_access_token(self) -> str | None:
        value = self.secrets.read("application_token")
        if not value:
            return None
        tokens = OAuthTokens.from_secret(value)
        return None if tokens.expired() else tokens.access_token

    async def get_application_token(
        self, token_call: Callable[[str, str], Awaitable[dict[str, object]]]
    ) -> str:
        async with self._async_lock():
            existing = self.application_access_token()
            if existing:
                return existing
            client_id, client_secret = self.application_credentials()
            tokens = OAuthTokens.from_response(await token_call(client_id, client_secret))
            self.secrets.write("application_token", tokens.as_secret(generation=0))
            return tokens.access_token


@dataclass(frozen=True, slots=True)
class PKCERequest:
    state: str
    verifier: str
    authorize_url: str


def make_pkce_request(settings: Settings, client_id: str) -> PKCERequest:
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    query = urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": settings.callback_url,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )
    return PKCERequest(state=state, verifier=verifier, authorize_url=f"{settings.authorize_url}?{query}")
