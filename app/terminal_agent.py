"""TerminalAgent: the MODEL object behind one terminal card.

Owned by the workspace model (via WorkspaceManager), never by a widget —
cards merely subscribe to its signals. That ownership is what guarantees
hidden workspaces keep executing and collecting logs: destroying or
rebuilding a view can never touch the process.
"""

import re
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

# strips escape sequences so on-screen TEXT can be matched: the raw stream
# positions words individually ("trust\x1b[20Gthis\x1b[25Gfolder"), so a
# phrase can never be matched against raw bytes
_CSI_RE = re.compile(r"\x1b\[[0-9;?<>=]*[@-~]|\x1b[()][AB0]|\x1b\][^\x07\x1b]*\x07?")


class TerminalAgent(QObject):
    output_segment = Signal(str, str)   # stream, text (line mode)
    pty_output = Signal(str)            # raw VT stream (pty mode)
    status_changed = Signal(object)     # AgentStatus
    task_changed = Signal(str)          # current-task text
    font_changed = Signal(int)          # per-agent console font px
    assignment_changed = Signal(object)  # AssignmentState
    role_changed = Signal(str)          # dynamic role/display name
    cleared = Signal()                  # console was cleared locally

    def __init__(self, spec: AgentSpec, parent: QObject | None = None):
        super().__init__(parent)
        self.id = uuid.uuid4().hex
        self.spec = spec
        self.is_pty = bool(spec.pty)
        self.status = AgentStatus.IDLE
        self.current_task = ""              # user/agent-set, feeds the board
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
        self._disposing = False       # teardown in progress (suppress retry)
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
        self._resume_attempt = self.spec.resume  # for the fast-fail fallback
        # a NON-resume start is a new conversation, so it gets a new pinned
        # identity (rotating also avoids --session-id colliding with an
        # existing transcript); a resume keeps its pin
        if self.spec.provider == "claude" and not self.spec.resume:
            self.spec.session_id = str(uuid.uuid4())
        self.worker.start()
        # resume is a one-shot restore aid: a manual restart later is fresh
        self.spec.resume = False

    def stop(self) -> None:
        if self.worker.is_running():
            self._emit(STREAM_SYSTEM, "[stopping...]\n")
        self.worker.stop()

    def kill(self) -> None:
        self.worker.kill()

    def restart(self) -> None:
        self._prompt_ready = False
        self._ready_tail = ""
        if self.spec.provider == "claude":  # deliberate fresh session
            self.spec.session_id = str(uuid.uuid4())
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
            self.current_task = text
            self.task_changed.emit(text)

    def set_assignment(self, state) -> None:
        if state is not self.assignment:
            self.assignment = state
            self.assignment_changed.emit(state)

    def set_role(self, name: str) -> None:
        """Dynamic role-based rename (Backend Architect, Testing Agent, …)."""
        name = sanitize_text(name or "").strip()
        if name and name != self.spec.name:
            self.spec.name = name
            self.spec.role = name
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
        # the task never runs
        QTimer.singleShot(350, lambda: self.worker.is_running()
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

    # -------------------------------------------------------------- slots ---

    def _emit(self, stream: str, text: str) -> None:
        self.log.append((stream, text))
        self.output_segment.emit(stream, text)

    def _on_output(self, stream: str, text: str) -> None:
        self._emit(stream, text)

    def _on_pty_output(self, _stream: str, text: str) -> None:
        # keep a bounded raw tail so a freshly created card can rebuild the
        # screen; the live TerminalView is fed directly via the signal
        self._pty_buffer.append(text)
        self._pty_bytes += len(text)
        while self._pty_bytes > PTY_BUFFER_CAP and len(self._pty_buffer) > 1:
            self._pty_bytes -= len(self._pty_buffer.pop(0))
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
