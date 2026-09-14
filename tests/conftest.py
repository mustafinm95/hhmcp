from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

from hh_mcp.config import Settings


@pytest.fixture
def workspace_tmp() -> Path:
    """Use an inherited-ACL temp root when a managed sandbox explicitly provides one."""
    override = os.environ.get("HH_MCP_TEST_TMP_ROOT")
    if override:
        path = Path(override) / uuid.uuid4().hex
        path.mkdir(parents=True)
        return path
    return Path(tempfile.mkdtemp(prefix="hh-mcp-tests-"))


@pytest.fixture
def settings(workspace_tmp: Path) -> Settings:
    return Settings(
        home=workspace_tmp,
        user_agent="HHMCPTests/0.1 tests@example.com",
        request_timeout_seconds=1,
    )
