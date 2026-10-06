"""Update AI Hive itself from GitHub `main`: the decisions, with no Qt.

Qt-free and stdlib-only, the same class of module as `cli_update.py`: every
function takes an injected `Runner` (argv, timeout -> (rc, output), never
raises), and the real one (`subprocess_runner`) is armed only from `main.py`
through `MainWindow.arm_self_update`. The offscreen suite shares
`create_main_window` and must never fetch, merge or pip install.

WHY GIT AND NOT A DOWNLOAD
--------------------------
AI Hive runs from a git clone (`AI Hive.bat` launches the clone's `.venv`), so
the clone already knows where it came from and how to move forward. A check is
`git fetch origin main` plus two `git show origin/main:<file>` reads; an
update is `git merge --ff-only origin/main`. No GitHub API, no token, no rate
limit, and nothing written anywhere git would not write it.

THE VERSION IS THE SIGNAL, NOT THE COMMIT
-----------------------------------------
Every PR merged to main bumps `app/__init__.py:__version__` (CLAUDE.md), so
"there is an update" means the remote version is HIGHER than the local one.
A remote with newer commits but the same version (a direct docs push) is not
offered, and a local clone AHEAD of main (a developer on this very folder) is
never told to "update" backwards.

THE CHANGELOG TRAVELS WITH THE CODE
-----------------------------------
`CHANGELOG.md` has one `## x.y.z` section per version, newest first, and is
read from `origin/main` by the same fetch, so the notes shown are exactly the
notes for what would be merged. Every section in (installed, remote] is shown,
so a user three versions behind reads all three. A smoke check keeps the top
section equal to `__version__`. If a section is missing (an older remote, or a
forgotten entry) the commit subjects stand in, so the dialog is never blank.

NOTHING HERE EVER OVERWRITES THE USER'S WORK
--------------------------------------------
This folder is also where the app is developed, often by several agents at
once, so an update is only attempted on a clean `main` that is an ancestor of
`origin/main`. Anything else (another branch, modified tracked files, local
commits not on GitHub) is reported as `blocked` with the reason, and no git
command that writes is run. There is no `reset`, no `stash`: fast-forward or
nothing. Untracked files are left alone; if the update would overwrite one,
git itself refuses the merge and that refusal is reported.

EVERY LAUNCH PULLS WHAT MERGED
------------------------------
Agents work in lanes and the integrator merges from its own lane, so nothing
else moves this folder after a PR merges. `main.py` calls `startup_update`
before it imports the app, so the next launch runs what merged. A session
that worked here on a branch leaves the folder on it, which blocked every
update until someone switched by hand (0.28.2 sat there for hours). So the
launch, and only the launch, switches a clean folder back to main when
`git cherry` finds nothing on its branch that main lacks. It never switches
away from unmerged commits or local changes.

THE USER RESTARTS, NEVER US
---------------------------
Python has already imported the running code, so the pulled files take effect
on the next launch. Closing the window is the user's call (their agents are
running), so an applied update only ever says "restart to finish". The
restart-pending state is DERIVED, not stored: the version in the file on disk
is higher than the version this process imported. That also covers a user who
ran `git pull` by hand.

No em dash in any string below: `detail` and `blocked` are read by the user.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum

from .cli_update import RC_TIMEOUT, needs_update, version_tuple

# The folder that holds `main.py`, `app/` and `.git`.
REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REMOTE = "origin"
BRANCH = "main"
UPSTREAM = f"{REMOTE}/{BRANCH}"

FETCH_TIMEOUT_S = 30.0
# the launch waits on this with no window up yet, so it gives up sooner
STARTUP_FETCH_TIMEOUT_S = 10.0
GIT_TIMEOUT_S = 15.0
MERGE_TIMEOUT_S = 60.0
PIP_TIMEOUT_S = 600.0

# fallback notes are commit subjects; a clone far behind must not fill the
# dialog with hundreds of lines
MAX_FALLBACK_COMMITS = 40

_VERSION_ASSIGN_RE = re.compile(
    r"""^__version__\s*=\s*["']([^"']+)["']""", re.M)
_SECTION_RE = re.compile(r"^##\s+v?(\d+\.\d+\.\d+)\b.*$", re.M)


class Status(str, Enum):
    UPDATE_AVAILABLE = "update_available"
    UP_TO_DATE = "up_to_date"
    RESTART_PENDING = "restart_pending"
    ERROR = "error"


@dataclass
class Check:
    status: Status
    running: str = ""       # the version this process imported
    installed: str = ""     # the version in app/__init__.py on disk
    remote: str = ""        # the version on origin/main
    notes: str = ""         # markdown, the changes the user would get
    blocked: str = ""       # why an available update cannot be applied here
    detail: str = ""        # the error, for Status.ERROR


@dataclass
class Applied:
    ok: bool
    version: str = ""
    detail: str = ""
    deps_changed: bool = False
    deps_ok: bool = True
    log: list = field(default_factory=list)


# ------------------------------------------------------------- versions ---

def version_from_source(text) -> str:
    """The `__version__ = "x.y.z"` value in `app/__init__.py` source, or ""."""
    if not isinstance(text, str):
        return ""
    found = _VERSION_ASSIGN_RE.search(text)
    return found.group(1).strip() if found else ""


def read_installed_version(repo: str = REPO_DIR) -> str:
    """The version the NEXT launch will run: the file on disk, not memory."""
    try:
        with open(os.path.join(repo, "app", "__init__.py"),
                  encoding="utf-8") as f:
            return version_from_source(f.read())
    except OSError:
        return ""


# ------------------------------------------------------------ changelog ---

def parse_changelog(text) -> list:
    """`CHANGELOG.md` -> [(version, body)] in file order. Anything above the
    first `## x.y.z` heading (the title, a preamble) is dropped."""
    if not isinstance(text, str) or not text:
        return []
    heads = list(_SECTION_RE.finditer(text))
    sections = []
    for i, head in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        sections.append((head.group(1), text[head.end():end].strip()))
    return sections


def notes_between(changelog, low: str, high: str) -> str:
    """Markdown for every section with low < version <= high, newest first.
    "" when the changelog says nothing about that range."""
    lo, hi = version_tuple(low), version_tuple(high)
    if not lo or not hi:
        return ""
    picked = [(v, body) for v, body in parse_changelog(changelog)
              if lo < version_tuple(v) <= hi]
    picked.sort(key=lambda s: version_tuple(s[0]), reverse=True)
    return "\n\n".join(f"### v{v}\n\n{body}".rstrip() for v, body in picked)


def _fallback_notes(log_output: str) -> str:
    lines = [ln.strip() for ln in (log_output or "").splitlines()
             if ln.strip()]
    if not lines:
        return ""
    return ("### Changes\n\n"
            + "\n".join(f"- {ln}" for ln in lines[:MAX_FALLBACK_COMMITS]))


# ------------------------------------------------------------------ git ---

def _git(repo: str, *args) -> list:
    return ["git", "-C", repo, *args]


def _first_line(text) -> str:
    for line in (text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def _git_error(what: str, rc: int, output: str) -> str:
    if rc == RC_TIMEOUT:
        return f"{what} took too long. Check your connection and try again."
    if rc == 127:
        return "Git was not found. Install Git for Windows to update AI Hive."
    reason = _first_line(output)
    return f"{what} failed" + (f": {reason}" if reason else ".")


def blocked_reason(runner, repo: str = REPO_DIR) -> str:
    """Why `git merge --ff-only origin/main` must not run here, or "".

    Read-only. Run at check time so the dialog can say it up front, and again
    right before the merge because agents may have changed the folder since."""
    rc, out = runner(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                     GIT_TIMEOUT_S)
    if rc != 0:
        return _git_error("Reading the current branch", rc, out)
    branch = _first_line(out)
    if branch != BRANCH:
        return (f"This folder is on the branch '{branch}', not '{BRANCH}'. "
                "Switch to main to update from here.")
    rc, out = runner(_git(repo, "status", "--porcelain",
                          "--untracked-files=no"), GIT_TIMEOUT_S)
    if rc != 0:
        return _git_error("Reading local changes", rc, out)
    if out.strip():
        return ("This folder has local changes to AI Hive's files. Commit "
                "or discard them, then update.")
    rc, out = runner(_git(repo, "merge-base", "--is-ancestor", "HEAD",
                          UPSTREAM), GIT_TIMEOUT_S)
    if rc == 1:
        return ("This folder has commits that are not on GitHub's main, so "
                "it cannot simply move forward. Update it with git by hand.")
    if rc != 0:
        return _git_error("Comparing with GitHub", rc, out)
    return ""


def check(runner, repo: str = REPO_DIR, running: str = "") -> Check:
    """Fetch main and decide. Never raises; every failure is `Status.ERROR`
    with a sentence the user can act on."""
    installed = read_installed_version(repo)
    pending = needs_update(running, installed)
    base = Check(Status.UP_TO_DATE, running=running, installed=installed)

    def restart_or(err: Check) -> Check:
        # a pulled but not yet running update is still worth reporting when
        # the network is down, so a failed fetch falls back to it
        return _restart_pending(base, runner, repo) if pending else err

    rc, out = runner(_git(repo, "fetch", "--quiet", REMOTE, BRANCH),
                     FETCH_TIMEOUT_S)
    if rc != 0:
        return restart_or(Check(Status.ERROR, running, installed,
                                detail=_git_error("Checking GitHub", rc, out)))
    rc, out = runner(_git(repo, "show", f"{UPSTREAM}:app/__init__.py"),
                     GIT_TIMEOUT_S)
    remote = version_from_source(out) if rc == 0 else ""
    if not remote:
        return restart_or(Check(
            Status.ERROR, running, installed,
            detail="Could not read the version on GitHub's main."))
    base.remote = remote
    if not needs_update(installed, remote):
        return _restart_pending(base, runner, repo) if pending else base

    base.status = Status.UPDATE_AVAILABLE
    base.notes = _notes(runner, repo, installed, remote, "HEAD", UPSTREAM)
    base.blocked = blocked_reason(runner, repo)
    return base


def _restart_pending(base: Check, runner, repo: str) -> Check:
    base.status = Status.RESTART_PENDING
    # the notes for what the user will get on restart come from the files
    # already on disk, so no network is needed for them
    try:
        with open(os.path.join(repo, "CHANGELOG.md"), encoding="utf-8") as f:
            base.notes = notes_between(f.read(), base.running, base.installed)
    except OSError:
        base.notes = ""
    return base


def _notes(runner, repo, low, high, old_ref, new_ref) -> str:
    rc, out = runner(_git(repo, "show", f"{new_ref}:CHANGELOG.md"),
                     GIT_TIMEOUT_S)
    notes = notes_between(out, low, high) if rc == 0 else ""
    if notes:
        return notes
    rc, out = runner(_git(repo, "log", "--no-merges", "--format=%s",
                          f"-{MAX_FALLBACK_COMMITS}", f"{old_ref}..{new_ref}"),
                     GIT_TIMEOUT_S)
    return _fallback_notes(out) if rc == 0 else ""


# ---------------------------------------------------------------- apply ---

def apply(runner, repo: str = REPO_DIR, python: str = "") -> Applied:
    """Fast-forward to origin/main, then install requirements if they changed.

    Assumes `check` fetched moments ago. Re-checks `blocked_reason` first, so
    a folder that changed since the dialog opened is refused, not merged."""
    blocked = blocked_reason(runner, repo)
    if blocked:
        return Applied(False, detail=blocked)
    rc, out = runner(_git(repo, "rev-parse", "HEAD"), GIT_TIMEOUT_S)
    old_head = _first_line(out) if rc == 0 else ""
    if not old_head:
        return Applied(False, detail=_git_error("Reading the current commit",
                                                rc, out))
    rc, out = runner(_git(repo, "merge", "--ff-only", "--quiet", UPSTREAM),
                     MERGE_TIMEOUT_S)
    if rc != 0:
        return Applied(False, detail=_git_error("Updating", rc, out),
                       log=[out])
    result = Applied(True, version=read_installed_version(repo), log=[out])

    rc, _ = runner(_git(repo, "diff", "--quiet", old_head, "HEAD", "--",
                        "requirements.txt"), GIT_TIMEOUT_S)
    result.deps_changed = rc != 0
    if result.deps_changed:
        py = python or venv_python()
        rc, out = runner([py, "-m", "pip", "install", "--disable-pip-version-check",
                          "-r", os.path.join(repo, "requirements.txt")],
                         PIP_TIMEOUT_S)
        result.log.append(out)
        result.deps_ok = rc == 0
    result.detail = applied_text(result)
    return result


# --------------------------------------------------------------- launch ---

def _leave_merged_branch(runner, repo: str) -> None:
    """Switch a clean folder back to main when its branch holds nothing that
    main lacks. A session that worked here on a branch leaves the folder on
    it after its PR merges, and every update after that is blocked. `git
    cherry` compares patches, so commits that reached main under new hashes
    (a cherry-pick, an integration branch) count as merged. It skips merge
    commits, though, and a merge can carry conflict resolutions, so a branch
    with a merge commit main lacks is never left."""
    rc, out = runner(_git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
                     GIT_TIMEOUT_S)
    branch = _first_line(out) if rc == 0 else ""
    if branch in ("", "HEAD", BRANCH):
        return
    rc, out = runner(_git(repo, "status", "--porcelain",
                          "--untracked-files=no"), GIT_TIMEOUT_S)
    if rc != 0 or out.strip():
        return
    rc, out = runner(_git(repo, "rev-list", "--merges", f"{UPSTREAM}..HEAD"),
                     GIT_TIMEOUT_S)
    if rc != 0 or out.strip():
        return
    rc, out = runner(_git(repo, "cherry", UPSTREAM, "HEAD"), GIT_TIMEOUT_S)
    if rc != 0 or any(ln.startswith("+") for ln in out.splitlines()):
        return
    runner(_git(repo, "switch", "--quiet", BRANCH), GIT_TIMEOUT_S)


def startup_update(runner, repo: str = REPO_DIR, python: str = ""):
    """Bring this clone to GitHub's main at launch, before `main.py` imports
    the app, so whatever merged since the last run is what runs now.

    None when the folder already has everything on main. Otherwise the
    `Applied`, which is a refusal (`ok=False`) whenever `apply` would refuse:
    local changes and unmerged commits are never touched."""
    rc, out = runner(_git(repo, "fetch", "--quiet", REMOTE, BRANCH),
                     STARTUP_FETCH_TIMEOUT_S)
    if rc != 0:
        return Applied(False, detail=_git_error("Checking GitHub", rc, out))
    rc, _ = runner(_git(repo, "merge-base", "--is-ancestor", UPSTREAM,
                        "HEAD"), GIT_TIMEOUT_S)
    if rc == 0:
        return None
    _leave_merged_branch(runner, repo)
    return apply(runner, repo, python)


def applied_text(result: Applied) -> str:
    if not result.ok:
        return result.detail
    text = (f"Updated to v{result.version}. Restart AI Hive to use it: close "
            "the window and open it again. Your agents keep running until "
            "you do.") if result.version else (
            "Updated. Restart AI Hive to use it.")
    if result.deps_changed and not result.deps_ok:
        text += ("\n\nThe update needs new Python packages and installing "
                 "them failed. After closing AI Hive, run:\n"
                 ".venv\\Scripts\\python.exe -m pip install -r requirements.txt")
    return text


def venv_python() -> str:
    """The console python next to the running interpreter. The app runs under
    `pythonw.exe`, which pip works with but whose output nobody could read."""
    exe = sys.executable or "python"
    folder, name = os.path.split(exe)
    if name.lower() == "pythonw.exe":
        candidate = os.path.join(folder, "python.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def audit_line(result) -> str:
    """One `SELF-UPDATE` line for session forensics."""
    if isinstance(result, Applied):
        return (f"SELF-UPDATE apply ok={result.ok} version={result.version} "
                f"deps_changed={result.deps_changed} deps_ok={result.deps_ok} "
                f"detail={_first_line(result.detail)!r}")
    if isinstance(result, Check):
        return (f"SELF-UPDATE check status={result.status.value} "
                f"running={result.running} installed={result.installed} "
                f"remote={result.remote} blocked={bool(result.blocked)} "
                f"detail={_first_line(result.detail)!r}")
    return "SELF-UPDATE ?"


# --------------------------------------------------------------- runner ---

def subprocess_runner(argv, timeout: float = GIT_TIMEOUT_S) -> tuple:
    """The one place this module touches the machine; never called from the
    smoke suite. No console window, closed stdin, and git is told never to
    prompt in a terminal nobody can see."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW",
                                          0x08000000)
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        res = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, env=env,
            timeout=(timeout if timeout and timeout > 0 else None), **kwargs)
    except subprocess.TimeoutExpired:
        return RC_TIMEOUT, ""
    except FileNotFoundError:
        return 127, ""
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return res.returncode, (res.stdout or "") + (res.stderr or "")
