"""The approved integration queue: agent lanes Phase 3.

Design: `.scratch/agent-lanes/spec-v3.md`, "Phase 3: Approved integration
queue". A laned agent's work reaches the base branch one lane at a time:

1. The USER submits a lane ("Submit to integrator" on its lane chip). That
   pins the lane's current commit; later commits stay on the lane.
2. When the workspace's integrator agent (a laned Claude agent the user picked)
   is idle and the queue's head item is queued, AI Hive types a short brief
   into it: the pinned commit, its log and diffstat, a precomputed merge
   check against the base and the other queued lanes, the test command and a
   checklist (`docs/agents/integration.md` from the base branch, or the
   built-in default). The integrator branches `integrate/<lane>-<item>` from
   the pinned commit, merges the base, bumps the version, commits, runs the
   suite ON THAT COMMIT, opens a pull request whose body names it
   (`Tested-commit: <sha>`) and stops. It never merges.
3. When the integrator's turn ends, AI Hive reads the PR (`read_outcome`). An
   open PR from the item's own branch, into the item's base, that contains
   the pinned commit and whose head is the tested commit (or adds only
   README.md on top of it) waits for approval. Anything else needs the user,
   with a `reason` saying why.
4. The USER clicks Approve merge, and AI Hive itself runs a guarded merge
   (`approve_merge`): it refuses when the PR head moved since the test run
   or the base can't be confirmed with GitHub, sends the item back to the
   integrator when the base moved since the test run, and merges with
   `gh pr merge --match-head-commit`, so GitHub refuses too if the head
   moved in between. It counts as merged only when GitHub says MERGED after
   that merge call, which also covers squash and rebase merges.

An item becomes MERGED only through Approve merge or the user's Mark merged
(a PR merged on github.com), and only when GitHub says MERGED. The queue never
skips a failed item by itself, and it is paused while the Agent lanes switch
is off.

What AI Hive can't prove: that the suite really ran on the Tested-commit. That
line is the integrator's claim; the RESULT line next to it is for the user,
who approves.

Qt-free and stdlib only. `gh` goes through GH_RUNNER and git through
`lanes.RUNNER`, so tests inject fakes. Anything that writes refs (a fetch, the
merge) runs on the repo's LaneOps worker like every other lane mutation.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field, fields

from . import lanes
from .lanes import GitResult

QUEUED = "queued"
INTEGRATING = "integrating"
AWAITING = "awaiting-approval"
MERGING = "merging"
MERGED = "merged"
NEEDS_YOU = "needs-you"
SKIPPED = "skipped"
STATES = (QUEUED, INTEGRATING, AWAITING, MERGING, MERGED, NEEDS_YOU, SKIPPED)
# still in line: the head of the queue is the first of these
OPEN_STATES = (QUEUED, INTEGRATING, AWAITING, MERGING, NEEDS_YOU)
DONE_KEEP = 20          # finished items kept, newest last, for the panel

STATE_LABEL = {
    QUEUED: "queued", INTEGRATING: "integrating",
    AWAITING: "awaiting your approval", MERGING: "merging",
    MERGED: "merged", NEEDS_YOU: "needs you", SKIPPED: "skipped",
}

# why an item needs the user (QueueItem.reason)
REASON_NO_PR = "no-pr"                  # no pull request from its branch yet
REASON_HEAD_MOVED = "head-moved"        # the PR changed after it was tested
REASON_MERGED_OUTSIDE = "merged-outside"  # merged, but not by Approve merge
REASON_UNTESTED = "untested-head"       # no usable Tested-commit for the head
REASON_BASE_UNKNOWN = "base-unknown"    # the base could not be confirmed
REASON_WRONG_PR = "wrong-pr"            # base, head branch or fork mismatch
REASON_PR_STATE = "pr-state"            # closed, or unreadable
# the only NEEDS_YOU the integrator's next turn end rechecks by itself: the
# integrator may simply not have opened the PR yet. Every other reason waits
# for the user's Recheck, or a turn end would re-approve what the user's
# Approve just refused.
AUTO_RECHECK_REASONS = (REASON_NO_PR,)

INTEGRATE_PREFIX = "integrate/"
CHECKLIST_DOC = "docs/agents/integration.md"
GH_TIMEOUT = 60.0
MERGE_TIMEOUT = 180.0
LOG_LINES = 30
STAT_LINES = 40
# the PR body line naming the commit the suite ran on
TESTED_RE = re.compile(r"^\s*Tested-commit:\s*`?([0-9a-fA-F]{7,40})`?\s*$",
                       re.MULTILINE)
# what may change between the tested commit and the PR head: the test count
AFTER_TEST_OK = ("README.md",)

CREATE_NO_WINDOW = 0x08000000


# ----------------------------------------------------------------- items ---

@dataclass
class QueueItem:
    """One submitted lane. `sha` is pinned when the user submits; `tested_sha`
    is the PR head the integrator tested (Approve merges only that head), and
    `suite_sha` the commit its PR body says the suite ran on. Persisted in the
    session (Workspace.integration_queue)."""
    id: str
    lane_uid: str
    agent: str              # the agent's name when it was submitted
    branch: str             # the lane branch
    sha: str
    base: str = ""          # branch NAME (`main`), as in the lane record
    submit_ahead: int = 0   # the lane's commits ahead of the base at submit
    ts: float = 0.0
    state: str = QUEUED
    pr: int = 0
    pr_url: str = ""
    tested_sha: str = ""
    note: str = ""          # why it needs the user, or how it ended
    rebrief: bool = False   # the base moved: send the short re-merge brief
    updated: float = 0.0
    # why a NEEDS_YOU item needs the user (REASON_*). Only AUTO_RECHECK_REASONS
    # let the integrator's next turn end recheck it by itself.
    reason: str = ""
    # the commit the suite ran on (the PR body's Tested-commit line)
    suite_sha: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d) -> "QueueItem | None":
        """A persisted item, validated. Anything malformed is dropped on load
        rather than raising: a bad queue row must never cost the session."""
        if not isinstance(d, dict):
            return None
        known = {f.name for f in fields(cls)}
        try:
            item = cls(**{k: v for k, v in d.items() if k in known})
        except TypeError:
            return None
        if not (isinstance(item.id, str) and item.id
                and isinstance(item.lane_uid, str) and item.lane_uid
                and isinstance(item.branch, str) and item.branch
                and isinstance(item.sha, str) and item.sha):
            return None
        if item.state not in STATES:
            item.state = NEEDS_YOU
        for name, default in (("agent", ""), ("base", ""), ("pr_url", ""),
                              ("tested_sha", ""), ("note", ""), ("reason", ""),
                              ("suite_sha", "")):
            if not isinstance(getattr(item, name), str):
                setattr(item, name, default)
        for name in ("pr", "submit_ahead"):
            try:
                setattr(item, name, int(getattr(item, name) or 0))
            except (TypeError, ValueError):
                setattr(item, name, 0)
        for name in ("ts", "updated"):
            try:
                setattr(item, name, float(getattr(item, name) or 0.0))
            except (TypeError, ValueError):
                setattr(item, name, 0.0)
        item.rebrief = bool(item.rebrief)
        return item

    @property
    def is_open(self) -> bool:
        return self.state in OPEN_STATES

    @property
    def short(self) -> str:
        return self.sha[:7]

    @property
    def integrate_branch(self) -> str:
        return integrate_branch(self.branch, self.id)


def new_item(lane_uid: str, agent: str, branch: str, sha: str, base: str,
             ahead: int = 0, now: float = 0.0) -> QueueItem:
    now = now or time.time()
    return QueueItem(id=uuid.uuid4().hex[:12], lane_uid=lane_uid, agent=agent,
                     branch=branch, sha=sha, base=base, submit_ahead=ahead,
                     ts=now, updated=now)


def clean_queue(value) -> list:
    """The persisted queue, validated: [QueueItem]. Never raises."""
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for d in value:
        item = QueueItem.from_dict(d)
        if item is not None and item.id not in seen:
            seen.add(item.id)
            out.append(item)
    return out


def restored(item: QueueItem) -> QueueItem:
    """An item as it should come back after AI Hive restarts. A merge that was
    in flight may or may not have happened, so it needs the user (Recheck
    reads the PR). Everything else keeps its state: an integrator that was
    mid-turn resumes its conversation and its turn end is read as usual."""
    if item.state == MERGING:
        item.state = NEEDS_YOU
        item.reason = REASON_PR_STATE
        item.note = ("AI Hive closed during the merge. Recheck to read the "
                     "pull request's state.")
    return item


def head(queue: list):
    """The item the queue is working on: the first one still in line."""
    return next((i for i in queue if i.is_open), None)


def prune(queue: list, keep: int = DONE_KEEP) -> list:
    """Every open item, plus the newest `keep` finished ones."""
    done = [i for i in queue if not i.is_open]
    drop = {i.id for i in done[:max(0, len(done) - keep)]}
    return [i for i in queue if i.id not in drop]


def integrate_branch(lane_branch: str, item_id: str = "") -> str:
    """`integrate/<lane branch tail>-<item id[:6]>`: hive/agent-5-3fa9c1,
    item 7d01c2... -> integrate/agent-5-3fa9c1-7d01c2.

    One per ITEM, not per lane: a lane lives for many tasks, and with one
    branch per lane a resubmitted lane's turn end found the previous item's
    merged pull request under the same branch name. Derived from the item,
    never stored."""
    tail = lane_branch[len(lanes.BRANCH_PREFIX):] \
        if lane_branch.startswith(lanes.BRANCH_PREFIX) else lane_branch
    tail = tail.strip("/")
    if item_id:
        tail = f"{tail}-{item_id[:6]}"
    return INTEGRATE_PREFIX + tail


# -------------------------------------------------------------------- gh ---

def _subprocess_gh(args: list, cwd: str, timeout: float) -> GitResult:
    env = dict(os.environ)
    env.update({"GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1",
                "NO_COLOR": "1", "GIT_TERMINAL_PROMPT": "0",
                "GCM_INTERACTIVE": "never"})
    try:
        proc = subprocess.run(
            ["gh", *args], cwd=cwd or None, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout, env=env,
            creationflags=CREATE_NO_WINDOW if sys.platform == "win32" else 0)
    except FileNotFoundError:
        return GitResult(127, "", "gh (the GitHub CLI) is not installed")
    except (OSError, subprocess.SubprocessError) as exc:
        return GitResult(-1, "", f"{type(exc).__name__}: {exc}")
    return GitResult(proc.returncode, proc.stdout.strip(), proc.stderr.strip())


# callable(args, cwd, timeout) -> lanes.GitResult. Replaced by tests.
GH_RUNNER = _subprocess_gh


def gh(args: list, cwd: str, timeout: float = GH_TIMEOUT) -> GitResult:
    return GH_RUNNER(list(args), cwd, timeout)


def gh_ready(repo: str) -> tuple:
    """(ok, reason): can AI Hive open and merge pull requests for `repo`?
    Needs an origin remote, an installed and logged-in `gh`, and a remote gh
    recognises as GitHub. One `gh repo view` answers the last three."""
    if not repo or not os.path.isdir(repo):
        return False, "The repository folder is missing."
    if not lanes.git(["remote", "get-url", "origin"], repo).ok:
        return False, "The repository has no origin remote to open pull requests on."
    r = gh(["repo", "view", "--json", "nameWithOwner"], repo)
    if r.ok:
        return True, ""
    err = (r.err or r.out or "").strip()
    low = err.lower()
    if r.rc == 127 or "not installed" in low:
        return False, "Install the GitHub CLI (gh) to use the integration queue."
    if "auth login" in low or "not logged" in low or "authenticat" in low:
        return False, "Log gh in first: run gh auth login in a terminal."
    if "remote" in low or "not a git repository" in low:
        return False, "The origin remote is not a GitHub repository."
    return False, f"gh could not read the repository: {err[:200]}"


PR_FIELDS = ("number,state,headRefOid,url,baseRefName,headRefName,"
             "isCrossRepository,body")


@dataclass
class PrInfo:
    number: int
    state: str              # OPEN | CLOSED | MERGED
    head: str               # headRefOid
    url: str = ""
    base: str = ""          # baseRefName
    head_branch: str = ""   # headRefName
    cross_repo: bool = False    # opened from a fork
    body: str = ""


def pr_view(repo: str, ref: str) -> "PrInfo | None":
    """The pull request for a branch name or number, or None when there is
    none (or gh can't say)."""
    r = gh(["pr", "view", str(ref), "--json", PR_FIELDS], repo)
    if not r.ok:
        return None
    try:
        d = json.loads(r.out)
        return PrInfo(number=int(d.get("number") or 0),
                      state=str(d.get("state") or "").upper(),
                      head=str(d.get("headRefOid") or ""),
                      url=str(d.get("url") or ""),
                      base=str(d.get("baseRefName") or ""),
                      head_branch=str(d.get("headRefName") or ""),
                      cross_repo=bool(d.get("isCrossRepository")),
                      body=str(d.get("body") or ""))
    except (ValueError, TypeError, AttributeError):
        return None


def gh_base_sha(repo: str, base: str) -> str:
    """The base branch's head as GitHub has it right now, "" when gh can't
    say. `{owner}/{repo}` are filled in by gh from the current repo."""
    r = gh(["api", f"repos/{{owner}}/{{repo}}/branches/{base}", "--jq",
            ".commit.sha"], repo)
    sha = r.out.strip() if r.ok else ""
    return sha if re.fullmatch(r"[0-9a-f]{40}", sha) else ""


# ------------------------------------------------------------- the brief ---

DEFAULT_TEST_COMMAND = "the project's full test suite (see its README)"

DEFAULT_CHECKLIST = """\
1. Create the branch {integrate} from the submitted commit in your own lane:
   `git switch -c {integrate} {sha}`. Work only on that branch.
2. Merge {base_ref} into it (`git fetch origin` first) and resolve any
   conflicts. Never rebase and never force-push.
3. If the project keeps a version number and a changelog, bump the version
   and add the changelog entry now, once for this pull request.
4. Review the change and fix what you find.
5. Commit.
6. Run the full test suite on that commit: {test_command}.
7. After that, only a test count in README.md may change. If anything else
   changes, commit it and run step 6 again.
8. Push with `git push -u origin {integrate}` and open the pull request with
   `gh pr create --base {base} --head {integrate}`. The PR body has a line
   `Tested-commit: <the full sha step 6 ran on>` and the test result. For
   every failure say whether it also fails on {base_ref}. Never decide
   yourself that a failure is acceptable.
9. Never run `gh pr merge`. Stop and report: the user approves the merge in
   AI Hive."""


def _section(text: str, title: str) -> str:
    """The body of the `## <title>` section of a markdown document."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().lower() == title.lower()
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def _test_command(text: str) -> str:
    """The command after a `Test command:` line, backticks stripped."""
    for line in text.splitlines():
        s = line.strip()
        if s.lower().startswith("test command:"):
            cmd = s.split(":", 1)[1].strip().strip("`").strip()
            if cmd:
                return cmd
    return ""


def checklist_for(repo: str, base_ref: str) -> tuple:
    """(checklist template, test command) for the brief. Read from the BASE
    branch's `docs/agents/integration.md` (its "## Checklist" section and its
    `Test command:` line), so what the integrator follows is what the base
    says, not whatever the main checkout has checked out. Falls back to the
    built-in default."""
    text = ""
    if base_ref:
        r = lanes.git(["show", f"{base_ref}:{CHECKLIST_DOC}"], repo)
        text = r.out if r.ok else ""
    steps = _section(text, "Checklist") if text else ""
    return (steps or DEFAULT_CHECKLIST), (_test_command(text)
                                          or DEFAULT_TEST_COMMAND)


def _fill(template: str, values: dict) -> str:
    out = template
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


# C0 control characters except tab and newline, and DEL: an ESC[201~ would
# end the brief's bracketed paste early and a CR would submit what follows
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def lane_text(text: str) -> str:
    """Lane-authored text (commit subjects, file names) made safe for the
    brief: control characters dropped, and a code fence can't be closed
    from inside one."""
    return _CONTROL.sub("", text or "").replace("```", "'''")


@dataclass
class BriefFacts:
    """What `gather_brief` read from git, for `integration_brief`."""
    base_ref: str = ""
    base_sha: str = ""
    log: list = field(default_factory=list)
    stat: list = field(default_factory=list)
    base_conflicts: object = None       # [] clean, [paths], None unknown
    peers: list = field(default_factory=list)   # [(item, conflicts)]
    checklist: str = DEFAULT_CHECKLIST
    test_command: str = DEFAULT_TEST_COMMAND


def _conflict_line(conflicts) -> str:
    if conflicts is None:
        return "not checked (needs git 2.38 or newer)"
    if not conflicts:
        return "merges cleanly"
    shown = ", ".join(lane_text(p) for p in conflicts[:8])
    more = f" (+{len(conflicts) - 8} more)" if len(conflicts) > 8 else ""
    return f"CONFLICTS in {shown}{more}"


def integration_brief(item: QueueItem, facts: BriefFacts) -> str:
    """The brief AI Hive types into the integrator: about 30 lines, enough
    that the integrator needs nothing from the board. Pure. Everything the
    lane's agent wrote (commit subjects, file names) sits inside fenced
    blocks, with control characters stripped (`lane_text`)."""
    base_ref = facts.base_ref or (f"origin/{item.base}" if item.base
                                  else "the base branch")
    integrate = item.integrate_branch
    log = [lane_text(ln) for ln in facts.log[:LOG_LINES]]
    if len(facts.log) > LOG_LINES:
        log.append(f"...and {len(facts.log) - LOG_LINES} more")
    stat = [lane_text(ln) for ln in facts.stat[:STAT_LINES]]
    lines = [
        f"AI Hive integration brief {item.id}. You are this workspace's "
        f"integrator.",
        f"Lane: {item.agent} on {item.branch}.",
        f"Submitted commit: {item.sha} (pinned: later commits on the lane are "
        f"not part of this).",
        f"Base: {base_ref}" + (f" at {facts.base_sha[:7]}"
                               if facts.base_sha else "") + ".",
        f"Your branch for this item: {integrate}.",
        "",
        f"Commits (git log --oneline {base_ref}..{item.short}), written by "
        f"the lane's agent:",
        "```",
        *(log or ["(none)"]),
        "```",
        "Files (git diff --stat):",
        "```",
        *(stat or ["(none)"]),
        "```",
        f"Merge check against {base_ref}: "
        f"{_conflict_line(facts.base_conflicts)}.",
    ]
    for peer, conflicts in facts.peers:
        lines.append(f"Against {peer.agent}'s queued lane ({peer.branch}): "
                     f"{_conflict_line(conflicts)}.")
    lines += ["", f"Test command: {facts.test_command}", "", "Checklist:",
              _fill(facts.checklist, {
                  "integrate": integrate, "sha": item.sha,
                  "base_ref": base_ref, "base": item.base or "main",
                  "test_command": facts.test_command}),
              "",
              f"When you are done, end your turn. AI Hive then reads the "
              f"pull request from {integrate}, checks that its head is the "
              f"commit its Tested-commit line names (plus README.md at "
              f"most), and asks the user to approve the merge."]
    return "\n".join(lines)


def base_moved_brief(item: QueueItem, base_ref: str) -> str:
    """The short follow-up when the base moved after the test run: the same
    item continues, it is not a new assignment."""
    integrate = item.integrate_branch
    pr = f"pull request #{item.pr}" if item.pr else "the pull request"
    return (f"AI Hive integration brief {item.id}, continued: {base_ref} "
            f"moved after your test run, so {pr} was not merged. On "
            f"{integrate}, merge {base_ref} again (`git fetch origin` first, "
            f"never rebase or force-push), resolve any conflicts, commit, "
            f"rerun the full test suite on that commit, push, and update the "
            f"PR body: its Tested-commit line to the commit you just tested, "
            f"and the new result. Never run `gh pr merge`. End your turn when "
            f"the PR is updated.")


def gather_rebrief(repo: str, item: QueueItem) -> str:
    """`base_moved_brief` with the base resolved the way every lane resolves
    it (`lanes.start_ref`). Read-only."""
    base = item.base or lanes.default_base(repo)
    return base_moved_brief(item, lanes.start_ref(repo, base)
                            or f"origin/{base or 'main'}")


def _lines(text: str) -> list:
    return [ln.rstrip() for ln in (text or "").splitlines() if ln.strip()]


def gather_brief(repo: str, item: QueueItem, peers: list) -> str:
    """Build the brief for `item` from git. Read-only (log, diff, show and an
    in-memory merge-tree), so it can run on any thread. `peers` are the other
    open items, each checked for a conflict with this one."""
    facts = BriefFacts()
    facts.base_ref = lanes.start_ref(repo, item.base or lanes.default_base(repo))
    if facts.base_ref:
        r = lanes.git(["rev-parse", facts.base_ref], repo)
        facts.base_sha = r.out if r.ok else ""
        r = lanes.git(["log", "--oneline", "--no-decorate",
                       f"{facts.base_ref}..{item.sha}"], repo)
        facts.log = _lines(r.out) if r.ok else []
        r = lanes.git(["diff", "--stat=100", f"{facts.base_ref}...{item.sha}"],
                      repo)
        facts.stat = _lines(r.out) if r.ok else []
        facts.checklist, facts.test_command = checklist_for(repo,
                                                            facts.base_ref)
    supported = lanes.merge_tree_supported()
    if facts.base_sha and supported:
        facts.base_conflicts = lanes.merge_conflicts(repo, item.sha,
                                                     facts.base_sha)
    for peer in peers:
        if peer.id == item.id or not peer.is_open:
            continue
        found = (lanes.merge_conflicts(repo, item.sha, peer.sha)
                 if supported else None)
        facts.peers.append((peer, found))
    return integration_brief(item, facts)


# ---------------------------------------------------- reading the result ---

@dataclass
class Outcome:
    """Where an item goes next, from `read_outcome`, `approve_merge` or
    `mark_merged`."""
    state: str
    note: str = ""
    pr: int = 0
    pr_url: str = ""
    tested_sha: str = ""
    base_ref: str = ""
    reason: str = ""        # REASON_* for a NEEDS_YOU outcome
    suite_sha: str = ""     # the Tested-commit, for AWAITING


def _is_ancestor(repo: str, a: str, b: str) -> bool:
    return lanes.git(["merge-base", "--is-ancestor", a, b], repo).ok


def _have_commit(repo: str, sha: str) -> bool:
    return bool(sha) and lanes.git(["cat-file", "-e", f"{sha}^{{commit}}"],
                                   repo).ok


def _fetch_branch(repo: str, branch: str) -> None:
    """Best effort: the integrator's lane is a worktree of this same repo,
    so its commits are normally in the shared object store already."""
    lanes.git(["fetch", "--quiet", "--no-tags", "origin", branch], repo,
              lanes.FETCH_TIMEOUT)


def _needs(note: str, reason: str, pr=None, tested: str = "") -> Outcome:
    return Outcome(NEEDS_YOU, note, pr.number if pr else 0,
                   pr.url if pr else "", tested, reason=reason)


def pr_mismatch(repo: str, item: QueueItem, pr: PrInfo) -> str:
    """Why `pr` is not this item's pull request, or "": it must come from
    this repository (not a fork), from the item's own integrate branch, into
    the item's base. A PR number typed or swapped by hand fails the head
    check; a retargeted PR fails the base check."""
    base = item.base or lanes.default_base(repo)
    if pr.cross_repo:
        return f"Pull request #{pr.number} comes from a fork."
    if base and pr.base != base:
        return (f"Pull request #{pr.number} targets "
                f"{pr.base or 'an unknown branch'}, not {base}.")
    if pr.head_branch != item.integrate_branch:
        return (f"Pull request #{pr.number} is from "
                f"{pr.head_branch or 'an unknown branch'}, not "
                f"{item.integrate_branch}.")
    return ""


def tested_commit(body: str) -> str:
    """The sha on the PR body's last `Tested-commit:` line, or ""."""
    found = TESTED_RE.findall(body or "")
    return found[-1].lower() if found else ""


def _resolve(repo: str, ref: str) -> str:
    r = lanes.git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
                  repo)
    return r.out if r.ok else ""


def check_tested(repo: str, item: QueueItem, pr: PrInfo) -> tuple:
    """(Outcome or None, suite sha). None means the PR head is what the suite
    tested: the commit its Tested-commit line names, or that commit plus
    changes to README.md only (the test count, set from the result). The
    named commit must contain the submitted one."""
    named = tested_commit(pr.body)
    if not named:
        return _needs(f"Pull request #{pr.number} has no Tested-commit line, "
                      f"so AI Hive can't tell which commit the suite ran "
                      f"on.", REASON_UNTESTED, pr), ""
    suite = _resolve(repo, named)
    if not suite:
        _fetch_branch(repo, item.integrate_branch)
        suite = _resolve(repo, named)
    if not suite:
        return _needs(f"Pull request #{pr.number} says it was tested at "
                      f"{named[:7]}, a commit AI Hive can't find.",
                      REASON_UNTESTED, pr), ""
    if not _is_ancestor(repo, item.sha, suite):
        return _needs(f"The tested commit {suite[:7]} does not contain the "
                      f"submitted commit {item.short}.", REASON_UNTESTED,
                      pr), suite
    if suite != pr.head:
        if not _is_ancestor(repo, suite, pr.head):
            return _needs(f"Pull request #{pr.number}'s head is not built on "
                          f"its tested commit {suite[:7]}.", REASON_UNTESTED,
                          pr), suite
        r = lanes.git(["diff", "--name-only", "-z", suite, pr.head], repo)
        changed = [p for p in (r.out or "").split("\0") if p] if r.ok \
            else ["(unreadable)"]
        extra = [p for p in changed if p not in AFTER_TEST_OK]
        if extra:
            more = f" (+{len(extra) - 8} more)" if len(extra) > 8 else ""
            return _needs(f"Changed after the test run at {suite[:7]}: "
                          f"{', '.join(extra[:8])}{more}. The suite has to run "
                          f"again and Tested-commit name that commit.",
                          REASON_UNTESTED, pr), suite
    return None, suite


def read_outcome(repo: str, item: QueueItem) -> Outcome:
    """After the integrator's turn (or the user's Recheck): is there an open
    PR from the item's own integrate branch, into its base, that contains the
    pinned commit and whose head is what the suite tested? Yes: awaiting
    approval, with the PR head as the tested commit. Never MERGED: a PR
    merged outside Approve merge needs the user (Mark merged). Writes refs
    (a fetch), so it runs on LaneOps."""
    branch = item.integrate_branch
    pr = pr_view(repo, branch)
    if pr is None or not pr.number:
        return _needs(f"No pull request from {branch} yet.", REASON_NO_PR)
    if pr.state == "MERGED":
        return _needs(f"Pull request #{pr.number} was merged outside Approve "
                      f"merge. Check it, then Mark merged or Skip.",
                      REASON_MERGED_OUTSIDE, pr)
    if pr.state != "OPEN":
        return _needs(f"Pull request #{pr.number} is "
                      f"{pr.state.lower() or 'not open'}.", REASON_PR_STATE,
                      pr)
    wrong = pr_mismatch(repo, item, pr)
    if wrong:
        return _needs(wrong, REASON_WRONG_PR, pr)
    if not _have_commit(repo, pr.head):
        _fetch_branch(repo, branch)
    if not _have_commit(repo, pr.head):
        return _needs(f"Could not read the commits of pull request "
                      f"#{pr.number}.", REASON_PR_STATE, pr)
    if not _is_ancestor(repo, item.sha, pr.head):
        return _needs(f"Pull request #{pr.number} does not contain the "
                      f"submitted commit {item.short}.", REASON_WRONG_PR, pr)
    untested, suite = check_tested(repo, item, pr)
    if untested is not None:
        return untested
    return Outcome(AWAITING, "", pr.number, pr.url, pr.head, suite_sha=suite)


def approve_merge(repo: str, item: QueueItem) -> Outcome:
    """The user approved: merge the PR, guarded. Runs on LaneOps.

    1. The PR must still be this item's (its branch, its base, not a fork)
       and open; one merged meanwhile by someone else needs the user.
    2. The PR head must still be the tested commit; otherwise refuse.
    3. The base must be fetched, and GitHub must agree on where it is. A
       failed fetch would compare against a stale local copy.
    4. The base must not have moved since the test run (it must be an
       ancestor of the tested commit); otherwise the item goes back to the
       integrator (state INTEGRATING with `base_ref`, the caller re-briefs).
    5. `gh pr merge --merge --match-head-commit <tested>`: GitHub refuses
       too if the head moved in between.
    6. Merged only when GitHub then says MERGED.

    A window remains between step 3 and step 5 in which someone can push to
    the base; "Require branches to be up to date" branch protection closes
    it on GitHub's side."""
    pr = pr_view(repo, str(item.pr) if item.pr else item.integrate_branch)
    if pr is None or not pr.number:
        return _needs("Could not read the pull request.", REASON_PR_STATE)
    if pr.state == "MERGED":
        return _needs(f"Pull request #{pr.number} was merged outside Approve "
                      f"merge. Check it, then Mark merged or Skip.",
                      REASON_MERGED_OUTSIDE, pr, item.tested_sha)
    if pr.state != "OPEN":
        return _needs(f"Pull request #{pr.number} is "
                      f"{pr.state.lower() or 'not open'}.", REASON_PR_STATE,
                      pr, item.tested_sha)
    wrong = pr_mismatch(repo, item, pr)
    if wrong:
        return _needs(f"Not merged: {wrong}", REASON_WRONG_PR, pr,
                      item.tested_sha)
    if not item.tested_sha or pr.head != item.tested_sha:
        return _needs(f"Not merged: pull request #{pr.number} changed after "
                      f"it was tested (head {pr.head[:7]}, tested "
                      f"{item.tested_sha[:7] or 'nothing'}). Recheck once it "
                      f"is tested again.", REASON_HEAD_MOVED, pr,
                      item.tested_sha)
    base = item.base or lanes.default_base(repo)
    if not lanes.fetch_base(repo, base):
        return _needs(f"Not merged: could not fetch {base or 'the base'} "
                      f"from GitHub, so whether it moved since the test run "
                      f"is unknown.", REASON_BASE_UNKNOWN, pr,
                      item.tested_sha)
    base_ref = lanes.start_ref(repo, base)
    base_sha = _resolve(repo, base_ref) if base_ref else ""
    if not base_sha:
        return _needs(f"Not merged: could not read {base or 'the base branch'}.",
                      REASON_BASE_UNKNOWN, pr, item.tested_sha)
    remote = gh_base_sha(repo, base)
    if remote != base_sha:
        return _needs(f"Not merged: GitHub says {base} is at "
                      f"{remote[:7] or 'an unknown commit'}, the fetch saw "
                      f"{base_sha[:7]}. Recheck in a moment.",
                      REASON_BASE_UNKNOWN, pr, item.tested_sha)
    if not _have_commit(repo, item.tested_sha):
        _fetch_branch(repo, item.integrate_branch)
    if not _have_commit(repo, item.tested_sha):
        return _needs("Not merged: the tested commit is not available "
                      "locally.", REASON_PR_STATE, pr, item.tested_sha)
    if not _is_ancestor(repo, base_sha, item.tested_sha):
        return Outcome(INTEGRATING, f"{base_ref} moved after the test run, "
                                    f"so it went back to the integrator.",
                       pr.number, pr.url, item.tested_sha, base_ref)
    m = gh(["pr", "merge", str(pr.number), "--merge", "--match-head-commit",
            item.tested_sha], repo, MERGE_TIMEOUT)
    if not m.ok:
        return _needs("Not merged: gh refused: "
                      f"{(m.err or m.out or str(m.rc))[:300]}",
                      REASON_PR_STATE, pr, item.tested_sha)
    after = pr_view(repo, str(pr.number))
    if after is not None and after.state == "MERGED":
        return Outcome(MERGED, "", pr.number, pr.url, item.tested_sha)
    state = after.state.lower() if after is not None else "unreadable"
    return _needs(f"gh reported success, but pull request #{pr.number} is "
                  f"{state}. Recheck later.", REASON_PR_STATE, pr,
                  item.tested_sha)


def mark_merged(repo: str, item: QueueItem) -> Outcome:
    """The user's Mark merged, for an item whose pull request was merged
    outside Approve merge. Accepted only on GitHub's word and the item's own
    terms: the PR is MERGED, it is this item's (branch, base, not a fork),
    and its head contains the submitted commit. Runs on LaneOps."""
    pr = pr_view(repo, str(item.pr) if item.pr else item.integrate_branch)
    if pr is None or not pr.number:
        return _needs("Could not read the pull request.",
                      REASON_MERGED_OUTSIDE)
    if pr.state != "MERGED":
        return _needs(f"Pull request #{pr.number} is "
                      f"{pr.state.lower() or 'unreadable'}, not merged.",
                      REASON_PR_STATE, pr)
    wrong = pr_mismatch(repo, item, pr)
    if wrong:
        return _needs(wrong, REASON_WRONG_PR, pr)
    if not _have_commit(repo, pr.head):
        _fetch_branch(repo, item.integrate_branch)
    if not _have_commit(repo, pr.head) or not _is_ancestor(repo, item.sha,
                                                            pr.head):
        return _needs(f"Pull request #{pr.number} does not contain the "
                      f"submitted commit {item.short}, so it was not marked "
                      f"merged.", REASON_WRONG_PR, pr)
    return Outcome(MERGED, f"Marked merged by you (pull request "
                           f"#{pr.number}).", pr.number, pr.url, pr.head)


def remove_merged_lane(repo: str, lane: dict, item: QueueItem,
                       pids=(), wait_s: float | None = None):
    """The user's "Remove lane (merged as #N)". A lane whose work reached
    the base by a squash or rebase merge is never "merged" by ancestry, so
    every close keeps it. This removes it, only when GitHub confirms the
    item's PR is MERGED (its own branch and base, not a fork, containing the
    pinned commit) and the lane is clean with its head still at that commit.
    Then the usual removal rules: links first, no --force, the branch by
    compare-and-delete. Raises lanes.LaneError. Runs on LaneOps."""
    pr = pr_view(repo, str(item.pr) if item.pr else item.integrate_branch)
    if pr is None or pr.state != "MERGED":
        raise lanes.LaneError("unmerged", f"pull request #{item.pr} is not "
                                          f"merged on GitHub")
    wrong = pr_mismatch(repo, item, pr)
    if wrong:
        raise lanes.LaneError("unmerged", wrong)
    if not _have_commit(repo, pr.head):
        _fetch_branch(repo, item.integrate_branch)
    if not _is_ancestor(repo, item.sha, pr.head):
        raise lanes.LaneError("unmerged", f"pull request #{pr.number} does "
                                          f"not contain {item.short}")
    if not lanes.wait_for_exit(pids, lanes.EXIT_WAIT_S if wait_s is None
                               else wait_s):
        raise lanes.LaneError("busy", "a program is still running in it")
    return lanes.remove_lane(lane, merged_as=item.sha)


def lane_head(lane: dict) -> tuple:
    """(head sha, commits ahead of the base) of a lane, for a submit.
    Read-only. Raises lanes.LaneError when the lane can't be read."""
    st = lanes.lane_status(lane)
    if not st.exists or not st.head:
        raise lanes.LaneError("missing", f"the lane folder {lane['root']} is "
                                         f"gone")
    return st.head, st.ahead
