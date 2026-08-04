import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repo_runner import (
    ClaimConflict,
    InvalidTransition,
    JobState,
    JobStore,
    StaleClaimRecovery,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now


class JobStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.clock = MutableClock()
        self.store = JobStore(self.database, clock=self.clock)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def select_job(self, *, max_attempts: int = 3) -> int:
        job = self.store.create_job("owner/repository", "a" * 40, max_attempts=max_attempts)
        for state in (JobState.ANALYZED, JobState.SCORED, JobState.SELECTED):
            job = self.store.set_state(job.id, state)
        return job.id

    def test_repository_identity_is_unique_and_immutable(self) -> None:
        job_id = self.select_job()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_job("owner/repository", "a" * 40)
        with closing(self.store._connect()) as connection:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "identity is immutable"):
                connection.execute(
                    "UPDATE jobs SET commit_sha = ? WHERE id = ?", ("b" * 40, job_id)
                )

    def test_concurrent_claiming_assigns_job_once(self) -> None:
        job_id = self.select_job()
        barrier = threading.Barrier(3)
        claims: list[object] = []

        def claim(worker: str) -> None:
            barrier.wait()
            claims.append(self.store.claim_next(worker))

        threads = [threading.Thread(target=claim, args=(f"worker-{i}",)) for i in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        successful = [claim for claim in claims if claim is not None]
        self.assertEqual(len(successful), 1)
        self.assertEqual(successful[0].id, job_id)
        self.assertEqual(self.store.get_job(job_id).attempt_count, 1)

    def test_stale_claim_recovery_requeues_work(self) -> None:
        job_id = self.select_job()
        self.store.claim_next("worker-1")
        self.clock.now += timedelta(minutes=6)

        self.assertEqual(
            self.store.recover_stale_claims(timedelta(minutes=5)), (1, 0)
        )
        recovered = self.store.get_job(job_id)
        self.assertEqual(recovered.state, JobState.SELECTED)
        self.assertIsNone(recovered.claimed_by)
        self.assertEqual(recovered.last_error, "claim heartbeat expired")

    def test_periodic_recovery_runs_at_startup_and_while_running(self) -> None:
        first_id = self.select_job()
        self.store.claim_next("worker-1")
        self.clock.now += timedelta(minutes=6)
        recovery = StaleClaimRecovery(
            self.store,
            stale_after=timedelta(minutes=5),
            interval=timedelta(milliseconds=10),
        )
        recovery.start()
        self.assertEqual(self.store.get_job(first_id).state, JobState.SELECTED)

        second = self.store.create_job("owner/second", "b" * 40)
        for state in (JobState.ANALYZED, JobState.SCORED, JobState.SELECTED):
            second = self.store.set_state(second.id, state)
        self.store.claim_next("worker-2")
        self.clock.now += timedelta(minutes=6)
        deadline = time.monotonic() + 1
        while self.store.get_job(second.id).state == JobState.CLAIMED:
            if time.monotonic() >= deadline:
                self.fail("periodic stale recovery did not run")
            time.sleep(0.01)
        recovery.stop()
        self.assertFalse(recovery.running)
        self.assertIsNone(recovery.last_error)

    def test_retry_limit_preserves_terminal_failure(self) -> None:
        job_id = self.select_job(max_attempts=2)
        first = self.store.claim_next("worker-1")
        self.assertIsNotNone(first)
        retried = self.store.release_for_retry(job_id, "worker-1", "first error")
        self.assertEqual(retried.state, JobState.SELECTED)

        second = self.store.claim_next("worker-2")
        self.assertEqual(second.attempt_count, 2)
        failed = self.store.release_for_retry(job_id, "worker-2", "final error")
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.last_error, "final error")
        self.assertEqual(failed.claimed_by, "worker-2")
        self.assertIsNotNone(failed.claimed_at)
        self.assertIsNotNone(failed.heartbeat_at)
        self.assertIsNone(self.store.claim_next("worker-3"))
        self.assertEqual(self.store.get_job(job_id).state, JobState.FAILED)

    def test_only_owner_can_heartbeat_or_release(self) -> None:
        job_id = self.select_job()
        self.store.claim_next("owner")
        with self.assertRaises(ClaimConflict):
            self.store.heartbeat(job_id, "other")
        with self.assertRaises(ClaimConflict):
            self.store.release_for_retry(job_id, "other", "error")

    def test_persistence_validates_transitions_and_preserves_completion(self) -> None:
        job_id = self.select_job()
        with self.assertRaises(InvalidTransition):
            self.store.set_state(job_id, JobState.COMPLETED)
        self.store.claim_next("worker")
        for state in (JobState.RUNNING, JobState.VERIFIED, JobState.COMPLETED):
            completed = self.store.set_state(job_id, state)
        self.assertEqual(completed.state, JobState.COMPLETED)
        with self.assertRaises(InvalidTransition):
            self.store.set_state(job_id, JobState.DISCOVERED)
        self.assertEqual(self.store.get_job(job_id).state, JobState.COMPLETED)


if __name__ == "__main__":
    unittest.main()

