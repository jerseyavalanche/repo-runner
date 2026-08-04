"""Worker interfaces and bounded built-in work."""

from __future__ import annotations

import re
import threading
from typing import Protocol

from .persistence import Job

_COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")


class Worker(Protocol):
    """A single-job handler with cooperative cancellation."""

    def run(self, job: Job, cancel: threading.Event) -> None:
        """Process one job or raise an exception describing the failure."""


class WorkerCanceled(RuntimeError):
    """Raised when work is canceled before it can safely start."""


class IdentityValidationWorker:
    """Validate immutable repository identity without executing repository code."""

    def run(self, job: Job, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise WorkerCanceled("shutdown requested before validation")
        owner, separator, repository = job.full_name.partition("/")
        if not separator or not owner or not repository or "/" in repository:
            raise ValueError("full_name must have the form owner/repository")
        if _COMMIT_SHA.fullmatch(job.commit_sha) is None:
            raise ValueError("commit_sha must be a 40- or 64-character hexadecimal hash")

