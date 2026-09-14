from __future__ import annotations

import subprocess
import sys
import os
from datetime import timedelta
from pathlib import Path

import pytest

from hh_mcp.errors import StateConflictError
from hh_mcp.state import PlaintextTestProtector, StateStore, utc_now


def _draft(store: StateStore, account: str = "A", vacancy: str = "V", generation: int = 1):
    return store.create_draft(
        account_id=account, auth_generation=generation, vacancy_id=vacancy,
        resume_id="R", message="letter", summary={"name": "vacancy"},
    )


WORKER = """
import sys
from pathlib import Path
from hh_mcp.errors import StateConflictError
from hh_mcp.state import PlaintextTestProtector, StateStore
store = StateStore(Path(sys.argv[1]), PlaintextTestProtector())
try:
    store.reserve_target(draft_id=sys.argv[2], account_id='A', current_generation=1)
    result = 'ok'
except StateConflictError:
    result = 'blocked'
Path(sys.argv[3]).write_text(result, encoding='ascii')
"""


def test_target_reservation_blocks_other_drafts_and_restart(workspace_tmp: Path) -> None:
    path = workspace_tmp / "state.db"
    store = StateStore(path, PlaintextTestProtector())
    first, second = _draft(store), _draft(store)
    operation = store.reserve_target(draft_id=first.id, account_id="A", current_generation=1)
    store.finish_operation(operation.id, "unknown")
    reopened = StateStore(path, PlaintextTestProtector())
    with pytest.raises(StateConflictError):
        reopened.reserve_target(draft_id=second.id, account_id="A", current_generation=1)


def test_target_key_is_account_bound(workspace_tmp: Path) -> None:
    store = StateStore(workspace_tmp / "state.db", PlaintextTestProtector())
    first = _draft(store, "A")
    second = _draft(store, "B")
    store.reserve_target(draft_id=first.id, account_id="A", current_generation=1)
    assert store.reserve_target(draft_id=second.id, account_id="B", current_generation=1).status == "sending"


def test_auth_epoch_mismatch_rejected(workspace_tmp: Path) -> None:
    store = StateStore(workspace_tmp / "state.db", PlaintextTestProtector())
    draft = _draft(store, generation=3)
    with pytest.raises(StateConflictError, match="lifecycle changed"):
        store.reserve_target(draft_id=draft.id, account_id="A", current_generation=4)


def test_expired_draft_is_erased(workspace_tmp: Path) -> None:
    store = StateStore(workspace_tmp / "state.db", PlaintextTestProtector())
    draft = store.create_draft(
        account_id="A", auth_generation=1, vacancy_id="V", resume_id="R",
        message="secret-letter", summary={}, ttl=timedelta(seconds=-1), now=utc_now(),
    )
    expired = store.get_draft(draft.id)
    assert expired.status == "expired"
    assert expired.message == ""
    assert b"secret-letter" not in (workspace_tmp / "state.db").read_bytes()


def test_parallel_processes_make_one_reservation(workspace_tmp: Path) -> None:
    path = workspace_tmp / "state.db"
    store = StateStore(path, PlaintextTestProtector())
    drafts = [_draft(store), _draft(store)]
    outputs = [workspace_tmp / f"result-{index}.txt" for index in range(2)]
    environment = os.environ.copy()
    source_root = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_root, environment.get("PYTHONPATH", "")) if part
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, str(path), item.id, str(output)],
            env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for item, output in zip(drafts, outputs, strict=True)
    ]
    for process in processes:
        assert process.wait(timeout=15) == 0
    assert sorted(output.read_text(encoding="ascii") for output in outputs) == ["blocked", "ok"]
