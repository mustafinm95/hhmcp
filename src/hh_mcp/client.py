from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from .config import Settings
from .errors import HHMCPError, ValidationError


RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_RETRY_AFTER_SECONDS = 5.0


class HHClient:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        environment_access: bool = False,
    ) -> None:
        self.settings = settings
        self.environment_access = environment_access
        self._client = httpx.AsyncClient(
            base_url=settings.api_base_url,
            timeout=settings.request_timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={
                "User-Agent": settings.user_agent,
                "HH-User-Agent": settings.user_agent,
                "Accept": "application/json",
            },
        )

    async def __aenter__(self) -> "HHClient":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _safe_path(self, path: str) -> str:
        if not path.startswith("/") or path.startswith("//") or urlparse(path).netloc:
            raise ValidationError("HH API path must be relative to the configured api.hh.ru origin")
        return path

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: Mapping[str, Any] | Sequence[tuple[str, Any]] | None = None,
        data: Mapping[str, Any] | None = None,
        files: Mapping[str, tuple[None, str]] | None = None,
        retry_reads: bool = True,
    ) -> tuple[httpx.Response, Any]:
        path = self._safe_path(path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        common_params: list[tuple[str, Any]] = [("host", self.settings.host), ("locale", self.settings.locale)]
        if params:
            common_params.extend(list(params.items()) if isinstance(params, Mapping) else params)
        attempts = 3 if method.upper() == "GET" and retry_reads else 1
        for attempt in range(attempts):
            try:
                response = await self._bounded_request(
                    method, path, headers=headers, params=common_params, data=data, files=files
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt + 1 >= attempts:
                    raise HHMCPError("network_error", "Could not reach the HH API") from exc
                await asyncio.sleep(0.2 * (2**attempt))
                continue
            if response.status_code in RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = _retry_delay(response, attempt)
                if delay is None:
                    break
                await asyncio.sleep(delay)
                continue
            break
        payload = _parse_payload(response)
        if 300 <= response.status_code < 400:
            return response, payload
        if response.is_error:
            raise _http_error(response, payload, environment_access=self.environment_access)
        return response, payload

    async def _bounded_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async with self._client.stream(method, path, **kwargs) as incoming:
            content = bytearray()
            async for chunk in incoming.aiter_bytes():
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise HHMCPError(
                        "response_too_large",
                        f"HH returned more than {MAX_RESPONSE_BYTES} bytes; narrow the request",
                        incoming.status_code,
                    )
            return httpx.Response(
                incoming.status_code,
                headers=incoming.headers,
                content=bytes(content),
                request=incoming.request,
            )

    async def token_request(self, data: Mapping[str, Any]) -> dict[str, object]:
        _, payload = await self.request("POST", "/token", data=data, retry_reads=False)
        if not isinstance(payload, dict):
            raise HHMCPError("invalid_response", "HH token endpoint returned a non-object response")
        return payload

    async def me(self, token: str) -> dict[str, object]:
        _, payload = await self.request("GET", "/me", token=token)
        return _object(payload)

    async def search_vacancies(self, token: str | None, params: Sequence[tuple[str, Any]]) -> dict[str, object]:
        _, payload = await self.request("GET", "/vacancies", token=token, params=params)
        result = _object(payload)
        items = result.get("items") if isinstance(result.get("items"), list) else []
        page = int(result.get("page", 0))
        pages = int(result.get("pages", 0))
        per_page = int(result.get("per_page", len(items)))
        result["returned"] = len(items)
        result["has_next_page"] = page + 1 < pages
        warnings: list[str] = []
        found = result.get("found")
        if isinstance(found, int) and found > 2000 and (page + 1) * per_page >= 2000:
            warnings.append("HH search depth is capped at 2000 results")
        result["warnings"] = warnings
        return result

    async def get_vacancy(self, vacancy_id: str, token: str | None) -> dict[str, object]:
        _, payload = await self.request("GET", f"/vacancies/{_identifier(vacancy_id)}", token=token)
        return _object(payload)

    async def get_employer(self, employer_id: str, token: str | None) -> dict[str, object]:
        _, payload = await self.request("GET", f"/employers/{_identifier(employer_id)}", token=token)
        return _object(payload)

    async def get_reference(
        self, name: str, identifier: str | None = None, token: str | None = None
    ) -> Any:
        paths = {
            "areas": "/areas" if identifier is None else f"/areas/{_identifier(identifier)}",
            "professional_roles": "/professional_roles",
            "metro": "/metro" if identifier is None else f"/metro/{_identifier(identifier)}",
            "industries": "/industries",
            "dictionaries": "/dictionaries",
        }
        if name not in paths:
            raise ValidationError("Unknown reference", details={"allowed": sorted(paths)})
        _, payload = await self.request("GET", paths[name], token=token)
        return payload

    async def list_my_resumes(self, token: str, page: int, per_page: int) -> dict[str, object]:
        _, payload = await self.request(
            "GET", "/resumes/mine", token=token, params={"page": page, "per_page": per_page}
        )
        return _object(payload)

    async def get_resume(self, resume_id: str, token: str, include_contacts: bool) -> dict[str, object]:
        _, payload = await self.request(
            "GET", f"/resumes/{_identifier(resume_id)}", token=token,
            params={"with_creds": str(include_contacts).lower()},
        )
        result = _object(payload)
        return result if include_contacts else strip_contacts(result)

    async def list_negotiations(
        self, token: str, *, page: int, per_page: int, status: str | None, vacancy_id: str | None
    ) -> dict[str, object]:
        params: dict[str, Any] = {"page": page, "per_page": per_page}
        if status:
            params["status"] = status
        if vacancy_id:
            params["vacancy_id"] = _identifier(vacancy_id)
        _, payload = await self.request("GET", "/negotiations", token=token, params=params)
        return _object(payload)

    async def get_negotiation(self, negotiation_id: str, token: str) -> dict[str, object]:
        _, payload = await self.request("GET", f"/negotiations/{_identifier(negotiation_id)}", token=token)
        return _object(payload)

    async def submit_negotiation(
        self, token: str, *, vacancy_id: str, resume_id: str, message: str
    ) -> tuple[int, str | None, Any]:
        files = {
            "vacancy_id": (None, _identifier(vacancy_id)),
            "resume_id": (None, _identifier(resume_id)),
            "message": (None, message),
        }
        response, payload = await self.request(
            "POST", "/negotiations", token=token, files=files, retry_reads=False
        )
        return response.status_code, response.headers.get("Location"), payload


def strip_contacts(value: Any) -> Any:
    blocked = {"contact", "contacts", "email", "phone", "phones"}
    if isinstance(value, dict):
        return {key: strip_contacts(item) for key, item in value.items() if key.lower() not in blocked}
    if isinstance(value, list):
        return [strip_contacts(item) for item in value]
    return value


def _identifier(value: str) -> str:
    if not value or len(value) > 128 or not all(c.isalnum() or c in "-_" for c in value):
        raise ValidationError("ID contains unsupported characters")
    return value


def _object(payload: Any) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise HHMCPError("invalid_response", "HH returned a non-object JSON response")
    return payload


def _parse_payload(response: httpx.Response) -> Any:
    if not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return response.json()
        except json.JSONDecodeError:
            return None
    return None


def _retry_delay(response: httpx.Response, attempt: int) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw:
        try:
            delay = max(0.0, float(raw))
        except ValueError:
            try:
                target = parsedate_to_datetime(raw)
                if target.tzinfo is None:
                    target = target.replace(tzinfo=timezone.utc)
                delay = max(0.0, (target - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None
        return delay if delay <= MAX_RETRY_AFTER_SECONDS else None
    return 0.2 * (2**attempt)


def _http_error(
    response: httpx.Response, payload: Any, *, environment_access: bool = False
) -> HHMCPError:
    status = response.status_code
    safe_details: dict[str, Any] = {}
    if isinstance(payload, dict):
        code = payload.get("error")
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
            safe_details["error"] = code
        error_types: list[str] = []
        if isinstance(payload.get("errors"), list):
            for item in payload["errors"]:
                if isinstance(item, dict):
                    candidate = item.get("type")
                    if isinstance(candidate, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", candidate):
                        error_types.append(candidate)
        if error_types:
            safe_details["error_types"] = error_types[:10]
    serialized = json.dumps(safe_details).lower()
    if status == 401 and environment_access:
        return HHMCPError(
            "environment_access_token_rejected",
            "HH rejected HH_MCP_ACCESS_TOKEN; replace it with a current applicant OAuth access token",
            status,
            safe_details or None,
        )
    if status == 401:
        kind = "authentication_required"
    elif status == 403 and "captcha" in serialized:
        kind = "captcha_required"
    elif status == 403:
        kind = "forbidden"
    elif status == 404:
        kind = "not_found"
    elif status == 429:
        kind = "rate_limited"
    elif status == 400:
        kind = "invalid_parameters"
    elif status >= 500:
        kind = "temporarily_unavailable"
    else:
        kind = "hh_api_error"
    return HHMCPError(kind, f"HH API returned HTTP {status}", status, safe_details or None)
