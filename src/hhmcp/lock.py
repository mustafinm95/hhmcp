from __future__ import annotations

from pathlib import Path

from filelock import FileLock, Timeout


class CollectorBusy(RuntimeError):
    pass


class CollectorLock:
    def __init__(self, path: Path):
        self.path = path
        self._lock = FileLock(path, timeout=0)

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except Timeout as exc:
            raise CollectorBusy("collector_busy") from exc

    def release(self) -> None:
        if self._lock.is_locked:
            self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
