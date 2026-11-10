"""Sync every workspace's folder with GitHub in one click: the decisions,
with no Qt.

The sidebar's sync button (`MainWindow.sync_all_workspaces`) runs `sync_all`
on a worker thread over every workspace's folder. The point is a user who
works from two or more computers: after pushing from one, a click on the
other brings every project up to date, without asking an agent in each
workspace to pull. Options > Sync workspaces with GitHub at launch (default
off) runs the same sync once per launch, only after the launch has settled
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

WORKSPACES SHARING A REPOSITORY
-------------------------------
Two workspaces on one folder (or on two folders inside one checkout) sync
once. Checkouts that share one git directory (worktrees of one repo) run one
after the other, because two fetches into one repo race for the same ref
locks; different repositories run in parallel.

AI HIVE'S OWN FOLDER
--------------------
A workspace on the clone AI Hive runs from syncs like any other, except
that an update which changes `requirements.txt` is left for the version
button, which installs the packages too (`self_update.apply`). The pulled
code runs from the next launch; the version button then offers the
restart.

No em dash in any string below: `detail` is read by the user.
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum

from .cli_update import RC_TIMEOUT

FETCH_TIMEOUT_S = 60.0
GIT_TIMEOUT_S = 15.0
MERGE_TIMEOUT_S = 60.0
# repositories fetched at once; each fetch is one network round trip
MAX_PARALLEL = 4
# a fetch that lost a ref lock to another fetch (the lane service's, an
# agent's) is tried once more after this pause
LOCK_RETRY_S = 1.5


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
        return "Git was not found. Install Git for Windows to sync."
    reason = _first_line(output)
    if reason.lower().startswith(("error: ", "fatal: ")):
        reason = reason.split(": ", 1)[1]
    return f"{what} failed" + (f": {reason}" if reason else ".")


def _same_path(a: str, b: str) -> bool:
    return bool(a and b) and (os.path.normcase(os.path.realpath(a))
                              == os.path.normcase(os.path.realpath(b)))


def locate(runner, folder: str) -> tuple:
    """(top folder, git common dir, why not) for a workspace folder.
    `why not` is a sentence when the folder cannot be synced at all."""
    if not folder or not os.path.isdir(folder):
        return "", "", "The folder is missing."
    rc, out = runner(_git(folder, "rev-parse", "--show-toplevel"),
                     GIT_TIMEOUT_S)
    if rc == 127 or rc == RC_TIMEOUT:
        return "", "", _git_error("Reading the folder", rc, out)
    if rc != 0:
        return "", "", "Not a git repository."
    top = os.path.normpath(_first_line(out))
    rc, out = runner(_git(top, "rev-parse", "--path-format=absolute",
                          "--git-common-dir"), GIT_TIMEOUT_S)
    common = os.path.normpath(_first_line(out)) if rc == 0 else top
    return top, common, ""


def _fetch(runner, top: str, remote: str) -> tuple:
    rc, out = runner(_git(top, "fetch", "--quiet", remote), FETCH_TIMEOUT_S)
    if rc != 0 and rc != RC_TIMEOUT and ".lock" in (out or ""):
        time.sleep(LOCK_RETRY_S)
        rc, out = runner(_git(top, "fetch", "--quiet", remote),
                         FETCH_TIMEOUT_S)
    return rc, out


def sync_folder(runner, top: str, app_repo: str = "",
                updating=None) -> Result:
    """Fetch and fast-forward one checkout. Never raises. `app_repo` is the
    folder AI Hive runs from (see AI HIVE'S OWN FOLDER). `updating(res)` is
    called right before the fast-forward, once it is known there is
    something to pull, so the window can say so while it runs."""
    res = Result(top, Outcome.SKIPPED)

    def done(outcome: Outcome, detail: str) -> Result:
        res.outcome, res.detail = outcome, detail
        return res

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
    rc, out = runner(_git(top, "rev-parse", "--verify", "--quiet",
                          "@{upstream}^{commit}"), GIT_TIMEOUT_S)
    target = _first_line(out) if rc == 0 else ""
    rc, out = runner(_git(top, "rev-parse", "--verify", "--quiet",
                          "HEAD^{commit}"), GIT_TIMEOUT_S)
    head = _first_line(out) if rc == 0 else ""
    if not head:
        return done(Outcome.SKIPPED, f"The branch '{branch}' has no commits "
                                     "yet.")
    if not target:
        return done(Outcome.FAILED, f"Could not read {upstream} after the "
                                    "fetch.")
    res.old_head = res.new_head = head
    # every read below names the fetched commit, not the ref: another fetch
    # landing meanwhile must not widen what this sync merges
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
    if app_repo and _same_path(top, app_repo):
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


def sync_all(runner, workspaces, app_repo: str = "",
             parallel: int = MAX_PARALLEL, progress=None) -> list:
    """Sync every workspace folder. `workspaces` is [(ws id, name, folder)]
    in sidebar order; the results come back in that order, one per checkout
    (workspaces sharing one carry all their names and ids). Never raises.

    `progress(event, result)` runs on a worker thread: "checking" as a
    checkout's sync starts, "updating" right before its fast-forward and
    "done" with its final result. The window turns these into the notice
    over each workspace."""
    names: dict = {}            # result key -> workspace names, in order
    ids: dict = {}              # result key -> workspace ids, in order
    results: dict = {}          # result key -> Result
    groups: dict = {}           # git common dir -> [(key, top folder)]

    def tell(event: str, res: Result, key) -> None:
        if progress is None:
            return
        res.names, res.ids = names[key], ids[key]
        try:
            progress(event, res)
        except Exception:  # noqa: BLE001 - a notice never stops the sync
            pass

    for ws_id, name, folder in workspaces:
        try:
            top, common, why = locate(runner, folder)
        except Exception as e:  # noqa: BLE001 - one folder never stops all
            top, common, why = "", "", f"Could not read the folder: {e}"
        key = (os.path.normcase(os.path.realpath(top)) if top
               else "?" + (folder or name))
        if key not in names:
            names[key], ids[key] = [], []
            if why:
                results[key] = Result(top or folder, Outcome.SKIPPED, why)
            else:
                groups.setdefault(common, []).append((key, top))
        names[key].append(name)
        ids[key].append(ws_id)

    for key, res in list(results.items()):     # the folders that can't sync
        tell("done", res, key)

    def run_group(items) -> list:
        out = []
        for key, top in items:
            tell("checking", Result(top, Outcome.SKIPPED), key)
            try:
                res = sync_folder(runner, top, app_repo,
                                  lambda r, k=key: tell("updating", r, k))
            except Exception as e:  # noqa: BLE001
                res = Result(top, Outcome.FAILED,
                             f"The sync stopped unexpectedly: {e}")
            tell("done", res, key)
            out.append((key, res))
        return out

    if groups:
        workers = max(1, min(parallel, len(groups)))
        with ThreadPoolExecutor(max_workers=workers) as ex:
            for pairs in ex.map(run_group, groups.values()):
                results.update(pairs)
    for key, res in results.items():
        res.names, res.ids = names[key], ids[key]
    return [results[key] for key in names]


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
