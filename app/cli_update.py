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
ANY target process is alive we skip that target without running an upgrade
command at all. Those processes are the user's own sessions or another app's,
outside our Job Object, so they are never killed -- killing one can destroy a
transcript.

THE TWO CLIs DIFFER, AND THE DIFFERENCE IS STRUCTURAL
-----------------------------------------------------
Claude Code is a winget package, so checking (`winget show`, a pure read) and
installing (`winget upgrade`) are separate acts and the check can decide
whether to install at all. `agy` is not a winget package, it self-updates, and
neither `agy update` nor `claude update` accepts any flags: there is NO dry
run, so for a self-updating target checking and installing are the SAME act
(measured 0.37s and no mutation when already current). That is why
`needs_apply` says yes unconditionally for a `self_update` target and why an
unchanged version after a self-update reads as UP_TO_DATE rather than as the
REPORTED_BUT_UNCHANGED that the same reading means for winget.
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
    images whose presence locks the binary.
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

def count_processes(names, runner, timeout: float = CHECK_TIMEOUT_S) -> int:
    """How many of `names` are running, via `tasklist` (stdlib only, no
    psutil). `UNKNOWN_PROCESSES` when tasklist could not answer.

    At gate time AI Hive has started no agents, so every hit is foreign by
    construction: another window of the user's, or another app's."""
    total = 0
    for name in names or ():
        rc, out = runner([
            "tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], timeout)
        if rc != 0:
            return UNKNOWN_PROCESSES
        lowered = name.lower()
        for line in (out or "").splitlines():
            head = line.strip().split(" ")[0].strip()
            if head.lower() == lowered:
                total += 1
    return total


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
    out = Outcome(target.key, Status.UP_TO_DATE, label=target.label)
    if not target.exe:
        return replace(out, status=Status.NOT_INSTALLED, detail="not installed")

    deadline = clock() + max(0.0, budget)

    def left() -> float:
        return max(0.1, deadline - clock())

    # process check FIRST: a blocked target must not issue any other command,
    # so there is no path on which a locked file reaches winget
    alive = count_processes(target.process_names, runner, left())
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
        lines.append(f"UPDATE-SKIP {key} ({outcome.detail})")
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
        lines.append(f"UPDATE-TIMEOUT {key} {outcome.detail}")
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


def pill_text(outcomes, installing=()) -> str:
    """One short line for the top-bar pill, or "" when there is nothing to
    say. Only the four reporting statuses speak."""
    parts = []
    for outcome in outcomes or ():
        if outcome.status in PILL_STATUSES:
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
        if outcome.status not in PILL_STATUSES:
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

def default_targets() -> list:
    """The two updatable CLIs, resolved to the SAME binaries the agents launch.

    Claude Code stays on winget deliberately: `claude update` installs a native
    build to a DIFFERENT location, and `providers.resolve_claude()` has a
    hardcoded WinGet-Packages fallback, so a migration could leave AI Hive
    silently launching the stale copy. `agy` is not a winget package at all
    (Google.AntigravityIDE is the separate IDE), so it self-updates.
    """
    from . import providers

    targets = []
    claude = providers.resolve_claude() if providers.detected("claude") else ""
    if claude:
        targets.append(Target(
            key="claude", label=LABELS["claude"], exe=claude,
            process_names=(_image_name(claude, "claude.exe"),),
            winget_id=CLAUDE_WINGET_ID))
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
