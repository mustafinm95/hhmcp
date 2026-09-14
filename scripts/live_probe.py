"""Opt-in anonymous viability probe; never uses an HH API or account."""

from __future__ import annotations

import asyncio
import json

from hhmcp.browser import BrowserAdapter
from hhmcp.parsing import parse_employer_page, parse_vacancy_page


async def main() -> None:
    result = {"pages": [], "vacancies": [], "employers": [], "filter_checks": []}
    employer_urls: list[str] = []
    async with BrowserAdapter(headless=True) as browser:
        async for page, final_url in browser.iter_search_pages(
            "https://hh.ru/search/vacancy?text=python&area=1&experience=noExperience",
            max_pages=3,
        ):
            result["pages"].append(
                {"url": final_url, "items": len(page.items), "filters": page.applied_filters}
            )
            for item in page.items:
                if len(result["vacancies"]) >= 10:
                    break
                source, url = await browser.fetch_html(item.url, readiness="vacancy")
                vacancy = parse_vacancy_page(source, url)
                result["vacancies"].append(
                    {
                        "id": vacancy.hh_id,
                        "title": vacancy.title,
                        "description": bool(vacancy.description_text),
                    }
                )
                if vacancy.employer_url and vacancy.employer_url not in employer_urls:
                    employer_urls.append(vacancy.employer_url)
        for employer_url in employer_urls[:2]:
            source, url = await browser.fetch_html(employer_url, readiness="employer")
            employer = parse_employer_page(source, url)
            result["employers"].append({"id": employer.hh_id, "name": employer.name})
        for parameter in ("salary=100000", "work_format=REMOTE"):
            url = f"https://hh.ru/search/vacancy?text=python&area=1&{parameter}"
            async for page, final_url in browser.iter_search_pages(url, max_pages=1):
                result["filter_checks"].append(
                    {"parameter": parameter, "url": final_url, "items": len(page.items)}
                )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
