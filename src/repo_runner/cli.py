"""Command-line interface for the repo-runner service."""

from __future__ import annotations

import argparse
import os
import signal
import socket
from pathlib import Path
from typing import Sequence

from .orchestrator import OrchestratorService, ServiceConfig, configure_logging
from .worker import IdentityValidationWorker

ENV_PREFIX = "REPO_RUNNER_"


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
    run = commands.add_parser("run", help="run one continuous worker")
    run.add_argument(
        "--database",
        type=Path,
        default=env.get(f"{ENV_PREFIX}DATABASE", "repo-runner.sqlite3"),
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


def main(arguments: Sequence[str] | None = None) -> int:
    config = parse_config(arguments)
    configure_logging()
    service = OrchestratorService(config, IdentityValidationWorker())
    install_signal_handlers(service)
    return service.run()

