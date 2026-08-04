import tempfile
import unittest
from pathlib import Path

from repo_runner.cli import parse_config


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


if __name__ == "__main__":
    unittest.main()

