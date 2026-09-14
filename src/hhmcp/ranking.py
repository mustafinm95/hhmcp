from __future__ import annotations

from typing import Any

from .models import CandidateProfile, Criterion, CriterionResult, RankingResult, Vacancy


def _value(v: Vacancy, path: str) -> Any:
    current: Any = v.model_dump()
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def evaluate(c: Criterion, actual: Any) -> tuple[str, str]:
    if actual is None:
        return "unknown", f"{c.field}: нет сопоставимых данных"
    if c.operator == "salary_minimum":
        if not isinstance(actual, dict):
            return "unknown", "зарплата не указана"
        if (
            actual.get("currency") != c.currency
            or actual.get("period") != c.period
            or actual.get("gross") != c.gross
        ):
            return "unknown", "валюта, период или налоговый режим несопоставимы"
        lower, upper, required = actual.get("lower"), actual.get("upper"), float(c.value)
        if lower is not None and lower >= required:
            return "match", "нижняя граница соответствует"
        if upper is not None and upper < required:
            return "miss", "верхняя граница ниже требования"
        return "unknown", "диапазон не подтверждает соответствие"
    if c.operator == "eq":
        ok = actual == c.value
    elif c.operator == "contains":
        values = actual if isinstance(actual, list) else [actual]
        ok = str(c.value).casefold() in {str(x).casefold() for x in values}
    elif c.operator == "gte":
        try:
            ok = float(actual) >= float(c.value)
        except (TypeError, ValueError):
            return "unknown", f"{c.field}: несопоставимые единицы"
    elif c.operator == "in":
        ok = actual in c.value
    else:
        return "unknown", "неизвестный оператор"
    return ("match" if ok else "miss"), f"{actual!r} {c.operator} {c.value!r}"


def rank(vacancy: Vacancy, profile: CandidateProfile) -> RankingResult:
    results: list[CriterionResult] = []
    weighted_match = known_weight = total_weight = 0.0
    required_miss = False
    required_unknown = False
    for c in profile.criteria:
        parts = c.field.split(".")
        state = vacancy.field_states.get(c.field) or vacancy.field_states.get(parts[-1])
        if state and state.state == "error":
            result, explanation = "unknown", f"{c.field}: ошибка текущего извлечения"
        else:
            result, explanation = evaluate(c, _value(vacancy, c.field))
        results.append(CriterionResult(criterion=c, result=result, explanation=explanation))
        if c.required:
            required_miss |= result == "miss"
            required_unknown |= result == "unknown"
        else:
            total_weight += c.weight
            if result != "unknown":
                known_weight += c.weight
            if result == "match":
                weighted_match += c.weight
    eligibility = (
        "не подходит"
        if required_miss
        else ("недостаточно данных" if required_unknown else "подходит")
    )
    return RankingResult(
        vacancy_id=vacancy.hh_id,
        eligibility=eligibility,
        score=round(100 * weighted_match / total_weight, 2) if total_weight else 0,
        completeness=round(100 * known_weight / total_weight, 2) if total_weight else 100,
        criteria=results,
    )


def salary_minimum(
    vacancy: Vacancy, amount: float, currency: str, period: str, gross: bool | None
) -> str:
    s = vacancy.salary
    if not s or s.currency != currency or s.period != period or s.gross != gross:
        return "unknown"
    if s.lower is not None and s.lower >= amount:
        return "match"
    if s.upper is not None and s.upper < amount:
        return "miss"
    return "unknown"
