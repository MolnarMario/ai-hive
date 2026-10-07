"""The suite runner itself: the parallel workers report the same checks as
a serial run, a crashed or hung worker costs one FAIL and never the run or
a sandbox, bad arguments are refused, and the stale-temp sweep only takes
old test folders."""

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .harness import ROOT, check

# three tests that take well under a second, from three modules
_FAST = ["test_ansi", "test_fsopen_helpers", "test_filetypes_icons"]


def _run(*args, env_extra=None, timeout=180):
    env = dict(os.environ)
    env.pop("AIHIVE_SMOKE_WORKER", None)
    env.update(env_extra or {})
    argv = [sys.executable, str(ROOT / "tests" / "smoke_test.py")]
    for name in _FAST:
        argv += ["-k", name]
    proc = subprocess.run(argv + list(args), capture_output=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(ROOT), timeout=timeout)
    result = re.search(r"RESULT: (\d+) passed, (\d+) failed, (\d+) skipped "
                       r"\((\d+) of", proc.stdout)
    return proc, (tuple(int(g) for g in result.groups()) if result else None)


def _sandboxes():
    # the nested runs make their sandboxes in THIS run's temp dir
    return sorted(p.name for p in Path(tempfile.gettempdir()).iterdir()
                  if p.name.startswith("ai-hive-home-"))


def test_parallel_runner():
    serial, serial_counts = _run("-j", "1")
    parallel, parallel_counts = _run("-j", "3")
    check("runner: a serial run of three tests passes",
          serial.returncode == 0 and serial_counts
          and serial_counts[3] == 3, serial.stdout[-400:])
    check("runner: three workers report the same counts as one process",
          parallel.returncode == 0 and parallel_counts == serial_counts,
          (serial_counts, parallel_counts, parallel.stdout[-400:]))
    # each test's output arrives as one block, never interleaved with
    # another worker's: every check line sits next to its test's others
    prefixes = [ln.split(":")[0] for ln in parallel.stdout.splitlines()
                if ln.startswith("[PASS] ")]
    runs = [p for i, p in enumerate(prefixes) if i == 0
            or p != prefixes[i - 1]]
    check("runner: worker output is not interleaved between tests",
          len(runs) == len(set(runs)), runs)

    bad, _ = _run("-j", "0")
    check("runner: -j 0 is refused", bad.returncode != 0
          and "-j needs a positive number" in bad.stdout + bad.stderr,
          bad.stdout[-200:] + bad.stderr[-200:])
    typo, _ = _run("-m", "nosuchmodule")
    check("runner: an unknown -m module is refused, not a green empty run",
          typo.returncode != 0
          and "no test module nosuchmodule" in typo.stdout + typo.stderr,
          typo.stdout[-200:] + typo.stderr[-200:])
    one, one_counts = _run("-m", "terminal", "-j", "1")
    check("runner: -m with -k runs only the tests matching both",
          one.returncode == 0 and one_counts and one_counts[3] == 1,
          one.stdout[-300:])

    # a worker that dies mid-test: that test is one FAIL, the checks it
    # printed first still count, and the other tests still run
    before = _sandboxes()
    crash, crash_counts = _run("-j", "2", env_extra={
        "AIHIVE_SMOKE_FAULT": "crash:test_fsopen_helpers"})
    fsopen_passes = serial.stdout.count("[PASS] fsopen")
    check("runner: a crashed worker fails the run with its own FAIL lines",
          crash.returncode == 1 and crash_counts
          and crash_counts[1] == 2
          and "test_fsopen_helpers: worker process died" in crash.stdout,
          (crash_counts, crash.stdout[-400:]))
    check("runner: the other tests still run after a worker crash",
          crash_counts and serial_counts
          and crash_counts[0] == serial_counts[0] - fsopen_passes,
          (crash_counts, serial_counts, fsopen_passes))

    # a hung worker: killed at the time limit, one FAIL, the run finishes,
    # and its sandbox goes even though the worker never ran its cleanup
    started = time.monotonic()
    hang, hang_counts = _run("-j", "2", env_extra={
        "AIHIVE_SMOKE_FAULT": "hang:test_ansi",
        "AIHIVE_SMOKE_TEST_TIMEOUT": "3"}, timeout=120)
    check("runner: a hung test is killed at the limit and fails the run",
          hang.returncode == 1 and hang_counts and hang_counts[1] == 2
          and "test_ansi: ran past 3s, worker killed" in hang.stdout
          and time.monotonic() - started < 60,
          (hang_counts, round(time.monotonic() - started), hang.stdout[-400:]))
    check("runner: no worker sandbox outlives a crashed or killed worker",
          _sandboxes() == before, (before, _sandboxes()))


def test_drop_windows():
    """The runner deletes each test's leftover windows. A main window can
    go while its manager lives on, and the manager's stats signal must not
    reach the dead window: its lambdas raised 'Internal C++ object already
    deleted' 26 times a serial run until they became a bound method."""
    from PySide6.QtWidgets import QApplication, QWidget

    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    from . import harness

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    store = SessionStore(path=Path(tempfile.mkdtemp(prefix="ai-hive-drop-"))
                         / "session.json")
    win = create_main_window(store)
    win.show()
    stray = QWidget()
    stray.show()
    mgr = win.manager
    ws_id = mgr.active_id
    harness.drop_windows()
    check("drop: no top-level widget survives the cleanup",
          not app.topLevelWidgets(), app.topLevelWidgets())

    raised = []
    real_hook = sys.excepthook
    sys.excepthook = lambda *exc: raised.append(exc[1])
    try:
        mgr.workspaceStatsChanged.emit(ws_id, {})
    finally:
        sys.excepthook = real_hook
    check("drop: a stats signal after the window is gone raises nothing",
          not raised, [str(e) for e in raised])


def test_stale_temp_sweep():
    """sweep_stale_temp deletes test folders older than the cutoff, read-only
    files and all, and nothing else: never a fresh one (a run in another
    lane), never the app's own aihive-* folders."""
    from . import harness

    root = Path(tempfile.mkdtemp(prefix="sweep-root-"))
    old = time.time() - harness.STALE_TEMP_AGE_S - 60
    names = {"ai-hive-home-old": True, "ai-hive-iso-test-old": True,
             "ai-hive-home-fresh": False, "aihive-theme": False,
             "someone-elses-old": False}
    for name, is_old in names.items():
        (root / name / "sub").mkdir(parents=True)
        (root / name / "sub" / "f.txt").write_text("x", encoding="utf-8")
        # read-only, the way git writes its objects
        os.chmod(root / name / "sub" / "f.txt", stat.S_IREAD)
        if is_old or name in ("aihive-theme", "someone-elses-old"):
            os.utime(root / name, (old, old))
    removed = harness.sweep_stale_temp(root)
    left = sorted(p.name for p in root.iterdir())
    check("sweep: old ai-hive-* test folders are removed",
          removed == 2 and "ai-hive-home-old" not in left
          and "ai-hive-iso-test-old" not in left, (removed, left))
    check("sweep: fresh test folders and other folders stay",
          left == ["ai-hive-home-fresh", "aihive-theme", "someone-elses-old"],
          left)
