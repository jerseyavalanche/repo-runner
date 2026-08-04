import unittest

from repo_runner.lifecycle import InvalidTransition, JobState, transition


class JobStateTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        state = JobState.DISCOVERED
        for target in (
            JobState.ANALYZED,
            JobState.SCORED,
            JobState.SELECTED,
            JobState.CLAIMED,
            JobState.RUNNING,
            JobState.VERIFIED,
            JobState.COMPLETED,
        ):
            state = transition(state, target)
        self.assertEqual(state, JobState.COMPLETED)
        self.assertTrue(state.terminal)

    def test_repeated_transition_is_idempotent(self) -> None:
        self.assertEqual(
            transition(JobState.ANALYZED, JobState.ANALYZED), JobState.ANALYZED
        )

    def test_skipping_a_stage_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            InvalidTransition, "cannot transition from discovered to running"
        ):
            transition(JobState.DISCOVERED, JobState.RUNNING)

    def test_terminal_states_cannot_restart(self) -> None:
        for state in (JobState.COMPLETED, JobState.FAILED):
            with self.subTest(state=state), self.assertRaises(InvalidTransition):
                transition(state, JobState.DISCOVERED)


if __name__ == "__main__":
    unittest.main()

