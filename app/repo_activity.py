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

The header button is Open repo for a folder with an origin remote and
Connect to repo for one without (`repo_state`, re-read off the GUI thread
when the folder changes, the window comes back to the front or a
connection is made). Connect adds `origin` (`connect_origin`) only after
`git ls-remote` reached the repository with the user's own git sign-in, so
a private repository the user can't read is refused before anything in the
folder changes. A folder with any origin, GitHub or not, never offers
Connect: replacing a remote the user set up is not this button's call.

Qt-free on purpose: `fetch`, `repo_state` and `connect_origin` run on
worker threads."""

from __future__ import annotations

import json
import os
import re
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
    # urlparse raises on some malformed input ("https://[" while the user
    # is still typing into Connect): that is not a GitHub URL either
    try:
        if remote.startswith("git@github.com:"):
            path = remote.partition(":")[2]
            parsed = urlparse(f"https://github.com/{path}")
        else:
            parsed = urlparse(remote)
            if (parsed.scheme in ("ssh", "git")
                    and parsed.hostname == "github.com"):
                parsed = parsed._replace(scheme="https", netloc="github.com")
        if (parsed.scheme not in ("http", "https")
                or parsed.hostname != "github.com"):
            return ""
    except ValueError:
        return ""
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]
    if not path.strip("/"):
        return ""
    return urlunparse(("https", "github.com", path, "", "", ""))


def github_repo_url(remote: str) -> str:
    """`browser_url` for a remote naming exactly an owner and a repository
    (https://github.com/owner/repo), else "": what Connect accepts."""
    url = browser_url(remote)
    parts = [part for part in urlparse(url).path.split("/") if part]
    return url if len(parts) == 2 else ""


def _origin_remote(project_path: str):
    """(git's exit code, origin's URL); (None, "") when git didn't run."""
    try:
        result = subprocess.run(
            ["git", "-C", project_path, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=3,
            creationflags=_CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None, ""
    return result.returncode, (result.stdout.strip()
                               if result.returncode == 0 else "")


def origin_url(project_path: str) -> str:
    """The folder's origin remote as a github.com browser URL, or ""."""
    return browser_url(_origin_remote(project_path)[1])


# repo_state answers
STATE_GITHUB = "github"     # origin is a github.com repository: Open repo
STATE_OTHER = "other"       # origin is somewhere else: Open repo explains
STATE_NONE = "none"         # no git repository, or no origin: Connect
STATE_UNKNOWN = ""          # missing folder, or git didn't answer: no change


def repo_state(project_path: str) -> str:
    """Which header button the folder gets (the STATE_* values). A folder
    outside any repository is answered by a folder walk, no git process."""
    from .lanes import find_repo_root
    if not project_path or not os.path.isdir(project_path):
        return STATE_UNKNOWN
    if not find_repo_root(project_path):
        return STATE_NONE
    rc, remote = _origin_remote(project_path)
    if rc == 0 and remote:
        return STATE_GITHUB if browser_url(remote) else STATE_OTHER
    # 2: "No such remote 'origin'"; anything else (a timeout, a broken
    # repository) is not proof there is no origin
    return STATE_NONE if rc == 2 else STATE_UNKNOWN


_ORIGIN_SECTION = re.compile(r'^\s*\[\s*remote\s+"origin"\s*\]',
                             re.MULTILINE | re.IGNORECASE)


def config_has_origin(project_path: str):
    """`repo_state` from files alone, for the GUI thread while the header
    is built: False outside any repository or when the repository's shared
    config has no [remote "origin"], True when it has one, None when that
    can't be read. No git process (an included config file is not read; the
    probe that follows corrects that rare case). Without it the probe's
    answer changed the label after the page's first layout, and the header
    re-layout resized every agent's terminal (a width change makes Claude
    redraw, see CLAUDE.md)."""
    from .lanes import find_repo_root, read_common_dir
    if not project_path or not os.path.isdir(project_path):
        return None
    if not find_repo_root(project_path):
        return False
    common = read_common_dir(project_path)
    if not common:
        return None
    try:
        with open(os.path.join(common, "config"), encoding="utf-8",
                  errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    return bool(_ORIGIN_SECTION.search(text))


# git gets this long to reach the repository; it never prompts (the runner
# sets GIT_TERMINAL_PROMPT=0 and GCM_INTERACTIVE=never), so a hang is the
# network's
CONNECT_REACH_TIMEOUT_S = 60.0
CONNECT_GIT_TIMEOUT_S = 15.0

NO_ACCESS_TEXT = (
    "AI Hive could not reach this repository. If it is private, your git "
    "account does not have access to it, or git is not signed in to "
    "GitHub on this computer. Check the URL and your access, then try "
    "again.")


def connect_origin(project_path: str, remote: str, runner) -> str:
    """Add `remote` as the folder's origin; return an error for the user,
    or "" on success. In order: the URL must name a github.com owner and
    repository, git must reach it with the user's own sign-in (`ls-remote`),
    a folder outside any repository is made one (`git init`), then
    `remote add origin`. A failure before the last step leaves the folder
    as it was. `runner(argv, timeout) -> (rc, output)`: MainWindow passes
    workspace_sync.sync_runner (it imports Qt, this module doesn't)."""
    from .lanes import find_repo_root
    remote = (remote or "").strip()
    if not github_repo_url(remote):
        return ("Enter a GitHub repository URL with an owner and "
                "repository name.")
    if not project_path or not os.path.isdir(project_path):
        return "Folder not found:\n" + str(project_path)
    rc, out = runner(["git", "ls-remote", "--heads", remote],
                     CONNECT_REACH_TIMEOUT_S)
    if rc != 0:
        detail = (out or "").strip() or "git did not answer."
        return NO_ACCESS_TEXT + "\n\n" + detail
    if not find_repo_root(project_path):
        rc, out = runner(["git", "-C", project_path, "init"],
                         CONNECT_GIT_TIMEOUT_S)
        if rc != 0:
            return ("Could not make this folder a git repository.\n\n"
                    + ((out or "").strip() or "git init failed."))
    rc, out = runner(["git", "-C", project_path, "remote", "add", "origin",
                      remote], CONNECT_GIT_TIMEOUT_S)
    if rc != 0:
        return ("Could not add the repository as this folder's origin.\n\n"
                + ((out or "").strip() or "git remote add failed."))
    return ""


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
