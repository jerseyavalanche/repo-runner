"""Heuristic scoring for discovered repository jobs.

Walks jobs discovered -> analyzed -> scored -> selected (or -> failed if
the score doesn't clear the bar), using only the metadata discovery.py
already captured -- no extra network calls.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from .lifecycle import JobState
from .persistence import Job, JobStore

DEFAULT_SELECTION_THRESHOLD = 50.0

STARS_WEIGHT = 40.0
RECENCY_WEIGHT = 40.0
DESCRIPTION_WEIGHT = 20.0

# Diminishing returns: log10(stars + 1), capped once it would exceed the
# weight -- a repo with 10,000+ stars doesn't need 100x the credit of one
# with 100.
_STARS_LOG_CAP = 4.0


def _stars_score(stars: int) -> float:
    if stars <= 0:
        return 0.0
    normalized = min(math.log10(stars + 1), _STARS_LOG_CAP) / _STARS_LOG_CAP
    return normalized * STARS_WEIGHT


def _recency_score(pushed_at: str, *, now: datetime | None = None) -> float:
    if not pushed_at:
        return 0.0
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    reference = now or datetime.now(timezone.utc)
    age_days = (reference - pushed).total_seconds() / 86400
    if age_days < 0:
        return RECENCY_WEIGHT
    if age_days <= 7:
        return RECENCY_WEIGHT
    if age_days <= 30:
        return RECENCY_WEIGHT * 0.75
    if age_days <= 90:
        return RECENCY_WEIGHT * 0.5
    if age_days <= 365:
        return RECENCY_WEIGHT * 0.25
    return 0.0


def _description_score(description: str) -> float:
    length = len(description.strip())
    if length == 0:
        return 0.0
    if length < 20:
        return DESCRIPTION_WEIGHT * 0.5
    return DESCRIPTION_WEIGHT


def score_job(metadata: dict[str, Any], *, now: datetime | None = None) -> float:
    """Return a 0-100 score from a job's discovery metadata."""

    stars = metadata.get("stars") or 0
    pushed_at = metadata.get("pushed_at") or ""
    description = metadata.get("description") or ""
    total = (
        _stars_score(stars)
        + _recency_score(pushed_at, now=now)
        + _description_score(description)
    )
    return round(min(total, 100.0), 2)


def advance_discovered_jobs(
    store: JobStore,
    *,
    threshold: float = DEFAULT_SELECTION_THRESHOLD,
    limit: int = 50,
) -> list[Job]:
    """Walk every currently-discovered job through analyzed -> scored, then
    either selected (score clears the threshold) or failed. Returns the
    jobs in their final state after this pass."""

    results: list[Job] = []
    for job in store.list_jobs(state=JobState.DISCOVERED, limit=limit):
        store.set_state(job.id, JobState.ANALYZED)
        score = score_job(job.metadata or {})
        scored = store.record_score(job.id, score)
        if score >= threshold:
            final = store.set_state(scored.id, JobState.SELECTED)
        else:
            final = store.set_state(scored.id, JobState.FAILED)
        results.append(final)
    return results
