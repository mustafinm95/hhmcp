import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from hhmcp.models import SearchSpec
from hhmcp.parsing import SearchItem, SearchPage
from hhmcp.service import Collector
from test_core import vacancy

FIRST_URL = "https://hh.ru/search/vacancy?text=first"
SECOND_URL = "https://hh.ru/search/vacancy?text=second"
SEARCH_URL = "https://hh.ru/search/vacancy?text=python"


class PageBrowser:
    pages_by_url: dict[str, list[SearchPage]] = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    def iter_search_pages(self, url):
        class Pages:
            def __init__(self, values):
                self.values = iter(values)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.values), url
                except StopIteration:
                    raise StopAsyncIteration from None

        return Pages(self.pages_by_url[url])


@pytest.mark.asyncio
async def test_batch_accepts_exactly_1000_unique_ids_with_duplicates_and_errors(
    tmp_path, monkeypatch
):
    first = [SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(600)]
    second = [
        *(SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(200)),
        *(SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(600, 1001)),
    ]
    PageBrowser.pages_by_url = {
        FIRST_URL: [SearchPage(first[i : i + 200], None) for i in range(0, 600, 200)],
        SECOND_URL: [SearchPage(second[i : i + 200], None) for i in range(0, len(second), 200)],
    }

    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None, _observed=None):
        if int(vacancy_id) % 100 == 0:
            raise ValueError("damaged vacancy page")
        value = vacancy(vacancy_id)
        self.repo.save_vacancy_and_finish_job(value, job_id)
        return value, False

    monkeypatch.setattr("hhmcp.service.BrowserAdapter", PageBrowser)
    monkeypatch.setattr(Collector, "_load_vacancy", load)

    collector = Collector(tmp_path)
    run_id = collector.start([SearchSpec(url=FIRST_URL), SearchSpec(url=SECOND_URL)])
    await collector.collect(run_id)

    run = collector.repo.get_run(run_id)
    assert (run.accepted, run.loaded, run.cached, run.errors) == (1000, 990, 0, 10)
    assert run.state == "interrupted"
    assert run.complete is False
    assert run.stop_reason == "unique vacancy limit reached"
    with collector.repo.db.connect() as con:
        accepted = con.execute(
            "SELECT COUNT(DISTINCT vacancy_id) FROM run_vacancies WHERE run_id=? AND accepted=1",
            (run_id,),
        ).fetchone()[0]
        overflow = con.execute(
            "SELECT accepted,outcome FROM run_vacancies WHERE run_id=? AND vacancy_id='1000'",
            (run_id,),
        ).fetchone()
        overflow_job = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE run_id=? AND kind='vacancy' AND target='1000'",
            (run_id,),
        ).fetchone()[0]
        memberships = con.execute(
            "SELECT COUNT(*) FROM run_vacancies WHERE run_id=? AND vacancy_id='42'", (run_id,)
        ).fetchone()[0]
    assert accepted == 1000
    assert overflow is None or tuple(overflow) == (0, None)
    assert overflow_job == 0
    assert memberships == 2


@pytest.mark.asyncio
async def test_resume_requeues_and_completes_a_job_left_running_by_a_crash(tmp_path, monkeypatch):
    PageBrowser.pages_by_url = {
        SEARCH_URL: [SearchPage([SearchItem("1", "https://hh.ru/vacancy/1")], None)]
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", PageBrowser)
    collector = Collector(tmp_path)
    run_id = collector.start([SearchSpec(url=SEARCH_URL)])
    collector.repo.observe(run_id, 0, "1", True)
    collector.repo.increment(run_id, discovered=1, accepted=1)
    with collector.repo.db.transaction() as con:
        con.execute(
            "INSERT INTO jobs(run_id,kind,target,payload_json,state,attempts) VALUES(?,?,?,?,?,?)",
            (
                run_id,
                "vacancy",
                "1",
                '{"url":"https://hh.ru/vacancy/1","observed":{}}',
                "running",
                1,
            ),
        )

    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None, _observed=None):
        value = vacancy(vacancy_id)
        self.repo.save_vacancy_and_finish_job(value, job_id)
        return value, False

    monkeypatch.setattr(Collector, "_load_vacancy", load)
    await collector.collect(run_id)

    run = collector.repo.get_run(run_id)
    assert (run.state, run.complete, run.accepted, run.loaded, run.errors) == (
        "completed",
        True,
        1,
        1,
        0,
    )
    with collector.repo.db.connect() as con:
        job = con.execute(
            "SELECT state,attempts,error FROM jobs WHERE run_id=? AND kind='vacancy'", (run_id,)
        ).fetchone()
        versions = con.execute(
            "SELECT COUNT(*) FROM vacancy_versions WHERE vacancy_id='1'"
        ).fetchone()[0]
    assert tuple(job) == ("loaded", 2, None)
    assert versions == 1


@pytest.mark.asyncio
async def test_successful_job_has_one_atomic_outcome_and_matching_counters(tmp_path, monkeypatch):
    PageBrowser.pages_by_url = {
        SEARCH_URL: [SearchPage([SearchItem("1", "u1"), SearchItem("2", "u2")], None)]
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", PageBrowser)

    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None, _observed=None):
        if vacancy_id == "2":
            raise RuntimeError("parse failed")
        value = vacancy(vacancy_id)
        self.repo.save_vacancy_and_finish_job(value, job_id)
        return value, False

    monkeypatch.setattr(Collector, "_load_vacancy", load)
    collector = Collector(tmp_path)
    run_id = collector.start([SearchSpec(url=SEARCH_URL)])
    await collector.collect(run_id)

    run = collector.repo.get_run(run_id)
    assert (run.discovered, run.accepted, run.loaded, run.cached, run.errors) == (2, 2, 1, 0, 1)
    with collector.repo.db.connect() as con:
        outcomes = dict(
            con.execute(
                "SELECT vacancy_id,outcome FROM run_vacancies WHERE run_id=?", (run_id,)
            ).fetchall()
        )
        jobs = dict(
            con.execute(
                "SELECT target,state FROM jobs WHERE run_id=? AND kind='vacancy'", (run_id,)
            )
        )
    assert outcomes == {"1": "loaded", "2": "error"}
    assert jobs == {"1": "loaded", "2": "error"}


class DetailBrowser:
    def __init__(self, title="Fresh"):
        self.title = title
        self.fetches = 0

    async def fetch_html(self, url, *, readiness):
        self.fetches += 1
        return (
            f'<h1 data-qa="vacancy-title">{self.title}</h1>'
            '<div data-qa="vacancy-description">Description</div>',
            url,
        )


@pytest.mark.asyncio
async def test_vacancy_ttl_cache_is_bypassed_only_by_refresh(tmp_path):
    collector = Collector(tmp_path)
    cached = vacancy(title="Cached")
    cached.fetched_at = datetime.now(UTC) - timedelta(hours=23)
    collector.repo.save_vacancy_and_finish_job(cached)
    browser = DetailBrowser()

    value, from_cache = await collector._load_vacancy(browser, "1", cached.url, False)
    assert (value.title, from_cache, browser.fetches) == ("Cached", True, 0)

    value, from_cache = await collector._load_vacancy(browser, "1", cached.url, True)
    assert (value.title, from_cache, browser.fetches) == ("Fresh", False, 1)


@pytest.mark.asyncio
async def test_old_parser_version_invalidates_fresh_detail_cache(tmp_path):
    collector = Collector(tmp_path)
    cached = vacancy(title="Old parser")
    cached.parser_version = "1"
    cached.fetched_at = datetime.now(UTC)
    collector.repo.save_vacancy_and_finish_job(cached)
    browser = DetailBrowser(title="Reparsed")

    value, from_cache = await collector._load_vacancy(browser, "1", cached.url, False)

    assert from_cache is False
    assert value.title == "Reparsed"
    assert browser.fetches == 1


@pytest.mark.asyncio
async def test_listing_mismatch_invalidates_a_fresh_detail_cache(tmp_path, monkeypatch):
    collector = Collector(tmp_path)
    cached = vacancy(title="Old title")
    cached.fetched_at = datetime.now(UTC)
    collector.repo.save_vacancy_and_finish_job(cached)

    class MismatchBrowser(PageBrowser):
        fetches = 0

        async def iter_search_pages(self, url):
            yield SearchPage([SearchItem("1", cached.url, title="New title")], None), url

        async def fetch_html(self, url, *, readiness):
            type(self).fetches += 1
            return (
                '<h1 data-qa="vacancy-title">New title</h1>'
                '<div data-qa="vacancy-description">Description</div>',
                url,
            )

    monkeypatch.setattr("hhmcp.service.BrowserAdapter", MismatchBrowser)
    run_id = collector.start([SearchSpec(url=SEARCH_URL)])
    await collector.collect(run_id)

    current, _ = collector.repo.get_vacancy("1")
    assert MismatchBrowser.fetches == 1
    assert current.title == "New title"
    assert collector.repo.get_run(run_id).cached == 0


class TrackingLock:
    def __init__(self):
        self.released = False

    def release(self):
        self.released = True


@pytest.mark.asyncio
async def test_mcp_invalid_search_spec_releases_reserved_collector(monkeypatch):
    import hhmcp.mcp_server as server

    reserved = False

    def reserve():
        nonlocal reserved
        reserved = True
        return TrackingLock()

    monkeypatch.setattr(server, "_reserve_collector", reserve)
    with pytest.raises(ValueError):
        await server.start_collection([{"url": "https://hh.ru/search/vacancy", "text": "python"}])
    assert reserved is False


@pytest.mark.asyncio
async def test_mcp_cancel_before_background_coroutine_starts_releases_lock(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    lock = TrackingLock()
    blocker = asyncio.Event()

    async def wait_forever(_run_id, *, refresh):
        await blocker.wait()

    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(local, "_collect_locked", wait_forever)
    monkeypatch.setattr(server, "tasks", {})
    monkeypatch.setattr(server, "_reserve_collector", lambda: lock)

    result = await server.start_collection([{"text": "python"}])
    await server.cancel_collection(result["run_id"])

    assert local.repo.get_run(result["run_id"]).state == "cancelled"
    assert lock.released


@pytest.mark.asyncio
async def test_mcp_background_exception_releases_lock(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    run_id = local.start([SearchSpec(text="python")])
    lock = TrackingLock()

    async def fail(_run_id, *, refresh):
        raise RuntimeError("collector crashed")

    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(local, "_collect_locked", fail)
    with pytest.raises(RuntimeError, match="collector crashed"):
        await server._run_collection(run_id, False, lock)
    assert lock.released


@pytest.mark.asyncio
@pytest.mark.parametrize("orphan_state", ["queued", "running", "completed"])
async def test_mcp_public_resume_accepts_orphaned_incomplete_state(
    tmp_path, monkeypatch, orphan_state
):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    run_id = local.start([SearchSpec(text="python")])
    local.repo.set_run_state(run_id, orphan_state, "simulated process loss", False)
    lock = TrackingLock()

    async def finish(resumed_id, *, refresh):
        assert resumed_id == run_id
        local.repo.set_run_state(run_id, "completed", complete=True)

    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(server, "tasks", {})
    monkeypatch.setattr(server, "reservations", {})
    monkeypatch.setattr(local, "_collect_locked", finish)
    monkeypatch.setattr(server, "_reserve_collector", lambda: lock)
    result = await server.resume_collection(run_id)
    await server.tasks[run_id]
    assert result == {"run_id": run_id, "state": "queued"}
    assert local.repo.get_run(run_id).complete is True
    assert lock.released


@pytest.mark.asyncio
async def test_structured_search_uses_url_and_reports_finished_progress(tmp_path, monkeypatch):
    seen = []

    class DirectBrowser(PageBrowser):
        async def iter_search_pages(self, url):
            seen.append(url)
            yield SearchPage([], None), url

        async def apply_structured_filters(self, _filters):
            raise AssertionError("structured filters must not be clicked in the UI")

    monkeypatch.setattr("hhmcp.service.BrowserAdapter", DirectBrowser)
    collector = Collector(tmp_path)
    run_id = collector.start([SearchSpec(text="HR Lead", area=["1"], employment=["full"])])
    await collector.collect(run_id)

    run = collector.repo.get_run(run_id)
    assert seen and "area=1" in seen[0] and "employment=full" in seen[0]
    assert run.state == "completed"
    assert run.progress.phase == "finished"
    assert run.progress.searches[0].complete is True


@pytest.mark.asyncio
async def test_detail_loading_is_bounded_to_three_workers(tmp_path, monkeypatch):
    active = 0
    maximum = 0

    class ConcurrentBrowser(PageBrowser):
        async def new_page_adapter(self):
            return self

        async def close_page(self):
            return None

    ConcurrentBrowser.pages_by_url = {
        SEARCH_URL: [
            SearchPage(
                [SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(1, 5)],
                None,
            )
        ]
    }

    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.01)
        value = vacancy(vacancy_id)
        self.repo.save_vacancy_and_finish_job(value, job_id)
        active -= 1
        return value, False

    monkeypatch.setattr("hhmcp.service.BrowserAdapter", ConcurrentBrowser)
    monkeypatch.setattr(Collector, "_load_vacancy", load)
    collector = Collector(tmp_path)
    run_id = collector.start([SearchSpec(url=SEARCH_URL)])
    await collector.collect(run_id)

    assert maximum == 3
    assert collector.repo.get_run(run_id).loaded == 4


@pytest.mark.asyncio
async def test_vacancy_collection_does_not_fetch_employer_page(tmp_path):
    class VacancyBrowser:
        last_status = 200
        last_availability = "active"

        def __init__(self):
            self.urls = []

        async def fetch_html(self, url, *, readiness):
            self.urls.append((url, readiness))
            return (
                '<h1 data-qa="vacancy-title">HR Lead</h1>'
                '<a data-qa="vacancy-company-name" href="/employer/42">Acme</a>'
                '<div data-qa="vacancy-description">Description</div>',
                url,
            )

    browser = VacancyBrowser()
    collector = Collector(tmp_path)
    await collector._load_vacancy(browser, "1", "https://hh.ru/vacancy/1", True)

    assert browser.urls == [("https://hh.ru/vacancy/1", "vacancy")]
    with collector.repo.db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM employers").fetchone()[0] == 0
