import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from repo_runner.docker_worker import (
    DockerSandboxWorker,
    SandboxError,
    _run_checked,
    _validate_identity,
    clone_at_commit,
)
from repo_runner.persistence import JobStore
from repo_runner.worker import WorkerCanceled


class FakeProcess:
    """Stands in for subprocess.Popen -- poll() returns None until told
    to finish, communicate() returns fixed output."""

    def __init__(self, output="", returncode=0, finish_after_polls=0):
        self.output = output
        self.returncode = returncode
        self._polls = 0
        self._finish_after = finish_after_polls
        self.killed = False

    def poll(self):
        self._polls += 1
        if self._polls > self._finish_after:
            return self.returncode
        return None

    def communicate(self):
        return self.output, None

    def kill(self):
        self.killed = True

    def wait(self):
        return self.returncode


class RunCheckedTests(unittest.TestCase):
    def test_returns_completed_process_on_success(self):
        cancel = threading.Event()
        with patch(
            "repo_runner.docker_worker.subprocess.Popen",
            return_value=FakeProcess(output="hello", returncode=0),
        ):
            result = _run_checked(["echo", "hi"], cwd=None, timeout=5, cancel=cancel)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "hello")

    def test_raises_sandbox_error_on_timeout(self):
        cancel = threading.Event()
        fake = FakeProcess(finish_after_polls=10_000_000)
        with patch("repo_runner.docker_worker.subprocess.Popen", return_value=fake):
            with self.assertRaises(SandboxError):
                _run_checked(["sleep", "999"], cwd=None, timeout=0.05, cancel=cancel)
        self.assertTrue(fake.killed)

    def test_raises_worker_canceled_when_cancel_is_set(self):
        cancel = threading.Event()
        cancel.set()
        fake = FakeProcess(finish_after_polls=10_000_000)
        with patch("repo_runner.docker_worker.subprocess.Popen", return_value=fake):
            with self.assertRaises(WorkerCanceled):
                _run_checked(["sleep", "999"], cwd=None, timeout=30, cancel=cancel)
        self.assertTrue(fake.killed)


class ValidateIdentityTests(unittest.TestCase):
    def _job(self, full_name, commit_sha):
        job = MagicMock()
        job.full_name = full_name
        job.commit_sha = commit_sha
        return job

    def test_accepts_valid_identity(self):
        _validate_identity(self._job("owner/repo", "a" * 40))  # does not raise

    def test_rejects_malformed_full_name(self):
        with self.assertRaises(ValueError):
            _validate_identity(self._job("not-a-repo-path", "a" * 40))

    def test_rejects_malformed_commit_sha(self):
        with self.assertRaises(ValueError):
            _validate_identity(self._job("owner/repo", "not-hex"))


class CloneAtCommitTests(unittest.TestCase):
    def test_raises_sandbox_error_when_fetch_fails(self):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "clone"

            def fake_run_checked(command, *, cwd, timeout, cancel):
                if command[:2] == ["git", "fetch"]:
                    return subprocess.CompletedProcess(command, 1, "unknown revision")
                return subprocess.CompletedProcess(command, 0, "")

            with patch(
                "repo_runner.docker_worker._run_checked", side_effect=fake_run_checked
            ):
                with self.assertRaisesRegex(SandboxError, "could not fetch"):
                    clone_at_commit("owner/repo", "a" * 40, dest, cancel=cancel)

    def test_raises_sandbox_error_when_checkout_fails(self):
        cancel = threading.Event()
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "clone"

            def fake_run_checked(command, *, cwd, timeout, cancel):
                if command[:2] == ["git", "checkout"]:
                    return subprocess.CompletedProcess(command, 1, "bad object")
                return subprocess.CompletedProcess(command, 0, "")

            with patch(
                "repo_runner.docker_worker._run_checked", side_effect=fake_run_checked
            ):
                with self.assertRaisesRegex(SandboxError, "could not check out"):
                    clone_at_commit("owner/repo", "a" * 40, dest, cancel=cancel)


class DockerSandboxWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")
        self.job = self.store.create_job("owner/repo", "a" * 40)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _worker(self):
        return DockerSandboxWorker(store=self.store)

    def test_rejects_before_doing_anything_if_identity_invalid(self):
        bad = self.store.create_job("bad-name", "z" * 40)
        with patch("repo_runner.docker_worker.clone_at_commit") as clone:
            with self.assertRaises(ValueError):
                self._worker().run(bad, threading.Event())
        clone.assert_not_called()

    def test_no_dockerfile_completes_without_building_and_records_metadata(self):
        def fake_clone(full_name, commit_sha, destination, *, cancel):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "README.md").write_text("no dockerfile here")

        with patch(
            "repo_runner.docker_worker.clone_at_commit", side_effect=fake_clone
        ), patch("repo_runner.docker_worker.build_image") as build:
            self._worker().run(self.job, threading.Event())
        build.assert_not_called()
        updated = self.store.get_job(self.job.id)
        self.assertEqual(updated.metadata["sandbox_ran"], False)

    def test_dockerfile_present_builds_and_runs_then_cleans_up(self):
        def fake_clone(full_name, commit_sha, destination, *, cancel):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "Dockerfile").write_text("FROM scratch")

        with patch(
            "repo_runner.docker_worker.clone_at_commit", side_effect=fake_clone
        ), patch("repo_runner.docker_worker.build_image") as build, patch(
            "repo_runner.docker_worker.run_image", return_value=(0, "all good")
        ) as run_image, patch(
            "repo_runner.docker_worker.remove_image"
        ) as remove_image:
            self._worker().run(self.job, threading.Event())
        build.assert_called_once()
        run_image.assert_called_once()
        remove_image.assert_called_once()
        updated = self.store.get_job(self.job.id)
        self.assertEqual(updated.metadata["sandbox_ran"], True)
        self.assertEqual(updated.metadata["sandbox_exit_code"], 0)
        self.assertEqual(updated.metadata["sandbox_output_tail"], "all good")

    def test_nonzero_exit_raises_sandbox_error_but_still_records_and_cleans_up(self):
        def fake_clone(full_name, commit_sha, destination, *, cancel):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "Dockerfile").write_text("FROM scratch")

        with patch(
            "repo_runner.docker_worker.clone_at_commit", side_effect=fake_clone
        ), patch("repo_runner.docker_worker.build_image"), patch(
            "repo_runner.docker_worker.run_image", return_value=(1, "it broke")
        ), patch("repo_runner.docker_worker.remove_image") as remove_image:
            with self.assertRaises(SandboxError):
                self._worker().run(self.job, threading.Event())
        remove_image.assert_called_once()
        updated = self.store.get_job(self.job.id)
        self.assertEqual(updated.metadata["sandbox_exit_code"], 1)

    def test_build_failure_still_removes_image_and_propagates(self):
        def fake_clone(full_name, commit_sha, destination, *, cancel):
            destination.mkdir(parents=True, exist_ok=True)
            (destination / "Dockerfile").write_text("FROM scratch")

        with patch(
            "repo_runner.docker_worker.clone_at_commit", side_effect=fake_clone
        ), patch(
            "repo_runner.docker_worker.build_image",
            side_effect=SandboxError("build broke"),
        ), patch("repo_runner.docker_worker.remove_image") as remove_image:
            with self.assertRaises(SandboxError):
                self._worker().run(self.job, threading.Event())
        remove_image.assert_called_once()

    def test_cancel_before_start_raises_worker_canceled(self):
        cancel = threading.Event()
        cancel.set()
        with patch("repo_runner.docker_worker.clone_at_commit") as clone:
            with self.assertRaises(WorkerCanceled):
                self._worker().run(self.job, cancel)
        clone.assert_not_called()

    def test_works_without_a_store(self):
        # store is optional -- the worker should still function, just
        # without persisting metadata.
        def fake_clone(full_name, commit_sha, destination, *, cancel):
            destination.mkdir(parents=True, exist_ok=True)

        worker = DockerSandboxWorker(store=None)
        with patch("repo_runner.docker_worker.clone_at_commit", side_effect=fake_clone):
            worker.run(self.job, threading.Event())  # does not raise


if __name__ == "__main__":
    unittest.main()
