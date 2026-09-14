import asyncio
import unittest

from hhmcp.browser import wait_for_stable_nonzero_count
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


if __name__ == "__main__":
    unittest.main()
