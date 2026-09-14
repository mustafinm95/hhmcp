import asyncio
import csv
import multiprocessing
import time
from pathlib import Path

import pytest

from hhmcp.db import Database
from hhmcp.exporting import export_rows
from hhmcp.lock import CollectorBusy, CollectorLock
from hhmcp.models import CandidateProfile, Criterion, FieldState, Salary, SearchSpec, Vacancy
from hhmcp.ranking import rank
from hhmcp.repository import Repository
from hhmcp.service import Collector


def vacancy(vid: str = "1", title: str = "Python") -> Vacancy:
    return Vacancy(
        hh_id=vid,
        url=f"https://hh.ru/vacancy/{vid}",
        title=title,
        description="Backend",
        employer_id="42",
        employer_name="Acme",
        salary=Salary(lower=200_000, upper=250_000, currency="RUR", period="month", gross=False),
    )


def test_search_spec_sources_are_exclusive():
    with pytest.raises(ValueError):
        SearchSpec(url="https://hh.ru/search/vacancy", text="python")
    assert SearchSpec(text="python").text == "python"


def test_versions_a_b_a_and_local_fields_survive(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite"))
    repo.save_vacancy_and_finish_job(vacancy(title="A"))
    repo.update_local("1", status="интересно", favorite=True, notes="note", tags=["python"])
    repo.save_vacancy_and_finish_job(vacancy(title="A"))
    repo.save_vacancy_and_finish_job(vacancy(title="B"))
    repo.save_vacancy_and_finish_job(vacancy(title="A"))
    assert [x["data"]["title"] for x in repo.history("1")] == ["A", "B", "A"]
    rows = repo.list_vacancies()
    assert rows[0]["status"] == "интересно" and rows[0]["favorite"] is True


def test_extraction_error_preserves_value_but_ranking_marks_unknown(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite"))
    repo.save_vacancy_and_finish_job(vacancy())
    bad = vacancy()
    bad.salary = None
    bad.field_states["salary"] = FieldState(state="error", error="selector missing")
    repo.save_vacancy_and_finish_job(bad)
    current, _ = repo.get_vacancy("1")
    assert current.salary and current.salary.lower == 200_000
    profile = CandidateProfile(
        id="p",
        name="p",
        criteria=[
            Criterion(
                field="salary",
                operator="salary_minimum",
                value=180_000,
                currency="RUR",
                period="month",
                gross=False,
                required=True,
            )
        ],
    )
    assert rank(current, profile).eligibility == "недостаточно данных"


def test_salary_ranking_units_and_soft_score():
    profile = CandidateProfile(
        id="p",
        name="p",
        criteria=[
            Criterion(
                field="salary",
                operator="salary_minimum",
                value=180_000,
                currency="RUR",
                period="month",
                gross=False,
                required=True,
            ),
            Criterion(field="skills", operator="contains", value="Python", weight=3),
            Criterion(field="skills", operator="contains", value="SQL", weight=1),
        ],
    )
    v = vacancy()
    v.skills = ["Python"]
    result = rank(v, profile)
    assert result.eligibility == "подходит"
    assert result.score == 75 and result.completeness == 100
    v.salary.currency = "USD"
    assert rank(v, profile).eligibility == "недостаточно данных"


def test_run_comparison_requires_same_normalized_query_and_reports_uncertainty(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite"))
    a = repo.create_run([SearchSpec(text="python")])
    b = repo.create_run([SearchSpec(text="python")])
    repo.observe(a.id, 0, "1", True)
    repo.observe(b.id, 0, "2", True)
    result = repo.compare_runs(b.id, a.id)
    assert result["appeared"] == ["2"] and result["disappeared"] == ["1"]
    assert result["comparison_complete"] is False
    c = repo.create_run([SearchSpec(text="java")])
    with pytest.raises(ValueError):
        repo.compare_runs(c.id, a.id)


def _hold_lock(path: str, ready):
    with CollectorLock(Path(path)):
        ready.set()
        time.sleep(1)


def test_multiprocess_lock_contention_and_release(tmp_path):
    path = tmp_path / "collector.lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=_hold_lock, args=(str(path), ready))
    proc.start()
    assert ready.wait(3)
    with pytest.raises(CollectorBusy):
        CollectorLock(path).acquire()
    proc.join(5)
    assert proc.exitcode == 0
    with CollectorLock(path):
        pass


def test_newer_database_is_rejected(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    with db.connect() as con:
        con.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    with pytest.raises(RuntimeError):
        Database(tmp_path / "db.sqlite")


def test_csv_formula_protection_and_unicode(tmp_path):
    path = tmp_path / "jobs.csv"
    export_rows([{"title": "Разработчик", "notes": "=cmd", "empty": None}], path, "csv")
    with path.open(encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["title"] == "Разработчик" and row["notes"] == "'=cmd" and row["empty"] == ""


def test_employer_exclusion_applies_by_id(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite"))
    repo.exclude_employer("42")
    assert repo.is_employer_excluded("42")
    assert not repo.is_employer_excluded("43")


@pytest.mark.asyncio
async def test_batch_limit_dedup_and_error_consumes_slot(tmp_path, monkeypatch):
    from hhmcp.parsing import SearchItem, SearchPage

    pages = {
        "a": [SearchPage([SearchItem("1", "u1"), SearchItem("2", "u2")], None)],
        "b": [
            SearchPage(
                [SearchItem("1", "u1"), SearchItem("3", "u3"), SearchItem("4", "u4")],
                None,
            )
        ],
    }

    class FakeBrowser:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def iter_search_pages(self, url):
            for page in pages[url]:
                yield page, url

        async def apply_structured_filters(self, filters):
            url = filters["text"]
            return pages[url][0], url

    async def fake_load(self, browser, vacancy_id, url, refresh, job_id=None):
        if vacancy_id == "2":
            raise ValueError("broken fixture")
        self.repo.save_vacancy_and_finish_job(vacancy(vacancy_id), job_id)
        return vacancy(vacancy_id), False

    monkeypatch.setattr("hhmcp.service.BrowserAdapter", FakeBrowser)
    monkeypatch.setattr("hhmcp.service.search_url", lambda spec: spec.text)
    monkeypatch.setattr(Collector, "_load_vacancy", fake_load)
    collector = Collector(tmp_path)
    run = collector.start([SearchSpec(text="a"), SearchSpec(text="b")], limit=3)
    await collector.collect(run)
    state = collector.repo.get_run(run)
    assert state.accepted == 3
    assert state.errors == 1
    assert state.stop_reason == "unique vacancy limit reached"


def test_vacancy_and_job_commit_is_atomic(tmp_path):
    repo = Repository(Database(tmp_path / "db.sqlite"))
    run = repo.create_run([SearchSpec(text="python")])
    with repo.db.transaction() as con:
        con.execute("INSERT INTO jobs(run_id,kind,target) VALUES(?,?,?)", (run.id, "vacancy", "1"))
        job_id = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    repo.save_vacancy_and_finish_job(vacancy(), job_id)
    with repo.db.connect() as con:
        assert con.execute("SELECT state FROM jobs WHERE id=?", (job_id,)).fetchone()[0] == "loaded"
        assert con.execute("SELECT COUNT(*) FROM vacancy_versions").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_mcp_explicit_cancel_stays_cancelled(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    run_id = local.start([SearchSpec(text="python")])
    blocker = asyncio.Event()

    async def wait_forever(_run_id, *, refresh):
        await blocker.wait()

    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(local, "_collect_locked", wait_forever)
    lock = CollectorLock(tmp_path / "collector.lock")
    lock.acquire()
    task = asyncio.create_task(server._run_collection(run_id, False, lock))
    await asyncio.sleep(0)
    local.cancel(run_id)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert local.repo.get_run(run_id).state == "cancelled"


def test_collector_reservation_blocks_other_process(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    monkeypatch.setattr(server, "data_path", lambda: tmp_path)
    monkeypatch.setattr(server, "tasks", {})
    lock = server._reserve_collector()
    try:
        with pytest.raises(CollectorBusy):
            CollectorLock(tmp_path / "collector.lock").acquire()
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_archived_page_without_description_updates_existing_card(tmp_path):
    collector = Collector(tmp_path)
    old = vacancy()
    old.description = "Last valid description"
    collector.repo.save_vacancy_and_finish_job(old)

    class ArchiveBrowser:
        async def fetch_html(self, url, *, readiness):
            return (
                '<h1 data-qa="vacancy-title">Python</h1>'
                '<div data-qa="vacancy-archive">Вакансия в архиве</div>',
                url,
            )

    await collector._load_vacancy(ArchiveBrowser(), "1", "https://hh.ru/vacancy/1", True)
    current, _ = collector.repo.get_vacancy("1")
    assert current.archived is True
    assert current.description == "Last valid description"


def test_condition_extraction_error_makes_ranking_unknown():
    value = vacancy()
    value.conditions["work_format"] = "REMOTE"
    value.field_states["work_format"] = FieldState(state="error", error="damaged")
    profile = CandidateProfile(
        id="p",
        name="p",
        criteria=[
            Criterion(field="conditions.work_format", operator="eq", value="REMOTE", required=True)
        ],
    )
    assert rank(value, profile).eligibility == "недостаточно данных"
