import logging
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repo_runner import JobState, JobStore
from repo_runner.orchestrator import (
    EXIT_FORCED_SHUTDOWN,
    EXIT_OK,
    OrchestratorService,
    ServiceConfig,
)


class SuccessfulWorker:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, _job: object, _cancel: threading.Event) -> None:
        self.calls += 1


class FailingWorker:
    def run(self, _job: object, _cancel: threading.Event) -> None:
        raise RuntimeError("worker failed")


class BlockingWorker:
    def __init__(self, *, finish_on_cancel: bool) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.finish_on_cancel = finish_on_cancel

    def run(self, _job: object, cancel: threading.Event) -> None:
        self.started.set()
        if self.finish_on_cancel:
            cancel.wait(2)
        else:
            self.release.wait(2)


class RecordingRecovery:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.stopped = threading.Event()

    def start(self) -> None:
        self.started.set()

    def stop(self) -> None:
        self.stopped.set()


class OrchestratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = Path(self.temp_dir.name) / "jobs.sqlite3"
        self.store = JobStore(self.database)
        self.config = ServiceConfig(
            database=self.database,
            worker_id="test-worker",
            poll_interval=0.01,
            heartbeat_interval=0.02,
            stale_claim_timeout=1,
            stale_recovery_interval=0.02,
            graceful_shutdown_timeout=0.2,
        )
        self.logger = logging.getLogger(f"test.{id(self)}")
        self.logger.addHandler(logging.NullHandler())

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_selected_job(self, *, max_attempts: int = 3, name: str = "owner/repo") -> int:
        job = self.store.create_job(name, "a" * 40, max_attempts=max_attempts)
        for state in (JobState.ANALYZED, JobState.SCORED, JobState.SELECTED):
            job = self.store.set_state(job.id, state)
        return job.id

    def run_service(self, service: OrchestratorService) -> tuple[threading.Thread, list[int]]:
        result: list[int] = []
        thread = threading.Thread(target=lambda: result.append(service.run()))
        thread.start()
        return thread, result

    def wait_for_state(self, job_id: int, state: JobState) -> None:
        deadline = time.monotonic() + 2
        while self.store.get_job(job_id).state != state:
            if time.monotonic() >= deadline:
                self.fail(f"job {job_id} did not reach {state}")
            time.sleep(0.005)

    def test_atomic_claim_and_successful_processing_flow(self) -> None:
        job_id = self.add_selected_job()
        worker = SuccessfulWorker()
        service = OrchestratorService(self.config, worker, store=self.store, logger=self.logger)
        thread, result = self.run_service(service)
        self.wait_for_state(job_id, JobState.COMPLETED)
        service.request_shutdown()
        thread.join(2)

        self.assertEqual(result, [EXIT_OK])
        self.assertEqual(worker.calls, 1)
        completed = self.store.get_job(job_id)
        self.assertEqual(completed.attempt_count, 1)
        self.assertEqual(completed.claimed_by, "test-worker")

    def test_heartbeat_updates_during_work(self) -> None:
        job_id = self.add_selected_job()
        worker = BlockingWorker(finish_on_cancel=True)
        service = OrchestratorService(self.config, worker, store=self.store, logger=self.logger)
        thread, result = self.run_service(service)
        self.assertTrue(worker.started.wait(1))
        initial = self.store.get_job(job_id).heartbeat_at
        deadline = time.monotonic() + 1
        while self.store.get_job(job_id).heartbeat_at == initial:
            if time.monotonic() >= deadline:
                self.fail("heartbeat was not updated")
            time.sleep(0.005)
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])

    def test_retryable_worker_failure(self) -> None:
        job_id = self.add_selected_job(max_attempts=2)
        service = OrchestratorService(
            self.config, FailingWorker(), store=self.store, logger=self.logger
        )
        thread, result = self.run_service(service)
        deadline = time.monotonic() + 2
        while self.store.get_job(job_id).attempt_count < 2:
            if time.monotonic() >= deadline:
                self.fail("job was not retried")
            time.sleep(0.005)
        thread.join(2)
        failed = self.store.get_job(job_id)
        self.assertEqual(result, [])
        self.assertEqual(failed.state, JobState.FAILED)
        self.assertEqual(failed.attempt_count, 2)
        self.assertIn("RuntimeError: worker failed", failed.last_error)
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])

    def test_exhausted_retry_failure(self) -> None:
        job_id = self.add_selected_job(max_attempts=1)
        service = OrchestratorService(
            self.config, FailingWorker(), store=self.store, logger=self.logger
        )
        thread, result = self.run_service(service)
        self.wait_for_state(job_id, JobState.FAILED)
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])
        self.assertEqual(self.store.get_job(job_id).attempt_count, 1)

    def test_graceful_shutdown_while_idle(self) -> None:
        recovery = RecordingRecovery()
        service = OrchestratorService(
            self.config,
            SuccessfulWorker(),
            store=self.store,
            recovery=recovery,
            logger=self.logger,
        )
        thread, result = self.run_service(service)
        self.assertTrue(recovery.started.wait(1))
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])
        self.assertTrue(recovery.stopped.is_set())

    def test_graceful_shutdown_during_active_work(self) -> None:
        job_id = self.add_selected_job()
        worker = BlockingWorker(finish_on_cancel=True)
        service = OrchestratorService(self.config, worker, store=self.store, logger=self.logger)
        thread, result = self.run_service(service)
        self.assertTrue(worker.started.wait(1))
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])
        self.assertEqual(self.store.get_job(job_id).state, JobState.COMPLETED)

    def test_shutdown_timeout_safely_releases_active_work(self) -> None:
        job_id = self.add_selected_job(max_attempts=2)
        worker = BlockingWorker(finish_on_cancel=False)
        config = replace(self.config, graceful_shutdown_timeout=0.05)
        service = OrchestratorService(config, worker, store=self.store, logger=self.logger)
        thread, result = self.run_service(service)
        self.assertTrue(worker.started.wait(1))
        service.request_shutdown()
        thread.join(2)
        worker.release.set()
        self.assertEqual(result, [EXIT_FORCED_SHUTDOWN])
        recovered = self.store.get_job(job_id)
        self.assertEqual(recovered.state, JobState.SELECTED)
        self.assertIn("shutdown timeout", recovered.last_error)

    def test_startup_recovers_stale_claim_before_processing(self) -> None:
        clock_time = datetime.now(timezone.utc) - timedelta(minutes=2)

        class OldClock:
            def __call__(self) -> datetime:
                return clock_time

        old_store = JobStore(self.database, clock=OldClock())
        job = old_store.create_job("owner/stale", "b" * 40)
        for state in (JobState.ANALYZED, JobState.SCORED, JobState.SELECTED):
            job = old_store.set_state(job.id, state)
        old_store.claim_next("dead-worker")

        worker = SuccessfulWorker()
        service = OrchestratorService(self.config, worker, store=self.store, logger=self.logger)
        thread, result = self.run_service(service)
        self.wait_for_state(job.id, JobState.COMPLETED)
        service.request_shutdown()
        thread.join(2)
        self.assertEqual(result, [EXIT_OK])
        self.assertEqual(self.store.get_job(job.id).attempt_count, 2)


if __name__ == "__main__":
    unittest.main()

