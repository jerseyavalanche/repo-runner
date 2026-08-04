"""Discover candidate GitHub repositories for repo-runner to evaluate.

Uses the `gh` CLI (already authenticated on this machine) rather than a
raw HTTP client + token management, matching this project's stdlib-only
dependency policy.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .persistence import JobStore

SEARCH_TIMEOUT_SECONDS = 30
RESOLVE_TIMEOUT_SECONDS = 15


class DiscoveryError(RuntimeError):
    """Raised when GitHub search/lookup itself fails (network, auth, gh missing)."""


@dataclass(frozen=True, slots=True)
class Candidate:
    full_name: str
    description: str
    stars: int
    language: str | None
    pushed_at: str
    url: str


def search_github(
    query: str, *, limit: int = 10, sort: str = "stars"
) -> list[dict[str, Any]]:
    """Search GitHub for candidate repositories. Returns the raw `items`
    list from GitHub's search API (each a dict of repo fields)."""

    if not query:
        raise ValueError("query is required")
    if limit <= 0:
        raise ValueError("limit must be positive")
    params = urllib.parse.urlencode(
        {"q": query, "sort": sort, "order": "desc", "per_page": limit}
    )
    try:
        result = subprocess.run(
            ["gh", "api", f"search/repositories?{params}"],
            capture_output=True,
            text=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DiscoveryError(f"gh search failed: {error}") from error
    if result.returncode != 0:
        raise DiscoveryError(f"gh search failed: {result.stderr.strip()}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DiscoveryError(f"gh search returned invalid JSON: {error}") from error
    return payload.get("items", [])


def resolve_head_commit(full_name: str) -> str:
    """Fetch the current HEAD commit SHA for a repo's default branch."""

    if not full_name:
        raise ValueError("full_name is required")
    try:
        result = subprocess.run(
            ["gh", "api", f"repos/{full_name}/commits/HEAD", "--jq", ".sha"],
            capture_output=True,
            text=True,
            timeout=RESOLVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DiscoveryError(f"could not resolve HEAD for {full_name}: {error}") from error
    if result.returncode != 0:
        raise DiscoveryError(
            f"could not resolve HEAD for {full_name}: {result.stderr.strip()}"
        )
    sha = result.stdout.strip()
    if not sha:
        raise DiscoveryError(f"empty commit sha for {full_name}")
    return sha


def item_to_candidate(item: dict[str, Any]) -> Candidate:
    return Candidate(
        full_name=item["full_name"],
        description=item.get("description") or "",
        stars=item.get("stargazers_count", 0),
        language=item.get("language"),
        pushed_at=item.get("pushed_at", ""),
        url=item.get("html_url", ""),
    )


def discover_and_ingest(
    store: JobStore,
    query: str,
    *,
    limit: int = 10,
    source_label: str | None = None,
) -> list[int]:
    """Search GitHub, resolve each candidate's HEAD commit, and insert each
    as a new discovered job. Repos already discovered at the exact same
    commit are skipped silently (the schema's UNIQUE(full_name,
    commit_sha) constraint enforces this) -- expected on repeat runs, not
    an error. A repo whose HEAD commit can't be resolved is skipped rather
    than aborting the whole batch."""

    items = search_github(query, limit=limit)
    source = source_label or f"github-search:{query}"
    inserted: list[int] = []
    for item in items:
        candidate = item_to_candidate(item)
        try:
            commit_sha = resolve_head_commit(candidate.full_name)
        except DiscoveryError:
            continue
        metadata = {
            "description": candidate.description,
            "stars": candidate.stars,
            "language": candidate.language,
            "pushed_at": candidate.pushed_at,
            "url": candidate.url,
        }
        try:
            job = store.create_job(
                candidate.full_name, commit_sha, metadata=metadata, source=source
            )
        except sqlite3.IntegrityError:
            continue
        inserted.append(job.id)
    return inserted
