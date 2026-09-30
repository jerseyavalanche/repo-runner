"""Read-only public software discovery suited to a small Termux phone."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SEARCHES = {
    "android-apps": "android open source offline app in:name,description,readme",
    "mesh-comms": "android mesh messaging reticulum lora in:name,description,readme",
    "offline-maps": "android offline maps navigation in:name,description,readme",
    "voice-calls": "android webrtc sip voice call open source in:name,description,readme",
    "food-resilience": "android food sharing community garden offline in:name,description,readme",
    "barcode-inventory": "android barcode inventory stock scanner open source in:name,description,readme",
    "zebra-datawedge": "zebra datawedge android sample in:name,description,readme",
    "retail-tools": "android retail shelf price inventory open source in:name,description,readme",
}


class ScoutError(RuntimeError):
    pass


def search(query: str, limit: int = 10) -> list[dict]:
    parameters = urllib.parse.urlencode({"q": query, "per_page": limit, "sort": "updated"})
    request = urllib.request.Request(
        f"https://api.github.com/search/repositories?{parameters}",
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "repo-runner-phone-scout/1.0",
            **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"}
               if os.environ.get("GITHUB_TOKEN") else {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)["items"]
    except (urllib.error.URLError, KeyError, ValueError) as error:
        raise ScoutError(f"GitHub search failed: {error}") from error


def rank(item: dict, category: str) -> int:
    """Small relevance score; stars alone must not drown out niche projects."""
    text = f"{item.get('name', '')} {item.get('description') or ''}".lower()
    terms = {
        "android-apps": ("android", "offline", "app"),
        "mesh-comms": ("mesh", "reticulum", "lora", "messag"),
        "offline-maps": ("offline", "map", "navigation"),
        "voice-calls": ("webrtc", "sip", "call", "voice"),
        "food-resilience": ("food", "garden", "sharing", "community"),
        "barcode-inventory": ("barcode", "inventory", "stock", "scan"),
        "zebra-datawedge": ("zebra", "datawedge", "scanner"),
        "retail-tools": ("retail", "shelf", "price", "inventory"),
    }[category]
    relevance = sum(15 for term in terms if term in text)
    stars = min(int(item.get("stargazers_count") or 0), 100) // 10
    return min(relevance + stars, 100)


def collect(categories: list[str], *, limit: int = 10, delay: float = 7.0) -> tuple[list[dict], list[str]]:
    results: dict[str, dict] = {}
    errors: list[str] = []
    for index, category in enumerate(categories):
        if index:
            time.sleep(delay)  # GitHub's unauthenticated search limit is restrictive.
        try:
            items = search(SEARCHES[category], limit)
        except ScoutError as error:
            errors.append(f"{category}: {error}")
            continue
        for item in items:
            if item.get("fork") or item.get("archived") or not item.get("full_name"):
                continue
            name = item["full_name"]
            entry = results.setdefault(name, {
                "name": name,
                "url": item.get("html_url", ""),
                "description": item.get("description") or "",
                "stars": item.get("stargazers_count", 0),
                "updated": item.get("pushed_at", ""),
                "language": item.get("language") or "unknown",
                "categories": [],
                "score": 0,
            })
            if category not in entry["categories"]:
                entry["categories"].append(category)
            entry["score"] = max(entry["score"], rank(item, category))
    return sorted(results.values(), key=lambda row: (-row["score"], row["name"])), errors


def markdown(rows: list[dict], errors: list[str], timestamp: str) -> str:
    lines = ["# Phone software scout", "", f"Scanned: {timestamp}", "",
             "Public GitHub source candidates. Scores reflect keyword matches and a small star bonus;",
             "inspect each project before using it. No APKs are installed or repositories executed.", "",
             "ShopRite's internal apps are not assumed to be public. The retail searches cover",
             "independent tools and public Zebra DataWedge examples only.", ""]
    for category in SEARCHES:
        matches = [row for row in rows if category in row["categories"]]
        lines += [f"## {category}", ""]
        if not matches:
            lines += ["No results in this scan.", ""]
        for row in matches:
            description = row["description"].replace("\n", " ").strip() or "No description"
            lines += [f"- [{row['name']}]({row['url']}) — {description} "
                      f"(score {row['score']}, ★ {row['stars']}, {row['language']}; "
                      f"pushed {row['updated'] or 'unknown'})"]
        lines.append("")
    if errors:
        lines += ["## Search errors", "", *[f"- {error}" for error in errors], ""]
    return "\n".join(lines)


def save_report(directory: Path, rows: list[dict], errors: list[str]) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    json_path, md_path = directory / "latest.json", directory / "latest.md"
    json_path.write_text(json.dumps({"scanned_at": timestamp, "results": rows,
                                     "errors": errors}, indent=2) + "\n")
    md_path.write_text(markdown(rows, errors, timestamp))
    return json_path, md_path
