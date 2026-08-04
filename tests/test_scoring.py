import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from repo_runner.lifecycle import JobState
from repo_runner.persistence import JobStore
from repo_runner.scoring import (
    DEFAULT_SELECTION_THRESHOLD,
    advance_discovered_jobs,
    score_job,
)

FIXED_NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


class ScoreJobTests(unittest.TestCase):
    def test_high_stars_recent_push_good_description_scores_high(self):
        metadata = {
            "stars": 50000,
            "pushed_at": (FIXED_NOW - timedelta(days=1)).isoformat(),
            "description": "A genuinely useful, well-described automation tool.",
        }
        score = score_job(metadata, now=FIXED_NOW)
        self.assertGreater(score, 80)

    def test_no_stars_stale_push_empty_description_scores_zero(self):
        metadata = {
            "stars": 0,
            "pushed_at": (FIXED_NOW - timedelta(days=1000)).isoformat(),
            "description": "",
        }
        self.assertEqual(score_job(metadata, now=FIXED_NOW), 0.0)

    def test_missing_fields_default_to_zero_contribution(self):
        self.assertEqual(score_job({}, now=FIXED_NOW), 0.0)

    def test_score_never_exceeds_100(self):
        metadata = {
            "stars": 10_000_000,
            "pushed_at": FIXED_NOW.isoformat(),
            "description": "x" * 500,
        }
        self.assertLessEqual(score_job(metadata, now=FIXED_NOW), 100.0)

    def test_recency_decays_with_age(self):
        fresh = score_job(
            {"stars": 100, "pushed_at": (FIXED_NOW - timedelta(days=1)).isoformat()},
            now=FIXED_NOW,
        )
        stale = score_job(
            {"stars": 100, "pushed_at": (FIXED_NOW - timedelta(days=200)).isoformat()},
            now=FIXED_NOW,
        )
        self.assertGreater(fresh, stale)

    def test_malformed_pushed_at_treated_as_no_signal(self):
        score = score_job(
            {"stars": 0, "pushed_at": "not-a-date", "description": ""}, now=FIXED_NOW
        )
        self.assertEqual(score, 0.0)


class AdvanceDiscoveredJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp_dir.name) / "jobs.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_high_scoring_job_becomes_selected(self):
        self.store.create_job(
            "owner/good",
            "a" * 40,
            metadata={
                "stars": 50000,
                "pushed_at": datetime.now(timezone.utc).isoformat(),
                "description": "A genuinely useful tool with a real description.",
            },
        )
        results = advance_discovered_jobs(self.store)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].state, JobState.SELECTED)
        self.assertIsNotNone(results[0].score)

    def test_low_scoring_job_becomes_failed(self):
        self.store.create_job(
            "owner/bad",
            "b" * 40,
            metadata={"stars": 0, "pushed_at": "", "description": ""},
        )
        results = advance_discovered_jobs(self.store)
        self.assertEqual(results[0].state, JobState.FAILED)

    def test_job_with_no_metadata_scores_zero_and_fails(self):
        self.store.create_job("owner/no-metadata", "c" * 40)
        results = advance_discovered_jobs(self.store)
        self.assertEqual(results[0].state, JobState.FAILED)
        self.assertEqual(results[0].score, 0.0)

    def test_custom_threshold_changes_outcome(self):
        self.store.create_job(
            "owner/borderline",
            "d" * 40,
            metadata={
                "stars": 10,
                "pushed_at": datetime.now(timezone.utc).isoformat(),
                "description": "",
            },
        )
        lenient = advance_discovered_jobs(self.store, threshold=0.0)
        self.assertEqual(lenient[0].state, JobState.SELECTED)

    def test_only_discovered_jobs_are_advanced(self):
        already_selected = self.store.create_job("owner/already", "e" * 40)
        for state in (JobState.ANALYZED, JobState.SCORED, JobState.SELECTED):
            self.store.set_state(already_selected.id, state)
        results = advance_discovered_jobs(self.store)
        self.assertEqual(results, [])
        untouched = self.store.get_job(already_selected.id)
        self.assertEqual(untouched.state, JobState.SELECTED)

    def test_default_threshold_constant_is_reasonable(self):
        self.assertTrue(0 < DEFAULT_SELECTION_THRESHOLD < 100)


if __name__ == "__main__":
    unittest.main()
