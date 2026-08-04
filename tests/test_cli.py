import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_runner.cli import main, parse_config
from repo_runner.lifecycle import JobState
from repo_runner.persistence import JobStore


class CliConfigTests(unittest.TestCase):
    def test_defaults(self) -> None:
        config = parse_config(["run"], environ={})
        self.assertEqual(config.database, Path("repo-runner.sqlite3"))
        self.assertTrue(config.worker_id)
        self.assertEqual(config.poll_interval, 1.0)
        self.assertEqual(config.heartbeat_interval, 10.0)
        self.assertEqual(config.stale_claim_timeout, 60.0)
        self.assertEqual(config.stale_recovery_interval, 30.0)
        self.assertEqual(config.graceful_shutdown_timeout, 30.0)

    def test_environment_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "jobs.db")
            config = parse_config(
                ["run"],
                environ={
                    "REPO_RUNNER_DATABASE": database,
                    "REPO_RUNNER_WORKER_ID": "env-worker",
                    "REPO_RUNNER_POLL_INTERVAL": "2",
                    "REPO_RUNNER_HEARTBEAT_INTERVAL": "3",
                    "REPO_RUNNER_STALE_CLAIM_TIMEOUT": "12",
                    "REPO_RUNNER_STALE_RECOVERY_INTERVAL": "4",
                    "REPO_RUNNER_GRACEFUL_SHUTDOWN_TIMEOUT": "5",
                },
            )
        self.assertEqual(config.database, Path(database))
        self.assertEqual(config.worker_id, "env-worker")
        self.assertEqual(config.poll_interval, 2)
        self.assertEqual(config.heartbeat_interval, 3)
        self.assertEqual(config.stale_claim_timeout, 12)

    def test_arguments_override_environment(self) -> None:
        config = parse_config(
            ["run", "--database", "cli.db", "--worker-id", "cli-worker"],
            environ={
                "REPO_RUNNER_DATABASE": "env.db",
                "REPO_RUNNER_WORKER_ID": "env-worker",
            },
        )
        self.assertEqual(config.database, Path("cli.db"))
        self.assertEqual(config.worker_id, "cli-worker")

    def test_invalid_timing_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            parse_config(
                [
                    "run",
                    "--heartbeat-interval",
                    "10",
                    "--stale-claim-timeout",
                    "5",
                ],
                environ={},
            )


class ScanCommandTests(unittest.TestCase):
    def test_scan_discovers_and_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            items = [
                {
                    "full_name": "owner/good",
                    "description": "a genuinely useful tool",
                    "stargazers_count": 50000,
                }
            ]
            with patch(
                "repo_runner.cli.discover_and_ingest",
                side_effect=lambda store, query, limit: [
                    store.create_job(
                        "owner/good",
                        "a" * 40,
                        metadata={
                            "description": "a genuinely useful tool",
                            "stars": 50000,
                            "pushed_at": "",
                        },
                        source=f"github-search:{query}",
                    ).id
                ],
            ):
                exit_code = main(
                    ["scan", "--database", str(database), "--query", "agents"]
                )
            self.assertEqual(exit_code, 0)
            store = JobStore(database)
            jobs = store.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].full_name, "owner/good")


class SubmitCommandTests(unittest.TestCase):
    def test_submit_creates_and_selects_a_job_bypassing_score(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            with patch(
                "repo_runner.cli.resolve_head_commit", return_value="b" * 40
            ):
                exit_code = main(
                    ["submit", "--database", str(database), "owner/repo"]
                )
            self.assertEqual(exit_code, 0)
            store = JobStore(database)
            jobs = store.list_jobs()
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0].state, JobState.SELECTED)
            self.assertEqual(jobs[0].source, "manual")

    def test_submit_custom_source(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            with patch(
                "repo_runner.cli.resolve_head_commit", return_value="c" * 40
            ):
                main(
                    [
                        "submit",
                        "--database",
                        str(database),
                        "owner/repo",
                        "--source",
                        "ruthchat",
                    ]
                )
            store = JobStore(database)
            self.assertEqual(store.list_jobs()[0].source, "ruthchat")


class StatusCommandTests(unittest.TestCase):
    def test_status_lists_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            store = JobStore(database)
            store.create_job("owner/repo", "a" * 40)
            exit_code = main(["status", "--database", str(database)])
            self.assertEqual(exit_code, 0)

    def test_status_with_no_jobs_does_not_error(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            JobStore(database)
            exit_code = main(["status", "--database", str(database)])
            self.assertEqual(exit_code, 0)

    def test_status_filters_by_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "jobs.sqlite3"
            store = JobStore(database)
            store.create_job("owner/repo", "a" * 40)
            exit_code = main(
                ["status", "--database", str(database), "--state", "selected"]
            )
            self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()

