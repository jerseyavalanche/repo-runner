"""Single-job orchestrator with heartbeats and graceful shutdown."""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .lifecycle import JobState
from .persistence import ClaimConflict, Job, JobStore, StaleClaimRecovery
from .worker import Worker

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_FORCED_SHUTDOWN = 2


@dataclass(frozen=True, slots=True)
class ServiceConfig:
    database: Path = Path("repo-runner.sqlite3")
    worker_id: str = "repo-runner-worker"
    poll_interval: float = 1.0
    heartbeat_interval: float = 10.0
    stale_claim_timeout: float = 60.0
    stale_recovery_interval: float = 30.0
    graceful_shutdown_timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.worker_id:
            raise ValueError("worker_id is required")
        intervals = {
            "poll_interval": self.poll_interval,
            "heartbeat_interval": self.heartbeat_interval,
            "stale_claim_timeout": self.stale_claim_timeout,
            "stale_recovery_interval": self.stale_recovery_interval,
            "graceful_shutdown_timeout": self.graceful_shutdown_timeout,
        }
        for name, value in intervals.items():
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.stale_claim_timeout <= self.heartbeat_interval:
            raise ValueError("stale_claim_timeout must exceed heartbeat_interval")


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    _fields = (
        "job_id",
        "full_name",
        "commit_sha",
        "worker_id",
        "state",
        "attempt_count",
        "error",
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "message": record.getMessage(),
        }
        for field in self._fields:
            if hasattr(record, field):
                payload[field] = getattr(record, field)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


class OrchestratorService:
    """Continuously claim and process at most one job at a time."""

    def __init__(
        self,
        config: ServiceConfig,
        worker: Worker,
        *,
        store: JobStore | None = None,
        recovery: StaleClaimRecovery | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self.config = config
        self.worker = worker
        self.store = store or JobStore(config.database)
        self.recovery = recovery or StaleClaimRecovery(
            self.store,
            stale_after=timedelta(seconds=config.stale_claim_timeout),
            interval=timedelta(seconds=config.stale_recovery_interval),
        )
        self.logger = logger or logging.getLogger("repo_runner.orchestrator")
        self._shutdown = threading.Event()
        self._shutdown_deadline: float | None = None
        self._current_job: Job | None = None
        self._lock = threading.Lock()

    @property
    def current_job(self) -> Job | None:
        with self._lock:
            return self._current_job

    def request_shutdown(self) -> None:
        """Stop claiming and begin the bounded graceful-shutdown period."""

        with self._lock:
            if not self._shutdown.is_set():
                self._shutdown_deadline = (
                    time.monotonic() + self.config.graceful_shutdown_timeout
                )
                self.logger.info(
                    "shutdown requested", extra={"worker_id": self.config.worker_id}
                )
            self._shutdown.set()

    def run(self) -> int:
        """Run until shutdown, returning a process exit status."""

        self.logger.info(
            "service starting",
            extra={"worker_id": self.config.worker_id},
        )
        try:
            self.recovery.start()
            while not self._shutdown.is_set():
                job = self.store.claim_next(self.config.worker_id)
                if job is None:
                    self._shutdown.wait(self.config.poll_interval)
                    continue
                status = self._process(job)
                if status != EXIT_OK:
                    return status
            return EXIT_OK
        except Exception as error:
            self.logger.exception(
                "service failed",
                extra={"worker_id": self.config.worker_id, "error": str(error)},
            )
            return EXIT_ERROR
        finally:
            self.recovery.stop()
            self.logger.info(
                "service stopped", extra={"worker_id": self.config.worker_id}
            )

    def _process(self, claimed: Job) -> int:
        running = self.store.set_state(claimed.id, JobState.RUNNING)
        with self._lock:
            self._current_job = running
        self._log_job(logging.INFO, "job started", running)

        heartbeat_stop = threading.Event()
        heartbeat_error: list[Exception] = []
        heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            args=(running, heartbeat_stop, heartbeat_error),
            name=f"heartbeat-{running.id}",
            daemon=True,
        )
        handler_done = threading.Event()
        handler_error: list[BaseException] = []

        def invoke() -> None:
            try:
                self.worker.run(running, self._shutdown)
            except BaseException as error:
                handler_error.append(error)
            finally:
                handler_done.set()

        handler = threading.Thread(
            target=invoke, name=f"worker-job-{running.id}", daemon=True
        )
        heartbeat.start()
        handler.start()

        forced = False
        while not handler_done.wait(min(self.config.poll_interval, 0.1)):
            if self._shutdown.is_set() and self._shutdown_expired():
                forced = True
                break

        heartbeat_stop.set()
        heartbeat.join()
        try:
            if forced:
                error = "graceful shutdown timeout exceeded"
                released = self.store.release_for_retry(
                    running.id, self.config.worker_id, error
                )
                self._log_job(logging.ERROR, "job interrupted", released, error=error)
                return EXIT_FORCED_SHUTDOWN
            if heartbeat_error:
                raise heartbeat_error[0]
            if handler_error:
                error = self._error_text(handler_error[0])
                released = self.store.release_for_retry(
                    running.id, self.config.worker_id, error
                )
                self._log_job(logging.ERROR, "job failed", released, error=error)
                return EXIT_OK
            verified = self.store.set_state(running.id, JobState.VERIFIED)
            completed = self.store.set_state(verified.id, JobState.COMPLETED)
            self._log_job(logging.INFO, "job completed", completed)
            return EXIT_OK
        except ClaimConflict as error:
            self._log_job(logging.ERROR, "job claim lost", running, error=str(error))
            return EXIT_ERROR
        except Exception as error:
            self._log_job(
                logging.ERROR,
                "job finalization failed",
                running,
                error=str(error),
                exc_info=True,
            )
            return EXIT_ERROR
        finally:
            with self._lock:
                self._current_job = None

    def _heartbeat_loop(
        self,
        job: Job,
        stop: threading.Event,
        errors: list[Exception],
    ) -> None:
        while not stop.wait(self.config.heartbeat_interval):
            try:
                updated = self.store.heartbeat(job.id, self.config.worker_id)
                self._log_job(logging.DEBUG, "job heartbeat", updated)
            except Exception as error:
                errors.append(error)
                self._log_job(
                    logging.ERROR, "job heartbeat failed", job, error=str(error)
                )
                self.request_shutdown()
                return

    def _shutdown_expired(self) -> bool:
        with self._lock:
            deadline = self._shutdown_deadline
        return deadline is not None and time.monotonic() >= deadline

    @staticmethod
    def _error_text(error: BaseException) -> str:
        message = str(error).strip()
        return f"{type(error).__name__}: {message}" if message else type(error).__name__

    def _log_job(
        self,
        level: int,
        message: str,
        job: Job,
        *,
        error: str | None = None,
        exc_info: bool = False,
    ) -> None:
        extra: dict[str, object] = {
            "job_id": job.id,
            "full_name": job.full_name,
            "commit_sha": job.commit_sha,
            "worker_id": self.config.worker_id,
            "state": job.state,
            "attempt_count": job.attempt_count,
        }
        if error is not None:
            extra["error"] = error
        self.logger.log(level, message, extra=extra, exc_info=exc_info)

