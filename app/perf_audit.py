"""Two `PERF` audit lines in session.log, so launches and stalls on the
user's real machine can be measured instead of guessed from an offscreen
copy. Every duration is whole milliseconds.

    PERF startup update=1312 imports=412 setup=45 cli_gate=0 window=8123
        show=120 autostart=700 recovery=710 total=12001

One line per launch (`StartupPhases`). `imports` runs at module level in
main.py, before the session store exists, so main.py carries it forward.
`update` stays 0: the launch no longer pulls from GitHub. `cli_gate` is 0
unless the user opted into CLI auto-update. `total` runs from the moment
Windows created the process to the end of `recovery`, so it also holds what
no phase names (interpreter start, the PySide6 import, loading the session
file). Two pieces of file work run on workers and show up in a phase only
when the GUI thread waits for them: the transcript backup, started in
`window` and joined at the top of `autostart`, and the limit scan's reads,
which `recovery` only hands off.

    PERF stall gap=312 paint=24 grid=255x66 feed=290 feed_chars=104857
        agent=Agent 3
    PERF stall suppressed=37 worst_gap=812

`StallProbe` runs a 50 ms timer that measures its own lateness. `gap` is
the time between two ticks. The GUI thread was busy for all of it except
the idle stretch before the block began, which is shorter than one tick,
so a gap over STALL_MS means at least STALL_MS - TICK_MS of busy time. The
line names the longest `TerminalView.feed` and `TerminalView.paintEvent`
inside the gap, which terminal_view records in module globals. `feed_chars` counts the
decoded text, which is the byte count for ASCII. A gap with no feed and no
paint was something else: a dialog's build, a save, or the machine sleeping.
At most STALL_LIMIT lines go out per STALL_WINDOW_S; the rest are counted
and one `suppressed` line reports them when the window ends or the probe
stops.

    PERF hang gap=4210 stack=threading.py:1132 _wait_for_tstate_lock < .. <
        app/lanes.py:140 _subprocess_git < .. < main.py:382 main

A gap says how long the GUI thread was gone, never where. On 0.48.0 the
user's window went "Not Responding" for 95 s after a launch with feed and
paint at about 20 ms each, so neither hook could name it, and a dozen 1 to
4 s gaps on other days were just as blank. So when the probe is given a
`dump_path`, every tick at least HANG_REARM_S after the last re-arms
`faulthandler.dump_traceback_later(HANG_S)`. If the GUI thread misses that
deadline, faulthandler's own C thread writes every thread's stack to the
dump file while the GUI thread is still stuck, without needing the GIL, so a
C call that holds it is caught too. A block of HANG_S or more always gets a
dump. One of HANG_S - HANG_REARM_S may, if it starts just before a re-arm
was due. The tick that ends the stall folds the GUI thread's
frames into one `PERF hang` line, innermost first: our own frames
(app/, main.py) in full and each run of library frames as "..". A stack
that is only `main.py ... main` was blocked inside Qt or Windows under
app.exec, or the machine slept. The other threads stay in the dump file,
which keeps every dump until it passes HANG_DUMP_MAX bytes and then only
the newest. A hang the user killed AI Hive in never gets its line, so the
next start moves a non-empty dump file to `<name>.1` rather than
truncating it: `<name>.1` holds the last run that had a dump, folded or
not. A re-arm is measured at
about 0.3 ms (it joins faulthandler's old thread and starts a new one),
which is why it runs once a second and not on every tick. At most
HANG_LIMIT hang lines go out per run, and a `suppressed` line counts the
rest when the probe stops.

Both are armed from main.py only, like `start_usage_polling`: the suite
shares `create_main_window` and must never write these lines or run the
timer unless a test arms it on purpose. Neither touches session.json, and a
failed write is swallowed.
"""

import contextlib
import faulthandler
import os
import re
import sys
import threading
import time

from PySide6.QtCore import QObject, QTimer

from .widgets import terminal_view as _tv

STARTUP_PHASES = ("update", "imports", "setup", "cli_gate", "window",
                  "show", "autostart", "recovery")

TICK_MS = 50
STALL_MS = 150
STALL_LIMIT = 5         # stall lines per window, the rest are counted
STALL_WINDOW_S = 60.0

HANG_S = 3.0            # faulthandler dumps after this long without a re-arm
HANG_REARM_S = 1.0
HANG_LIMIT = 30         # hang lines per run, the rest are counted
HANG_FRAMES = 16        # frames shown on one hang line, ".." included
HANG_DUMP_MAX = 1024 * 1024

# the repo root: frames under it, outside the venv, are our own code
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_THREAD_RE = re.compile(r"^(?:Current thread|Thread) 0x([0-9a-fA-F]+)")
_FRAME_RE = re.compile(r'^\s+File "(.*)", line (\d+) in (.*)$')


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _own_path(path: str, root: str) -> str:
    """`path` relative to `root` with forward slashes when it is our own
    code, else "". A pseudo-path like "<frozen importlib._bootstrap>" is
    never ours, though relpath would place it under the cwd."""
    if not os.path.isabs(path):
        return ""
    # faulthandler writes each non-ASCII character as an escape ("\xe9"),
    # which reads as a path separator here, so under a root with one the
    # dumped path only matches the root spelled the same way
    dumped = root.encode("ascii", "backslashreplace").decode("ascii")
    if dumped != root and os.path.normcase(path).startswith(
            os.path.normcase(dumped)):
        path = root + path[len(dumped):]
    try:
        rel = os.path.relpath(path, root)
    except ValueError:          # another drive
        return ""
    rel = rel.replace("\\", "/")
    if rel.startswith("../") or rel == ".." or rel.startswith(".venv/"):
        return ""
    return rel


def gui_stack(dump: str, ident: int, root: str = _ROOT) -> str:
    """The stack of thread `ident` in the newest faulthandler dump in
    `dump`, as one line, innermost frame first: "file:line func" joined by
    " < ". Our own frames keep their path under `root`; a run of other
    frames after the innermost shows as "..". "" when the dump has no frame
    for that thread."""
    frames: list[tuple[str, str, str]] = []
    current = None
    for line in dump.splitlines():
        head = _THREAD_RE.match(line)
        if head:
            current = int(head.group(1), 16) == ident
            if current:
                frames = []     # a newer dump replaces an older one
            continue
        frame = _FRAME_RE.match(line) if current else None
        if frame:
            frames.append(frame.groups())
    parts: list[str] = []
    for i, (path, lineno, func) in enumerate(frames):
        own = _own_path(path, root)
        if i and not own:
            if parts[-1] != "..":
                parts.append("..")
            continue
        parts.append(f"{own or os.path.basename(path)}:{lineno} {func}")
    if len(parts) > HANG_FRAMES:
        parts = parts[:HANG_FRAMES - 1]
        if parts[-1] != "..":
            parts.append("..")
    return " < ".join(parts)


def process_age_s() -> float | None:
    """Seconds since Windows created this process, or None off Windows or
    when the call fails."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")   # its own copy: argtypes stay here
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        k32.GetProcessTimes.argtypes = (
            [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4)
        k32.GetProcessTimes.restype = wintypes.BOOL
        created, exited, kernel, user, now = (wintypes.FILETIME()
                                              for _ in range(5))
        if not k32.GetProcessTimes(
                k32.GetCurrentProcess(), ctypes.byref(created),
                ctypes.byref(exited), ctypes.byref(kernel),
                ctypes.byref(user)):
            return None
        k32.GetSystemTimePreciseAsFileTime(ctypes.byref(now))

        def ticks(ft):  # 100 ns units
            return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
        return max(0.0, (ticks(now) - ticks(created)) / 1e7)
    except Exception:  # noqa: BLE001 - a missing total beats a dead launch
        return None


class StartupPhases:
    """The `PERF startup` line, one phase at a time."""

    def __init__(self, started: float, **carried: float):
        self.started = started   # perf_counter at the top of main.py
        self.seconds = dict.fromkeys(STARTUP_PHASES, 0.0)
        self.seconds.update(carried)

    @contextlib.contextmanager
    def measure(self, phase: str):
        t = time.perf_counter()
        try:
            yield
        finally:
            self.seconds[phase] = time.perf_counter() - t

    def line(self, total_s: float | None = None) -> str:
        if total_s is None:
            total_s = process_age_s()
        if total_s is None:
            total_s = time.perf_counter() - self.started
        parts = [f"{p}={_ms(self.seconds[p])}" for p in STARTUP_PHASES]
        return f"PERF startup {' '.join(parts)} total={_ms(total_s)}"

    def write(self, store) -> None:
        try:
            store.audit(self.line())
        except Exception:  # noqa: BLE001 - forensics, never fatal
            pass


class StallProbe(QObject):
    """The `PERF stall` lines, and the `PERF hang` lines when `dump_path`
    names a file for faulthandler's dumps. `name_of(view)` names the agent
    a TerminalView belongs to. `clock` is for tests that drive `_tick` by
    hand. `hang_s` and `rearm_s` are for tests that cannot wait seconds."""

    def __init__(self, store, name_of, parent=None, clock=time.perf_counter,
                 dump_path: str = "", hang_s: float = HANG_S,
                 rearm_s: float = HANG_REARM_S):
        super().__init__(parent)
        self._store = store
        self._name_of = name_of
        self._clock = clock
        self._last = 0.0
        self._window_start = float("-inf")
        self._written = 0
        self._suppressed = 0
        self._worst_suppressed = 0.0
        self._dump_path = dump_path
        self._hang_s = hang_s
        self._rearm_s = rearm_s
        self._dump = None           # the open dump file while armed
        self._dump_seen = 0         # bytes of it already folded
        self._armed_at = 0.0
        self._gui_ident = 0
        self._hangs = 0
        self._hangs_suppressed = 0
        self._timer = QTimer(self)
        self._timer.setObjectName("perfStallTimer")
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._reset_max()
        self._last = self._clock()
        self._open_dump()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        if self._suppressed:
            self._flush_suppressed()
        self._close_dump()

    def is_running(self) -> bool:
        return self._timer.isActive()

    def _tick(self) -> None:
        # 20 times a second for the life of the process: a clock read, a
        # compare and four stores, unless something stalled. Once a second
        # also a re-arm (about 0.3 ms) when the hang dump is on.
        now = self._clock()
        gap = now - self._last
        self._last = now
        stalled = gap > STALL_MS / 1000
        if self._dump is not None and (
                stalled or now - self._armed_at >= self._rearm_s):
            # first: re-arming waits for a dump still being written
            self._arm(now, gap)
        if stalled:
            self._on_stall(gap, now)
            if self._dump is not None:
                self._fold_hang(gap)
        elif (self._suppressed
              and now - self._window_start >= STALL_WINDOW_S):
            self._flush_suppressed()
        self._reset_max()

    # ---- the hang dump ---------------------------------------------------

    def _open_dump(self) -> None:
        if not self._dump_path or self._dump is not None:
            return
        try:
            # an earlier run's dumps, and one nobody folded is a hang AI
            # Hive was killed in: keep them
            if os.path.getsize(self._dump_path) > 0:
                os.replace(self._dump_path, self._dump_path + ".1")
        except OSError:
            pass
        try:
            # append: faulthandler writes through the descriptor, and after
            # a truncate the next dump must land at the start again
            self._dump = open(self._dump_path, "ab")
            self._dump_seen = os.fstat(self._dump.fileno()).st_size
        except OSError:
            self._dump = None
            return
        self._gui_ident = threading.get_ident()
        self._arm(self._clock())

    def _arm(self, now: float, gap: float = 0.0) -> None:
        """`gap` is the stall the calling tick ends, for the fold a failed
        re-arm makes: the tick has already moved `_last` up to now."""
        self._armed_at = now
        try:
            faulthandler.dump_traceback_later(self._hang_s, file=self._dump)
        except (OSError, ValueError, RuntimeError):
            self._close_dump(gap)

    def _close_dump(self, gap: float | None = None) -> None:
        if self._dump is None:
            return
        with contextlib.suppress(Exception):
            faulthandler.cancel_dump_traceback_later()
        with contextlib.suppress(Exception):
            # a dump no tick has folded yet: the block ran up to now
            self._fold_hang(self._clock() - self._last if gap is None
                            else gap)
        with contextlib.suppress(Exception):
            self._dump.close()
        self._dump = None
        if self._hangs_suppressed:
            self._audit(f"PERF hang suppressed={self._hangs_suppressed}")
            self._hangs_suppressed = 0

    def _fold_hang(self, gap: float) -> None:
        """One `PERF hang` line for a dump written since the last fold."""
        try:
            fd = self._dump.fileno()
            size = os.fstat(fd).st_size
            if size <= self._dump_seen:
                return
            with open(self._dump_path, "rb") as fh:
                fh.seek(self._dump_seen)
                new = fh.read()
            self._dump_seen = size
            if size > HANG_DUMP_MAX:
                # keep only the dump just read. Nothing writes meanwhile:
                # the tick re-armed the watchdog a moment ago.
                os.ftruncate(fd, 0)
                os.write(fd, new)
                self._dump_seen = len(new)
        except (OSError, ValueError):
            return
        text = new.decode("utf-8", "replace")
        if self._hangs >= HANG_LIMIT:
            self._hangs_suppressed += 1
            return
        self._hangs += 1
        stack = gui_stack(text, self._gui_ident) or "?"
        self._audit(f"PERF hang gap={_ms(gap)} stack={stack}")

    @staticmethod
    def _reset_max() -> None:
        _tv._perf_feed_s = 0.0
        _tv._perf_feed_view = None
        _tv._perf_paint_s = 0.0
        _tv._perf_paint_view = None

    def _on_stall(self, gap: float, now: float) -> None:
        if now - self._window_start >= STALL_WINDOW_S:
            if self._suppressed:
                self._flush_suppressed()
            self._window_start = now
            self._written = 0
        if self._written >= STALL_LIMIT:
            self._suppressed += 1
            self._worst_suppressed = max(self._worst_suppressed, gap)
            return
        self._written += 1
        self._audit(self._stall_line(gap))

    def _stall_line(self, gap: float) -> str:
        line = f"PERF stall gap={_ms(gap)}"
        view = _tv._perf_paint_view
        if view is not None:
            grid = f"{view.screen.columns}x{view.screen.lines}"
            line += f" paint={_ms(_tv._perf_paint_s)} grid={grid}"
        else:
            line += " paint=0 grid=-"
        view = _tv._perf_feed_view
        if view is not None:
            # last: an agent's name can hold spaces
            line += (f" feed={_ms(_tv._perf_feed_s)}"
                     f" feed_chars={_tv._perf_feed_len}"
                     f" agent={self._name_of(view) or '?'}")
        else:
            line += " feed=0 feed_chars=0 agent=-"
        return line

    def _flush_suppressed(self) -> None:
        self._audit(f"PERF stall suppressed={self._suppressed} "
                    f"worst_gap={_ms(self._worst_suppressed)}")
        self._suppressed = 0
        self._worst_suppressed = 0.0

    def _audit(self, line: str) -> None:
        try:
            self._store.audit(line)
        except Exception:  # noqa: BLE001 - forensics, never fatal
            pass
