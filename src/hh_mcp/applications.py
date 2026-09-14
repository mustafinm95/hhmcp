from __future__ import annotations

import httpx

from .auth import AuthManager
from .client import HHClient
from .errors import HHMCPError, StateConflictError, ValidationError
from .state import Draft, StateStore


class ApplicationService:
    def __init__(self, client: HHClient, auth: AuthManager, state: StateStore) -> None:
        self.client = client
        self.auth = auth
        self.state = state

    async def prepare(self, vacancy_id: str, resume_id: str, message: str) -> dict[str, object]:
        if len(message) > 10_000:
            raise ValidationError("Cover letter must not exceed 10000 characters")
        async with self.auth.bound_user(self._refresh) as bound:
            resume = await require_owned_resume(self.client, bound.token, resume_id)
            vacancy = await self.client.get_vacancy(vacancy_id, bound.token)
            _validate_vacancy_for_application(vacancy, message)
            if self.state.operation_for_target(str(bound.identity.account_id), vacancy_id):
                raise StateConflictError("A local operation already exists for this account and vacancy")
            negotiations = await self.client.list_negotiations(
                bound.token, page=0, per_page=20, status=None, vacancy_id=vacancy_id
            )
            if _contains_vacancy(negotiations, vacancy_id):
                raise StateConflictError("HH history already contains an application for this vacancy")
            summary: dict[str, object] = {
                "vacancy": {
                    "id": vacancy_id, "name": vacancy.get("name"),
                    "employer": vacancy.get("employer"), "alternate_url": vacancy.get("alternate_url"),
                },
                "resume": {"id": resume_id, "title": resume.get("title")},
            }
            draft = self.state.create_draft(
                account_id=str(bound.identity.account_id), auth_generation=bound.identity.generation,
                vacancy_id=vacancy_id, resume_id=resume_id, message=message, summary=summary,
            )
        return draft_view(draft)

    async def submit_from_cli(self, draft_id: str) -> dict[str, object]:
        async with self.auth.bound_user(self._refresh) as bound:
            draft = self.state.get_draft(draft_id)
            await require_owned_resume(self.client, bound.token, draft.resume_id)
            vacancy = await self.client.get_vacancy(draft.vacancy_id, bound.token)
            _validate_vacancy_for_application(vacancy, draft.message)
            negotiations = await self.client.list_negotiations(
                bound.token, page=0, per_page=20, status=None, vacancy_id=draft.vacancy_id
            )
            if _contains_vacancy(negotiations, draft.vacancy_id):
                raise StateConflictError("HH history now contains an application for this vacancy")
            operation = self.state.reserve_target(
                draft_id=draft_id, account_id=str(bound.identity.account_id),
                current_generation=bound.identity.generation,
            )
            try:
                status, location, payload = await self.client.submit_negotiation(
                    bound.token, vacancy_id=draft.vacancy_id,
                    resume_id=draft.resume_id, message=draft.message,
                )
            except HHMCPError as exc:
                terminal = "failed" if exc.status_code in {400, 403} else "unknown"
                self.state.finish_operation(operation.id, terminal, {"error": exc.kind})
                raise
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                self.state.finish_operation(operation.id, "unknown", {"error": "transport"})
                raise HHMCPError(
                    "application_result_unknown",
                    "The connection failed after submission may have started; do not retry this vacancy",
                ) from exc
        if status == 201:
            self.state.finish_operation(operation.id, "sent", {"location": location})
            return {"ok": True, "status": "sent", "location": location}
        if status == 303:
            self.state.finish_operation(operation.id, "failed", {"location": location})
            return {
                "ok": False,
                "status": "external_action_required",
                "location": location,
                "message": "HH requires completing this response on the employer site; no redirect was followed.",
            }
        self.state.finish_operation(operation.id, "unknown", {"http_status": status})
        raise HHMCPError(
            "application_result_unknown",
            f"Unexpected HTTP {status}; do not retry this vacancy until manually reconciled",
        )

    async def status(self, draft_id: str) -> dict[str, object]:
        draft = self.state.get_draft(draft_id)
        operation = self.state.operation_for_draft(draft_id)
        result: dict[str, object] = {"draft": draft_view(draft), "operation": operation}
        if operation and operation["status"] in {"sending", "unknown"}:
            async with self.auth.bound_user(self._refresh) as bound:
                if bound.identity.account_id != draft.account_id:
                    raise StateConflictError("Draft belongs to another HH account")
                history = await self.client.list_negotiations(
                    bound.token, page=0, per_page=20, status=None, vacancy_id=draft.vacancy_id
                )
            result["history_contains_vacancy"] = _contains_vacancy(history, draft.vacancy_id)
            result["reconciliation_note"] = (
                "A matching vacancy in HH history does not prove delivery of this exact saved letter; "
                "local unknown/sending remains blocked."
            )
        return result

    async def _refresh(self, refresh_token: str) -> dict[str, object]:
        return await self.client.token_request(
            {"grant_type": "refresh_token", "refresh_token": refresh_token}
        )


def draft_view(draft: Draft) -> dict[str, object]:
    return {
        "ok": True,
        "draft_id": draft.id,
        "account_id": draft.account_id,
        "vacancy_id": draft.vacancy_id,
        "resume_id": draft.resume_id,
        "message": draft.message,
        "message_sha256": draft.message_hash,
        "summary": draft.summary,
        "status": draft.status,
        "expires_at": draft.expires_at.isoformat(),
        "submission": "MCP submission is disabled; use the manual CLI only after reviewing this exact content.",
    }


def _contains_vacancy(value: object, vacancy_id: str) -> bool:
    if isinstance(value, dict):
        vacancy = value.get("vacancy")
        if isinstance(vacancy, dict) and str(vacancy.get("id")) == vacancy_id:
            return True
        return any(_contains_vacancy(child, vacancy_id) for child in value.values())
    if isinstance(value, list):
        return any(_contains_vacancy(child, vacancy_id) for child in value)
    return False


async def require_owned_resume(client: HHClient, token: str, resume_id: str) -> dict[str, object]:
    page = 0
    while True:
        listing = await client.list_my_resumes(token, page=page, per_page=100)
        items = listing.get("items")
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict) and str(item.get("id")) == resume_id:
                    return await client.get_resume(resume_id, token, include_contacts=False)
        pages = listing.get("pages")
        if not isinstance(pages, int) or page + 1 >= pages:
            break
        page += 1
    raise StateConflictError("The requested resume is not owned by the current HH account")


def _validate_vacancy_for_application(vacancy: dict[str, object], message: str) -> None:
    if bool(vacancy.get("archived")):
        raise StateConflictError("The vacancy is archived")
    vacancy_type = vacancy.get("type")
    if isinstance(vacancy_type, dict) and vacancy_type.get("id") == "direct":
        raise StateConflictError("This direct vacancy requires a manual employer-site response")
    if vacancy.get("has_test"):
        raise StateConflictError("Vacancies with a required test must be completed manually on HH")
    if bool(vacancy.get("response_letter_required")) and not message.strip():
        raise ValidationError("This vacancy requires a cover letter")
