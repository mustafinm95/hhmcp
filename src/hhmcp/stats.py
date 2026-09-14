from __future__ import annotations

from collections import Counter, defaultdict


def calculate(rows: list[dict]) -> dict:
    employers, regions, skills = Counter(), Counter(), Counter()
    salaries: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        employers[row.get("employer_name") or "не указан"] += 1
        address = row.get("address") or "не указан"
        regions[address.split(",", 1)[0].strip()] += 1
        skills.update(row.get("skills") or [])
        s = row.get("salary")
        if s and (s.get("lower") is not None or s.get("upper") is not None):
            key = f"{s.get('currency')}:{s.get('period')}:{s.get('gross')}"
            values = [x for x in (s.get("lower"), s.get("upper")) if x is not None]
            salaries[key].append(sum(values) / len(values))
    return {
        "employers": employers.most_common(),
        "regions": regions.most_common(),
        "skills": skills.most_common(),
        "salaries": {
            k: {"count": len(v), "min": min(v), "max": max(v), "average": sum(v) / len(v)}
            for k, v in salaries.items()
        },
    }
