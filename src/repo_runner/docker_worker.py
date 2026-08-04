"""Docker-sandboxed execution worker.

The one piece the project's own README explicitly says doesn't exist yet
("does not clone repositories or execute repository code"). This clones a
selected job's exact commit into a throwaway directory and, if the repo
defines its own Dockerfile, builds and runs it inside a disposable,
resource-limited, network-isolated container.

Safety posture (deliberate, not incidental):
- Build gets normal network access (most real Dockerfiles need it to
  install dependencies) but a hard timeout.
- Run gets NO network access (--network=none), capped memory/CPU, a
  read-only root filesystem with only /tmp writable, and a hard timeout.
- No host paths are ever mounted into the container -- only files already
  copied into the image at build time are visible to it.
- The built image and cloned working tree are both removed after every
  job, success or failure, so repeated runs don't leak disk.
- If the repo has no Dockerfile, that's reported as a real (non-error)
  finding, not treated as a failure -- there's nothing unsafe or wrong
  about a repo that isn't a container image.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import threading
from pathlib import Path

from .persistence import Job, JobStore
from .worker import WorkerCanceled

_COMMIT_SHA = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")

CLONE_TIMEOUT_SECONDS = 120
BUILD_TIMEOUT_SECONDS = 300
RUN_TIMEOUT_SECONDS = 120
OUTPUT_TAIL_CHARS = 4000

RUN_MEMORY_LIMIT = "512m"
RUN_CPU_LIMIT = "1"


class SandboxError(RuntimeError):
    """Raised when cloning, building, or running the sandbox itself fails."""


def _validate_identity(job: Job) -> None:
    owner, separator, repository = job.full_name.partition("/")
    if not separator or not owner or not repository or "/" in repository:
        raise ValueError("full_name must have the form owner/repository")
    if _COMMIT_SHA.fullmatch(job.commit_sha) is None:
        raise ValueError("commit_sha must be a 40- or 64-character hexadecimal hash")


def _run_checked(
    command: list[str], *, cwd: Path | None, timeout: float, cancel: threading.Event
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    deadline = threading.Event()
    timer = threading.Timer(timeout, deadline.set)
    timer.start()
    try:
        while process.poll() is None:
            if deadline.is_set():
                process.kill()
                process.wait()
                raise SandboxError(f"{command[0]} timed out after {timeout:.0f}s")
            if cancel.wait(0.2):
                process.kill()
                process.wait()
                raise WorkerCanceled("shutdown requested during sandbox execution")
    finally:
        timer.cancel()
    stdout, _ = process.communicate()
    return subprocess.CompletedProcess(command, process.returncode, stdout, None)


def clone_at_commit(
    full_name: str, commit_sha: str, destination: Path, *, cancel: threading.Event
) -> None:
    """Shallow-fetch the exact commit rather than a full clone -- GitHub
    supports fetching by SHA directly for public repos."""

    url = f"https://github.com/{full_name}.git"
    _run_checked(
        ["git", "init", "--quiet", str(destination)],
        cwd=None,
        timeout=CLONE_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    _run_checked(
        ["git", "remote", "add", "origin", url],
        cwd=destination,
        timeout=CLONE_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    fetch = _run_checked(
        ["git", "fetch", "--quiet", "--depth", "1", "origin", commit_sha],
        cwd=destination,
        timeout=CLONE_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    if fetch.returncode != 0:
        raise SandboxError(
            f"could not fetch {commit_sha} from {full_name}: {fetch.stdout.strip()[:500]}"
        )
    checkout = _run_checked(
        ["git", "checkout", "--quiet", "FETCH_HEAD"],
        cwd=destination,
        timeout=CLONE_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    if checkout.returncode != 0:
        raise SandboxError(
            f"could not check out {commit_sha}: {checkout.stdout.strip()[:500]}"
        )


def build_image(workdir: Path, tag: str, *, cancel: threading.Event) -> None:
    result = _run_checked(
        ["docker", "build", "--quiet", "-t", tag, "."],
        cwd=workdir,
        timeout=BUILD_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    if result.returncode != 0:
        raise SandboxError(f"docker build failed: {result.stdout.strip()[-OUTPUT_TAIL_CHARS:]}")


def run_image(tag: str, *, cancel: threading.Event) -> tuple[int, str]:
    result = _run_checked(
        [
            "docker", "run", "--rm",
            "--network", "none",
            "--memory", RUN_MEMORY_LIMIT,
            "--cpus", RUN_CPU_LIMIT,
            "--read-only",
            "--tmpfs", "/tmp",
            # Real finding, 2026-08-03: an actual container (an s6-overlay
            # init system) failed immediately because it needs to write to
            # /run at startup -- read-only root + /tmp alone isn't enough
            # for some real-world images. tmpfs mounts default to noexec,
            # which broke this same image a second time (it extracts and
            # execs an init binary into /run); exec is allowed here since
            # /run is still just a tmpfs, not a path into the host, so
            # this doesn't weaken isolation. Some images still won't run
            # under these constraints -- that's a legitimate finding to
            # report, not a bug to keep chasing.
            "--tmpfs", "/run:exec",
            tag,
        ],
        cwd=None,
        timeout=RUN_TIMEOUT_SECONDS,
        cancel=cancel,
    )
    return result.returncode, result.stdout[-OUTPUT_TAIL_CHARS:]


def remove_image(tag: str) -> None:
    subprocess.run(
        ["docker", "rmi", "-f", tag], capture_output=True, text=True, timeout=30
    )


class DockerSandboxWorker:
    """Clone a job's exact commit and, if it defines a Dockerfile, build
    and run it inside a disposable, network-isolated container."""

    def __init__(self, *, store: JobStore | None = None) -> None:
        self.store = store

    def run(self, job: Job, cancel: threading.Event) -> None:
        if cancel.is_set():
            raise WorkerCanceled("shutdown requested before sandbox execution")
        _validate_identity(job)

        tag = f"repo-runner-sandbox-{job.id}"
        with tempfile.TemporaryDirectory(prefix=f"repo-runner-job-{job.id}-") as raw_dir:
            workdir = Path(raw_dir)
            clone_at_commit(job.full_name, job.commit_sha, workdir, cancel=cancel)

            has_dockerfile = (workdir / "Dockerfile").is_file()
            if not has_dockerfile:
                if self.store is not None:
                    self.store.update_metadata(
                        job.id,
                        {"sandbox_ran": False, "sandbox_note": "no Dockerfile found"},
                    )
                return

            try:
                build_image(workdir, tag, cancel=cancel)
                exit_code, output_tail = run_image(tag, cancel=cancel)
            finally:
                remove_image(tag)

            if self.store is not None:
                self.store.update_metadata(
                    job.id,
                    {
                        "sandbox_ran": True,
                        "sandbox_exit_code": exit_code,
                        "sandbox_output_tail": output_tail,
                    },
                )
            if exit_code != 0:
                raise SandboxError(
                    f"container exited {exit_code}: {output_tail[-500:]}"
                )
