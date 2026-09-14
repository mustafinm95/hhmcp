"""Rate-limited Playwright adapter for public HH pages."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from .parsing import SearchPage, parse_search_page, validate_search_url


class BrowserBlocked(RuntimeError):
    """HH displayed a CAPTCHA or access-block page."""


class PageNotReady(RuntimeError):
    """The page did not reach an explicit, parseable state."""


async def wait_for_stable_nonzero_count(
    read_count: Callable[[], Awaitable[int]],
    *,
    stable_for: float = 2.0,
    interval: float = 0.5,
    attempts: int = 20,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Wait until a non-zero count remains unchanged for ``stable_for``."""
    previous = 0
    stable_elapsed = 0.0
    for _ in range(attempts):
        current = await read_count()
        if current > 0 and current == previous:
            stable_elapsed += interval
            if stable_elapsed >= stable_for:
                return current
        else:
            stable_elapsed = 0.0
        previous = current
        await sleep(interval)
    raise PageNotReady("search card count did not stabilize")


class BrowserAdapter:
    def __init__(
        self,
        *,
        headless: bool = True,
        min_delay: float = 2.0,
        retries: int = 3,
        timeout_ms: int = 30_000,
        profile_dir: str | Path | None = None,
    ) -> None:
        if min_delay < 2:
            raise ValueError("min_delay must be at least 2 seconds")
        self.headless = headless
        self.min_delay = min_delay
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.profile_dir = Path(profile_dir or Path.home() / ".hhmcp" / "browser-profile")
        self._playwright = self._page = None
        self._context = None
        self._last_navigation = 0.0

    async def __aenter__(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            message = "install hhmcp with Playwright and run 'playwright install chromium'"
            raise RuntimeError(message) from exc
        start_task: asyncio.Task[Any] = asyncio.create_task(async_playwright().start())
        try:
            self._playwright = cast(Any, await asyncio.shield(start_task))
        except asyncio.CancelledError:
            playwright = await start_task
            await playwright.stop()
            raise
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, locale="ru-RU"
            )
            self._page = (
                self._context.pages[0] if self._context.pages else await self._context.new_page()
            )
            return self
        except BaseException:
            await self._playwright.stop()
            self._playwright = None
            raise

    async def __aexit__(self, *_):
        async def cleanup() -> None:
            try:
                if self._context:
                    await self._context.close()
            finally:
                if self._playwright:
                    await self._playwright.stop()
                self._context = self._playwright = self._page = None

        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def _pace(self) -> None:
        await asyncio.sleep(max(0.0, self.min_delay - (time.monotonic() - self._last_navigation)))

    async def fetch_html(self, url: str, *, readiness: str = "generic") -> tuple[str, str]:
        if self._page is None:
            raise RuntimeError("BrowserAdapter must be used as an async context manager")
        error: Exception | None = None
        for attempt in range(self.retries):
            await self._pace()
            try:
                self._last_navigation = time.monotonic()
                await self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                await self._raise_if_blocked()
                await self._wait_ready(readiness)
                await self._raise_if_blocked()
                return await self._page.content(), self._page.url
            except BrowserBlocked:
                if self.headless:
                    raise
                await self.visible_resume(self._page.url)
                await self._wait_ready(readiness)
                return await self._page.content(), self._page.url
            except Exception as exc:
                error = exc
                if attempt + 1 < self.retries:
                    await asyncio.sleep(2**attempt)
        raise PageNotReady(f"page failed after {self.retries} attempts: {error}") from error

    async def apply_structured_filters(
        self, filters: dict[str, str | list[str]]
    ) -> tuple[SearchPage, str]:
        """Apply filters through controls present in HH's search form.

        Values are never translated into undocumented query constants.  A missing
        control is an explicit error, and the resulting page still reports URL
        parameters that could not be confirmed from checked UI controls.
        """
        if self._page is None:
            raise RuntimeError("BrowserAdapter must be used as an async context manager")
        await self._pace()
        self._last_navigation = time.monotonic()
        await self._page.goto(
            "https://hh.ru/search/vacancy",
            wait_until="domcontentloaded",
            timeout=self.timeout_ms,
        )
        await self._raise_if_blocked()
        verified: dict[str, list[str]] = {}
        text_value = filters.pop("text", None)
        if text_value is not None:
            search_input = self._page.locator('[data-qa="search-input"], input[name="text"]')
            if not await search_input.count():
                raise ValueError("filter is unavailable in HH UI: text")
            await search_input.first.fill(str(text_value))
            verified["text"] = [str(text_value)]
        drawer = self._page.locator('[data-qa="header-search-filters-button"]')
        if filters:
            if not await drawer.count():
                raise ValueError("HH UI has no filters panel")
            await drawer.click()
        aliases = {
            "employment": "employment_form",
            "schedule": "work_schedule_by_days",
        }
        for name, raw_values in filters.items():
            values = raw_values if isinstance(raw_values, list) else [raw_values]
            for value in values:
                actual_name = aliases.get(name, name)
                escaped_name = actual_name.replace('"', '\\"')
                escaped_value = str(value).replace('"', '\\"')
                control = self._page.locator(
                    f'input[name="{escaped_name}"][value="{escaped_value}"]'
                )
                if not await control.count() and name == "salary":
                    control = self._page.locator('[data-qa="search-filter-compensation-input"]')
                if not await control.count() and name == "excluded_text":
                    control = self._page.locator('[data-qa="filter-select-excluded_text"]')
                if not await control.count():
                    raise ValueError(f"filter is unavailable in HH UI: {name}={value}")
                kind = await control.first.get_attribute("type")
                if kind in {"checkbox", "radio"}:
                    await control.first.check()
                else:
                    await control.first.fill(str(value))
                verified.setdefault(name, []).append(str(value))
        submit = self._page.locator(
            '[data-qa="search-drawer-filters-submit"]' if filters else '[data-qa="search-button"]'
        )
        if not await submit.count():
            raise PageNotReady("HH search form has no submit control")
        await submit.first.click()
        await self._page.wait_for_load_state("domcontentloaded", timeout=self.timeout_ms)
        await self._wait_ready("search")
        source = await self._page.content()
        parsed = parse_search_page(source, self._page.url)
        parsed.applied_filters.update(verified)
        parsed.unchecked_parameters = [
            name for name in parsed.unchecked_parameters if name not in verified
        ]
        return parsed, self._page.url

    async def _raise_if_blocked(self) -> None:
        if await self._is_blocked():
            raise BrowserBlocked("HH requested human verification; resume in a visible browser")

    async def _is_blocked(self) -> bool:
        assert self._page is not None
        title = (await self._page.title()).lower()
        body = (await self._page.locator("body").inner_text(timeout=5_000)).lower()
        path = self._page.url.lower()
        challenge_selector = (
            'iframe[src*="captcha"], form[action*="captcha"], '
            '[data-qa="captcha"], [data-qa="account-captcha"]'
        )
        exact_phrases = (
            "подтвердите, что вы не робот",
            "доступ временно ограничен",
            "пройдите проверку, чтобы продолжить",
        )
        challenge = self._page.locator(challenge_selector)
        selector_visible = await challenge.count() > 0 and await challenge.first.is_visible()
        return (
            "/captcha" in path
            or "captcha" in title
            or selector_visible
            or any(phrase in body[:5000] for phrase in exact_phrases)
        )

    async def _wait_ready(self, readiness: str) -> None:
        assert self._page is not None
        selectors = {
            "search": (
                '[data-qa="serp-item__title"], '
                '[data-qa="vacancy-serp__vacancy-title"], '
                '[data-qa="vacancy-serp__empty"], '
                '[data-qa="search-result-empty"]'
            ),
            "vacancy": '[data-qa="vacancy-title"], [data-qa="vacancy-title-text"]',
            "employer": '[data-qa="company-header-title-name"], [data-qa="employer-name"]',
            "generic": "body",
        }
        if readiness == "search":
            outcome = self._page.locator(selectors["search"])
            await outcome.first.wait_for(state="attached", timeout=self.timeout_ms)
            loading = self._page.locator('[data-qa*="loading"], [aria-busy="true"], .bloko-loading')
            if await loading.count():
                await loading.first.wait_for(state="hidden", timeout=self.timeout_ms)
            empty = self._page.locator(
                '[data-qa="vacancy-serp__empty"], [data-qa="search-result-empty"]'
            )
            if await empty.count():
                return
            cards = self._page.locator('a[href*="/vacancy/"]')
            await wait_for_stable_nonzero_count(cards.count)
            return
        if readiness == "vacancy":
            terminal = self._page.locator(
                '[data-qa*="vacancy-archive"], [data-qa="vacancy-unavailable"]'
            )
            vacancy_outcome = self._page.locator(
                selectors["vacancy"]
                + ', [data-qa*="vacancy-archive"], [data-qa="vacancy-unavailable"]'
            )
            await vacancy_outcome.first.wait_for(state="attached", timeout=self.timeout_ms)
            if await terminal.count():
                return
            details = self._page.locator(
                '[data-qa="vacancy-description"], [data-qa="vacancy-company-name"]'
            )
            await details.first.wait_for(state="attached", timeout=self.timeout_ms)
            return
        await self._page.locator(selectors.get(readiness, "body")).first.wait_for(
            state="attached", timeout=self.timeout_ms
        )

    async def iter_search_pages(
        self, url: str, *, max_pages: int | None = None
    ) -> AsyncIterator[tuple[SearchPage, str]]:
        next_url = validate_search_url(url)
        seen_urls: set[str] = set()
        seen_signatures: set[tuple[str, ...]] = set()
        page_number = 0
        while next_url and (max_pages is None or page_number < max_pages):
            if next_url in seen_urls:
                raise PageNotReady("pagination repeated without progress")
            seen_urls.add(next_url)
            source, final_url = await self.fetch_html(next_url, readiness="search")
            parsed = parse_search_page(source, final_url)
            signature = tuple(sorted(item.hh_id for item in parsed.items))
            if signature and signature in seen_signatures:
                raise PageNotReady("search page repeated the same vacancy set")
            seen_signatures.add(signature)
            yield parsed, final_url
            next_url = parsed.next_url
            page_number += 1

    async def visible_resume(
        self, url: str, *, challenge_timeout: float = 600.0
    ) -> tuple[str, str]:
        if self.headless:
            raise RuntimeError("manual resume requires BrowserAdapter(headless=False)")
        if self._page is None:
            raise RuntimeError("BrowserAdapter must be used as an async context manager")
        await self._pace()
        self._last_navigation = time.monotonic()
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        deadline = time.monotonic() + challenge_timeout
        while await self._is_blocked():
            if time.monotonic() >= deadline:
                raise BrowserBlocked("human verification was not completed before timeout")
            await asyncio.sleep(1)
        await self._wait_ready("generic")
        return await self._page.content(), self._page.url
