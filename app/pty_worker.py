"""ConPTY execution backend: a real Windows pseudo-console per terminal.

Where ProcessWorker gives a line-oriented piped console, PtyWorker gives a
genuine terminal: child processes see a TTY, so interactive TUIs (Claude
Code, PSReadLine prompts, spinners) work, and Ctrl+C is a real interrupt.

Threading model: pywinpty reads are blocking, so one daemon reader thread
per PTY pumps chunks to the GUI thread via a queued signal; the worker
batches them on a 33 ms timer exactly like ProcessWorker. All control
(write/resize/kill) happens on the GUI thread — pywinpty's read and write
pipes are independent, so this split is safe.
"""

import os
import threading

from PySide6.QtCore import QObject, QTimer, Signal

from .process_worker import WinJob, WorkerState, _taskkill_tree, AgentSpec


def agent_environment() -> dict:
    """Child env for agents, with Claude Code's NESTED-SESSION markers
    removed. A claude launched with CLAUDECODE/CLAUDE_CODE_* in its env
    thinks it is a child of another claude session and SILENTLY DISABLES
    transcript persistence (verified live: same session, markers present ->
    no transcript ever written; markers stripped -> transcript + pinned
    --session-id honored). If AI Hive is ever launched from inside a Claude
    Code terminal, every agent conversation would otherwise be unsaved and
    unresumable."""
    env = dict(os.environ)
    for key in list(env):
        up = key.upper()
        if up.startswith(("CLAUDECODE", "CLAUDE_CODE", "CLAUDE_EFFORT")):
            env.pop(key)
    return env

try:
    from winpty import PtyProcess
    HAS_CONPTY = True
except ImportError:  # pragma: no cover - dependency always present on target
    PtyProcess = None
    HAS_CONPTY = False

FLUSH_INTERVAL_MS = 33
DEFAULT_ROWS = 30
DEFAULT_COLS = 100
CTRL_C = "\x03"


class _PtyReader(QObject):
    """Daemon thread pumping blocking PTY reads to the GUI thread."""

    chunk = Signal(str)
    eof = Signal()

    def __init__(self, proc):
        super().__init__()
        self._proc = proc
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="pty-reader")

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        try:
            while True:
                data = self._proc.read(4096)
                if not data:
                    break
                if isinstance(data, bytes):
                    data = data.decode("utf-8", "replace")
                self.chunk.emit(data)
        except (EOFError, OSError, RuntimeError):
            pass
        self.eof.emit()


class PtyWorker(QObject):
    """Same signal surface as ProcessWorker, backed by a pseudo-console."""

    started = Signal(int)            # pid
    output = Signal(str, str)        # stream ("pty"), decoded VT text
    finished = Signal(int, bool)     # exit code, crashed
    failed = Signal(str)
    state_changed = Signal(object)   # WorkerState

    def __init__(self, spec: AgentSpec, parent: QObject | None = None):
        super().__init__(parent)
        self.spec = spec
        self.state = WorkerState.IDLE
        self.exit_info: tuple[int, bool] | None = None
        self.rows = DEFAULT_ROWS
        self.cols = DEFAULT_COLS
        self._proc = None
        self._reader: _PtyReader | None = None
        self._job: WinJob | None = None
        self._gen = 0
        self._restart_pending = False
        self._pend: list[str] = []
        self._flush_timer = QTimer(self)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

    # ------------------------------------------------------------ control ---

    def start(self) -> None:
        if self.state in (WorkerState.STARTING, WorkerState.RUNNING,
                          WorkerState.STOPPING):
            return
        if not HAS_CONPTY:
            self._set_state(WorkerState.DEAD)
            self.failed.emit("pywinpty is not installed, so full-terminal "
                             "mode unavailable")
            return
        self._gen += 1
        self.exit_info = None
        self._pend = []
        self._set_state(WorkerState.STARTING)

        argv = [self.spec.program] + list(self.spec.effective_args())
        cmd = argv if len(argv) > 1 else argv[0]
        # spec.env carries per-agent launch markers (e.g. AIHIVE_AGENT_ID, which
        # attributes SessionStart-hook output to this exact agent). agent_env
        # strips the nested-session markers FIRST; spec.env is layered on top.
        env = agent_environment()
        if self.spec.env:
            env.update(self.spec.env)
        try:
            self._proc = PtyProcess.spawn(
                cmd, dimensions=(self.rows, self.cols),
                cwd=self.spec.cwd or None, env=env)
        except Exception as exc:  # FileNotFoundError, WinError, ...
            self._proc = None
            self._set_state(WorkerState.DEAD)
            self.failed.emit(f"failed to start: {self.spec.program} ({exc})")
            return

        self._job = WinJob()
        self._job.assign(self._proc.pid)
        self._reader = _PtyReader(self._proc)
        self._reader.chunk.connect(self._on_chunk)
        self._reader.eof.connect(self._on_eof)
        self._reader.start()
        self._set_state(WorkerState.RUNNING)
        self.started.emit(self._proc.pid)

    def write(self, data: str) -> bool:
        """Raw VT input (keystrokes) straight into the terminal."""
        if self.state is not WorkerState.RUNNING or self._proc is None:
            return False
        try:
            self._proc.write(data)
            return True
        except (OSError, EOFError, RuntimeError):
            return False

    def send_line(self, text: str) -> bool:
        return self.write(text + "\r")

    def resize(self, rows: int, cols: int) -> None:
        rows, cols = max(2, rows), max(10, cols)
        if (rows, cols) == (self.rows, self.cols):
            return
        self.rows, self.cols = rows, cols
        if self._proc is not None and self.state is WorkerState.RUNNING:
            try:
                self._proc.setwinsize(rows, cols)
            except (OSError, RuntimeError):
                pass

    def stop(self, grace_ms: int = 1200) -> None:
        """Ctrl+C first (a real interrupt now), tree kill after the grace."""
        if self.state not in (WorkerState.RUNNING, WorkerState.STARTING):
            return
        self._set_state(WorkerState.STOPPING)
        self.write_raw_unchecked(CTRL_C)
        gen = self._gen
        QTimer.singleShot(grace_ms, self, lambda: self._grace_kill(gen))

    def write_raw_unchecked(self, data: str) -> None:
        if self._proc is not None:
            try:
                self._proc.write(data)
            except (OSError, EOFError, RuntimeError):
                pass

    def kill(self) -> None:
        if self._proc is None or self.state in (WorkerState.IDLE,
                                                WorkerState.DEAD):
            return
        killed = self._job.terminate_tree() if self._job else False
        if not killed and getattr(self._proc, "pid", None):
            _taskkill_tree(self._proc.pid)
        try:
            self._proc.terminate(force=True)
        except (OSError, RuntimeError):
            pass

    def restart(self) -> None:
        if self.state in (WorkerState.RUNNING, WorkerState.STARTING,
                          WorkerState.STOPPING):
            self._restart_pending = True
            self._set_state(WorkerState.STOPPING)
            self.kill()
        else:
            self.start()

    def dispose(self) -> None:
        self._restart_pending = False
        if self._proc is None or self.state in (WorkerState.IDLE,
                                                WorkerState.DEAD):
            return
        self.kill()
        # reader thread sees EOF and exits on its own (daemon)

    def is_running(self) -> bool:
        return self.state in (WorkerState.STARTING, WorkerState.RUNNING)

    def pid(self) -> int | None:
        if self._proc is not None and self.is_running():
            return self._proc.pid
        return None

    def process(self):
        return self._proc

    def job_process_count(self) -> int:
        return self._job.process_count() if self._job else 0

    # -------------------------------------------------------------- slots ---

    def _set_state(self, state: WorkerState) -> None:
        if state is not self.state:
            self.state = state
            self.state_changed.emit(state)

    def _on_chunk(self, data: str) -> None:
        self._pend.append(data)
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _flush(self) -> None:
        if self._pend:
            text = "".join(self._pend)
            self._pend = []
            self.output.emit("pty", text)
        else:
            self._flush_timer.stop()

    def _grace_kill(self, gen: int) -> None:
        if gen == self._gen and self.state is WorkerState.STOPPING:
            self.kill()

    def _on_eof(self) -> None:
        self._flush()
        self._flush_timer.stop()
        code = 0
        if self._proc is not None:
            try:
                status = self._proc.exitstatus
                code = int(status) if status is not None else 0
            except (OSError, RuntimeError):
                code = 1
        if self._job:
            self._job.close()
            self._job = None
        self.exit_info = (code, False)
        self._set_state(WorkerState.DEAD)
        self.finished.emit(code, False)
        if self._restart_pending:
            self._restart_pending = False
            self._set_state(WorkerState.IDLE)
            self.start()
