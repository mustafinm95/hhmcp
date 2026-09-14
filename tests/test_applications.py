from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hh_mcp.applications import ApplicationService
from hh_mcp.auth import AuthManager, OAuthTokens
from hh_mcp.errors import HHMCPError, StateConflictError
from hh_mcp.secrets import MemorySecretStore
from hh_mcp.state import PlaintextTestProtector, StateStore


class FakeClient:
    def __init__(self) -> None:
        self.vacancy = {
            "id": "V", "name": "Engineer", "archived": False,
            "response_letter_required": False, "employer": {"id": "E", "name": "Co"},
        }
        self.history: dict[str, object] = {"items": [], "pages": 1}
        self.submit_result: object = (201, "/negotiations/1", None)
        self.posts = 0

    async def list_my_resumes(self, token, page, per_page):
        return {"items": [{"id": "R", "title": "Resume"}], "pages": 1}

    async def get_resume(self, resume_id, token, include_contacts):
        return {"id": resume_id, "title": "Resume"}

    async def get_vacancy(self, vacancy_id, token):
        return dict(self.vacancy)

    async def list_negotiations(self, token, **kwargs):
        return self.history

    async def submit_negotiation(self, token, **kwargs):
        self.posts += 1
        if isinstance(self.submit_result, Exception):
            raise self.submit_result
        return self.submit_result

    async def token_request(self, data):
        return {"access_token": "refreshed", "refresh_token": "refresh-2", "expires_in": 3600}


def setup_service(settings):
    state = StateStore(settings.state_db, PlaintextTestProtector())
    auth = AuthManager(settings, state, MemorySecretStore())
    auth.commit_login(
        "A",
        OAuthTokens("token", "bearer", datetime.now(timezone.utc) + timedelta(hours=1), "refresh"),
    )
    client = FakeClient()
    return ApplicationService(client, auth, state), auth, state, client


@pytest.mark.asyncio
async def test_prepare_is_account_bound_and_preserves_exact_text(settings) -> None:
    service, _, state, _ = setup_service(settings)
    result = await service.prepare("V", "R", "Exact letter\nline two")
    draft = state.get_draft(str(result["draft_id"]))
    assert draft.account_id == "A"
    assert draft.message == "Exact letter\nline two"
    assert result["message_sha256"] == draft.message_hash


@pytest.mark.asyncio
async def test_prepare_rejects_unowned_resume(settings) -> None:
    service, _, _, _ = setup_service(settings)
    with pytest.raises(StateConflictError, match="not owned"):
        await service.prepare("V", "OTHER", "letter")


@pytest.mark.asyncio
async def test_unknown_blocks_another_draft_and_restart(settings) -> None:
    service, _, state, client = setup_service(settings)
    first = await service.prepare("V", "R", "one")
    second = await service.prepare("V", "R", "two")
    client.submit_result = HHMCPError("network_error", "generic network failure")
    with pytest.raises(HHMCPError):
        await service.submit_from_cli(str(first["draft_id"]))
    reopened = StateStore(settings.state_db, PlaintextTestProtector())
    service.state = reopened
    with pytest.raises(StateConflictError, match="reserves"):
        await service.submit_from_cli(str(second["draft_id"]))
    assert client.posts == 1


@pytest.mark.asyncio
async def test_account_switch_rejects_old_draft_before_post(settings) -> None:
    service, auth, _, client = setup_service(settings)
    draft = await service.prepare("V", "R", "letter")
    auth.commit_login(
        "B", OAuthTokens("token-b", "bearer", datetime.now(timezone.utc) + timedelta(hours=1), "r-b")
    )
    with pytest.raises(StateConflictError):
        await service.submit_from_cli(str(draft["draft_id"]))
    assert client.posts == 0


@pytest.mark.asyncio
async def test_submit_revalidates_direct_vacancy_before_reservation(settings) -> None:
    service, _, state, client = setup_service(settings)
    draft = await service.prepare("V", "R", "letter")
    client.vacancy["type"] = {"id": "direct"}
    with pytest.raises(StateConflictError, match="direct"):
        await service.submit_from_cli(str(draft["draft_id"]))
    assert state.operation_for_draft(str(draft["draft_id"])) is None
    assert client.posts == 0


@pytest.mark.asyncio
async def test_submit_revalidates_required_test_before_reservation(settings) -> None:
    service, _, state, client = setup_service(settings)
    draft = await service.prepare("V", "R", "letter")
    client.vacancy["has_test"] = True
    with pytest.raises(StateConflictError, match="test"):
        await service.submit_from_cli(str(draft["draft_id"]))
    assert state.operation_for_draft(str(draft["draft_id"])) is None
    assert client.posts == 0


@pytest.mark.asyncio
async def test_success_clears_letter_and_keeps_target_reserved(settings) -> None:
    service, _, state, client = setup_service(settings)
    first = await service.prepare("V", "R", "secret letter")
    second = await service.prepare("V", "R", "other")
    result = await service.submit_from_cli(str(first["draft_id"]))
    assert result["status"] == "sent"
    assert state.get_draft(str(first["draft_id"])).message == ""
    with pytest.raises(StateConflictError):
        await service.submit_from_cli(str(second["draft_id"]))
    assert client.posts == 1


@pytest.mark.asyncio
async def test_processing_error_after_possible_201_is_unknown(settings) -> None:
    service, _, state, client = setup_service(settings)
    draft = await service.prepare("V", "R", "letter")
    client.submit_result = HHMCPError("response_too_large", "bounded", status_code=201)
    with pytest.raises(HHMCPError):
        await service.submit_from_cli(str(draft["draft_id"]))
    operation = state.operation_for_draft(str(draft["draft_id"]))
    assert operation is not None and operation["status"] == "unknown"


@pytest.mark.asyncio
async def test_unknown_status_checks_history_without_unblocking(settings) -> None:
    service, _, state, client = setup_service(settings)
    draft = await service.prepare("V", "R", "letter")
    client.submit_result = HHMCPError("network_error", "generic")
    with pytest.raises(HHMCPError):
        await service.submit_from_cli(str(draft["draft_id"]))
    client.history = {"items": [{"vacancy": {"id": "V"}}]}
    status = await service.status(str(draft["draft_id"]))
    assert status["history_contains_vacancy"] is True
    assert state.operation_for_target("A", "V").status == "unknown"
