from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class HHMCPError(Exception):
    kind: str
    message: str
    status_code: int | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.kind, "message": self.message}
        if self.status_code is not None:
            result["status_code"] = self.status_code
        if self.details:
            result["details"] = self.details
        return result


class ConfigurationError(HHMCPError):
    def __init__(self, message: str) -> None:
        super().__init__("configuration", message)


class AuthenticationError(HHMCPError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("authentication_required", message, details=details)


class StateConflictError(HHMCPError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("state_conflict", message, details=details)


class ValidationError(HHMCPError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__("invalid_parameters", message, details=details)

