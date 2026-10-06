"""Agent lanes: a private git worktree ("lane") per agent.

Design and rationale: `.scratch/agent-lanes/spec-v2.md`. Every agent in a
workspace used to share one checkout, one index and one branch, so agents
reset, restaged and committed each other's work. A laned agent works in its
own worktree on its own branch instead:

    folder  <parent>\\<repo>.lanes\\<slug>-<uid6>\\
    branch  hive/<slug>-<uid6>

`<uid6>` is the first 6 hex chars of `AgentSpec.uid`, so two agents with one
name still get different folders and branches. Both are recorded in
`AgentSpec.lane` when the lane is created and NEVER recomputed from the
agent's name: a rename (only ever by the user) moves nothing.

Qt-free and stdlib only. Every git call goes through `RUNNER` (tests inject a
recording one) with CREATE_NO_WINDOW and a timeout. Every MUTATING function
here (`create_lane`, `repair_lane`, `remove_lane`, `retire_lane`) must run
through `app.lane_ops.LaneOps`, which serializes them per repository: git
takes locks in the shared git dir and parallel calls fail on them.

Step 0 findings, measured here on 2026-10-02 (git 2.51.2, Claude Code
2.1.287), not assumed:

* `git worktree remove` (no --force) FOLLOWS a directory junction inside the
  worktree and deletes everything in its target. A lane with a `.venv`
  junction emptied the real `.venv` of the main checkout. So `remove_lane`
  unlinks its own junctions first (`os.rmdir` removes only the link) and then
  refuses while ANY directory link is left anywhere in the lane.
* `git worktree add` took about 70 ms on a small repo.
* `claude --resume <id>` finds a conversation from ANY folder and keeps
  appending to its original transcript file, with the new cwd on the new
  records. So a lane's path stays fixed for AI Hive's own sake rather than
  the CLI's: every transcript reader (titles, context usage, the model chip,
  session sync, the resume picker) looks the conversation up by the agent's
  cwd, and the conversation itself says which checkout the agent works in.
  An agent moved back to the main checkout would edit it while believing it
  is in its lane.

Shared state never lives in a lane: the board and every later lane file stay
in the workspace's own `.aihive\\` folder, addressed by absolute path (see
"Where shared state lives" in the spec). `.aihive/` is also added to the
shared `info/exclude`, so a stray copy inside a lane can never be committed.

Junctions: `.venv`, `venv` and `node_modules` are linked into a new lane when
they exist at the repo root AND git ignores them, so the project's usual test
command works unchanged in a lane. They point at the main checkout's copy: an
install run inside a lane writes there. Other gitignored local files (a
`.env`) are COPIED into a new lane when the repo's `.worktreeinclude` names
them (gitignore syntax, the same file and rule Claude Code's own worktrees
use). The lane records which ones it got, in its own git folder, so closing
a card keeps a lane whose copy was edited OR deleted (`local_changes`), and
keeps it too when that check can't finish.

Awareness (Phase 2, bottom of this file): `snapshot_repo` reads every lane
of a repo (head, ahead/behind, committed and uncommitted files, what the
base changed since the fork) and finds overlaps: files two lanes both
changed, and files a lane changed that the base changed too. When both
sides committed, a real in-memory merge (`git merge-tree --write-tree`, git
2.38+) says whether they CONFLICT. All of it is read-only and lock-free, so
app/lane_service.py polls it off the LaneOps queue. The same read finds the
lane's newest commit whose message has a line saying just "Task done"
(`DONE_GREP`): the agent's flag that its slice of work is finished, which
MainWindow passes on to the workspace's integrator.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

LANES_SUFFIX = ".lanes"
BRANCH_PREFIX = "hive/"
JUNCTION_NAMES = (".venv", "venv", "node_modules")
EXCLUDE_LINE = ".aihive/"
SLUG_MAX = 24

READ_TIMEOUT = 15.0
# a worktree add checks out the whole tree, which on a big repo is not quick
WRITE_TIMEOUT = 300.0
# how long a retire waits for the closed agent's processes to exit: Windows
# refuses to delete a folder that is some process's working directory
EXIT_WAIT_S = 10.0

CREATE_NO_WINDOW = 0x08000000

# a lane agent's "my task is finished" flag: a commit message line holding
# only "Task done" (any case, an optional full stop). `git log --grep`
# matches it per line, so a subject that mentions it in passing doesn't count.
DONE_MARK = "Task done"
DONE_GREP = r"^[[:space:]]*task done[.!]?[[:space:]]*$"


class LaneError(Exception):
    """A lane operation that did not happen. `code` says why, for the audit
    line: exists, base, git, dirty, unmerged, links, missing, location,
    moved."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class GitResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _subprocess_git(args: list, cwd: str, timeout: float) -> GitResult:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"     # never wait on a credential prompt
    # Git Credential Manager ignores GIT_TERMINAL_PROMPT and can open a GUI
    # sign-in window from a background fetch
    env["GCM_INTERACTIVE"] = "never"
    try:
        proc = subprocess.run(
            ["git", *args], cwd=cwd or None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(-1, "", f"{type(exc).__name__}: {exc}")
    return GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


# callable(args, cwd, timeout) -> GitResult. Replaced by tests.
RUNNER = _subprocess_git


def git(args: list, cwd: str, timeout: float = READ_TIMEOUT) -> GitResult:
    return RUNNER(list(args), cwd, timeout)


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path)) if path else ""


def same_path(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _norm(a) == _norm(b)


# ------------------------------------------------------------ discovery ---

def find_repo_root(path: str) -> str:
    """The repository folder that contains `path`, or "" when there is none.

    Found by walking up to the first folder holding a `.git` entry (a folder
    in a main checkout, a file in a worktree or submodule), WITHOUT running
    git. The New Agent dialog and the workspace header ask this to decide
    whether to offer a lane, and with no lane anywhere AI Hive must start no
    git process at all."""
    try:
        cur = os.path.abspath(path)
    except (TypeError, ValueError):
        return ""
    if not os.path.isdir(cur):
        return ""
    while True:
        if os.path.exists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return ""
        cur = parent


repo_root = find_repo_root


def common_dir(path: str) -> str:
    """The shared git dir (`.git` of the main checkout) for any checkout of
    the repo, normalized for use as a key. "" when git can't say."""
    r = git(["rev-parse", "--path-format=absolute", "--git-common-dir"], path)
    return _norm(r.out) if r.ok and r.out else ""


def read_common_dir(path: str) -> str:
    """`common_dir` without running git, for callers on the GUI thread. A
    `.git` folder is the common dir. A `.git` file (a worktree) names its
    gitdir, whose `commondir` file, when present, points at the shared one,
    relative to the gitdir. "" when `path` is in no repo or a file is
    unreadable."""
    root = find_repo_root(path)
    if not root:
        return ""
    dot = os.path.join(root, ".git")
    if os.path.isdir(dot):
        return _norm(dot)
    try:
        with open(dot, encoding="utf-8", errors="replace") as fh:
            line = fh.readline().strip()
        if not line.startswith("gitdir:"):
            return ""
        gitdir = os.path.normpath(os.path.join(root, line[len("gitdir:"):]
                                               .strip()))
        common = os.path.join(gitdir, "commondir")
        if os.path.isfile(common):
            with open(common, encoding="utf-8", errors="replace") as fh:
                rel = fh.readline().strip()
            return _norm(os.path.join(gitdir, rel)) if rel else _norm(gitdir)
        return _norm(gitdir)
    except OSError:
        return ""


def ref_exists(repo: str, ref: str) -> bool:
    return git(["show-ref", "--verify", "--quiet", ref], repo).ok


def default_base(repo: str) -> str:
    """The branch lanes start from and merge back into: what origin/HEAD
    names, else `main` or `master`, else the main checkout's current branch.
    "" when the repo has no branch at all."""
    r = git(["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            repo)
    if r.ok and r.out.startswith("origin/"):
        return r.out[len("origin/"):]
    for name in ("main", "master"):
        if (ref_exists(repo, f"refs/remotes/origin/{name}")
                or ref_exists(repo, f"refs/heads/{name}")):
            return name
    r = git(["symbolic-ref", "--quiet", "--short", "HEAD"], repo)
    return r.out if r.ok else ""


def start_ref(repo: str, base: str) -> str:
    """Where a new lane branches from: `origin/<base>` when the remote has
    it (the shared truth, not a possibly stale local branch), else the local
    `<base>`. "" when neither exists."""
    if not base:
        return ""
    if ref_exists(repo, f"refs/remotes/origin/{base}"):
        return f"origin/{base}"
    if ref_exists(repo, f"refs/heads/{base}"):
        return base
    return ""


# ------------------------------------------------------------- planning ---

def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")
    return s[:SLUG_MAX].strip("-") or "agent"


def lanes_dir_for(repo: str) -> str:
    """`<parent>\\<repo>.lanes`: beside the repo, never inside it, so a
    lane is not part of the main checkout's tree (tools that scan the
    project would otherwise index every lane too)."""
    repo = os.path.normpath(repo)
    return os.path.join(os.path.dirname(repo),
                        os.path.basename(repo) + LANES_SUFFIX)


@dataclass
class LanePlan:
    root: str
    branch: str
    repo: str
    cwd: str            # where the agent starts: root, or its subfolder

    def lane_dict(self) -> dict:
        # "base" is filled in by create_lane, which asks git for it
        return {"root": self.root, "branch": self.branch, "base": "",
                "repo": self.repo}


def plan_lane(repo: str, project_path: str, name: str, uid: str) -> LanePlan:
    """Folder, branch and starting folder for a new lane. Pure: no git, no
    filesystem writes. When the workspace is a subfolder of the repo, the
    agent starts in that same subfolder of its lane."""
    repo = os.path.normpath(repo)
    if not os.path.basename(repo):
        raise LaneError("location", f"no folder beside {repo} for lanes")
    tail = f"{slugify(name)}-{(uid or '')[:6] or '000000'}"
    root = os.path.join(lanes_dir_for(repo), tail)
    return LanePlan(root=root, branch=BRANCH_PREFIX + tail, repo=repo,
                    cwd=_mapped_cwd(root, repo, project_path))


def _mapped_cwd(root: str, repo: str, project_path: str) -> str:
    try:
        rel = os.path.relpath(os.path.normpath(project_path), repo)
    except ValueError:              # another drive
        rel = "."
    if rel in (".", "") or rel.startswith(".."):
        return root
    return os.path.join(root, rel)


def clean_lane(value) -> dict:
    """A persisted lane record, validated. Anything malformed is "no lane"
    rather than an exception on load."""
    if not isinstance(value, dict):
        return {}
    out = {k: value.get(k) for k in ("root", "branch", "base", "repo")}
    if not all(isinstance(out[k], str) and out[k]
               for k in ("root", "branch", "repo")):
        return {}
    if not isinstance(out["base"], str):
        out["base"] = ""
    return out


# ------------------------------------------------------------ junctions ---

def is_junction(path: str) -> bool:
    isj = getattr(os.path, "isjunction", None)
    if isj is not None:
        return isj(path)
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return bool(getattr(st, "st_file_attributes", 0) & 0x400) \
        and not os.path.islink(path)


def _make_junction(src: str, dst: str) -> None:
    try:
        import _winapi
        _winapi.CreateJunction(src, dst)
        return
    except (ImportError, AttributeError):
        pass
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", dst, src],
                          capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW)
    if proc.returncode != 0:
        raise OSError(proc.stderr.strip() or "mklink failed")


def link_junctions(repo: str, root: str) -> list:
    """Link the repo root's ignored `.venv` / `venv` / `node_modules` into
    the lane. Returns the names linked. Best-effort: a missing link costs a
    reinstall in the lane, never data."""
    made = []
    for name in JUNCTION_NAMES:
        src = os.path.join(repo, name)
        dst = os.path.join(root, name)
        if not os.path.isdir(src) or os.path.lexists(dst):
            continue
        # only an IGNORED folder: a tracked one is the lane's own business,
        # and an unignored link would show up in every `git status`
        if not git(["check-ignore", "-q", name], repo).ok:
            continue
        try:
            _make_junction(src, dst)
            made.append(name)
        except OSError:
            pass
    return made


def unlink_junctions(root: str) -> list:
    """Remove the lane's own junctions. `os.rmdir` on a junction removes the
    link and never touches its target. Raises LaneError if one can't go:
    removing the worktree with it in place would delete the target."""
    gone = []
    for name in JUNCTION_NAMES:
        path = os.path.join(root, name)
        if is_junction(path):
            try:
                os.rmdir(path)
            except OSError as exc:
                raise LaneError("links", f"could not unlink {path}: {exc}")
            gone.append(name)
    return gone


def directory_links(root: str, limit: int = 20) -> list:
    """Every junction or directory symlink inside `root`, not followed.
    `git worktree remove` descends into these and deletes their targets."""
    found, stack = [], [root]
    while stack and len(found) < limit:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            try:
                junction = (entry.is_junction()
                            if hasattr(entry, "is_junction")
                            else is_junction(entry.path))
                link = junction or (entry.is_symlink() and entry.is_dir())
                if link:
                    found.append(entry.path)
                elif entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
            except OSError:
                continue
    return found


def ensure_exclude(repo: str) -> bool:
    """Add `.aihive/` to the shared `info/exclude` (it covers every worktree
    of the repo). A safety net for repos that don't gitignore `.aihive/`.
    Returns True when the line was added."""
    cd = common_dir(repo)
    if not cd:
        return False
    path = os.path.join(cd, "info", "exclude")
    try:
        text = ""
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        wanted = {".aihive", ".aihive/", "/.aihive", "/.aihive/"}
        if any(ln.strip() in wanted for ln in text.splitlines()):
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            if text and not text.endswith("\n"):
                fh.write("\n")
            fh.write("# AI Hive shared coordination folder (agent lanes)\n"
                     f"{EXCLUDE_LINE}\n")
        return True
    except OSError:
        return False


# ------------------------------------------------------------ mutations ---

def _git_or_fail(args: list, cwd: str, timeout: float = WRITE_TIMEOUT) -> str:
    r = git(args, cwd, timeout)
    if not r.ok:
        raise LaneError("git", f"git {' '.join(args[:2])}: "
                               f"{r.err or r.out or r.rc}")
    return r.out


def _is_worktree(root: str) -> bool:
    if not os.path.isdir(root) or not os.path.exists(os.path.join(root, ".git")):
        return False
    r = git(["rev-parse", "--show-toplevel"], root)
    return r.ok and same_path(r.out, root)


def create_lane(lane: dict, cwd: str = "") -> dict:
    """Create a NEW lane: `git worktree add --no-track -b <branch> <root>
    <start>`, then the junctions and the exclude line. Returns the lane
    record with its base filled in.

    Collisions fail and never reuse: an existing folder or branch raises
    LaneError("exists") and touches nothing. Attaching a new agent to an
    existing branch could hand it someone else's work.

    The branch gets NO upstream. Leaving out `--track` is not enough: from a
    remote-tracking start point git sets one anyway (`branch.autoSetupMerge`).
    With `origin/<base>` as its upstream, `git status` and a bare `git push`
    in the lane suggest `git push origin HEAD:<base>`, a push straight to the
    base that skips every review. `remove_lane` judges merged-ness itself.

    `cwd` is where the agent will start: the lane root, or the subfolder of
    it that matches a workspace in a subfolder of the repo. When the
    worktree doesn't contain that folder (it is untracked or ignored), the
    fresh lane is removed again and LaneError("location") raised, so the
    agent starts in the workspace folder rather than in a missing one."""
    root, branch, repo = lane["root"], lane["branch"], lane["repo"]
    if os.path.lexists(root):
        raise LaneError("exists", f"the folder {root} already exists")
    if not os.path.isdir(repo):
        raise LaneError("missing", f"the repository {repo} is gone")
    if ref_exists(repo, f"refs/heads/{branch}"):
        raise LaneError("exists", f"the branch {branch} already exists")
    base = default_base(repo)
    start = start_ref(repo, base)
    if not start:
        raise LaneError("base", "the repository has no branch to start from")
    try:
        os.makedirs(os.path.dirname(root), exist_ok=True)
    except OSError as exc:
        raise LaneError("location", f"could not create {os.path.dirname(root)}"
                                    f": {exc}")
    _git_or_fail(["worktree", "add", "--no-track", "-b", branch, root, start],
                 repo)
    if cwd and not os.path.isdir(cwd):
        _discard_fresh_lane(root, branch, repo)
        raise LaneError("location", f"{os.path.relpath(cwd, root)} is not "
                                    f"tracked in the repository, so a lane "
                                    f"would not contain it")
    link_junctions(repo, root)
    copy_worktree_includes(repo, root)
    ensure_exclude(repo)
    return {**clean_lane(lane), "base": base}


def _discard_fresh_lane(root: str, branch: str, repo: str) -> None:
    """Undo a create that turned out unusable. The lane is moments old and
    holds nothing, but the removal rules still apply: links first, no
    `--force`, and the branch only by compare-and-delete."""
    head = git(["rev-parse", "HEAD"], root)
    try:
        unlink_junctions(root)
    except LaneError:
        return
    if directory_links(root):
        return
    if git(["worktree", "remove", root], repo, WRITE_TIMEOUT).ok and head.ok:
        git(["update-ref", "-d", f"refs/heads/{branch}", head.out], repo)
    try:
        os.rmdir(os.path.dirname(root))     # only if it is now empty
    except OSError:
        pass


def _drop_base_upstream(repo: str, branch: str, base: str) -> None:
    """Lanes made before `--no-track` track the base: unset that, so git
    stops suggesting a push to it. Any other upstream is the user's."""
    r = git(["rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}"], repo)
    if r.ok and base and r.out in (base, f"origin/{base}"):
        git(["branch", "--unset-upstream", branch], repo)


def repair_lane(lane: dict) -> tuple:
    """Bring a recorded lane back at its IDENTICAL path: prune stale worktree
    records, then re-add the folder from the recorded branch, or recreate
    that branch from the base when it is gone (it was merged and deleted).
    Returns (lane record, recreated_branch). An intact lane is left alone.

    The one place that attaches to an existing branch on purpose: the branch
    is this agent's own, named in its persisted record."""
    root, branch, repo = lane["root"], lane["branch"], lane["repo"]
    if not os.path.isdir(repo):
        raise LaneError("missing", f"the repository {repo} is gone")
    git(["worktree", "prune"], repo, WRITE_TIMEOUT)
    base = lane.get("base") or default_base(repo)
    if _is_worktree(root):
        _drop_base_upstream(repo, branch, base)
        link_junctions(repo, root)
        ensure_exclude(repo)
        return {**clean_lane(lane), "base": base}, False
    if os.path.lexists(root):
        if os.path.isdir(root) and not os.listdir(root):
            os.rmdir(root)
        else:
            raise LaneError("exists",
                            f"{root} exists but is not this lane's worktree")
    os.makedirs(os.path.dirname(root), exist_ok=True)
    if ref_exists(repo, f"refs/heads/{branch}"):
        _git_or_fail(["worktree", "add", root, branch], repo)
        _drop_base_upstream(repo, branch, base)
        recreated = False
    else:
        start = start_ref(repo, base)
        if not start:
            raise LaneError("base", "the repository has no branch to start from")
        _git_or_fail(["worktree", "add", "--no-track", "-b", branch, root,
                      start], repo)
        recreated = True
    link_junctions(repo, root)
    copy_worktree_includes(repo, root)      # the re-added folder lost them
    ensure_exclude(repo)
    return {**clean_lane(lane), "base": base}, recreated


@dataclass
class LaneStatus:
    exists: bool
    head: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: list = field(default_factory=list)
    # head is in the base, or its changes landed there as other commits
    # (_landed): removing loses nothing
    merged: bool = False
    base_ref: str = ""
    # ignored `.worktreeinclude` files changed or deleted in the lane, plus
    # LOCAL_UNCHECKED when they could not all be checked (local_changes)
    local: list = field(default_factory=list)

    def describe(self) -> str:
        """"3 unmerged commits and 2 modified files", for the close prompt."""
        parts = []
        if not self.merged and self.ahead:
            parts.append(f"{self.ahead} unmerged commit"
                         f"{'' if self.ahead == 1 else 's'}")
        elif not self.merged:
            parts.append("commits that are not in the base branch")
        if self.dirty:
            n = len(self.dirty)
            parts.append(f"{n} modified file{'' if n == 1 else 's'}")
        files = [p for p in self.local if p != LOCAL_UNCHECKED]
        if files:
            more = f" (+{len(files) - 3} more)" if len(files) > 3 else ""
            parts.append(f"local files changed: "
                         f"{', '.join(files[:3])}{more}")
        if len(files) < len(self.local):
            parts.append("local files that could not be checked")
        return " and ".join(parts)

    def dirty_paths(self) -> list:
        """The file names in `dirty`, which holds `status --porcelain` lines.
        The runner strips its output, so the first line may have lost the
        space in front of its status: split on whitespace instead of
        slicing. A rename gives its new name. A name git quoted (spaces at
        the ends, non-ASCII with core.quotePath) is unquoted back to UTF-8."""
        out = []
        for line in self.dirty:
            parts = line.strip().split(None, 1)
            if len(parts) < 2:
                continue
            path = parts[1].split(" -> ")[-1]
            if len(path) > 1 and path[0] == path[-1] == '"':
                path = _unquote_c(path[1:-1])
            out.append(path)
        return out


def _unquote_c(text: str) -> str:
    """git's C-style quoting undone: octal escapes back to the UTF-8 name,
    and `\\t`, `\\"` and `\\\\` back to themselves. The text as it was
    when it doesn't decode."""
    try:
        raw = text.encode("ascii").decode("unicode_escape")
        return raw.encode("latin-1").decode("utf-8")
    except (UnicodeError, ValueError):
        return text


def lane_status(lane: dict) -> LaneStatus:
    """Head, ahead/behind the base, dirty files and whether the head's work
    is already in the base (_in_base). Read-only: `status` runs with --no-optional-locks so
    it never writes the index."""
    root, branch, repo = lane["root"], lane["branch"], lane["repo"]
    base = lane.get("base") or default_base(repo)
    base_ref = start_ref(repo, base)
    if not _is_worktree(root):
        r = git(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"],
                repo)
        head = r.out if r.ok else ""
        st = LaneStatus(exists=False, head=head, base_ref=base_ref)
        st.merged = not head or _in_base(repo, head, base, base_ref)
        return st
    r = git(["rev-parse", "HEAD"], root)
    if not r.ok:
        raise LaneError("git", f"could not read the lane's HEAD: {r.err}")
    st = LaneStatus(exists=True, head=r.out, base_ref=base_ref)
    r = git(["--no-optional-locks", "status", "--porcelain"], root)
    if not r.ok:
        raise LaneError("git", f"could not read the lane's status: {r.err}")
    st.dirty = [ln for ln in r.out.splitlines() if ln.strip()]
    st.local = local_changes(repo, root)
    if base_ref:
        r = git(["rev-list", "--left-right", "--count", f"{base_ref}...HEAD"],
                root)
        if r.ok:
            try:
                behind, ahead = (int(x) for x in r.out.split())
                st.behind, st.ahead = behind, ahead
            except ValueError:
                pass
    st.merged = _in_base(root, st.head, base, base_ref)
    return st


def _in_base(cwd: str, head: str, base: str, base_ref: str) -> bool:
    """True when removing a lane at `head` loses no change the base lacks:
    `head` is in the base, or its work landed there as other commits."""
    refs = list(dict.fromkeys(r for r in (base_ref, base) if r))
    for ref in refs:
        if git(["merge-base", "--is-ancestor", head, ref], cwd).ok:
            return True
    return any(_landed(cwd, head, ref) for ref in refs)


def _landed(cwd: str, head: str, ref: str) -> bool:
    """`head`'s changes are already in `ref` under other commits. An
    integrator that cherry-picks a lane into a batch, a rebase merge and a
    squash merge all leave the lane's own commits out of the base, and an
    ancestry check alone then kept every such lane on close, forever.

    Either test is enough. Every lane commit has a patch-equivalent commit
    in `ref` (cherry-pick, rebase; survives later edits to those lines).
    Or merging `head` into `ref` leaves `ref`'s tree as it is (a squash).
    A lane holding a merge commit skips the first test, because a merge
    has no patch of its own and its conflict resolution could be the only
    copy of some change. The merge test still covers it.

    Like the ancestry check, the patch test reads the base's history, not
    its current tree: a landed commit the base later reverted still counts,
    and the base's history still holds it. Testing the tree instead would
    keep every landed lane again once the base edits the same lines."""
    r = git(["rev-list", "--merges", "--count", f"{ref}..{head}"], cwd)
    if not r.ok:
        return False
    if r.out == "0":
        r = git(["rev-list", "--right-only", "--cherry-pick",
                 f"{ref}...{head}"], cwd)
        if r.ok and not r.out:
            return True
    if not merge_tree_supported():
        return False
    merged = git(["merge-tree", "--write-tree", "--no-messages", ref, head],
                 cwd, MERGE_TIMEOUT)
    tree = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}"], cwd)
    return (merged.rc == 0 and tree.ok and bool(tree.out)
            and merged.out.splitlines()[:1] == [tree.out])


@dataclass
class RemoveResult:
    status: LaneStatus
    branch_deleted: bool = False
    # the ignored files and folders (`dir/`) that went with the worktree
    ignored: list = field(default_factory=list)


def ignored_files(root: str) -> list:
    """The lane's ignored files, with an ignored folder listed once as
    `dir/` and never walked, leaving out the junctions. `git worktree remove`
    deletes these without asking, so the removal's audit line names them."""
    r = git(["ls-files", "-z", "--others", "--ignored", "--exclude-standard",
             "--directory", "--", ".",
             *(f":(exclude){name}" for name in JUNCTION_NAMES)], root)
    return [p for p in r.out.split("\0") if p] if r.ok else []


def remove_lane(lane: dict) -> RemoveResult:
    """Remove a lane that holds nothing: worktree and branch.

    Refuses unless the lane is clean AND both its head and its branch are in
    the base, by ancestry or because their changes landed there (_landed). Unlinks its junctions first and refuses while any directory
    link remains (see the module docstring: git follows them). Never
    `--force`, never `-D`: git's own refusal is the last line of defense,
    and it stays armed.

    The branch goes by compare-and-delete, `update-ref -d <ref> <sha>`, with
    the sha checked here. `branch -d` can't be used: it judges "merged"
    against the branch's upstream or the main checkout's HEAD, and lanes
    have no upstream (see create_lane). If the branch moved meanwhile, git
    refuses, the branch is kept, and LaneError("moved") says so.

    Ignored files (`.env`, build output) go with the worktree. They are
    listed first, into RemoveResult.ignored, so nothing goes silently."""
    st = lane_status(lane)
    if st.dirty or st.local:
        raise LaneError("dirty", st.describe())
    if not st.merged:
        raise LaneError("unmerged", st.describe())
    root, branch, repo = lane["root"], lane["branch"], lane["repo"]
    ref = f"refs/heads/{branch}"
    r = git(["rev-parse", "--verify", "--quiet", ref], repo)
    branch_head = r.out if r.ok else ""
    # a lane whose agent switched branches: its own branch was not measured
    if (branch_head and branch_head != st.head and not _in_base(
            repo, branch_head, lane.get("base") or "", st.base_ref)):
        raise LaneError("unmerged", f"the branch {branch} has commits that "
                                    f"are not in the base branch")
    result = RemoveResult(status=st)
    if st.exists:
        result.ignored = ignored_files(root)
        unlink_junctions(root)
        links = directory_links(root)
        if links:
            raise LaneError("links", "the lane contains links git would "
                                     f"follow: {', '.join(links[:3])}")
        _git_or_fail(["worktree", "remove", root], repo)
    else:
        git(["worktree", "prune"], repo, WRITE_TIMEOUT)
    if branch_head:
        r = git(["update-ref", "-d", ref, branch_head], repo)
        if not r.ok:
            raise LaneError("moved", f"the branch {branch} changed while its "
                                     f"lane was removed, so it was kept: "
                                     f"{r.err or r.out}")
        result.branch_deleted = True
    try:
        os.rmdir(os.path.dirname(root))     # only if it is now empty
    except OSError:
        pass
    return result


@dataclass
class RetireResult:
    removed: bool
    status: LaneStatus | None = None
    reason: str = ""
    code: str = ""          # the LaneError code, or "busy"
    ignored: list = field(default_factory=list)


def retire_lane(lane: dict, pids=(), wait_s: float | None = None) -> RetireResult:
    """Closing a laned card: remove the lane when it holds nothing, keep it
    otherwise.

    Waits for the closed agent's processes to exit first, since Windows
    won't delete a folder that is still a working directory, and a
    half-deleted worktree can no longer be removed cleanly. If one is still
    running when the wait ends, nothing is touched: the result is "busy",
    and the caller may try again later."""
    exited = wait_for_exit(pids, EXIT_WAIT_S if wait_s is None else wait_s)
    try:
        st = lane_status(lane)
    except LaneError as exc:
        return RetireResult(False, None, str(exc), exc.code)
    if st.dirty or st.local or not st.merged:
        return RetireResult(False, st, "")
    if not exited:
        return RetireResult(False, st, "a program is still running in it",
                            "busy")
    try:
        res = remove_lane(lane)
    except LaneError as exc:
        return RetireResult(False, st, str(exc), exc.code)
    return RetireResult(True, res.status, ignored=res.ignored)


def wait_for_exit(pids, timeout: float) -> bool:
    """Block until every pid has exited or `timeout` passed (Windows). True
    when all of them are gone."""
    pids = [p for p in (pids or ()) if p]
    if not pids or sys.platform != "win32":
        return True
    import ctypes
    import time
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    deadline = time.monotonic() + max(0.0, timeout)
    gone = True
    for pid in pids:
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            # ERROR_ACCESS_DENIED: it exists, we just may not wait on it
            if ctypes.get_last_error() == 5:
                gone = False
            continue
        try:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            if kernel32.WaitForSingleObject(handle, left) != 0:  # WAIT_OBJECT_0
                gone = False
        finally:
            kernel32.CloseHandle(handle)
    return gone


# --------------------------------------------------- lane conversations ---

@dataclass
class LaneConversation:
    session_id: str
    mtime: float
    preview: str
    root: str           # the lane folder
    cwd: str            # where the conversation started (root or a subfolder)
    branch: str
    repo: str

    @property
    def lane_name(self) -> str:
        return os.path.basename(self.root)

    def lane_dict(self) -> dict:
        return {"root": self.root, "branch": self.branch, "base": "",
                "repo": self.repo}


def _transcript_origin(path: str, max_lines: int = 60) -> tuple:
    """(cwd, gitBranch) of a Claude transcript's first record that has a
    cwd: where the conversation started. Later records can name another cwd
    (a resume from elsewhere appends to this same file)."""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= max_lines:
                    break
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and isinstance(obj.get("cwd"), str):
                    return obj["cwd"], str(obj.get("gitBranch") or "")
    except OSError:
        pass
    return "", ""


def lane_conversations(repo: str, exclude_roots=(), exclude_ids=()) -> list:
    """Claude conversations that started in one of this repo's lanes, newest
    first, for the New Agent dialog's resume picker. Needs no state of its
    own: `encode_project_dir` maps every non-alphanumeric char to "-", so
    each lane's transcript folder starts with the encoded lanes folder plus
    "-", and each transcript's first cwd gives the exact lane back. The
    prefix also matches unrelated folders (`<repo>-lanes`), which is why the
    cwd is checked. Lanes in `exclude_roots` (a live agent's) and ids in
    `exclude_ids` (a running agent's) are left out. Never raises."""
    from . import session_sync, transcripts
    if not repo:
        return []
    lanes_dir = lanes_dir_for(repo)
    prefix = transcripts.encode_project_dir(lanes_dir) + "-"
    skip_roots = {_norm(r) for r in exclude_roots if r}
    skip_ids = set(exclude_ids or ())
    out = []
    try:
        dirs = [e for e in os.scandir(transcripts.projects_root())
                if e.name.startswith(prefix) and e.is_dir()]
    except OSError:
        return []
    for d in dirs:
        try:
            files = [f for f in os.scandir(d.path) if f.name.endswith(".jsonl")]
        except OSError:
            continue
        for f in files:
            sid = f.name[:-len(".jsonl")]
            if not session_sync.is_session_id(sid) or sid in skip_ids:
                continue
            cwd, branch = _transcript_origin(f.path)
            if not cwd or transcripts.encode_project_dir(cwd) != d.name:
                continue
            try:
                rel = os.path.relpath(os.path.normpath(cwd), lanes_dir)
            except ValueError:
                continue
            first = rel.split(os.sep)[0]
            if not first or first.startswith(".."):
                continue
            root = os.path.join(lanes_dir, first)
            if _norm(root) in skip_roots:
                continue
            if not branch.startswith(BRANCH_PREFIX):
                branch = BRANCH_PREFIX + first
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            out.append(LaneConversation(
                session_id=sid, mtime=mtime,
                preview=session_sync._first_user_text(f.path),
                root=root, cwd=os.path.normpath(cwd), branch=branch,
                repo=os.path.normpath(repo)))
    out.sort(key=lambda c: c.mtime, reverse=True)
    return out


# --------------------------------------------- awareness (Phase 2) ---
# What each lane holds and where lanes overlap, for app/lane_service.py's
# poller. All read-only and lock-free, so the poller calls them directly,
# off the LaneOps queue: status and diff run with --no-optional-locks (no
# index refresh, so no index.lock), and `merge-tree --write-tree` only adds
# objects to the object store (atomic files; no index, no refs). Anything
# that writes refs (fetch, the fast-forward refresh) is further down and
# goes through LaneOps like every other mutation.

# `merge-tree --write-tree` (a real three-way merge with no checkout)
# arrived in git 2.38. Older git falls back to file overlap only.
MERGE_TREE_MIN = (2, 38)
# a lane with a huge untracked tree must not make every poll slow and every
# lanes.json huge: past this many paths the rest are ignored
PATHS_CAP = 2000
MERGE_TIMEOUT = 60.0
FETCH_TIMEOUT = 120.0

_git_version: list = []         # cached (major, minor); tests may clear it


def git_version() -> tuple:
    if not _git_version:
        r = git(["version"], "")
        m = re.search(r"(\d+)\.(\d+)", r.out or "")
        _git_version.append((int(m.group(1)), int(m.group(2))) if m
                            else (0, 0))
    return _git_version[0]


def merge_tree_supported() -> bool:
    return git_version() >= MERGE_TREE_MIN


def _z_list(out: str) -> list:
    return [p for p in (out or "").split("\0") if p][:PATHS_CAP]


def status_paths(root: str) -> list:
    """Repo-relative paths with uncommitted changes in a checkout (modified,
    staged, untracked, the new name of a rename). Porcelain v2 with -z: no
    quoting, and no line starts with a space (the runner strips output)."""
    r = git(["--no-optional-locks", "status", "--porcelain=v2", "-z",
             "--untracked-files=all"], root)
    if not r.ok:
        raise LaneError("git", f"could not read the lane's status: {r.err}")
    toks, out, i = r.out.split("\0"), [], 0
    while i < len(toks) and len(out) < PATHS_CAP:
        tok = toks[i]
        i += 1
        if tok.startswith("1 "):
            parts = tok.split(" ", 8)
        elif tok.startswith("2 "):
            parts = tok.split(" ", 9)
            i += 1                      # the next token is the old name
        elif tok.startswith("u "):
            parts = tok.split(" ", 10)
        elif tok.startswith("? "):
            parts = ["", tok[2:]]
        else:
            continue                    # "! ignored" or noise
        if parts and parts[-1]:
            out.append(parts[-1])
    return out


def committed_paths(root: str, base_ref: str) -> list:
    """Paths the lane's own commits changed since it forked from the base."""
    r = git(["--no-optional-locks", "diff", "--name-only", "-z",
             f"{base_ref}...HEAD"], root)
    return _z_list(r.out) if r.ok else []


def base_paths(root: str, base_ref: str) -> list:
    """Paths the BASE changed since the lane forked from it: what a merge of
    the base into the lane would bring in."""
    r = git(["--no-optional-locks", "diff", "--name-only", "-z",
             f"HEAD...{base_ref}"], root)
    return _z_list(r.out) if r.ok else []


def merge_conflicts(cwd: str, a: str, b: str):
    """Paths that would conflict if commits `a` and `b` were merged, from a
    real three-way merge done in memory (`git merge-tree --write-tree`). []
    when they merge cleanly, None when git can't say (old git, an error)."""
    r = git(["merge-tree", "--write-tree", "--name-only", "--no-messages",
             "-z", a, b], cwd, MERGE_TIMEOUT)
    if r.rc == 0:
        return []
    if r.rc == 1:
        return _z_list(r.out)[1:]       # the first entry is the tree id
    return None


@dataclass
class LaneSnap:
    """One lane at one poll. Paths are repo-relative with "/" separators."""
    uid: str
    agent: str
    ws_id: str
    branch: str
    root: str
    base: str = ""
    base_ref: str = ""
    exists: bool = True
    head: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: list = field(default_factory=list)        # uncommitted
    committed: list = field(default_factory=list)    # own commits since fork
    base_changed: list = field(default_factory=list)  # base, since the fork
    # `git merge-base HEAD <base_ref>`: where this round of work forked from
    # the base. It moves when the lane merges or fast-forwards to the base.
    fork: str = ""
    # "integrator" for the workspace integrator's lane: it holds other
    # lanes' commits by design, so it is never anyone's overlap peer
    role: str = ""
    # the newest of the lane's own commits flagged "Task done" (DONE_GREP)
    # that the base doesn't have yet, by ancestry or as a patch-equal commit
    done: str = ""
    error: str = ""

    def files(self) -> dict:
        """path -> "committed" | "dirty" (uncommitted wins: it is newest)."""
        out = {p: "committed" for p in self.committed}
        out.update({p: "dirty" for p in self.dirty})
        return out


OVERLAP = "overlap"
CONFLICTS = "conflicts"
INTEGRATOR_ROLE = "integrator"


@dataclass
class Overlap:
    """A file this lane changed that someone else changed too. `peer_uid` is
    "" when the someone is the base branch (it moved since the lane forked).
    `state` is the peer's: "dirty" or "committed", or "conflicts" when a
    real merge of the two sides conflicts on this file."""
    path: str
    peer_uid: str
    peer: str
    peer_branch: str
    state: str
    fork: str = ""          # this lane's fork from the base (LaneSnap.fork)
    peer_fork: str = ""     # the peer lane's; "" for the base

    @property
    def level(self) -> str:
        return CONFLICTS if self.state == CONFLICTS else OVERLAP

    @property
    def key(self) -> str:
        """Dedupe key for notices and warnings, built exactly like the
        overlap hook's (session_hook.overlap_key). Uses the level, not the raw
        state: a peer committing a change it already had uncommitted is not
        news, the change turning into a conflict is. Carries both forks: the
        same file overlapping again in a later round of work is news too."""
        from .session_hook import overlap_key
        return overlap_key(self.path, self.peer_uid or "base", self.level,
                           self.fork, self.peer_fork)


@dataclass
class LaneView:
    """What the UI shows for one laned agent: the card chip, the Activity
    panel, the roster columns. Built from a RepoSnapshot."""
    uid: str
    branch: str
    root: str
    base: str = ""
    base_ref: str = ""
    exists: bool = True
    ahead: int = 0
    behind: int = 0
    dirty: list = field(default_factory=list)
    committed: list = field(default_factory=list)
    overlaps: list = field(default_factory=list)
    error: str = ""
    head: str = ""
    done: str = ""

    @property
    def state(self) -> str:
        """"conflict", "overlap", "done" or "clean", for the chip's colour.
        A "Task done" commit turns the chip green, but a file another lane
        also changed outranks it."""
        if any(o.level == CONFLICTS for o in self.overlaps):
            return "conflict"
        if self.overlaps:
            return "overlap"
        return "done" if self.done else "clean"

    def touching(self) -> list:
        """Every file this lane changed, committed or not, sorted."""
        return sorted(set(self.committed) | set(self.dirty))

    def can_refresh(self) -> bool:
        """A fast-forward to the base is safe and useful: nothing of its own
        (no commits, nothing uncommitted) and the base has moved on."""
        return (self.exists and not self.dirty and not self.ahead
                and self.behind > 0 and bool(self.base_ref))


@dataclass
class RepoSnapshot:
    repo: str
    lanes: list = field(default_factory=list)       # [LaneSnap]
    overlaps: dict = field(default_factory=dict)    # uid -> [Overlap]
    merge_tree: bool = False
    ts: float = 0.0

    def lane(self, uid: str):
        return next((s for s in self.lanes if s.uid == uid), None)

    def view(self, uid: str):
        s = self.lane(uid)
        if s is None:
            return None
        return LaneView(uid=s.uid, branch=s.branch, root=s.root, base=s.base,
                        base_ref=s.base_ref, exists=s.exists, ahead=s.ahead,
                        behind=s.behind, dirty=list(s.dirty),
                        committed=list(s.committed),
                        overlaps=list(self.overlaps.get(uid, [])),
                        error=s.error, head=s.head, done=s.done)

    def index(self) -> dict:
        """repo path -> [{uid, agent, branch, state}] over every lane, for
        lanes.json (what the overlap hook reads). An owner's state is its own
        "dirty"/"committed", or "conflicts" when its side of that file
        conflicts with someone's."""
        conflicted = {(uid, o.path) for uid, ovs in self.overlaps.items()
                      for o in ovs if o.level == CONFLICTS}
        out: dict = {}
        for s in self.lanes:
            if s.role == INTEGRATOR_ROLE:
                continue
            for path, state in s.files().items():
                if (s.uid, path) in conflicted:
                    state = CONFLICTS
                out.setdefault(path, []).append(
                    {"uid": s.uid, "agent": s.agent, "branch": s.branch,
                     "state": state})
        return out


def lane_snap(entry: dict, cache: dict | None = None) -> LaneSnap:
    """Read one lane for a poll. Never raises: a lane that can't be read
    comes back with `exists` False or an `error`. `cache` (the poller's, per
    repo) keeps each lane's fork by (head, base sha)."""
    lane = entry["lane"]
    root, repo = lane["root"], lane["repo"]
    snap = LaneSnap(uid=entry["uid"], agent=entry.get("agent", ""),
                    ws_id=entry.get("ws_id", ""), branch=lane["branch"],
                    root=root, base=lane.get("base") or "",
                    role=entry.get("role", ""))
    try:
        if not _is_worktree(root):
            snap.exists = False
            return snap
        snap.base = snap.base or default_base(repo)
        snap.base_ref = start_ref(repo, snap.base)
        r = git(["rev-parse", "HEAD"], root)
        if not r.ok:
            raise LaneError("git", f"could not read the lane's HEAD: {r.err}")
        snap.head = r.out
        snap.dirty = status_paths(root)
        if snap.base_ref:
            r = git(["rev-list", "--left-right", "--count",
                     f"{snap.base_ref}...HEAD"], root)
            if r.ok:
                try:
                    snap.behind, snap.ahead = (int(x) for x in r.out.split())
                except ValueError:
                    pass
            if snap.ahead:
                snap.committed = committed_paths(root, snap.base_ref)
            if snap.behind:
                snap.base_changed = base_paths(root, snap.base_ref)
            snap.fork = _fork(root, snap.head, snap.base_ref, cache)
            if snap.ahead and snap.role != INTEGRATOR_ROLE:
                snap.done = _done_commit(root, snap.head, snap.base_ref,
                                         cache)
    except LaneError as exc:
        snap.error = str(exc)
    return snap


def _fork(root: str, head: str, base_ref: str, cache) -> str:
    """`git merge-base <head> <base_ref>`, cached by (head, base sha): both
    are commits, so the answer never changes for that pair."""
    r = git(["rev-parse", base_ref], root)
    if not r.ok:
        return ""
    key = ("fork", head, r.out)
    if cache is not None and key in cache:
        return cache[key]
    m = git(["merge-base", head, r.out], root)
    fork = m.out if m.ok else ""
    if cache is not None:
        cache[key] = fork
    return fork


def _done_commit(root: str, head: str, base_ref: str, cache) -> str:
    """The newest commit in `head` flagged "Task done" whose change the
    base lacks, or "". `--cherry-pick` leaves out a flagged commit the base
    took as another commit (a cherry-pick or rebase merge), so a lane that
    already landed stops asking. Cached by (head, base sha) like `_fork`."""
    r = git(["rev-parse", base_ref], root)
    if not r.ok:
        return ""
    key = ("done", head, r.out)
    if cache is not None and key in cache:
        return cache[key]
    grep = ["log", "-1", "--format=%H", "-i", "-E", f"--grep={DONE_GREP}"]
    # the cheap read first: most lanes have no flag at all, and only a
    # flagged one is worth the patch ids --cherry-pick computes
    m = git(grep + [f"{r.out}..{head}"], root)
    if m.ok and m.out:
        m = git(grep + ["--right-only", "--cherry-pick",
                        f"{r.out}...{head}"], root)
    done = m.out if m.ok else ""
    if cache is not None:
        cache[key] = done
    return done


def _related(cache, cwd: str, a: str, b: str) -> bool:
    """Is one of the two commits an ancestor of the other (or the same)?
    Cached like the merge results: commits never change."""
    if a == b:
        return True
    key = ("related",) + ((a, b) if a <= b else (b, a))
    if cache is not None and key in cache:
        return cache[key]
    found = (git(["merge-base", "--is-ancestor", a, b], cwd).ok
             or git(["merge-base", "--is-ancestor", b, a], cwd).ok)
    if cache is not None:
        cache[key] = found
    return found


def _cached_conflicts(cache, cwd: str, a: str, b: str):
    key = (a, b) if a <= b else (b, a)
    if cache is not None and key in cache:
        return cache[key]
    found = merge_conflicts(cwd, a, b)
    if cache is not None:
        if len(cache) > 500:            # heads move on; old pairs are dead
            cache.clear()
        cache[key] = found
    return found


def snapshot_repo(repo: str, entries: list, cache: dict | None = None,
                  now: float = 0.0, lock=None) -> RepoSnapshot:
    """Every lane of one repository, and every overlap between them and with
    their base. `entries` are {uid, agent, ws_id, lane} dicts.

    Two lanes overlap on a file both changed (committed or not). When both
    committed changes to it, a real merge of the two heads decides whether it
    CONFLICTS. A lane overlaps its base on a file it changed that the base
    also changed since the fork. `cache` maps a head pair to its merge result
    (commits are immutable, so a result never goes stale); the poller keeps
    one per repo. The base is compared by its ref name, which moves, so that
    pair is cached under the base's resolved sha. `lock` (LaneOps.lock_for)
    is held around each lane read, so no git process sits inside a lane
    folder while a queued job removes it. Never raises for one bad lane."""
    import contextlib
    import time as _time
    snap = RepoSnapshot(repo=repo, merge_tree=merge_tree_supported(),
                        ts=now or _time.time())
    for e in entries:
        with lock if lock is not None else contextlib.nullcontext():
            snap.lanes.append(lane_snap(e, cache))
    # the integrator's lane is left out: it merges the submitted lanes, so
    # "it changed your files too" would be about the integrator itself
    live = [s for s in snap.lanes if s.exists and s.head and not s.error
            and s.role != INTEGRATOR_ROLE]
    files = {s.uid: s.files() for s in live}
    ovs: dict = {}

    def add(uid, overlap):
        ovs.setdefault(uid, []).append(overlap)

    for i, a in enumerate(live):
        for b in live[i + 1:]:
            shared = set(files[a.uid]) & set(files[b.uid])
            if not shared:
                continue
            conflicts = set()
            if (snap.merge_tree and a.head != b.head
                    and set(a.committed) & set(b.committed)):
                conflicts = set(_cached_conflicts(cache, repo, a.head,
                                                  b.head) or ())
            # one lane's commits already contain the other's (it merged that
            # lane): their committed changes are the same work, not news
            related = (set(a.committed) & set(b.committed)
                       and _related(cache, repo, a.head, b.head))
            for path in sorted(shared):
                if (related and files[a.uid][path] == "committed"
                        and files[b.uid][path] == "committed"):
                    continue
                hot = path in conflicts
                add(a.uid, Overlap(path, b.uid, b.agent, b.branch,
                                   CONFLICTS if hot else files[b.uid][path],
                                   a.fork, b.fork))
                add(b.uid, Overlap(path, a.uid, a.agent, a.branch,
                                   CONFLICTS if hot else files[a.uid][path],
                                   b.fork, a.fork))
    base_heads: dict = {}
    for s in live:
        shared = set(files[s.uid]) & set(s.base_changed)
        if not shared or not s.base_ref:
            continue
        conflicts = set()
        if snap.merge_tree and set(s.committed) & shared:
            if s.base_ref not in base_heads:
                r = git(["rev-parse", s.base_ref], repo)
                base_heads[s.base_ref] = r.out if r.ok else ""
            if base_heads[s.base_ref]:
                conflicts = set(_cached_conflicts(
                    cache, repo, s.head, base_heads[s.base_ref]) or ())
        for path in sorted(shared):
            add(s.uid, Overlap(path, "", s.base_ref, s.base_ref,
                               CONFLICTS if path in conflicts else "committed",
                               s.fork))
    snap.overlaps = ovs
    return snap


def describe_overlap(o: Overlap) -> str:
    """One sentence for the agent that owns the lane, as a notice or a hook
    warning. No em dashes: agents quote these back to the user."""
    if not o.peer_uid:
        if o.level == CONFLICTS:
            return (f"{o.peer} changed {o.path} in a way that CONFLICTS with "
                    f"your commits. Merge {o.peer} into your branch now and "
                    f"resolve it while it is small.")
        return (f"{o.peer} changed {o.path}, which you also changed. Merge "
                f"{o.peer} into your branch now, while any conflict is small.")
    who = f"{o.peer} ({o.peer_branch})"
    if o.level == CONFLICTS:
        return (f"Your commits and those of {who} both change {o.path} and "
                f"would CONFLICT when merged. Keep your change there minimal "
                f"and coordinate through the board.")
    how = "uncommitted" if o.state == "dirty" else "committed"
    return (f"{who} also changed {o.path} ({how}). Keep your change there "
            f"small and local; the integrator merges both.")


def to_repo_path(path: str, roots: dict) -> str:
    """The main checkout's path for a file inside a lane (`roots` maps lane
    root -> repo root); any other path comes back unchanged. Lets the File
    Map show one tree for a repo, however many lanes touched it."""
    if not path:
        return path
    norm = os.path.normpath(path)
    for root, repo in roots.items():
        try:
            rel = os.path.relpath(norm, root)
        except ValueError:                  # another drive
            continue
        if rel == os.curdir or rel == os.pardir or \
                rel.startswith(os.pardir + os.sep):
            continue
        return os.path.join(repo, rel)
    return path


def fetch_base(repo: str, base: str) -> bool:
    """`git fetch origin <base>`, so overlap checks see what landed on the
    base. Writes refs: runs through LaneOps. False when there is no origin
    or the fetch failed (offline is normal, not an error worth a dialog).
    An empty `base` is looked up here, on the LaneOps worker."""
    if not git(["remote", "get-url", "origin"], repo).ok:
        return False
    base = base or default_base(repo)
    if not base:
        return False
    return git(["fetch", "--quiet", "--no-tags", "origin", base], repo,
               FETCH_TIMEOUT).ok


def refresh_lane(lane: dict) -> str:
    """Fast-forward a lane that holds nothing of its own to its base, so the
    agent starts its next task from the current base. Refuses a dirty lane
    and a lane with commits of its own (only ever `--ff-only`: no merge
    commit, no rewrite, nothing to lose). Returns the ref it moved to, ""
    when it was already there. Writes refs: runs through LaneOps."""
    st = lane_status(lane)
    if not st.exists:
        raise LaneError("missing", f"the lane folder {lane['root']} is gone")
    if st.dirty:
        raise LaneError("dirty", st.describe())
    if st.ahead or not st.merged:
        raise LaneError("unmerged", "the lane has commits of its own")
    if not st.base_ref:
        raise LaneError("base", "the lane has no base branch to follow")
    if not st.behind:
        return ""
    _git_or_fail(["merge", "--ff-only", "--quiet", st.base_ref], lane["root"])
    return st.base_ref


# -------------------------------------------------- .worktreeinclude ---

WORKTREEINCLUDE = ".worktreeinclude"
INCLUDE_MAX_FILES = 200
INCLUDE_MAX_BYTES = 50 * 1024 * 1024
# the record of what copy_worktree_includes copied into a lane, kept in the
# lane's own git folder (_include_manifest_path)
INCLUDE_MANIFEST = "aihive-worktreeinclude.json"
# the local_changes entry for files it could not check. `<` and `>` can't be
# in a Windows file name, so no real path reads the same.
LOCAL_UNCHECKED = "<unchecked>"


def _include_candidates(cwd: str, spec: str) -> tuple:
    """(paths, complete): untracked files under `cwd` that `spec` (a
    `.worktreeinclude`) names AND git ignores, repo-relative. `complete` is
    False when git failed or there were more candidates than the walk checks:
    the list may then leave files out. The junction folders are pruned from
    the walk by pathspec exclusion. `--exclude=node_modules/` would not do
    it: it marks every file under node_modules ignored, so git lists all of
    them (10,001 paths on a 10,000-file node_modules, against 1 with the
    pathspec, measured)."""
    r = git(["ls-files", "-z", "--others", "--ignored",
             f"--exclude-from={spec}", "--", ".",
             *(f":(exclude){name}" for name in JUNCTION_NAMES)], cwd)
    if not r.ok:
        return [], False
    cands = [p for p in _z_list(r.out)
             if p.replace("\\", "/").split("/")[0] not in JUNCTION_NAMES]
    complete = len(cands) <= INCLUDE_MAX_FILES * 2
    cands = cands[:INCLUDE_MAX_FILES * 2]
    ignored = []
    for i in range(0, len(cands), 50):
        chunk = cands[i:i + 50]
        # -z needs --stdin, which the runner has no pipe for: read lines, and
        # keep only exact matches (a name git had to quote is skipped)
        r = git(["-c", "core.quotepath=off", "check-ignore", "--", *chunk],
                cwd)
        if r.rc in (0, 1):          # 1 = none of these is ignored
            wanted = set(chunk)
            ignored.extend(ln for ln in r.out.splitlines() if ln in wanted)
        else:
            complete = False
    return ignored, complete


def _include_manifest_path(root: str) -> str:
    """Where the lane at `root` records the files copied into it: its own
    git folder, `<repo>/.git/worktrees/<id>/`. No commit can reach it, and
    `git worktree remove` deletes it with the lane. "" when `root` is not
    the top folder of a linked worktree (its `.git` is then no file), so
    the main checkout's git folder is never written."""
    if not os.path.isfile(os.path.join(root, ".git")):
        return ""
    r = git(["rev-parse", "--show-toplevel", "--git-path", INCLUDE_MANIFEST],
            root)
    lines = r.out.splitlines() if r.ok else []
    if len(lines) != 2 or not same_path(lines[0], root):
        return ""
    return os.path.join(root, lines[1])     # --git-path may be cwd-relative


def _write_include_manifest(root: str, copied: list) -> bool:
    path = _include_manifest_path(root)
    if not path:
        return False
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"copied": copied}, fh)
        os.replace(tmp, path)
    except OSError:
        return False
    return True


def _read_include_manifest(root: str):
    """The repo paths copy_worktree_includes recorded copying into the lane
    at `root`, or None without a readable record: a lane made before AI Hive
    kept one, or git or the file failed."""
    path = _include_manifest_path(root)
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    paths = data.get("copied") if isinstance(data, dict) else None
    if not isinstance(paths, list) or not all(
            isinstance(p, str) and p for p in paths):
        return None
    return paths


def local_changes(repo: str, root: str) -> list:
    """The lane's `.worktreeinclude` files that differ from the main
    checkout's copy: a `.env` the agent edited in its lane, one the main
    checkout doesn't have, or a copy the agent deleted (listed as
    "<path> (deleted)"). They are ignored, so `git status` never shows
    them, and `git worktree remove` deletes ignored files without asking.

    A deletion is judged against the record copy_worktree_includes left, so
    a file the main checkout gained after the lane was made is not taken
    for one. A lane without a readable record counts every file the main
    checkout would copy as copied.

    Fails closed: when a listing can't be completed (git failed, or more
    files than the walk checks) the result holds LOCAL_UNCHECKED, which
    keeps the lane like any change does. [] when the repo has no
    `.worktreeinclude` and the lane recorded no copies."""
    import filecmp
    spec = os.path.join(repo, WORKTREEINCLUDE)
    copied = _read_include_manifest(root)
    present, complete = [], True
    if os.path.isfile(spec):
        present, complete = _include_candidates(root, spec)
        if copied is None:
            copied, main_complete = _include_candidates(repo, spec)
            complete = complete and main_complete
    out = []
    for rel in dict.fromkeys([*present, *(copied or ())]):
        mine, theirs = os.path.join(root, rel), os.path.join(repo, rel)
        name = rel.replace("\\", "/")
        try:
            if not os.path.lexists(mine):
                if os.path.lexists(theirs):
                    out.append(f"{name} (deleted)")
                continue
            same = (os.path.isfile(theirs)
                    and filecmp.cmp(mine, theirs, shallow=False))
        except OSError:
            same = False            # can't compare: keep the lane
        if not same:
            out.append(name)
    if not complete:
        out.append(LOCAL_UNCHECKED)
    return out


def copy_worktree_includes(repo: str, root: str) -> list:
    """Copy the repo's gitignored local files that `.worktreeinclude`
    (gitignore syntax, at the repo root) names into a new lane: a `.env`, a
    local config. Only files git IGNORES are copied (a tracked file is
    already in the lane), never anything under a junction, never over an
    existing file, and at most INCLUDE_MAX_FILES / INCLUDE_MAX_BYTES.
    Records what it copied in the lane's git folder, an empty record
    without a `.worktreeinclude`, so local_changes can tell a copy the agent
    deleted from a file the lane never got. Best-effort: returns the repo
    paths copied, never raises."""
    import shutil
    spec = os.path.join(repo, WORKTREEINCLUDE)
    ignored = (_include_candidates(repo, spec)[0] if os.path.isfile(spec)
               else [])
    copied, total = [], 0
    for rel in ignored:
        if len(copied) >= INCLUDE_MAX_FILES:
            break
        src, dst = os.path.join(repo, rel), os.path.join(root, rel)
        try:
            if not os.path.isfile(src) or os.path.lexists(dst):
                continue
            size = os.path.getsize(src)
            if total + size > INCLUDE_MAX_BYTES:
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            total += size
            copied.append(rel.replace("\\", "/"))
        except OSError:
            continue
    _write_include_manifest(root, copied)
    return copied
