from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hh_mcp.config import Settings, environment_access
from hh_mcp.errors import ConfigurationError, HHMCPError
from hh_mcp.runtime import Runtime


ENV = {
    "HH_MCP_ACCESS_TOKEN": "environment-secret-token",
    "HH_MCP_USER_AGENT": "HHMCPTests/0.1 tests@example.com",
}


@pytest.mark.parametrize(
    "values",
    [
        {"HH_MCP_ACCESS_TOKEN": "token"},
        {"HH_MCP_USER_AGENT": "Tests tests@example.com"},
        {"HH_MCP_ACCESS_TOKEN": "", "HH_MCP_USER_AGENT": "Tests tests@example.com"},
        {"HH_MCP_ACCESS_TOKEN": " token ", "HH_MCP_USER_AGENT": "Tests tests@example.com"},
        {"HH_MCP_ACCESS_TOKEN": "token", "HH_MCP_USER_AGENT": "no-contact"},
        {"HH_MCP_ACCESS_TOKEN": "token", "HH_MCP_USER_AGENT": "x@example.com\r\nbad"},
    ],
)
def test_environment_access_requires_complete_safe_values(values: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError):
        environment_access(values)


def test_environment_access_is_absent_without_either_variable() -> None:
    assert environment_access({}) is None
    access = environment_access(ENV)
    assert access is not None
    assert access.token == "environment-secret-token"
    assert access.user_agent == "HHMCPTests/0.1 tests@example.com"


@pytest.mark.asyncio
async def test_environment_reads_skip_config_keyring_and_local_state(
    monkeypatch: pytest.MonkeyPatch, workspace_tmp: Path
) -> None:
    home = workspace_tmp / "unused-home"
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.url.path,
                request.headers.get("authorization", ""),
                request.headers.get("user-agent", ""),
            )
        )
        if request.url.path == "/vacancies":
            return httpx.Response(200, json={"items": [], "page": 0, "pages": 0, "per_page": 20})
        if request.url.path == "/resumes/mine":
            return httpx.Response(200, json={"items": [], "page": 0, "pages": 0, "per_page": 20})
        if request.url.path == "/areas":
            return httpx.Response(200, json=[])
        raise AssertionError(request.url.path)

    monkeypatch.setattr("hh_mcp.runtime.load_settings", lambda: (_ for _ in ()).throw(AssertionError()))
    monkeypatch.setattr("hh_mcp.runtime.default_home", lambda environ: home)
    monkeypatch.setattr(
        "hh_mcp.runtime.KeyringCredentialBackend.from_system",
        lambda: (_ for _ in ()).throw(AssertionError()),
    )
    runtime = Runtime(
        environ=ENV,
        http_transport=httpx.MockTransport(handler),
        secure_directory=lambda path: (_ for _ in ()).throw(AssertionError()),
    )
    try:
        await runtime.service.search_vacancies(page=0, per_page=20)
        await runtime.service.list_resumes(0, 20, False)
        await runtime.service.get_reference("areas", None, 10)
        assert not home.exists()
        assert runtime.state is None
        assert runtime.secrets is None
        assert {item[0] for item in seen} == {"/vacancies", "/resumes/mine", "/areas"}
        assert all(item[1] == "Bearer environment-secret-token" for item in seen)
        assert all(item[2] == ENV["HH_MCP_USER_AGENT"] for item in seen)
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_prepare_lazily_binds_applicant_before_creating_encrypted_state(
    workspace_tmp: Path,
) -> None:
    home = workspace_tmp / "env-state"
    settings = Settings(home=home, user_agent="configured@example.com")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"] == "Bearer environment-secret-token"
        if request.url.path == "/me":
            assert not home.exists()
            return httpx.Response(200, json={"id": "account-7", "is_applicant": True, "auth_type": "applicant"})
        assert home.exists()
        if request.url.path == "/resumes/mine":
            return httpx.Response(200, json={"items": [{"id": "resume-1"}], "pages": 1})
        if request.url.path == "/resumes/resume-1":
            return httpx.Response(200, json={"id": "resume-1", "title": "Backend"})
        if request.url.path == "/vacancies/vacancy-1":
            return httpx.Response(200, json={"id": "vacancy-1", "name": "Python", "archived": False})
        if request.url.path == "/negotiations":
            return httpx.Response(200, json={"items": []})
        raise AssertionError(request.url.path)

    runtime = Runtime(
        settings,
        environ=ENV,
        http_transport=httpx.MockTransport(handler),
        secure_directory=lambda path: path.mkdir(parents=True, exist_ok=True),
    )
    try:
        result = await runtime.service.applications.prepare(
            "vacancy-1", "resume-1", "private-draft-marker"
        )
        assert result["account_id"] == "account-7"
        assert calls[0] == "/me"
        assert runtime.state is not None
        database = settings.environment_state_db.read_bytes()
        assert b"private-draft-marker" not in database
        assert b"environment-secret-token" not in database
        assert runtime.auth.status()["account_id"] == "account-7"
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_prepare_rejects_non_applicant_before_creating_state(workspace_tmp: Path) -> None:
    home = workspace_tmp / "wrong-role"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/me"
        return httpx.Response(200, json={"id": "employer", "is_applicant": False, "auth_type": "employer"})

    runtime = Runtime(
        Settings(home=home, user_agent="configured@example.com"),
        environ=ENV,
        http_transport=httpx.MockTransport(handler),
        secure_directory=lambda path: path.mkdir(parents=True, exist_ok=True),
    )
    try:
        with pytest.raises(HHMCPError, match="applicant"):
            await runtime.service.applications.prepare("vacancy", "resume", "")
        assert runtime.state is None
        assert not home.exists()
    finally:
        await runtime.close()


@pytest.mark.asyncio
async def test_environment_401_requests_token_replacement_without_leakage(
    workspace_tmp: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={
                "error": "invalid_token",
                "error_description": "environment-secret-token",
            },
        )

    runtime = Runtime(
        Settings(home=workspace_tmp / "unused", user_agent="configured@example.com"),
        environ=ENV,
        http_transport=httpx.MockTransport(handler),
        secure_directory=lambda path: (_ for _ in ()).throw(AssertionError()),
    )
    try:
        with pytest.raises(HHMCPError) as caught:
            await runtime.service.list_resumes(0, 20, False)
        assert caught.value.kind == "environment_access_token_rejected"
        assert "replace" in caught.value.message
        assert "environment-secret-token" not in repr(caught.value.as_dict())
    finally:
        await runtime.close()
