"""Command-line interface for the repo-runner service."""

from __future__ import annotations

import argparse
import os
import signal
import socket
from pathlib import Path
from typing import Sequence

from .discovery import discover_and_ingest, resolve_head_commit
from .docker_worker import DockerSandboxWorker
from .lifecycle import JobState
from .orchestrator import OrchestratorService, ServiceConfig, configure_logging
from .persistence import Job, JobStore
from .scoring import DEFAULT_SELECTION_THRESHOLD, advance_discovered_jobs
from .scout import SEARCHES, collect, save_report

ENV_PREFIX = "REPO_RUNNER_"
DEFAULT_DATABASE = "repo-runner.sqlite3"


def _default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser(environ: dict[str, str] | None = None) -> argparse.ArgumentParser:
    env = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="repo-runner")
    commands = parser.add_subparsers(dest="command", required=True)

    scout = commands.add_parser("scout", help="research public Android and off-grid projects on a phone")
    scout.add_argument("--category", choices=list(SEARCHES) + ["all"], default="all")
    scout.add_argument("--limit", type=int, default=10, help="results per search (1-30)")
    scout.add_argument("--output", type=Path, default=Path("reports"))

    run = commands.add_parser("run", help="run one continuous worker")
    run.add_argument(
        "--database",
        type=Path,
        default=env.get(f"{ENV_PREFIX}DATABASE", DEFAULT_DATABASE),
    )
    run.add_argument(
        "--worker-id",
        default=env.get(f"{ENV_PREFIX}WORKER_ID", _default_worker_id()),
    )
    intervals = (
        ("poll-interval", "POLL_INTERVAL", 1.0),
        ("heartbeat-interval", "HEARTBEAT_INTERVAL", 10.0),
        ("stale-claim-timeout", "STALE_CLAIM_TIMEOUT", 60.0),
        ("stale-recovery-interval", "STALE_RECOVERY_INTERVAL", 30.0),
        ("graceful-shutdown-timeout", "GRACEFUL_SHUTDOWN_TIMEOUT", 30.0),
    )
    for option, variable, default in intervals:
        run.add_argument(
            f"--{option}",
            type=_positive_float,
            default=env.get(f"{ENV_PREFIX}{variable}", str(default)),
            metavar="SECONDS",
        )

    scan = commands.add_parser(
        "scan", help="discover candidate repos on GitHub and score them"
    )
    scan.add_argument("--database", type=Path, default=Path(DEFAULT_DATABASE))
    scan.add_argument("--query", required=True, help="GitHub search query")
    scan.add_argument("--limit", type=int, default=10)
    scan.add_argument("--threshold", type=float, default=DEFAULT_SELECTION_THRESHOLD)

    submit = commands.add_parser(
        "submit", help="manually queue one specific repo, bypassing scoring"
    )
    submit.add_argument("--database", type=Path, default=Path(DEFAULT_DATABASE))
    submit.add_argument("full_name", help="owner/repository")
    submit.add_argument("--source", default="manual")

    status = commands.add_parser("status", help="list recent jobs")
    status.add_argument("--database", type=Path, default=Path(DEFAULT_DATABASE))
    status.add_argument("--state", default=None, choices=[s.value for s in JobState])
    status.add_argument("--limit", type=int, default=20)

    return parser


def parse_config(
    arguments: Sequence[str] | None = None,
    *,
    environ: dict[str, str] | None = None,
) -> ServiceConfig:
    parser = build_parser(environ)
    options = parser.parse_args(arguments)
    try:
        return ServiceConfig(
            database=options.database,
            worker_id=options.worker_id,
            poll_interval=options.poll_interval,
            heartbeat_interval=options.heartbeat_interval,
            stale_claim_timeout=options.stale_claim_timeout,
            stale_recovery_interval=options.stale_recovery_interval,
            graceful_shutdown_timeout=options.graceful_shutdown_timeout,
        )
    except ValueError as error:
        parser.error(str(error))


def install_signal_handlers(service: OrchestratorService) -> None:
    def request_shutdown(_signum: int, _frame: object) -> None:
        service.request_shutdown()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)


def _format_job(job: Job) -> str:
    score = f"{job.score:.1f}" if job.score is not None else "-"
    description = ""
    if job.metadata and job.metadata.get("description"):
        description = f" -- {job.metadata['description'][:80]}"
    return f"[{job.id}] {job.full_name} ({job.commit_sha[:8]}) state={job.state} score={score}{description}"


def _run(options: argparse.Namespace) -> int:
    config = ServiceConfig(
        database=options.database,
        worker_id=options.worker_id,
        poll_interval=options.poll_interval,
        heartbeat_interval=options.heartbeat_interval,
        stale_claim_timeout=options.stale_claim_timeout,
        stale_recovery_interval=options.stale_recovery_interval,
        graceful_shutdown_timeout=options.graceful_shutdown_timeout,
    )
    configure_logging()
    store = JobStore(config.database)
    worker = DockerSandboxWorker(store=store)
    service = OrchestratorService(config, worker, store=store)
    install_signal_handlers(service)
    return service.run()


def _scan(options: argparse.Namespace) -> int:
    store = JobStore(options.database)
    inserted = discover_and_ingest(store, options.query, limit=options.limit)
    print(f"discovered {len(inserted)} new job(s) for query {options.query!r}")
    results = advance_discovered_jobs(store, threshold=options.threshold)
    for job in results:
        print(_format_job(job))
    return 0


def _submit(options: argparse.Namespace) -> int:
    store = JobStore(options.database)
    commit_sha = resolve_head_commit(options.full_name)
    job = store.create_job(options.full_name, commit_sha, source=options.source)
    job = store.set_state(job.id, JobState.ANALYZED)
    job = store.record_score(job.id, 100.0)
    job = store.set_state(job.id, JobState.SELECTED)
    print(_format_job(job))
    return 0


def _status(options: argparse.Namespace) -> int:
    store = JobStore(options.database)
    state = JobState(options.state) if options.state else None
    jobs = store.list_jobs(state=state, limit=options.limit)
    if not jobs:
        print("no jobs")
        return 0
    for job in jobs:
        print(_format_job(job))
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    parser = build_parser()
    options = parser.parse_args(arguments)
    if options.command == "scout":
        if not 1 <= options.limit <= 30:
            parser.error("--limit must be between 1 and 30")
        categories = list(SEARCHES) if options.category == "all" else [options.category]
        rows, errors = collect(categories, limit=options.limit)
        json_path, md_path = save_report(options.output, rows, errors)
        print(f"Found {len(rows)} repositories; report: {md_path}; data: {json_path}")
        for error in errors:
            print(f"Search error: {error}")
        return 1 if errors else 0
    if options.command == "run":
        try:
            return _run(options)
        except ValueError as error:
            parser.error(str(error))
    if options.command == "scan":
        return _scan(options)
    if options.command == "submit":
        return _submit(options)
    if options.command == "status":
        return _status(options)
    parser.error(f"unknown command {options.command!r}")
    return 2
