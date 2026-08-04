"""Validated lifecycle transitions for repository jobs."""

from enum import StrEnum


class JobState(StrEnum):
    DISCOVERED = "discovered"
    ANALYZED = "analyzed"
    SCORED = "scored"
    SELECTED = "selected"
    CLAIMED = "claimed"
    RUNNING = "running"
    VERIFIED = "verified"
    FAILED = "failed"
    COMPLETED = "completed"

    @property
    def terminal(self) -> bool:
        return self in {self.FAILED, self.COMPLETED}


class InvalidTransition(ValueError):
    """Raised when a job attempts an unsupported state transition."""


_ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.DISCOVERED: frozenset({JobState.ANALYZED, JobState.FAILED}),
    JobState.ANALYZED: frozenset({JobState.SCORED, JobState.FAILED}),
    JobState.SCORED: frozenset({JobState.SELECTED, JobState.FAILED}),
    JobState.SELECTED: frozenset({JobState.CLAIMED, JobState.FAILED}),
    JobState.CLAIMED: frozenset(
        {JobState.SELECTED, JobState.RUNNING, JobState.FAILED}
    ),
    JobState.RUNNING: frozenset(
        {JobState.SELECTED, JobState.VERIFIED, JobState.FAILED}
    ),
    JobState.VERIFIED: frozenset({JobState.COMPLETED, JobState.FAILED}),
    JobState.FAILED: frozenset(),
    JobState.COMPLETED: frozenset(),
}


def transition(current: JobState, target: JobState) -> JobState:
    """Validate and return a job's next state.

    Repeating a state is idempotent. Returning claimed or running work to
    selected is reserved for retry and stale-claim recovery.
    """

    if current == target:
        return current
    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidTransition(f"cannot transition from {current} to {target}")
    return target

