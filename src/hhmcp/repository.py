from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .db import Database, utcnow
from .models import CandidateProfile, Employer, Run, RunProgress, RunState, SearchSpec, Vacancy
from .parsing import parse_published_at

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

    def create_run(
        self,
        specs: list[SearchSpec],
        limit: int = 1000,
        *,
        vacancy_cache_ttl_hours: int = 24,
        navigation_interval_seconds: float = 1.0,
        per_search_limit: int | None = None,
        collection_strategy: str = "round_robin",
        fetch_details: str = "all",
        shortlist_size: int = 30,
        timezone: str = "Europe/Moscow",
    ) -> Run:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if vacancy_cache_ttl_hours < 0:
            raise ValueError("vacancy_cache_ttl_hours must be non-negative")
        if not math.isfinite(navigation_interval_seconds) or navigation_interval_seconds < 1:
            raise ValueError("navigation_interval_seconds must be at least 1")
        if per_search_limit is not None and not 1 <= per_search_limit <= 1000:
            raise ValueError("per_search_limit must be between 1 and 1000")
        if collection_strategy != "round_robin":
            raise ValueError("only round_robin collection_strategy is supported")
        if fetch_details not in {"all", "shortlist", "none"}:
            raise ValueError("fetch_details must be all, shortlist, or none")
        if not 1 <= shortlist_size <= 1000:
            raise ValueError("shortlist_size must be between 1 and 1000")
        run_id, now = str(uuid.uuid4()), utcnow()
        with self.db.transaction() as con:
            con.execute(
                """INSERT INTO runs(
                   id,state,created_at,updated_at,limit_count,
                   vacancy_cache_ttl_hours,navigation_interval_seconds,per_search_limit,
                   collection_strategy,fetch_details,shortlist_size,timezone
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    "queued",
                    now,
                    now,
                    limit,
                    vacancy_cache_ttl_hours,
                    navigation_interval_seconds,
                    per_search_limit,
                    collection_strategy,
                    fetch_details,
                    shortlist_size,
                    timezone,
                ),
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
            vacancy_cache_ttl_hours=row["vacancy_cache_ttl_hours"],
            navigation_interval_seconds=row["navigation_interval_seconds"],
            per_search_limit=row["per_search_limit"],
            collection_strategy=row["collection_strategy"],
            fetch_details=row["fetch_details"],
            shortlist_size=row["shortlist_size"],
            timezone=row["timezone"],
            revision=row["revision"],
            search_specs=specs,
            progress=RunProgress.model_validate_json(row["progress_json"] or "{}"),
        )

    def queued_run_ids(self) -> list[str]:
        with self.db.connect() as con:
            return [
                row[0]
                for row in con.execute(
                    "SELECT id FROM runs WHERE state='queued' ORDER BY created_at,id"
                )
            ]

    def update_run_settings(
        self,
        run_id: str,
        *,
        vacancy_cache_ttl_hours: int | None = None,
        navigation_interval_seconds: float | None = None,
    ) -> None:
        values = {
            "vacancy_cache_ttl_hours": vacancy_cache_ttl_hours,
            "navigation_interval_seconds": navigation_interval_seconds,
        }
        values = {key: value for key, value in values.items() if value is not None}
        if not values:
            return
        if vacancy_cache_ttl_hours is not None and vacancy_cache_ttl_hours < 0:
            raise ValueError("vacancy_cache_ttl_hours must be non-negative")
        if navigation_interval_seconds is not None and (
            not math.isfinite(navigation_interval_seconds) or navigation_interval_seconds < 1
        ):
            raise ValueError("navigation_interval_seconds must be at least 1")
        with self.db.transaction() as con:
            cursor = con.execute(
                f"UPDATE runs SET {','.join(f'{key}=?' for key in values)},updated_at=? WHERE id=?",
                (*values.values(), utcnow(), run_id),
            )
            if not cursor.rowcount:
                raise KeyError(run_id)

    def set_progress(self, run_id: str, progress: RunProgress) -> None:
        with self.db.transaction() as con:
            con.execute(
                "UPDATE runs SET progress_json=?,updated_at=?,revision=revision+1 WHERE id=?",
                (progress.model_dump_json(), utcnow(), run_id),
            )

    def set_run_state(
        self,
        run_id: str,
        state: RunState | str,
        reason: str | None = None,
        complete: bool | None = None,
    ) -> None:
        sets, args = (
            ["state=?", "updated_at=?", "stop_reason=?", "revision=revision+1"],
            [
                str(state),
                utcnow(),
                reason,
            ],
        )
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
                f"UPDATE runs SET {sql},updated_at=?,revision=revision+1 WHERE id=?",
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
                (int(not existed), int(take), utcnow(), run_id),
            )
            return not bool(existed), take, rejected_by_limit

    def record_page_observations(
        self,
        run_id: str,
        pos: int,
        observations: list[dict[str, Any]],
        limit: int,
        *,
        cache_cutoff: str | None,
        parser_version: str,
        checkpoint_url: str,
        applied_filters: dict[str, Any],
        complete: bool,
        per_search_limit: int | None = None,
        enqueue_details: bool = True,
    ) -> list[dict[str, Any]]:
        """Record one search page and return only jobs that require detail loading."""
        results: list[dict[str, Any]] = []
        run = self.get_run(run_id)
        spec = run.search_specs[pos]
        now = datetime.now(ZoneInfo(run.timezone))

        def period_match_for(value: datetime | str | None) -> bool | None:
            if spec.period is None:
                return None
            published = value if isinstance(value, datetime) else parse_published_at(value, now=now)
            if published is None:
                return None
            cutoff_date = now.date() - timedelta(days=spec.period - 1)
            cutoff = datetime.combine(cutoff_date, datetime.min.time(), now.tzinfo)
            return published.astimezone(now.tzinfo) >= cutoff

        with self.db.transaction() as con:
            accepted_count = con.execute(
                "SELECT accepted FROM runs WHERE id=?", (run_id,)
            ).fetchone()[0]
            search_count = con.execute(
                "SELECT COUNT(*) FROM run_vacancies WHERE run_id=? AND search_position=?",
                (run_id, pos),
            ).fetchone()[0]
            discovered_delta = accepted_delta = cached_delta = 0
            for observation in observations:
                if accepted_count >= limit or (
                    per_search_limit is not None and search_count >= per_search_limit
                ):
                    break
                vacancy_id = observation["vacancy_id"]
                previous = con.execute(
                    """SELECT accepted,outcome FROM run_vacancies
                       WHERE run_id=? AND vacancy_id=? ORDER BY accepted DESC""",
                    (run_id, vacancy_id),
                ).fetchall()
                membership_exists = bool(
                    con.execute(
                        """SELECT 1 FROM run_vacancies
                           WHERE run_id=? AND search_position=? AND vacancy_id=?""",
                        (run_id, pos, vacancy_id),
                    ).fetchone()
                )
                discovered = not previous
                already_accepted = any(bool(row["accepted"]) for row in previous)
                accepted = not already_accepted
                previous_outcome = next(
                    (row["outcome"] for row in previous if row["outcome"] is not None), None
                )
                cached = False
                cached_data = None
                if accepted and cache_cutoff is not None:
                    cached_row = con.execute(
                        """SELECT data_json FROM vacancies
                           WHERE hh_id=? AND julianday(fetched_at)>=julianday(?)
                           AND parser_version=?""",
                        (vacancy_id, cache_cutoff, parser_version),
                    ).fetchone()
                    cached = bool(cached_row)
                    if cached_row:
                        cached_data = json.loads(cached_row["data_json"])
                outcome = "cached" if cached else previous_outcome
                observed_json = canonical(
                    {"url": observation["url"], **(observation.get("observed") or {})}
                )
                observed = observation.get("observed") or {}
                period_match = period_match_for(
                    (cached_data or {}).get("published_at") or observed.get("published_text")
                )
                if period_match is False:
                    outcome = "excluded_period"
                con.execute(
                    """INSERT OR IGNORE INTO run_vacancies
                       (run_id,search_position,vacancy_id,discovered_at,accepted,outcome,
                        observed_json,period_match)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        pos,
                        vacancy_id,
                        utcnow(),
                        int(accepted),
                        outcome,
                        observed_json,
                        None if period_match is None else int(period_match),
                    ),
                )
                if not membership_exists:
                    search_count += 1
                job = None
                if accepted:
                    accepted_count += 1
                    accepted_delta += 1
                    if cached:
                        cached_delta += 1
                        con.execute(
                            """UPDATE run_vacancies SET outcome=?,period_match=?
                               WHERE run_id=? AND search_position=? AND vacancy_id=?""",
                            (
                                outcome,
                                None if period_match is None else int(period_match),
                                run_id,
                                pos,
                                vacancy_id,
                            ),
                        )
                    elif enqueue_details:
                        payload = canonical(
                            {
                                "url": observation["url"],
                                "observed": observation.get("observed") or {},
                            }
                        )
                        con.execute(
                            """INSERT OR IGNORE INTO jobs(run_id,kind,target,payload_json)
                               VALUES(?,?,?,?)""",
                            (run_id, "vacancy", vacancy_id, payload),
                        )
                        row = con.execute(
                            """SELECT * FROM jobs
                               WHERE run_id=? AND kind='vacancy' AND target=?""",
                            (run_id, vacancy_id),
                        ).fetchone()
                        job = dict(row)
                discovered_delta += int(discovered)
                results.append(
                    {
                        "vacancy_id": vacancy_id,
                        "discovered": discovered,
                        "search_discovered": not membership_exists,
                        "accepted": accepted,
                        "cached": cached,
                        "job": job,
                    }
                )
            con.execute(
                """UPDATE runs SET discovered=discovered+?,accepted=accepted+?,cached=cached+?,
                   updated_at=? WHERE id=?""",
                (discovered_delta, accepted_delta, cached_delta, utcnow(), run_id),
            )
            con.execute(
                """UPDATE run_searches SET final_url=?,applied_filters_json=?,complete=?,
                   stop_reason=CASE WHEN ? THEN NULL ELSE stop_reason END
                   WHERE run_id=? AND position=?""",
                (
                    checkpoint_url,
                    canonical(applied_filters),
                    int(complete and len(results) == len(observations)),
                    int(complete and len(results) == len(observations)),
                    run_id,
                    pos,
                ),
            )
        return results

    def complete_search(self, run_id: str, position: int, reason: str | None = None) -> None:
        with self.db.transaction() as con:
            con.execute(
                """UPDATE run_searches SET complete=1,stop_reason=?
                   WHERE run_id=? AND position=?""",
                (reason, run_id, position),
            )

    def enqueue_shortlist(self, run_id: str, size: int) -> list[sqlite3.Row]:
        """Choose a deterministic metadata shortlist and enqueue missing details."""
        with self.db.transaction() as con:
            rows = con.execute(
                """SELECT rv.vacancy_id,rv.search_position,rv.discovered_at,rv.observed_json
                   FROM run_vacancies rv
                   WHERE rv.run_id=? AND rv.accepted=1
                     AND COALESCE(rv.outcome,'') NOT IN ('cached','excluded_period')
                   ORDER BY rv.discovered_at,rv.vacancy_id""",
                (run_id,),
            ).fetchall()
            specs = self.get_run(run_id).search_specs
            candidates: dict[str, tuple[int, str, dict[str, Any]]] = {}
            for row in rows:
                observed = json.loads(row["observed_json"] or "{}")
                title = re.sub(r"\W+", " ", observed.get("title") or "").casefold()
                query = re.sub(r"\W+", " ", specs[row["search_position"]].text or "").casefold()
                score = 4 * int(bool(query and query in title))
                score += sum(
                    int(bool(observed.get(field)))
                    for field in ("title", "employer_name", "salary_text", "published_text")
                )
                score += int(bool((observed.get("conditions") or {}).get("work_format")))
                current = candidates.get(row["vacancy_id"])
                if current is None or score > current[0]:
                    candidates[row["vacancy_id"]] = (score, row["discovered_at"], observed)
            selected = sorted(
                candidates.items(), key=lambda item: (-item[1][0], item[1][1], item[0])
            )[:size]
            for vacancy_id, (_, _, observed) in selected:
                payload = canonical(
                    {
                        "url": observed.pop("url", None) or f"https://hh.ru/vacancy/{vacancy_id}",
                        "observed": observed,
                    }
                )
                con.execute(
                    """INSERT OR IGNORE INTO jobs(run_id,kind,target,payload_json)
                       VALUES(?,?,?,?)""",
                    (run_id, "vacancy", vacancy_id, payload),
                )
            return con.execute(
                """SELECT * FROM jobs WHERE run_id=? AND kind='vacancy'
                   AND state IN ('queued','running','error') ORDER BY id""",
                (run_id,),
            ).fetchall()

    def pending_jobs(self, run_id: str) -> list[sqlite3.Row]:
        with self.db.connect() as con:
            return con.execute(
                """SELECT * FROM jobs WHERE run_id=? AND kind='vacancy'
                   AND state IN ('queued','running','error') ORDER BY id""",
                (run_id,),
            ).fetchall()

    def cancel_pending_jobs(self, run_id: str) -> int:
        with self.db.transaction() as con:
            rows = con.execute(
                """SELECT target FROM jobs
                   WHERE run_id=? AND state IN ('queued','running')""",
                (run_id,),
            ).fetchall()
            con.execute(
                """UPDATE jobs SET state='cancelled',error='cancelled by user'
                   WHERE run_id=? AND state IN ('queued','running')""",
                (run_id,),
            )
            con.execute(
                """UPDATE run_vacancies SET outcome='cancelled'
                   WHERE run_id=? AND vacancy_id IN (
                     SELECT target FROM jobs WHERE run_id=? AND state='cancelled'
                   ) AND COALESCE(outcome,'') NOT IN ('loaded','cached')""",
                (run_id, run_id),
            )
        return len(rows)

    def apply_period_filter(self, run_id: str, vacancy: Vacancy) -> None:
        run = self.get_run(run_id)
        now = datetime.now(ZoneInfo(run.timezone))
        with self.db.transaction() as con:
            memberships = con.execute(
                "SELECT search_position FROM run_vacancies WHERE run_id=? AND vacancy_id=?",
                (run_id, vacancy.hh_id),
            ).fetchall()
            for membership in memberships:
                period = run.search_specs[membership["search_position"]].period
                match: bool | None = None
                if period is not None and vacancy.published_at is not None:
                    cutoff_date = now.date() - timedelta(days=period - 1)
                    cutoff = datetime.combine(cutoff_date, datetime.min.time(), now.tzinfo)
                    match = vacancy.published_at.astimezone(now.tzinfo) >= cutoff
                con.execute(
                    """UPDATE run_vacancies SET period_match=?,outcome=CASE
                       WHEN ?=0 THEN 'excluded_period' ELSE outcome END
                       WHERE run_id=? AND search_position=? AND vacancy_id=?""",
                    (
                        None if match is None else int(match),
                        None if match is None else int(match),
                        run_id,
                        membership["search_position"],
                        vacancy.hh_id,
                    ),
                )

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
            con.execute("UPDATE jobs SET state='running',attempts=attempts+1 WHERE id=?", (job_id,))
            self._update_outcome_count(con, job["run_id"], job["state"], "running")

    def finish_job(self, job_id: int, state: str, error: str | None = None) -> None:
        if state not in {"loaded", "cached", "error", "queued"}:
            raise ValueError("invalid job outcome")
        with self.db.transaction() as con:
            job = con.execute(
                "SELECT run_id,target,state FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
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
            if changed:
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
                con.execute(
                    "DELETE FROM similar_vacancies WHERE a=? OR b=?",
                    (vacancy.hh_id, vacancy.hh_id),
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
                """SELECT rv.vacancy_id,v.data_json,MAX(rv.outcome) outcome,
                          MIN(rv.discovered_at) discovered_at,MAX(rv.observed_json) observed_json,
                          MIN(rv.period_match) period_match
                   FROM run_vacancies rv
                   JOIN run_searches rs ON rs.run_id=rv.run_id AND rs.position=rv.search_position
                   LEFT JOIN vacancies v ON v.hh_id=rv.vacancy_id
                   WHERE rv.run_id=? GROUP BY rv.vacancy_id
                   HAVING MAX(
                     CASE WHEN json_extract(rs.spec_json,'$.period') IS NULL
                               OR rv.period_match=1 THEN 1 ELSE 0 END
                   )=1
                   ORDER BY discovered_at LIMIT ? OFFSET ?""",
                (run_id, min(limit, 1000), offset),
            ).fetchall()
        results = []
        for row in rows:
            observed = json.loads(row["observed_json"] or "{}")
            data = (
                json.loads(row["data_json"])
                if row["data_json"]
                else {
                    "hh_id": row["vacancy_id"],
                    "url": observed.get("url") or f"https://hh.ru/vacancy/{row['vacancy_id']}",
                    "title": observed.get("title"),
                    "employer_name": observed.get("employer_name"),
                    "salary_text": observed.get("salary_text"),
                    "conditions": observed.get("conditions") or {},
                    "published_text": observed.get("published_text"),
                }
            )
            results.append(
                data
                | {
                    "outcome": row["outcome"] or "metadata_only",
                    "discovered_at": row["discovered_at"],
                    "period_match": (
                        None if row["period_match"] is None else bool(row["period_match"])
                    ),
                }
            )
        return results

    def run_search_stats(self, run_id: str) -> list[dict[str, Any]]:
        with self.db.connect() as con:
            rows = con.execute(
                """SELECT rs.position,rs.spec_json,rs.complete,rs.stop_reason,
                          COUNT(DISTINCT rv.vacancy_id) discovered,
                          COUNT(DISTINCT CASE WHEN rv.accepted=1 THEN rv.vacancy_id END) unique_count,
                          COUNT(DISTINCT CASE WHEN rv.outcome IN ('loaded','cached') THEN rv.vacancy_id END) loaded,
                          COUNT(DISTINCT CASE WHEN rv.outcome='error' THEN rv.vacancy_id END) errors
                   FROM run_searches rs LEFT JOIN run_vacancies rv
                     ON rv.run_id=rs.run_id AND rv.search_position=rs.position
                   WHERE rs.run_id=? GROUP BY rs.position ORDER BY rs.position""",
                (run_id,),
            ).fetchall()
        return [
            {
                "position": row["position"],
                "search": json.loads(row["spec_json"]),
                "discovered": row["discovered"],
                "unique": row["unique_count"],
                "details_loaded": row["loaded"],
                "complete": bool(row["complete"]),
                "stop_reason": row["stop_reason"]
                or (None if row["complete"] else "run stopped before completion"),
                "errors": row["errors"],
            }
            for row in rows
        ]

    def save_employer(self, employer: Employer) -> None:
        raw = employer.model_dump_json()
        h = hashlib.sha256(raw.encode()).hexdigest()
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO employers VALUES(?,?,?,?) ON CONFLICT(hh_id) DO UPDATE SET data_json=excluded.data_json,fetched_at=excluded.fetched_at,content_hash=excluded.content_hash",
                (employer.hh_id, raw, utcnow(), h),
            )

    def history(self, vacancy_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self.db.connect() as con:
            return [
                dict(r) | {"data": json.loads(r["data_json"])}
                for r in con.execute(
                    """SELECT * FROM vacancy_versions WHERE vacancy_id=?
                       ORDER BY observed_at LIMIT ? OFFSET ?""",
                    (vacancy_id, limit, offset),
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

    def save_recommendation_snapshot(self, fingerprint: str, items: list[dict]) -> str:
        snapshot_id = str(uuid.uuid4())
        with self.db.transaction() as con:
            con.execute(
                "INSERT INTO recommendation_snapshots VALUES(?,?,?,?)",
                (snapshot_id, fingerprint, canonical(items), utcnow()),
            )
            con.execute(
                """DELETE FROM recommendation_snapshots
                   WHERE julianday(created_at) < julianday('now','-7 days')"""
            )
        return snapshot_id

    def get_recommendation_snapshot(self, snapshot_id: str) -> tuple[str, list[dict]]:
        with self.db.connect() as con:
            row = con.execute(
                """SELECT fingerprint,items_json FROM recommendation_snapshots
                   WHERE id=?""",
                (snapshot_id,),
            ).fetchone()
        if not row:
            raise ValueError("recommendation cursor expired or does not exist")
        return row["fingerprint"], json.loads(row["items_json"])

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
        stored = []
        for row in self.list_vacancies(limit=1000):
            if row["hh_id"] == vacancy_id:
                continue
            candidate = f"{row.get('title') or ''} {row.get('employer_name') or ''} {row.get('description') or ''}".casefold()
            score = SequenceMatcher(None, needle, candidate).ratio()
            if score >= threshold:
                a, b = sorted((vacancy_id, row["hh_id"]))
                stored.append((a, b, score))
                matches.append({"vacancy_id": row["hh_id"], "score": round(score, 3)})
        if stored:
            with self.db.transaction() as con:
                con.executemany("INSERT OR REPLACE INTO similar_vacancies VALUES(?,?,?)", stored)
        return sorted(matches, key=lambda x: -x["score"])
