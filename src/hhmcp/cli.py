from __future__ import annotations

import asyncio
import json
import os
import platform
import sqlite3
from pathlib import Path
from typing import Annotated

import typer

from .db import Database
from .exporting import export_rows
from .models import CandidateProfile, SearchSpec
from .ranking import rank as rank_vacancy
from .repository import Repository
from .service import Collector
from .stats import calculate

app = typer.Typer(no_args_is_help=True)
saved_app = typer.Typer(no_args_is_help=True)
profile_app = typer.Typer(no_args_is_help=True)
app.add_typer(saved_app, name="saved-search")
app.add_typer(profile_app, name="profile")


def data_path() -> Path:
    return Path(os.environ.get("HHMCP_DATA_DIR", Path.home() / ".hhmcp"))


def repo() -> Repository:
    return Repository(Database(data_path() / "hhmcp.sqlite3"))


def emit(value, as_json: bool = False):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2, default=str) if as_json else value)


@app.command()
def search(
    url: str | None = None,
    text: str | None = None,
    area: list[str] | None = None,
    exclude: list[str] | None = None,
    salary: float | None = None,
    experience: str | None = None,
    employment: list[str] | None = None,
    schedule: list[str] | None = None,
    working_hours: list[str] | None = None,
    work_format: list[str] | None = None,
    period: int | None = None,
    order_by: str | None = None,
    limit: int = 1000,
    refresh: bool = False,
    visible: bool = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
):
    """Create and synchronously execute one search."""
    spec = SearchSpec(
        url=url,
        text=text,
        area=area or [],
        exclude=exclude or [],
        salary=salary,
        experience=experience,
        employment=employment or [],
        schedule=schedule or [],
        working_hours=working_hours or [],
        work_format=work_format or [],
        period=period,
        order_by=order_by,
    )
    collector = Collector(data_path(), headless=not visible)
    run_id = collector.start([spec], limit)
    asyncio.run(collector.collect(run_id, refresh=refresh))
    emit(collector.repo.get_run(run_id), json_output)


@app.command()
def vacancy(
    vacancy_id: str,
    refresh: bool = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
):
    if refresh:
        collector = Collector(data_path())
        from .browser import BrowserAdapter

        async def load() -> None:
            with CollectorLock(data_path() / "collector.lock"):
                async with BrowserAdapter() as browser:
                    await collector._load_vacancy(
                        browser, vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", True
                    )

        from .lock import CollectorLock

        asyncio.run(load())
    item, local = repo().get_vacancy(vacancy_id)
    emit({"vacancy": item.model_dump(mode="json"), "local": local}, json_output)


@app.command()
def employer(
    employer_id: str,
    refresh: bool = False,
    exclude: bool = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
):
    if exclude:
        repo().exclude_employer(employer_id)
    if refresh:
        collector = Collector(data_path())
        from .browser import BrowserAdapter
        from .lock import CollectorLock

        async def load() -> None:
            with CollectorLock(data_path() / "collector.lock"):
                async with BrowserAdapter() as browser:
                    await collector._load_employer(
                        browser, employer_id, f"https://hh.ru/employer/{employer_id}", True
                    )

        asyncio.run(load())
    with repo().db.connect() as con:
        row = con.execute(
            "SELECT data_json,fetched_at FROM employers WHERE hh_id=?", (employer_id,)
        ).fetchone()
    if not row:
        raise typer.BadParameter("employer not found")
    emit(json.loads(row[0]) | {"cache_fetched_at": row[1]}, json_output)


@saved_app.command("set")
def saved_set(name: str, spec_json: str):
    spec = SearchSpec.model_validate_json(spec_json)
    with repo().db.transaction() as con:
        con.execute(
            "INSERT INTO saved_searches VALUES(?,?,datetime('now')) ON CONFLICT(name) DO UPDATE SET spec_json=excluded.spec_json",
            (name, spec.model_dump_json()),
        )
    emit({"saved": name}, True)


@saved_app.command("list")
def saved_list():
    with repo().db.connect() as con:
        emit(
            [
                {"name": r[0], "spec": json.loads(r[1])}
                for r in con.execute("SELECT name,spec_json FROM saved_searches")
            ],
            True,
        )


@saved_app.command("delete")
def saved_delete(name: str):
    with repo().db.transaction() as con:
        con.execute("DELETE FROM saved_searches WHERE name=?", (name,))
    emit({"deleted": name}, True)


@app.command()
def run(names: list[str], limit: int = 1000, refresh: bool = False, visible: bool = False):
    with repo().db.connect() as con:
        rows = [
            con.execute("SELECT spec_json FROM saved_searches WHERE name=?", (n,)).fetchone()
            for n in names
        ]
    if any(r is None for r in rows):
        raise typer.BadParameter("saved search not found")
    collector = Collector(data_path(), headless=not visible)
    run_id = collector.start([SearchSpec.model_validate_json(r[0]) for r in rows if r], limit)
    asyncio.run(collector.collect(run_id, refresh=refresh))
    emit(collector.repo.get_run(run_id), True)


@app.command()
def library(
    query: str | None = None,
    status: str | None = None,
    favorite: bool | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
    limit: int = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
):
    emit(
        repo().list_vacancies(
            query=query,
            status=status,
            favorite=favorite,
            tags=tags,
            employer_id=employer_id,
            work_format=work_format,
            limit=limit,
        ),
        json_output,
    )


@app.command("library-update")
def library_update(
    vacancy_id: str,
    status: str | None = None,
    favorite: bool | None = None,
    notes: str | None = None,
    tags: list[str] | None = None,
):
    repo().update_local(vacancy_id, status=status, favorite=favorite, notes=notes, tags=tags)
    emit({"updated": vacancy_id}, True)


@profile_app.command("set")
def profile_set(profile_json: str):
    value = CandidateProfile.model_validate_json(profile_json)
    repo().save_profile(value)
    emit(value, True)


@profile_app.command("show")
def profile_show(profile_id: str):
    emit(repo().get_profile(profile_id), True)


@profile_app.command("list")
def profile_list():
    with repo().db.connect() as con:
        emit([json.loads(row[0]) for row in con.execute("SELECT data_json FROM profiles")], True)


@app.command("run-status")
def run_status(run_id: str):
    emit(repo().get_run(run_id), True)


@app.command("run-resume")
def run_resume(run_id: str, refresh: bool = False, visible: bool = False):
    collector = Collector(data_path(), headless=not visible)
    asyncio.run(collector.collect(run_id, refresh=refresh))
    emit(collector.repo.get_run(run_id), True)


@app.command("run-cancel")
def run_cancel(run_id: str):
    Collector(data_path()).cancel(run_id)
    emit({"run_id": run_id, "state": "cancelled"}, True)


@app.command("run-results")
def run_results(run_id: str, limit: int = 20, offset: int = 0):
    emit(repo().run_results(run_id, limit, offset), True)


@app.command("run-compare")
def run_compare(current_run_id: str, previous_run_id: str):
    emit(repo().compare_runs(current_run_id, previous_run_id), True)


@app.command("similar")
def similar(vacancy_id: str, threshold: float = 0.72):
    emit(repo().find_similar(vacancy_id, threshold), True)


@app.command()
def rank(vacancy_id: str, profile_id: str):
    vacancy_value, _ = repo().get_vacancy(vacancy_id)
    emit(rank_vacancy(vacancy_value, repo().get_profile(profile_id)), True)


@app.command()
def history(vacancy_id: str):
    emit(repo().history(vacancy_id), True)


@app.command()
def stats(
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
):
    rows = repo().list_vacancies(
        query=query,
        status=status,
        tags=tags,
        employer_id=employer_id,
        work_format=work_format,
        limit=None,
    )
    emit(calculate(rows), True)


@app.command("export")
def export_command(
    path: Path,
    format: str = "json",
    query: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    employer_id: str | None = None,
    work_format: str | None = None,
):
    rows = repo().list_vacancies(
        query=query,
        status=status,
        tags=tags,
        employer_id=employer_id,
        work_format=work_format,
        limit=None,
    )
    export_rows(rows, path, format)
    emit({"path": str(path), "count": len(rows)}, True)


@app.command()
def doctor():
    checks = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "fts5": False,
        "chromium": False,
        "data_dir": str(data_path()),
    }
    try:
        with Database(data_path() / "hhmcp.sqlite3").connect() as con:
            con.execute("CREATE VIRTUAL TABLE temp.check_fts USING fts5(value)")
        checks["fts5"] = True
    except Exception as exc:
        checks["fts5_error"] = str(exc)
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        checks["chromium"] = True
    except Exception as exc:
        checks["chromium_error"] = str(exc)
    emit(checks, True)
    if not checks["fts5"] or not checks["chromium"]:
        raise typer.Exit(1)


@app.command()
def mcp():
    from .mcp_server import main

    main()


if __name__ == "__main__":
    app()
