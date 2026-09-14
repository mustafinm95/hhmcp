from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .browser import BrowserAdapter, BrowserBlocked, PageNotReady
from .db import Database
from .lock import CollectorLock
from .models import (
    PARSER_VERSION,
    ActiveVacancy,
    Employer,
    FieldState,
    RunProgress,
    Salary,
    SearchProgress,
    SearchSpec,
    Vacancy,
)
from .parsing import parse_employer_page, parse_salary, parse_vacancy_page, validate_search_url
from .repository import Repository

DETAIL_TTL = timedelta(days=1)
EMPLOYER_TTL = timedelta(days=7)

FILTERS = {
    "experience": {"noExperience", "between1And3", "between3And6", "moreThan6"},
    "employment": {"full", "part", "project", "volunteer", "probation"},
    "schedule": {"fullDay", "shift", "flexible", "remote", "flyInFlyOut"},
    "work_format": {"ON_SITE", "REMOTE", "HYBRID"},
    "working_hours": {"HOURS_2", "HOURS_3", "HOURS_4", "HOURS_5", "HOURS_6", "HOURS_7", "HOURS_8"},
    "order_by": {"publication_time", "salary_desc", "salary_asc", "relevance"},
}


def search_url(spec: SearchSpec) -> str:
    if spec.url:
        return validate_search_url(spec.url)
    for name, values in FILTERS.items():
        actual = getattr(spec, name)
        if actual:
            values_to_check = actual if isinstance(actual, list) else [actual]
            invalid = set(values_to_check) - values
            if invalid:
                raise ValueError(f"unsupported {name}: {', '.join(sorted(invalid))}")
    if spec.period is not None and spec.period not in {1, 3, 7, 30}:
        raise ValueError("unsupported period")
    params: list[tuple[str, str]] = []
    if spec.text:
        params.append(("text", spec.text))
    for value in spec.exclude:
        params.append(("excluded_text", value))
    params += [("area", a) for a in spec.area]
    for name in ("employment", "schedule", "working_hours", "work_format"):
        params += [(name, x) for x in getattr(spec, name)]
    if spec.salary is not None:
        params.append(("salary", str(spec.salary)))
    if spec.experience:
        params.append(("experience", spec.experience))
    if spec.period:
        params.append(("period", str(spec.period)))
    if spec.order_by:
        params.append(("order_by", spec.order_by))
    return "https://hh.ru/search/vacancy?" + urlencode(params)


def _salary(text: str | None):
    parsed = parse_salary(text)
    if not parsed:
        return None
    from .models import Salary

    return Salary(
        lower=parsed.lower,
        upper=parsed.upper,
        currency=parsed.currency,
        period=parsed.period,
        gross=parsed.gross,
    )


class Collector:
    def __init__(self, data_dir: Path, *, headless: bool = True):
        self.data_dir = data_dir
        self.repo = Repository(Database(data_dir / "hhmcp.sqlite3"))
        self.headless = headless
        self.cancelled: set[str] = set()

    def start(self, specs: list[SearchSpec], limit: int = 1000) -> str:
        return self.repo.create_run(specs, limit).id

    def cancel(self, run_id: str) -> None:
        self.cancelled.add(run_id)
        self.repo.set_run_state(run_id, "cancelled", "cancelled by user", False)
        self.finish_progress(run_id)

    def finish_progress(self, run_id: str, *, paused: bool = False) -> None:
        progress = self.repo.get_run(run_id).progress
        progress.phase = "paused" if paused else "finished"
        progress.current_search = None
        progress.active_vacancies = []
        self.repo.set_progress(run_id, progress)

    async def collect(self, run_id: str, *, refresh: bool = False) -> None:
        lock = CollectorLock(self.data_dir / "collector.lock")
        try:
            with lock:
                await self._collect_locked(run_id, refresh=refresh)
        except Exception as exc:
            if self.repo.get_run(run_id).state not in ("paused", "cancelled"):
                self.repo.set_run_state(run_id, "failed", str(exc), False)
                self.finish_progress(run_id)
            raise

    async def _collect_locked(self, run_id: str, *, refresh: bool) -> None:
        run = self.repo.get_run(run_id)
        self.repo.set_run_state(run_id, "running")
        with self.repo.db.transaction() as con:
            con.execute(
                "UPDATE jobs SET state='queued' WHERE run_id=? AND state='running'", (run_id,)
            )
        with self.repo.db.connect() as con:
            checkpoints = con.execute(
                "SELECT position,final_url,complete FROM run_searches WHERE run_id=? ORDER BY position",
                (run_id,),
            ).fetchall()
            discovered_by_search = {
                row["search_position"]: row["count"]
                for row in con.execute(
                    """SELECT search_position,COUNT(*) AS count FROM run_vacancies
                       WHERE run_id=? GROUP BY search_position""",
                    (run_id,),
                )
            }
        urls = [
            row["final_url"] or search_url(spec)
            for row, spec in zip(checkpoints, run.search_specs, strict=True)
        ]
        searches = [
            SearchProgress(
                position=position,
                label=spec.text or spec.url or f"search {position + 1}",
                url=url,
                page=int(parse_qs(urlparse(url).query).get("page", ["0"])[0]) + 1,
                complete=bool(checkpoints[position]["complete"]),
                discovered=discovered_by_search.get(position, 0),
            )
            for position, (spec, url) in enumerate(zip(run.search_specs, urls, strict=True))
        ]
        progress = RunProgress(phase="discovering", searches=searches)
        progress_lock = asyncio.Lock()
        last_progress_write = 0.0
        self.repo.set_progress(run_id, progress)

        async def save_progress(*, force: bool = False) -> None:
            nonlocal last_progress_write
            async with progress_lock:
                progress.pending_details = queue.qsize()
                if not force and time.monotonic() - last_progress_write < 0.25:
                    return
                self.repo.set_progress(run_id, progress)
                last_progress_write = time.monotonic()

        page_iters = {}
        queue: asyncio.Queue = asyncio.Queue()
        stop_workers = asyncio.Event()
        accepted_count = run.accepted
        limit_reached = accepted_count >= run.limit
        async with BrowserAdapter(
            headless=self.headless, profile_dir=self.data_dir / "browser-profile"
        ) as browser:
            worker_browsers = []
            if hasattr(browser, "new_page_adapter"):
                worker_browsers = [await browser.new_page_adapter() for _ in range(3)]
            else:
                worker_browsers = [browser]

            async def worker(worker_browser) -> None:
                while True:
                    job = await queue.get()
                    if job is None:
                        queue.task_done()
                        return
                    if stop_workers.is_set():
                        queue.task_done()
                        return
                    payload = json.loads(job["payload_json"])
                    observed = payload.get("observed") or {}
                    active_vacancy = ActiveVacancy(
                        hh_id=job["target"], title=observed.get("title")
                    )
                    async with progress_lock:
                        if progress.current_search is None:
                            progress.phase = "loading_vacancies"
                        progress.active_vacancies.append(active_vacancy)
                        progress.pending_details = queue.qsize()
                    await save_progress()
                    try:
                        if not await self._process_job(
                            worker_browser, run_id, job, refresh
                        ):
                            stop_workers.set()
                    finally:
                        async with progress_lock:
                            progress.active_vacancies = [
                                item
                                for item in progress.active_vacancies
                                if item.hh_id != job["target"]
                            ]
                            progress.pending_details = queue.qsize()
                        await save_progress()
                        queue.task_done()

            workers = [asyncio.create_task(worker(value)) for value in worker_browsers]
            try:
                for job in self.repo.pending_jobs(run_id):
                    await queue.put(job)
                await save_progress()
                for pos, (url, spec, checkpoint) in enumerate(
                    zip(urls, run.search_specs, checkpoints, strict=True)
                ):
                    if checkpoint["complete"]:
                        continue
                    page_iters[pos] = self._iter_search(
                        browser, spec, url, bool(checkpoint["final_url"])
                    ).__aiter__()
                active = list(page_iters)
                while active and not limit_reached and not stop_workers.is_set():
                    for pos in list(active):
                        if run_id in self.cancelled or self.repo.get_run(run_id).state == "cancelled":
                            stop_workers.set()
                            break
                        progress.phase = "discovering"
                        progress.current_search = searches[pos]
                        await save_progress(force=True)
                        try:
                            page, final_url = await anext(page_iters[pos])
                        except StopAsyncIteration:
                            active.remove(pos)
                            searches[pos].complete = True
                            with self.repo.db.transaction() as con:
                                con.execute(
                                    "UPDATE run_searches SET complete=1 WHERE run_id=? AND position=?",
                                    (run_id, pos),
                                )
                            await save_progress(force=True)
                            continue
                        except BrowserBlocked as exc:
                            self.repo.set_run_state(run_id, "paused", str(exc), False)
                            progress.phase = "paused"
                            stop_workers.set()
                            await save_progress(force=True)
                            break
                        except PageNotReady as exc:
                            self.repo.set_run_state(run_id, "interrupted", str(exc), False)
                            stop_workers.set()
                            break
                        searches[pos].url = final_url
                        searches[pos].page = (
                            int(parse_qs(urlparse(final_url).query).get("page", ["0"])[0]) + 1
                        )
                        if page.unchecked_parameters:
                            missing = ", ".join(page.unchecked_parameters)
                            self.repo.set_run_state(
                                run_id,
                                "interrupted",
                                f"search filters were not retained: {missing}",
                                False,
                            )
                            stop_workers.set()
                            break
                        with self.repo.db.transaction() as con:
                            con.execute(
                                "UPDATE run_searches SET final_url=?,applied_filters_json=? WHERE run_id=? AND position=?",
                                (
                                    final_url,
                                    json.dumps(
                                        {
                                            "applied": page.applied_filters,
                                            "unchecked": page.unchecked_parameters,
                                        }
                                    ),
                                    run_id,
                                    pos,
                                ),
                            )
                        for item in page.items:
                            discovered, accepted, _ = self.repo.record_observation(
                                run_id,
                                pos,
                                item.hh_id,
                                item.url,
                                run.limit,
                                {
                                    "title": item.title,
                                    "employer_name": item.employer_name,
                                    "salary_text": item.salary_text,
                                    "published_text": item.published_text,
                                    "conditions": item.conditions,
                                },
                            )
                            if discovered:
                                searches[pos].discovered += 1
                            if accepted:
                                accepted_count += 1
                                await queue.put(self.repo.get_job(run_id, item.hh_id))
                            if accepted_count >= run.limit:
                                limit_reached = True
                                break
                        if page.next_url and not limit_reached:
                            searches[pos].url = page.next_url
                            searches[pos].page = (
                                int(
                                    parse_qs(urlparse(page.next_url).query).get("page", ["0"])[0]
                                )
                                + 1
                            )
                        await save_progress()
                        if limit_reached or stop_workers.is_set():
                            break

                if stop_workers.is_set():
                    progress.current_search = None
                    progress.active_vacancies = []
                    progress.phase = (
                        "paused" if self.repo.get_run(run_id).state == "paused" else "finished"
                    )
                    await save_progress(force=True)
                    return
                progress.current_search = None
                progress.phase = "loading_vacancies"
                await save_progress(force=True)
                join_task = asyncio.create_task(queue.join())
                stop_task = asyncio.create_task(stop_workers.wait())
                done, _ = await asyncio.wait(
                    {join_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in ({join_task, stop_task} - done):
                    task.cancel()
                if stop_workers.is_set():
                    progress.phase = "paused"
                    await save_progress(force=True)
                    return
                final = self.repo.get_run(run_id)
                if limit_reached:
                    self.repo.set_run_state(
                        run_id, "interrupted", "unique vacancy limit reached", False
                    )
                else:
                    self.repo.set_run_state(
                        run_id,
                        "completed",
                        "one or more vacancy details failed" if final.errors else None,
                        not bool(final.errors),
                    )
                progress.phase = "finished"
                progress.active_vacancies = []
                progress.pending_details = 0
                await save_progress(force=True)
            finally:
                for iterator in page_iters.values():
                    close = getattr(iterator, "aclose", None)
                    if close:
                        await close()
                for _ in workers:
                    await queue.put(None)
                await asyncio.gather(*workers, return_exceptions=True)
                for worker_browser in worker_browsers:
                    if worker_browser is not browser and hasattr(worker_browser, "close_page"):
                        await worker_browser.close_page()

    async def _iter_search(self, browser, spec: SearchSpec, url: str, resumed: bool):
        async for value in browser.iter_search_pages(url):
            yield value

    async def _process_job(self, browser, run_id: str, job, refresh: bool) -> bool:
        payload = json.loads(job["payload_json"])
        observed = payload.get("observed")
        effective_refresh = refresh
        if observed and not refresh:
            try:
                cached, _ = self.repo.get_vacancy(job["target"])
                effective_refresh = bool(
                    (observed.get("title") and observed["title"] != cached.title)
                    or (
                        observed.get("employer_name")
                        and observed["employer_name"] != cached.employer_name
                    )
                    or (
                        observed.get("salary_text")
                        and _salary(observed["salary_text"]) != cached.salary
                    )
                )
            except KeyError:
                pass
        self.repo.start_job(job["id"])
        try:
            _, from_cache = await self._load_vacancy(
                browser,
                job["target"],
                payload.get("url") or f"https://hh.ru/vacancy/{job['target']}",
                effective_refresh,
                job["id"],
            )
            if from_cache:
                self.repo.finish_job(job["id"], "cached")
            return True
        except BrowserBlocked as exc:
            self.repo.finish_job(job["id"], "queued", str(exc))
            self.repo.set_run_state(run_id, "paused", str(exc), False)
            return False
        except Exception as exc:
            self.repo.finish_job(job["id"], "error", str(exc))
            return True

    async def _load_vacancy(
        self,
        browser: BrowserAdapter,
        vacancy_id: str,
        url: str,
        refresh: bool,
        job_id: int | None = None,
    ):
        if not refresh:
            try:
                cached, meta = self.repo.get_vacancy(vacancy_id)
                age = datetime.now(UTC) - datetime.fromisoformat(meta["fetched_at"])
                if age <= DETAIL_TTL and meta["parser_version"] == PARSER_VERSION:
                    cached.cache_age_seconds = int(age.total_seconds())
                    return cached, True
            except KeyError:
                pass
        source, final_url = await browser.fetch_html(url, readiness="vacancy")
        observed = None
        if job_id is not None:
            with self.repo.db.connect() as con:
                row = con.execute("SELECT payload_json FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row:
                observed = json.loads(row[0]).get("observed")
        browser_availability = getattr(browser, "last_availability", None)
        if f"/vacancy/{vacancy_id}" not in final_url:
            browser_availability = "unavailable"
        parsed = parse_vacancy_page(
            source,
            final_url,
            availability=browser_availability,
            http_status=getattr(browser, "last_status", None),
            fallback_fields=observed,
            visible_fields=getattr(browser, "last_visible_fields", None),
        )
        terminal = parsed.availability in {"archived", "unavailable"}
        if (not parsed.title or not parsed.description_text) and not terminal:
            raise ValueError("vacancy title or description was not extracted")
        states = {
            name: FieldState(
                value=value.value, state=value.state, error=value.error, source=value.source
            )
            for name, value in parsed.field_states.items()
        }
        states["skills"] = FieldState(
            value=parsed.skills,
            state=(
                "value"
                if parsed.skills
                else ("absent" if "ключевые навыки" in source.casefold() else "error")
            ),
            error=None
            if parsed.skills or "ключевые навыки" in source.casefold()
            else "selector missing",
        )
        states["availability"] = FieldState(
            value=parsed.availability,
            state="value" if parsed.availability != "unknown" else "absent",
            source=(
                "http"
                if getattr(browser, "last_status", None) in {404, 410}
                else ("dom" if browser_availability else None)
            ),
        )
        if terminal:
            for name in ("title", "description"):
                if states.get(name) and states[name].state == "absent":
                    states[name] = FieldState(state="error", error="terminal page omitted field")
        salary = parsed.salary
        vacancy = Vacancy(
            hh_id=vacancy_id,
            url=final_url,
            title=parsed.title,
            description=parsed.description_text,
            skills=parsed.skills,
            salary=(
                Salary(
                    lower=salary.lower,
                    upper=salary.upper,
                    currency=salary.currency,
                    period=salary.period,
                    gross=salary.gross,
                )
                if salary
                else None
            ),
            address=parsed.address,
            metro=[parsed.metro] if parsed.metro else [],
            employer_id=parsed.employer_id,
            employer_name=parsed.employer_name,
            department_name=parsed.department_name,
            conditions=parsed.conditions | {"published_text": parsed.published_text},
            published_at=parsed.published_at,
            contacts={"text": parsed.contacts} if parsed.contacts else {},
            archived=parsed.archived,
            unavailable=parsed.unavailable,
            availability=parsed.availability,
            field_states=states,
        )
        if job_id is not None:
            with self.repo.db.connect() as con:
                discovered = con.execute(
                    """SELECT MIN(rv.discovered_at) FROM run_vacancies rv
                       JOIN jobs j ON j.run_id=rv.run_id AND j.target=rv.vacancy_id
                       WHERE j.id=?""",
                    (job_id,),
                ).fetchone()[0]
            vacancy.discovered_at = datetime.fromisoformat(discovered) if discovered else None
        self.repo.save_vacancy_and_finish_job(vacancy, job_id)
        if self.repo.is_employer_excluded(vacancy.employer_id):
            self.repo.update_local(vacancy.hh_id, status="скрыто")
        return vacancy, False

    async def _load_employer(
        self, browser: BrowserAdapter, employer_id: str, url: str, refresh: bool
    ):
        if not refresh:
            with self.repo.db.connect() as con:
                row = con.execute(
                    "SELECT fetched_at FROM employers WHERE hh_id=?", (employer_id,)
                ).fetchone()
            if row and datetime.now(UTC) - datetime.fromisoformat(row[0]) <= EMPLOYER_TTL:
                return
        source, final_url = await browser.fetch_html(url, readiness="employer")
        parsed = parse_employer_page(source, final_url)
        self.repo.save_employer(
            Employer(
                hh_id=employer_id,
                name=parsed.name,
                description=parsed.description_text,
                site_url=parsed.website,
            )
        )
