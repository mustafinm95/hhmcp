from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from filelock import FileLock

from hh_mcp.auth import AuthManager, OAuthTokens, make_pkce_request
from hh_mcp.errors import AuthenticationError, StateConflictError
from hh_mcp.secrets import MemorySecretStore
from hh_mcp.state import PlaintextTestProtector, StateStore


def _tokens(access: str, refresh: str = "refresh", *, expired: bool = False) -> OAuthTokens:
    expiry = datetime.now(timezone.utc) + timedelta(seconds=-1 if expired else 3600)
    return OAuthTokens(access, "bearer", expiry, refresh)


def _auth(settings):
    state = StateStore(settings.state_db, PlaintextTestProtector())
    secrets = MemorySecretStore()
    return AuthManager(settings, state, secrets), state, secrets


def test_login_logout_generation_and_no_secret_in_status(settings) -> None:
    auth, state, _ = _auth(settings)
    assert auth.commit_login("applicant-A", _tokens("marker-access")) == 1
    status = auth.status()
    assert status["account_id"] == "applicant-A"
    assert "marker-access" not in repr(status)
    assert auth.logout() == 2
    assert state.auth_snapshot().logged_in is False


@pytest.mark.asyncio
async def test_refresh_is_singleflight_and_keeps_auth_epoch(settings) -> None:
    auth, state, _ = _auth(settings)
    auth.commit_login("A", _tokens("old", expired=True))
    calls = 0

    async def refresh(value: str):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        assert value == "refresh"
        return {"access_token": "new", "refresh_token": "new-refresh", "expires_in": 3600}

    assert await asyncio.gather(auth.user_access_token(refresh), auth.user_access_token(refresh)) == ["new", "new"]
    snapshot = state.auth_snapshot()
    assert calls == 1
    assert snapshot.generation == 1
    assert snapshot.credential_revision == 1


@pytest.mark.asyncio
async def test_uncertain_refresh_is_not_retried(settings) -> None:
    auth, state, _ = _auth(settings)
    auth.commit_login("A", _tokens("old", expired=True))

    async def fail(_: str):
        raise RuntimeError("connection lost")

    with pytest.raises(RuntimeError):
        await auth.user_access_token(fail)
    assert state.auth_snapshot().refresh_inflight is True
    with pytest.raises(AuthenticationError):
        await auth.user_access_token(fail)


def test_stale_refresh_commit_cannot_cross_logout(settings) -> None:
    auth, state, _ = _auth(settings)
    auth.commit_login("A", _tokens("old"))
    snapshot = state.auth_snapshot()
    state.set_refresh_inflight(snapshot.generation, snapshot.credential_revision)
    auth.logout()
    with pytest.raises(StateConflictError):
        state.finish_refresh(snapshot.generation, snapshot.credential_revision)


def test_pkce_has_s256_and_unique_state(settings) -> None:
    first = make_pkce_request(settings, "client")
    second = make_pkce_request(settings, "client")
    assert first.state != second.state
    assert "code_challenge_method=S256" in first.authorize_url
    assert first.verifier not in first.authorize_url


def test_failed_login_secret_save_does_not_advance_auth(settings) -> None:
    class FailingStore(MemorySecretStore):
        def write(self, name, value):
            raise OSError("storage failed")

    state = StateStore(settings.state_db, PlaintextTestProtector())
    auth = AuthManager(settings, state, FailingStore())
    with pytest.raises(OSError):
        auth.commit_login("A", _tokens("token"))
    assert state.auth_snapshot().generation == 0
    assert state.auth_snapshot().logged_in is False


@pytest.mark.asyncio
async def test_failed_refresh_save_leaves_recoverable_inflight(settings) -> None:
    class FailingStore(MemorySecretStore):
        fail = False

        def write(self, name, value):
            if self.fail and name == "user_token":
                raise OSError("storage failed")
            super().write(name, value)

    state = StateStore(settings.state_db, PlaintextTestProtector())
    store = FailingStore()
    auth = AuthManager(settings, state, store)
    auth.commit_login("A", _tokens("old", expired=True))
    store.fail = True

    async def refresh(_: str):
        return {"access_token": "new", "refresh_token": "new-r", "expires_in": 3600}

    with pytest.raises(OSError):
        await auth.user_access_token(refresh)
    assert state.auth_snapshot().refresh_inflight is True
    assert auth.status()["requires_login"] is True


@pytest.mark.asyncio
async def test_cancelled_lock_wait_does_not_acquire_later(settings) -> None:
    auth, _, _ = _auth(settings)
    auth.commit_login("A", _tokens("token"))
    blocker = FileLock(settings.auth_lock)
    blocker.acquire()

    async def unused(_: str):
        raise AssertionError("refresh is not expected")

    task = asyncio.create_task(auth.user_access_token(unused))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    blocker.release()
    assert await asyncio.wait_for(auth.user_access_token(unused), timeout=2) == "token"
