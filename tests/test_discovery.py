import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from repo_runner.discovery import (
    Candidate,
    DiscoveryError,
    discover_and_ingest,
    item_to_candidate,
    resolve_head_commit,
    search_github,
)
from repo_runner.persistence import JobStore


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class SearchGithubTests(unittest.TestCase):
    def test_parses_items_from_gh_output(self):
        payload = {"items": [{"full_name": "owner/repo", "stargazers_count": 5}]}
        with patch(
            "repo_runner.discovery.subprocess.run",
            return_value=_completed(stdout=json.dumps(payload)),
        ) as run:
            items = search_github("agents", limit=5)
        self.assertEqual(items, payload["items"])
        args = run.call_args.args[0]
        self.assertIn("gh", args)
        self.assertTrue(any("agents" in part for part in args))

    def test_raises_on_nonzero_exit(self):
        with patch(
            "repo_runner.discovery.subprocess.run",
            return_value=_completed(returncode=1, stderr="rate limited"),
        ):
            with self.assertRaisesRegex(DiscoveryError, "rate limited"):
                search_github("agents")

    def test_raises_on_invalid_json(self):
        with patch(
            "repo_runner.discovery.subprocess.run",
            return_value=_completed(stdout="not json"),
        ):
            with self.assertRaises(DiscoveryError):
                search_github("agents")

    def test_rejects_empty_query(self):
        with self.assertRaises(ValueError):
            search_github("")


class ResolveHeadCommitTests(unittest.TestCase):
    def test_returns_sha(self):
        with patch(
            "repo_runner.discovery.subprocess.run",
            return_value=_completed(stdout="a" * 40 + "\n"),
        ):
            self.assertEqual(resolve_head_commit("owner/repo"), "a" * 40)

    def test_raises_on_empty_sha(self):
        with patch(
            "repo_runner.discovery.subprocess.run", return_value=_completed(stdout="")
        ):
            with self.assertRaises(DiscoveryError):
                resolve_head_commit("owner/repo")

    def test_raises_on_failure(self):
        with patch(
            "repo_runner.discovery.subprocess.run",
            return_value=_completed(returncode=1, stderr="not found"),
        ):
            with self.assertRaises(DiscoveryError):
                resolve_head_commit("owner/repo")


class ItemToCandidateTests(unittest.TestCase):
    def test_maps_known_fields(self):
        item = {
            "full_name": "owner/repo",
            "description": "does things",
            "stargazers_count": 100,
            "language": "Python",
            "pushed_at": "2026-08-01T00:00:00Z",
            "html_url": "https://github.com/owner/repo",
        }
        candidate = item_to_candidate(item)
        self.assertEqual(
            candidate,
            Candidate(
                full_name="owner/repo",
                description="does things",
                stars=100,
                language="Python",
                pushed_at="2026-08-01T00:00:00Z",
                url="https://github.com/owner/repo",
            ),
        )

    def test_handles_missing_optional_fields(self):
        candidate = item_to_candidate({"full_name": "owner/repo"})
        self.assertEqual(candidate.description, "")
        self.assertEqual(candidate.stars, 0)
        self.assertIsNone(candidate.language)


class DiscoverAndIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_ingests_new_candidates_with_metadata(self):
        items = [
            {
                "full_name": "owner/one",
                "description": "first",
                "stargazers_count": 10,
                "language": "Python",
                "pushed_at": "2026-08-01T00:00:00Z",
                "html_url": "https://github.com/owner/one",
            }
        ]
        with patch(
            "repo_runner.discovery.search_github", return_value=items
        ), patch(
            "repo_runner.discovery.resolve_head_commit", return_value="a" * 40
        ):
            inserted = discover_and_ingest(self.store, "agents", limit=5)
        self.assertEqual(len(inserted), 1)
        job = self.store.get_job(inserted[0])
        self.assertEqual(job.full_name, "owner/one")
        self.assertEqual(job.commit_sha, "a" * 40)
        self.assertEqual(job.metadata["stars"], 10)
        self.assertEqual(job.source, "github-search:agents")

    def test_skips_duplicate_full_name_and_commit(self):
        items = [{"full_name": "owner/one", "stargazers_count": 1}]
        with patch(
            "repo_runner.discovery.search_github", return_value=items
        ), patch(
            "repo_runner.discovery.resolve_head_commit", return_value="a" * 40
        ):
            first = discover_and_ingest(self.store, "agents")
            second = discover_and_ingest(self.store, "agents")
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 0)

    def test_skips_candidates_whose_head_cannot_be_resolved(self):
        items = [
            {"full_name": "owner/bad"},
            {"full_name": "owner/good"},
        ]

        def fake_resolve(full_name):
            if full_name == "owner/bad":
                raise DiscoveryError("no HEAD")
            return "b" * 40

        with patch(
            "repo_runner.discovery.search_github", return_value=items
        ), patch(
            "repo_runner.discovery.resolve_head_commit", side_effect=fake_resolve
        ):
            inserted = discover_and_ingest(self.store, "agents")
        self.assertEqual(len(inserted), 1)
        self.assertEqual(self.store.get_job(inserted[0]).full_name, "owner/good")

    def test_custom_source_label(self):
        items = [{"full_name": "owner/one"}]
        with patch(
            "repo_runner.discovery.search_github", return_value=items
        ), patch(
            "repo_runner.discovery.resolve_head_commit", return_value="c" * 40
        ):
            inserted = discover_and_ingest(
                self.store, "agents", source_label="ruthchat-submit"
            )
        self.assertEqual(self.store.get_job(inserted[0]).source, "ruthchat-submit")


if __name__ == "__main__":
    unittest.main()
