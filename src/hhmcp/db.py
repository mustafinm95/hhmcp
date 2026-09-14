from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs(
 id TEXT PRIMARY KEY, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 stop_reason TEXT, complete INTEGER NOT NULL DEFAULT 0, limit_count INTEGER NOT NULL,
 discovered INTEGER NOT NULL DEFAULT 0, accepted INTEGER NOT NULL DEFAULT 0,
 loaded INTEGER NOT NULL DEFAULT 0, cached INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
 progress_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS run_searches(
 run_id TEXT NOT NULL REFERENCES runs(id), position INTEGER NOT NULL, spec_json TEXT NOT NULL,
 normalized_query TEXT NOT NULL, original_url TEXT, final_url TEXT, applied_filters_json TEXT,
 complete INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(run_id, position)
);
CREATE TABLE IF NOT EXISTS jobs(
 id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL REFERENCES runs(id), kind TEXT NOT NULL,
 target TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', state TEXT NOT NULL DEFAULT 'queued',
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, UNIQUE(run_id, kind, target)
);
CREATE TABLE IF NOT EXISTS employers(
 hh_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, fetched_at TEXT NOT NULL, content_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vacancies(
 hh_id TEXT PRIMARY KEY, data_json TEXT NOT NULL, content_hash TEXT NOT NULL,
 parser_version TEXT NOT NULL, first_discovered_at TEXT NOT NULL, fetched_at TEXT NOT NULL,
 local_status TEXT NOT NULL DEFAULT 'новое', favorite INTEGER NOT NULL DEFAULT 0,
 notes TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]', employer_excluded INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS vacancy_versions(
 id INTEGER PRIMARY KEY AUTOINCREMENT, vacancy_id TEXT NOT NULL REFERENCES vacancies(hh_id),
 observed_at TEXT NOT NULL, parser_version TEXT NOT NULL, content_hash TEXT NOT NULL,
 data_json TEXT NOT NULL, baseline INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS run_vacancies(
 run_id TEXT NOT NULL REFERENCES runs(id), search_position INTEGER NOT NULL, vacancy_id TEXT NOT NULL,
 discovered_at TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0, outcome TEXT,
 PRIMARY KEY(run_id, search_position, vacancy_id)
);
CREATE TABLE IF NOT EXISTS saved_searches(name TEXT PRIMARY KEY, spec_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS excluded_employers(employer_id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, name TEXT NOT NULL, data_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS similar_vacancies(a TEXT NOT NULL, b TEXT NOT NULL, score REAL NOT NULL,
 PRIMARY KEY(a,b));
CREATE VIRTUAL TABLE IF NOT EXISTS vacancy_fts USING fts5(vacancy_id UNINDEXED, title, description, employer, skills);
CREATE INDEX IF NOT EXISTS jobs_run_state ON jobs(run_id,state);
CREATE INDEX IF NOT EXISTS run_vacancies_vid ON run_vacancies(vacancy_id);
"""


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA journal_mode=WAL")
        return con

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def migrate(self) -> None:
        with self.connect() as con:
            try:
                row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            except sqlite3.OperationalError:
                row = None
            if row and int(row[0]) > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {row[0]} is newer than supported {SCHEMA_VERSION}"
                )
            con.executescript(SCHEMA)
            if not row:
                con.execute(
                    "INSERT INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),)
                )
            elif int(row[0]) < 2:
                columns = {item[1] for item in con.execute("PRAGMA table_info(runs)")}
                if "progress_json" not in columns:
                    con.execute("ALTER TABLE runs ADD COLUMN progress_json TEXT NOT NULL DEFAULT '{}'")
                con.execute(
                    "UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),)
                )
