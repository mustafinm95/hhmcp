from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client

from hh_mcp.server import create_server


class FakeAuth:
    def status(self):
        return {"logged_in": False, "marker": "safe"}


@pytest.mark.asyncio
async def test_tools_list_excludes_submit_and_has_strict_schemas() -> None:
    server = create_server(SimpleNamespace(auth=FakeAuth()))
    async with Client(server) as client:
        tools = await client.list_tools()
    names = {tool.name for tool in tools.tools}
    assert len(names) == 10
    assert "hh_prepare_application" in names
    assert "hh_submit_application" not in names
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools.tools)


@pytest.mark.asyncio
async def test_unknown_tool_argument_is_rejected_before_handler() -> None:
    server = create_server(SimpleNamespace(auth=FakeAuth()))
    async with Client(server) as client:
        result = await client.call_tool("hh_auth_status", {"unexpected": 1})
    assert result.is_error is True
    assert "extra_forbidden" in result.content[0].text


@pytest.mark.asyncio
async def test_auth_status_returns_structured_content() -> None:
    server = create_server(SimpleNamespace(auth=FakeAuth()))
    async with Client(server) as client:
        result = await client.call_tool("hh_auth_status", {})
    assert result.is_error is False
    assert result.structured_content == {"ok": True, "logged_in": False, "marker": "safe"}


def test_stdio_initialization_and_tools_list(workspace_tmp: Path) -> None:
    project = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(project / "src")
    environment["HH_MCP_HOME"] = str(workspace_tmp)
    environment["HH_MCP_ACCESS_TOKEN"] = "stdio-secret-token"
    environment["HH_MCP_USER_AGENT"] = "HHMCPTests/0.1 tests@example.com"
    process = subprocess.Popen(
        [sys.executable, "-m", "hh_mcp", "serve"],
        cwd=project, env=environment, text=True, encoding="utf-8", bufsize=1,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        initialize = {
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "hh-mcp-tests", "version": "1"},
            },
        }
        process.stdin.write(json.dumps(initialize) + "\n")
        process.stdin.flush()
        assert json.loads(process.stdout.readline())["id"] == 1
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
        process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}) + "\n")
        process.stdin.flush()
        response = json.loads(process.stdout.readline())
        tools = response["result"]["tools"]
        assert len(tools) == 10
        assert all(tool["name"] != "hh_submit_application" for tool in tools)
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "hh_auth_status", "arguments": {"unexpected": 1}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        rejected = json.loads(process.stdout.readline())
        assert rejected["result"]["isError"] is True
        assert "extra_forbidden" in rejected["result"]["content"][0]["text"]
        process.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "hh_auth_status", "arguments": {}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        status = json.loads(process.stdout.readline())
        serialized = json.dumps(status)
        assert status["result"]["structuredContent"]["mode"] == "environment_access_token"
        assert "stdio-secret-token" not in serialized
        assert not (workspace_tmp / "config.json").exists()
        assert not (workspace_tmp / "state.db").exists()
        assert not (workspace_tmp / "environment-state.db").exists()
    finally:
        process.terminate()
        process.wait(timeout=5)
