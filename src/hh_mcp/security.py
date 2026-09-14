from __future__ import annotations

import csv
import os
import stat
import subprocess
import sys
from pathlib import Path

from .errors import ConfigurationError


def _reject_symlink(path: Path) -> None:
    if os.path.lexists(path) and path.is_symlink():
        raise ConfigurationError("The HH MCP runtime directory must not be a symbolic link")


def _secure_windows(path: Path) -> None:
    sid_result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"], check=False,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if sid_result.returncode != 0:
        raise ConfigurationError("Could not determine the current Windows user SID")
    try:
        fields = next(csv.reader([sid_result.stdout.strip()]))
    except (csv.Error, StopIteration) as exc:
        raise ConfigurationError("Could not parse the current Windows user SID") from exc
    if len(fields) < 2 or not fields[1].strip().startswith("S-"):
        raise ConfigurationError("Could not parse the current Windows user SID")
    user_sid = fields[1].strip()
    acl = subprocess.run(
        ["icacls.exe", os.fspath(path), "/inheritance:r", "/grant:r",
         f"*{user_sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if acl.returncode != 0:
        raise ConfigurationError("Could not restrict the HH MCP runtime directory ACL")


def _secure_posix(path: Path) -> None:
    before = os.lstat(path)
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
        raise ConfigurationError("The HH MCP runtime path must be a real directory")
    if before.st_uid != os.getuid():
        raise ConfigurationError("The HH MCP runtime directory must be owned by the current user")
    os.chmod(path, 0o700)
    after = os.lstat(path)
    if stat.S_ISLNK(after.st_mode) or after.st_uid != os.getuid() or stat.S_IMODE(after.st_mode) != 0o700:
        raise ConfigurationError("Could not enforce owner-only permissions on the HH MCP runtime directory")


def ensure_private_directory(path: Path, *, platform_name: str | None = None) -> None:
    """Create and verify the private runtime directory, failing closed."""
    platform_name = platform_name or sys.platform
    _reject_symlink(path)
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    _reject_symlink(path)
    if platform_name == "win32":
        _secure_windows(path)
    elif platform_name == "darwin" or platform_name.startswith("linux"):
        _secure_posix(path)
    else:
        raise ConfigurationError(f"Unsupported platform for private storage: {platform_name}")
