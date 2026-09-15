from __future__ import annotations

import asyncio
import atexit
import base64
import hashlib
import json
import math
import time
from contextlib import asynccontextmanager
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .browser import BrowserAdapter
from .cli import data_path
from .exporting import export_rows
from .lock import CollectorBusy, CollectorLock
from .models import CandidateProfile, SearchSpec
from .ranking import rank
from .service import Collector
from .stats import calculate

collector = Collector(data_path())
tasks: dict[str, asyncio.Task[None]] = {}
reservations: dict[str, CollectorLock] = {}
queued_runs: list[tuple[str, bool]] = []
queue_dispatcher: asyncio.Task[None] | None = None


def _mark_unfinished_interrupted() -> None:
    for run_id, task in list(tasks.items()):
        if not task.done():
            collector.repo.set_run_state(run_id, "interrupted", "MCP server stopped", False)
            if run_id in reservations:
                reservations[run_id].release()


atexit.register(_mark_unfinished_interrupted)


@asynccontextmanager
async def server_lifespan(_server):
    for run_id in collector.repo.queued_run_ids():
        if not any(item[0] == run_id for item in queued_runs):
            queued_runs.append((run_id, False))
    _ensure_queue_dispatcher()
    try:
        yield {}
    finally:
        global queue_dispatcher
        if queue_dispatcher and not queue_dispatcher.done():
            queue_dispatcher.cancel()
            await asyncio.gather(queue_dispatcher, return_exceptions=True)
        queue_dispatcher = None
        running = [(run_id, task) for run_id, task in tasks.items() if not task.done()]
        for _, task in running:
            task.cancel()
        if running:
            await asyncio.gather(*(task for _, task in running), return_exceptions=True)
        for run_id, _ in running:
            if collector.repo.get_run(run_id).state != "cancelled":
                collector.repo.set_run_state(run_id, "interrupted", "MCP server stopped", False)
            reservations.pop(run_id, None)


mcp = FastMCP("hhmcp", lifespan=server_lifespan)


def _reserve_collector() -> CollectorLock:
    if any(not task.done() for task in tasks.values()):
        raise CollectorBusy("collector_busy")
    lock = CollectorLock(data_path() / "collector.lock")
    lock.acquire()
    return lock


async def _run_collection(
    run_id: str,
    refresh: bool,
    lock: CollectorLock,
) -> None:
    try:
        await collector._collect_locked(run_id, refresh=refresh)
    except asyncio.CancelledError:
        if collector.repo.get_run(run_id).state != "cancelled":
            collector.repo.set_run_state(run_id, "interrupted", "MCP server stopped", False)
            collector.finish_progress(run_id)
        raise
    except Exception as exc:
        collector.repo.set_run_state(run_id, "failed", str(exc), False)
        collector.finish_progress(run_id)
        raise
    finally:
        lock.release()
        reservations.pop(run_id, None)


def _consume_task_result(run_id: str, task: asyncio.Task[None]) -> None:
    try:
        if not task.cancelled():
            task.exception()
    finally:
        if tasks.get(run_id) is task:
            tasks.pop(run_id, None)
        if queued_runs:
            _ensure_queue_dispatcher()


def _queue_run(run_id: str, refresh: bool) -> int:
    if not any(item[0] == run_id for item in queued_runs):
        queued_runs.append((run_id, refresh))
    _ensure_queue_dispatcher()
    return next(index for index, item in enumerate(queued_runs, 1) if item[0] == run_id)


def _ensure_queue_dispatcher() -> None:
    global queue_dispatcher
    if queued_runs and (queue_dispatcher is None or queue_dispatcher.done()):
        queue_dispatcher = asyncio.create_task(_dispatch_queue())


async def _dispatch_queue() -> None:
    global queue_dispatcher
    try:
        while queued_runs:
            if any(not task.done() for task in tasks.values()):
                await asyncio.sleep(0.2)
                continue
            run_id, refresh = queued_runs[0]
            if collector.repo.get_run(run_id).state == "cancelled":
                queued_runs.pop(0)
                continue
            try:
                lock = _reserve_collector()
            except CollectorBusy:
                await asyncio.sleep(1)
                continue
            queued_runs.pop(0)
            reservations[run_id] = lock
            _schedule_collection(run_id, refresh, lock)
            await asyncio.gather(tasks[run_id], return_exceptions=True)
    finally:
        queue_dispatcher = None


def _schedule_collection(run_id: str, refresh: bool, lock: CollectorLock) -> None:
    task = asyncio.create_task(_run_collection(run_id, refresh, lock))
    tasks[run_id] = task
    task.add_done_callback(lambda completed: _consume_task_result(run_id, completed))


def _validate_collection_settings(
    vacancy_cache_ttl_hours: int | None,
    navigation_interval_seconds: float | None,
) -> None:
    if vacancy_cache_ttl_hours is not None and vacancy_cache_ttl_hours < 0:
        raise ValueError("vacancy_cache_ttl_hours must be non-negative")
    if navigation_interval_seconds is not None and (
        not math.isfinite(navigation_interval_seconds) or navigation_interval_seconds < 1
    ):
        raise ValueError("navigation_interval_seconds must be at least 1")


COMPACT_FIELDS = {
    "hh_id",
    "url",
    "title",
    "employer_name",
    "published_at",
    "published_text",
    "salary",
    "salary_text",
    "work_format",
    "conditions",
    "address",
    "summary",
    "availability",
    "outcome",
    "discovered_at",
    "period_match",
}


def _project(data: dict, fields: list[str]) -> dict:
    result = {}
    for field in fields:
        if field in data:
            result[field] = data[field]
    return result


def _vacancy_view(data: dict, view: str, fields: list[str] | None = None) -> dict:
    if view not in {"compact", "assessment", "full", "raw"}:
        raise ValueError("view must be compact, assessment, full, or raw")
    value = dict(data)
    if view != "raw" and isinstance(value.get("field_states"), dict):
        value["field_states"] = {
            key: ({**state, "value": None} if key == "description" else state)
            for key, state in value["field_states"].items()
        }
    if view in {"compact", "assessment"}:
        value.pop("description", None)
        value.pop("field_states", None)
        value.pop("contacts", None)
        value.pop("metro", None)
        value.pop("cache_age_seconds", None)
        value.pop("parser_version", None)
        if view == "compact":
            value.pop("assessment", None)
            value.pop("diagnostics", None)
            value = {key: item for key, item in value.items() if key in COMPACT_FIELDS}
    if fields is not None:
        unknown = set(fields) - set(value)
        if unknown:
            raise ValueError(f"unknown or unavailable fields: {', '.join(sorted(unknown))}")
        value = _project(value, fields)
    return value


@mcp.tool()
async def start_collection(
    searches: list[dict],
    limit: int = 1000,
    refresh: bool = False,
    vacancy_cache_ttl_hours: int = 24,
    navigation_interval_seconds: float = 1.0,
    fetch_details: str = "all",
    shortlist_size: int = 30,
    per_search_limit: int | None = None,
    global_limit: int | None = None,
    collection_strategy: str = "round_robin",
    timezone: str = "Europe/Moscow",
) -> dict:
    """Start a background collection and immediately return its run id.

    ``vacancy_cache_ttl_hours=0`` disables detail reuse. Navigation starts are
    globally spaced by at least ``navigation_interval_seconds`` (minimum 1).
    """
    _validate_collection_settings(vacancy_cache_ttl_hours, navigation_interval_seconds)
    specs = [SearchSpec.model_validate(x) for x in searches]
    if global_limit is not None:
        limit = global_limit
    active_run_id = next((key for key, task in tasks.items() if not task.done()), None)
    if active_run_id:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
        position = _queue_run(run_id, refresh)
        return {
            "run_id": run_id,
            "state": "queued",
            "position": position,
            "active_run_id": active_run_id,
        }
    try:
        lock = _reserve_collector()
    except CollectorBusy:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
        position = _queue_run(run_id, refresh)
        return {"run_id": run_id, "state": "queued", "position": position}
    try:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
    except Exception:
        lock.release()
        raise
    reservations[run_id] = lock
    _schedule_collection(run_id, refresh, lock)
    await asyncio.sleep(0)
    return {"run_id": run_id, "state": "queued"}


@mcp.tool()
def collection_status(run_id: str, verbose: bool = False) -> dict:
    run = collector.repo.get_run(run_id)
    if verbose:
        return run.model_dump(mode="json") | {"searches": collector.repo.run_search_stats(run_id)}
    return {
        "run_id": run.id,
        "state": run.state,
        "discovered": run.discovered,
        "loaded": run.loaded,
        "cached": run.cached,
        "pending": run.progress.pending_details,
        "errors": run.errors,
        "revision": run.revision,
        "complete": run.complete,
        "stop_reason": run.stop_reason,
    }


@mcp.tool()
async def wait_collection(run_id: str, after_revision: int = 0, timeout_ms: int = 30000) -> dict:
    if not 0 <= timeout_ms <= 60000:
        raise ValueError("timeout_ms must be between 0 and 60000")
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        status = collection_status(run_id)
        if status["revision"] > after_revision or status["state"] in {
            "completed",
            "failed",
            "cancelled",
            "paused",
            "interrupted",
        }:
            return status
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return status | {"timed_out": True}
        await asyncio.sleep(min(0.2, remaining))


@mcp.tool()
async def cancel_collection(run_id: str) -> dict:
    pending = collector.cancel(run_id)
    queued_runs[:] = [item for item in queued_runs if item[0] != run_id]
    task = tasks.get(run_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {
        "run_id": run_id,
        "state": "cancelled",
        "cancelled_jobs": pending,
        "unfinished": pending,
        "loaded": collector.repo.get_run(run_id).loaded,
    }


@mcp.tool()
async def resume_collection(
    run_id: str,
    refresh: bool = False,
    vacancy_cache_ttl_hours: int | None = None,
    navigation_interval_seconds: float | None = None,
) -> dict:
    """Resume an incomplete run, optionally replacing its persisted collection settings."""
    _validate_collection_settings(vacancy_cache_ttl_hours, navigation_interval_seconds)
    run = collector.repo.get_run(run_id)
    if run.state == "completed" and run.complete:
        raise ValueError(f"run cannot be resumed from {run.state}")
    active_run_id = next((key for key, task in tasks.items() if not task.done()), None)
    if active_run_id:
        collector.repo.update_run_settings(
            run_id,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
        )
        collector.cancelled.discard(run_id)
        position = _queue_run(run_id, refresh)
        return {
            "run_id": run_id,
            "state": "queued",
            "position": position,
            "active_run_id": active_run_id,
        }
    try:
        lock = _reserve_collector()
    except CollectorBusy:
        collector.repo.update_run_settings(
            run_id,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
        )
        collector.cancelled.discard(run_id)
        position = _queue_run(run_id, refresh)
        return {"run_id": run_id, "state": "queued", "position": position}
    try:
        collector.repo.update_run_settings(
            run_id,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
        )
        collector.cancelled.discard(run_id)
    except Exception:
        lock.release()
        raise
    reservations[run_id] = lock
    _schedule_collection(run_id, refresh, lock)
    await asyncio.sleep(0)
    return {"run_id": run_id, "state": "queued"}


@mcp.tool()
def list_vacancies(
    query: str | None = None,
    status: str | None = None,
    favorite: bool | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> list[dict]:
    rows = collector.repo.list_vacancies(
        query=query,
        status=status,
        favorite=favorite,
        tags=tags,
        employer_id=employer_id,
        work_format=work_format,
        limit=limit,
        offset=offset,
    )
    for row in rows:
        row.pop("description", None)
    return rows


@mcp.tool()
def collection_results(
    run_id: str,
    limit: int = 20,
    offset: int = 0,
    view: str = "compact",
    fields: list[str] | None = None,
) -> list[dict]:
    return [
        _vacancy_view(value, view, fields)
        for value in collector.repo.run_results(run_id, limit, offset)
    ]


@mcp.tool()
def get_vacancy(vacancy_id: str, view: str = "full", fields: list[str] | None = None) -> dict:
    vacancy, local = collector.repo.get_vacancy(vacancy_id)
    local_fields = {key: local[key] for key in ("local_status", "favorite", "notes", "tags_json")}
    return {
        "vacancy": _vacancy_view(vacancy.model_dump(mode="json"), view, fields),
        "local": local_fields,
    }


@mcp.tool()
def get_vacancies(
    vacancy_ids: list[str], view: str = "assessment", fields: list[str] | None = None
) -> list[dict]:
    if not 1 <= len(vacancy_ids) <= 100:
        raise ValueError("vacancy_ids must contain between 1 and 100 ids")
    return [get_vacancy(vacancy_id, view, fields) for vacancy_id in vacancy_ids]


@mcp.tool()
def update_vacancy(
    vacancy_id: str,
    status: str | None = None,
    favorite: bool | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    collector.repo.update_local(
        vacancy_id, status=status, favorite=favorite, notes=notes, tags=tags
    )
    return {"updated": vacancy_id}


@mcp.tool()
def exclude_employer(employer_id: str, excluded: bool = True) -> dict:
    collector.repo.exclude_employer(employer_id, excluded)
    return {"employer_id": employer_id, "excluded": excluded}


@mcp.tool()
def get_employer(employer_id: str) -> dict:
    with collector.repo.db.connect() as con:
        row = con.execute(
            "SELECT data_json,fetched_at FROM employers WHERE hh_id=?", (employer_id,)
        ).fetchone()
    if not row:
        raise KeyError(employer_id)
    return json.loads(row["data_json"]) | {"cache_fetched_at": row["fetched_at"]}


@mcp.tool()
async def refresh_vacancy(vacancy_id: str) -> dict:
    lock = _reserve_collector()
    try:
        async with BrowserAdapter() as browser:
            vacancy, _ = await collector._load_vacancy(
                browser, vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", True
            )
    finally:
        lock.release()
    return vacancy.model_dump(mode="json")


@mcp.tool()
async def refresh_employer(employer_id: str) -> dict:
    lock = _reserve_collector()
    try:
        async with BrowserAdapter() as browser:
            await collector._load_employer(
                browser, employer_id, f"https://hh.ru/employer/{employer_id}", True
            )
    finally:
        lock.release()
    return get_employer(employer_id)


@mcp.tool()
def save_search(name: str, search: dict) -> dict:
    spec = SearchSpec.model_validate(search)
    with collector.repo.db.transaction() as con:
        con.execute(
            """INSERT INTO saved_searches VALUES(?,?,datetime('now'))
               ON CONFLICT(name) DO UPDATE SET spec_json=excluded.spec_json""",
            (name, spec.model_dump_json()),
        )
    return {"saved": name}


@mcp.tool()
def list_saved_searches() -> list[dict]:
    with collector.repo.db.connect() as con:
        return [
            {"name": row[0], "search": json.loads(row[1])}
            for row in con.execute("SELECT name,spec_json FROM saved_searches ORDER BY name")
        ]


@mcp.tool()
def delete_saved_search(name: str) -> dict:
    with collector.repo.db.transaction() as con:
        con.execute("DELETE FROM saved_searches WHERE name=?", (name,))
    return {"deleted": name}


@mcp.tool()
async def run_saved_searches(
    names: list[str],
    limit: int = 1000,
    refresh: bool = False,
    vacancy_cache_ttl_hours: int = 24,
    navigation_interval_seconds: float = 1.0,
    fetch_details: str = "all",
    shortlist_size: int = 30,
    per_search_limit: int | None = None,
    global_limit: int | None = None,
    collection_strategy: str = "round_robin",
    timezone: str = "Europe/Moscow",
) -> dict:
    """Run saved searches with persisted cache and navigation settings."""
    _validate_collection_settings(vacancy_cache_ttl_hours, navigation_interval_seconds)
    with collector.repo.db.connect() as con:
        rows = [
            con.execute("SELECT spec_json FROM saved_searches WHERE name=?", (name,)).fetchone()
            for name in names
        ]
    if any(row is None for row in rows):
        raise KeyError("saved search not found")
    specs = [SearchSpec.model_validate_json(row[0]) for row in rows if row]
    if global_limit is not None:
        limit = global_limit
    active_run_id = next((key for key, task in tasks.items() if not task.done()), None)
    if active_run_id:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
        position = _queue_run(run_id, refresh)
        return {
            "run_id": run_id,
            "state": "queued",
            "position": position,
            "active_run_id": active_run_id,
        }
    try:
        lock = _reserve_collector()
    except CollectorBusy:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
        position = _queue_run(run_id, refresh)
        return {"run_id": run_id, "state": "queued", "position": position}
    try:
        run_id = collector.start(
            specs,
            limit,
            vacancy_cache_ttl_hours=vacancy_cache_ttl_hours,
            navigation_interval_seconds=navigation_interval_seconds,
            per_search_limit=per_search_limit,
            collection_strategy=collection_strategy,
            fetch_details=fetch_details,
            shortlist_size=shortlist_size,
            timezone=timezone,
        )
    except Exception:
        lock.release()
        raise
    reservations[run_id] = lock
    _schedule_collection(run_id, refresh, lock)
    await asyncio.sleep(0)
    return {"run_id": run_id, "state": "queued"}


@mcp.tool()
def save_profile(profile: dict) -> dict:
    value = CandidateProfile.model_validate(profile)
    collector.repo.save_profile(value)
    return value.model_dump(mode="json")


@mcp.tool()
def get_profile(profile_id: str) -> dict:
    return collector.repo.get_profile(profile_id).model_dump(mode="json")


@mcp.tool()
def list_profiles() -> list[dict]:
    with collector.repo.db.connect() as con:
        return [json.loads(row[0]) for row in con.execute("SELECT data_json FROM profiles")]


@mcp.tool()
def rank_vacancy(vacancy_id: str, profile_id: str) -> dict:
    vacancy, _ = collector.repo.get_vacancy(vacancy_id)
    return rank(vacancy, collector.repo.get_profile(profile_id)).model_dump(mode="json")


@mcp.tool()
def rank_vacancies(
    profile_id: str,
    vacancy_ids: list[str],
    top_k: int | None = None,
    explain: bool = True,
) -> list[dict]:
    if not 1 <= len(vacancy_ids) <= 1000:
        raise ValueError("vacancy_ids must contain between 1 and 1000 ids")
    profile = collector.repo.get_profile(profile_id)
    results = []
    for vacancy_id in dict.fromkeys(vacancy_ids):
        try:
            vacancy, _ = collector.repo.get_vacancy(vacancy_id)
        except KeyError:
            results.append(
                {
                    "vacancy_id": vacancy_id,
                    "eligibility": "недостаточно данных",
                    "score": 0.0,
                    "completeness": 0.0,
                    "criteria": [] if explain else None,
                    "error": "vacancy details not loaded",
                }
            )
            continue
        result = rank(vacancy, profile).model_dump(mode="json")
        if not explain:
            result.pop("criteria", None)
        results.append(result)
    eligibility_order = {"подходит": 0, "недостаточно данных": 1, "не подходит": 2}
    results.sort(
        key=lambda item: (
            eligibility_order.get(str(item["eligibility"]), 3),
            -float(item["score"]),
            -float(item["completeness"]),
            str(item["vacancy_id"]),
        )
    )
    return results[:top_k] if top_k is not None else results


@mcp.tool()
def recommend_vacancies(
    profile_id: str,
    run_ids: list[str],
    limit: int = 10,
    cursor: str | None = None,
    exclude_vacancy_ids: list[str] | None = None,
    categories: list[str] | None = None,
) -> dict:
    if not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    excluded = set(exclude_vacancy_ids or [])
    requested = set(
        categories or {"confirmed_remote", "other_formats", "insufficient_data", "rejected"}
    )
    allowed_categories = {
        "confirmed_remote",
        "other_formats",
        "insufficient_data",
        "rejected",
    }
    if not requested <= allowed_categories:
        raise ValueError("unsupported recommendation category")
    fingerprint = hashlib.sha256(
        json.dumps(
            {
                "profile_id": profile_id,
                "run_ids": run_ids,
                "exclude_vacancy_ids": sorted(excluded),
                "categories": sorted(requested),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()

    offset = 0
    snapshot_id = None
    ranked: list[dict]
    if cursor:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
            snapshot_id = str(payload["snapshot"])
            offset = int(payload["offset"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ValueError("invalid cursor") from exc
        stored_fingerprint, ranked = collector.repo.get_recommendation_snapshot(snapshot_id)
        if stored_fingerprint != fingerprint:
            raise ValueError("cursor does not match recommendation parameters")
    else:
        rows: list[dict] = []
        for run_id in run_ids:
            page_offset = 0
            while True:
                page = collector.repo.run_results(run_id, 1000, page_offset)
                rows.extend(item for item in page if item.get("hh_id") not in excluded)
                if len(page) < 1000:
                    break
                page_offset += len(page)
        metadata = {item["hh_id"]: item for item in rows}
        ordered_ids = list(dict.fromkeys(item["hh_id"] for item in rows))
        ranked = rank_vacancies(profile_id, ordered_ids, explain=True) if ordered_ids else []
        ranked = [
            item
            | {
                key: metadata[item["vacancy_id"]].get(key)
                for key in ("title", "url", "employer_name", "published_at", "period_match")
                if metadata[item["vacancy_id"]].get(key) is not None
            }
            for item in ranked
        ]

    def category(item: dict) -> str:
        if item["eligibility"] == "не подходит":
            return "rejected"
        if item["eligibility"] == "недостаточно данных":
            return "insufficient_data"
        vacancy, _ = collector.repo.get_vacancy(item["vacancy_id"])
        work = vacancy.assessment.get("work_format") or {}
        if work.get("current") == "remote" and work.get("permanent") is True:
            return "confirmed_remote"
        return "other_formats"

    if snapshot_id is None:
        ranked = [item | {"category": category(item)} for item in ranked]
        ranked = [item for item in ranked if item["category"] in requested]
        snapshot_id = collector.repo.save_recommendation_snapshot(fingerprint, ranked)
    page = ranked[offset : offset + limit]
    next_offset = offset + len(page)
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps({"snapshot": snapshot_id, "offset": next_offset}).encode()
        ).decode()
        if next_offset < len(ranked)
        else None
    )
    groups = {
        name: [item for item in page if item["category"] == name]
        for name in ("confirmed_remote", "other_formats", "insufficient_data", "rejected")
        if any(item["category"] == name for item in page)
    }
    return {"items": page, "groups": groups, "next_cursor": next_cursor, "total": len(ranked)}


@mcp.tool()
def vacancy_history(vacancy_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    return collector.repo.history(vacancy_id, limit=limit, offset=offset)


@mcp.tool()
def compare_runs(current_run_id: str, previous_run_id: str) -> dict:
    return collector.repo.compare_runs(current_run_id, previous_run_id)


@mcp.tool()
async def similar_vacancies(vacancy_id: str, threshold: float = 0.72) -> list[dict]:
    return await asyncio.to_thread(collector.repo.find_similar, vacancy_id, threshold)


@mcp.tool()
async def library_stats(
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
) -> dict:
    def load() -> dict:
        return calculate(
            collector.repo.list_vacancies(
                query=query,
                status=status,
                tags=tags,
                employer_id=employer_id,
                work_format=work_format,
                limit=None,
            )
        )

    return await asyncio.to_thread(load)


@mcp.tool()
async def export_library(
    path: str,
    format: str = "json",
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
) -> dict:
    destination = Path(path).resolve()

    def export() -> int:
        rows = collector.repo.list_vacancies(
            query=query,
            status=status,
            tags=tags,
            employer_id=employer_id,
            work_format=work_format,
            limit=None,
        )
        export_rows(rows, destination, format)
        return len(rows)

    count = await asyncio.to_thread(export)
    return {"path": str(destination), "count": count}


def main() -> None:
    mcp.run(transport="stdio")
