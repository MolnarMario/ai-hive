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


def _e2e_accept_trust(agent, pump) -> None:
    """Choose "Yes, I trust this folder". CLI 2.1.283 puts the cursor on
    "No, exit", so a bare Enter quits: move down when the cursor is on "No",
    then confirm as a separate write."""
    import re as _re
    pump(300)   # let the frame finish before reading the cursor
    if _re.search("❯\\s*(\\d\\.\\s*)?no", _e2e_screen_text(agent)):
        agent.write("\x1b[B"); pump(300)
    agent.write("\r")


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
    _e2e_accept_trust(agent, pump)
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
