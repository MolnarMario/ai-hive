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
        self.assignment = AssignmentState.IDLE  # orchestration lifecycle
        self.auto_created = False           # spawned by the orchestrator (#9)
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
        self._waiting = False         # settled on a prompt/question for the user
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
        self._set_waiting(False)
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
        self._set_waiting(False)
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
        the name custom so an orchestrator retask (set_role) never clobbers it."""
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
        """Give this agent a task to work on (orchestrator/reassign path).

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
        """True when the agent has settled on a prompt/question awaiting the
        user (a permission prompt or an interactive option menu). Derived from
        the settled screen; any fresh output clears it."""
        return self._waiting

    def _set_waiting(self, waiting: bool) -> None:
        if waiting != self._waiting:
            self._waiting = waiting
            self.waiting_changed.emit(waiting)

    def _mark_busy(self) -> None:
        # only a live agent can be working; guard on status (not worker state)
        # so this is unit-testable without a real child process
        if self.status not in (AgentStatus.RUNNING, AgentStatus.STARTING):
            return
        self._set_waiting(False)  # producing output => not waiting on the user
        if not self._busy:
            self._busy = True
            self.activity_changed.emit(True)
        self._idle_timer.start()  # (re)arm; fires once output falls quiet

    def _on_idle_timeout(self) -> None:
        if self._busy:
            self._busy = False
            self.activity_changed.emit(False)
        # the screen has settled (2 s quiet) — is it a prompt awaiting the user?
        self._set_waiting(self._screen_waiting())

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
        # readiness to receive a task. For Claude the ONLY reliable signal is
        # the input-box footer ("? for shortcuts"): the folder-trust dialog
        # also enables bracketed paste (and does NOT disable it on dismissal
        # — both verified live), so 2004h alone would deliver the task into
        # the dialog. The footer renders exactly when the prompt is truly
        # interactive, including after trust dialogs and resume replays.
        # Other TUIs (pty PowerShell via PSReadLine, agy) keep the
        # paste-enable signal.
        if not self._prompt_ready:
            if self.spec.provider == "claude":
                self._ready_tail = (self._ready_tail
                                    + _CSI_RE.sub("", text))[-600:]
                ready = "? for shortcuts" in self._ready_tail.lower()
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
                self._set_waiting(False)  # a dead/stopped agent isn't waiting
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
