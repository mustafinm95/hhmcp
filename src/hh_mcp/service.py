from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from .applications import ApplicationService, require_owned_resume
from .auth import AuthManager
from .client import HHClient, strip_contacts
from .errors import ValidationError


class HHService:
    def __init__(self, client: HHClient, auth: AuthManager, applications: ApplicationService) -> None:
        self.client = client
        self.auth = auth
        self.applications = applications

    async def public_token(self) -> str:
        return await self.auth.get_application_token(
            lambda client_id, client_secret: self.client.token_request(
                {
                    "grant_type": "client_credentials", "client_id": client_id,
                    "client_secret": client_secret,
                }
            )
        )

    async def user_token(self) -> str:
        return await self.auth.user_access_token(
            lambda refresh: self.client.token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh}
            )
        )

    async def search_vacancies(self, **values: Any) -> dict[str, object]:
        page = _bounded_int(values.pop("page", 0), "page", 0, 1999)
        per_page = _bounded_int(values.pop("per_page", 20), "per_page", 1, 100)
        if (page + 1) * per_page > 2000:
            raise ValidationError("Requested page exceeds HH's 2000-result search depth")
        if values.get("period") is not None and (values.get("date_from") or values.get("date_to")):
            raise ValidationError("period cannot be combined with date_from or date_to")
        if values.get("salary") is not None and values["salary"] < 0:
            raise ValidationError("salary must be non-negative")
        if values.get("period") is not None and values["period"] < 1:
            raise ValidationError("period must be positive")
        for key in ("date_from", "date_to"):
            if values.get(key):
                try:
                    datetime.fromisoformat(str(values[key]).replace("Z", "+00:00"))
                except ValueError as exc:
                    raise ValidationError(f"{key} must use ISO 8601 format") from exc
        allowed_orders = {None, "publication_time", "salary_desc", "salary_asc", "relevance", "distance"}
        if values.get("order_by") not in allowed_orders:
            raise ValidationError("order_by is not supported", details={"allowed": sorted(x for x in allowed_orders if x)})
        allowed_education = {"not_required_or_not_specified", "special_secondary", "higher"}
        if values.get("education") and not set(values["education"]) <= allowed_education:
            raise ValidationError("education contains an unsupported value", details={"allowed": sorted(allowed_education)})
        geo = [values.get(name) for name in ("top_lat", "bottom_lat", "left_lng", "right_lng")]
        if any(item is not None for item in geo) and not all(item is not None for item in geo):
            raise ValidationError("All four geographic bounds must be provided together")
        if values.get("order_by") == "distance" and (
            values.get("sort_point_lat") is None or values.get("sort_point_lng") is None
        ):
            raise ValidationError("distance ordering requires sort_point_lat and sort_point_lng")
        params: list[tuple[str, Any]] = [("page", page), ("per_page", per_page)]
        for key, value in values.items():
            if value is None:
                continue
            if isinstance(value, list):
                params.extend((key, item) for item in value)
            else:
                params.append((key, value))
        return await self.client.search_vacancies(await self.public_token(), params)

    async def get_vacancy(self, vacancy_id: str, detailed: bool) -> dict[str, object]:
        value = await self.client.get_vacancy(vacancy_id, await self.public_token())
        return value if detailed else _pick(
            value,
            "id", "name", "employer", "area", "salary", "salary_range", "experience",
            "employment_form", "work_format", "snippet", "alternate_url", "published_at",
            "archived", "response_letter_required",
        )

    async def get_employer(self, employer_id: str, detailed: bool) -> dict[str, object]:
        value = await self.client.get_employer(employer_id, await self.public_token())
        return value if detailed else _pick(
            value, "id", "name", "type", "site_url", "alternate_url", "area", "industries", "open_vacancies"
        )

    async def get_reference(self, name: str, query: str | None, limit: int) -> dict[str, object]:
        limit = _bounded_int(limit, "limit", 1, 500)
        value = await self.client.get_reference(name, token=await self.public_token())
        if not query:
            data, truncated = _limit_reference(value, limit)
            return {"name": name, "data": data, "limit": limit, "truncated": truncated}
        all_matches = list(_search_reference(value, query.casefold()))
        matches = all_matches[:limit]
        return {
            "name": name, "query": query, "items": matches, "returned": len(matches),
            "limit": limit, "truncated": len(all_matches) > limit,
        }

    async def list_resumes(self, page: int, per_page: int, include_contacts: bool) -> dict[str, object]:
        page = _bounded_int(page, "page", 0, 10000)
        per_page = _bounded_int(per_page, "per_page", 1, 100)
        value = await self.client.list_my_resumes(await self.user_token(), page, per_page)
        return value if include_contacts else strip_contacts(value)

    async def get_resume(self, resume_id: str, detailed: bool, include_contacts: bool) -> dict[str, object]:
        token = await self.user_token()
        await require_owned_resume(self.client, token, resume_id)
        value = await self.client.get_resume(resume_id, token, include_contacts)
        return value if detailed else _pick(
            value, "id", "title", "status", "area", "salary", "skill_set", "experience", "education", "updated_at"
        )

    async def list_applications(
        self, page: int, per_page: int, status: str | None, vacancy_id: str | None
    ) -> dict[str, object]:
        return await self.client.list_negotiations(
            await self.user_token(),
            page=_bounded_int(page, "page", 0, 10000),
            per_page=_bounded_int(per_page, "per_page", 1, 100),
            status=status,
            vacancy_id=vacancy_id,
        )

    async def get_application(self, application_id: str) -> dict[str, object]:
        return await self.client.get_negotiation(application_id, await self.user_token())


def _bounded_int(value: int, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _pick(value: dict[str, object], *keys: str) -> dict[str, object]:
    return {key: value[key] for key in keys if key in value}


def _search_reference(value: object, query: str) -> Iterable[dict[str, object]]:
    if isinstance(value, dict):
        label = " ".join(str(value.get(key, "")) for key in ("id", "name", "code"))
        if query in label.casefold() and label.strip():
            yield value
        for child in value.values():
            yield from _search_reference(child, query)
    elif isinstance(value, list):
        for child in value:
            yield from _search_reference(child, query)


def _limit_reference(value: object, limit: int) -> tuple[object, bool]:
    if isinstance(value, list):
        return value[:limit], len(value) > limit
    if isinstance(value, dict):
        items = list(value.items())
        return dict(items[:limit]), len(items) > limit
    return value, False
