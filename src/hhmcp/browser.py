"""Rate-limited Playwright adapter for public HH pages."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlparse

from .parsing import SearchPage, parse_search_page, validate_search_url


class BrowserBlocked(RuntimeError):
    """HH displayed a CAPTCHA or access-block page."""


class PageNotReady(RuntimeError):
    """The page did not reach an explicit, parseable state."""


class NavigationLimiter:
    """Space navigation starts globally and back off all workers together."""

    def __init__(self, interval: float = 1.0) -> None:
        self.interval = interval
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        while True:
            async with self._lock:
                delay = self._next_start - time.monotonic()
                if delay <= 0:
                    self._next_start = time.monotonic() + self.interval
                    return
            await asyncio.sleep(delay)

    async def backoff(self, seconds: float) -> None:
        async with self._lock:
            self._next_start = max(self._next_start, time.monotonic() + seconds)


def retained_search_filters(
    request_url: str, final_url: str
) -> tuple[dict[str, list[str]], list[str]]:
    ignored = {"page", "search_session_id", "hhtmFrom"}
    requested = {
        key: values
        for key, values in parse_qs(urlparse(request_url).query).items()
        if key not in ignored
    }
    final_parameters = parse_qs(urlparse(final_url).query)
    retained = {
        key: values
        for key, values in requested.items()
        if sorted(final_parameters.get(key, [])) == sorted(values)
    }
    return retained, sorted(set(requested) - set(retained))


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
        min_delay: float = 1.0,
        retries: int = 3,
        timeout_ms: int = 30_000,
        profile_dir: str | Path | None = None,
        _limiter: NavigationLimiter | None = None,
    ) -> None:
        if min_delay < 1:
            raise ValueError("min_delay must be at least 1 second")
        self.headless = headless
        self.min_delay = min_delay
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.profile_dir = Path(profile_dir or Path.home() / ".hhmcp" / "browser-profile")
        self._playwright = self._page = None
        self._context = None
        self._limiter = _limiter or NavigationLimiter(min_delay)
        self.last_status: int | None = None
        self.last_availability: str | None = None
        self.last_visible_fields: dict | None = None

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
        await self._limiter.wait()

    async def new_page_adapter(self) -> BrowserAdapter:
        if self._context is None:
            raise RuntimeError("BrowserAdapter must be used as an async context manager")
        child = BrowserAdapter(
            headless=self.headless,
            min_delay=self.min_delay,
            retries=self.retries,
            timeout_ms=self.timeout_ms,
            profile_dir=self.profile_dir,
            _limiter=self._limiter,
        )
        child._context = self._context
        child._page = await self._context.new_page()
        return child

    async def close_page(self) -> None:
        if self._page is not None:
            await self._page.close()
            self._page = None

    async def fetch_html(self, url: str, *, readiness: str = "generic") -> tuple[str, str]:
        if self._page is None:
            raise RuntimeError("BrowserAdapter must be used as an async context manager")
        error: Exception | None = None
        for attempt in range(self.retries):
            await self._pace()
            try:
                response = await self._page.goto(
                    url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                self.last_status = response.status if response else None
                if self.last_status in {429, 503}:
                    headers = await response.all_headers() if response else {}
                    retry_after = headers.get("retry-after")
                    delay = float(2**attempt)
                    if retry_after:
                        try:
                            delay = float(retry_after)
                        except ValueError:
                            with suppress(TypeError, ValueError):
                                delay = max(
                                    0.0,
                                    parsedate_to_datetime(retry_after).timestamp() - time.time(),
                                )
                    await self._limiter.backoff(max(delay, 1.0))
                    raise PageNotReady(f"HH returned HTTP {self.last_status}")
                await self._raise_if_blocked()
                if self.last_status not in {404, 410}:
                    await self._wait_ready(readiness)
                await self._raise_if_blocked()
                self.last_availability = await self._visible_availability(readiness)
                self.last_visible_fields = await self._visible_vacancy_fields(readiness)
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

    async def _visible_availability(self, readiness: str) -> str | None:
        if readiness != "vacancy" or self._page is None:
            return None
        if self.last_status in {404, 410}:
            return "unavailable"
        archived = self._page.locator(
            '[data-qa*="vacancy-archive"]:visible, [data-qa="vacancy-archived"]:visible'
        )
        if await archived.count():
            return "archived"
        unavailable = self._page.locator(
            '[data-qa="vacancy-unavailable"]:visible, [data-qa="vacancy-not-found"]:visible'
        )
        if await unavailable.count():
            return "unavailable"
        title = self._page.locator(
            '[data-qa="vacancy-title"]:visible, [data-qa="vacancy-title-text"]:visible'
        )
        description = self._page.locator('[data-qa="vacancy-description"]:visible')
        if await title.count() and await description.count():
            return "active"
        return "unknown"

    async def _visible_vacancy_fields(self, readiness: str) -> dict | None:
        if readiness != "vacancy" or self._page is None or self.last_status in {404, 410}:
            return None
        qas = {
            "experience": ("vacancy-experience", "vacancy-view-experience"),
            "employment": ("vacancy-employment", "vacancy-view-employment-mode"),
            "schedule": ("vacancy-schedule", "vacancy-work-schedule-by-days"),
            "hours": ("vacancy-working-hours", "working-hours", "vacancy-working-hours-text"),
            "work_format": ("vacancy-work-format", "work-format", "vacancy-work-format-text"),
        }
        conditions = {}
        for name, values in qas.items():
            selector = ", ".join(f'[data-qa="{value}"]:visible' for value in values)
            locator = self._page.locator(selector)
            if await locator.count():
                value = (await locator.first.inner_text()).strip()
                if value:
                    conditions[name] = value
        publication = self._page.locator(
            '[data-qa="vacancy-creation-time"]:visible, '
            '[data-qa="vacancy-creation-time-redesigned"]:visible, '
            '[data-qa="vacancy-view-creation-time"]:visible'
        )
        result: dict[str, Any] = {"conditions": conditions}
        if await publication.count():
            node = publication.first
            result["published_text"] = (await node.inner_text()).strip()
            result["published_at"] = await node.get_attribute(
                "datetime"
            ) or await node.get_attribute("content")
        return result

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
                '[data-qa*="vacancy-archive"]:visible, '
                '[data-qa="vacancy-unavailable"]:visible, '
                '[data-qa="vacancy-not-found"]:visible'
            )
            vacancy_outcome = self._page.locator(
                '[data-qa="vacancy-title"]:visible, '
                '[data-qa="vacancy-title-text"]:visible, '
                '[data-qa*="vacancy-archive"]:visible, '
                '[data-qa="vacancy-unavailable"]:visible, '
                '[data-qa="vacancy-not-found"]:visible'
            )
            await vacancy_outcome.first.wait_for(state="visible", timeout=self.timeout_ms)
            if await terminal.count():
                return
            details = self._page.locator(
                '[data-qa="vacancy-description"]:visible, [data-qa="vacancy-company-name"]:visible'
            )
            await details.first.wait_for(state="visible", timeout=self.timeout_ms)
            return
        await self._page.locator(selectors.get(readiness, "body")).first.wait_for(
            state="attached", timeout=self.timeout_ms
        )

    async def iter_search_pages(
        self, url: str, *, max_pages: int | None = None
    ) -> AsyncIterator[tuple[SearchPage, str]]:
        next_url = validate_search_url(url)
        request_url = next_url
        seen_urls: set[str] = set()
        seen_signatures: set[tuple[str, ...]] = set()
        page_number = 0
        while next_url and (max_pages is None or page_number < max_pages):
            if next_url in seen_urls:
                raise PageNotReady("pagination repeated without progress")
            seen_urls.add(next_url)
            source, final_url = await self.fetch_html(next_url, readiness="search")
            parsed = parse_search_page(source, final_url)
            retained, missing = retained_search_filters(request_url, final_url)
            parsed.applied_filters.update(retained)
            parsed.unchecked_parameters = missing
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
        await self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        deadline = time.monotonic() + challenge_timeout
        while await self._is_blocked():
            if time.monotonic() >= deadline:
                raise BrowserBlocked("human verification was not completed before timeout")
            await asyncio.sleep(1)
        await self._wait_ready("generic")
        return await self._page.content(), self._page.url
