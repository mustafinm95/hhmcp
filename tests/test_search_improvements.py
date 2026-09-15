from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from hhmcp.assessment import analyze_vacancy
from hhmcp.models import CandidateProfile, Salary, SearchSpec, Vacancy
from hhmcp.parsing import SearchItem, SearchPage
from hhmcp.service import Collector, search_url


class Browser:
    pages = {}

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def new_page_adapter(self):
        return self

    async def close_page(self):
        return None

    def iter_search_pages(self, url):
        async def values():
            for page in self.pages[url]:
                yield page, url

        return values()


def vacancy(vacancy_id: str, *, published_at=None, description="Description") -> Vacancy:
    return Vacancy(
        hh_id=vacancy_id,
        url=f"https://hh.ru/vacancy/{vacancy_id}",
        title="HR Lead",
        description=description,
        published_at=published_at,
    )


def install_loader(monkeypatch, loaded):
    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None, *_args):
        value = vacancy(vacancy_id, published_at=datetime.now(UTC))
        self.repo.save_vacancy_and_finish_job(value, job_id)
        loaded.append(vacancy_id)
        return value, False

    monkeypatch.setattr(Collector, "_load_vacancy", load)


@pytest.mark.asyncio
async def test_period_is_verified_against_loaded_card(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR", period=3)
    url = search_url(spec)
    Browser.pages = {url: [SearchPage([SearchItem("1", "https://hh.ru/vacancy/1")], None)]}
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)

    async def load(self, _browser, vacancy_id, _url, _refresh, job_id=None, *_args):
        value = vacancy(vacancy_id, published_at=datetime.now(UTC) - timedelta(days=10))
        self.repo.save_vacancy_and_finish_job(value, job_id)
        return value, False

    monkeypatch.setattr(Collector, "_load_vacancy", load)
    collector = Collector(tmp_path)
    run_id = collector.start([spec])
    await collector.collect(run_id)

    assert collector.repo.run_results(run_id) == []


@pytest.mark.asyncio
async def test_period_is_verified_for_cached_card(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR", period=3)
    url = search_url(spec)
    Browser.pages = {url: [SearchPage([SearchItem("1", "https://hh.ru/vacancy/1")], None)]}
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    collector = Collector(tmp_path)
    collector.repo.save_vacancy_and_finish_job(
        vacancy("1", published_at=datetime.now(UTC) - timedelta(days=10))
    )
    run_id = collector.start([spec], fetch_details="all")

    await collector.collect(run_id)

    assert collector.repo.run_results(run_id) == []
    assert collector.repo.get_run(run_id).cached == 1


@pytest.mark.asyncio
async def test_period_excludes_metadata_without_a_publication_date(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR", period=3)
    url = search_url(spec)
    Browser.pages = {url: [SearchPage([SearchItem("1", "https://hh.ru/vacancy/1")], None)]}
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    collector = Collector(tmp_path)
    run_id = collector.start([spec], fetch_details="none")

    await collector.collect(run_id)

    assert collector.repo.run_results(run_id) == []


@pytest.mark.asyncio
async def test_title_phrase_filter_rejects_unrelated_listing(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR Lead", search_field="title", match_mode="phrase")
    url = search_url(spec)
    Browser.pages = {
        url: [
            SearchPage(
                [
                    SearchItem("1", "https://hh.ru/vacancy/1", title="Продажи HR-платформы"),
                    SearchItem("2", "https://hh.ru/vacancy/2", title="Senior HR Lead"),
                ],
                None,
            )
        ]
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    loaded = []
    install_loader(monkeypatch, loaded)
    collector = Collector(tmp_path)
    run_id = collector.start([spec])
    await collector.collect(run_id)

    assert loaded == ["2"]


@pytest.mark.asyncio
async def test_per_search_limit_preserves_quota_for_every_query(tmp_path, monkeypatch):
    specs = [SearchSpec(text="first"), SearchSpec(text="second")]
    urls = [search_url(spec) for spec in specs]
    Browser.pages = {
        urls[0]: [
            SearchPage([SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(3)], None)
        ],
        urls[1]: [
            SearchPage(
                [SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(3, 6)], None
            )
        ],
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    loaded = []
    install_loader(monkeypatch, loaded)
    collector = Collector(tmp_path)
    run_id = collector.start(specs, limit=10, per_search_limit=2)
    await collector.collect(run_id)

    assert set(loaded) == {"0", "1", "3", "4"}
    stats = collector.repo.run_search_stats(run_id)
    assert [item["discovered"] for item in stats] == [2, 2]
    assert [item["complete"] for item in stats] == [True, True]
    assert [item["stop_reason"] for item in stats] == [
        "per_search_limit reached",
        "per_search_limit reached",
    ]


@pytest.mark.asyncio
async def test_cancel_stops_running_detail_workers(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR")
    url = search_url(spec)
    Browser.pages = {
        url: [
            SearchPage(
                [SearchItem(str(i), f"https://hh.ru/vacancy/{i}") for i in range(12)],
                None,
            )
        ]
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    started = asyncio.Event()
    attempts = 0

    async def slow_load(self, _browser, vacancy_id, _url, _refresh, job_id=None, *_args):
        nonlocal attempts
        attempts += 1
        if attempts >= 3:
            started.set()
        await asyncio.sleep(1)
        value = vacancy(vacancy_id, published_at=datetime.now(UTC))
        self.repo.save_vacancy_and_finish_job(value, job_id)
        return value, False

    monkeypatch.setattr(Collector, "_load_vacancy", slow_load)
    collector = Collector(tmp_path)
    run_id = collector.start([spec])
    task = asyncio.create_task(collector.collect(run_id))
    await asyncio.wait_for(started.wait(), timeout=2)

    collector.cancel(run_id)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)

    assert collector.repo.get_run(run_id).state == "cancelled"
    assert collector.repo.get_run(run_id).loaded == 0
    with collector.repo.db.connect() as con:
        assert (
            con.execute(
                "SELECT COUNT(*) FROM jobs WHERE run_id=? AND state IN ('queued','running')",
                (run_id,),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.asyncio
async def test_shortlist_fetches_only_requested_number_of_details(tmp_path, monkeypatch):
    spec = SearchSpec(text="HR")
    url = search_url(spec)
    Browser.pages = {
        url: [
            SearchPage(
                [
                    SearchItem(str(i), f"https://hh.ru/vacancy/{i}", title=f"HR {i}")
                    for i in range(5)
                ],
                None,
            )
        ]
    }
    monkeypatch.setattr("hhmcp.service.BrowserAdapter", Browser)
    loaded = []
    install_loader(monkeypatch, loaded)
    collector = Collector(tmp_path)
    run_id = collector.start([spec], fetch_details="shortlist", shortlist_size=2)
    await collector.collect(run_id)

    run = collector.repo.get_run(run_id)
    assert run.accepted == 5
    assert run.loaded == 2
    assert len(loaded) == 2


def test_assessment_normalizes_temporary_remote_kdp_role_and_conflicts():
    value = vacancy(
        "1",
        description=(
            "Временно удаленная работа, после адаптации гибрид. В подчинении 3-4 человека "
            "и специалист по кадровому делопроизводству. Ответственность за всю HR-функцию, "
            "подчинение CEO. Оклад 180 000 ₽ + KPI."
        ),
    )
    value.salary = Salary(lower=260000, upper=260000, currency="RUB", period="month")
    assessment, diagnostics = analyze_vacancy(value)

    assert assessment["work_format"]["current"] == "remote"
    assert assessment["work_format"]["permanent"] is False
    assert assessment["work_format"]["future"] == "hybrid"
    assert assessment["kdp"]["hands_on"] is False
    assert assessment["kdp"]["oversight"] is True
    assert assessment["leadership"]["role_level"] == "hr_director"
    assert diagnostics["salary_conflict"] is True


def test_assessment_respects_explicit_negations():
    value = vacancy(
        "1",
        description=(
            "Удаленная работа не предусмотрена. В подчинении сотрудников нет. "
            "Вести КДП не требуется."
        ),
    )

    assessment, _ = analyze_vacancy(value)

    assert assessment["work_format"]["current"] == "on_site"
    assert assessment["leadership"]["has_team"] is False
    assert assessment["leadership"]["has_team_state"] == "confirmed_absent"
    assert assessment["kdp"]["hands_on"] is False
    assert assessment["kdp"]["hands_on_state"] == "confirmed_absent"


def test_compact_view_and_batch_ranking_and_cursor(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    for vacancy_id in ("1", "2", "3"):
        value = vacancy(vacancy_id, description="long description")
        value.assessment, value.diagnostics = analyze_vacancy(value)
        local.repo.save_vacancy_and_finish_job(value)
    local.repo.save_profile(CandidateProfile(id="p", name="P", criteria=[]))
    monkeypatch.setattr(server, "collector", local)

    compact = server.get_vacancy("1", view="compact")["vacancy"]
    assert "description" not in compact
    assert "field_states" not in compact
    assert len(server.get_vacancies(["1", "2"])) == 2
    assert len(server.rank_vacancies("p", ["1", "2"], top_k=1)) == 1

    run_id = local.start([SearchSpec(text="HR")], fetch_details="none")
    for vacancy_id in ("1", "2", "3"):
        local.repo.record_observation(
            run_id, 0, vacancy_id, f"https://hh.ru/vacancy/{vacancy_id}", 100
        )
    first = server.recommend_vacancies("p", [run_id], limit=2)
    local.repo.save_vacancy_and_finish_job(vacancy("4"))
    local.repo.record_observation(run_id, 0, "4", "https://hh.ru/vacancy/4", 100)
    second = server.recommend_vacancies("p", [run_id], limit=2, cursor=first["next_cursor"])
    assert {item["vacancy_id"] for item in first["items"]}.isdisjoint(
        {item["vacancy_id"] for item in second["items"]}
    )
    assert {item["vacancy_id"] for item in first["items"] + second["items"]} == {
        "1",
        "2",
        "3",
    }
    with pytest.raises(ValueError, match="does not match"):
        server.recommend_vacancies(
            "p", [run_id], limit=2, cursor=first["next_cursor"], categories=["rejected"]
        )


def test_recommendation_handles_metadata_only_vacancy(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    local.repo.save_profile(CandidateProfile(id="p", name="P", criteria=[]))
    run_id = local.start([SearchSpec(text="HR")], fetch_details="none")
    local.repo.record_observation(
        run_id,
        0,
        "missing",
        "https://hh.ru/vacancy/missing",
        100,
        {"title": "HR Lead"},
    )
    monkeypatch.setattr(server, "collector", local)

    result = server.recommend_vacancies("p", [run_id])

    assert result["items"][0]["vacancy_id"] == "missing"
    assert result["items"][0]["category"] == "insufficient_data"


def test_full_and_raw_collection_results_include_description(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    local.repo.save_vacancy_and_finish_job(vacancy("1", description="Full text"))
    run_id = local.start([SearchSpec(text="HR")])
    local.repo.record_observation(run_id, 0, "1", "https://hh.ru/vacancy/1", 100)
    monkeypatch.setattr(server, "collector", local)

    assert server.collection_results(run_id, view="full")[0]["description"] == "Full text"
    assert server.collection_results(run_id, view="raw")[0]["description"] == "Full text"


def test_title_aliases_include_original_query_upstream_and_locally():
    from hhmcp.service import _title_matches

    spec = SearchSpec(
        text="HR Director",
        search_field="title",
        match_mode="title_aliases",
        title_aliases=["HRD", "Head of HR"],
    )

    url = search_url(spec)

    assert "%22HR+Director%22+OR+%22HRD%22+OR+%22Head+of+HR%22" in url
    assert _title_matches(spec, "HR Director")
    assert _title_matches(spec, "HRD")
    assert not _title_matches(spec, "HR Manager")


@pytest.mark.asyncio
async def test_second_mcp_run_is_queued_instead_of_busy(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server

    local = Collector(tmp_path)
    pending = asyncio.Future()
    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(server, "tasks", {"active": pending})
    monkeypatch.setattr(server, "queued_runs", [])
    monkeypatch.setattr(server, "_ensure_queue_dispatcher", lambda: None)

    result = await server.start_collection([{"text": "HR"}], fetch_details="none")

    assert result["state"] == "queued"
    assert result["position"] == 1
    assert result["active_run_id"] == "active"
    pending.cancel()


@pytest.mark.asyncio
async def test_external_collector_lock_also_creates_a_durable_queue_entry(tmp_path, monkeypatch):
    import hhmcp.mcp_server as server
    from hhmcp.lock import CollectorBusy

    local = Collector(tmp_path)
    monkeypatch.setattr(server, "collector", local)
    monkeypatch.setattr(server, "tasks", {})
    monkeypatch.setattr(server, "queued_runs", [])
    monkeypatch.setattr(server, "_ensure_queue_dispatcher", lambda: None)

    def busy():
        raise CollectorBusy("collector_busy")

    monkeypatch.setattr(server, "_reserve_collector", busy)

    result = await server.start_collection([{"text": "HR"}], fetch_details="none")

    assert result["state"] == "queued"
    assert local.repo.queued_run_ids() == [result["run_id"]]
    assert server.queued_runs == [(result["run_id"], False)]
