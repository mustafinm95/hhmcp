"""Pure HTML parsers for public HH pages.

The selectors intentionally prefer HH's ``data-qa`` attributes.  Text and
JSON-LD fallbacks keep fixtures and lightly branded pages parseable without
executing scripts from vacancy descriptions.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import parse_qs, urljoin, urlparse

HH_HOST_RE = re.compile(r"(?:^|\.)hh\.ru$", re.IGNORECASE)
VACANCY_ID_RE = re.compile(r"/vacancy/(\d+)(?:[/?#]|$)")


class InvalidHHUrl(ValueError):
    pass


def validate_search_url(url: str) -> str:
    """Return *url* when it is a public HTTPS HH vacancy-search URL."""
    parsed = urlparse(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or not HH_HOST_RE.search(parsed.hostname)
        or parsed.path.rstrip("/") != "/search/vacancy"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise InvalidHHUrl("expected an HTTPS hh.ru search/vacancy URL")
    return url


@dataclass(slots=True)
class SearchItem:
    hh_id: str
    url: str
    title: str | None = None
    employer_name: str | None = None
    salary_text: str | None = None
    published_text: str | None = None
    conditions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class SearchPage:
    items: list[SearchItem]
    next_url: str | None
    applied_filters: dict[str, list[str]] = field(default_factory=dict)
    is_empty: bool = False
    blocked: bool = False
    unchecked_parameters: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ExtractedField:
    value: object = None
    state: str = "absent"
    error: str | None = None
    source: str | None = None


@dataclass(slots=True)
class ParsedSalary:
    lower: float | None
    upper: float | None
    currency: str | None
    period: str | None
    gross: bool | None


@dataclass(slots=True)
class VacancyPage:
    hh_id: str | None
    title: str | None
    description_html: str | None
    description_text: str | None
    employer_name: str | None
    employer_id: str | None
    employer_url: str | None
    salary_text: str | None
    address: str | None
    metro: str | None
    published_text: str | None
    skills: list[str]
    contacts: str | None
    archived: bool
    unavailable: bool = False
    availability: str = "unknown"
    salary: ParsedSalary | None = None
    conditions: dict[str, str] = field(default_factory=dict)
    department_name: str | None = None
    field_states: dict[str, ExtractedField] = field(default_factory=dict)
    published_at: datetime | None = None


@dataclass(slots=True)
class EmployerPage:
    hh_id: str | None
    name: str | None
    description_html: str | None
    description_text: str | None
    website: str | None


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None = None
    children: list[_Node] = field(default_factory=list)
    chunks: list[str] = field(default_factory=list)
    content: list[object] = field(default_factory=list)

    @property
    def text(self) -> str:
        if self.tag in {"script", "style"}:
            return ""
        value = " ".join(item if isinstance(item, str) else item.text for item in self.content)
        return re.sub(r"\s+", " ", html.unescape(value)).strip()

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class _TreeParser(HTMLParser):
    VOID = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "wbr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, dict(attrs), self.stack[-1])
        self.stack[-1].children.append(node)
        self.stack[-1].content.append(node)
        if tag not in self.VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        if data.strip():
            self.stack[-1].chunks.append(data)
            self.stack[-1].content.append(data)


def _tree(source: str) -> _Node:
    parser = _TreeParser()
    parser.feed(source)
    return parser.root


def _qa(node: _Node, *values: str) -> bool:
    qa = node.attrs.get("data-qa", "")
    return any(value == qa or value in qa.split() for value in values)


def _statically_hidden(node: _Node) -> bool:
    current: _Node | None = node
    while current is not None:
        style = current.attrs.get("style", "").replace(" ", "").casefold()
        if (
            "hidden" in current.attrs
            or current.attrs.get("aria-hidden", "").casefold() == "true"
            or "display:none" in style
            or "visibility:hidden" in style
        ):
            return True
        current = current.parent
    return False


def _first(root: _Node, *qas: str) -> _Node | None:
    return next(
        (node for node in root.descendants() if _qa(node, *qas) and not _statically_hidden(node)),
        None,
    )


def _inner_html(node: _Node | None) -> str | None:
    if node is None:
        return None

    def render(current: _Node) -> str:
        attrs = "".join(
            f' {k}="{html.escape(v, quote=True)}"'
            for k, v in current.attrs.items()
            if not k.lower().startswith("on")
        )
        body = "".join(
            html.escape(item) if isinstance(item, str) else render(item)
            for item in current.content
            if not isinstance(item, _Node) or item.tag not in {"script", "style"}
        )
        return f"<{current.tag}{attrs}>{body}</{current.tag}>"

    return "".join(
        html.escape(item) if isinstance(item, str) else render(item)
        for item in node.content
        if not isinstance(item, _Node) or item.tag not in {"script", "style"}
    )


def _href(node: _Node | None, base_url: str) -> str | None:
    return urljoin(base_url, node.attrs["href"]) if node and node.attrs.get("href") else None


def _field(root: _Node, *qas: str) -> ExtractedField:
    node = _first(root, *qas)
    if node is None:
        return ExtractedField()
    value = node.text
    if not value:
        return ExtractedField(state="error", error="matched element is empty")
    return ExtractedField(value=value, state="value", source="dom")


def _raw_text(node: _Node) -> str:
    return "".join(item if isinstance(item, str) else _raw_text(item) for item in node.content)


def _json_ld_job(root: _Node) -> dict:
    def find_job(value):
        if isinstance(value, dict):
            kind = value.get("@type")
            if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
                return value
            for child in value.values():
                found = find_job(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_job(child)
                if found:
                    return found
        return None

    for node in root.descendants():
        if node.tag != "script" or "ld+json" not in node.attrs.get("type", "").casefold():
            continue
        try:
            found = find_job(json.loads(_raw_text(node)))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if found:
            return found
    return {}


def _conditions_from_text(text: str) -> dict[str, str]:
    normalized = re.sub(r"\s+", " ", text).strip().casefold()
    result: dict[str, str] = {}
    patterns = {
        "employment": (
            (r"\bполная занятость\b", "Полная занятость"),
            (r"\bчастичная занятость\b", "Частичная занятость"),
        ),
        "work_format": (
            (r"\b(?:формат работы\s*[:—-]?\s*)?гибрид(?:ный формат)?\b", "Гибрид"),
            (r"\bудал[её]нная работа\b|\bформат работы\s*[:—-]?\s*удал[её]н", "Удалённо"),
            (r"\bработа в офисе\b|\bформат работы\s*[:—-]?\s*офис", "В офисе"),
        ),
    }
    for field_name, candidates in patterns.items():
        for pattern, label in candidates:
            if re.search(pattern, normalized):
                result[field_name] = label
                break
    schedule = re.search(r"\bграфик(?: работы)?\s*[:—-]?\s*(\d\s*/\s*\d)\b", normalized)
    if schedule:
        result["schedule"] = schedule.group(1).replace(" ", "")
    elif re.search(r"\bполный день\b", normalized):
        result["schedule"] = "Полный день"
    elif re.search(r"\bсменный график\b", normalized):
        result["schedule"] = "Сменный график"
    elif re.search(r"\bгибкий график\b", normalized):
        result["schedule"] = "Гибкий график"
    hours = re.search(
        r"\b(?:рабоч(?:ий день|ие часы)|занятость)\s*[:—-]?\s*(\d{1,2})\s*час"
        r"|\b(\d{1,2})\s*час(?:ов|а)?\s+в день\b",
        normalized,
    )
    if hours:
        result["hours"] = f"{hours.group(1) or hours.group(2)} часов"
    return result


def _published_at(root: _Node) -> datetime | None:
    node = _first(
        root,
        "vacancy-creation-time",
        "vacancy-creation-time-redesigned",
        "vacancy-view-creation-time",
    )
    raw = (node.attrs.get("datetime") or node.attrs.get("content")) if node else None
    if raw:
        try:
            value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        except ValueError:
            pass
    if not node:
        return None
    text = node.text.casefold()
    now = datetime.now(UTC)
    if "сегодня" in text:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "вчера" in text:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    match = re.search(r"(\d{1,2})\s+([а-яё]+)(?:\s+(\d{4}))?", text)
    if match and match.group(2) in months:
        return datetime(
            int(match.group(3) or now.year),
            months[match.group(2)],
            int(match.group(1)),
            tzinfo=UTC,
        )
    return None


def parse_salary(text: str | None) -> ParsedSalary | None:
    """Parse HH salary text without converting currency, period, or tax mode."""
    if not text or not text.strip():
        return None
    value = re.sub(r"[\u00a0\u202f\s]+", " ", html.unescape(text)).strip()
    currency = None
    currencies = ((r"₽|руб", "RUB"), (r"\$|USD", "USD"), (r"€|EUR", "EUR"), (r"₸|KZT|тенге", "KZT"))
    for pattern, code in currencies:
        if re.search(pattern, value, re.IGNORECASE):
            currency = code
            break
    period = None
    lowered = value.lower()
    periods = {
        "hour": ("час", "hour"),
        "day": ("день", "дня", "day"),
        "week": ("недел", "week"),
        "month": ("месяц", "month"),
        "year": ("год", "year"),
    }
    for candidate, markers in periods.items():
        if any(marker in lowered for marker in markers):
            period = candidate
            break
    gross = True if "до вычета" in lowered or "gross" in lowered else None
    if "на руки" in lowered or "net" in lowered:
        gross = False
    numbers = []
    for token in re.findall(r"\d[\d \u00a0\u202f]*(?:[.,]\d+)?", value):
        normalized = re.sub(r"[ \u00a0\u202f]", "", token).replace(",", ".")
        numbers.append(float(normalized))
    if not numbers:
        return None
    lower = upper = None
    if re.search(r"\bот\b|\bfrom\b", lowered):
        lower, upper = numbers[0], numbers[1] if len(numbers) > 1 else None
    elif re.search(r"\bдо\b(?!\s+вычета)|\bup to\b", lowered):
        upper = numbers[0]
    elif len(numbers) >= 2:
        lower, upper = numbers[:2]
    else:
        lower = upper = numbers[0]
    return ParsedSalary(lower, upper, currency, period, gross)


def parse_search_page(source: str, base_url: str) -> SearchPage:
    root = _tree(source)
    found: dict[str, SearchItem] = {}
    for node in root.descendants():
        href = node.attrs.get("href", "")
        match = VACANCY_ID_RE.search(href)
        if not match or node.tag != "a":
            continue
        ancestors, cursor = [], node.parent
        while cursor is not None:
            ancestors.append(cursor)
            cursor = cursor.parent
        context = next(
            (
                a
                for a in ancestors
                if "vacancy-serp" in a.attrs.get("data-qa", "")
                or "serp-item" in a.attrs.get("class", "")
            ),
            node.parent or node,
        )
        markers = " ".join(
            a.attrs.get("data-qa", "") + " " + a.attrs.get("class", "")
            for a in [context, *ancestors[:3]]
        ).lower()
        if any(word in markers for word in ("advert", "banner", "recommendation", "promo")):
            continue
        hh_id = match.group(1)
        title_node = _first(context, "serp-item__title", "vacancy-serp__vacancy-title")
        employer = _first(context, "vacancy-serp__vacancy-employer")
        salary = _first(
            context, "vacancy-serp__vacancy-compensation", "vacancy-serp__vacancy-salary"
        )
        published = _first(
            context, "vacancy-serp__vacancy-date", "vacancy-serp__publication-date"
        )
        found.setdefault(
            hh_id,
            SearchItem(
                hh_id,
                urljoin(base_url, href),
                (title_node or node).text or None,
                employer.text if employer else None,
                salary.text if salary else None,
                published.text if published else None,
                _conditions_from_text(context.text),
            ),
        )

    next_node = next(
        (
            n
            for n in root.descendants()
            if n.tag == "a" and (_qa(n, "pager-next") or n.attrs.get("rel") == "next")
        ),
        None,
    )
    if next_node is None:
        current_page = int(parse_qs(urlparse(base_url).query).get("page", ["0"])[0])
        numbered: list[tuple[int, _Node]] = []
        for node in root.descendants():
            href = node.attrs.get("href", "")
            if node.tag != "a" or "/search/vacancy" not in href:
                continue
            try:
                number = int(
                    parse_qs(urlparse(urljoin(base_url, href)).query).get("page", ["-1"])[0]
                )
            except ValueError:
                continue
            if number > current_page:
                numbered.append((number, node))
        if numbered:
            next_node = min(numbered, key=lambda item: item[0])[1]
    empty = _first(root, "vacancy-serp__empty", "search-result-empty") is not None
    filters: dict[str, list[str]] = {}
    for node in root.descendants():
        if node.tag == "input" and "checked" in node.attrs and node.attrs.get("name"):
            filters.setdefault(node.attrs["name"], []).append(node.attrs.get("value", ""))
        elif node.attrs.get("aria-checked") == "true" and node.attrs.get("data-qa"):
            key = node.attrs["data-qa"]
            filters.setdefault(key, []).append(node.attrs.get("value", node.text))
    query = parse_qs(urlparse(base_url).query)
    unchecked = sorted(key for key, values in query.items() if filters.get(key) != values)
    return SearchPage(
        list(found.values()),
        _href(next_node, base_url),
        filters,
        empty,
        unchecked_parameters=unchecked,
    )


def parse_vacancy_page(
    source: str,
    url: str,
    *,
    availability: str | None = None,
    http_status: int | None = None,
    fallback_fields: dict | None = None,
    visible_fields: dict | None = None,
) -> VacancyPage:
    root = _tree(source)
    fallback_fields = fallback_fields or {}
    json_ld = _json_ld_job(root)
    match = VACANCY_ID_RE.search(url)
    employer = _first(root, "vacancy-company-name", "vacancy-company-name-text")
    employer_link = (
        employer
        if employer and employer.tag == "a"
        else next((n for n in (employer.descendants() if employer else []) if n.tag == "a"), None)
    )
    employer_url = _href(employer_link, url)
    employer_match = re.search(r"/employer/(\d+)", employer_url or "")
    description = _first(root, "vacancy-description")
    skills = []
    for node in root.descendants():
        if (
            _qa(node, "skills-element", "bloko-tag__section_text")
            and node.text
            and node.text not in skills
        ):
            skills.append(node.text)
    archived_nodes = [
        n
        for n in root.descendants()
        if _qa(n, "vacancy-archive", "vacancy-archived") and not _statically_hidden(n)
    ]
    unavailable_nodes = [
        n
        for n in root.descendants()
        if _qa(n, "vacancy-unavailable", "vacancy-not-found") and not _statically_hidden(n)
    ]
    leaf_texts = {
        n.text.casefold()
        for n in root.descendants()
        if not n.children and not _statically_hidden(n)
    }
    archived = bool(archived_nodes) or "вакансия в архиве" in leaf_texts
    unavailable = bool(unavailable_nodes) or bool(
        leaf_texts
        & {"вакансия недоступна", "вакансия удалена", "страница не найдена"}
    )
    title_field = _field(root, "vacancy-title", "vacancy-title-text")
    salary_field = _field(root, "vacancy-salary", "vacancy-salary-compensation-type-net")
    condition_qas = {
        "experience": ("vacancy-experience", "vacancy-view-experience"),
        "employment": ("vacancy-employment", "vacancy-view-employment-mode"),
        "schedule": ("vacancy-schedule", "vacancy-work-schedule-by-days"),
        "hours": ("vacancy-working-hours", "working-hours", "vacancy-working-hours-text"),
        "work_format": ("vacancy-work-format", "work-format", "vacancy-work-format-text"),
    }
    if visible_fields is None:
        condition_fields = {key: _field(root, *qas) for key, qas in condition_qas.items()}
    else:
        visible_conditions = visible_fields.get("conditions") or {}
        condition_fields = {
            key: (
                ExtractedField(value=visible_conditions[key], state="value", source="dom")
                if visible_conditions.get(key)
                else ExtractedField()
            )
            for key in condition_qas
        }
    condition_block = " ".join(
        str(item.value) for item in condition_fields.values() if item.state == "value"
    )
    for key, extracted in condition_fields.items():
        if extracted.state != "value":
            continue
        normalized_value = _conditions_from_text(str(extracted.value)).get(key)
        if normalized_value:
            extracted.value = normalized_value
    description_conditions = _conditions_from_text(description.text if description else "")
    page_conditions = _conditions_from_text(condition_block)
    json_conditions: dict[str, str] = {}
    employment_type = json_ld.get("employmentType")
    if isinstance(employment_type, list):
        employment_type = employment_type[0] if employment_type else None
    employment_labels = {
        "FULL_TIME": "Полная занятость",
        "PART_TIME": "Частичная занятость",
        "CONTRACTOR": "Проектная работа",
        "TEMPORARY": "Временная работа",
    }
    if isinstance(employment_type, str):
        json_conditions["employment"] = employment_labels.get(employment_type, employment_type)
    if str(json_ld.get("jobLocationType", "")).casefold() == "telecommute":
        json_conditions["work_format"] = "Удалённо"
    listing_conditions = fallback_fields.get("conditions") or {}
    for key, extracted in condition_fields.items():
        if extracted.state == "value":
            continue
        for candidate, source_name in (
            (page_conditions.get(key), "dom"),
            (json_conditions.get(key), "json_ld"),
            (listing_conditions.get(key), "listing"),
            (description_conditions.get(key), "description"),
        ):
            if candidate:
                condition_fields[key] = ExtractedField(
                    value=str(candidate), state="value", source=source_name
                )
                break

    published_at = _published_at(root)
    published_qas = (
        "vacancy-creation-time",
        "vacancy-creation-time-redesigned",
        "vacancy-view-creation-time",
    )
    published_node = _first(root, *published_qas)
    published_text = published_node.text if published_node else None
    published_state = _field(root, *published_qas)
    if visible_fields is not None:
        published_text = visible_fields.get("published_text") or None
        visible_published = visible_fields.get("published_at") or published_text
        published_at = None
        published_state = ExtractedField()
        if visible_published:
            synthetic = _tree(
                f'<time data-qa="vacancy-creation-time" '
                f'datetime="{html.escape(str(visible_published), quote=True)}">'
                f"{html.escape(str(visible_published))}</time>"
            )
            published_at = _published_at(synthetic)
            published_state = ExtractedField(
                value=str(visible_published),
                state="value" if published_at else "error",
                error=None if published_at else "publication date could not be parsed",
                source="dom",
            )
    if published_at is None:
        for raw, source_name in (
            (json_ld.get("datePosted") or json_ld.get("datePublished"), "json_ld"),
            (fallback_fields.get("published_at"), "listing"),
            (fallback_fields.get("published_text"), "listing"),
        ):
            if not raw:
                continue
            synthetic = _tree(
                f'<time data-qa="vacancy-creation-time" datetime="{html.escape(str(raw), quote=True)}">'
                f"{html.escape(str(raw))}</time>"
            )
            value = _published_at(synthetic)
            if value is not None:
                published_at = value
                published_text = published_text or str(raw)
                published_state = ExtractedField(
                    value=str(raw), state="value", source=source_name
                )
                break
    if published_at is None and published_state.state == "value":
        published_state = ExtractedField(
            value=published_state.value,
            state="error",
            error="publication date could not be parsed",
            source="dom",
        )
    field_states = {
        "title": title_field,
        "description": _field(root, "vacancy-description"),
        "salary": salary_field,
        "address": _field(root, "vacancy-view-raw-address"),
        "metro": _field(root, "vacancy-view-raw-address-metro-station"),
        "published_at": published_state,
        "contacts": _field(root, "vacancy-contacts"),
        **condition_fields,
    }
    department_name = None
    if employer and employer.attrs.get("data-department"):
        department_name = employer.attrs["data-department"]
    if http_status in {404, 410}:
        availability = "unavailable"
    elif availability is None:
        if archived:
            availability = "archived"
        elif unavailable:
            availability = "unavailable"
        elif title_field.state == "value" and description is not None and description.text:
            availability = "active"
        else:
            availability = "unknown"
    archived = availability == "archived"
    unavailable = availability == "unavailable"
    return VacancyPage(
        match.group(1) if match else None,
        str(title_field.value) if title_field.state == "value" else None,
        _inner_html(description),
        description.text if description else None,
        employer.text if employer else None,
        employer_match.group(1) if employer_match else None,
        employer_url,
        str(salary_field.value) if salary_field.state == "value" else None,
        _first(root, "vacancy-view-raw-address").text
        if _first(root, "vacancy-view-raw-address")
        else None,
        _first(root, "vacancy-view-raw-address-metro-station").text
        if _first(root, "vacancy-view-raw-address-metro-station")
        else None,
        published_text,
        skills,
        _first(root, "vacancy-contacts").text if _first(root, "vacancy-contacts") else None,
        archived,
        unavailable,
        availability,
        parse_salary(str(salary_field.value)) if salary_field.state == "value" else None,
        {key: str(item.value) for key, item in condition_fields.items() if item.state == "value"},
        department_name,
        field_states,
        published_at,
    )


def parse_employer_page(source: str, url: str) -> EmployerPage:
    root = _tree(source)
    match = re.search(r"/employer/(\d+)", url)
    description = _first(root, "company-description", "employer-description")
    website_node = _first(root, "company-site", "employer-site")
    return EmployerPage(
        match.group(1) if match else None,
        (_first(root, "company-header-title-name", "employer-name")).text
        if _first(root, "company-header-title-name", "employer-name")
        else None,
        _inner_html(description),
        description.text if description else None,
        _href(website_node, url) or (website_node.text if website_node else None),
    )
