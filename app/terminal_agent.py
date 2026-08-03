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
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

from .coordination import sanitize_text
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

_NUM_OPTION_RE = re.compile(r"(?m)^\s*[>❯❱│┃|]*\s*\d+\.\s+\S")
# a numbered option with a selection caret in front (optionally past box
# borders) — the highlighted row of a live menu, absent from plain prose lists
_OPTION_CARET_RE = re.compile(r"(?m)^[\s│┃|]*[>❯❱]\s*\d+\.\s+\S")

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
from .limit_banner import (LIMIT_HIT_RE, LIMIT_MENU_RE,  # noqa: F401
                           banner_reset_at, is_limit_screen, parse_reset_clock)


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
    limit_blocked_changed = Signal(bool)  # cut off by the plan limit (latched)

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
        self.assignment = AssignmentState.IDLE  # task-assignment lifecycle
        self.auto_created = False           # created with a task via spawn_worker
        self.autostart_on_restore = False  # set from persisted run state
        self.log: deque = deque(maxlen=LOG_CAP)  # line-mode segments
        self._pty_buffer: list[str] = []         # pty raw tail (for replay)
        self._pty_bytes = 0
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
        self._busy = False            # actively streaming output right now
        self._last_output_ts = 0.0    # walltime of the last output burst
        self._last_input_ts = 0.0     # walltime the user last sent keystrokes
        # latched "the plan limit cut this agent off" + the reset time its own
        # banner stated. Transient like the waiting flags — never persisted.
        self._limit_blocked = False
        self._limit_resets_at: float | None = None
        self._limit_tries = 0          # resume attempts since the cut-off
        self._limit_last_try = 0.0
        self._limit_from_startup = False   # recovered from disk vs seen live
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
        self._prompt_ready = False  # re-armed for the fresh TUI
        self._ready_tail = ""
        self._screen_tail = ""
        self._reset_waiting()
        self.clear_limit_block()
        self._submit_gen += 1  # invalidate any pending task-submit Enter
        self._resume_attempt = self.spec.resume  # for the fast-fail fallback
        # a NON-resume start is a new conversation, so it gets a new pinned
        # identity (rotating also avoids --session-id colliding with an
        # existing transcript); a resume keeps its pin
        if self.spec.provider == "claude" and not self.spec.resume:
            self.spec.session_id = str(uuid.uuid4())
        elif (self.spec.provider == "claude" and self.spec.resume
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
        if session_sync.transcript_exists(self.spec.cwd, self.spec.session_id):
            return
        if not session_sync.list_transcripts(self.spec.cwd):
            return  # unused folder: nothing to recover; fallback handles it
        exclude = set(self._sibling_sessions() if self._sibling_sessions else ())
        exclude.add(self.spec.session_id)
        candidate = session_sync.best_recovery_id(self.spec.cwd, exclude=exclude)
        if candidate:
            self.notice("— pinned conversation missing; recovering the most "
                        "recent one in this folder —")
            self.spec.session_id = candidate

    def stop(self) -> None:
        if self.worker.is_running():
            self._emit(STREAM_SYSTEM, "[stopping...]\n")
        self.worker.stop()

    def kill(self) -> None:
        self.worker.kill()

    def restart(self) -> None:
        self._prompt_ready = False
        self._ready_tail = ""
        self._screen_tail = ""
        self._reset_waiting()
        self.clear_limit_block()
        self._submit_gen += 1  # invalidate any pending task-submit Enter
        if self.spec.provider == "claude":  # deliberate fresh session
            self.spec.session_id = str(uuid.uuid4())
        self._session_started = time.time()
        if self.is_pty:
            self._pty_buffer = []
            self._pty_bytes = 0
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

    def pty_replay(self) -> str:
        return "".join(self._pty_buffer)

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
            self._emit(STREAM_SYSTEM, "[not running — press Start (▶)]\n")

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
                       f"[{program} is a full-screen terminal app — it won't "
                       "render in this line-mode console. Add it in a \"Full "
                       "terminal (interactive)\" card instead.]\n")

    def is_running(self) -> bool:
        return self.worker.is_running()

    def is_busy(self) -> bool:
        """True while the agent is actively producing output — the accurate
        'working' signal, as opposed to is_running() which stays True for an
        interactive process idling at its prompt."""
        return self._busy

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
            self.activity_changed.emit(False)
        # the screen has settled (2 s quiet) — is it a prompt awaiting the user?
        self._scrape_waiting = self._screen_waiting()
        self._emit_waiting()
        # ...and is this the plan-limit banner? Latch it NOW, while the frame is
        # current; by reset time the rolling tail no longer holds it.
        self._scrape_limit()

    def _screen_waiting(self) -> bool:
        # ground-truth on the drawn box; suppress for a mode that shows no
        # prompts. bypassPermissions skips ALL prompts; other modes (incl.
        # acceptEdits) still surface questions, so only bypass is suppressed.
        if self.spec.provider != "claude":
            return False
        if getattr(self.spec, "permission_mode", "") == "bypassPermissions":
            return False
        region = "\n".join(self._screen_tail.splitlines()[-18:])
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
        if self.spec.provider != "claude" or self._limit_blocked:
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
            return
        # Deliberately a WIDER region than _screen_waiting's last 18 lines.
        # That bound is right for a selection menu, which is anchored just
        # above the input box; the banner is NOT bottom-anchored — the options
        # menu, the input box and the footer all render below it, so 18 lines
        # can push it out of view on a full frame. Both patterns are specific
        # enough to search a wider window safely.
        region = "\n".join(self._screen_tail.splitlines()[-40:])
        if not is_limit_screen(region):
            return
        self._limit_blocked = True
        # The reset clock lives in the banner, not the menu, so it may be
        # absent (the banner can have scrolled while the menu is still up).
        # None simply means "no network-free due time" — the watchdog then
        # leaves this one to the plan-usage edge rather than guessing.
        self._limit_resets_at = parse_reset_clock(region)
        self._limit_from_startup = False
        self.limit_blocked_changed.emit(True)

    def mark_limit_blocked(self, resets_at: float | None,
                           from_startup: bool = True) -> None:
        """Seed the latch from OUTSIDE the live screen — startup recovery,
        which reconstructs the cut-off from the transcript on disk because the
        screen shows a replayed conversation rather than a live banner.

        `from_startup` records which toggle owns this latch, so the two
        preferences stay independent: a cut-off found at startup is resumed
        only if startup recovery is on, one observed live only if
        resume-on-reset is on.
        """
        if self._limit_blocked:
            return
        self._limit_blocked = True
        self._limit_resets_at = resets_at
        self._limit_from_startup = bool(from_startup)
        self.limit_blocked_changed.emit(True)

    def limit_from_startup(self) -> bool:
        """True when this latch was recovered from disk rather than seen live."""
        return self._limit_from_startup

    def prompt_ready(self) -> bool:
        """True once the TUI's input prompt is live and will accept typing."""
        return self._prompt_ready

    def clear_limit_block(self) -> None:
        """Forget the latched cut-off (it resumed, or it restarted)."""
        self._limit_blocked = False
        self._limit_resets_at = None
        self._limit_tries = 0
        self._limit_last_try = 0.0
        self._limit_from_startup = False

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
        """
        if not self._limit_blocked:
            return False
        region = "\n".join(self._screen_tail.splitlines()[-40:])
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
        while self._pty_bytes > PTY_BUFFER_CAP and len(self._pty_buffer) > 1:
            self._pty_bytes -= len(self._pty_buffer.pop(0))
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
                tail = self._ready_tail.lower()
                ready = any(h in tail for h in _CLAUDE_READY_HINTS)
            else:
                ready = "\x1b[?2004h" in text
            if ready:
                self._prompt_ready = True
                if self._pending_task is not None and self.worker.is_running():
                    task, self._pending_task = self._pending_task, None
                    self._write_task_to_pty(task)
        self.pty_output.emit(text)

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
            self.notice("— no earlier session to resume; starting fresh —")
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
