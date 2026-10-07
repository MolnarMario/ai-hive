"""What every smoke test shares: the sandbox profile, check()/skip() and
their counters, the real-AI-launch guard.

Importing this module IS the sandbox setup: it points the profile env vars
at a throwaway folder and sets the offscreen Qt platform. So it must be
imported before anything from `app` or PySide6, and test modules keep their
app imports inside the test functions.
"""

import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # must precede any Qt import
os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

# failure DETAILS can contain terminal screen text (box drawing, prompt
# glyphs) — a cp1252 console must degrade them, never crash the suite
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
SHOTS = ROOT / "tests" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

# The whole suite runs with a throwaway profile. Many app paths hang off
# USERPROFILE (~/.claude/projects, ~/.gemini, ~/.local/bin, %APPDATA%), and a
# test that forgets to redirect one of them used to write fixture transcripts
# into the user's real ~/.claude and could reach their real session file.
# Only test_lifecycle_e2e needs the real profile (a logged-in claude), and it
# borrows it through real_profile().
_PROFILE_VARS = ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA",
                 "CLAUDE_CONFIG_DIR", "TEMP", "TMP")
_REAL_PROFILE = {k: os.environ.get(k) for k in _PROFILE_VARS}
REAL_TMP = Path(tempfile.gettempdir()).resolve()
SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="ai-hive-home-"))


def _set_env(values: dict) -> None:
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


_SANDBOX_PROFILE = {
    "USERPROFILE": str(SANDBOX_HOME), "HOME": str(SANDBOX_HOME),
    "APPDATA": str(SANDBOX_HOME / "AppData" / "Roaming"),
    "LOCALAPPDATA": str(SANDBOX_HOME / "AppData" / "Local"),
    "CLAUDE_CONFIG_DIR": None,
    "TEMP": str(SANDBOX_HOME / "tmp"), "TMP": str(SANDBOX_HOME / "tmp"),
}
for _d in ("APPDATA", "LOCALAPPDATA", "TEMP"):
    Path(_SANDBOX_PROFILE[_d]).mkdir(parents=True, exist_ok=True)
_set_env(_SANDBOX_PROFILE)
# every test's mkdtemp lands inside the sandbox and goes with it at exit:
# before this, each run left ~40 folders in %TEMP% (4046 by 2026-09-28)
tempfile.tempdir = _SANDBOX_PROFILE["TEMP"]

# The cwd for every test agent. Never the repo: CLAUDE.md forbids test Claude
# sessions in real project folders (they pollute resume ordering), and a card
# whose cwd is the repo reads the user's LIVE transcripts, so its results
# depended on whatever conversation was running at the time.
SCRATCH_CWD = str(SANDBOX_HOME / "project")
Path(SCRATCH_CWD).mkdir()


class real_profile:
    """Borrow the user's real profile for a test that must run a real,
    logged-in claude. Everything written under it must be cleaned up by the
    test itself, in a finally. Also the only place a real AI CLI may start
    (see _guard_real_ai_launches)."""

    def __enter__(self):
        global _REAL_AI_ALLOWED
        _set_env(_REAL_PROFILE)
        _REAL_AI_ALLOWED = True

    def __exit__(self, *exc):
        global _REAL_AI_ALLOWED
        _set_env(_SANDBOX_PROFILE)
        _REAL_AI_ALLOWED = False
        return False


_REAL_AI_ALLOWED = False
REAL_AI_LAUNCHES = []   # (test-visible) every blocked launch, for the report
_AI_CLI_STEMS = {"claude", "codex", "agy", "gemini", "grok"}


def _guard_real_ai_launches():
    """Refuse, and record, any worker start whose argv runs an AI CLI outside
    real_profile(). A test agent that reached a real claude used to start one
    in the repo folder (restart() from IDLE starts the worker, add_terminal
    autostarts by default), and nothing noticed. main() turns a recorded
    launch into a FAIL."""
    from app import process_worker, pty_worker

    def guarded(real_start):
        def start(self):
            argv = [self.spec.program] + list(self.spec.effective_args())[:3]
            stems = {Path(str(a)).stem.lower() for a in argv}
            if not _REAL_AI_ALLOWED and stems & _AI_CLI_STEMS:
                REAL_AI_LAUNCHES.append(f"{self.spec.name}: {argv[0]}")
                return
            return real_start(self)
        return start

    for cls in (pty_worker.PtyWorker, process_worker.ProcessWorker):
        cls.start = guarded(cls.start)


PASS = 0
FAIL = 0
SKIP = 0


def skip(name, reason):
    """A check that could not run here. Counted apart from PASS, so a machine
    that skips everything never reads as green."""
    global SKIP
    print(f"[SKIP] {name} :: {reason}", flush=True)
    SKIP += 1


def check(name, cond, detail=""):
    global PASS, FAIL
    cond = bool(cond)
    line = ("[PASS] " if cond else "[FAIL] ") + name
    if detail and not cond:
        line += " :: " + str(detail)
    print(line, flush=True)
    PASS += cond
    FAIL += not cond



def drop_windows():
    """Delete every top-level widget a test left behind. Tests show windows
    and cards and never delete them, and every app-wide restyle (a theme or
    font change, setup_application) sends events to each live widget. By
    the window tests a serial run held thousands, and test_themes took 22 s
    against 0.2 s in a fresh process. Hidden and deleted, never close()d: a
    closeEvent could open a dialog that nobody answers."""
    if "PySide6.QtWidgets" not in sys.modules:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.hide()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete.value)


def _rmtree(path):
    """shutil.rmtree that also deletes read-only files. Git writes its
    objects read-only, and rmtree(ignore_errors=True) silently left every
    sandbox that held a repo: that is how 4150 test folders piled up in
    %TEMP%."""
    def clear_and_retry(func, failed, _exc):
        try:
            os.chmod(failed, stat.S_IWRITE)
            func(failed)
        except OSError:
            pass   # still in use: the next sweep_stale_temp gets it
    shutil.rmtree(path, onerror=clear_and_retry)


def remove_sandbox(home, wait_s=0.0):
    """Delete a throwaway profile, retrying for up to wait_s while the
    processes that had it as their cwd finish dying. Refuses anything that
    is not a direct child of the temp dir named like one, so a bad path can
    never aim this at a real folder."""
    home = Path(home).resolve()
    if home.parent != REAL_TMP or not home.name.startswith("ai-hive-home-"):
        return False
    deadline = time.monotonic() + wait_s
    while True:
        _rmtree(home)
        if not home.exists() or time.monotonic() >= deadline:
            return not home.exists()
        time.sleep(0.25)


def _remove_sandbox_home():
    """Delete this run's throwaway profile. Whatever this process's own
    agents still hold stays until the next run's sweep_stale_temp."""
    remove_sandbox(SANDBOX_HOME)


# Every folder the suite makes in the real temp dir starts with this: the
# sandboxes, and the mkdtemp folders from before the sandbox existed. The app
# names its own temp folders "aihive-...", so this never matches them.
_TEST_TEMP_PREFIX = "ai-hive-"
# No run lasts this long, so an older folder belongs to a killed or
# crashed run, never to one in progress in another lane.
STALE_TEMP_AGE_S = 6 * 3600


def sweep_stale_temp(root=None):
    """Delete test folders that runs left in the real temp dir: a killed
    run never reaches its cleanup, and a serial run can't remove a sandbox
    its own agents still use as their cwd. 4150 had piled up by
    2026-10-07. Returns how many it removed."""
    root = Path(root).resolve() if root else REAL_TMP
    cutoff = time.time() - STALE_TEMP_AGE_S
    removed = 0
    try:
        entries = list(os.scandir(root))
    except OSError:
        return 0
    for entry in entries:
        try:
            if (entry.name.startswith(_TEST_TEMP_PREFIX)
                    and entry.is_dir(follow_symlinks=False)
                    and entry.stat(follow_symlinks=False).st_mtime < cutoff
                    and Path(entry.path).resolve().parent == root):
                _rmtree(entry.path)
                removed += not os.path.exists(entry.path)
        except OSError:
            continue
    return removed


def _remove_fixture_transcripts():
    """Delete the conversation folders the suite left in the REAL
    ~/.claude/projects. test_lifecycle_e2e cleans its own in a finally; this
    sweeps what a killed run left behind, and what runs from before the
    sandbox profile wrote there (105 folders by 2026-09-24, all showing in
    Claude's /resume picker). Every such folder is named after a
    `tempfile.mkdtemp(prefix="ai-hive-...")` cwd, so it starts with the
    encoded temp dir plus "-ai-hive-", and nothing else does."""
    from app import transcripts
    with real_profile():
        root = Path(transcripts.projects_root())
    # the REAL temp dir: mkdtemp now lands in the sandbox, whose folders all
    # sit under REAL_TMP\ai-hive-home-..., so this one prefix covers both
    prefix = transcripts.encode_project_dir(str(REAL_TMP)) + "-ai-hive-"
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and entry.name.startswith(prefix):
            shutil.rmtree(entry, ignore_errors=True)
