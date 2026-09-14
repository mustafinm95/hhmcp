from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from .db import Database, utcnow
from .models import CandidateProfile, Employer, Run, RunProgress, RunState, SearchSpec, Vacancy

LOCAL_STATUSES = {"новое", "интересно", "откликнулся", "интервью", "предложение", "отказ", "скрыто"}
MEANINGFUL = {
    "title",
    "description",
    "skills",
    "conditions",
    "salary",
    "address",
    "metro",
    "published_at",
    "employer_id",
    "employer_name",
    "department_name",
    "contacts",
    "archived",
    "unavailable",
    "availability",
}


def canonical(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def normalize_spec(spec: SearchSpec) -> str:
    data = spec.model_dump(exclude={"unchecked_parameters"}, exclude_none=True)
    if "url" in data:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(data["url"])
        query = urlencode(
            sorted((k, v) for k, v in parse_qsl(parts.query) if k != "search_session_id")
        )
        data["url"] = urlunsplit(
            (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), query, "")
        )
    return canonical(data)


def content_hash(vacancy: Vacancy) -> str:
    data = vacancy.model_dump(mode="json", include=MEANINGFUL)
    text_fields = ("title", "description", "address", "employer_name", "department_name")
    for key in text_fields:
        if isinstance(data.get(key), str):
            data[key] = re.sub(r"\s+", " ", data[key]).strip()
    return hashlib.sha256(canonical(data).encode()).hexdigest()


class Repository:
    def __init__(self, db: Database):
        self.db = db

    def create_run(self, specs: list[SearchSpec], limit: int = 1000) -> Run:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        run_id, now = str(uuid.uuid4()), utcnow()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO runs(id,state,created_at,updated_at,limit_count) VALUES(?,?,?,?,?)",
                (run_id, "queued", now, now, limit),
            )
            for pos, spec in enumerate(specs):
                con.execute(
                    "INSERT INTO run_searches(run_id,position,spec_json,normalized_query,original_url) VALUES(?,?,?,?,?)",
                    (run_id, pos, spec.model_dump_json(), normalize_spec(spec), spec.url),
                )
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> Run:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise KeyError(run_id)
            specs = [
                SearchSpec.model_validate_json(r[0])
                for r in con.execute(
                    "SELECT spec_json FROM run_searches WHERE run_id=? ORDER BY position", (run_id,)
                )
            ]
        return Run(
            id=row["id"],
            state=row["state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            stop_reason=row["stop_reason"],
            complete=bool(row["complete"]),
            limit=row["limit_count"],
            discovered=row["discovered"],
            accepted=row["accepted"],
            loaded=row["loaded"],
            cached=row["cached"],
            errors=row["errors"],
            search_specs=specs,
            progress=RunProgress.model_validate_json(row["progress_json"] or "{}"),
        )

    def set_progress(self, run_id: str, progress: RunProgress) -> None:
        with self.db.transaction() as con:
            con.execute(
                "UPDATE runs SET progress_json=?,updated_at=? WHERE id=?",
                (progress.model_dump_json(), utcnow(), run_id),
            )

    def set_run_state(
        self,
        run_id: str,
        state: RunState | str,
        reason: str | None = None,
        complete: bool | None = None,
    ) -> None:
        sets, args = ["state=?", "updated_at=?", "stop_reason=?"], [str(state), utcnow(), reason]
        if complete is not None:
            sets.append("complete=?")
            args.append(int(complete))
        args.append(run_id)
        with self.db.transaction() as con:
            con.execute(f"UPDATE runs SET {','.join(sets)} WHERE id=?", args)

    def increment(self, run_id: str, **counts: int) -> None:
        allowed = {"discovered", "accepted", "loaded", "cached", "errors"}
        if not counts.keys() <= allowed:
            raise ValueError("invalid counter")
        sql = ",".join(f"{k}={k}+?" for k in counts)
        with self.db.transaction() as con:
            con.execute(
                f"UPDATE runs SET {sql},updated_at=? WHERE id=?",
                (*counts.values(), utcnow(), run_id),
            )

    def observe(self, run_id: str, pos: int, vacancy_id: str, accepted: bool) -> bool:
        with self.db.transaction() as con:
            before = con.execute(
                "SELECT 1 FROM run_vacancies WHERE run_id=? AND vacancy_id=? AND accepted=1",
                (run_id, vacancy_id),
            ).fetchone()
            con.execute(
                "INSERT OR IGNORE INTO run_vacancies(run_id,search_position,vacancy_id,discovered_at,accepted) VALUES(?,?,?,?,?)",
                (run_id, pos, vacancy_id, utcnow(), int(accepted and not before)),
            )
            return not bool(before)

    def record_observation(
        self,
        run_id: str,
        pos: int,
        vacancy_id: str,
        url: str,
        limit: int,
        observed: dict[str, Any] | None = None,
    ) -> tuple[bool, bool, bool]:
        """Atomically record membership and enqueue a unique accepted vacancy."""
        with self.db.transaction() as con:
            existed = con.execute(
                "SELECT 1 FROM run_vacancies WHERE run_id=? AND vacancy_id=?",
                (run_id, vacancy_id),
            ).fetchone()
            globally_discovered = con.execute(
                "SELECT 1 FROM run_vacancies WHERE run_id=? AND vacancy_id=?",
                (run_id, vacancy_id),
            ).fetchone()
            accepted = con.execute("SELECT accepted FROM runs WHERE id=?", (run_id,)).fetchone()[0]
            globally_accepted = con.execute(
                "SELECT 1 FROM run_vacancies WHERE run_id=? AND vacancy_id=? AND accepted=1",
                (run_id, vacancy_id),
            ).fetchone()
            take = not globally_accepted and accepted < limit
            rejected_by_limit = not globally_accepted and accepted >= limit
            con.execute(
                """INSERT OR IGNORE INTO run_vacancies
                   (run_id,search_position,vacancy_id,discovered_at,accepted)
                   VALUES(?,?,?,?,?)""",
                (run_id, pos, vacancy_id, utcnow(), int(take)),
            )
            if take:
                con.execute(
                    "UPDATE run_vacancies SET accepted=1 WHERE run_id=? AND vacancy_id=?",
                    (run_id, vacancy_id),
                )
                con.execute(
                    """INSERT OR IGNORE INTO jobs(run_id,kind,target,payload_json)
                       VALUES(?,?,?,?)""",
                    (
                        run_id,
                        "vacancy",
                        vacancy_id,
                        canonical({"url": url, "observed": observed or {}}),
                    ),
                )
            con.execute(
                """UPDATE runs SET discovered=discovered+?,accepted=accepted+?,updated_at=?
                   WHERE id=?""",
                (int(not globally_discovered), int(take), utcnow(), run_id),
            )
            return not bool(existed), take, rejected_by_limit

    def pending_jobs(self, run_id: str) -> list[sqlite3.Row]:
        with self.db.connect() as con:
            return con.execute(
                """SELECT * FROM jobs WHERE run_id=? AND kind='vacancy'
                   AND state IN ('queued','running','error') ORDER BY id""",
                (run_id,),
            ).fetchall()

    def get_job(self, run_id: str, vacancy_id: str) -> sqlite3.Row:
        with self.db.connect() as con:
            row = con.execute(
                "SELECT * FROM jobs WHERE run_id=? AND kind='vacancy' AND target=?",
                (run_id, vacancy_id),
            ).fetchone()
        if not row:
            raise KeyError((run_id, vacancy_id))
        return row

    @staticmethod
    def _update_outcome_count(
        con: sqlite3.Connection, run_id: str, old_state: str, new_state: str
    ) -> None:
        columns = {"loaded": "loaded", "cached": "cached", "error": "errors"}
        deltas = {name: 0 for name in columns.values()}
        if old_state in columns:
            deltas[columns[old_state]] -= 1
        if new_state in columns:
            deltas[columns[new_state]] += 1
        assignments = [f"{name}={name}+?" for name, delta in deltas.items() if delta]
        values = [delta for delta in deltas.values() if delta]
        if assignments:
            con.execute(
                f"UPDATE runs SET {','.join(assignments)},updated_at=? WHERE id=?",
                (*values, utcnow(), run_id),
            )

    def start_job(self, job_id: int) -> None:
        with self.db.transaction() as con:
            job = con.execute("SELECT run_id,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError(job_id)
            con.execute(
                "UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (job_id,)
            )
            self._update_outcome_count(con, job["run_id"], job["state"], "running")

    def finish_job(self, job_id: int, state: str, error: str | None = None) -> None:
        if state not in {"loaded", "cached", "error", "queued"}:
            raise ValueError("invalid job outcome")
        with self.db.transaction() as con:
            job = con.execute("SELECT run_id,target,state FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job:
                raise KeyError(job_id)
            con.execute("UPDATE jobs SET state=?,error=? WHERE id=?", (state, error, job_id))
            con.execute(
                "UPDATE run_vacancies SET outcome=? WHERE run_id=? AND vacancy_id=?",
                (state, job["run_id"], job["target"]),
            )
            self._update_outcome_count(con, job["run_id"], job["state"], state)

    def save_vacancy_and_finish_job(
        self, vacancy: Vacancy, job_id: int | None = None, outcome: str = "loaded"
    ) -> bool:
        now = utcnow()
        vacancy.fetched_at = vacancy.fetched_at or datetime.now(UTC)
        h, raw = content_hash(vacancy), vacancy.model_dump_json()
        with self.db.transaction() as con:
            old = con.execute(
                "SELECT content_hash,parser_version,data_json FROM vacancies WHERE hh_id=?",
                (vacancy.hh_id,),
            ).fetchone()
            changed = (
                not old
                or old["content_hash"] != h
                or old["parser_version"] != vacancy.parser_version
            )
            if old:
                old_data = json.loads(old["data_json"])
                # Extraction errors never erase the last known correct field.
                merged = vacancy.model_dump(mode="json")
                for field, state in vacancy.field_states.items():
                    if state.state == "error" and old_data.get(field) is not None:
                        merged[field] = old_data[field]
                    elif state.state == "error" and field in old_data.get("conditions", {}):
                        merged.setdefault("conditions", {})[field] = old_data["conditions"][field]
                vacancy = Vacancy.model_validate(merged)
                raw, h = vacancy.model_dump_json(), content_hash(vacancy)
                changed = (
                    old["content_hash"] != h or old["parser_version"] != vacancy.parser_version
                )
            con.execute(
                """INSERT INTO vacancies(hh_id,data_json,content_hash,parser_version,first_discovered_at,fetched_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(hh_id) DO UPDATE SET data_json=excluded.data_json,
                content_hash=excluded.content_hash,parser_version=excluded.parser_version,fetched_at=excluded.fetched_at""",
                (
                    vacancy.hh_id,
                    raw,
                    h,
                    vacancy.parser_version,
                    now,
                    vacancy.fetched_at.isoformat(),
                ),
            )
            if changed:
                baseline = int(bool(old and old["parser_version"] != vacancy.parser_version))
                con.execute(
                    "INSERT INTO vacancy_versions(vacancy_id,observed_at,parser_version,content_hash,data_json,baseline) VALUES(?,?,?,?,?,?)",
                    (vacancy.hh_id, now, vacancy.parser_version, h, raw, baseline),
                )
            con.execute("DELETE FROM vacancy_fts WHERE vacancy_id=?", (vacancy.hh_id,))
            con.execute(
                "INSERT INTO vacancy_fts VALUES(?,?,?,?,?)",
                (
                    vacancy.hh_id,
                    vacancy.title or "",
                    vacancy.description or "",
                    vacancy.employer_name or "",
                    " ".join(vacancy.skills),
                ),
            )
            if job_id is not None:
                job = con.execute(
                    "SELECT run_id,target,state FROM jobs WHERE id=?", (job_id,)
                ).fetchone()
                con.execute("UPDATE jobs SET state='loaded',error=NULL WHERE id=?", (job_id,))
                con.execute(
                    "UPDATE run_vacancies SET outcome='loaded' WHERE run_id=? AND vacancy_id=?",
                    (job["run_id"], job["target"]),
                )
                self._update_outcome_count(con, job["run_id"], job["state"], "loaded")
            return changed

    def get_vacancy(self, vacancy_id: str) -> tuple[Vacancy, dict]:
        with self.db.connect() as con:
            row = con.execute("SELECT * FROM vacancies WHERE hh_id=?", (vacancy_id,)).fetchone()
        if not row:
            raise KeyError(vacancy_id)
        return Vacancy.model_validate_json(row["data_json"]), dict(row)

    def update_local(
        self,
        vacancy_id: str,
        *,
        status: str | None = None,
        favorite: bool | None = None,
        notes: str | None = None,
        tags: list[str] | None = None,
        employer_excluded: bool | None = None,
    ) -> None:
        if status is not None and status not in LOCAL_STATUSES:
            raise ValueError("invalid status")
        values = {
            "local_status": status,
            "favorite": None if favorite is None else int(favorite),
            "notes": notes,
            "tags_json": None if tags is None else canonical(sorted(set(tags))),
            "employer_excluded": None if employer_excluded is None else int(employer_excluded),
        }
        values = {k: v for k, v in values.items() if v is not None}
        if not values:
            return
        with self.db.transaction() as con:
            cur = con.execute(
                f"UPDATE vacancies SET {','.join(f'{k}=?' for k in values)} WHERE hh_id=?",
                (*values.values(), vacancy_id),
            )
            if not cur.rowcount:
                raise KeyError(vacancy_id)

    def list_vacancies(
        self,
        query: str | None = None,
        status: str | None = None,
        favorite: bool | None = None,
        tags: list[str] | None = None,
        employer_id: str | None = None,
        work_format: str | None = None,
        limit: int | None = 20,
        offset: int = 0,
    ) -> list[dict]:
        sql = "SELECT v.* FROM vacancies v"
        args: list[Any] = []
        where = []
        if query:
            sql += " JOIN vacancy_fts f ON f.vacancy_id=v.hh_id"
            where.append("vacancy_fts MATCH ?")
            args.append(query)
        if status:
            where.append("v.local_status=?")
            args.append(status)
        if favorite is not None:
            where.append("v.favorite=?")
            args.append(int(favorite))
        for tag in tags or []:
            where.append("EXISTS (SELECT 1 FROM json_each(v.tags_json) WHERE value=?)")
            args.append(tag)
        if employer_id:
            where.append("json_extract(v.data_json,'$.employer_id')=?")
            args.append(employer_id)
        if work_format:
            where.append("json_extract(v.data_json,'$.conditions.work_format')=?")
            args.append(work_format)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY v.fetched_at DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            args += [min(limit, 1000), offset]
        with self.db.connect() as con:
            rows = con.execute(sql, args).fetchall()
        result = [
            {
                **(
                    json.loads(r["data_json"])
                    | {
                        "cache_age_seconds": max(
                            0,
                            int(
                                (
                                    datetime.now(UTC) - datetime.fromisoformat(r["fetched_at"])
                                ).total_seconds()
                            ),
                        )
                    }
                ),
                "status": r["local_status"],
                "favorite": bool(r["favorite"]),
                "notes": r["notes"],
                "tags": json.loads(r["tags_json"]),
            }
            for r in rows
        ]
        return result

    def run_results(self, run_id: str, limit: int = 20, offset: int = 0) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT v.data_json,rv.outcome,MIN(rv.discovered_at) discovered_at
                   FROM run_vacancies rv LEFT JOIN vacancies v ON v.hh_id=rv.vacancy_id
                   WHERE rv.run_id=? GROUP BY rv.vacancy_id
                   ORDER BY discovered_at LIMIT ? OFFSET ?""",
                (run_id, min(limit, 1000), offset),
            ).fetchall()
        results = []
        for row in rows:
            data = json.loads(row["data_json"]) if row["data_json"] else {}
            data.pop("description", None)
            results.append(
                data | {"outcome": row["outcome"], "discovered_at": row["discovered_at"]}
            )
        return results

    def save_employer(self, employer: Employer) -> None:
        raw = employer.model_dump_json()
        h = hashlib.sha256(raw.encode()).hexdigest()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO employers VALUES(?,?,?,?) ON CONFLICT(hh_id) DO UPDATE SET data_json=excluded.data_json,fetched_at=excluded.fetched_at,content_hash=excluded.content_hash",
                (employer.hh_id, raw, utcnow(), h),
            )

    def history(self, vacancy_id: str) -> list[dict]:
        with self.db.connect() as con:
            return [
                dict(r) | {"data": json.loads(r["data_json"])}
                for r in con.execute(
                    "SELECT * FROM vacancy_versions WHERE vacancy_id=? ORDER BY observed_at",
                    (vacancy_id,),
                )
            ]

    def save_profile(self, profile: CandidateProfile) -> None:
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO profiles VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,data_json=excluded.data_json",
                (profile.id, profile.name, profile.model_dump_json()),
            )

    def get_profile(self, profile_id: str) -> CandidateProfile:
        with self.db.connect() as con:
            row = con.execute("SELECT data_json FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not row:
            raise KeyError(profile_id)
        return CandidateProfile.model_validate_json(row[0])

    def exclude_employer(self, employer_id: str, excluded: bool = True) -> None:
        with self.db.transaction() as con:
            if excluded:
                con.execute(
                    "INSERT OR IGNORE INTO excluded_employers VALUES(?,?)", (employer_id, utcnow())
                )
            else:
                con.execute("DELETE FROM excluded_employers WHERE employer_id=?", (employer_id,))

    def is_employer_excluded(self, employer_id: str | None) -> bool:
        if not employer_id:
            return False
        with self.db.connect() as con:
            return bool(
                con.execute(
                    "SELECT 1 FROM excluded_employers WHERE employer_id=?", (employer_id,)
                ).fetchone()
            )

    def compare_runs(self, current_id: str, previous_id: str) -> dict:
        with self.db.connect() as con:
            current = con.execute(
                "SELECT normalized_query,complete FROM run_searches WHERE run_id=? ORDER BY position",
                (current_id,),
            ).fetchall()
            previous = con.execute(
                "SELECT normalized_query,complete FROM run_searches WHERE run_id=? ORDER BY position",
                (previous_id,),
            ).fetchall()
            if [r[0] for r in current] != [r[0] for r in previous]:
                raise ValueError("runs have different normalized searches")
            cur_ids = {
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT vacancy_id FROM run_vacancies WHERE run_id=?", (current_id,)
                )
            }
            prev_ids = {
                r[0]
                for r in con.execute(
                    "SELECT DISTINCT vacancy_id FROM run_vacancies WHERE run_id=?", (previous_id,)
                )
            }
        complete = all(bool(r[1]) for r in current) and all(bool(r[1]) for r in previous)
        return {
            "appeared": sorted(cur_ids - prev_ids),
            "disappeared": sorted(prev_ids - cur_ids),
            "comparison_complete": complete,
            "uncertainty": None if complete else "one or both traversals are partial",
        }

    def find_similar(self, vacancy_id: str, threshold: float = 0.72) -> list[dict]:
        from difflib import SequenceMatcher

        vacancy, _ = self.get_vacancy(vacancy_id)
        needle = f"{vacancy.title or ''} {vacancy.employer_name or ''} {vacancy.description or ''}".casefold()
        matches = []
        for row in self.list_vacancies(limit=1000):
            if row["hh_id"] == vacancy_id:
                continue
            candidate = f"{row.get('title') or ''} {row.get('employer_name') or ''} {row.get('description') or ''}".casefold()
            score = SequenceMatcher(None, needle, candidate).ratio()
            if score >= threshold:
                a, b = sorted((vacancy_id, row["hh_id"]))
                with self.db.transaction() as con:
                    con.execute(
                        "INSERT OR REPLACE INTO similar_vacancies VALUES(?,?,?)", (a, b, score)
                    )
                matches.append({"vacancy_id": row["hh_id"], "score": round(score, 3)})
        return sorted(matches, key=lambda x: -x["score"])
