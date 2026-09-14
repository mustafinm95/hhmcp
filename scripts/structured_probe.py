"""Opt-in end-to-end structured-filter Collector probe."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from hhmcp.models import SearchSpec
from hhmcp.service import Collector


async def main() -> None:
    data_dir = Path(os.environ.get("HHMCP_DATA_DIR", ".structured-live"))
    limit = int(os.environ.get("HHMCP_PROBE_LIMIT", "20"))
    collector = Collector(data_dir)
    run_id = collector.start(
        [
            SearchSpec(
                text="python",
                experience="noExperience",
                salary=100_000,
                work_format=["REMOTE"],
            )
        ],
        limit,
    )
    started = time.monotonic()
    await collector.collect(run_id)
    elapsed = time.monotonic() - started
    result = collector.repo.get_run(run_id).model_dump(mode="json")
    result["benchmark"] = {
        "elapsed_seconds": round(elapsed, 2),
        "vacancies_per_minute": round(60 * result["loaded"] / elapsed, 2),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
