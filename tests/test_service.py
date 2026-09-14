from __future__ import annotations

import pytest

from hh_mcp.errors import ValidationError
from hh_mcp.service import HHService


class SearchClient:
    async def search_vacancies(self, token, params):
        return {"token": token, "params": params}


class SearchAuth:
    async def get_application_token(self, token_call):
        return "app"


def service() -> HHService:
    return HHService(SearchClient(), SearchAuth(), object())  # type: ignore[arg-type]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "values",
    [
        {"page": 20, "per_page": 100},
        {"page": 0, "per_page": 20, "salary": -1},
        {"page": 0, "per_page": 20, "period": 2, "date_from": "2026-01-01"},
        {"page": 0, "per_page": 20, "date_from": "not-a-date"},
        {"page": 0, "per_page": 20, "order_by": "typo"},
        {"page": 0, "per_page": 20, "top_lat": 1.0},
    ],
)
async def test_search_rejects_invalid_combinations(values) -> None:
    with pytest.raises(ValidationError):
        await service().search_vacancies(**values)


@pytest.mark.asyncio
async def test_search_maps_lists_to_repeated_parameters() -> None:
    result = await service().search_vacancies(page=0, per_page=20, area=["1", "2"], text="python")
    assert result["params"].count(("area", "1")) == 1
    assert result["params"].count(("area", "2")) == 1
