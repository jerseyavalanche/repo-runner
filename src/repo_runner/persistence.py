"""SQLite persistence and atomic coordination for repository jobs."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .lifecycle import InvalidTransition, JobState, transition

SCHEMA_VERSION = 1


class JobNotFound(LookupError):
    """Raised when a requested job does not exist."""


class ClaimConflict(RuntimeError):
    """Raised when a worker no longer owns a job claim."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    full_name: str
    commit_sha: str
    state: JobState
    attempt_count: int
    max_attempts: int
    claimed_at: datetime | None
    claimed_by: str | None
    heartbeat_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


class JobStore:
    """Durable job storage with transactionally atomic claims."""

    def __init__(
        self,
        database: str | Path,
        *,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.database = str(database)
        self._clock = clock
        self.initialize_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize_schema(self) -> None:
        with closing(self._connect()) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {version} is newer than supported {SCHEMA_VERSION}"
                )
            if version == 0:
                connection.executescript(
                    """
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS jobs (
                        id INTEGER PRIMARY KEY,
                        full_name TEXT NOT NULL CHECK (length(full_name) > 0),
                        commit_sha TEXT NOT NULL CHECK (length(commit_sha) > 0),
                        state TEXT NOT NULL CHECK (state IN (
                            'discovered', 'analyzed', 'scored', 'selected',
                            'claimed', 'running', 'verified', 'failed', 'completed'
                        )),
                        attempt_count INTEGER NOT NULL DEFAULT 0
                            CHECK (attempt_count >= 0),
                        max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
                        claimed_at TEXT,
                        claimed_by TEXT,
                        heartbeat_at TEXT,
                        last_error TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        UNIQUE (full_name, commit_sha),
                        CHECK (attempt_count <= max_attempts),
                        CHECK (
                            (state IN ('claimed', 'running') AND
                             claimed_at IS NOT NULL AND claimed_by IS NOT NULL AND
                             heartbeat_at IS NOT NULL)
                            OR state NOT IN ('claimed', 'running')
                        )
                    );
                    CREATE INDEX IF NOT EXISTS jobs_claimable_idx
                        ON jobs(state, attempt_count, max_attempts, created_at, id);
                    CREATE INDEX IF NOT EXISTS jobs_stale_idx ON jobs(state, heartbeat_at);
                    CREATE TRIGGER IF NOT EXISTS jobs_identity_immutable
                    BEFORE UPDATE OF full_name, commit_sha ON jobs
                    WHEN OLD.full_name != NEW.full_name OR OLD.commit_sha != NEW.commit_sha
                    BEGIN
                        SELECT RAISE(ABORT, 'repository identity is immutable');
                    END;
                    PRAGMA user_version = 1;
                    COMMIT;
                    """
                )

    def create_job(
        self, full_name: str, commit_sha: str, *, max_attempts: int = 3
    ) -> Job:
        if not full_name or not commit_sha:
            raise ValueError("full_name and commit_sha are required")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        now = _timestamp(self._clock())
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                INSERT INTO jobs (
                    full_name, commit_sha, state, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (full_name, commit_sha, JobState.DISCOVERED, max_attempts, now, now),
            )
            return self.get_job(cursor.lastrowid, connection=connection)

    def get_job(
        self, job_id: int, *, connection: sqlite3.Connection | None = None
    ) -> Job:
        owns_connection = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFound(job_id)
            return self._to_job(row)
        finally:
            if owns_connection:
                connection.close()

    def set_state(self, job_id: int, target: JobState) -> Job:
        """Atomically validate and apply a non-claim lifecycle transition."""

        if target == JobState.CLAIMED:
            raise InvalidTransition("jobs must enter claimed via claim_next")
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self.get_job(job_id, connection=connection)
            if (
                current.state in {JobState.CLAIMED, JobState.RUNNING}
                and target == JobState.SELECTED
            ):
                connection.rollback()
                raise InvalidTransition(
                    "active jobs must be retried via release_for_retry"
                )
            transition(current.state, target)
            now = _timestamp(self._clock())
            connection.execute(
                "UPDATE jobs SET state = ?, updated_at = ? WHERE id = ?",
                (target, now, job_id),
            )
            connection.commit()
            return self.get_job(job_id, connection=connection)

    def claim_next(self, worker_id: str) -> Job | None:
        """Atomically claim the oldest eligible selected job."""

        if not worker_id:
            raise ValueError("worker_id is required")
        now = _timestamp(self._clock())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id FROM jobs
                WHERE state = ? AND attempt_count < max_attempts
                ORDER BY created_at, id
                LIMIT 1
                """,
                (JobState.SELECTED,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            cursor = connection.execute(
                """
                UPDATE jobs
                SET state = ?, attempt_count = attempt_count + 1,
                    claimed_at = ?, claimed_by = ?, heartbeat_at = ?,
                    last_error = NULL, updated_at = ?
                WHERE id = ? AND state = ? AND attempt_count < max_attempts
                """,
                (
                    JobState.CLAIMED,
                    now,
                    worker_id,
                    now,
                    now,
                    row["id"],
                    JobState.SELECTED,
                ),
            )
            if cursor.rowcount != 1:
                connection.rollback()
                return None
            connection.commit()
            return self.get_job(row["id"], connection=connection)

    def heartbeat(self, job_id: int, worker_id: str) -> Job:
        now = _timestamp(self._clock())
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET heartbeat_at = ?, updated_at = ?
                WHERE id = ? AND claimed_by = ? AND state IN (?, ?)
                """,
                (
                    now,
                    now,
                    job_id,
                    worker_id,
                    JobState.CLAIMED,
                    JobState.RUNNING,
                ),
            )
            if cursor.rowcount != 1:
                raise ClaimConflict(f"worker {worker_id!r} does not own job {job_id}")
            return self.get_job(job_id, connection=connection)

    def release_for_retry(self, job_id: int, worker_id: str, error: str) -> Job:
        """Release owned work for retry, or fail it after its final attempt."""

        if not error:
            raise ValueError("error is required")
        now = _timestamp(self._clock())
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = self.get_job(job_id, connection=connection)
            if job.claimed_by != worker_id or job.state not in {
                JobState.CLAIMED,
                JobState.RUNNING,
            }:
                connection.rollback()
                raise ClaimConflict(f"worker {worker_id!r} does not own job {job_id}")
            target = (
                JobState.FAILED
                if job.attempt_count >= job.max_attempts
                else JobState.SELECTED
            )
            transition(job.state, target)
            if target == JobState.SELECTED:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, claimed_at = NULL, claimed_by = NULL,
                        heartbeat_at = NULL, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target, error, now, job_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, last_error = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (target, error, now, job_id),
                )
            connection.commit()
            return self.get_job(job_id, connection=connection)

    def recover_stale_claims(self, stale_after: timedelta) -> tuple[int, int]:
        """Recover stale work, returning counts of retried and failed jobs."""

        if stale_after.total_seconds() <= 0:
            raise ValueError("stale_after must be positive")
        now_value = self._clock()
        now = _timestamp(now_value)
        cutoff = _timestamp(now_value - stale_after)
        error = "claim heartbeat expired"
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            retried = connection.execute(
                """
                UPDATE jobs
                SET state = ?, claimed_at = NULL, claimed_by = NULL,
                    heartbeat_at = NULL, last_error = ?, updated_at = ?
                WHERE state IN (?, ?) AND heartbeat_at < ?
                    AND attempt_count < max_attempts
                """,
                (
                    JobState.SELECTED,
                    error,
                    now,
                    JobState.CLAIMED,
                    JobState.RUNNING,
                    cutoff,
                ),
            ).rowcount
            failed = connection.execute(
                """
                UPDATE jobs
                SET state = ?, last_error = ?, updated_at = ?
                WHERE state IN (?, ?) AND heartbeat_at < ?
                    AND attempt_count >= max_attempts
                """,
                (
                    JobState.FAILED,
                    error,
                    now,
                    JobState.CLAIMED,
                    JobState.RUNNING,
                    cutoff,
                ),
            ).rowcount
            connection.commit()
            return retried, failed

    @staticmethod
    def _to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"],
            full_name=row["full_name"],
            commit_sha=row["commit_sha"],
            state=JobState(row["state"]),
            attempt_count=row["attempt_count"],
            max_attempts=row["max_attempts"],
            claimed_at=_parse_timestamp(row["claimed_at"]),
            claimed_by=row["claimed_by"],
            heartbeat_at=_parse_timestamp(row["heartbeat_at"]),
            last_error=row["last_error"],
            created_at=_parse_timestamp(row["created_at"]),  # type: ignore[arg-type]
            updated_at=_parse_timestamp(row["updated_at"]),  # type: ignore[arg-type]
        )


class StaleClaimRecovery:
    """Run stale-claim recovery immediately and at a fixed interval."""

    def __init__(
        self,
        store: JobStore,
        *,
        stale_after: timedelta,
        interval: timedelta,
    ) -> None:
        if interval.total_seconds() <= 0:
            raise ValueError("interval must be positive")
        self.store = store
        self.stale_after = stale_after
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: Exception | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self.store.recover_stale_claims(self.stale_after)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="stale-claim-recovery", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        seconds = self.interval.total_seconds()
        while not self._stop.wait(seconds):
            try:
                self.store.recover_stale_claims(self.stale_after)
                self.last_error = None
            except Exception as error:  # keep recovery alive after transient DB errors
                self.last_error = error

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()

    def __enter__(self) -> StaleClaimRecovery:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

