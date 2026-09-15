from __future__ import annotations

import re
from typing import Any

from .models import Salary, Vacancy


def _contains(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _statement(
    text: str,
    positive: tuple[str, ...],
    negative: tuple[str, ...] = (),
) -> tuple[bool, str]:
    if negative and _contains(text, negative):
        return False, "confirmed_absent"
    if _contains(text, positive):
        return True, "confirmed"
    return False, "unknown"


def _evidence(text: str, pattern: str) -> str | None:
    match = re.search(pattern, text, re.I)
    if not match:
        return None
    start, end = max(0, match.start() - 70), min(len(text), match.end() + 100)
    return re.sub(r"\s+", " ", text[start:end]).strip()


def _team_size(text: str) -> dict[str, int | None]:
    match = re.search(
        r"(?:команд[аые]|подчинени[ияе]|руководств[оа])[^.\n]{0,60}?(\d+)(?:\s*[-–]\s*(\d+))?",
        text,
        re.I,
    )
    if not match:
        return {"min": None, "max": None}
    return {"min": int(match.group(1)), "max": int(match.group(2) or match.group(1))}


def _description_salary(text: str) -> Salary | None:
    match = re.search(
        r"(?P<lower>\d[\d\s]{3,})(?:\s*[-–—]\s*(?P<upper>\d[\d\s]{3,}))?\s*(?:₽|руб(?:лей|ля|\.)?)",
        text,
        re.I,
    )
    if not match:
        return None

    def number(value: str | None) -> float | None:
        return float(re.sub(r"\s+", "", value)) if value else None

    return Salary(
        lower=number(match.group("lower")),
        upper=number(match.group("upper")) or number(match.group("lower")),
        currency="RUR",
        period="month",
        gross=None,
    )


def analyze_vacancy(vacancy: Vacancy) -> tuple[dict[str, Any], dict[str, Any]]:
    text = f"{vacancy.title or ''}\n{vacancy.description or ''}"
    folded = text.casefold()
    has_team, team_state = _statement(
        folded,
        (r"в подчинени", r"руковод\w* команд", r"управлен\w* команд"),
        (
            r"(?:сотрудник\w*|подчиненн\w*|подчинённ\w*)[^.\n]{0,25}\bнет\b",
            r"\bнет\b[^.\n]{0,25}(?:сотрудник\w*|подчиненн\w*|подчинённ\w*)",
            r"без (?:своей )?команд",
            r"команд\w* (?:не будет|нет)",
        ),
    )
    function_owner = _contains(
        folded,
        (
            r"ответствен\w* за (?:всю )?(?:hr|эйчар)[- ]?функц",
            r"постро\w* hr[- ]?функц",
            r"возглав\w* hr",
        ),
    )
    reports_to_ceo = _contains(
        folded, (r"подчинени\w* (?:ceo|генеральн|собственник)", r"report\w* to ceo")
    )
    hands_on_kdp, hands_on_kdp_state = _statement(
        folded,
        (
            r"самостоятельн\w* ведени\w* кдп",
            r"вести кдп",
            r"кадров\w* делопроизводств\w* в полном объеме",
        ),
        (
            r"вести кдп[^.\n]{0,30}не (?:нужно|требуется|придется|придётся)",
            r"(?:не нужно|не требуется)[^.\n]{0,30}(?:вести )?кдп",
            r"без (?:самостоятельного )?ведени\w* кдп",
            r"кдп[^.\n]{0,40}(?:ведет|ведёт|занимается) (?:отдельн\w* )?специалист",
        ),
    )
    kdp_oversight = _contains(
        folded,
        (
            r"контрол\w* кдп",
            r"в подчинени\w*[^.\n]{0,120}специалист\w* по кадров",
            r"контрол\w* кадров\w* делопроизводств",
        ),
    )
    labor_law = _contains(folded, (r"трудов\w* законодательств", r"тк рф"))
    remote_denied = _contains(
        folded,
        (
            r"удален\w* работ\w* не предусмотр",
            r"удалён\w* работ\w* не предусмотр",
            r"удален\w* формат\w* (?:нет|не предусмотр)",
            r"удалён\w* формат\w* (?:нет|не предусмотр)",
            r"только (?:работа )?в офис",
            r"исключительно офис",
        ),
    )
    temporary_remote = _contains(
        folded,
        (r"временно\w* удал", r"после .*?(?:офис|гибрид)", r"с последующ\w* выход\w* в офис"),
    )
    hybrid = _contains(folded, (r"гибрид", r"\d\s*(?:дн|раз)\w* в (?:недел|офис)"))
    remote = _contains(folded, (r"удален", r"удалён", r"remote"))
    if remote_denied:
        current, permanent, future = "on_site", True, None
    elif temporary_remote:
        current, permanent, future = "remote", False, "hybrid" if hybrid else "on_site"
    elif hybrid:
        current, permanent, future = "hybrid", True, None
    elif remote:
        current, permanent, future = "remote", True, None
    else:
        current, permanent, future = "unknown", None, None
    office_days_match = re.search(r"(\d)\s*(?:дн\w*|раз\w*)[^.\n]{0,30}в офис", folded)
    team_size = _team_size(text)
    if function_owner and reports_to_ceo:
        role_level = "hr_director"
    elif function_owner:
        role_level = "function_lead"
    elif has_team:
        role_level = "team_lead"
    elif _contains(folded, (r"ведущ", r"senior", r"старш")):
        role_level = "senior_individual_contributor"
    else:
        role_level = "individual_contributor"
    assessment = {
        "leadership": {
            "has_team": has_team,
            "has_team_state": team_state,
            "team_size": team_size,
            "responsibility": "function_owner"
            if function_owner
            else ("team_manager" if has_team else "individual"),
            "role_level": role_level,
            "reporting_line": "CEO" if reports_to_ceo else "unknown",
        },
        "recruitment": {
            "mass": _contains(folded, (r"массов\w* подбор",)),
            "professional": _contains(folded, (r"точечн\w* подбор", r"профессиональн\w* подбор")),
            "it": _contains(folded, (r"it[- ]?подбор", r"техническ\w* подбор")),
            "executive_search": _contains(
                folded, (r"executive search", r"подбор\w* топ[- ]?менедж")
            ),
            "personal_hiring_required": _contains(
                folded, (r"личн\w* закрыва", r"самостоятельн\w* подбор", r"вести ваканс")
            ),
        },
        "hr_scope": [
            name
            for name, patterns in {
                "adaptation": (r"адаптац", r"онбординг"),
                "retention": (r"удержан",),
                "engagement": (r"вовлеченн", r"вовлечённ"),
                "analytics": (r"hr[- ]?аналит", r"аналитик\w* персонал"),
                "automation": (
                    r"автоматизац",
                    r"hr tech",
                    r"hrtech",
                    r"\bats\b",
                    r"искусственн\w* интеллект",
                    r"\bai\b",
                ),
                "assessment": (r"оценк\w* персонал",),
                "development": (r"развити\w* персонал", r"l&d"),
                "compensation_benefits": (r"c&b", r"компенсац", r"льгот"),
                "budgeting": (r"бюджетир", r"hr[- ]?бюджет"),
            }.items()
            if _contains(folded, patterns)
        ],
        "kdp": {
            "hands_on": hands_on_kdp,
            "hands_on_state": hands_on_kdp_state,
            "oversight": kdp_oversight,
            "labor_law_knowledge": labor_law,
            "state": (
                "confirmed"
                if hands_on_kdp or kdp_oversight or labor_law
                else ("confirmed_absent" if hands_on_kdp_state == "confirmed_absent" else "unknown")
            ),
            "evidence": _evidence(text, r"кдп|кадров\w* делопроизводств|трудов\w* законодательств"),
        },
        "work_format": {
            "current": current,
            "permanent": permanent,
            "future": future,
            "remote_geography": "Russia"
            if _contains(folded, (r"по россии", r"из россии", r"рф"))
            else "unknown",
            "office_days_per_week": int(office_days_match.group(1)) if office_days_match else None,
            "office_location": vacancy.address,
            "confidence": "confirmed_from_description" if current != "unknown" else "unknown",
            "regular_travel": _contains(folded, (r"регулярн\w* командиров", r"част\w* командиров")),
            "relocation_required": _contains(
                folded, (r"обязательн\w* переезд", r"релокац\w* обязатель")
            ),
        },
    }
    description_salary = _description_salary(vacancy.description or "")
    salary_conflict = bool(
        vacancy.salary
        and description_salary
        and vacancy.salary.lower is not None
        and description_salary.lower is not None
        and vacancy.salary.lower != description_salary.lower
    )
    declared_format = str(vacancy.conditions.get("work_format") or "").casefold()
    format_conflict = bool(
        declared_format
        and current != "unknown"
        and (
            ("удал" in declared_format and current != "remote")
            or ("гибрид" in declared_format and current != "hybrid")
        )
    )
    employment = str(vacancy.conditions.get("employment") or "").casefold()
    schedule = str(vacancy.conditions.get("schedule") or "").casefold()
    employment_conflict = bool(
        "полная" in employment and _contains(folded, (r"частичн\w* занятост", r"неполн\w* день"))
    )
    schedule_conflict = bool(
        "полный день" in schedule and _contains(folded, (r"сменн\w* график", r"вахтов\w* метод"))
    )
    role_title_conflict = bool(
        _contains(vacancy.title or "", (r"\bhrd\b", r"директор\w* по персонал", r"head of hr"))
        and role_level in {"individual_contributor", "senior_individual_contributor"}
    )
    diagnostics = {
        "salary_conflict": salary_conflict,
        "published_salary": vacancy.salary.model_dump(mode="json") if vacancy.salary else None,
        "description_salary": description_salary.model_dump(mode="json")
        if description_salary
        else None,
        "work_format_conflict": format_conflict,
        "employment_conflict": employment_conflict,
        "schedule_conflict": schedule_conflict,
        "role_title_conflict": role_title_conflict,
        "verification_required": any(
            (
                salary_conflict,
                format_conflict,
                employment_conflict,
                schedule_conflict,
                role_title_conflict,
            )
        ),
    }
    return assessment, diagnostics
