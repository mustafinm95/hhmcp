import asyncio
import unittest

from hhmcp.browser import (
    BrowserAdapter,
    PageNotReady,
    retained_search_filters,
    wait_for_stable_nonzero_count,
)
from hhmcp.parsing import (
    InvalidHHUrl,
    parse_employer_page,
    parse_salary,
    parse_search_page,
    parse_vacancy_page,
    validate_search_url,
)


class ParserTests(unittest.TestCase):
    def test_salary_variants(self):
        cases = (
            ("от 200\u202f000 ₽ в месяц, на руки", (200000, None, "RUB", "month", False)),
            ("до 3 000 $ в неделю gross", (None, 3000, "USD", "week", True)),
            ("1 500–2 000 € за день", (1500, 2000, "EUR", "day", None)),
            ("500 000 KZT в год до вычета налогов", (500000, 500000, "KZT", "year", True)),
            ("750 ₽ в час", (750, 750, "RUB", "hour", None)),
        )
        for text, expected in cases:
            salary = parse_salary(text)
            self.assertIsNotNone(salary)
            self.assertEqual(
                (salary.lower, salary.upper, salary.currency, salary.period, salary.gross),
                expected,
            )

    def test_missing_corrupt_branded_and_archive_fields(self):
        missing = parse_vacancy_page("<main></main>", "https://hh.ru/vacancy/8")
        self.assertEqual(missing.field_states["salary"].state, "absent")
        corrupt = parse_vacancy_page(
            '<h1 data-qa="vacancy-title"></h1><div data-qa="vacancy-salary"></div>',
            "https://hh.ru/vacancy/9",
        )
        self.assertEqual(corrupt.field_states["title"].state, "error")
        branded = parse_vacancy_page(
            """<h1 data-qa="vacancy-title-text">Lead</h1>
            <a data-qa="vacancy-company-name" data-department="Cloud"
               href="/employer/7">Brand</a>
            <div data-qa="vacancy-description">Details</div>
            <div data-qa="vacancy-experience">3–6 лет</div>
            <time data-qa="vacancy-creation-time"
                  datetime="2026-09-14T12:00:00+03:00">сегодня</time>""",
            "https://hh.ru/vacancy/10",
        )
        self.assertEqual(branded.department_name, "Cloud")
        self.assertEqual(branded.conditions["experience"], "3–6 лет")
        self.assertIsNotNone(branded.published_at)
        archived = parse_vacancy_page(
            '<div data-qa="vacancy-archive">Вакансия в архиве</div>',
            "https://hh.ru/vacancy/11",
        )
        self.assertTrue(archived.archived)

        combined = parse_vacancy_page(
            '<h1 data-qa="vacancy-title">Lead</h1>'
            '<div data-qa="vacancy-description">Details</div>'
            '<p data-qa="vacancy-view-employment-mode">Полная занятость, полный день</p>',
            "https://hh.ru/vacancy/17",
        )
        self.assertEqual(combined.conditions["employment"], "Полная занятость")
        self.assertEqual(combined.conditions["schedule"], "Полный день")

    def test_delayed_search_render_waits_past_initial_stable_zero(self):
        values = iter([0, 0, 0, 1, 1, 1, 1, 1])
        calls = 0

        async def count():
            nonlocal calls
            calls += 1
            return next(values)

        async def no_sleep(_seconds):
            return None

        result = asyncio.run(
            wait_for_stable_nonzero_count(count, sleep=no_sleep, stable_for=2, interval=0.5)
        )
        self.assertEqual(result, 1)
        self.assertEqual(calls, 8)

    def test_http_429_uses_retry_path_instead_of_parsing_page(self):
        class Response:
            status = 429

            async def all_headers(self):
                return {"retry-after": "1"}

        class Page:
            async def goto(self, *_args, **_kwargs):
                return Response()

        browser = BrowserAdapter(retries=1)
        browser._page = Page()
        with self.assertRaisesRegex(PageNotReady, "HTTP 429"):
            asyncio.run(browser.fetch_html("https://hh.ru/vacancy/1", readiness="vacancy"))

    def test_search_parsing_deduplicates_excludes_ads_and_finds_next(self):
        html = """
    <div data-qa="vacancy-serp__results">
      <div class="serp-item"><a data-qa="serp-item__title" href="/vacancy/123?from=serp">Python разработчик</a><span data-qa="vacancy-serp__vacancy-employer">Ромашка</span></div>
      <div class="serp-item"><a href="https://hh.ru/vacancy/123">duplicate</a></div>
      <div class="serp-item advert"><a href="/vacancy/999">ad</a></div>
    </div>
    <input name="experience" value="between1And3" checked>
    <a data-qa="pager-next" href="/search/vacancy?page=1&text=python">next</a>"""
        result = parse_search_page(html, "https://hh.ru/search/vacancy?text=python")
        self.assertEqual(
            [(x.hh_id, x.title) for x in result.items], [("123", "Python разработчик")]
        )
        self.assertEqual(result.next_url, "https://hh.ru/search/vacancy?page=1&text=python")
        self.assertEqual(result.applied_filters["experience"], ["between1And3"])

    def test_empty_and_broken_search_are_distinct(self):
        self.assertTrue(
            parse_search_page(
                '<div data-qa="vacancy-serp__empty">Нет вакансий</div>',
                "https://hh.ru/search/vacancy",
            ).is_empty
        )
        self.assertFalse(
            parse_search_page(
                "<main>changed markup</main>", "https://hh.ru/search/vacancy"
            ).is_empty
        )

    def test_vacancy_parses_and_removes_scripts_from_description(self):
        html = """<h1 data-qa="vacancy-title">Инженер</h1>
    <a data-qa="vacancy-company-name" href="/employer/42">ООО Компания</a>
    <div data-qa="vacancy-salary">от 200 000 ₽ на руки</div>
    <div data-qa="vacancy-description"><p>Пишите Python</p><script>ignore me()</script></div>
    <span data-qa="skills-element">Python</span><span data-qa="skills-element">SQL</span>
    <div data-qa="vacancy-view-raw-address">Москва</div>"""
        result = parse_vacancy_page(html, "https://hh.ru/vacancy/123")
        self.assertEqual((result.hh_id, result.employer_id), ("123", "42"))
        self.assertEqual(result.skills, ["Python", "SQL"])
        self.assertNotIn("ignore me", result.description_html or "")
        self.assertEqual(result.salary_text, "от 200 000 ₽ на руки")

    def test_employer_and_archived(self):
        result = parse_employer_page(
            '<h1 data-qa="company-header-title-name">Яндекс</h1><div data-qa="company-description">Описание</div><a data-qa="company-site" href="https://ya.ru">Сайт</a>',
            "https://hh.ru/employer/1740",
        )
        self.assertEqual(
            (result.hh_id, result.name, result.website), ("1740", "Яндекс", "https://ya.ru")
        )
        self.assertTrue(
            parse_vacancy_page("<div>Вакансия в архиве</div>", "https://hh.ru/vacancy/1").archived
        )

    def test_url_validation(self):
        self.assertTrue(validate_search_url("https://spb.hh.ru/search/vacancy?text=python"))
        for bad in (
            "http://hh.ru/search/vacancy",
            "https://evil-hh.ru/search/vacancy",
            "https://hh.ru/vacancy/1",
            "https://hh.ru:444/search/vacancy",
        ):
            try:
                validate_search_url(bad)
            except InvalidHHUrl:
                pass
            else:
                self.fail(bad)

    def test_filter_verification_uses_final_url_and_ignores_service_parameters(self):
        request = (
            "https://hh.ru/search/vacancy?text=HR&area=1&area=2&employment=full"
            "&search_session_id=old"
        )
        applied, missing = retained_search_filters(
            request,
            "https://hh.ru/search/vacancy?employment=full&area=2&area=1&text=HR&page=1",
        )
        self.assertEqual(applied["area"], ["1", "2"])
        self.assertEqual(missing, [])

        _, missing = retained_search_filters(
            request, "https://hh.ru/search/vacancy?employment=full&text=HR"
        )
        self.assertEqual(missing, ["area"])

    def test_mixed_text_order_and_script_style_are_removed(self):
        result = parse_vacancy_page(
            '<h1 data-qa="vacancy-title">Python</h1>'
            '<div data-qa="vacancy-description">от <b>100</b> до <b>200</b>'
            "<script>ignore me</script><style>ignore too</style></div>",
            "https://hh.ru/vacancy/1",
        )
        self.assertEqual(result.description_text, "от 100 до 200")
        self.assertNotIn("script", result.description_html or "")
        self.assertNotIn("style", result.description_html or "")

    def test_hidden_terminal_templates_do_not_mark_active_vacancy_closed(self):
        result = parse_vacancy_page(
            '<div style="display:none"><span>Вакансия в архиве</span>'
            '<span>Страница не найдена</span></div>'
            '<h1 data-qa="vacancy-title">HR Lead</h1>'
            '<div data-qa="vacancy-description">Полная занятость, удалённая работа</div>',
            "https://hh.ru/vacancy/12",
        )
        self.assertEqual(result.availability, "active")
        self.assertFalse(result.archived)
        self.assertFalse(result.unavailable)
        self.assertEqual(result.conditions["employment"], "Полная занятость")
        self.assertEqual(result.conditions["work_format"], "Удалённо")

    def test_json_ld_and_listing_fallbacks_fill_structured_fields(self):
        source = """
        <h1 data-qa="vacancy-title">HRD</h1>
        <div data-qa="vacancy-description">Описание</div>
        <script type="application/ld+json">
          {"@type":"JobPosting","datePosted":"2026-09-10",
           "employmentType":"FULL_TIME","jobLocationType":"TELECOMMUTE"}
        </script>"""
        result = parse_vacancy_page(source, "https://hh.ru/vacancy/13")
        self.assertEqual(result.published_at.isoformat(), "2026-09-10T00:00:00+00:00")
        self.assertEqual(result.field_states["published_at"].source, "json_ld")
        self.assertEqual(result.conditions["employment"], "Полная занятость")
        self.assertEqual(result.conditions["work_format"], "Удалённо")

        listing = parse_vacancy_page(
            '<h1 data-qa="vacancy-title">Lead</h1>'
            '<div data-qa="vacancy-description">Описание</div>',
            "https://hh.ru/vacancy/14",
            fallback_fields={"published_text": "10 сентября 2026"},
        )
        self.assertEqual(listing.published_at.isoformat(), "2026-09-10T00:00:00+00:00")
        self.assertEqual(listing.field_states["published_at"].source, "listing")

    def test_http_terminal_evidence_overrides_page_templates(self):
        unavailable = parse_vacancy_page(
            "<main></main>", "https://hh.ru/vacancy/15", http_status=404
        )
        self.assertEqual(unavailable.availability, "unavailable")
        archived = parse_vacancy_page(
            '<h1 data-qa="vacancy-title">Old</h1>',
            "https://hh.ru/vacancy/16",
            availability="archived",
        )
        self.assertTrue(archived.archived)

    def test_browser_visible_fields_override_hidden_dom_candidates(self):
        result = parse_vacancy_page(
            '<h1 data-qa="vacancy-title">Lead</h1>'
            '<div data-qa="vacancy-description">Описание</div>'
            '<div class="css-hidden" data-qa="vacancy-work-format">В офисе</div>',
            "https://hh.ru/vacancy/18",
            visible_fields={
                "conditions": {"work_format": "Удалённая работа"},
                "published_text": "10 сентября 2026",
            },
        )
        self.assertEqual(result.conditions["work_format"], "Удалённо")
        self.assertEqual(result.field_states["work_format"].source, "dom")
        self.assertEqual(result.published_at.isoformat(), "2026-09-10T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
