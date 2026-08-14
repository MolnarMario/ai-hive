"""Startup CLI auto-update gate: the decisions, with no I/O of its own.

Qt-free and stdlib-only, the same class of module as `chime.py`,
`limit_banner.py`, `taskbar_overlay.py` and `screen_snapshot.py`: nothing here
imports PySide6, nothing here raises, and every decision is pure given an
injected `Runner`. The real subprocess runner (`subprocess_runner`) is only
ever handed in from `main.py`, exactly like `start_usage_polling()` is opted
into there: the offscreen smoke suite shares `create_main_window` and must
never upgrade the user's CLI mid-run.

WHY THE GATE RUNS BEFORE THE WINDOW, AND WHY WINGET IS NEVER BELIEVED
---------------------------------------------------------------------
`claude.exe` is a single self-contained binary, and Windows cannot overwrite a
running `.exe`. Every AI Hive agent IS a `claude.exe` child held alive by the
Job Object, so an upgrade run with agents up cannot replace the file -- but
winget records the new version in its own database anyway. From that moment
three things all lie in the same direction: the file on disk is still old (so
its baked-in model table has no newer alias to resolve, the stale-CLI symptom
CLAUDE.md documents), that old binary keeps printing its own "Update
available!" banner forever, and winget answers "No available upgrade found"
because by its books the job is done.

Two consequences shape everything below:

1. The startup window is not merely a convenient moment, it is the ONLY moment
   the binary is unlocked. Hence the gate runs above `create_main_window()` and
   far above `autostart_active_workspace()`, so no agent can hold a file.
2. Winget's report can never be treated as evidence. The installed version is
   read from the FILE (`<exe> --version`, measured 0.09s) before and after, and
   winget is asked only what the MANIFEST offers (`winget show`). A poisoned
   database cannot hide an update from us because we never read its
   installed-version record.

And the direct repair for how the database got poisoned in the first place: if
ANY process is holding that target's binary we skip it without running an
upgrade command at all. Those processes are the user's own sessions or another
app's, outside our Job Object, so they are never killed -- killing one can
destroy a transcript. "Holding it" is decided by PATH and not by image name:
`Claude.exe` is both the CLI and the unrelated desktop app, and counting the
latter made the gate skip every launch forever (see `count_processes`).

THE TWO CLIs DIFFER, AND THE DIFFERENCE IS STRUCTURAL
-----------------------------------------------------
A WINGET Claude Code is a package, so checking (`winget show`, a pure read) and
installing (`winget upgrade`) are separate acts and the check can decide
whether to install at all. A NATIVE Claude Code is not (see `_claude_target`):
it self-updates, so it takes the second shape below, and `Status.DB_STALE`
cannot arise for it at all. `agy` is not a winget package, it self-updates, and
neither `agy update` nor `claude update` accepts any flags: there is NO dry
run, so for a self-updating target checking and installing are the SAME act
(measured 0.37s and no mutation when already current). That is why
`needs_apply` says yes unconditionally for a `self_update` target and why an
unchanged version after a self-update reads as UP_TO_DATE rather than as the
REPORTED_BUT_UNCHANGED that the same reading means for winget.

THAT DIFFERENCE REACHES THE WORDS, NOT JUST THE COMMANDS
--------------------------------------------------------
A skipped check means opposite things in the two shapes, so `Outcome` carries
`self_updating` and the reporting surfaces read it. On a WINGET target, a check
that timed out or found the file locked is a genuinely missed update: nothing
else will fetch one, and the user keeps the old binary and its banner. On a
SELF-UPDATING target the CLI fetches its own new version in the background
regardless, so the same statuses are non events -- `_SELF_UPDATING_TEXT`
rewords them ("left to its own updater"), `needs_pill` withholds the top-bar
nag whose advice would not apply, and `worth_reading` does not hold the splash
open for them. This was a live report: after the native migration the splash
flashed "took too long, skipped" for under a second on a launch where nothing
was wrong and the CLI updated itself a minute later, with no surface left
afterwards that could say so. Hence `last_check_summary`, which unlike
`pill_text` lets EVERY outcome speak, for the Updates panel the user can open
on purpose. The audit lines record the shape for the same reason: which of the
two a line describes is read off the install, so it can change between runs on
one machine.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from enum import Enum

# argv, timeout seconds -> (rc, combined output). A Runner NEVER raises; a
# command it had to kill comes back as RC_TIMEOUT so the contract stays total
# (the same "never raises" rule the rest of these stdlib modules follow).
# Callable[[list, float], tuple] -- spelled out rather than typed, so this
# module needs no typing import at all.
RC_TIMEOUT = -1

# The whole CHECK phase for one target is bounded, and fails OPEN to launch: a
# hung network must never cost the user the app. The INSTALL phase deliberately
# has no such bound -- a 285 MB binary killed mid-write is worse than the nag
# banner, so the splash's Skip button is the escape hatch instead of a timer.
CHECK_TIMEOUT_S = 5.0
INSTALL_TIMEOUT_S = 3600.0

# count_processes could not answer. Treated exactly like "processes are alive":
# we would rather skip an update than run an upgrade against a file we cannot
# prove is unlocked, since that is what writes the false database record.
UNKNOWN_PROCESSES = -1

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?")
_VERSION_LINE_RE = re.compile(r"^\s*Version\s*:\s*(.+)$", re.I | re.M)
# winget's way of saying it believes there is nothing to do. Paired with a
# binary that is demonstrably behind the manifest, this is the poisoned-database
# signature (see Status.DB_STALE).
_NO_UPGRADE_RE = re.compile(r"no\s+(?:available\s+)?upgrade\s+found", re.I)

WINGET = "winget"
CLAUDE_WINGET_ID = "Anthropic.ClaudeCode"

# how each target reads to the user. Kept here rather than only on the Target,
# because the pill has to name a target whose Outcome never arrived: an install
# the user skipped mid-flight is exactly the case with no outcome to read a
# label from.
LABELS = {"claude": "Claude Code", "gemini": "Gemini (agy)"}


class Status(str, Enum):
    """What happened to one target. `str` mixin so a status survives being
    logged or compared against a plain string."""

    UP_TO_DATE = "up-to-date"
    UPDATED = "updated"
    BLOCKED_PROCESSES = "blocked-processes"
    DB_STALE = "db-stale"
    REPORTED_BUT_UNCHANGED = "reported-but-unchanged"
    FAILED = "failed"
    TIMEOUT = "timeout"
    NOT_INSTALLED = "not-installed"
    DISABLED = "disabled"


# The statuses that earn the quiet top-bar pill. UPDATED/UP_TO_DATE/TIMEOUT/
# NOT_INSTALLED/DISABLED say nothing: either it worked, or there was nothing to
# do, or failing open to launch is the whole point and a nag would be noise.
PILL_STATUSES = (Status.BLOCKED_PROCESSES, Status.DB_STALE,
                 Status.REPORTED_BUT_UNCHANGED, Status.FAILED)


@dataclass(frozen=True)
class Target:
    """One updatable CLI.

    `exe` is the RESOLVED binary (`providers.resolve_claude()` /
    `resolve_program`), never a bare name: it is the file whose `--version` is
    the only evidence this module accepts, and it must be the same file the
    agents will launch. Empty means not installed, which is skipped silently.

    Exactly one of `winget_id` / `self_update` is set. `process_names` are the
    images to LOOK for, not the identity: which of them actually lock this
    binary is decided against `exe` by path (see `count_processes`), because an
    image name is shared by unrelated programs.
    """

    key: str
    label: str
    exe: str
    process_names: tuple = ()
    winget_id: str | None = None     # winget-managed (Claude Code)
    self_update: tuple | None = None  # e.g. ("update",) for `agy update`


@dataclass(frozen=True)
class Outcome:
    """The result for one target. Purely transient: nothing here is ever
    persisted (the same rule the plan-usage reading obeys), because a stored
    version goes stale the moment anything installs anything."""

    target: str
    status: Status
    before: str = ""      # version read off the FILE before we touched it
    after: str = ""       # ...and after
    available: str = ""   # what the manifest offers (winget targets only)
    detail: str = ""
    label: str = ""
    # Whether this target updates ITSELF (`Target.self_update`). Carried on the
    # outcome rather than looked up from the Target, because what a status MEANS
    # to the user depends on it and the reporting surfaces (splash, panel, pill)
    # only ever see outcomes. A skipped check is a missed update on a package
    # managed install and a non event on a self-updating one, and telling the
    # user the same sentence for both is how a harmless launch reads as a fault.
    self_updating: bool = False


@dataclass(frozen=True)
class GateResult:
    """What the gate did, handed to `MainWindow.note_update_outcomes`.

    `installing` carries the target keys whose INSTALL was still running when
    the user pressed Skip. An install that is still writing a multi-hundred-MB
    binary while agents autostart could hand a child a half-written file, so
    those providers are held out of the autostart and the pill says so."""

    outcomes: tuple = ()
    installing: tuple = ()


# ------------------------------------------------------------- versions ---

def parse_version(text) -> str:
    """The first `N.N.N` in `text`, or "" when there is none.

    Serves all three shapes this module reads: `2.1.224 (Claude Code)`,
    a bare `1.1.11`, and winget's `Version: 2.1.224` line. A `Version:` line
    wins when present, because `winget show` also prints URLs and descriptions
    that can carry version-shaped numbers of their own."""
    if not isinstance(text, str) or not text:
        return ""
    line = _VERSION_LINE_RE.search(text)
    if line:
        found = _VERSION_RE.search(line.group(1))
        if found:
            return found.group(0)
    found = _VERSION_RE.search(text)
    return found.group(0) if found else ""


def version_tuple(s) -> tuple:
    """`"2.1.224"` -> `(2, 1, 224)`. An unparseable version is `()`, which
    `needs_update` treats as "do not install": a parse failure must never be
    the reason software gets replaced."""
    if not isinstance(s, str):
        return ()
    found = _VERSION_RE.search(s)
    if not found:
        return ()
    return tuple(int(part) for part in found.groups() if part is not None)


def needs_update(installed: str, available: str) -> bool:
    """Is the FILE genuinely behind the MANIFEST? Fails open in both
    directions: if either side could not be parsed the answer is no."""
    left, right = version_tuple(installed), version_tuple(available)
    if not left or not right:
        return False
    return left < right


# ------------------------------------------------------------ processes ---

def tasklist_argv(name: str) -> list:
    return ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"]


def process_paths_argv(name: str) -> list:
    """List the FULL PATH of every running process with this image name.

    An image name is not an identity: `Claude.exe` is BOTH the Claude Code CLI
    and the unrelated Claude desktop app, which a user keeps open all day. That
    collision made the gate count eight desktop windows as a locked CLI and skip
    the update on every single launch, forever (found live: `UPDATE-SKIP claude
    (8 claude.EXE alive)` three seconds BEFORE AI Hive started an agent of its
    own, against a desktop app running since morning). Only the path can tell
    the two apart, and `tasklist` cannot report one.

    A process whose path cannot be read prints `?` rather than an empty line, so
    "unreadable" stays distinguishable from "no output" -- it must still count
    as blocking, and a blank line could not carry that."""
    return ["powershell", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='%s'\" | "
            "ForEach-Object { if ($_.ExecutablePath) "
            "{ $_.ExecutablePath } else { '?' } }" % name]


def count_processes(names, runner, timeout: float = CHECK_TIMEOUT_S,
                    exe: str = "") -> int:
    """How many processes are holding `exe` open. `UNKNOWN_PROCESSES` when that
    could not be answered.

    Two passes, cheapest first. `tasklist` (stdlib only, no psutil) answers "is
    anything by this name running at all", which is the common case and costs
    almost nothing; only when it says yes does a second, path-listing command
    run (measured 0.40s) to decide how many of those are actually OUR binary.
    So the usual launch pays one cheap command, and the expensive one is spent
    exactly when there is an ambiguity worth resolving.

    Every fallback leans the same way, towards over-counting: an unreadable
    path, a path query that fails, or a `tasklist` that cannot answer all end as
    "assume it is ours". Over-counting costs a skipped update, which the pill
    reports; under-counting runs an installer against a locked file, which is
    what writes the false database record this whole module exists to avoid.

    At gate time AI Hive has started no agents, so every hit is foreign by
    construction: another window of the user's, or another app's."""
    total = 0
    for name in names or ():
        rc, out = runner(tasklist_argv(name), timeout)
        if rc != 0:
            return UNKNOWN_PROCESSES
        lowered = name.lower()
        hits = 0
        for line in (out or "").splitlines():
            head = line.strip().split(" ")[0].strip()
            if head.lower() == lowered:
                hits += 1
        if hits and exe:
            refined = _count_by_path(name, exe, runner, timeout)
            if refined is not None:
                hits = refined
        total += hits
    return total


def _count_by_path(name: str, exe: str, runner, timeout: float):
    """How many `name` processes are running THIS file, or None when the
    question could not be answered (the caller then keeps the name count).

    None is also the answer when the listing comes back empty while `tasklist`
    saw hits: the two disagreeing means the path pass cannot see what the name
    pass can, and believing the smaller number is the unsafe direction."""
    rc, out = runner(process_paths_argv(name), timeout)
    if rc != 0:
        return None
    lines = [line.strip() for line in (out or "").splitlines() if line.strip()]
    if not lines:
        return None
    mine = _canonical(exe)
    return sum(1 for line in lines
               if line == "?" or _canonical(line) == mine)


def _canonical(path: str) -> str:
    """A path reduced to something two spellings of the same file agree on.
    `realpath` matters rather than just `normcase`: winget also installs a
    symlink shim (`WinGet\\Links\\claude.exe`) and a process launched through it
    reports the LINK, so a plain string compare would read the user's own
    terminal session as somebody else's program."""
    try:
        return os.path.normcase(os.path.realpath(path))
    except (OSError, ValueError):
        return os.path.normcase(path or "")


def _blocked_detail(names, alive: int) -> str:
    if alive == UNKNOWN_PROCESSES:
        return "could not tell whether the CLI was in use"
    first = names[0] if names else "process"
    return f"{alive} {first} alive"


# ---------------------------------------------------------------- argv ---

def check_argv(target: Target) -> list:
    """The pure READ that asks what the manifest offers. `winget show`, never
    `winget upgrade`: the latter mutates, and running it against a locked file
    is what poisons the database in the first place. The source-agreement
    acceptance is not optional -- an unattended prompt on a first run would
    park the splash forever."""
    if not target.winget_id:
        return []
    return [WINGET, "show", "--id", target.winget_id, "--exact",
            "--accept-source-agreements"]


def upgrade_argv(target: Target) -> list:
    """The command that actually replaces the binary."""
    if target.self_update:
        return [target.exe, *target.self_update]
    if target.winget_id:
        return [WINGET, "upgrade", "--id", target.winget_id, "--exact",
                "--accept-source-agreements", "--accept-package-agreements"]
    return []


def version_argv(target: Target) -> list:
    return [target.exe, "--version"]


# --------------------------------------------------------------- phases ---

def check(target: Target, runner, budget: float = CHECK_TIMEOUT_S,
          clock=time.monotonic) -> Outcome:
    """The CHECK phase: reads only, mutates nothing.

    Returns UP_TO_DATE both when the target is provably current AND when the
    check simply could not decide yet (a self-updating CLI has no dry run) --
    `needs_apply` is the one place that turns a finished check into an install,
    so those two cases never have to be told apart by a status."""
    out = Outcome(target.key, Status.UP_TO_DATE, label=target.label,
                  self_updating=bool(target.self_update))
    if not target.exe:
        return replace(out, status=Status.NOT_INSTALLED, detail="not installed")

    deadline = clock() + max(0.0, budget)

    def left() -> float:
        return max(0.1, deadline - clock())

    # process check FIRST: a blocked target must not issue any other command,
    # so there is no path on which a locked file reaches winget
    alive = count_processes(target.process_names, runner, left(),
                            exe=target.exe)
    if alive != 0:
        return replace(out, status=Status.BLOCKED_PROCESSES,
                       detail=_blocked_detail(target.process_names, alive))

    if clock() >= deadline:
        return replace(out, status=Status.TIMEOUT, detail=_timeout_detail(budget))
    rc, text = runner(version_argv(target), left())
    if rc == RC_TIMEOUT:
        return replace(out, status=Status.TIMEOUT, detail=_timeout_detail(budget))
    if rc != 0:
        return replace(out, status=Status.FAILED, detail=_fail_detail(rc, text))
    before = parse_version(text)
    out = replace(out, before=before, after=before)

    if not target.winget_id:
        return out  # self-updating: the install IS the check (no dry run)

    if clock() >= deadline:
        return replace(out, status=Status.TIMEOUT, detail=_timeout_detail(budget))
    rc, text = runner(check_argv(target), left())
    if rc == RC_TIMEOUT:
        return replace(out, status=Status.TIMEOUT, detail=_timeout_detail(budget))
    if rc != 0:
        return replace(out, status=Status.FAILED, detail=_fail_detail(rc, text))
    return replace(out, available=parse_version(text))


def needs_apply(target: Target, outcome: Outcome) -> bool:
    """Should the INSTALL phase run? Only a clean check continues."""
    if outcome.status is not Status.UP_TO_DATE:
        return False
    if target.self_update:
        return True   # no dry run exists: checking and installing are one act
    return needs_update(outcome.before, outcome.available)


def apply(target: Target, outcome: Outcome, runner,
          timeout: float = INSTALL_TIMEOUT_S) -> Outcome:
    """The INSTALL phase, and then the only question that matters: did the FILE
    change? Success is decided by `--version` on the resolved binary, never by
    what the installer said about itself."""
    argv = upgrade_argv(target)
    if not argv:
        return outcome
    rc, text = runner(argv, timeout)

    vrc, vtext = runner(version_argv(target), CHECK_TIMEOUT_S)
    after = parse_version(vtext) if vrc == 0 else ""
    moved = bool(after) and version_tuple(after) != version_tuple(outcome.before)
    if moved:
        return replace(outcome, status=Status.UPDATED, after=after)

    out = replace(outcome, after=after or outcome.before)
    if target.self_update:
        # a self-updating CLI that reports success and is unchanged was simply
        # already current -- that is its whole "nothing to do" answer
        if rc == 0:
            return replace(out, status=Status.UP_TO_DATE)
        return replace(out, status=Status.FAILED, detail=_fail_detail(rc, text))

    # the poisoned-database signature: the file is behind the manifest and
    # winget insists there is nothing to upgrade. Checked BEFORE the return
    # code, because winget reports "nothing to do" with a non-zero rc.
    if _NO_UPGRADE_RE.search(text or "") and \
            needs_update(out.after, outcome.available):
        return replace(out, status=Status.DB_STALE,
                       detail=_stale_detail(out.after, outcome.available))
    if rc != 0:
        return replace(out, status=Status.FAILED, detail=_fail_detail(rc, text))
    return replace(out, status=Status.REPORTED_BUT_UNCHANGED,
                   detail=f"binary still {out.after or 'unreadable'}")


def run_gate(targets, runner, enabled: bool = True, on_event=None,
             budget: float = CHECK_TIMEOUT_S,
             install_timeout: float = INSTALL_TIMEOUT_S) -> list:
    """Check then, if needed, install every target SERIALLY.

    Serial is not stylistic: two multi-hundred-MB installers writing at once is
    a bad day, and one target at a time is what lets the splash name what is
    happening. `on_event(kind, payload)` reports progress to the splash and the
    audit trail: "start"/"install" carry the Target, "outcome" an Outcome,
    "audit" one `session.log` line."""
    results = []
    for target in targets or ():
        if not enabled:
            results.append(Outcome(target.key, Status.DISABLED,
                                   label=target.label))
            continue
        _emit(on_event, "start", target)
        outcome = check(target, runner, budget)
        if needs_apply(target, outcome):
            _emit(on_event, "install", target)
            outcome = apply(target, outcome, runner, install_timeout)
        results.append(outcome)
        _emit(on_event, "outcome", outcome)
        for line in audit_lines(outcome):
            _emit(on_event, "audit", line)
    return results


def _emit(on_event, kind: str, payload) -> None:
    if on_event is None:
        return
    try:
        on_event(kind, payload)
    except Exception:  # noqa: BLE001 - progress reporting must never break the gate
        pass


# ---------------------------------------------------------------- audit ---

def audit_lines(outcome: Outcome) -> list:
    """The `session.log` lines for one outcome.

    Every version transition is recorded and there is deliberately NO
    patch-versus-minor gate anywhere in this module: nothing in a CLI's
    numbering predicts whether a flag moved, so a version gate would buy false
    safety. An audit line is what turns "it broke this morning" into a lookup.
    """
    key = outcome.target
    lines = []
    if outcome.status is Status.DISABLED:
        return lines
    if outcome.before:
        lines.append(f"UPDATE-CHECK {key} installed={outcome.before}"
                     + (f" available={outcome.available}"
                        if outcome.available else ""))
    if outcome.status is Status.UPDATED:
        lines.append(f"UPDATE {key} {outcome.before or '?'} -> {outcome.after}")
    elif outcome.status is Status.BLOCKED_PROCESSES:
        lines.append(f"UPDATE-SKIP {key} ({outcome.detail})"
                     + (" (self-updating, left to the CLI)"
                        if outcome.self_updating else ""))
    elif outcome.status is Status.NOT_INSTALLED:
        lines.append(f"UPDATE-SKIP {key} (not installed)")
    elif outcome.status is Status.DB_STALE:
        lines.append(f"UPDATE-STALE {key} {outcome.detail}")
    elif outcome.status is Status.REPORTED_BUT_UNCHANGED:
        lines.append(f"UPDATE-UNCHANGED {key} reported success, "
                     f"{outcome.detail}")
    elif outcome.status is Status.FAILED:
        lines.append(f"UPDATE-FAIL {key} {outcome.detail}")
    elif outcome.status is Status.TIMEOUT:
        # the shape is recorded because it is what decides whether this line
        # describes a missed update or a non event, and the shape is read off
        # the install, so it can differ between two runs on the same machine
        lines.append(f"UPDATE-TIMEOUT {key} {outcome.detail}"
                     + (" (self-updating, left to the CLI)"
                        if outcome.self_updating else ""))
    return lines


def _fail_detail(rc: int, text) -> str:
    first = ""
    for line in (text or "").splitlines():
        if line.strip():
            first = line.strip()[:160]
            break
    return f"rc={rc} {first}".strip()


def _stale_detail(installed: str, available: str) -> str:
    return (f"winget reported no upgrade but binary "
            f"{installed or 'unreadable'} < {available}")


def _timeout_detail(budget: float) -> str:
    return f"check exceeded {budget:g}s"


# ----------------------------------------------------------------- pill ---
# Quiet, never a chime: this answers "why am I still seeing the nag?" without
# interrupting, the same principle as the usage badge's can't-read pill. No em
# dash anywhere below -- these strings reach the user.

# Kept SHORT on purpose: this sits in a top bar that already carries two usage
# readouts, so the pill states the fact and the tooltip carries the reason.
_PILL_SHORT = {
    Status.BLOCKED_PROCESSES: "{label} update pending",
    Status.DB_STALE: "{label} update needs a manual step",
    Status.REPORTED_BUT_UNCHANGED: "{label} update did not apply",
    Status.FAILED: "{label} update check failed",
}

_PILL_LONG = {
    Status.BLOCKED_PROCESSES:
        "{label}: {detail}, and Windows cannot replace a running program, so "
        "the update was skipped without asking the installer to try (asking "
        "while the file is locked is what makes the installer believe it "
        "already updated). Close those programs and restart AI Hive.",
    Status.DB_STALE:
        "{label}: the installed file is {before} but the package source "
        "offers {available}, and the installer still reports nothing to do, "
        "so its own record is ahead of the file. Run this by hand once, with "
        "AI Hive closed:\n    {command}",
    Status.REPORTED_BUT_UNCHANGED:
        "{label}: the installer reported success but the program on disk is "
        "still {after}. Nothing was replaced, so you keep the old version and "
        "its update banner.",
    Status.FAILED:
        "{label}: the update check could not complete ({detail}). AI Hive "
        "started normally and nothing was changed.",
}

_MANUAL_COMMAND = ("winget install --id {winget_id} --exact --force")


# --------------------------------------------------------- outcome words ---
# One short phrase per finished target, shown on the splash row and reused
# verbatim by the Updates panel. It lives HERE rather than in the widget for the
# same reason every other decision does: the panel and the splash must not be
# able to describe the same outcome differently.

_STATE_TEXT = {
    Status.UP_TO_DATE: "up to date",
    Status.UPDATED: "updated",
    Status.BLOCKED_PROCESSES: "skipped, the CLI was in use",
    Status.DB_STALE: "needs a manual reinstall",
    Status.REPORTED_BUT_UNCHANGED: "unchanged, see the top bar",
    Status.FAILED: "check failed",
    Status.TIMEOUT: "took too long, skipped",
    Status.NOT_INSTALLED: "not installed",
    Status.DISABLED: "off",
}

# A self-updating CLI that we did not manage to check is NOT a missed update:
# it fetches its own new versions in the background, so the gate standing down
# changes nothing about what the user ends up running. Saying "skipped" there
# reports a failure that did not happen, and leaves the user looking for
# something to fix.
_SELF_UPDATING_TEXT = {
    Status.TIMEOUT: "left to its own updater",
    Status.BLOCKED_PROCESSES: "in use, left to its own updater",
}


def state_text(outcome) -> str:
    """One short phrase per finished target. Never an em dash: this is read."""
    if outcome.self_updating and outcome.status in _SELF_UPDATING_TEXT:
        return _SELF_UPDATING_TEXT[outcome.status]
    base = _STATE_TEXT.get(outcome.status, str(outcome.status))
    if outcome.status is Status.UPDATED:
        return f"updated {outcome.before or '?'} to {outcome.after}"
    if outcome.status is Status.UP_TO_DATE and outcome.before:
        return f"up to date ({outcome.before})"
    return base


def worth_reading(outcomes) -> bool:
    """Is there an outcome here the user may want a moment to actually READ?

    The splash auto closes in well under a second, which is right for "nothing
    to do" and wrong for anything that might send someone looking for a
    problem. This is deliberately NARROWER than "not a clean run": a timed out
    check on a self-updating CLI is a genuine non event (see
    `_SELF_UPDATING_TEXT`), so holding the window open for it would manufacture
    the very concern the reworded phrase removes."""
    for outcome in outcomes or ():
        if needs_pill(outcome):
            return True
        if outcome.status is Status.TIMEOUT and not outcome.self_updating:
            return True
    return False


def last_check_summary(outcomes, installing=()) -> str:
    """What the startup gate did, for the Updates panel.

    Unlike `pill_text`, EVERY outcome speaks here. The pill is an interruption
    and so only reports what needs acting on; this is a place the user chose to
    open, and the whole point of it is that a message which flashed past on the
    splash can be read again afterwards. A status with nowhere to be re-read is
    indistinguishable from one the app never produced."""
    parts = []
    for outcome in outcomes or ():
        if outcome.status is Status.DISABLED:
            continue
        parts.append(f"{_label(outcome)}: {state_text(outcome)}")
    if installing:
        parts.append(_joined(_label_for_key(key, outcomes)
                             for key in installing) + ": still installing")
    return "; ".join(parts)


def needs_pill(outcome) -> bool:
    """Does this outcome earn the top-bar nag?

    `PILL_STATUSES` says which statuses can, and `self_updating` is the veto:
    every line of `_PILL_LONG` tells the user to go and DO something (close
    programs, run an installer by hand), and on a self-updating install none of
    that applies -- the CLI fetches its own new version whatever the gate did.
    A nag whose instructions are unnecessary is worse than silence, and it
    would also contradict the splash, which now calls the same outcome a non
    event. One predicate so the two surfaces cannot disagree."""
    if outcome.status not in PILL_STATUSES:
        return False
    return not (outcome.self_updating and outcome.status in _SELF_UPDATING_TEXT)


def pill_text(outcomes, installing=()) -> str:
    """One short line for the top-bar pill, or "" when there is nothing to
    say. Only the reporting statuses speak, and only where the user can act."""
    parts = []
    for outcome in outcomes or ():
        if needs_pill(outcome):
            parts.append(_PILL_SHORT[outcome.status].format(
                label=_label(outcome)))
    if installing:
        parts.append(_joined(_label_for_key(key, outcomes)
                             for key in installing) + " agents held back")
    if not parts:
        return ""
    return "↓ " + "; ".join(parts)


def pill_tooltip(outcomes, installing=()) -> str:
    """The long form, including the one command to run by hand for a stale
    package record. Kept out of the widget so it can be tested without Qt."""
    blocks = []
    for outcome in outcomes or ():
        if not needs_pill(outcome):
            continue
        blocks.append(_PILL_LONG[outcome.status].format(
            label=_label(outcome), detail=outcome.detail or "no detail",
            before=outcome.before or "unreadable",
            after=outcome.after or "unreadable",
            available=outcome.available or "a newer version",
            command=_MANUAL_COMMAND.format(
                winget_id=_winget_id_for(outcome))))
    if installing:
        blocks.append(
            "An update was still installing when you skipped, so "
            + _joined(_label_for_key(key, outcomes) for key in installing)
            + " agents have not been started. Starting one now could run a "
            "half written program. Restart AI Hive once the install has "
            "finished, or start those agents yourself.")
    return "\n\n".join(blocks)


def _label(outcome: Outcome) -> str:
    return outcome.label or outcome.target


def _label_for_key(key: str, outcomes) -> str:
    for outcome in outcomes or ():
        if outcome.target == key and outcome.label:
            return outcome.label
    return LABELS.get(key, key)


def _winget_id_for(outcome: Outcome) -> str:
    return CLAUDE_WINGET_ID if outcome.target == "claude" else outcome.target


def _joined(items) -> str:
    items = [str(i) for i in items]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


# -------------------------------------------------------------- targets ---

def _claude_target(exe: str) -> Target:
    """Which updater owns this binary.

    A native install self-updates (`claude update`); a winget package does not,
    and asking `claude update` to replace a winget-managed file is how you end
    up with two installs. Decided on the PATH because nothing else
    distinguishes them: the binary, the version string and the process name are
    identical.

    Nothing downstream changes for the native case: `needs_apply` already
    returns True unconditionally for a `self_update` target, `upgrade_argv`
    already yields `[exe, "update"]`, and `apply` already reads an unchanged
    version after a clean self-update as UP_TO_DATE rather than the
    REPORTED_BUT_UNCHANGED that the same reading means for winget. One real
    consequence: `Status.DB_STALE` becomes structurally UNREACHABLE for Claude
    on a native install, because there is no package database to go stale,
    which retires the one status whose only remedy was a manual command.

    No flag day either: the same build serves a winget machine and a native
    one, so rolling the migration back needs no code revert.
    """
    from . import cli_install   # local: cli_install imports this module

    common = dict(key="claude", label=LABELS["claude"], exe=exe,
                  process_names=(_image_name(exe, "claude.exe"),))
    if cli_install.classify_install(exe) is cli_install.InstallKind.WINGET:
        return Target(**common, winget_id=CLAUDE_WINGET_ID)
    return Target(**common, self_update=("update",))


def default_targets() -> list:
    """The two updatable CLIs, resolved to the SAME binaries the agents launch.

    How Claude Code is updated now depends on how it is INSTALLED (see
    `_claude_target`): a winget package is upgraded through winget, a native
    install updates itself. `agy` is not a winget package at all
    (Google.AntigravityIDE is the separate IDE), so it self-updates.
    """
    from . import providers

    targets = []
    claude = providers.resolve_claude() if providers.detected("claude") else ""
    if claude:
        targets.append(_claude_target(claude))
    gemini = providers.resolve_program("gemini") if \
        providers.detected("gemini") else ""
    # only `agy` self-updates. The provider also answers to a plain `gemini`
    # binary, which knows no `update` subcommand, so it gets no target rather
    # than a guessed command.
    if gemini and os.path.basename(gemini).lower().startswith("agy"):
        targets.append(Target(
            key="gemini", label=LABELS["gemini"], exe=gemini,
            process_names=(_image_name(gemini, "agy.exe"),),
            self_update=("update",)))
    return targets


def _image_name(exe: str, fallback: str) -> str:
    name = os.path.basename(exe or "")
    if not name:
        return fallback
    return name if name.lower().endswith(".exe") else name + ".exe"


def subprocess_runner(argv, timeout: float = CHECK_TIMEOUT_S) -> tuple:
    """The one place this module touches the machine, and it is never called
    from the smoke suite. Never raises, never opens a console window, and
    gives every child a closed stdin so a prompt we did not anticipate EOFs
    instead of parking the splash forever."""
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW",
                                          0x08000000)
    try:
        res = subprocess.run(
            list(argv), capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL,
            timeout=(timeout if timeout and timeout > 0 else None), **kwargs)
    except subprocess.TimeoutExpired:
        return RC_TIMEOUT, ""
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return 127, str(e)
    return res.returncode, (res.stdout or "") + (res.stderr or "")
