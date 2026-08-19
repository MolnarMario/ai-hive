"""TerminalAgent: the MODEL object behind one terminal card.

Owned by the workspace model (via WorkspaceManager), never by a widget —
cards merely subscribe to its signals. That ownership is what guarantees
hidden workspaces keep executing and collecting logs: destroying or
rebuilding a view can never touch the process.
"""

import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from . import providers, scheduled_send, transcripts
from .coordination import sanitize_text
from .scheduled_send import MISSED, PENDING, SENT, ScheduledMessage
from .process_worker import AgentSpec, ProcessWorker, WorkerState
from .pty_worker import PtyWorker

LOG_CAP = 4000  # retained (stream, text) segments per agent


class AgentStatus(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    EXITED_OK = "exited"
    EXITED_ERR = "exited-err"
    CRASHED = "crashed"
    FAILED = "failed"


class AssignmentState(Enum):
    """Orchestration lifecycle, distinct from process status: an interactive
    Claude process stays RUNNING even after finishing a task, so this is what
    says 'done / free for a new task'. Auto-created agents NEVER auto-close —
    they sit in COMPLETED/IDLE until the user closes or reassigns them."""
    IDLE = "idle"                    # running, no assigned task
    AWAITING = "awaiting"            # spawned, waiting for its first task
    WORKING = "working"             # actively on an assigned task
    COMPLETED = "completed"         # finished its task, kept for review/reuse


ASSIGNMENT_LABEL = {
    AssignmentState.IDLE: "Idle",
    AssignmentState.AWAITING: "Awaiting Assignment",
    AssignmentState.WORKING: "Working",
    AssignmentState.COMPLETED: "Completed",
}


# Streams beyond the worker's stdout/stderr, rendered specially by cards.
STREAM_INPUT = "input"    # local echo of a submitted command
STREAM_SYSTEM = "system"  # lifecycle notices

# Handled locally: piped PS 5.1 can't Clear-Host ("host does not implement
# it"), so the console widget clears itself instead.
CLEAR_COMMANDS = {"cls", "clear", "clear-host"}

# Full-screen/TTY programs that cannot work over anonymous pipes.
TUI_PROGRAMS = {"vim", "vi", "nano", "htop", "top", "less", "ssh"}


PTY_BUFFER_CAP = 512 * 1024  # raw VT tail kept for fresh-card replay
PROMPT_MARK_CAP = 200        # prompt milestones kept per agent (FIFO)
REPLY_MARK_CAP = 200         # reply-finished milestones kept per agent (FIFO)

# "Busy" = the agent is actively streaming output (thinking, generating,
# running a command). An interactive process (Claude at its prompt, an idle
# shell) stays RUNNING indefinitely without doing anything, so process-alive is
# NOT a work signal — recent output is. If nothing streams for this long the
# agent is treated as standing by. Claude's working spinner ticks about once a
# second, so this window comfortably spans the gaps between token bursts.
BUSY_IDLE_MS = 2000

# The pty echoes the user's OWN keystrokes straight back as output — but that
# echo is not the agent working. Output arriving within this window of the last
# keystroke the user sent is treated as echo and does NOT light the "working"
# pulse (typing at an idle prompt made the sidebar row animate as if the agent
# were thinking). Genuine work outlasts the window, so a submitted prompt still
# pulses a beat later. Seconds, compared against time.time().
INPUT_ECHO_S = 0.8

# How long a job's process count must stay above this agent's learned resting
# level, while the agent itself is NOT busy, before it's flagged as "idle but
# a background command it started is still running." Debounced like
# BUSY_IDLE_MS so a process that comes and goes quickly (git, rg) never
# flickers the indicator.
BG_SHELL_DEBOUNCE_S = 5.0

# baseline learning is deliberately deferred for this long after (re)start:
# ConPTY's own helper processes (conhost/OpenConsole) can still be spinning up
# right when the job is first assigned, so a sample taken too early can read
# LOWER than true steady state -- and since the baseline only ever ratchets
# DOWN (see poll_bg_shell), one too-early low sample becomes a permanent
# floor a perfectly idle agent can never satisfy again (this was a live-
# reported bug: an agent with nothing running kept showing the gear badge).
# Giving the process tree a moment to settle before the first sample avoids
# seeding a baseline that is wrong for the agent's entire remaining lifetime.
BG_SHELL_WARMUP_S = 6.0

# ...but the warmup alone was not enough (live-reported again: an agent's job
# settled at count=3 while its baseline had locked onto 1). The board bridge
# every Claude agent gets (`log_activity`, spawned as a python child of the
# claude.exe process) itself double-forks a second interpreter, and on a slow
# machine -- e.g. right after a full reboot, cold disk caches, AV scanning --
# that second fork can still be missing when the FIRST post-warmup sample is
# taken. Since that single sample became a permanent floor, the badge could
# never clear again for the rest of that agent's life. So baseline learning
# gets a further settle window on top of the warmup: for this long AFTER
# warmup ends, a sample still sets the baseline outright (tracking the latest
# count, not just ratcheting it down) instead of being compared for "extra",
# so a late-arriving steady-state process is absorbed as normal rather than
# flagged forever. Only once BOTH windows have elapsed does the baseline
# switch to ratchet-down-only, which is what protects a genuine long-running
# background job started later from ever being silently absorbed as "the new
# normal".
BG_SHELL_SETTLE_S = 10.0

# How long `request_repaint` holds the child one column narrower before giving
# the width back. Long enough that ConPTY delivers two distinct size changes
# rather than coalescing them into nothing, short enough that no one sees it.
REPAINT_RESTORE_MS = 120

# How long after a launch a Gemini agent's output can still be its previous
# conversation being redrawn (`agy --continue`). A quota message inside this
# window is treated as the cut-off that ENDED that conversation rather than a
# live one — see `_replay_cut_off`. Generous on purpose: mistaking a live
# refusal for a replayed one only probes the quota sooner, and a still-spent
# quota corrects the retry time itself.
LIMIT_REPLAY_S = 120.0

# How much of the settled screen counts as "the conversation ended here" for
# that replay. The message wraps over three rows and the prompt plus footer sit
# below it, so this is a handful of lines of CONTENT, not the 40 the detector
# searches: anything further up has real work after it and is history.
LIMIT_REPLAY_TAIL_LINES = 12

# strips escape sequences so on-screen TEXT can be matched: the raw stream
# positions words individually ("trust\x1b[20Gthis\x1b[25Gfolder"), so a
# phrase can never be matched against raw bytes
_CSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[@-~]|\x1b[()][AB0]|\x1b\][^\x07\x1b]*\x07?")

# --- "waiting for the user" detection (Claude prompts/questions) ---
# Claude emits no machine-readable "I'm waiting" event, so we scrape the settled
# screen (gated on the idle timer so a half-drawn frame never trips it). A
# A real interactive menu (permission prompt OR AskUserQuestion) is TWO things
# together: a numbered-option list (`1. Yes` / `2. No…`) AND a SELECTION CARET
# (`❯`/`>`) drawn on the currently-highlighted option. The caret is the
# discriminator: an agent's OWN prose routinely contains numbered lists and
# "do you want…/proceed?" phrasing, but never a selection caret in front of a
# numbered option — so keying off the caret stops the "?"/chime from
# false-firing on ordinary output (which it did). Heuristic and non-blocking: a
# missed exotic prompt just doesn't light up.
_NUM_OPTION_RE = re.compile(r"(?m)^\s*[>❯❱│┃|]*\s*\d+\.\s+\S")
# a numbered option with a selection caret in front (optionally past box
# borders) — the highlighted row of a live menu, absent from plain prose lists
_OPTION_CARET_RE = re.compile(r"(?m)^[\s│┃|]*[>❯❱]\s*\d+\.\s+\S")

# Claude's input-box footer rotates through several hints; ANY of them means
# the prompt is live and will accept typing. Only "? for shortcuts" was matched
# originally, which made readiness a coin-flip on whatever the rotation happened
# to be showing (a restored agent sat "not ready" indefinitely — see
# _on_pty_output). Kept lowercase; matched against the lowered tail. Chosen to
# be footer-specific rather than words an agent might write in ordinary prose,
# since a false positive here would deliver a task into a dialog.
_CLAUDE_READY_HINTS = (
    "? for shortcuts",
    "shift+tab to cycle",
    "for agents",
    "auto mode on",
    "ctrl+t to show tasks",
)


def _despace(text: str) -> str:
    """Lowered, with every run of whitespace removed. Readiness matching runs
    through this on both sides -- see TerminalAgent._has_ready_hint for the
    renderer difference that makes it load-bearing."""
    return _WS_RE.sub("", text.lower())


_WS_RE = re.compile(r"\s+")
_READY_HINTS_DESPACED = tuple(_despace(h) for h in _CLAUDE_READY_HINTS)

# --- "this agent was cut off by the plan limit" detection ---
# WHICH agents to resume when the window reopens. The plan-usage reading
# (app/claude_usage.py) is ACCOUNT-wide — it says the account is out and until
# when, never which agents were mid-turn — so attribution comes from the
# screen, LATCHED THE MOMENT THE CUT-OFF IS DRAWN. The patterns themselves live
# in `limit_banner` because `transcripts` (Qt-free) must match the same thing
# off disk; see that module for which signal is authoritative and why.
#
# Two hard-won rules, both from live misses:
#  * Latch on the OUTPUT BURST, not on the idle-timer settle. The cut-off lands
#    right after the user submits a prompt — exactly when `_mark_busy` treats
#    output as keystroke echo and skips arming that timer — and a silently
#    parked agent then emits nothing more to arm it, so the settle never comes.
#  * Never re-derive it later from `_screen_tail`: that is a 4000-char ROLLING
#    buffer, and hours of idle redraws evict the banner entirely.
#  * The banner is not evidence that the cut-off is HAPPENING NOW, only that
#    one happened. It stays on screen (and is re-emitted by every frame
#    repaint) long after a resume, so it must never re-latch an agent that has
#    already been resumed off it — see `_scrape_limit`.
from .limit_banner import (LIMIT_HIT_RE, LIMIT_MENU_RE,  # noqa: F401
                           LIMIT_PROVIDERS, banner_line, banner_reset_at,
                           banner_window, gemini_banner_line, gemini_reset_at,
                           is_limit_screen, parse_reset_clock)


@dataclass
class PromptMark:
    """One "the user submitted a prompt here" milestone on the scrollbar.

    `pos` is a CHARACTER offset into this agent's pty stream, not a screen
    line, because screen lines do not survive a card rebuild: a new
    TerminalView starts with `pushed == 0` and re-feeds the replay buffer, so
    the only coordinate both sides can agree on is how far into the stream the
    submit happened (see TerminalCard._replay_with_marks).

    `uid` is the key any per-card map must use. `id(mark)` cannot be: marks are
    FIFO-capped, and CPython reuses an address once the object is collected, so
    a dropped mark can hand its id to a new one, which would then silently
    inherit the dropped marker's line. `pos` is not unique either (two submits
    with no output between them share an offset)."""

    uid: int
    pos: int
    text: str
    ts: float


@dataclass
class ReplyMark:
    """One "a reply finished here" milestone, rendered inline in the terminal
    right above the input box (see TerminalView.reply_anchor_line) rather than
    on the scrollbar. Mirrors PromptMark's shape and the same `pos`/`uid`
    reasoning -- a character offset into the pty stream survives a card
    rebuild where a screen line does not, and `uid` (never `id()`) is the only
    safe FIFO-capped key."""

    uid: int
    pos: int
    ts: float


class TerminalAgent(QObject):
    output_segment = Signal(str, str)   # stream, text (line mode)
    pty_output = Signal(str)            # raw VT stream (pty mode)
    status_changed = Signal(object)     # AgentStatus
    task_changed = Signal(str)          # current-task text
    font_changed = Signal(int)          # per-agent console font px
    assignment_changed = Signal(object)  # AssignmentState
    role_changed = Signal(str)          # dynamic role/display name
    name_changed = Signal(str)          # user manual display-name rename
    cleared = Signal()                  # console was cleared locally
    activity_changed = Signal(bool)     # busy (streaming output) vs standby
    waiting_changed = Signal(bool)      # waiting for the user (prompt/question)
    summary_changed = Signal(str)       # displayed summary (task or AI title)
    tokens_changed = Signal(str)        # context-usage badge text ("" = hide)
    model_changed = Signal(str)         # live model/effort badge text ("" = hide)
    limit_blocked_changed = Signal(bool)  # cut off by the plan limit (latched)
    # idle (not streaming output) but a background command it started is
    # still running -- see poll_bg_shell(). Transient, like activity/waiting.
    bg_shell_changed = Signal(bool)
    # the child TUI's input prompt went interactive (or was re-armed by a
    # (re)start). Purely a VIEW signal — the card uses it to lift its boot
    # veil — and, like activity/waiting, it must never mark the session dirty.
    prompt_ready_changed = Signal(bool)
    # the deferred-message queue changed (added/cancelled/sent/missed). NOT a
    # countdown tick: this list IS persisted, so the manager wires this to a
    # save, and a per-second tick on that would rewrite session.json all day.
    scheduled_changed = Signal()
    # a prompt milestone was added or the set was cleared. TRANSIENT like
    # activity_changed: milestones are never persisted (the transcript is the
    # durable record of a conversation, and a stored marker list would go stale
    # exactly the way a stored limit latch does), so this must NEVER be wired
    # to a save.
    prompt_marks_changed = Signal()
    # a reply-finished milestone was added or the set was cleared. Same
    # transient contract as prompt_marks_changed -- never persisted, never
    # wired to a save.
    reply_marks_changed = Signal()
    # the walltime shown for the agent's last finished reply changed -- either a
    # live settle or the manager's transcript poll (see set_transcript_reply_at).
    # A live reading exactly like activity_changed and the model chip: NEVER
    # wire it to a save.
    reply_time_changed = Signal()
    # the agent's live conversation was REPLACED (/clear, or a /resume onto a
    # different session), so the scrollback behind the current screen belongs
    # to a conversation that is no longer on display. Transient view signal.
    conversation_replaced = Signal()

    def __init__(self, spec: AgentSpec, parent: QObject | None = None):
        super().__init__(parent)
        self.id = uuid.uuid4().hex
        self.spec = spec
        self.is_pty = bool(spec.pty)
        self.status = AgentStatus.IDLE
        self.current_task = ""              # user/agent-set, feeds the board
        self._ai_title = ""                 # Claude's live conversation title
        self._token_used = 0                # last-turn context occupancy (tokens)
        self._token_window = 0              # sized context window for the model
        # what the agent is ACTUALLY running on, which the user can change
        # mid-session with /model and /effort. Seeded from the launch flags so
        # the card is never blank, then kept true by the manager's transcript
        # poll. Transient: never written back to spec (that is the persisted
        # command line) and never marks the session dirty.
        self._live_model = self._seed_model()
        self._live_effort = (spec.effort or "").strip()
        # ...and which permission mode (Shift+Tab) it is in. Same live reading,
        # with ONE difference: this one IS written back to the spec by the
        # manager, because the CLI does not carry a permission mode across a
        # --resume and a reopened agent must come back in the mode it was in.
        self._live_mode = (getattr(spec, "permission_mode", "") or "").strip()
        self.assignment = AssignmentState.IDLE  # task-assignment lifecycle
        self.auto_created = False           # created with a task via spawn_worker
        self.autostart_on_restore = False  # set from persisted run state
        self.log: deque = deque(maxlen=LOG_CAP)  # line-mode segments
        self._pty_buffer: list[str] = []         # pty raw tail (for replay)
        self._pty_bytes = 0
        # Stream coordinates for prompt milestones. _pty_total counts EVERY
        # character this agent has ever emitted and is never trimmed;
        # _pty_dropped counts what has aged out of the front of _pty_buffer.
        # The difference is what turns a mark's absolute stream position into
        # an offset within pty_replay() -- see replay_marks.
        self._pty_total = 0
        self._pty_dropped = 0
        self._prompt_marks: list[PromptMark] = []
        self._mark_seq = 0     # monotonic; source of PromptMark.uid
        self._reply_marks: list[ReplyMark] = []
        self._reply_mark_seq = 0   # monotonic; source of ReplyMark.uid
        self._pty_seed = ""    # restored screen, until a child draws over it
        self._prompt_ready = False    # the TUI's input prompt is interactive
        self._ready_tail = ""         # rolling stripped tail (pre-ready only)
        self._pending_task = None     # task queued until the TUI is ready
        self._resume_attempt = False  # last start() launched with --continue
        self._resume_fallback_done = False  # already retried fresh once
        # set by the restore path (main.py): the NEXT resume start should
        # verify its pinned conversation still exists and recover if not.
        # One-shot, consumed alongside `resume`, so a plain unit-test start()
        # or a manual restart never touches the filesystem.
        self._verify_resume_target = False
        # walltime of the current process launch; lets session_sync tell which
        # transcript was written DURING this run when reconciling a drifted pin
        self._session_started = 0.0
        # resolves sibling agents' pinned ids in this cwd (set by the manager),
        # so recovery never lands on a peer's conversation
        self._sibling_sessions = None
        self._disposing = False       # teardown in progress (suppress retry)
        # bumped on every (re)start so a queued task-submit Enter from a prior
        # session is never delivered into a fresh, not-yet-ready TUI
        self._submit_gen = 0
        # messages the user wrote now to be typed in later (app/scheduled_send).
        # Persisted (only the PENDING ones) because the whole point is
        # unattended operation, and an app restart must not silently drop a
        # queued hand-off.
        self._scheduled: list[ScheduledMessage] = []
        self._busy = False            # actively streaming output right now
        self._last_output_ts = 0.0    # walltime of the last output burst
        self._last_input_ts = 0.0     # walltime the user last sent keystrokes
        # walltime the agent last settled after streaming output (i.e. the end
        # of a reply), set ONLY in _on_idle_timeout so a forced busy->idle
        # clear from _set_status (stop/crash/exit) never overwrites it with a
        # non-reply moment. Transient like _last_output_ts -- never persisted.
        self._last_reply_ts: float | None = None
        # the same walltime read off the TRANSCRIPT, which is the only source
        # that survives a restart -- _last_reply_ts is stamped from the clock at
        # a settle this process watched, so a resumed conversation has none for
        # any of its past turns. Refreshed by the manager's poll; transient like
        # the AI title, never persisted.
        self._transcript_reply_ts: float | None = None
        # False until the FIRST busy -> idle settle of the current launch has
        # happened. See _on_idle_timeout: a --resume launch replays the whole
        # past conversation as real output before it ever settles, and that
        # one settle must not be mistaken for a fresh reply finishing NOW.
        self._settled_once = False
        # latched "the plan limit cut this agent off" + the reset time its own
        # banner stated. Transient like the waiting flags — never persisted.
        self._limit_blocked = False
        self._limit_resets_at: float | None = None
        self._limit_tries = 0          # resume attempts since the cut-off
        self._limit_last_try = 0.0
        self._limit_from_startup = False   # recovered from disk vs seen live
        self._limit_at = 0.0               # when the cut-off was NOTICED
        # ...and when it actually HAPPENED, which is not the same thing: a
        # cut-off reconstructed from the transcript at startup happened hours
        # before this process existed. The ledger records the real time.
        self._limit_cut_off_at = 0.0
        self._limit_window = ""            # "session" / "weekly" / ...
        self._limit_banner = ""            # the banner line itself
        # the banner line that produced the last latch — the identity of that
        # cut-off, kept ACROSS clear_limit_block so the same line still on
        # screen can't re-latch the agent we just resumed (see _scrape_limit)
        self._limit_last_banner = ""
        # optional hook (set by WorkspaceManager._wire_agent): a line into
        # session.log. Only used by _note_limit_skip; None is a no-op.
        self.audit = None
        # last (reason, banner) reported by _note_limit_skip, so a rejection
        # that persists across hundreds of repaints is recorded ONCE
        self._limit_last_skip = None
        # "waiting for the user" is the OR of three independent sources (see
        # _emit_waiting): _scrape_waiting (the settled screen shows a numbered
        # menu + selection caret — a permission prompt), _tool_waiting (an
        # AskUserQuestion/ExitPlanMode prompt is open, reported by a Claude
        # PreToolUse hook — the scrape CANNOT see these; confirmed no
        # Notification fires either), and _turn_waiting (the agent ended a turn
        # on a plain free-text question, reported by the Stop hook). _waiting is
        # the emitted effective value.
        self._scrape_waiting = False
        self._tool_waiting = False
        self._turn_waiting = False
        self._waiting = False
        self._screen_tail = ""        # rolling escape-stripped output tail
        # "idle, but a background command it started is still running" (see
        # poll_bg_shell): _bg_baseline is the quietest job-process-count ever
        # observed for this launch (learned downward, so a wrapper process
        # like ConPTY's conhost is never mistaken for "extra"); _bg_extra_since
        # is when a count above baseline was first seen while not busy;
        # _bg_shell is the debounced, emitted effective value.
        self._bg_baseline: int | None = None
        # the actual PIDs seen while learning the baseline above -- identity,
        # not just a count, so a later kill (see kill_bg_shell_extras) can
        # tell "known resting overhead" (keep) apart from "showed up on top
        # of that" (safe to kill) instead of only knowing there ARE extras.
        # Mirrors _bg_baseline's own rules: replaced outright during the
        # settle window, only ever intersected (never grown) after.
        self._bg_baseline_pids: set[int] | None = None
        self._bg_extra_since: float | None = None
        self._bg_shell = False
        # single-shot: (re)armed on each output burst; firing = output went
        # quiet, so the agent has dropped back to standby
        self._idle_timer = QTimer(self)
        self._idle_timer.setSingleShot(True)
        self._idle_timer.setInterval(BUSY_IDLE_MS)
        self._idle_timer.timeout.connect(self._on_idle_timeout)
        if self.is_pty:
            self.worker = PtyWorker(spec, parent=self)
            self.worker.output.connect(self._on_pty_output)
        else:
            self.worker = ProcessWorker(spec, parent=self)
            self.worker.output.connect(self._on_output)
        self.worker.state_changed.connect(self._on_worker_state)
        self.worker.finished.connect(self._on_finished)
        self.worker.failed.connect(self._on_failed)

    # ------------------------------------------------------------ control ---

    def start(self) -> None:
        self._set_prompt_ready(False)  # re-armed for the fresh TUI
        self._ready_tail = ""
        self._screen_tail = ""
        self._reset_waiting()
        self._reset_bg_shell()
        self.clear_limit_block()
        self._limit_last_banner = ""   # a new screen: nothing is an echo yet
        self._limit_last_skip = None   # ...so a skip is reported again too
        self._submit_gen += 1  # invalidate any pending task-submit Enter
        self._resume_attempt = self.spec.resume  # for the fast-fail fallback
        self._settled_once = False  # see _on_idle_timeout
        # a NON-resume start is a new conversation, so it gets a new pinned
        # identity (rotating also avoids --session-id colliding with an
        # existing transcript); a resume keeps its pin
        if self.spec.provider in ("claude", "gemini") and not self.spec.resume:
            self.spec.session_id = str(uuid.uuid4())
        elif (self.spec.provider in ("claude", "gemini") and self.spec.resume
              and self._verify_resume_target):
            self._recover_missing_resume_target()
        self._session_started = time.time()
        self.worker.start()
        # resume + verify are one-shot restore aids: a manual restart later is
        # a deliberate fresh session
        self.spec.resume = False
        self._verify_resume_target = False

    def _recover_missing_resume_target(self) -> None:
        """Before a restore-resume, make sure the pinned conversation still
        exists on disk. If it doesn't (a stale id, or a fresh id an earlier
        fallback minted but never used — the case that made `--resume` error
        with a black terminal), resume the most recent REAL conversation in
        this folder instead; if the folder has none, leave the resume as-is
        and let the fast-fail fallback start it fresh. Excludes sibling
        agents' conversations so two agents sharing a folder never resume the
        same transcript (that once truncated one)."""
        from . import session_sync
        if self.spec.provider == "gemini":
            if session_sync.gemini_transcript_exists(self.spec.cwd, self.spec.session_id):
                return
            exclude = set(self._sibling_sessions() if self._sibling_sessions else ())
            exclude.add(self.spec.session_id)
            candidate = session_sync.best_gemini_recovery_id(self.spec.cwd, exclude=exclude)
            if candidate:
                self.notice("[pinned conversation missing; recovering the most "
                            "recent one in this folder]")
                self.spec.session_id = candidate
            return

        if session_sync.transcript_exists(self.spec.cwd, self.spec.session_id):
            return
        if not session_sync.list_transcripts(self.spec.cwd):
            return  # unused folder: nothing to recover; fallback handles it
        exclude = set(self._sibling_sessions() if self._sibling_sessions else ())
        exclude.add(self.spec.session_id)
        candidate = session_sync.best_recovery_id(self.spec.cwd, exclude=exclude)
        if candidate:
            self.notice("[pinned conversation missing; recovering the most "
                        "recent one in this folder]")
            self.spec.session_id = candidate

    def stop(self) -> None:
        if self.worker.is_running():
            self._emit(STREAM_SYSTEM, "[stopping...]\n")
        self.worker.stop()

    def kill(self) -> None:
        self.worker.kill()

    def restart(self) -> None:
        self._set_prompt_ready(False)
        self._ready_tail = ""
        self._screen_tail = ""
        self._reset_waiting()
        self._reset_bg_shell()
        self.clear_limit_block()
        self._limit_last_banner = ""   # a new screen: nothing is an echo yet
        self._limit_last_skip = None   # ...so a skip is reported again too
        self._submit_gen += 1  # invalidate any pending task-submit Enter
        if self.spec.provider in ("claude", "gemini"):  # deliberate fresh session
            self.spec.session_id = str(uuid.uuid4())
        self._session_started = time.time()
        # a restart is always a fresh, non-resumed conversation -- there is no
        # replay to protect the first settle from (see _on_idle_timeout)
        self._settled_once = True
        # ...and it has replied nothing yet, in either source. The new session
        # id above points at a transcript that does not exist, so leaving the
        # old reading in place would show the PREVIOUS conversation's time on a
        # card that no longer has that conversation.
        self._last_reply_ts = None
        self._transcript_reply_ts = None
        if self.is_pty:
            self._pty_buffer = []
            self._pty_bytes = 0
            # a restart is a deliberately fresh conversation, so the stream
            # coordinates and every milestone anchored into them go with it
            self._pty_total = 0
            self._pty_dropped = 0
            self.clear_prompt_marks()
            self.clear_reply_marks()
            self._pty_seed = ""
        else:
            self._emit(STREAM_SYSTEM, "--- restarted ---\n")
        self.worker.restart()

    # ---- pty-mode I/O (keystrokes / resize come straight from the view) ---

    def write(self, data: str) -> bool:
        # remember when the USER last typed, so _mark_busy can tell the pty's
        # echo of that typing apart from genuine agent output — echo must not
        # light the "working" pulse (see _mark_busy / INPUT_ECHO_S).
        self._last_input_ts = time.time()
        return self.worker.write(data)

    def resize(self, rows: int, cols: int) -> None:
        if self.is_pty:
            self.worker.resize(rows, cols)

    def request_repaint(self) -> bool:
        """Ask the child TUI to redraw its whole frame. False if it can't.

        A size change is the only portable way to make a full-screen TUI repaint
        on demand, and it is what an ordinary terminal emitter does whenever its
        window is dragged. Sending one column narrower and back is a no-op for
        the user (the card re-sends its true size on the next `sizeChanged`
        anyway) but forces a fresh frame.

        This exists because a card in a workspace the user has not opened never
        gets one. Qt gives a QStackedWidget page NO resizeEvent until it is made
        current, so `TerminalView.sizeChanged` never fires, `resize` is never
        called, and nothing asks the child to redraw. If that child's prompt
        footer was missed on the way past (see `_on_idle_timeout`), the agent
        looks un-nudgeable until the user clicks the workspace — which is not a
        recovery mechanism.

        Deliberately a request and not a policy: the caller decides when an
        agent has waited long enough to be worth poking.
        """
        if not self.is_pty or not self.worker.is_running():
            return False
        rows, cols = self.worker.rows, self.worker.cols
        if cols <= 10:      # already at PtyWorker's floor; nothing to give back
            return False
        self.worker.resize(rows, cols - 1)
        QTimer.singleShot(REPAINT_RESTORE_MS,
                          lambda: self.worker.resize(rows, cols))
        return True

    def pty_replay(self) -> str:
        return "".join(self._pty_buffer)

    # ------------------------------------------------- prompt milestones ---

    def note_prompt_submitted(self, text: str):
        """Record that the user submitted `text` at the current stream
        position. Returns the mark, or None if it was not recorded.

        Called from TerminalCard, off TerminalView.promptSubmitted -- which
        fires ONLY on a bare Enter in the terminal, so AI Hive's own writes
        (deliver_task, nudge, scheduled sends) can never land here."""
        if not self.is_pty:
            return None
        self._mark_seq += 1
        mark = PromptMark(uid=self._mark_seq, pos=self._pty_total,
                          text=text, ts=time.time())
        self._prompt_marks.append(mark)
        while len(self._prompt_marks) > PROMPT_MARK_CAP:
            self._prompt_marks.pop(0)
        self.prompt_marks_changed.emit()
        return mark

    def prompt_marks(self) -> list:
        return list(self._prompt_marks)

    def clear_prompt_marks(self) -> bool:
        """Drop every milestone (the conversation was cleared or replaced).
        Guarded so a no-op stays silent."""
        if not self._prompt_marks:
            return False
        self._prompt_marks = []
        self.prompt_marks_changed.emit()
        return True

    def note_conversation_replaced(self) -> None:
        """The pinned conversation changed under us (a /clear or an in-TUI
        /resume). Drop the milestones and tell the card to drop the scrollback
        they were anchored in.

        This is the PRIMARY reset signal, not the ED 3 escape: measured on a
        live classic-renderer session, /clear emits no ED 3 at all and simply
        reprints the banner, so the old conversation would otherwise sit in the
        scrollbar behind a screen that has moved on."""
        if not self.is_pty:
            return
        self.clear_prompt_marks()
        self.clear_reply_marks()
        # the reply time goes with them: it describes a turn of the conversation
        # being replaced. The next poll re-reads it from the new transcript
        # (which, after a /clear, has no reply in it yet).
        if self._last_reply_ts is not None or self._transcript_reply_ts is not None:
            self._last_reply_ts = None
            self._transcript_reply_ts = None
            self.reply_time_changed.emit()
        self.conversation_replaced.emit()

    def replay_marks(self) -> list:
        """[(offset within pty_replay(), mark)] for the marks whose bytes are
        still in the replay buffer.

        A mark older than `_pty_dropped` is discarded rather than clamped: its
        content has aged out of the buffer too, so there is no line left for it
        to point at and a clamped marker would just lie."""
        return [(m.pos - self._pty_dropped, m) for m in self._prompt_marks
                if m.pos >= self._pty_dropped]

    # -------------------------------------------------- reply milestones ---

    def note_reply_settled(self):
        """Record that a reply just finished (busy -> idle) at the current
        stream position -- the inline reply-time stamp shown right above the
        input box (see TerminalView.reply_anchor_line). Returns the mark, or
        None if it was not recorded.

        Called unconditionally from `_on_idle_timeout`, NOT gated on a card
        existing (unlike a typed prompt, which can only ever originate from
        one): a hidden workspace keeps executing per the model-owns-processes
        invariant, and its reply history must still be there — via
        reply_replay_marks() — whenever a card is next built for it."""
        if not self.is_pty:
            return None
        self._reply_mark_seq += 1
        mark = ReplyMark(uid=self._reply_mark_seq, pos=self._pty_total,
                         ts=time.time())
        self._reply_marks.append(mark)
        while len(self._reply_marks) > REPLY_MARK_CAP:
            self._reply_marks.pop(0)
        self.reply_marks_changed.emit()
        return mark

    def reply_marks(self) -> list:
        return list(self._reply_marks)

    def clear_reply_marks(self) -> bool:
        """Drop every reply milestone. Guarded so a no-op stays silent."""
        if not self._reply_marks:
            return False
        self._reply_marks = []
        self.reply_marks_changed.emit()
        return True

    def reply_replay_marks(self) -> list:
        """Same shape and same discard rule as replay_marks(), for reply
        milestones."""
        return [(m.pos - self._pty_dropped, m) for m in self._reply_marks
                if m.pos >= self._pty_dropped]

    def seed_pty_replay(self, text: str) -> bool:
        """Preload the screen a previous run left behind, so a RESTORED but
        not-yet-started card paints its conversation instead of a black
        rectangle (see app/screen_snapshot.py).

        Only ever seeds a pty agent that has produced nothing this run —
        never a live one, whose buffer is the real thing. It goes into
        `_pty_buffer` rather than straight to the view because that is the
        one source every card already replays from (`TerminalCard.__init__`),
        so a card built later (a retile, a workspace switch) shows the same
        screen instead of only the card that happened to exist at launch.

        `restart()` clears the buffer, so a deliberate fresh session drops the
        old screen. `start()` deliberately does NOT: waking a stopped card
        resumes its conversation, and the replayed screen scrolling up out of
        the way is exactly what a real terminal would do."""
        if not self.is_pty or self._pty_buffer or not text:
            return False
        self._pty_buffer = [text]
        self._pty_bytes = len(text)
        self._pty_total = len(text)
        self._pty_dropped = 0
        self._pty_seed = text
        return True

    def seed_written_over(self) -> bool:
        """True when this buffer STARTED as a restored snapshot and a live
        child has since written past it.

        The distinction the card's settled-size projection needs. Re-projecting
        the buffer at the real width is right for an ordinary rebuild (a
        retile, a workspace switch), where the buffer IS the live conversation.
        It is wrong for a snapshot a child has drawn over: that seed is the
        previous run's screen, and a running agent is supposed to get a clean
        terminal it fills itself (see `drop_seeded_screen`). Comparing the
        buffer to the snapshot is the only way to tell those apart -- the agent
        being RUNNING is not, since the launch autostart starts agents
        synchronously, well before any card has a settled size."""
        return bool(self._pty_seed) and self._pty_buffer != [self._pty_seed]

    def drop_seeded_screen(self) -> bool:
        """Forget a restored screen that no live child has drawn over.

        A snapshot exists to keep a card the user left STOPPED from reading
        as a dead black rectangle. The moment a child is launching behind
        that card the snapshot has no job left: the TUI paints its own frame
        within seconds, and until it does the seeded copy is WORSE than an
        empty terminal, because it was fed before the tiling grid gave the
        card a real size and pyte cannot reflow it (see
        `TerminalCard._on_status`, which is what calls this).

        Only ever drops the seed itself: once the child has written a single
        byte the buffer is the real screen and must survive."""
        if not self._pty_seed:
            return False
        dropped = self._pty_buffer == [self._pty_seed]
        self._pty_seed = ""
        if not dropped:
            return False
        self._pty_buffer = []
        self._pty_bytes = 0
        return True

    def dispose(self) -> None:
        """Final teardown on card close / workspace delete / app quit."""
        self._disposing = True
        self.worker.dispose()

    def notice(self, text: str) -> None:
        """Append a system-stream notice to the log (and any live cards)."""
        self._emit(STREAM_SYSTEM, text.rstrip("\n") + "\n")

    def set_task(self, text: str) -> None:
        # sanitize at the model boundary: task text can arrive from MCP tool
        # calls / session JSON carrying lone surrogates, which strict UTF-8
        # sinks (board file, pty pipe) refuse to encode
        text = sanitize_text(text).strip()
        if text != self.current_task:
            before = self.summary()
            self.current_task = text
            self.task_changed.emit(text)
            if self.summary() != before:
                self.summary_changed.emit(self.summary())

    def summary(self) -> str:
        """One-line 'what this agent is working on': the assigned task if set,
        otherwise Claude Code's own AI-generated conversation title (the same
        summary shown in `/resume`, read from the live transcript)."""
        return self.current_task or self._ai_title

    def set_ai_title(self, text: str) -> None:
        """Adopt Claude's latest AI conversation title (from the transcript).
        Transient — never persisted; refreshed by the manager's poll."""
        text = sanitize_text(text or "").strip()
        if text == self._ai_title:
            return
        before = self.summary()
        self._ai_title = text
        if self.summary() != before:   # only when the DISPLAYED summary changes
            self.summary_changed.emit(self.summary())

    def set_token_usage(self, used: int, window: int) -> None:
        """Adopt the latest context-window occupancy read from the transcript.
        Transient — never persisted, never marks the session dirty (like the AI
        title); emits only when the DISPLAYED badge text actually changes."""
        if used == self._token_used and window == self._token_window:
            return
        before = self.token_badge()
        self._token_used = max(0, int(used))
        self._token_window = max(0, int(window))
        if self.token_badge() != before:
            self.tokens_changed.emit(self.token_badge())

    def token_badge(self) -> str:
        """Compact context-usage string for the card header, e.g. "20% of 1M".
        "" when there is no usage data yet (fresh / non-Claude agent)."""
        if self._token_used <= 0 or self._token_window <= 0:
            return ""
        pct = min(100, round(self._token_used * 100 / self._token_window))
        if self._token_window >= 1_000_000:
            win = f"{self._token_window // 1_000_000}M"
        else:
            win = f"{self._token_window // 1000}K"
        return f"{pct}% of {win}"

    def _seed_model(self) -> str:
        """The model label to show before the transcript has said anything.
        Claude agents launched on "Default" pass no --model, so the CLI falls
        back to the user's own saved setting: read that rather than show
        nothing. Other providers bake effort into their model string already
        (agy's "Gemini 3.1 Pro (High)"), so it is shown verbatim."""
        chosen = (self.spec.model or "").strip()
        if self.spec.provider != "claude":
            return chosen
        return transcripts.model_display(chosen or providers.user_default_model())

    def set_live_model(self, model: str, effort: str, mode: str = "") -> None:
        """Adopt the model/effort/permission mode the conversation is actually
        on, as read from the transcript. The BADGE is transient like the AI
        title and the token badge: never marks the session dirty here, and emits
        only when the DISPLAYED text changes (this polls every couple of
        seconds). An empty reading is ignored rather than blanking a good label:
        a fresh conversation has no evidence yet, and the launch seed is still
        right. Persisting the mode is the manager's job, deliberately kept out
        of here so this stays a pure display update."""
        model = (model or "").strip()
        effort = (effort or "").strip()
        mode = (mode or "").strip()
        if not model and not effort and not mode:
            return
        before = self.model_badge()
        if model:
            self._live_model = model
        if effort:
            self._live_effort = effort
        if mode:
            self._live_mode = mode
        if self.model_badge() != before:
            self.model_changed.emit(self.model_badge())

    def permission_mode(self) -> str:
        """The raw permission-mode token this agent is in, as the CLI names it
        ("", "default", "auto", "plan", ...). "" for a fresh Claude agent means
        the CLI's own ask-each-time default; for anything else it means the
        concept does not apply."""
        return self._live_mode

    def permission_mode_label(self) -> str:
        """That mode as it reads on the card, e.g. "auto" / "plan" / "manual".
        "" for a non-Claude agent, which has no such mode at all."""
        if self.spec.provider != "claude":
            return ""
        return providers.permission_mode_display(self._live_mode)

    def model_badge(self) -> str:
        """Compact "what am I running on" string for the card header, e.g.
        "Opus 5 · high · plan" (model, effort, permission mode). Effort is
        dropped when it is the CLI's own default and the mode when the agent
        has none; "" when even the model is unknown (a non-Claude agent on a
        bare command), which hides the chip entirely."""
        if not self._live_model:
            return ""
        parts = [self._live_model]
        if self._live_effort:
            parts.append(self._live_effort)
        mode = self.permission_mode_label()
        if mode:
            parts.append(mode)
        return " · ".join(parts)

    def live_model(self) -> tuple[str, str]:
        return (self._live_model, self._live_effort)

    def set_assignment(self, state) -> None:
        if state is not self.assignment:
            self.assignment = state
            self.assignment_changed.emit(state)

    def set_name(self, name: str) -> None:
        """User-facing rename of the display NAME only (role untouched). Marks
        the name custom so a retask (set_role) never clobbers it."""
        name = sanitize_text(name or "").strip()
        if name and name != self.spec.name:
            self.spec.name = name
            self.spec.custom_name = True
            self.name_changed.emit(name)

    def set_role(self, name: str) -> None:
        """Dynamic role-based rename (Backend Architect, Testing Agent, …).

        Always updates the role label. The display NAME follows the role only
        while it hasn't been manually set — once the user renames the agent
        (custom_name), a retask updates the role sublabel but leaves the name."""
        name = sanitize_text(name or "").strip()
        if not name:
            return
        changed = name != self.spec.role
        self.spec.role = name
        if not self.spec.custom_name and name != self.spec.name:
            self.spec.name = name
            changed = True
        if changed:
            self.role_changed.emit(name)

    def set_font(self, px: int) -> None:
        px = int(px)
        if px != self.spec.font_px:
            self.spec.font_px = px
            self.font_changed.emit(px)

    def roster_row(self) -> dict:
        return {
            "name": self.spec.name, "role": self.spec.role,
            "provider": self.spec.provider,
            # match the Activity panel's fallback so both roster views agree
            "model": self.spec.model or self.spec.provider or self.spec.kind.value,
            "status": self.status.value, "task": self.current_task,
            "assignment": ASSIGNMENT_LABEL.get(self.assignment, ""),
        }

    def deliver_task(self, text: str) -> None:
        """Give this agent a task to work on (spawn_worker/reassign path).

        For a pty agent (Claude Code) the task is delivered only once the TUI
        is prompt-ready (we watch its output stream for bracketed-paste-enable,
        ESC[?2004h — a fixed delay is unreliable on a cold start). Multi-line
        text is wrapped in bracketed paste so it isn't submitted at the first
        newline, then a separate Enter submits it."""
        text = sanitize_text(text or "").strip()
        if not text:
            return
        self.set_task(text)
        self.set_assignment(AssignmentState.WORKING)
        if not self.is_pty:
            self.send_command(text)
            return
        if self._prompt_ready and self.worker.is_running():
            self._write_task_to_pty(text)
        else:
            self._pending_task = text  # flushed when the prompt is ready

    def nudge(self, text: str) -> bool:
        """Type `text` at the agent's prompt and submit it, WITHOUT touching any
        persisted metadata. Returns False when the agent can't take it.

        The difference from `deliver_task` is the whole point: that path is for
        ASSIGNING work, so it overwrites `current_task`, flips the assignment to
        WORKING and re-infers the role from the text. A nudge is a message
        inside work the agent already has (the auto-continue after a plan-limit
        reset), so none of that may change — `current_task` in particular is
        persisted and shown in the sidebar and on the board.

        Unlike `write`, this does NOT stamp `_last_input_ts`: the resumed work's
        output must still light the sidebar's "working" pulse, exactly as a
        delivered task's does.
        """
        text = sanitize_text(text or "").strip()
        if not text or not self.is_pty:
            return False
        if not (self._prompt_ready and self.worker.is_running()):
            return False
        self._write_task_to_pty(text)
        return True

    # ------------------------------------------------- deferred messages ---
    # A scheduled message is a submit the user deferred: they typed it now and
    # chose when it should go in. Everything here is bookkeeping — the actual
    # send is `nudge` above, driven by MainWindow's tick, for the same reason
    # the plan-limit auto-continue lives there rather than in the model.

    def schedule_message(self, text: str, due_ts: float) -> ScheduledMessage | None:
        """Queue `text` to be typed in at `due_ts`. Returns the entry, or None
        when the text is empty or this agent is already at its cap."""
        text = sanitize_text(text or "").strip()
        if not text or len(self.pending_scheduled()) >= scheduled_send.MAX_PER_AGENT:
            return None
        msg = ScheduledMessage(text=text, due_ts=float(due_ts))
        self._scheduled.append(msg)
        self.scheduled_changed.emit()
        return msg

    def scheduled_messages(self) -> list[ScheduledMessage]:
        """Everything still held, soonest first. Sent entries are dropped as
        they go out, so in practice this is the pending ones plus any missed
        entry still waiting for the user to notice it."""
        return sorted(self._scheduled, key=lambda m: m.due_ts)

    def pending_scheduled(self) -> list[ScheduledMessage]:
        return [m for m in self.scheduled_messages() if m.is_pending()]

    def next_scheduled(self) -> ScheduledMessage | None:
        """The soonest pending message — what the card's countdown shows."""
        pending = self.pending_scheduled()
        return pending[0] if pending else None

    def due_scheduled(self, now: float | None = None) -> list[ScheduledMessage]:
        return [m for m in self.scheduled_messages() if m.is_due(now)]

    def missed_scheduled(self) -> list[ScheduledMessage]:
        return [m for m in self.scheduled_messages() if m.state == MISSED]

    def find_scheduled(self, mid: str) -> ScheduledMessage | None:
        return next((m for m in self._scheduled if m.id == mid), None)

    def cancel_scheduled(self, mid: str) -> bool:
        """Drop a message entirely (the user cancelled it, or dismissed a
        missed one). Removes rather than marks so it stops being persisted."""
        msg = self.find_scheduled(mid)
        if msg is None:
            return False
        self._scheduled.remove(msg)
        self.scheduled_changed.emit()
        return True

    def reschedule(self, mid: str, text: str, due_ts: float) -> bool:
        """Edit an already-queued message's text and/or fire time in place,
        keeping its identity -- the alternative (cancel + re-create) loses its
        spot silently. A MISSED entry given a future time is revived to
        PENDING: MISSED only means "was due and nobody acted on it", not a
        dead end."""
        msg = self.find_scheduled(mid)
        text = sanitize_text(text or "").strip()
        if msg is None or not text:
            return False
        msg.text = text
        msg.due_ts = float(due_ts)
        if msg.due_ts > time.time():
            msg.state = PENDING
            msg.attempts = 0
        self.scheduled_changed.emit()
        return True

    def mark_scheduled_sent(self, mid: str) -> bool:
        """It went in. The entry is DROPPED, not kept: the conversation itself
        is the record of what was said, and this is a queue, not a ledger."""
        msg = self.find_scheduled(mid)
        if msg is None:
            return False
        msg.state = SENT
        self._scheduled.remove(msg)
        self.scheduled_changed.emit()
        return True

    def mark_scheduled_missed(self, mid: str) -> bool:
        """Give up on delivering it, but KEEP it visible. The user chose a time
        and it did not happen; silently discarding that is how an unattended
        hand-off disappears without trace."""
        msg = self.find_scheduled(mid)
        if msg is None or msg.state == MISSED:
            return False
        msg.state = MISSED
        self.scheduled_changed.emit()
        return True

    def note_scheduled_attempt(self, mid: str) -> None:
        """Record a delivery attempt that the agent refused (its TUI is not
        ready yet). Deliberately does NOT emit: this happens on the tick, and
        the queue's shape has not changed."""
        msg = self.find_scheduled(mid)
        if msg is not None:
            msg.attempts += 1

    def scheduled_dicts(self) -> list[dict]:
        """The PENDING queue, for the session file. Missed entries are left out
        on purpose: on the next launch they would be re-derived as missed
        anyway, and a stale one would linger on the card forever."""
        return [m.to_dict() for m in self.pending_scheduled()]

    def restore_scheduled(self, rows) -> None:
        """Rebuild the queue from a session record.

        CRITICAL: anything whose time has already passed comes back MISSED, and
        is never sent. A message set for 3am that the app was closed for must
        not fire at 10am into a conversation that has moved on — the agent may
        have been restarted, the work it was chaining onto may be long done, and
        an unexpected prompt hours late is both a surprise and real quota spent.
        The user sees it on the card and decides.
        """
        if not rows:
            return
        now = time.time()
        for row in rows or []:
            if len(self._scheduled) >= scheduled_send.MAX_PER_AGENT:
                break
            if not isinstance(row, dict):
                continue
            msg = ScheduledMessage.from_dict(row)
            if msg is None:
                continue
            if msg.state == PENDING and msg.due_ts <= now:
                msg.state = MISSED
            self._scheduled.append(msg)
        if self._scheduled:
            self.scheduled_changed.emit()

    def _write_task_to_pty(self, text: str) -> None:
        body = text.replace("\r\n", "\r").replace("\n", "\r")
        if "\r" in body:  # multi-line: bracketed paste, then submit
            self.worker.write("\x1b[200~" + body + "\x1b[201~")
        else:
            self.worker.write(body)
        # submit AFTER a beat: a CR arriving in the same input burst as the
        # text reads as part of a paste (verified live against Claude Code) —
        # it inserts a newline into the input box instead of submitting, and
        # the task never runs. Guard on the submit generation so a restart inside
        # the 350 ms window (which bumps _submit_gen) can't fire this stray CR
        # into a fresh session. We deliberately do NOT also gate on _prompt_ready:
        # in the normal path it is always True here, and adding it only risks
        # suppressing a legitimate submit on this timing-sensitive path — the
        # generation check alone fully covers the restart race.
        gen = self._submit_gen
        QTimer.singleShot(350, lambda: self._submit_gen == gen
                          and self.worker.is_running()
                          and self.worker.write("\r"))

    def send_command(self, text: str) -> None:
        if self.is_pty:  # pty terminals take raw keystrokes, not line commands
            self.worker.send_line(text)
            return
        text = text.rstrip("\n")
        if not text:
            return
        if text.strip().lower() in CLEAR_COMMANDS:
            self.log.clear()
            self.cleared.emit()
            return
        self._emit(STREAM_INPUT, f"> {text}\n")
        self._hint_if_tty_only(text.strip())
        if not self.worker.send_line(text):
            self._emit(STREAM_SYSTEM,
                       "[not running; right-click the card header to start]\n")

    def _hint_if_tty_only(self, command: str) -> None:
        parts = command.split()
        if not parts:
            return
        program = parts[0].lower().replace("\\", "/").rsplit("/", 1)[-1]
        program = program.removesuffix(".exe").removesuffix(".cmd")
        if program == "claude" and not {"-p", "--print"} & set(parts[1:]):
            self._emit(STREAM_SYSTEM,
                       "[This is a line-mode terminal, so interactive Claude "
                       "Code can't run here. Add a \"Claude Code "
                       "(interactive)\" terminal (+ Terminal), or tick \"Full "
                       "terminal\" when adding one. For a one-shot in this "
                       'card, use:  claude -p "your prompt"]\n')
        elif program in TUI_PROGRAMS:
            self._emit(STREAM_SYSTEM,
                       f"[{program} is a full-screen terminal app, so it won't "
                       "render in this line-mode console. Add it in a \"Full "
                       "terminal (interactive)\" card instead.]\n")

    def is_running(self) -> bool:
        return self.worker.is_running()

    def is_busy(self) -> bool:
        """True while the agent is actively producing output — the accurate
        'working' signal, as opposed to is_running() which stays True for an
        interactive process idling at its prompt."""
        return self._busy

    def last_reply_at(self) -> float | None:
        """Walltime (epoch seconds) the agent last finished a reply, or None if
        there is no evidence of one. A live reading like is_busy(); never
        persisted.

        TWO sources, and the later one wins. `_last_reply_ts` is the busy ->
        idle settle in _on_idle_timeout: exact, instant, and only ever exists
        for a turn THIS process watched finish. `_transcript_reply_ts` is read
        off the conversation Claude wrote to disk, which is the only thing that
        survives a restart -- so a resumed conversation, where the live source
        has nothing at all, now reports when its last answer was REALLY
        generated instead of hiding the badge.

        max() rather than a preference for either: the settle lands a couple of
        seconds after the record Claude wrote, so the live source wins the turn
        in progress (no waiting for the next poll to see a reply that just
        landed) and the transcript wins everything this run never saw."""
        live, disk = self._last_reply_ts, self._transcript_reply_ts
        if live and disk:
            return max(live, disk)
        return live or disk or None

    def set_transcript_reply_at(self, ts: float) -> None:
        """Adopt the finish time of this conversation's last reply as read from
        the transcript (transcripts.latest_reply_at), refreshed by the manager's
        poll. Transient -- never persisted, never marks the session dirty (same
        rule as the AI title and the model chip); emits only when the walltime
        the card would DISPLAY actually changes, so a poll over an idle agent is
        free."""
        ts = float(ts or 0.0)
        if ts <= 0 or ts == self._transcript_reply_ts:
            return
        before = self.last_reply_at()
        self._transcript_reply_ts = ts
        if self.last_reply_at() != before:
            self.reply_time_changed.emit()

    def is_bg_shell_busy(self) -> bool:
        """True when the agent itself is quiet (not is_busy()) but a
        background command it started is still running -- see
        poll_bg_shell()."""
        return self._bg_shell

    def is_waiting(self) -> bool:
        """True when the agent needs the user: it has settled on a numbered
        prompt, an interactive AskUserQuestion/ExitPlanMode prompt is open, or
        it ended a turn on a free-text question. The OR of three sources — see
        _emit_waiting."""
        return self._waiting

    def _emit_waiting(self) -> None:
        """Recompute the effective waiting state from its three sources and emit
        only on a genuine change. No single source is complete: the screen
        scrape misses AskUserQuestion (no numbered+caret menu) and plain
        questions; the hook edges miss classic permission menus. Together they
        cover every 'needs the user' case."""
        eff = self._scrape_waiting or self._tool_waiting or self._turn_waiting
        if eff != self._waiting:
            self._waiting = eff
            self.waiting_changed.emit(eff)

    def set_tool_waiting(self, waiting: bool) -> None:
        """Authoritative Claude-hook edge: a PreToolUse for AskUserQuestion/
        ExitPlanMode opened an interactive prompt (True), or a PostToolUse/Stop
        for the same closed it (False). STICKY against output — the tool draws
        its own UI, so an output burst must NOT clear it (that is exactly why the
        screen scrape alone missed these); only the matching hook clears it."""
        if waiting != self._tool_waiting:
            self._tool_waiting = waiting
            self._emit_waiting()

    def set_turn_waiting(self, waiting: bool) -> None:
        """Authoritative Claude-hook edge: the agent ended a turn on a free-text
        question (Stop hook, last message ends with '?'). Unlike a tool prompt
        this clears on the next output burst (the user engaged / the agent
        resumed), as well as on a following non-question turn end."""
        if waiting != self._turn_waiting:
            self._turn_waiting = waiting
            self._emit_waiting()

    def _reset_waiting(self) -> None:
        """Clear every waiting source (start/restart/exit) and emit if needed."""
        self._scrape_waiting = False
        self._tool_waiting = False
        self._turn_waiting = False
        self._emit_waiting()

    def _reset_bg_shell(self) -> None:
        """Clear the background-shell latch and its learned baseline
        (start/restart/exit) and emit if it was set. A fresh launch's job
        starts from a clean slate, and a dead agent isn't waiting on
        anything."""
        self._bg_baseline = None
        self._bg_baseline_pids = None
        self._bg_extra_since = None
        if self._bg_shell:
            self._bg_shell = False
            self.bg_shell_changed.emit(False)

    def poll_bg_shell(self) -> None:
        """Externally ticked (see WorkspaceManager.poll_bg_shell_activity):
        notice a job whose live process count sits above this agent's
        learned resting level while the agent ITSELF is quiet -- the
        signature of a background command (a Bash tool call with
        run_in_background, a shell's own `cmd &`) still running after the
        agent returned to its prompt. Debounced like _mark_busy/idle so a
        process that comes and goes quickly never flickers the indicator."""
        if self.status not in (AgentStatus.RUNNING, AgentStatus.STARTING):
            return
        elapsed = time.time() - self._session_started
        # let the process tree settle before trusting any sample as the
        # resting baseline (see BG_SHELL_WARMUP_S)
        if elapsed < BG_SHELL_WARMUP_S:
            return
        count = self.worker.job_process_count()
        if count <= 0:
            return  # query unsupported/failed -- don't flap on missing data
        if elapsed < BG_SHELL_WARMUP_S + BG_SHELL_SETTLE_S:
            # still settling: track the latest count outright (up or down)
            # rather than only ratcheting down, so a steady-state process
            # that spawns a little late (see BG_SHELL_SETTLE_S) is learned as
            # baseline instead of being locked in as "extra" forever. Same
            # replace-outright treatment for the PID identities behind it.
            self._bg_baseline = count
            self._bg_baseline_pids = set(self.worker.job_process_ids())
            self._bg_extra_since = None
            if self._bg_shell:
                self._bg_shell = False
                self.bg_shell_changed.emit(False)
            return
        # learn the resting size down over time: only a count ABOVE the
        # quietest one ever seen for this launch counts as "extra" -- this is
        # what keeps a ConPTY wrapper process (conhost/OpenConsole) or any
        # other fixed overhead from being mistaken for background work. This
        # can ONLY move the floor down, never up, so it can never absorb a
        # genuine background job (which raises the count) as "the new
        # normal" -- the risk is entirely in the other direction (see
        # BG_SHELL_WARMUP_S/BG_SHELL_SETTLE_S), which is why those exist.
        if self._bg_baseline is None or count < self._bg_baseline:
            self._bg_baseline = count
        # the PID identities mirror that same one-way rule: intersect only
        # (drop a baseline pid that has since exited), never grow past
        # settling -- so a process that shows up AFTER settling is always
        # "extra" for kill_bg_shell_extras, never silently adopted as normal.
        if self._bg_baseline_pids is not None:
            self._bg_baseline_pids &= set(self.worker.job_process_ids())
        extra = count > self._bg_baseline
        now = time.time()
        if self.is_busy() or not extra:
            self._bg_extra_since = None
            if self._bg_shell:
                self._bg_shell = False
                self.bg_shell_changed.emit(False)
            return
        if self._bg_extra_since is None:
            self._bg_extra_since = now
        elif (now - self._bg_extra_since >= BG_SHELL_DEBOUNCE_S
              and not self._bg_shell):
            self._bg_shell = True
            self.bg_shell_changed.emit(True)
            # forensic trail: if the baseline is ever wrong (a live report
            # already happened once), this is what lets it be diagnosed from
            # session.log instead of reconstructed by elimination
            if self.audit is not None:
                try:
                    self.audit(f"BG-SHELL agent={self.spec.name} "
                               f"count={count} baseline={self._bg_baseline}")
                except Exception:
                    pass

    def _bg_shell_keep_pids(self) -> set[int]:
        """Pids that must never be treated as 'extra': the agent's own root
        process, plus everything seen while the baseline was learned (the
        log_activity mcp bridge, ConPTY's own conhost/OpenConsole helper).
        Shared by every read/kill path below so they can never disagree
        about what's safe to touch."""
        keep = set(self._bg_baseline_pids or ())
        root = self.worker.pid()
        if root:
            keep.add(root)
        return keep

    def bg_shell_extra_pids(self) -> list[int]:
        """The actual extra processes behind the gear badge right now -- a
        fresh query, not the last poll's snapshot -- so a kill menu can list
        exactly what's there and let the user choose, instead of an
        all-or-nothing kill. Empty before a baseline exists to compare
        against (i.e. before poll_bg_shell has ever settled)."""
        if self._bg_baseline_pids is None:
            return []
        keep = self._bg_shell_keep_pids()
        return [p for p in self.worker.job_process_ids() if p not in keep]

    def kill_bg_shell_pid(self, pid: int) -> bool:
        """Kill exactly one process bg_shell_extra_pids() listed -- the
        per-item action in the kill menu, for when killing everything at
        once risks taking down a command the agent is actually waiting on.
        Refuses anything not CURRENTLY a genuine extra (re-checked here, not
        trusted from a menu built a moment ago), so it can never be used to
        kill the agent's own process or something from its baseline."""
        if pid not in self.bg_shell_extra_pids():
            return False
        self.worker.kill_pid(pid)
        if self.audit is not None:
            try:
                self.audit(f"BG-SHELL-KILL agent={self.spec.name} "
                           f"pids=[{pid}]")
            except Exception:
                pass
        if not self.bg_shell_extra_pids():
            self._bg_extra_since = None
            if self._bg_shell:
                self._bg_shell = False
                self.bg_shell_changed.emit(False)
        return True

    def kill_bg_shell_extras(self) -> list[int]:
        """Hard-kill every extra at once (the menu's "kill all" action) --
        see kill_bg_shell_pid for the per-item equivalent and what "extra"
        excludes. A no-op (returns []) unless the badge is actually lit, so
        a stray click on a just-cleared marker can't kill anything."""
        if not self._bg_shell:
            return []
        killed = self.worker.kill_extra_processes(self._bg_shell_keep_pids())
        if killed and self.audit is not None:
            try:
                self.audit(f"BG-SHELL-KILL agent={self.spec.name} "
                           f"pids={killed}")
            except Exception:
                pass
        self._bg_extra_since = None
        if self._bg_shell:
            self._bg_shell = False
            self.bg_shell_changed.emit(False)
        return killed

    def _mark_busy(self) -> None:
        # only a live agent can be working; guard on status (not worker state)
        # so this is unit-testable without a real child process
        if self.status not in (AgentStatus.RUNNING, AgentStatus.STARTING):
            return
        now = time.time()
        self._last_output_ts = now
        # producing output => not waiting on a scrape menu, and any free-text
        # turn-question is resolved (the user engaged / the agent resumed). A
        # tool prompt (AskUserQuestion) renders its OWN output, so _tool_waiting
        # is deliberately NOT cleared here — only its PostToolUse/Stop hook does.
        self._scrape_waiting = False
        self._turn_waiting = False
        self._emit_waiting()
        # Local echo of the user's own typing comes straight back through the
        # pty as output, but it is NOT the agent working. If this burst lands
        # within the echo window of the last keystroke the user sent, don't
        # light the "working" pulse for it (and don't re-arm the idle timer, so
        # a pulse left over from real work still drops on schedule instead of
        # being held alive by the typing). Genuine work outlasts the window.
        if now - self._last_input_ts >= INPUT_ECHO_S:
            if not self._busy:
                self._busy = True
                self.activity_changed.emit(True)
            self._idle_timer.start()  # (re)arm; fires once output falls quiet

    def _on_idle_timeout(self) -> None:
        if self._busy:
            self._busy = False
            # A --resume launch replays the WHOLE past conversation as real
            # terminal output before it ever goes quiet, so the FIRST settle
            # of a resumed launch is that replay finishing, not a fresh reply
            # -- stamping it "now" is exactly the live-reported bug where
            # reopening the app showed the CURRENT time next to the last
            # reply instead of when it actually happened. Only that one
            # settle is suppressed; every settle after it (including the
            # very next one, moments later, once the user sends something
            # new) is a genuine reply and stamps normally. A non-resumed
            # launch has nothing to replay, so its first settle is real too.
            replay_settle = self._resume_attempt and not self._settled_once
            self._settled_once = True
            if not replay_settle:
                self._last_reply_ts = time.time()
                self.note_reply_settled()
                self.reply_time_changed.emit()
            self.activity_changed.emit(False)
        # the screen has settled (2 s quiet) — is it a prompt awaiting the user?
        self._scrape_waiting = self._screen_waiting()
        self._emit_waiting()
        # ...and is this the plan-limit banner? Latch it NOW, while the frame is
        # current; by reset time the rolling tail no longer holds it.
        self._scrape_limit()
        # ...and did we MISS the prompt going live? _on_pty_output decides
        # readiness once per burst against a 600-char tail, so a footer followed
        # by more than that in the same burst is never seen — and if the child
        # then falls quiet (a resumed conversation parked at its prompt) nothing
        # ever looks again. The agent stays "not ready" forever: `nudge` refuses
        # it, so a plan-limit resume is declined every minute, and a delivered
        # task waits in _pending_task indefinitely. Observed live 2026-08-07:
        # nine consecutive "WAIT (TUI not ready)" ticks on an agent whose child
        # had been up for ten minutes, ending only when the user happened to
        # click that workspace.
        #
        # Re-checking here can only ever flip readiness LATE (this fires 2 s
        # after output settles, never before the burst path has had its go), so
        # it cannot perturb launch or first-task-submit timing — the one thing
        # the SessionStart invariant is about. It reads the 4000-char screen
        # tail rather than the 600-char one for the same reason as above.
        if not self._prompt_ready and self.spec.provider == "claude":
            if self._has_ready_hint(self._screen_tail):
                self._became_prompt_ready()

    def _tail_lines(self, n: int, skip_blank: bool = False) -> str:
        """The last `n` lines of the escape-stripped screen tail, joined.

        `skip_blank` COUNTS ONLY LINES WITH CONTENT, and the difference is not
        cosmetic. Claude's TUI pads its frame with blank rows, so a raw
        `[-40:]` slice can span as little as 432 characters and 5 non-blank
        lines — measured on real screen snapshots, against a median ~900
        characters per frame repaint. That is less than half a frame, which is
        how a plan-limit banner went unseen by BOTH the per-burst scrape and
        the settle scrape 2 s later, and stranded an agent overnight
        (2026-08-07, CVsummer2026).

        Callers that hunt for something NOT anchored to the bottom of the frame
        (the limit banner, which renders above the menu, the input box and the
        footer) pass skip_blank=True. The two callers that do NOT are
        deliberate, and must stay that way:

          * `_screen_waiting` wants the drawn menu just above the input box,
            and its 18-line bound plus the caret requirement is the tuning that
            keeps the "?" chime off an agent's own numbered prose.
          * `recheck_limit` wants the menu that is TORN DOWN on a resume. The
            raw tail still holds that menu's earlier renders, so reaching
            further back would find it forever and report "still blocked" on an
            agent that is already going again.
        """
        lines = self._screen_tail.splitlines()
        if skip_blank:
            lines = [ln for ln in lines if ln.strip()]
        return "\n".join(lines[-n:])

    def _screen_waiting(self) -> bool:
        # ground-truth on the drawn box; suppress for a mode that shows no
        # prompts. bypassPermissions skips ALL prompts; other modes (incl.
        # acceptEdits) still surface questions, so only bypass is suppressed.
        if self.spec.provider != "claude":
            return False
        if getattr(self.spec, "permission_mode", "") == "bypassPermissions":
            return False
        # raw lines on purpose — see _tail_lines
        region = self._tail_lines(18)
        if not region:
            return False
        # a live menu = 2+ numbered options AND a selection caret on one of
        # them. Requiring the caret is what keeps the agent's own numbered
        # prose (which has no caret) from lighting the "?" and ringing the bell.
        n_opts = len(_NUM_OPTION_RE.findall(region))
        has_caret = bool(_OPTION_CARET_RE.search(region))
        return has_caret and n_opts >= 2

    def is_limit_blocked(self) -> bool:
        """True when this agent was cut off by the plan limit and hasn't been
        resumed yet. LATCHED when the banner was drawn (see _scrape_limit), not
        re-derived on demand — by reset time the banner is long gone from the
        rolling screen tail. Says WHICH agents to resume, which the
        account-wide usage reading cannot know."""
        return self._limit_blocked

    def limit_resets_at(self) -> float | None:
        """Epoch seconds the banner said this agent's limit resets, or None if
        it didn't say. The network-free half of the auto-continue trigger."""
        return self._limit_resets_at

    def _scrape_limit(self) -> None:
        """Latch the plan-limit banner off the screen tail.

        Called on EVERY output burst, not only on the idle-timer settle. The
        settle gate is right for `_screen_waiting` (a half-drawn menu is
        ambiguous), but it is wrong here and cost a second live miss: the
        banner lands immediately after the user submits a prompt, which is
        exactly when `_mark_busy` treats output as keystroke echo and SKIPS
        arming the idle timer — and once the agent parks silently there is no
        further output to arm it, so the settle never comes and the cut-off is
        never seen. "You've hit your … limit" is unambiguous the moment it
        appears, so it needs no settle.

        Sticky once set: the agent is parked and its own redraws must not clear
        it, which is what a rolling-buffer re-scrape got wrong. It is cleared
        explicitly instead — on start/restart, and by `clear_limit_block` once
        the agent has genuinely resumed.
        """
        if self.spec.provider not in LIMIT_PROVIDERS or self._limit_blocked:
            return
        # Only a LIVE cut-off counts. On `--resume` Claude redraws the whole
        # prior conversation, so a banner from a previous session scrolls past
        # as history — latching that would schedule a phantom Continue for the
        # next time that clock came round. The input-box footer marks the end
        # of the replay, and a real cut-off can only happen after it, so
        # gating on prompt-readiness separates the two exactly. (This is
        # checked before _on_pty_output sets the flag, so the burst that ends
        # the replay is itself excluded.)
        if not self._prompt_ready:
            self._note_limit_skip("prompt not ready (launch or resume replay)")
            return
        # Deliberately a WIDER region than _screen_waiting's last 18 lines.
        # That bound is right for a selection menu, which is anchored just
        # above the input box; the banner is NOT bottom-anchored — the options
        # menu, the input box and the footer all render below it, so 18 lines
        # can push it out of view on a full frame. Both patterns are specific
        # enough to search a wider window safely.
        #
        # skip_blank is what makes "40 lines" mean 40 lines of CONTENT. Counting
        # the TUI's blank padding rows instead shrank this window to a handful
        # of characters on a real frame and lost a genuine cut-off — see
        # _tail_lines.
        region = self._tail_lines(40, skip_blank=True)
        menu, banner, window, resets_at = self._read_limit_screen(region)
        from_replay = False
        if banner and self.spec.provider == "gemini" and self._in_launch_replay():
            ok, resets_at = self._replay_cut_off(banner)
            if not ok:
                return
            from_replay = True
        if not menu:
            # A BANNER ON ITS OWN IS NOT PROOF OF A LIVE CUT-OFF. It is ordinary
            # output that stays in view — and is re-emitted by every frame
            # repaint — long after the agent has been resumed off it, so the
            # moment `recheck_limit` clears the latch the very next burst
            # re-latched on the SAME historical line. Worse, `parse_reset_clock`
            # then dated it a full day out (its clock had just passed), which
            # muted the agent's "?" chime for a day and later typed a stray
            # Continue into an agent that was working fine. Observed twice on
            # 2026-08-04: re-BLOCKED 13 s after RESUMED, and again five hours
            # later, both with `resets` exactly 24 h ahead of the latch.
            #
            # The menu is the discriminator, exactly as in `recheck_limit`: it
            # is torn down on the resume, and on a genuine cut-off it renders
            # directly BELOW the banner, so it is in view whenever the banner
            # is. A new cut-off also states a different clock — successive
            # 5-hour windows never end at the same wall time — so an identical
            # line with no menu can only be the echo of one we already handled.
            if not banner or banner == self._limit_last_banner:
                if banner:
                    self._note_limit_skip("no menu, and the same banner line "
                                          "already produced a latch", banner)
                return
        self._limit_blocked = True
        self._limit_at = time.time()
        self._limit_cut_off_at = self._limit_at   # seen as it happened
        self._limit_last_banner = banner
        self._limit_banner = banner
        self._limit_window = window
        # The reset clock lives in the banner, not the menu, so it may be
        # absent (the banner can have scrolled while the menu is still up).
        # None simply means "no network-free due time" — the watchdog then
        # leaves this one to the plan-usage edge rather than guessing.
        self._limit_resets_at = resets_at
        # A cut-off read off a launch REPLAY is a startup recovery in every
        # sense but the source, so it answers to the toggle that owns those.
        self._limit_from_startup = from_replay
        self.limit_blocked_changed.emit(True)

    def _in_launch_replay(self) -> bool:
        """Whether output arriving now can still be a previous session being
        redrawn rather than anything happening live.

        Deliberately generous. A quota message that is genuinely live inside
        this window is handled correctly anyway — `_replay_cut_off` only makes
        the reset EARLIER, and an early probe that turns out to be premature
        draws a fresh refusal whose countdown is exact. Being late is the
        expensive mistake here; being early costs one message.
        """
        return (self._session_started > 0.0
                and time.time() - self._session_started <= LIMIT_REPLAY_S)

    def _replay_cut_off(self, banner: str) -> tuple:
        """`(latch_it, resets_at)` for a quota message drawn during the replay.

        This is Gemini's startup recovery, and it exists because the two
        mechanisms that rescue a Claude agent both refuse to work here: the live
        latch is gone (it is never persisted) and there is no conversation on
        disk to reconstruct it from. What agy DOES have is the message itself,
        still on screen, because `--continue` redraws the conversation it ended
        on. Verified live (2026-08-09, Agent 10): the cut-off latched 5 s after
        launch, entirely off the replay.

        TWO corrections make that usable.

        (1) THE COUNTDOWN IS STALE, and by an unknowable amount. "Resets in
        1h39m56s" is frozen at the moment it was printed, so resolving it
        against the clock at launch dates the reset late by EXACTLY THE AGE OF
        THE MESSAGE. Measured end to end on 2026-08-09: agy printed it at ~17:25
        (reopening ~19:05), the app was restarted at 20:15:02 and dated the
        reset 21:54, and the nudge went out at 21:57:58 — 2h50m of an idle agent
        for a quota that had been back for nearly three hours. There is nothing
        on screen to date it with, so the countdown is DISCARDED and the agent
        is probed straight away. That is safe precisely because a refusal is
        self-correcting: a quota that is still spent answers with a WHOLE NEW
        message, and `recheck_limit` adopts its countdown, which is exact
        because it was printed just now. So the cost of guessing early is one
        message; the cost of guessing late is hours of an idle agent.

        (2) IT MUST BE THE LAST THING THAT HAPPENED. A message with real work
        after it belongs to a cut-off the conversation already recovered from,
        and continuing that would interrupt finished work — the same rule
        `transcripts.ended_on_limit` applies to a Claude transcript, read off
        the screen instead. The agent's own prompt and footer sit below it, so
        the window is a handful of lines rather than one.
        """
        tail = self._tail_lines(LIMIT_REPLAY_TAIL_LINES, skip_blank=True)
        if not gemini_banner_line(tail):
            # Judged history — so ARM THE ECHO GUARD with it, or the judgement
            # only holds until the launch window lapses: the message stays in
            # the rolling tail long after, and the next repaint would read it as
            # live and latch it with its stale countdown. This is where the old
            # `_seed_limit_history` belonged all along — applied to a message we
            # have positively decided about, rather than to whatever happened to
            # be on screen at the readiness edge.
            self._limit_last_banner = banner
            self._note_limit_skip("a replayed quota message with work after "
                                  "it (the conversation carried on)", banner)
            return False, None
        return True, time.time()

    def _read_limit_screen(self, region: str) -> tuple:
        """`(menu, banner, window, resets_at)` for whichever CLI this agent is.

        The two providers cut an agent off in different shapes and only the
        latch itself is common, so the reading is the one place they diverge —
        see the Gemini section of `limit_banner` for what each difference costs.
        Two of them show up right here:

          * agy renders NO menu, so `menu` is always False and a Gemini cut-off
            always goes through the identity guard below. That is the intended
            outcome, not a degradation: the guard is what the weak scrollback
            signal needs, and agy's message carries a far sharper identity than
            Claude's (a per-message Error ID rather than a wall clock).
          * its countdown is RELATIVE, so it must be resolved to an epoch here,
            once, at the moment the message is read. It is parsed from the
            joined banner rather than the whole region for the same reason the
            identity is: the countdown sits on a continuation row, and reading
            the region would let an older message's countdown win.

        There is no `weekly` ambiguity on the Gemini side either — a duration is
        equally correct for a window days out — so its window is simply
        "quota", which `_check_limit_resets` treats as ordinary and due on its
        own clock.
        """
        if self.spec.provider == "gemini":
            banner = gemini_banner_line(region)
            return False, banner, ("quota" if banner else ""), \
                gemini_reset_at(banner)
        banner = banner_line(region)
        return (bool(LIMIT_MENU_RE.search(region)), banner,
                banner_window(banner), parse_reset_clock(region))

    def _note_limit_skip(self, reason: str, banner: str = "") -> None:
        """Record that a plan-limit banner was ON SCREEN and nothing latched.

        Everything AFTER a latch is audited (BLOCKED / NUDGE / WAIT / PHANTOM
        / RESUMED), but the decision NOT to latch was invisible, and that is
        the one that strands work: a cut-off nobody saw looks exactly like a
        cut-off that never happened. It cost a real diagnosis — an agent was
        found sitting on a spent limit hours later with no trace anywhere of
        why the live scrape had passed over it, and the cause could only be
        narrowed by elimination, never identified.

        Bounded twice over, because `_scrape_limit` runs on EVERY output burst
        and this must not become a log flood: it says nothing at all unless a
        banner is actually visible (the overwhelmingly common case is that
        there is none), and it repeats only when the (reason, banner) pair
        CHANGES, so a state that persists across hundreds of repaints of the
        same frame is written once. `_limit_last_skip` is reset by
        start/restart, so a fresh screen reports again.
        """
        try:
            if not banner:
                # the SAME window AND the same reading the detector used, or
                # this reports "nothing to see" for exactly the frames it is
                # meant to explain
                banner = self._read_limit_screen(
                    self._tail_lines(40, skip_blank=True))[1]
            if not banner:
                return
            state = (reason, banner)
            if state == self._limit_last_skip:
                return
            self._limit_last_skip = state
            if self.audit is not None:
                self.audit(f"LIMIT NO-LATCH agent={self.spec.name} ({reason}) "
                           f"banner={banner[:80]!r}")
        except Exception:
            pass    # forensics must never break the feature they observe

    def mark_limit_blocked(self, resets_at: float | None,
                           from_startup: bool = True,
                           cut_off_at: float = 0.0, window: str = "",
                           banner: str = "") -> None:
        """Seed the latch from OUTSIDE the live screen — startup recovery,
        which reconstructs the cut-off from the transcript on disk because the
        screen shows a replayed conversation rather than a live banner.

        `from_startup` records which toggle owns this latch, so the two
        preferences stay independent: a cut-off found at startup is resumed
        only if startup recovery is on, one observed live only if
        resume-on-reset is on.

        `cut_off_at` is when the limit ACTUALLY stopped the agent (the
        transcript record's own timestamp), which is what the ledger files and
        what the user is shown — not `time.time()`, which here is only "when
        this process got round to looking".
        """
        if self._limit_blocked:
            return
        self._limit_blocked = True
        self._limit_at = time.time()
        self._limit_cut_off_at = cut_off_at or self._limit_at
        self._limit_resets_at = resets_at
        self._limit_from_startup = bool(from_startup)
        self._limit_window = window
        self._limit_banner = banner
        # ARM THE ECHO GUARD, exactly as a live latch does. Without this a
        # disk-recovered latch left `_limit_last_banner` empty, so the moment
        # `recheck_limit` cleared the latch on a successful resume, the very
        # next output burst re-latched on the SAME banner still sitting on
        # screen — and `parse_reset_clock` dated it a full day out, because
        # that clock had just passed. Observed live on 2026-08-07: RESUMED at
        # 22:29:42, BLOCKED again at 22:29:43 with a reset 24 h ahead, which
        # mutes the agent's chime for a day and later types a stray Continue
        # into an agent that is working fine.
        #
        # Comparing the LINE works because both sources normalize through the
        # same `limit_banner.banner_line`: the transcript record and the live
        # re-latch above carried byte-identical text. (A banner the TUI wrapped
        # across two rows would not match — that is equally true of a live
        # latch today, and is not made worse here.)
        if banner:
            self._limit_last_banner = banner
        self.limit_blocked_changed.emit(True)

    def limit_window(self) -> str:
        """Which limit window stopped this agent ("session" / "weekly" / ...).

        A weekly cut-off must not be resumed on its banner's clock alone: that
        clock is a bare wall time for a reset that can be days out. See
        `limit_banner.banner_window`.
        """
        return self._limit_window

    def limit_cut_off_at(self) -> float:
        """When the limit actually stopped this agent (epoch), as opposed to
        `limit_latched_at()` — when AI Hive noticed."""
        return self._limit_cut_off_at

    def limit_banner_text(self) -> str:
        """The banner line that evidenced this cut-off (for the ledger)."""
        return self._limit_banner

    def limit_summary(self) -> str:
        """One line describing this agent's cut-off, for the marker's tooltip.
        "" when it is not cut off.

        Lives on the model rather than in either view because both the card
        header and the sidebar row show the same thing, and every fact in it
        (when it stopped, when the window reopens, how many resumes have been
        tried) is state only the agent holds.
        """
        if not self._limit_blocked:
            return ""
        parts = ["Stopped by the usage limit"]
        if self._limit_cut_off_at:
            parts[0] += time.strftime(
                " on %Y-%m-%d at %H:%M", time.localtime(self._limit_cut_off_at))
        if self._limit_resets_at:
            parts.append(time.strftime("limit resets %H:%M",
                                       time.localtime(self._limit_resets_at)))
        else:
            parts.append("reset time unknown")
        if self._limit_tries:
            parts.append(f"auto-continue tried {self._limit_tries}x")
        else:
            parts.append("waiting to auto-continue")
        return " · ".join(parts)

    def limit_from_startup(self) -> bool:
        """True when this latch was recovered from disk rather than seen live."""
        return self._limit_from_startup

    def limit_latched_at(self) -> float:
        """When this cut-off was noticed (epoch). Backstop for a latch whose
        reset time is unknown — a 5-hour window cannot outlast it forever."""
        return self._limit_at

    def set_limit_reset(self, at: float | None) -> None:
        """Supply a reset time the SCREEN could not give.

        The menu ("Stop and wait for limit to reset") carries no clock, and the
        banner that does may have scrolled out of the region we search — so a
        latch can end up with no due time, which strands the network-free
        watchdog and leaves only the flaky usage API to trigger it. Observed
        live: an agent cut off at 05:10 with `resets=unknown` sat for five
        hours. The account-level reading knows the answer even when the screen
        doesn't, so it is filled in from there.
        """
        if self._limit_blocked and self._limit_resets_at is None and at:
            self._limit_resets_at = at

    def prompt_ready(self) -> bool:
        """True once the TUI's input prompt is live and will accept typing."""
        return self._prompt_ready

    def _set_prompt_ready(self, ready: bool) -> None:
        """Flip readiness and announce a real change (never a repeat).

        The only consumer is the card's boot veil, so this stays edge-only for
        the same reason `activity_changed` does: readiness is re-armed on every
        (re)start and settled once per launch, and a signal per output burst
        would be pure churn."""
        if bool(ready) is self._prompt_ready:
            return
        self._prompt_ready = bool(ready)
        self.prompt_ready_changed.emit(self._prompt_ready)

    def clear_limit_block(self) -> None:
        """Forget the latched cut-off (it resumed, or it restarted).

        `_limit_last_banner` deliberately SURVIVES this: it is the only thing
        stopping the banner still on screen from instantly re-latching the
        agent we just resumed. It is reset by `start`/`restart` alone, where
        the screen genuinely starts over.

        Emits the FALLING edge (guarded, so start()/restart() calling this
        on an agent that was never blocked stays silent) — the card header's
        hourglass and the sidebar's blocked-count badge are both signal-driven,
        not polled, and without this emit they never noticed a resume; the
        marker used to sit there forever after auto-continue or a manual
        restart had already put the agent back to work.
        """
        was_blocked = self._limit_blocked
        self._limit_blocked = False
        self._limit_resets_at = None
        self._limit_tries = 0
        self._limit_last_try = 0.0
        self._limit_from_startup = False
        self._limit_at = 0.0
        self._limit_cut_off_at = 0.0
        self._limit_window = ""
        self._limit_banner = ""
        if was_blocked:
            self.limit_blocked_changed.emit(False)

    def note_limit_attempt(self) -> None:
        """Record that we just tried to resume this agent."""
        self._limit_tries += 1
        self._limit_last_try = time.time()

    def limit_attempts(self) -> int:
        return self._limit_tries

    def limit_retry_ready(self, retry_after_s: float, max_tries: int) -> bool:
        """Whether a resume may be (re)tried now. A nudge can land while the
        window is still shut — clock skew, or a reset that isn't exactly on the
        stated minute — so one attempt is not enough; but it must not become a
        Continue every 60 s forever either."""
        if self._limit_tries >= max_tries:
            return False
        return (self._limit_tries == 0
                or time.time() - self._limit_last_try >= retry_after_s)

    def recheck_limit(self) -> bool:
        """After a resume attempt: is the agent STILL parked? True means retry.

        Keys on the MENU alone, deliberately — it is the interactive element
        that actually blocks input, so it is present exactly while the agent is
        stuck and gone the moment it is going again. The banner must NOT be
        used here: it is scrollback, so it lingers in the tail well after a
        successful resume and would report a false "still blocked" forever.

        agy has no menu to key on, so the Gemini half asks the same question a
        different way: a quota that is still spent answers the nudge with a
        FRESH message (its own countdown and Error ID), so a banner that differs
        from the one we latched on is the proof, and the unchanged one still
        sitting in the scrollback is not. Same discriminator as the latch's
        identity guard, read from the other direction. The newer message also
        REPLACES the latched reset time: its countdown is the current answer to
        "when can this be tried again", and keeping the spent one would have the
        watchdog retry immediately and burn the whole budget in a minute.
        """
        if not self._limit_blocked:
            return False
        if self.spec.provider == "gemini":
            banner = gemini_banner_line(self._tail_lines(40, skip_blank=True))
            if banner and banner != self._limit_last_banner:
                self._limit_last_banner = banner
                self._limit_banner = banner
                resets_at = gemini_reset_at(banner)
                if resets_at is not None:
                    self._limit_resets_at = resets_at
                return True
            self.clear_limit_block()
            return False
        # raw lines on purpose — see _tail_lines
        region = self._tail_lines(40)
        if region and LIMIT_MENU_RE.search(region):
            return True
        self.clear_limit_block()
        return False

    # -------------------------------------------------------------- slots ---

    def _emit(self, stream: str, text: str) -> None:
        self.log.append((stream, text))
        self.output_segment.emit(stream, text)

    def _on_output(self, stream: str, text: str) -> None:
        self._mark_busy()  # real process output => the agent is working
        self._emit(stream, text)

    def _on_pty_output(self, _stream: str, text: str) -> None:
        self._mark_busy()  # streaming VT output => the agent is working
        # keep a bounded raw tail so a freshly created card can rebuild the
        # screen; the live TerminalView is fed directly via the signal
        self._pty_buffer.append(text)
        self._pty_bytes += len(text)
        self._pty_total += len(text)
        while self._pty_bytes > PTY_BUFFER_CAP and len(self._pty_buffer) > 1:
            dropped = self._pty_buffer.pop(0)
            self._pty_bytes -= len(dropped)
            self._pty_dropped += len(dropped)
        # rolling escape-stripped tail for waiting-for-input detection (the idle
        # timer scans it once output settles — see _screen_waiting)
        self._screen_tail = (self._screen_tail + _CSI_RE.sub("", text))[-4000:]
        # latch a plan-limit cut-off the INSTANT it is drawn — see _scrape_limit
        # for why this must not wait for the idle-timer settle
        if not self._limit_blocked:
            self._scrape_limit()
        # readiness to receive a task. For Claude the signal is the input-box
        # footer: the folder-trust dialog also enables bracketed paste (and
        # does NOT disable it on dismissal — both verified live), so 2004h
        # alone would deliver the task into the dialog. The footer renders
        # exactly when the prompt is truly interactive, including after trust
        # dialogs and resume replays. Other TUIs (pty PowerShell via
        # PSReadLine, agy) keep the paste-enable signal.
        #
        # CRITICAL: match the whole footer-hint FAMILY, not just "? for
        # shortcuts". That hint is only ONE member of a rotating set — the
        # footer may instead be showing "auto mode on(shift+tab to cycle) ...
        # <- for agents" — so keying on it alone leaves an agent permanently
        # "not ready" whenever the rotation sits elsewhere. Verified live: a
        # restored agent parked on a spent plan limit sat un-nudged through
        # repeated watchdog ticks for exactly this reason, and a task
        # delivered to it would have hung in _pending_task forever too. If a
        # future CLI renames these, this tuple is the one place to fix.
        if not self._prompt_ready:
            if self.spec.provider == "claude":
                self._ready_tail = (self._ready_tail
                                    + _CSI_RE.sub("", text))[-600:]
                ready = self._has_ready_hint(self._ready_tail)
            else:
                ready = "\x1b[?2004h" in text
            if ready:
                self._became_prompt_ready()
        self.pty_output.emit(text)

    @staticmethod
    def _has_ready_hint(text: str) -> bool:
        """Whether `text` shows Claude's input-box footer, in any of its
        rotating forms.

        Matched with ALL WHITESPACE REMOVED from both sides, which is not
        cosmetic: the classic main-screen renderer (`tui: "default"`, the one
        AI Hive selects to own the scrollback) lays the footer out by MOVING
        THE CURSOR between segments instead of emitting literal spaces. Strip
        the escapes -- which is exactly what `_ready_tail`/`_screen_tail` do --
        and "? for shortcuts" arrives as "?forshortcuts", so a spaced match
        never fires. Measured on a live classic-renderer session: the hint was
        present and correctly spaced on the rendered pyte screen from the first
        frame, and matched the escape-stripped stream NEVER. Since readiness
        gates task delivery, that silently parks every first task in
        `_pending_task` forever and leaves the BootVeil up until its timeout.
        Despacing both sides is a superset of the old comparison, so the
        alt-screen renderer keeps matching exactly as before."""
        low = _despace(text)
        return any(h in low for h in _READY_HINTS_DESPACED)

    def _became_prompt_ready(self) -> None:
        """The TUI's prompt just went live: announce it and release any task
        that was waiting for exactly this."""
        self._set_prompt_ready(True)
        if self._pending_task is not None and self.worker.is_running():
            task, self._pending_task = self._pending_task, None
            self._write_task_to_pty(task)

    def _set_status(self, status: AgentStatus) -> None:
        if status is not self.status:
            self.status = status
            # leaving the live states ends any "working" pulse immediately,
            # even if the idle timer hasn't fired yet (stop/crash/exit)
            if status not in (AgentStatus.RUNNING, AgentStatus.STARTING):
                self._idle_timer.stop()
                if self._busy:
                    self._busy = False
                    self.activity_changed.emit(False)
                self._reset_waiting()  # a dead/stopped agent isn't waiting
                self._reset_bg_shell()  # ...nor waiting on a shell to finish
            self.status_changed.emit(status)

    def _on_worker_state(self, state: WorkerState) -> None:
        if state is WorkerState.STARTING:
            self._set_status(AgentStatus.STARTING)
        elif state is WorkerState.RUNNING:
            self._set_status(AgentStatus.RUNNING)
        elif state is WorkerState.STOPPING:
            self._set_status(AgentStatus.STOPPING)
        elif state is WorkerState.IDLE:
            self._set_status(AgentStatus.IDLE)
        # DEAD is refined by _on_finished/_on_failed

    def _on_finished(self, code: int, crashed: bool) -> None:
        # A resume (--continue) launch that dies before the interactive prompt
        # ever came up means there was no conversation to continue: Claude Code
        # prints "No conversation found to continue" and exits immediately,
        # leaving a black, dead terminal. Relaunch once, fresh, so the user
        # always gets a live terminal — it keeps its cwd, MCP tools and the
        # shared board, so it still has the prior work's context.
        if (self._resume_attempt and self.is_pty and not self._prompt_ready
                and not self._resume_fallback_done and not self._disposing
                and self.status is not AgentStatus.STOPPING):
            self._resume_fallback_done = True
            self._resume_attempt = False
            self.spec.resume = False
            self.notice("[no earlier session to resume; starting fresh]")
            self.start()
            return
        if crashed:
            self._emit(STREAM_SYSTEM, "[process crashed]\n")
            self._set_status(AgentStatus.CRASHED)
        else:
            self._emit(STREAM_SYSTEM, f"[exited with code {code}]\n")
            self._set_status(AgentStatus.EXITED_OK if code == 0
                             else AgentStatus.EXITED_ERR)

    def _on_failed(self, msg: str) -> None:
        self._emit(STREAM_SYSTEM, f"[{msg}]\n")
        self._set_status(AgentStatus.FAILED)
