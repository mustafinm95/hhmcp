from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any


def _safe(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_rows(rows: list[dict], path: Path, format: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if format == "json":
        path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    elif format == "csv":
        fields = sorted({k for row in rows for k in row})
        with path.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            writer.writerows([{k: _safe(row.get(k)) for k in fields} for row in rows])
    elif format == "markdown":
        chunks = []
        for row in rows:
            chunks.append(f"## {row.get('title') or row.get('hh_id')}\n\n")
            chunks.extend(f"- **{k}:** {_safe(v)}\n" for k, v in row.items() if k != "description")
            if row.get("description"):
                chunks.append(f"\n{row['description']}\n")
        path.write_text("".join(chunks), encoding="utf-8")
    else:
        raise ValueError("format must be json, csv or markdown")
