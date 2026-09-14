from __future__ import annotations

import asyncio
import atexit
import json
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


def _mark_unfinished_interrupted() -> None:
    for run_id, task in tasks.items():
        if not task.done():
            collector.repo.set_run_state(run_id, "interrupted", "MCP server stopped", False)
            if run_id in reservations:
                reservations[run_id].release()


atexit.register(_mark_unfinished_interrupted)


@asynccontextmanager
async def server_lifespan(_server):
    try:
        yield {}
    finally:
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


async def _run_collection(run_id: str, refresh: bool, lock: CollectorLock) -> None:
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


def _consume_task_result(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()


@mcp.tool()
async def start_collection(searches: list[dict], limit: int = 1000, refresh: bool = False) -> dict:
    """Start a background collection and immediately return its run id."""
    specs = [SearchSpec.model_validate(x) for x in searches]
    lock = _reserve_collector()
    try:
        run_id = collector.start(specs, limit)
    except Exception:
        lock.release()
        raise
    reservations[run_id] = lock
    tasks[run_id] = asyncio.create_task(_run_collection(run_id, refresh, lock))
    tasks[run_id].add_done_callback(_consume_task_result)
    await asyncio.sleep(0)
    return {"run_id": run_id, "state": "queued"}


@mcp.tool()
def collection_status(run_id: str) -> dict:
    return collector.repo.get_run(run_id).model_dump(mode="json")


@mcp.tool()
async def cancel_collection(run_id: str) -> dict:
    collector.cancel(run_id)
    task = tasks.get(run_id)
    if task:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    return {"run_id": run_id, "state": "cancelled"}


@mcp.tool()
async def resume_collection(run_id: str, refresh: bool = False) -> dict:
    run = collector.repo.get_run(run_id)
    if run.state == "completed" and run.complete:
        raise ValueError(f"run cannot be resumed from {run.state}")
    lock = _reserve_collector()
    collector.cancelled.discard(run_id)
    reservations[run_id] = lock
    tasks[run_id] = asyncio.create_task(_run_collection(run_id, refresh, lock))
    tasks[run_id].add_done_callback(_consume_task_result)
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
def collection_results(run_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
    return collector.repo.run_results(run_id, limit, offset)


@mcp.tool()
def get_vacancy(vacancy_id: str) -> dict:
    vacancy, local = collector.repo.get_vacancy(vacancy_id)
    local_fields = {key: local[key] for key in ("local_status", "favorite", "notes", "tags_json")}
    return {"vacancy": vacancy.model_dump(mode="json"), "local": local_fields}


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
async def run_saved_searches(names: list[str], limit: int = 1000, refresh: bool = False) -> dict:
    lock = _reserve_collector()
    with collector.repo.db.connect() as con:
        rows = [
            con.execute("SELECT spec_json FROM saved_searches WHERE name=?", (name,)).fetchone()
            for name in names
        ]
    if any(row is None for row in rows):
        lock.release()
        raise KeyError("saved search not found")
    specs = [SearchSpec.model_validate_json(row[0]) for row in rows if row]
    run_id = collector.start(specs, limit)
    reservations[run_id] = lock
    tasks[run_id] = asyncio.create_task(_run_collection(run_id, refresh, lock))
    tasks[run_id].add_done_callback(_consume_task_result)
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
def vacancy_history(vacancy_id: str) -> list[dict]:
    return collector.repo.history(vacancy_id)


@mcp.tool()
def compare_runs(current_run_id: str, previous_run_id: str) -> dict:
    return collector.repo.compare_runs(current_run_id, previous_run_id)


@mcp.tool()
def similar_vacancies(vacancy_id: str, threshold: float = 0.72) -> list[dict]:
    return collector.repo.find_similar(vacancy_id, threshold)


@mcp.tool()
def library_stats(
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
) -> dict:
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


@mcp.tool()
def export_library(
    path: str,
    format: str = "json",
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
) -> dict:
    destination = Path(path).resolve()
    rows = collector.repo.list_vacancies(
        query=query,
        status=status,
        tags=tags,
        employer_id=employer_id,
        work_format=work_format,
        limit=None,
    )
    export_rows(rows, destination, format)
    return {"path": str(destination), "count": len(rows)}


def main() -> None:
    mcp.run(transport="stdio")
