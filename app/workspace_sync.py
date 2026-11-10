"""Sync every workspace's folder with GitHub in one click: the decisions,
with no Qt.

The sidebar's sync button (`MainWindow.sync_all_workspaces`) syncs every
workspace's folder off the GUI thread. The point is a user who works from
two or more computers: after pushing from one, a click on the other brings
every project up to date, without asking an agent in each workspace to
pull. Options > Sync workspaces with GitHub at launch (default off) runs the
same sync once per launch, only after the launch has settled
(`MainWindow.start_workspace_sync_at_launch`), so it never slows startup.
`progress` events drive the notice over each workspace
(`workspace_page.SyncNotice`).

FAST-FORWARD OR NOTHING
-----------------------
Each repository is fetched from the remote its current branch tracks, then
moved with `git merge --ff-only <the fetched commit>`. That is all a sync
ever writes. There is no reset, no stash, no rebase, no branch switch and
no commit, so it can never lose work:

* a branch with local commits GitHub lacks is left alone and reported
  (moving it would take a real merge, the user's call);
* a branch with no upstream (a new local branch, every agent lane) or a
  detached HEAD is skipped;
* uncommitted changes are fine as long as GitHub did not change the same
  files: git's fast-forward refuses before it writes anything when a local
  change or an untracked file would be overwritten, and that refusal is
  reported. That is exactly what `git pull --ff-only` in the folder does,
  which is what the user would otherwise ask an agent to run. Agents in a
  workspace without lanes always have some uncommitted work, so refusing
  any dirty folder would make the button useless there.

Agent lanes are never touched: a lane branch has no upstream, and lanes
live in their own worktrees, not in the workspace folder. The fetch does
refresh the remote branches the lanes compare against.

NOBODY IS THERE TO ANSWER
-------------------------
A sync nobody clicked (the launch one) must never wait on a sign-in. Git
runs with its terminal prompt and Git Credential Manager's window both off,
so a private repo whose token expired fails with the reason instead. Each
git call has a timeout, and `sync_runner` kills the whole process tree when
it runs out, then reads output from a file rather than a pipe: a pipe that
a helper (`git-remote-https`, the credential manager) still holds would keep
the wait going past the timeout and leave the sync button busy for good.

WORKSPACES SHARING A REPOSITORY
-------------------------------
Two workspaces on one folder (or on two folders inside one checkout) sync
once. `plan` reads every folder in parallel and groups the checkouts by git
directory; `run_group` syncs one group's checkouts one after the other,
because two fetches into one repo race for the same ref locks. The window
runs each group on that repository's lane queue (`LaneOps.submit` with
`exclusive=False`, beside the lane service's base fetch), and the fast-
forward itself holds the repository's lane lock (`merge_lock`, the one
`LaneOps.lock_for` gives every lane create and remove), since it moves a
branch and the checkout. The fetch stays outside that lock, so a slow
network never makes a lane operation wait. Different repositories run in
parallel. `sync_all` is the same thing on a plain thread pool, for tests and
callers without a window.

AI HIVE'S OWN FOLDER
--------------------
A workspace on the clone AI Hive runs from follows the version button's
rules, not the general ones: it moves only a clean `main` that GitHub's
main is ahead of (`self_update.blocked_reason`), and an update that changes
`requirements.txt` is left for the version button, which installs the
packages too. It is skipped altogether while AI Hive is updating itself
(`app_busy`), since both would fetch and merge in that one folder. The
pulled code runs from the next launch; the version button then offers the
restart.

No em dash in any string below: `detail` is read by the user.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

from . import self_update
from .cli_update import RC_TIMEOUT
from .self_update import _first_line, _git

FETCH_TIMEOUT_S = 60.0
GIT_TIMEOUT_S = 15.0
MERGE_TIMEOUT_S = 60.0
# folders read, and (in `sync_all`) repositories synced, at once
MAX_PARALLEL = 4
# a fetch that lost a ref lock to another fetch (the lane service's, an
# agent's) is tried once more after this pause
LOCK_RETRY_S = 1.5

_CREATE_NO_WINDOW = 0x08000000


class Outcome(str, Enum):
    UPDATED = "updated"
    UP_TO_DATE = "up_to_date"
    SKIPPED = "skipped"     # deliberately left alone, with the reason
    FAILED = "failed"       # git or the network said no


@dataclass
class Result:
    folder: str                     # the checkout's top folder
    outcome: Outcome
    detail: str = ""                # one sentence for the user
    names: list = field(default_factory=list)   # workspaces on this folder
    ids: list = field(default_factory=list)     # ...and their ids
    branch: str = ""
    upstream: str = ""              # what the branch tracks, "origin/main"
    behind: int = 0                 # commits GitHub had that this lacked
    pulled: int = 0                 # commits the fast-forward brought
    old_head: str = ""
    new_head: str = ""


@dataclass
class Plan:
    """What `plan` found: every checkout to sync, grouped by git directory,
    and the folders that cannot sync at all, already decided."""
    order: list = field(default_factory=list)    # result keys, sidebar order
    names: dict = field(default_factory=dict)    # key -> workspace names
    ids: dict = field(default_factory=dict)      # key -> workspace ids
    results: dict = field(default_factory=dict)  # key -> Result
    groups: dict = field(default_factory=dict)   # git dir -> [(key, top)]

    def finish(self, results=()) -> list:
        """Every result in sidebar order, each naming its workspaces.
        `results` are the synced groups' (key, Result) pairs."""
        self.results.update(results)
        for key, res in self.results.items():
            res.names, res.ids = self.names[key], self.ids[key]
        return [self.results[key] for key in self.order
                if key in self.results]


# --------------------------------------------------------------- runner ---

def sync_runner(argv, timeout: float = GIT_TIMEOUT_S) -> tuple:
    """Run git with nobody to answer it (see NOBODY IS THERE TO ANSWER):
    (rc, stdout + stderr), RC_TIMEOUT when it ran out of time, never
    raises. Never called from the smoke suite's own repo."""
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GCM_INTERACTIVE"] = "never"
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    with tempfile.TemporaryFile() as out:
        try:
            proc = subprocess.Popen(list(argv), stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env=env,
                                    **kwargs)
        except FileNotFoundError:
            return 127, ""
        except (OSError, ValueError) as e:
            return 127, str(e)
        try:
            rc = proc.wait(timeout if timeout and timeout > 0 else None)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            return RC_TIMEOUT, ""
        out.seek(0)
        return rc, out.read().decode("utf-8", errors="replace")


def _kill_tree(proc) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, timeout=10,
                           creationflags=_CREATE_NO_WINDOW)
        except (OSError, subprocess.SubprocessError):
            pass
    try:
        proc.kill()
        proc.wait(5)
    except (OSError, subprocess.SubprocessError):
        pass


# ------------------------------------------------------------------ git ---

def _git_error(what: str, rc: int, output: str) -> str:
    if rc == RC_TIMEOUT:
        return f"{what} took too long. Check your connection and try again."
    if rc == 127:
        return "Git was not found. Install Git for Windows to sync."
    reason = _first_line(output)
    if reason.lower().startswith(("error: ", "fatal: ")):
        reason = reason.split(": ", 1)[1]
    return f"{what} failed" + (f": {reason}" if reason else ".")


def _same_path(a: str, b: str) -> bool:
    return bool(a and b) and (os.path.normcase(os.path.realpath(a))
                              == os.path.normcase(os.path.realpath(b)))


def locate(runner, folder: str) -> tuple:
    """(top folder, git common dir, why not) for a workspace folder, in one
    git call. `why not` is a sentence when the folder cannot be synced."""
    if not folder or not os.path.isdir(folder):
        return "", "", "The folder is missing."
    rc, out = runner(_git(folder, "rev-parse", "--path-format=absolute",
                          "--show-toplevel", "--git-common-dir"),
                     GIT_TIMEOUT_S)
    if rc != 0 and "path-format" in (out or ""):
        # git before 2.31: the same question without the option, whose
        # common dir may come back relative to the folder
        rc, out = runner(_git(folder, "rev-parse", "--show-toplevel",
                              "--git-common-dir"), GIT_TIMEOUT_S)
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        if rc == 0 and len(lines) > 1:
            out = "\n".join([lines[0], os.path.join(folder, lines[1])])
    if rc == 127 or rc == RC_TIMEOUT:
        return "", "", _git_error("Reading the folder", rc, out)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if rc != 0 or not lines:
        return "", "", "Not a git repository."
    top = os.path.normpath(lines[0])
    common = os.path.normpath(lines[1]) if len(lines) > 1 else top
    return top, common, ""


def _fetch(runner, top: str, remote: str) -> tuple:
    rc, out = runner(_git(top, "fetch", "--quiet", remote), FETCH_TIMEOUT_S)
    if rc != 0 and rc != RC_TIMEOUT and ".lock" in (out or ""):
        time.sleep(LOCK_RETRY_S)
        rc, out = runner(_git(top, "fetch", "--quiet", remote),
                         FETCH_TIMEOUT_S)
    return rc, out


def sync_folder(runner, top: str, app_repo: str = "", updating=None,
                app_busy: bool = False, merge_lock=None) -> Result:
    """Fetch and fast-forward one checkout. Never raises. `app_repo` is the
    folder AI Hive runs from and `app_busy` says AI Hive is updating itself
    (see AI HIVE'S OWN FOLDER). `updating(res)` is called right before the
    fast-forward, once it is known there is something to pull, so the
    window can say so while it runs. `merge_lock` is held around the
    fast-forward (see WORKSPACES SHARING A REPOSITORY)."""
    res = Result(top, Outcome.SKIPPED)
    is_app = bool(app_repo) and _same_path(top, app_repo)

    def done(outcome: Outcome, detail: str) -> Result:
        res.outcome, res.detail = outcome, detail
        return res

    if is_app and app_busy:
        return done(Outcome.SKIPPED,
                    "AI Hive is updating itself right now. Sync again once "
                    "it is done.")
    rc, out = runner(_git(top, "symbolic-ref", "--quiet", "--short", "HEAD"),
                     GIT_TIMEOUT_S)
    if rc != 0:
        return done(Outcome.SKIPPED,
                    "Not on a branch (detached HEAD), so there is nothing "
                    "to move forward.")
    res.branch = branch = _first_line(out)
    rc, out = runner(_git(top, "config", "--get", f"branch.{branch}.remote"),
                     GIT_TIMEOUT_S)
    remote = _first_line(out) if rc == 0 else ""
    rc, out = runner(_git(top, "rev-parse", "--abbrev-ref",
                          "--symbolic-full-name", "@{upstream}"),
                     GIT_TIMEOUT_S)
    res.upstream = upstream = _first_line(out) if rc == 0 else ""
    if not remote or not upstream:
        return done(Outcome.SKIPPED,
                    f"The branch '{branch}' does not track a branch on "
                    "GitHub.")
    if remote != ".":
        rc, out = _fetch(runner, top, remote)
        if rc != 0:
            return done(Outcome.FAILED,
                        _git_error(f"Fetching from {remote}", rc, out))
    # one call for both: every read below names these commits, not the
    # refs, so another fetch landing meanwhile cannot widen the merge
    rc, out = runner(_git(top, "rev-parse", "HEAD^{commit}",
                          "@{upstream}^{commit}"), GIT_TIMEOUT_S)
    shas = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if rc != 0 or len(shas) != 2:
        rc, out = runner(_git(top, "rev-parse", "--verify", "--quiet",
                              "HEAD^{commit}"), GIT_TIMEOUT_S)
        if rc != 0 or not _first_line(out):
            return done(Outcome.SKIPPED, f"The branch '{branch}' has no "
                                         "commits yet.")
        return done(Outcome.FAILED, f"Could not read {upstream} after the "
                                    "fetch.")
    head, target = shas
    res.old_head = res.new_head = head
    rc, out = runner(_git(top, "rev-list", "--left-right", "--count",
                          f"{head}...{target}"), GIT_TIMEOUT_S)
    parts = out.split() if rc == 0 else []
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return done(Outcome.FAILED, _git_error("Comparing with GitHub",
                                               rc, out))
    ahead, behind = int(parts[0]), int(parts[1])
    res.behind = behind
    if behind == 0:
        if ahead:
            return done(Outcome.UP_TO_DATE,
                        f"Up to date. {commits_text(ahead)} on '{branch}' "
                        "not pushed yet.")
        return done(Outcome.UP_TO_DATE, "Up to date.")
    if ahead:
        return done(Outcome.SKIPPED,
                    f"'{branch}' has {commits_text(ahead)} GitHub lacks and "
                    f"GitHub has {commits_text(behind)} it lacks. Merge them "
                    "by hand.")
    if is_app:
        why = self_update.blocked_reason(runner, top, target)
        if why:
            return done(Outcome.SKIPPED, why)
        rc, _ = runner(_git(top, "diff", "--quiet", head, target, "--",
                            "requirements.txt"), GIT_TIMEOUT_S)
        if rc != 0:
            return done(Outcome.SKIPPED,
                        "This update of AI Hive needs new Python packages. "
                        "Use the update button beside the version.")
    if updating is not None:
        try:
            updating(res)
        except Exception:  # noqa: BLE001 - a notice never stops the sync
            pass
    with merge_lock if merge_lock is not None else contextlib.nullcontext():
        rc, out = runner(_git(top, "merge", "--ff-only", "--quiet", target),
                         MERGE_TIMEOUT_S)
    if rc != 0:
        text = out or ""
        if "would be overwritten" in text:
            return done(Outcome.SKIPPED,
                        f"GitHub has {commits_text(behind)}, but they change "
                        "files with local changes here. Commit or discard "
                        "those, then sync again.")
        if "index.lock" in text:
            return done(Outcome.FAILED,
                        "Another git command was running in this folder. "
                        "Sync again in a moment.")
        return done(Outcome.FAILED, _git_error("Moving forward", rc, text))
    res.new_head, res.pulled = target, behind
    return done(Outcome.UPDATED, f"Pulled {commits_text(behind)} into "
                                 f"'{branch}'.")


def commits_text(n: int) -> str:
    return f"{n} commit" if n == 1 else f"{n} commits"


# ----------------------------------------------------------- many folders ---

def plan(runner, workspaces, parallel: int = MAX_PARALLEL) -> Plan:
    """Read every workspace folder (in parallel) and group what can sync.
    `workspaces` is [(ws id, name, folder)] in sidebar order. Never
    raises."""
    def look(folder):
        try:
            return locate(runner, folder)
        except Exception as e:  # noqa: BLE001 - one folder never stops all
            return "", "", f"Could not read the folder: {e}"

    items = list(workspaces)
    if items:
        with ThreadPoolExecutor(max_workers=max(1, min(parallel,
                                                       len(items)))) as ex:
            found = list(ex.map(look, [folder for _i, _n, folder in items]))
    else:
        found = []
    p = Plan()
    for (ws_id, name, folder), (top, common, why) in zip(items, found):
        key = (os.path.normcase(os.path.realpath(top)) if top
               else "?" + (folder or name))
        if key not in p.names:
            p.order.append(key)
            p.names[key], p.ids[key] = [], []
            if why:
                p.results[key] = Result(top or folder, Outcome.SKIPPED, why)
            else:
                p.groups.setdefault(common, []).append((key, top))
        p.names[key].append(name)
        p.ids[key].append(ws_id)
    return p


def _tell(progress, p: Plan, event: str, res: Result, key) -> None:
    if progress is None:
        return
    res.names, res.ids = p.names[key], p.ids[key]
    try:
        progress(event, res)
    except Exception:  # noqa: BLE001 - a notice never stops the sync
        pass


def tell_unsyncable(p: Plan, progress) -> None:
    """A "done" event for each folder `plan` already decided."""
    for key, res in list(p.results.items()):
        _tell(progress, p, "done", res, key)


def run_group(runner, p: Plan, items, app_repo: str = "", progress=None,
              app_busy: bool = False, merge_lock=None) -> list:
    """Sync one group's checkouts, one after the other: [(key, Result)].

    `progress(event, result)` runs on this (worker) thread: "checking" as a
    checkout's sync starts, "updating" right before its fast-forward and
    "done" with its final result. The window turns these into the notice
    over each workspace. Never raises."""
    out = []
    for key, top in items:
        _tell(progress, p, "checking", Result(top, Outcome.SKIPPED), key)
        try:
            res = sync_folder(
                runner, top, app_repo,
                lambda r, k=key: _tell(progress, p, "updating", r, k),
                app_busy, merge_lock)
        except Exception as e:  # noqa: BLE001
            res = Result(top, Outcome.FAILED,
                         f"The sync stopped unexpectedly: {e}")
        _tell(progress, p, "done", res, key)
        out.append((key, res))
    return out


def sync_all(runner, workspaces, app_repo: str = "",
             parallel: int = MAX_PARALLEL, progress=None,
             app_busy: bool = False) -> list:
    """`plan`, then every group on a thread pool: the results in sidebar
    order, one per checkout (workspaces sharing one carry all their names
    and ids). Never raises."""
    p = plan(runner, workspaces, parallel)
    tell_unsyncable(p, progress)
    pairs = []
    if p.groups:
        workers = max(1, min(parallel, len(p.groups)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for got in ex.map(lambda items: run_group(
                    runner, p, items, app_repo, progress, app_busy),
                    p.groups.values()):
                pairs += got
    return p.finish(pairs)


def needs_attention(res: Result) -> bool:
    """A result the user should hear about even from a sync nobody clicked:
    GitHub has commits this folder could not take, or git failed. A folder
    that simply has nothing to track (no upstream, not a repo) is quiet."""
    return (res.outcome is Outcome.FAILED
            or (res.outcome is Outcome.SKIPPED and res.behind > 0))


def summary(results) -> str:
    """One line: "2 updated, 3 up to date, 1 skipped"."""
    counts = {o: 0 for o in Outcome}
    for r in results:
        counts[r.outcome] += 1
    words = {Outcome.UPDATED: "updated", Outcome.UP_TO_DATE: "up to date",
             Outcome.SKIPPED: "skipped", Outcome.FAILED: "failed"}
    parts = [f"{counts[o]} {words[o]}" for o in Outcome if counts[o]]
    return ", ".join(parts) or "No workspaces to sync."


def audit_line(res: Result, how: str = "click") -> str:
    """The session audit line for one result. `how` is what started the
    sync: "click" (the sidebar button) or "launch" (the Options switch)."""
    names = ", ".join(res.names) or "?"
    moved = (f" {res.old_head[:10]}..{res.new_head[:10]}"
             if res.outcome is Outcome.UPDATED else "")
    return (f"WS-SYNC {how} {res.outcome.value} [{names}] {res.folder}"
            f"{' on ' + res.branch if res.branch else ''}{moved}: "
            f"{res.detail}")
