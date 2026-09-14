from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hh_mcp.config import default_home, load_settings
from hh_mcp.errors import ConfigurationError
from hh_mcp import security


def test_default_data_directories_for_supported_platforms() -> None:
    home = Path("C:/Users/test")
    assert default_home(
        platform_name="win32", environ={"LOCALAPPDATA": "C:/Data"}, user_home=home
    ) == Path("C:/Data/HHMCP")
    assert default_home(platform_name="darwin", environ={}, user_home=Path("/Users/test")) == (
        Path("/Users/test/Library/Application Support/HHMCP").absolute()
    )
    assert default_home(platform_name="linux", environ={}, user_home=Path("/home/test")) == (
        Path("/home/test/.local/share/hh-mcp").absolute()
    )
    assert default_home(
        platform_name="linux", environ={"XDG_DATA_HOME": "C:/data/test"}, user_home=Path("/home/test")
    ) == Path("C:/data/test/hh-mcp")


def test_relative_xdg_data_home_is_rejected() -> None:
    with pytest.raises(ConfigurationError):
        default_home(platform_name="linux", environ={"XDG_DATA_HOME": "relative"})


def test_load_settings_does_not_resolve_runtime_symlink(monkeypatch, workspace_tmp: Path) -> None:
    target = workspace_tmp / "target"
    link = workspace_tmp / "link"
    target.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is unavailable in this environment")
    loaded = load_settings(link)
    assert loaded.home == link.absolute()
    with pytest.raises(ConfigurationError):
        security.ensure_private_directory(link)


def test_platform_security_dispatch(monkeypatch, workspace_tmp: Path) -> None:
    calls: list[str] = []
    monkeypatch.setattr(security, "_secure_windows", lambda path: calls.append("windows"))
    monkeypatch.setattr(security, "_secure_posix", lambda path: calls.append("posix"))
    security.ensure_private_directory(workspace_tmp / "windows", platform_name="win32")
    security.ensure_private_directory(workspace_tmp / "mac", platform_name="darwin")
    security.ensure_private_directory(workspace_tmp / "linux", platform_name="linux")
    assert calls == ["windows", "posix", "posix"]


def test_posix_owner_mismatch_fails_closed(monkeypatch, workspace_tmp: Path) -> None:
    directory = workspace_tmp / "private"
    directory.mkdir()
    real = os.lstat(directory)
    fake = SimpleNamespace(st_mode=real.st_mode, st_uid=123)
    monkeypatch.setattr(security.os, "lstat", lambda path: fake)
    monkeypatch.setattr(security.os, "getuid", lambda: 456, raising=False)
    with pytest.raises(ConfigurationError):
        security._secure_posix(directory)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission integration")
def test_posix_directory_is_owner_only(workspace_tmp: Path) -> None:
    directory = workspace_tmp / "private"
    security.ensure_private_directory(directory, platform_name=sys.platform)
    info = os.lstat(directory)
    assert info.st_uid == os.getuid()
    assert stat.S_IMODE(info.st_mode) == 0o700
