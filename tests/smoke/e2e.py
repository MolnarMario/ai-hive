"""The real-claude close/reopen test, run last. Slow and billed, skipped by
--quick. Its timeouts guard data loss, don't shorten them."""

import os
import shutil
import tempfile
import time
from pathlib import Path

from .harness import check, real_profile, skip


def test_lifecycle_e2e():
    """THE journey that kept losing user data, end to end with a REAL Claude
    agent: talk -> graceful close -> reopen -> the SAME conversation is back
    on the same card (pinned --resume), and a transcript backup exists.

    Runs under the user's real profile (a logged-in claude) and cleans up
    everything it writes there in a finally. Skipped when the claude CLI
    isn't installed. Bails at the first broken step instead of sitting out
    every later timeout."""
    import uuid as _uuid
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import providers, transcripts
    from app.pty_worker import HAS_CONPTY

    app = QApplication.instance() or QApplication([])
    from main import setup_application
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-e2e-"))
    windows = []
    with real_profile():
        if not (HAS_CONPTY and providers.detected("claude")):
            skip("lifecycle e2e", "claude CLI or ConPTY unavailable")
            shutil.rmtree(tmp, ignore_errors=True)
            return
        proj = os.path.join(transcripts.projects_root(),
                            transcripts.encode_project_dir(str(tmp)))
        try:
            _lifecycle_e2e_body(tmp, windows, pump, f"PINEAPPLE-"
                                f"{_uuid.uuid4().hex[:8]}")
        finally:
            for w in windows:
                try:
                    w.close()
                except RuntimeError:
                    pass
            pump(600)
            shutil.rmtree(proj, ignore_errors=True)
            shutil.rmtree(tmp, ignore_errors=True)


def _e2e_screen_text(agent) -> str:
    """The agent's raw stream with escapes stripped, lowercased. The stream
    positions words individually, so this is for substring tests only."""
    import re as _re
    csi = _re.compile(
        r"\x1b\[[0-9;?<>=]*[@-~]|\x1b[()][AB0]|\x1b\][^\x07\x1b]*\x07?")
    return csi.sub("", agent.pty_replay()).lower()


def _e2e_assistant_said(path, needle) -> bool:
    """True once an ASSISTANT record in the transcript carries `needle`. The
    user's own prompt holds it too, so a plain substring test would pass on
    the prompt alone."""
    import json as _json
    try:
        lines = Path(path).read_text(encoding="utf-8",
                                     errors="replace").splitlines()
    except OSError:
        return False
    for line in lines:
        try:
            rec = _json.loads(line)
        except ValueError:
            continue
        if (isinstance(rec, dict) and rec.get("type") == "assistant"
                and needle in _json.dumps(rec)):
            return True
    return False


def _e2e_trust_options_drawn(agent) -> bool:
    """The trust dialog's OPTION rows are on screen, not just its heading.
    It draws top-down, and reading the cursor before the rows exist sent a
    bare Enter onto "No, exit"."""
    flat = "".join(_e2e_screen_text(agent).split())
    return "itrustthisfolder" in flat and "no,exit" in flat


_TRUST_LABELS = (("yes", "yes, i trust this folder"), ("no", "no, exit"))


def _e2e_screen_rows(agent) -> list:
    """The agent's screen as rows of text: its raw stream replayed through
    pyte (the emulator the cards use) at the PTY's size. Claude redraws a
    dialog with relative cursor moves, so only a rendered screen says which
    row a cursor glyph landed on."""
    import pyte
    screen = pyte.Screen(agent.worker.cols, agent.worker.rows)
    pyte.Stream(screen).feed(agent.pty_replay())
    return screen.display


def _e2e_trust_rows(rows) -> dict:
    """{"yes"|"no": (row index, highlighted)} for the trust options drawn.
    The highlighted option has a marker in the gutter left of its label. The
    marker was "❯" in CLI 2.1.283 and is ">" in 2.1.290, so any mark there
    counts (box borders and an option number like "1." don't)."""
    import re as _re
    found = {}
    for i, row in enumerate(rows):
        low = row.lower()
        for key, label in _TRUST_LABELS:
            at = low.find(label)
            if at >= 0:
                gutter = _re.sub(r"[\s│┃|]|\d+\.", "", low[:at])
                found[key] = (i, bool(gutter))
    return found


def _e2e_accept_trust(agent, pump) -> bool:
    """Choose "Yes, I trust this folder". Returns False, without pressing
    Enter, when the highlight can't be put on Yes.

    Enter on "No, exit" quits claude, and the cursor glyph and the option
    order have both changed between CLI versions. So nothing is assumed: the
    rendered screen says where the highlight is, an arrow key moves it toward
    Yes, and Enter goes only once the screen shows it on Yes."""
    for _ in range(4):
        pump(300)   # let the frame finish before reading it
        found = _e2e_trust_rows(_e2e_screen_rows(agent))
        marked = [k for k, (_i, on) in found.items() if on]
        if len(found) < 2 or len(marked) != 1:
            continue    # half drawn, or mid-redraw
        if marked == ["yes"]:
            agent.write("\r")
            return True
        up = found["yes"][0] < found["no"][0]
        agent.write("\x1b[A" if up else "\x1b[B")
    return False


# CLI 2.1.290's trust dialog as it reaches the PTY (120 columns), and the
# bytes it sends after one Down arrow, captured from a real claude
_TRUST_DIALOG_2_1_290 = (
    "\r\n" + "─" * 120 + "\r\n"
    "\x1b[2GAccessing\x1b[12Gworkspace:\r\n\r\n"
    "\x1b[2GC:\\scratch\\e2e\r\n\r\n"
    "\x1b[2GQuick\x1b[8Gsafety\x1b[15Gcheck:\x1b[22GIs\x1b[25Gthis\x1b[30Ga"
    "\x1b[32Gproject\x1b[40Gyou\x1b[44Gcreated\x1b[52Gor\x1b[55Gone"
    "\x1b[59Gyou\x1b[63Gtrust?\r\n\r\n"
    "\x1b[2GSecurity\x1b[11Gguide\r\n\r\n"
    "\x1b[2G>\x1b[4GNo,\x1b[8Gexit\r\n"
    "\x1b[4GYes,\x1b[9GI\x1b[11Gtrust\x1b[17Gthis\x1b[22Gfolder\r\n\r\n"
    "\x1b[2GEnter\x1b[8Gto\x1b[11Gconfirm\x1b[19G·\x1b[21GEsc\x1b[25Gto"
    "\x1b[28Gcancel\r\n"
    "\x1b[1C\x1b[4A\x1b[>0q\x1b[?u\x1b[c")
_TRUST_DOWN_2_1_290 = ("\x1b[1D\x1b[4B\r\x1b[1C\x1b[4A \r\x1b[1C\x1b[1B>"
                       "\r\n\n\n\x1b[1C\x1b[3A")
# the 2.1.283 shape: numbered options, "❯", Yes first
_TRUST_DIALOG_2_1_283 = (
    "\x1b[2GDo you trust the files in this folder?\r\n\r\n"
    "\x1b[2G❯ 1. Yes, I trust this folder\r\n"
    "\x1b[4G2. No, exit\r\n")


def test_e2e_trust_dialog_accept():
    """The e2e's trust-dialog helper, offline against captured CLI output.
    CLI 2.1.290 drew its cursor as ">" instead of "❯", the old helper didn't
    see it, sent Enter onto "No, exit" and claude quit, failing the e2e."""
    from types import SimpleNamespace

    class FakeAgent:
        """Echoes what claude draws for each key: `moves` maps a key to the
        bytes claude answers with; any other key changes nothing."""
        def __init__(self, stream, moves=None):
            self.stream, self.moves, self.writes = stream, moves or {}, []
            self.worker = SimpleNamespace(rows=40, cols=120)

        def pty_replay(self):
            return self.stream

        def write(self, data):
            self.writes.append(data)
            self.stream += self.moves.get(data, "")

    def pump(_ms):
        pass

    new = FakeAgent(_TRUST_DIALOG_2_1_290)
    found = _e2e_trust_rows(_e2e_screen_rows(new))
    check("e2e trust: 2.1.290 dialog, both options found, cursor on No",
          set(found) == {"yes", "no"} and found["no"][1]
          and not found["yes"][1], found)

    new = FakeAgent(_TRUST_DIALOG_2_1_290, {"\x1b[B": _TRUST_DOWN_2_1_290})
    ok = _e2e_accept_trust(new, pump)
    check("e2e trust: 2.1.290, one Down moves to Yes, then Enter",
          ok and new.writes == ["\x1b[B", "\r"], new.writes)

    old = FakeAgent(_TRUST_DIALOG_2_1_283)
    ok = _e2e_accept_trust(old, pump)
    check("e2e trust: 2.1.283 numbered dialog with Yes selected, Enter only",
          ok and old.writes == ["\r"], old.writes)

    stuck = FakeAgent(_TRUST_DIALOG_2_1_290)
    ok = _e2e_accept_trust(stuck, pump)
    check("e2e trust: a highlight that won't reach Yes never gets Enter",
          not ok and "\r" not in stuck.writes, stuck.writes)

    half = FakeAgent(_TRUST_DIALOG_2_1_290.split("\x1b[4GYes")[0])
    ok = _e2e_accept_trust(half, pump)
    check("e2e trust: a half-drawn dialog gets no keys at all",
          not ok and half.writes == [], half.writes)


def _lifecycle_e2e_body(tmp, windows, pump, marker):
    from app import transcripts
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from main import create_main_window

    def wait_until(pred, timeout_ms, step=100):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    screen_text = _e2e_screen_text
    store = SessionStore(path=tmp / "s.json")

    # --- session 1: real claude, say the marker --------------------------
    win = create_main_window(store)
    windows.append(win)
    win.show(); pump(150)
    ws = win.manager.workspaces[0]
    spec = build_spec(AgentKind.CLAUDE, "E2E", cwd=str(tmp), pty=True,
                      model="haiku")
    agent = win.manager.add_terminal(ws.id, spec, autostart=True)
    sid = agent.spec.session_id
    check("e2e: launch minted a pinned session id", bool(sid))
    # a brand-new folder shows the trust dialog first; the task is queued and
    # must NOT be typed into the dialog
    agent.deliver_task(
        f"Reply with exactly the word {marker} and nothing else.")
    dialog = wait_until(lambda: _e2e_trust_options_drawn(agent), 45000)
    check("e2e: trust dialog appeared for the fresh folder", dialog,
          screen_text(agent)[-300:])
    if not dialog:
        return
    check("e2e: prompt not ready while the trust dialog is up",
          not agent._prompt_ready)
    accepted = _e2e_accept_trust(agent, pump)
    check("e2e: the trust dialog's highlight reached Yes", accepted,
          "\n".join(r.rstrip() for r in _e2e_screen_rows(agent) if r.strip()))
    if not accepted:
        return
    ready = wait_until(lambda: agent._prompt_ready, 45000)
    check("e2e: accepting trust reaches the real prompt", ready,
          screen_text(agent)[-300:])
    if not ready:
        return
    tpath = Path(transcripts.transcript_path(str(tmp), sid))
    # the transcript flush lags the on-screen answer (file first, content
    # later): wait until the REPLY is in the file before closing, or the
    # close-time backup snapshots a partial conversation
    answered = wait_until(lambda: _e2e_assistant_said(tpath, marker), 120000)
    check("e2e: the queued task was delivered and claude's reply is in the "
          "pinned transcript", answered, screen_text(agent)[-300:])
    if not answered:
        return
    first_worker = agent.worker
    n_agents = len(ws.agents)
    win.close(); pump(600)  # graceful close: save + transcript backup
    check("e2e: the first claude exited before any reopen",
          wait_until(lambda: not first_worker.is_running(), 15000))

    on_disk = (tmp / "s.json").read_text(encoding="utf-8")
    check("e2e: close persisted the pinned id + running state",
          sid in on_disk and '"running": true' in on_disk)
    check("e2e: close snapshotted the transcript, reply included",
          _e2e_assistant_said(tmp / "transcripts" / f"{sid}.jsonl", marker))

    # --- session 2: reopen -> the SAME conversation returns --------------
    win2 = create_main_window(store)
    windows.append(win2)
    win2.show(); pump(150)
    agents2 = win2.manager.workspaces[0].agents
    check("e2e: every agent came back", len(agents2) == n_agents,
          (len(agents2), n_agents))
    agent2 = agents2[-1]
    check("e2e: reopened agent kept its pin", agent2.spec.session_id == sid)
    check("e2e: reopened agent resumes, not --continue",
          agent2.spec.resume
          and "--resume" in agent2.spec.effective_args())
    win2.autostart_active_workspace()
    # the card was seeded with the previous run's screen, which holds the
    # marker. The launch must drop it, or the check below would pass off the
    # snapshot instead of a real --resume.
    check("e2e: the launch dropped the restored screen",
          marker.lower() not in screen_text(agent2),
          screen_text(agent2)[-200:])
    # the folder was trusted in session 1, but another claude on this machine
    # can rewrite ~/.claude.json from its own stale copy and drop that entry,
    # so the dialog may come back. Accept it again rather than fail on it.
    accepted = []

    def resumed():
        if not accepted and _e2e_trust_options_drawn(agent2):
            accepted.append(1)
            _e2e_accept_trust(agent2, pump)
        return screen_text(agent2).count(marker.lower()) >= 2
    check("e2e: THE SAME conversation came back on the card (prompt AND "
          "reply reprinted by --resume)", wait_until(resumed, 90000),
          screen_text(agent2)[-300:])
    check("e2e: no silent fresh-fallback (pin unchanged)",
          agent2.spec.session_id == sid)
