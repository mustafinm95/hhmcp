from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class EnvironmentAccess:
    token: str
    user_agent: str


@dataclass(frozen=True, slots=True)
class Settings:
    home: Path
    api_base_url: str = "https://api.hh.ru"
    authorize_url: str = "https://hh.ru/oauth/authorize"
    token_url: str = "https://api.hh.ru/token"
    callback_url: str = "http://127.0.0.1:8765/callback"
    host: str = "hh.ru"
    locale: str = "RU"
    user_agent: str = ""
    request_timeout_seconds: float = 20.0

    @property
    def state_db(self) -> Path:
        return self.home / "state.db"

    @property
    def environment_state_db(self) -> Path:
        return self.home / "environment-state.db"

    @property
    def auth_lock(self) -> Path:
        return self.home / "auth.lock"

    @property
    def secrets_dir(self) -> Path:
        return self.home / "secrets"

    def validate(self) -> None:
        if (
            not self.user_agent.strip()
            or "@" not in self.user_agent
            or "\r" in self.user_agent
            or "\n" in self.user_agent
        ):
            raise ConfigurationError("HH User-Agent must contain a contact email and no line breaks")
        for name, value, expected_host in (
            ("api_base_url", self.api_base_url, "api.hh.ru"),
            ("token_url", self.token_url, "api.hh.ru"),
            ("authorize_url", self.authorize_url, "hh.ru"),
        ):
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.hostname != expected_host:
                raise ConfigurationError(f"{name} must use https://{expected_host}")
        callback = urlparse(self.callback_url)
        if (
            callback.scheme != "http"
            or callback.hostname != "127.0.0.1"
            or not callback.port
            or callback.username
            or callback.password
            or callback.query
            or callback.fragment
            or not callback.path.startswith("/")
        ):
            raise ConfigurationError("callback_url must be an explicit http://127.0.0.1:<port>/ path")


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def default_home(
    *, platform_name: str | None = None,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
) -> Path:
    platform_name = platform_name or sys.platform
    environment = os.environ if environ is None else environ
    override = environment.get("HH_MCP_HOME")
    if override:
        return _absolute_without_resolving(Path(override))
    if platform_name == "win32":
        local = environment.get("LOCALAPPDATA")
        if not local:
            raise ConfigurationError("LOCALAPPDATA is unavailable; set HH_MCP_HOME explicitly")
        return _absolute_without_resolving(Path(local) / "HHMCP")
    home = user_home or Path.home()
    if platform_name == "darwin":
        return _absolute_without_resolving(home / "Library" / "Application Support" / "HHMCP")
    if platform_name.startswith("linux"):
        xdg = environment.get("XDG_DATA_HOME")
        if xdg:
            xdg_path = Path(xdg).expanduser()
            if not xdg_path.is_absolute():
                raise ConfigurationError("XDG_DATA_HOME must be absolute")
            return _absolute_without_resolving(xdg_path / "hh-mcp")
        return _absolute_without_resolving(home / ".local" / "share" / "hh-mcp")
    raise ConfigurationError(f"Unsupported platform: {platform_name}")


def environment_access(environ: Mapping[str, str] | None = None) -> EnvironmentAccess | None:
    environment = os.environ if environ is None else environ
    token_present = "HH_MCP_ACCESS_TOKEN" in environment
    user_agent_present = "HH_MCP_USER_AGENT" in environment
    if not token_present and not user_agent_present:
        return None
    if not token_present or not user_agent_present:
        raise ConfigurationError(
            "HH_MCP_ACCESS_TOKEN and HH_MCP_USER_AGENT must be set together for environment access mode"
        )
    token = environment["HH_MCP_ACCESS_TOKEN"]
    if not token or token != token.strip() or "\r" in token or "\n" in token:
        raise ConfigurationError("HH_MCP_ACCESS_TOKEN must be a non-empty value without surrounding whitespace")
    user_agent = environment["HH_MCP_USER_AGENT"]
    if not user_agent.strip() or "@" not in user_agent or "\r" in user_agent or "\n" in user_agent:
        raise ConfigurationError("HH_MCP_USER_AGENT must contain a contact email and no line breaks")
    return EnvironmentAccess(token=token, user_agent=user_agent)


def load_settings(home: Path | None = None) -> Settings:
    resolved = _absolute_without_resolving(home or default_home())
    path = resolved / "config.json"
    if not path.exists():
        return Settings(home=resolved)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Cannot read {path}: {exc}") from exc
    allowed = {field for field in Settings.__dataclass_fields__ if field != "home"}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ConfigurationError(f"Unknown settings: {', '.join(unknown)}")
    return Settings(home=resolved, **data)


def save_settings(settings: Settings) -> None:
    settings.home.mkdir(parents=True, exist_ok=True)
    data = asdict(settings)
    data.pop("home")
    path = settings.home / "config.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
