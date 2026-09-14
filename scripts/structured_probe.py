"""Opt-in end-to-end structured-filter Collector probe."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from hhmcp.models import SearchSpec
from hhmcp.service import Collector


async def main() -> None:
    data_dir = Path(os.environ.get("HHMCP_DATA_DIR", ".structured-live"))
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
        1,
    )
    await collector.collect(run_id)
    print(json.dumps(collector.repo.get_run(run_id).model_dump(mode="json"), ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
