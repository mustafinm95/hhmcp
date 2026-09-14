from __future__ import annotations

import httpx
import pytest

from hh_mcp.client import HHClient
from hh_mcp.errors import HHMCPError


@pytest.mark.asyncio
async def test_search_preserves_repeated_filters_and_pagination(settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"items": [{"id": str(i)} for i in range(12)], "found": 3000, "page": 19, "pages": 20, "per_page": 100},
        )

    client = HHClient(settings, httpx.MockTransport(handler))
    result = await client.search_vacancies("app-token", [("area", "1"), ("area", "2"), ("page", 19), ("per_page", 100)])
    assert requests[0].url.params.get_list("area") == ["1", "2"]
    assert result["returned"] == 12
    assert result["has_next_page"] is False
    assert result["warnings"] == ["HH search depth is capped at 2000 results"]
    await client.aclose()


@pytest.mark.asyncio
async def test_redirect_is_not_followed(settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(303, headers={"Location": "https://evil.example/collect"})

    client = HHClient(settings, httpx.MockTransport(handler))
    status, location, _ = await client.submit_negotiation(
        "marker-token", vacancy_id="1", resume_id="2", message="hello"
    )
    assert (status, location, calls) == (303, "https://evil.example/collect", 1)
    await client.aclose()


@pytest.mark.asyncio
async def test_error_body_cannot_reflect_secret(settings) -> None:
    marker = "SUPER-SECRET-MARKER"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": "invalid_request",
                "error_description": marker,
                "errors": [{"type": "bad_argument", "value": marker}],
            },
        )

    client = HHClient(settings, httpx.MockTransport(handler))
    with pytest.raises(HHMCPError) as caught:
        await client.get_vacancy("1", marker)
    assert marker not in repr(caught.value.as_dict())
    assert caught.value.details == {"error": "invalid_request", "error_types": ["bad_argument"]}
    await client.aclose()


@pytest.mark.asyncio
async def test_long_retry_after_is_not_shortened(settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "60"}, json={"errors": [{"type": "too_many_requests"}]})

    client = HHClient(settings, httpx.MockTransport(handler))
    with pytest.raises(HHMCPError) as caught:
        await client.get_vacancy("1", None)
    assert caught.value.kind == "rate_limited"
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_submit_is_multipart_and_no_automatic_retry(settings) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        body = request.read()
        assert request.headers["content-type"].startswith("multipart/form-data")
        assert all(value in body for value in (b"vacancy_id", b"resume_id", b"message", b"letter"))
        return httpx.Response(201, headers={"Location": "/negotiations/99"})

    client = HHClient(settings, httpx.MockTransport(handler))
    assert await client.submit_negotiation("token", vacancy_id="1", resume_id="2", message="letter") == (201, "/negotiations/99", None)
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_contact_fields_are_removed_recursively(settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "r", "contact": [{"value": "x"}], "nested": {"email": "x", "safe": 1}})

    client = HHClient(settings, httpx.MockTransport(handler))
    assert await client.get_resume("r", "token", False) == {"id": "r", "nested": {"safe": 1}}
    await client.aclose()
