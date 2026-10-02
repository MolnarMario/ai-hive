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
install run inside a lane writes there.
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


class LaneError(Exception):
    """A lane operation that did not happen. `code` says why, for the audit
    line: exists, base, git, dirty, unmerged, links, missing, location."""

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
    git. The New Agent dialog asks this to decide whether to offer a lane,
    and with the master switch off AI Hive must run no git command at all."""
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


def create_lane(lane: dict) -> dict:
    """Create a NEW lane: `git worktree add --track -b <branch> <root>
    <start>`, then the junctions and the exclude line. Returns the lane
    record with its base filled in.

    Collisions fail and never reuse: an existing folder or branch raises
    LaneError("exists") and touches nothing. Attaching a new agent to an
    existing branch could hand it someone else's work. (`--track` makes the
    branch's upstream the base, so `git branch -d` later accepts it once it
    is merged there, whatever the main checkout has checked out.)"""
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
    _git_or_fail(["worktree", "add", "--track", "-b", branch, root, start], repo)
    link_junctions(repo, root)
    ensure_exclude(repo)
    return {**clean_lane(lane), "base": base}


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
        recreated = False
    else:
        start = start_ref(repo, base)
        if not start:
            raise LaneError("base", "the repository has no branch to start from")
        _git_or_fail(["worktree", "add", "--track", "-b", branch, root, start],
                     repo)
        recreated = True
    link_junctions(repo, root)
    ensure_exclude(repo)
    return {**clean_lane(lane), "base": base}, recreated


@dataclass
class LaneStatus:
    exists: bool
    head: str = ""
    ahead: int = 0
    behind: int = 0
    dirty: list = field(default_factory=list)
    merged: bool = False        # head is in the base: removing loses nothing
    base_ref: str = ""

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
        return " and ".join(parts)


def lane_status(lane: dict) -> LaneStatus:
    """Head, ahead/behind the base, dirty files and whether the head is
    already in the base. Read-only: `status` runs with --no-optional-locks so
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
    for ref in dict.fromkeys(r for r in (base_ref, base) if r):
        if git(["merge-base", "--is-ancestor", head, ref], cwd).ok:
            return True
    return False


@dataclass
class RemoveResult:
    status: LaneStatus
    branch_deleted: bool = False
    branch_error: str = ""


def remove_lane(lane: dict) -> RemoveResult:
    """Remove a lane that holds nothing: worktree and branch.

    Refuses unless the lane is clean AND its head is in the base. Unlinks
    its junctions first and refuses while any directory link remains (see
    the module docstring: git follows them). Never `--force`, never `-D`:
    git's own refusal is the last line of defense, and it stays armed."""
    st = lane_status(lane)
    if st.dirty:
        raise LaneError("dirty", st.describe())
    if not st.merged:
        raise LaneError("unmerged", st.describe())
    root, branch, repo = lane["root"], lane["branch"], lane["repo"]
    if st.exists:
        unlink_junctions(root)
        links = directory_links(root)
        if links:
            raise LaneError("links", "the lane contains links git would "
                                     f"follow: {', '.join(links[:3])}")
        _git_or_fail(["worktree", "remove", root], repo)
    else:
        git(["worktree", "prune"], repo, WRITE_TIMEOUT)
    result = RemoveResult(status=st)
    if ref_exists(repo, f"refs/heads/{branch}"):
        r = git(["branch", "-d", branch], repo)
        result.branch_deleted = r.ok
        result.branch_error = "" if r.ok else (r.err or r.out)
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
    branch_error: str = ""


def retire_lane(lane: dict, pids=(), wait_s: float = EXIT_WAIT_S) -> RetireResult:
    """Closing a laned card: remove the lane when it holds nothing, keep it
    otherwise. Waits for the closed agent's processes to exit first, since
    Windows won't delete a folder that is still a working directory, and a
    half-deleted worktree can no longer be removed cleanly."""
    wait_for_exit(pids, wait_s)
    try:
        st = lane_status(lane)
    except LaneError as exc:
        return RetireResult(False, None, str(exc))
    if st.dirty or not st.merged:
        return RetireResult(False, st, "")
    try:
        res = remove_lane(lane)
    except LaneError as exc:
        return RetireResult(False, st, str(exc))
    return RetireResult(True, res.status, "", res.branch_error)


def wait_for_exit(pids, timeout: float) -> None:
    """Block until every pid has exited or `timeout` passed (Windows)."""
    pids = [p for p in (pids or ()) if p]
    if not pids or sys.platform != "win32":
        return
    import ctypes
    import time
    kernel32 = ctypes.windll.kernel32
    deadline = time.monotonic() + max(0.0, timeout)
    for pid in pids:
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            continue                    # already gone
        try:
            left = max(0, int((deadline - time.monotonic()) * 1000))
            kernel32.WaitForSingleObject(handle, left)
        finally:
            kernel32.CloseHandle(handle)


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
