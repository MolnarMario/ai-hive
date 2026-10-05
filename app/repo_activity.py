"""Recent GitHub pull requests and commits for the workspace header's
Open repo dropdown.

The dropdown used to fetch only when clicked, so every click opened on a
loading line. MainWindow now keeps the last result per workspace, fills it
once in the background a few seconds after startup (`main.py` opts in, the
suite never does), and refreshes on a click only when the copy is older
than `STALE_AFTER_S`. A click always shows whatever is cached first.

Requests are anonymous, and GitHub allows 60 of those an hour per IP. Each
fetch is two of them; the old fetch-per-click spent that on every open.
Conditional requests don't help here: GitHub answers an anonymous
If-None-Match with a 304 but still counts it (measured). A failed refresh
keeps the last good list (see MainWindow).

Qt-free and network-only on purpose: `fetch` runs on a worker thread."""

from __future__ import annotations

import json
import subprocess
import urllib.request
from urllib.parse import urlparse, urlunparse

# a click refreshes a cached list older than this; younger ones just show
STALE_AFTER_S = 180.0
# after startup, the prefetch waits this long so the restored agents get
# the machine first
PREFETCH_DELAY_MS = 8000

_CREATE_NO_WINDOW = 0x08000000


def browser_url(remote: str) -> str:
    """Convert a GitHub origin URL (HTTPS or SSH) to its browser URL, or ""
    for anything that isn't github.com."""
    remote = (remote or "").strip()
    if not remote:
        return ""
    # Git's SCP-like SSH syntax is not accepted by urlparse as a URL.
    if remote.startswith("git@github.com:"):
        path = remote.partition(":")[2]
        parsed = urlparse(f"https://github.com/{path}")
    else:
        parsed = urlparse(remote)
        if parsed.scheme in ("ssh", "git") and parsed.hostname == "github.com":
            parsed = parsed._replace(scheme="https", netloc="github.com")
    if parsed.scheme not in ("http", "https") or parsed.hostname != "github.com":
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path.strip("/"):
        return ""
    return urlunparse(("https", "github.com", path, "", "", ""))


def origin_url(project_path: str) -> str:
    """The folder's origin remote as a github.com browser URL, or ""."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
            creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return browser_url(result.stdout.strip() if result.returncode == 0 else "")


def _get_json(endpoint: str):
    request = urllib.request.Request(
        endpoint, headers={"Accept": "application/vnd.github+json",
                           "User-Agent": "AI-Hive"})
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch(project_path: str) -> dict:
    """Recent pull requests and commits for the folder's GitHub origin.

    Returns {"pull_requests", "commits", "error", "repo_url"}. `error` is
    "" on success; `repo_url` is "" whenever `error` is set."""
    out = {"pull_requests": [], "commits": [], "error": "", "repo_url": ""}
    try:
        repo_url = origin_url(project_path)
        if not repo_url:
            out["error"] = "No GitHub origin remote found"
            return out
        parts = [part for part in urlparse(repo_url).path.split("/") if part]
        if len(parts) != 2:
            out["error"] = "Could not identify this GitHub repository"
            return out
        api_base = f"https://api.github.com/repos/{parts[0]}/{parts[1]}"
        prs = _get_json(api_base + "/pulls?state=all&sort=updated"
                                   "&direction=desc&per_page=10")
        recent_commits = _get_json(api_base + "/commits?per_page=10")
        out["pull_requests"] = [{
            "number": item["number"],
            "title": item["title"],
            "state": item["state"],
            "merged": bool(item.get("merged_at")),
            "url": item["html_url"],
        } for item in prs]
        out["commits"] = [{
            "sha": item["sha"],
            "message": item["commit"]["message"],
            "url": item["html_url"],
        } for item in recent_commits]
        out["repo_url"] = repo_url
    except (OSError, subprocess.SubprocessError, ValueError,
            KeyError, TypeError) as exc:
        out.update(pull_requests=[], commits=[], repo_url="",
                   error=f"Could not load GitHub activity: {exc}")
    return out
