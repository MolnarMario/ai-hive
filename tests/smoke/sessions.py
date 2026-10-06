"""The session file and resuming agents: migration, persistence, pinning
with --resume, recovery, hooks, snapshots and the boot veil."""

import os
import shutil
import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check


def test_session_migration():
    """A pre-v2 line-mode shell agent upgrades to interactive on load."""
    from app.pty_worker import HAS_CONPTY
    from app.workspace_manager import WorkspaceManager

    legacy = {
        "version": 1, "active": "w1",
        "workspaces": [{
            "id": "w1", "name": "Legacy", "project_path": os.path.expanduser("~"),
            "terminals": [
                {"kind": "powershell", "name": "Agent 1", "role": "",
                 "cwd": os.path.expanduser("~"), "user_program": "",
                 "user_args": [], "pty": False, "running": True},
                {"kind": "custom", "name": "Script", "role": "",
                 "cwd": os.path.expanduser("~"), "user_program": "x.exe",
                 "user_args": [], "pty": False, "running": False},
            ],
        }],
    }
    mgr = WorkspaceManager()
    mgr.load_session_dict(legacy)
    agents = mgr.workspaces[0].agents
    ps = next(a for a in agents if a.spec.kind.value == "powershell")
    custom = next(a for a in agents if a.spec.kind.value == "custom")
    check("migration: legacy PowerShell upgraded to interactive",
          ps.spec.pty is HAS_CONPTY and ps.is_pty is HAS_CONPTY)
    check("migration: non-shell agent left as-is", custom.spec.pty is False)
    check("migration: session now persists as current version",
          mgr.to_session_dict()["version"] >= 2)

    # a current-version session with an explicit line-mode shell is respected
    mgr2 = WorkspaceManager()
    current = mgr.to_session_dict()
    current["workspaces"][0]["terminals"][0]["pty"] = False  # explicit opt-out
    mgr2.load_session_dict(current)
    ps2 = next(a for a in mgr2.workspaces[0].agents
               if a.spec.kind.value == "powershell")
    check("migration: explicit line-mode in v2 session is preserved",
          ps2.spec.pty is False)


def test_persistence_resume():
    """Agents survive an abrupt kill (immediate save) and running Claude agents
    resume on reopen (--continue) — the regression behind the lost-agent bug."""
    import json as _json
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-persist-"))
    sp = tmp / "s.json"
    store = SessionStore(path=sp)
    win = create_main_window(store)
    win.show()
    pump(120)
    ws = win.manager.workspaces[0]
    win.manager.add_terminal(ws.id, build_spec(AgentKind.CMD, "KeepMe", cwd=str(tmp)),
                             autostart=False)
    # read disk WITHOUT closing or waiting for the debounce — models a kill
    on_disk = _json.loads(sp.read_text(encoding="utf-8"))
    names = [t["name"] for t in on_disk["workspaces"][0]["terminals"]]
    check("persist: agent saved immediately (survives a kill)", "KeepMe" in names, names)
    win.close(); pump(120)

    # LEGACY: a v3 session has no pinned id, so --continue is the only resume
    # it can have. Pinned agents resume with --resume <id> (test_session_pinning)
    store.save({"version": 3, "active": "w", "workspaces": [{
        "id": "w", "name": "R", "project_path": str(tmp), "layout": "auto",
        "terminals": [{"kind": "claude", "name": "Coder", "role": "Claude Code",
                       "cwd": str(tmp), "user_program": "", "user_args": [],
                       "pty": True, "provider": "claude", "model": "", "effort": "",
                       "custom_command": "", "font_px": 0, "is_orchestrator": False,
                       "running": True, "task": "x", "assignment": "working",
                       "auto_created": True}]}]})
    win2 = create_main_window(store)
    win2.show(); pump(120)
    ag = win2.manager.workspaces[0].agents[0]
    check("persist: a running LEGACY unpinned Claude agent resumes (--continue)",
          ag.spec.resume and "--continue" in ag.spec.effective_args())
    from app.terminal_agent import AssignmentState
    check("persist: a saved 'working' assignment restores as idle (nothing "
          "would ever move it on)", ag.assignment is AssignmentState.IDLE,
          ag.assignment)
    win2.close(); pump(120)

    # a UTF-8 BOM must NOT make a valid session look corrupt (the bug that
    # wiped workspaces when the file was edited by a BOM-adding tool)
    bomp = tmp / "bom.json"
    payload = {"version": 3, "active": "b", "workspaces": [
        {"id": "b", "name": "BomWs", "project_path": str(tmp), "layout": "auto",
         "terminals": []}]}
    bomp.write_bytes(b"\xef\xbb\xbf" + _json.dumps(payload).encode("utf-8"))
    loaded = SessionStore(path=bomp).load()
    check("persist: BOM-prefixed session still loads (not wiped)",
          [w["name"] for w in loaded.get("workspaces", [])] == ["BomWs"], loaded)

    # sticky auto-resume intent: an auto-created agent (spawned with a task via
    # spawn_worker) that died keeps its "resume on next open" flag so it never
    # silently goes dormant (the bug where a task-spawned agent vanished after
    # its process failed)
    win3 = create_main_window(SessionStore(path=tmp / "sticky.json"))
    win3.show(); pump(120)
    ws3 = win3.manager.workspaces[0]
    auto = win3.manager.add_terminal(
        ws3.id, build_spec(AgentKind.CLAUDE, "Auto Worker", cwd=str(tmp),
                           pty=True), autostart=False)
    auto.auto_created = True
    auto.autostart_on_restore = True   # was meant to be running
    manual = win3.manager.add_terminal(
        ws3.id, build_spec(AgentKind.CMD, "Manual", cwd=str(tmp)), autostart=False)
    dumped = {t["name"]: t["running"]
              for t in win3.manager.to_session_dict()["workspaces"][0]["terminals"]}
    check("persist: dead auto-agent keeps resume intent (running=True)",
          dumped.get("Auto Worker") is True, dumped)
    check("persist: idle user agent is not force-resumed (running=False)",
          dumped.get("Manual") is False, dumped)
    win3.close(); pump(120)

    # A save must NEVER fail silently. Three holes closed after a live incident
    # where a workspace's agents existed in the running window but never on disk
    # and NOTHING appeared in session.log:
    #  (a) saves suppressed while _closing were silent (a failed-quit zombie
    #      accepts new work forever, logging nothing)
    #  (b) an exception BUILDING the payload bypassed store.save's SAVE-FAIL log
    #  (c) nothing re-saved a state that a missed/suppressed signal had stranded
    sp4 = tmp / "guard.json"
    store4 = SessionStore(path=sp4)
    win4 = create_main_window(store4)
    win4.show(); pump(120)
    logp = sp4.with_suffix(".log")
    ws4 = win4.manager.workspaces[0]

    # (a) a suppressed save leaves a forensic breadcrumb, not silence
    win4._closing = True
    win4._save_now()
    win4._closing = False
    log_txt = logp.read_text(encoding="utf-8") if logp.exists() else ""
    check("persist: suppressed save is logged, never silent",
          "SAVE-SKIP closing" in log_txt, log_txt[-200:])

    # (b) a payload-build exception is logged as SAVE-FAIL (was: vanished)
    boom = win4._session_payload
    win4._session_payload = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    win4._save_session()
    win4._session_payload = boom
    log_txt = logp.read_text(encoding="utf-8")
    check("persist: payload-build error logged as SAVE-FAIL (not silent)",
          "SAVE-FAIL payload" in log_txt, log_txt[-200:])

    # (c) the heartbeat rescues a structural change that a suppressed save
    #     stranded — models the live incident exactly
    win4._closing = True                       # suppress the add's own save
    win4.manager.add_terminal(
        ws4.id, build_spec(AgentKind.CMD, "Rescued", cwd=str(tmp)),
        autostart=False)
    win4._closing = False
    disk_before = _json.loads(sp4.read_text(encoding="utf-8"))
    names_before = [t["name"] for t in disk_before["workspaces"][0]["terminals"]]
    check("persist: suppressed add is NOT yet on disk (setup)",
          "Rescued" not in names_before, names_before)
    win4._heartbeat_save()                     # safety net catches divergence
    disk_after = _json.loads(sp4.read_text(encoding="utf-8"))
    names_after = [t["name"] for t in disk_after["workspaces"][0]["terminals"]]
    check("persist: heartbeat re-saves stranded state (bounds worst-case loss)",
          "Rescued" in names_after, names_after)
    win4.close(); pump(120)

    # one un-serializable agent must NOT drop every other agent from the save
    # (the whole-session throw that stranded a folder's agents with no log line)
    from app.workspace_manager import WorkspaceManager
    mgr5 = WorkspaceManager()
    ws5 = mgr5.create_workspace("WS5", str(tmp))
    good = mgr5.add_terminal(ws5.id, build_spec(AgentKind.CMD, "Good", cwd=str(tmp)),
                             autostart=False)
    bad = mgr5.add_terminal(ws5.id, build_spec(AgentKind.CMD, "Bad", cwd=str(tmp)),
                            autostart=False)
    saved_assignment = bad.assignment
    bad.assignment = object()                  # .value now raises -> would throw
    terms = mgr5.to_session_dict()["workspaces"][0]["terminals"]
    bad.assignment = saved_assignment          # restore before teardown/GC
    names5 = [t["name"] for t in terms]
    check("persist: bad agent cannot abort the whole save (good survives)",
          "Good" in names5 and len(terms) >= 1, names5)

    # THE DEGRADE MUST NOT BE SILENT. Found in production: two claude agents
    # were being written as fallback records on EVERY save, losing model,
    # effort, permission mode, role and task to defaults on the next restore --
    # and the cause could not be recovered from the file, because two different
    # failures in AgentSpec.to_dict produce byte-identical output and nothing
    # had recorded which one fired.
    # END TO END: an agent created through the manager inherits the audit hook,
    # so a NO-LATCH line from deep in the scrape actually reaches session.log.
    # The helper is unit-tested elsewhere; the PLUMBING is the part that breaks.
    sp6 = tmp / "wired.json"
    store6 = SessionStore(path=sp6)
    win6 = create_main_window(store6)
    win6.show(); pump(120)
    ws6 = win6.manager.workspaces[0]
    wired = win6.manager.add_terminal(
        ws6.id, build_spec(AgentKind.CLAUDE, "Wired", cwd=str(tmp), pty=True),
        autostart=False)
    wired._prompt_ready = False          # as during a resume replay
    wired._screen_tail = ("You've hit your session limit \xb7 resets 3am "
                          "(Europe/Bucharest)\n")
    wired._on_idle_timeout()
    win6.close(); pump(120)
    logged = sp6.with_suffix(".log").read_text(encoding="utf-8", errors="replace")
    check("persist: a manager-wired agent's NO-LATCH reaches session.log",
          "NO-LATCH" in logged and "Wired" in logged,
          [l for l in logged.splitlines() if "LIMIT" in l][-3:])

    lines = []
    mgr5.audit = lines.append
    pty_spec = build_spec(AgentKind.CLAUDE, "Degraded", cwd=str(tmp), pty=True,
                          model="opus", effort="high")
    degraded = mgr5.add_terminal(ws5.id, pty_spec, autostart=False)
    degraded.current_task = "keep me"
    degraded.assignment = object()             # .value raises -> degrade path
    rec = mgr5._agent_dict_safe(degraded)
    degraded.assignment = saved_assignment     # restore before teardown/GC
    check("persist: a degraded save is audited with the exception",
          any(m.startswith("SAVE-DEGRADE") and "Degraded" in m
              and "AttributeError" in m for m in lines), lines)
    check("persist: the fallback record keeps pty", rec.get("pty") is True, rec)
    check("persist: the fallback record keeps model/effort/task",
          rec.get("model") == "opus" and rec.get("effort") == "high"
          and rec.get("task") == "keep me", rec)
    # and it must still round-trip into a real spec
    from app.process_worker import AgentSpec as _Spec
    check("persist: the fallback record still restores as a pty claude agent",
          _Spec.from_dict(rec).pty is True
          and _Spec.from_dict(rec).provider == "claude")
    # an unset audit hook stays a no-op (a bare manager must never depend on it)
    mgr5.audit = None
    degraded.assignment = object()
    check("persist: degrading without an audit hook does not raise",
          mgr5._agent_dict_safe(degraded).get("name") == "Degraded")
    degraded.assignment = saved_assignment

    shutil.rmtree(tmp, ignore_errors=True)


def test_transcript_backups():
    """AI Hive snapshots each pinned agent's Claude transcript on app start
    and close. The high-water copy (<id>.max.jsonl) is never replaced by
    anything smaller — so a truncated transcript (a real incident: concurrent
    resume cut a conversation back to its first 34 lines) can rotate through
    the snapshots but can never destroy the full copy."""
    from app import transcripts

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tb-"))
    check("backup: project dir encoding matches Claude's",
          transcripts.encode_project_dir(r"C:\Users\Mario\Downloads\hive-test2")
          == "C--Users-Mario-Downloads-hive-test2")

    # fake transcript + a stub agent pointing at it via a patched path
    bdir = str(tmp / "backups")
    src = tmp / "conv.jsonl"
    sid = "12345678-1234-4123-8123-123456789abc"

    class _Spec:
        provider = "claude"
        cwd = str(tmp)
        session_id = sid

    class _Agent:
        spec = _Spec()

    real_tp = transcripts.transcript_path
    transcripts.transcript_path = lambda cwd, s: str(src)
    try:
        src.write_text("line1\nline2\nline3\n" * 40, encoding="utf-8")
        n = transcripts.backup_for_agents([_Agent()], bdir)
        newest = Path(bdir) / f"{sid}.jsonl"
        high = Path(bdir) / f"{sid}.max.jsonl"
        check("backup: first snapshot written", n == 1 and newest.exists())
        check("backup: high-water copy created", high.exists())
        full_size = newest.stat().st_size

        # unchanged source -> no re-copy
        n = transcripts.backup_for_agents([_Agent()], bdir)
        check("backup: unchanged transcript not re-copied", n == 0)

        # the incident: transcript truncated -> snapshot rotates, but the
        # high-water copy keeps the FULL version
        import time as _t
        _t.sleep(1.1)  # ensure a different integer mtime
        src.write_text("line1\n", encoding="utf-8")
        n = transcripts.backup_for_agents([_Agent()], bdir)
        check("backup: truncated version snapshotted", n == 1)
        check("backup: previous snapshot rotated to .1",
              (Path(bdir) / f"{sid}.jsonl.1").stat().st_size == full_size)
        check("backup: high-water copy NEVER shrinks",
              high.stat().st_size == full_size,
              (high.stat().st_size, full_size))

        # growth replaces the high-water copy
        _t.sleep(1.1)
        src.write_text("x" * (full_size + 100), encoding="utf-8")
        transcripts.backup_for_agents([_Agent()], bdir)
        check("backup: larger transcript raises the high-water mark",
              high.stat().st_size == full_size + 100)
    finally:
        transcripts.transcript_path = real_tp
    shutil.rmtree(tmp, ignore_errors=True)


def test_session_pinning():
    """Each Claude agent owns ONE conversation, pinned by session id: fresh
    starts declare --session-id, resumes target --resume <id>. Never
    --continue when pinned — 'most recent in this folder' broke the moment
    two agents shared a project folder (on relaunch they raced for the same
    conversation; the loser opened the wrong one / a fresh session)."""
    import re as _re
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    uuid_re = _re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def stub(agent, counter):
        agent.worker = type("W", (), {
            "start": lambda s: counter.__setitem__("n", counter["n"] + 1),
            "restart": lambda s: counter.__setitem__("n", counter["n"] + 1),
            "is_running": lambda s: False, "dispose": lambda s: None})()

    spec = build_spec(AgentKind.CLAUDE, "Pin", cwd=SCRATCH_CWD, pty=True)
    agent = TerminalAgent(spec)
    stub(agent, {"n": 0})
    agent.start()  # fresh start mints an identity
    sid1 = spec.session_id
    check("pin: fresh start mints a session id", bool(uuid_re.match(sid1)), sid1)
    fresh_args = spec.effective_args()
    check("pin: fresh launch declares --session-id",
          "--session-id" in fresh_args and sid1 in fresh_args, fresh_args)

    spec.resume = True
    args = spec.effective_args()
    check("pin: resume targets THIS conversation (--resume <id>)",
          "--resume" in args and args[args.index("--resume") + 1] == sid1, args)
    check("pin: pinned resume never uses --continue", "--continue" not in args)
    agent.start()  # resume start keeps the pin
    check("pin: resume keeps the pinned id", spec.session_id == sid1)
    agent.start()  # deliberate fresh start rotates it
    check("pin: next fresh start rotates the id",
          spec.session_id != sid1 and bool(uuid_re.match(spec.session_id)))
    agent.restart()
    rotated = spec.session_id
    check("pin: manual restart is a new conversation (new id)",
          rotated != sid1 and bool(uuid_re.match(rotated)))

    # legacy sessions (no pin) still resume via --continue, once
    legacy = build_spec(AgentKind.CLAUDE, "Legacy", cwd=SCRATCH_CWD, pty=True)
    legacy.session_id = ""
    legacy.resume = True
    check("pin: legacy unpinned resume falls back to --continue",
          "--continue" in legacy.effective_args())

    # a failed resume (conversation gone) relaunches fresh under a NEW id
    gone = "11111111-1111-4111-8111-111111111111"
    spec2 = build_spec(AgentKind.CLAUDE, "Fb", cwd=SCRATCH_CWD, pty=True)
    spec2.session_id = gone
    spec2.resume = True
    a2 = TerminalAgent(spec2)
    c2 = {"n": 0}
    stub(a2, c2)
    a2.start()
    a2._prompt_ready = False
    a2.status = AgentStatus.RUNNING
    a2._on_finished(1, False)  # died before the prompt -> fresh-fallback
    check("pin: failed resume relaunches fresh under a new id",
          c2["n"] == 2 and spec2.session_id != gone
          and bool(uuid_re.match(spec2.session_id)), spec2.session_id)

    # the pin survives save/restore
    back = AgentSpec.from_dict(spec2.to_dict())
    check("pin: session id survives save/restore",
          back.session_id == spec2.session_id)

    # nested-session env markers must never reach agents: a claude launched
    # with CLAUDECODE/CLAUDE_CODE_* in its env silently disables transcript
    # persistence (verified live), making conversations unsaved+unresumable
    from app.pty_worker import agent_environment
    had = os.environ.get("CLAUDECODE")
    os.environ["CLAUDECODE"] = "1"
    os.environ["CLAUDE_CODE_TEST_MARKER"] = "x"
    env = agent_environment()
    os.environ.pop("CLAUDE_CODE_TEST_MARKER", None)
    if had is None:
        os.environ.pop("CLAUDECODE", None)
    else:
        os.environ["CLAUDECODE"] = had
    check("pin: nested claude markers stripped from agent env",
          "CLAUDECODE" not in env and "CLAUDE_CODE_TEST_MARKER" not in env
          and any(k.upper() == "PATH" for k in env))

    # Claude readiness = the input-box footer. The folder-trust dialog also
    # enables bracketed paste (and never disables it on dismissal — verified
    # live), so 2004h alone would type a queued task INTO the dialog.
    spec3 = build_spec(AgentKind.CLAUDE, "Trust", cwd=SCRATCH_CWD, pty=True)
    a3 = TerminalAgent(spec3)
    a3._on_pty_output("", "Do you trust\x1b[20Gthis\x1b[26Gfolder?\x1b[?2004h")
    check("pin: trust dialog does not trip prompt-ready",
          not a3._prompt_ready)
    a3._on_pty_output("", "\x1b[2G? for shortcuts \x1b[38;2;1;2;3m- more")
    check("pin: input-box footer marks Claude readiness", a3._prompt_ready)
    # split across chunks: the rolling tail must still assemble the footer
    a5 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Split", cwd=SCRATCH_CWD,
                                  pty=True))
    a5._on_pty_output("", "\x1b[?2004h? for sho")
    a5._on_pty_output("", "rtcuts")
    check("pin: footer split across chunks still detected", a5._prompt_ready)
    # non-Claude ptys (PSReadLine etc.) keep the paste-enable signal
    a4 = TerminalAgent(build_spec(AgentKind.POWERSHELL, "Sh", cwd=SCRATCH_CWD,
                                  pty=True))
    a4._on_pty_output("", "\x1b[?2004h")
    check("pin: non-claude pty still ready on paste-enable", a4._prompt_ready)


def test_two_claude_agents_one_folder_resume_their_own():
    """THE historical data-loss bug, at the level where it lived: two Claude
    agents in ONE folder, the app closes, the app reopens. Each must resume
    its OWN conversation by id. With --continue both raced for the folder's
    newest conversation and one transcript was destroyed. The e2e test covers
    one real agent; this covers the pair without launching anything."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    folder = Path(tempfile.mkdtemp(prefix="ai-hive-pair-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Pair", str(folder))
    pair = []
    for name in ("Left", "Right"):
        a = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, name,
                                               cwd=str(folder)),
                             autostart=False)
        a.worker.start = lambda: None      # mint the pin, launch nothing
        a.start()
        pair.append(a)
    ids = {a.spec.name: a.spec.session_id for a in pair}
    check("pair: two agents in one folder get two different pins",
          all(ids.values()) and ids["Left"] != ids["Right"], ids)

    # what a save writes. Taken without any close, so it is also what an
    # abrupt kill leaves on disk; both were running.
    data = mgr.to_session_dict()
    for w in data["workspaces"]:
        for t in w["terminals"]:
            t["running"] = True
    # through the real reopen path: create_main_window is what marks every
    # restored Claude agent for a one-shot resume
    from app.session_store import SessionStore
    from main import create_main_window
    store = SessionStore(path=folder / "s.json")
    store.save(data)
    win = create_main_window(store)
    restored = {a.spec.name: a for w in win.manager.workspaces
                for a in w.agents if a.spec.provider == "claude"}
    check("pair: both come back, each with its own pin",
          {n: a.spec.session_id for n, a in restored.items()} == ids,
          {n: a.spec.session_id for n, a in restored.items()})
    for name, a in restored.items():
        args = a.spec.effective_args()
        check(f"pair: {name} resumes ITS conversation (--resume <own id>)",
              "--resume" in args
              and args[args.index("--resume") + 1] == ids[name]
              and "--continue" not in args, args)
    win._save_timer.stop()
    win.close()
    for a in pair:
        a.dispose()
    shutil.rmtree(folder, ignore_errors=True)


def test_session_recovery():
    """A Claude agent's pinned session id must track the conversation it is
    REALLY writing, and a resume whose pinned transcript is gone must recover
    the folder's most recent one rather than error on a black terminal.
    Regression for two live incidents: a stale pin brought back a closed
    morning thread instead of the afternoon one, and a fresh never-used id
    made `--resume` fail on reopen."""
    from app import session_sync
    from app.session_sync import AgentInfo, resolve_live_ids
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.workspace_manager import WorkspaceManager

    A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    D = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    # -- reconciliation logic (pure, filesystem injected) ------------------
    files = {A: 1000.0, B: 2000.0}
    drift = AgentInfo("k", "C:/proj", A, 500.0)  # pinned old, B written since
    check("recover: drift to the conversation the agent is really writing",
          resolve_live_ids([drift], lister=lambda c: dict(files)) == {"k": B})
    live = AgentInfo("k", "C:/proj", B, 500.0)
    check("recover: pinned to the live conversation is a no-op",
          resolve_live_ids([live], lister=lambda c: dict(files)) == {})
    stale_only = AgentInfo("k", "C:/proj", C, 9000.0)  # nothing since start
    check("recover: a transcript written before this run is never adopted",
          resolve_live_ids([stale_only], lister=lambda c: dict(files)) == {})
    # two agents share a folder: the filesystem can't say which agent owns
    # which transcript (a resume touches them all at launch), so a multi-agent
    # folder is left EXACTLY as launched -- NEVER auto-reshuffled. Mis-
    # attribution swapped two live conversations and pushed a good chat onto an
    # empty /recap stub (a real, thrice-repeated incident).
    two = {A: 1000.0, B: 5000.0, D: 3000.0}
    a1 = AgentInfo("a1", "C:/proj", A, 500.0)
    a2 = AgentInfo("a2", "C:/proj", B, 500.0)
    check("recover: a multi-agent folder is never auto-reshuffled",
          resolve_live_ids([a1, a2], lister=lambda c: dict(two)) == {})

    # a substantial pinned conversation must NOT be demoted to a fresh empty
    # stub, even though the stub was written more recently (the left-card-came-
    # back-blank incident: a stray /recap-only session stranded a 10 MB chat)
    big, stub = 50000, 300  # bytes
    sz = {A: big, D: stub}
    demote = {A: 1000.0, D: 2000.0}  # D newer but empty
    keeps = AgentInfo("k", "C:/proj", A, 500.0)
    check("recover: a real conversation is never demoted to an empty stub",
          resolve_live_ids([keeps], lister=lambda c: dict(demote),
                           sizer=lambda c, s: sz.get(s, 0)) == {})
    # but when the current pin is ITSELF a stub, following the newer one is fine
    sz2 = {A: stub, D: stub + 10}
    check("recover: a stub pin still follows the newer conversation",
          resolve_live_ids([keeps], lister=lambda c: dict(demote),
                           sizer=lambda c, s: sz2.get(s, 0)) == {"k": D})

    # THE INCIDENT (three times over): two agents in one folder, and mtime
    # correlation swapped them -- one agent's substantial conversation got
    # pushed onto the other's empty /recap stub, orphaning a 9 MB chat. With two
    # agents present NOTHING is auto-repointed, however tempting the transcripts
    # look: a hot conversation, a tiny stub, a stale pin -- all left alone.
    S = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"  # a stub launch pin
    X = "ffffffff-ffff-ffff-ffff-ffffffffffff"  # a hot conversation
    inc = {S: 600.0, C: 1000.0, X: 3000.0}
    sw = AgentInfo("sw", "C:/proj", S, 500.0)
    idle = AgentInfo("idle", "C:/proj", C, 500.0)
    check("recover: two-agent folder untouched even with a hot transcript",
          resolve_live_ids([sw, idle], lister=lambda c: dict(inc),
                           sizer=lambda c, sid: {S: 2_000, C: 1_000_000,
                                                 X: 700_000}.get(sid, 0)) == {})

    # -- verify-before-resume at start(), against a real temp ~/.claude ----
    def stub(agent):
        agent.worker = type("W", (), {
            "start": lambda s: None, "restart": lambda s: None,
            "is_running": lambda s: False, "dispose": lambda s: None})()

    home = Path(tempfile.mkdtemp(prefix="ai-hive-home-"))
    cwd = Path(tempfile.mkdtemp(prefix="ai-hive-cwd-"))
    proj = home / ".claude" / "projects" / (
        session_sync.transcripts.encode_project_dir(str(cwd)))
    proj.mkdir(parents=True)
    (proj / f"{D}.jsonl").write_text("{}\n", encoding="utf-8")  # the real convo
    old_home, old_up = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    try:
        # pinned id was never written -> recover the folder's real conversation
        spec = build_spec(AgentKind.CLAUDE, "Rec", cwd=str(cwd), pty=True)
        spec.session_id, spec.resume = C, True
        a = TerminalAgent(spec)
        a._verify_resume_target = True
        stub(a)
        a.start()
        check("recover: resume of a missing pin recovers the real conversation",
              spec.session_id == D, spec.session_id)
        check("recover: the recovered launch still resumes (not fresh)",
              a._resume_attempt is True)

        # pinned id that DOES exist is left exactly as-is
        spec2 = build_spec(AgentKind.CLAUDE, "Keep", cwd=str(cwd), pty=True)
        spec2.session_id, spec2.resume = D, True
        a_keep = TerminalAgent(spec2)
        a_keep._verify_resume_target = True
        stub(a_keep)
        a_keep.start()
        check("recover: an existing pinned conversation is never disturbed",
              spec2.session_id == D)

        # a sibling's conversation is off-limits: recover the OTHER one
        (proj / f"{A}.jsonl").write_text("{}\n", encoding="utf-8")
        spec3 = build_spec(AgentKind.CLAUDE, "Sib", cwd=str(cwd), pty=True)
        spec3.session_id, spec3.resume = C, True
        a_sib = TerminalAgent(spec3)
        a_sib._verify_resume_target = True
        a_sib._sibling_sessions = lambda: {D}  # D belongs to a peer here
        stub(a_sib)
        a_sib.start()
        check("recover: recovery skips a sibling's live conversation",
              spec3.session_id == A and spec3.session_id != D, spec3.session_id)

        # an unused folder: leave the resume alone (the fast-fail fallback,
        # not us, decides), so behavior is unchanged where there's nothing
        empty_cwd = Path(tempfile.mkdtemp(prefix="ai-hive-empty-"))
        spec4 = build_spec(AgentKind.CLAUDE, "New", cwd=str(empty_cwd), pty=True)
        spec4.session_id, spec4.resume = C, True
        a_new = TerminalAgent(spec4)
        a_new._verify_resume_target = True
        stub(a_new)
        a_new.start()
        check("recover: unused folder leaves the pin for the fresh-fallback",
              spec4.session_id == C and a_new._resume_attempt is True)
        shutil.rmtree(empty_cwd, ignore_errors=True)

        # recovery prefers a SUBSTANTIAL conversation over a newer empty stub,
        # so a stray fresh session never wins the resume
        big_cwd = Path(tempfile.mkdtemp(prefix="ai-hive-big-"))
        bproj = home / ".claude" / "projects" / (
            session_sync.transcripts.encode_project_dir(str(big_cwd)))
        bproj.mkdir(parents=True)
        (bproj / f"{A}.jsonl").write_text("x" * 50000, encoding="utf-8")  # real
        (bproj / f"{B}.jsonl").write_text("{}\n", encoding="utf-8")       # stub
        os.utime(bproj / f"{A}.jsonl", (100.0, 100.0))   # older
        os.utime(bproj / f"{B}.jsonl", (200.0, 200.0))   # newer, but empty
        check("recover: a newer empty stub never beats a real conversation",
              session_sync.best_recovery_id(str(big_cwd)) == A)
        shutil.rmtree(big_cwd, ignore_errors=True)

        # -- manager surface: sibling lookup, multi-agent safety, lone track --
        mgr = WorkspaceManager()
        ws = mgr.create_workspace("Rec", project_path=str(cwd))
        s_peer = build_spec(AgentKind.CLAUDE, "Peer", cwd=str(cwd), pty=True)
        s_peer.session_id = D
        peer = mgr.add_terminal(ws.id, s_peer, autostart=False)
        s_drift = build_spec(AgentKind.CLAUDE, "Drift", cwd=str(cwd), pty=True)
        s_drift.session_id = C
        drifter = mgr.add_terminal(ws.id, s_drift, autostart=False)
        check("recover: manager reports a folder-mate's pinned id",
              mgr.sibling_session_ids(drifter) == {D})
        for ag in (peer, drifter):
            ag.worker = type("W", (), {"is_running": lambda s: True})()
            ag._session_started = 1.0
        os.utime(proj / f"{A}.jsonl", (10.0, 10.0))  # a newer transcript appears
        changed_multi = mgr.sync_live_sessions()
        check("recover: sync NEVER reshuffles a multi-agent folder",
              changed_multi == [] and s_drift.session_id == C
              and s_peer.session_id == D, (changed_multi, s_drift.session_id))

        # a LONE agent in its folder IS tracked -- the unambiguous, safe case:
        # it follows the conversation it is actually writing
        solo_cwd = Path(tempfile.mkdtemp(prefix="ai-hive-solo-"))
        sproj = home / ".claude" / "projects" / (
            session_sync.transcripts.encode_project_dir(str(solo_cwd)))
        sproj.mkdir(parents=True)
        (sproj / f"{A}.jsonl").write_text("x" * 50000, encoding="utf-8")  # real
        os.utime(sproj / f"{A}.jsonl", (10.0, 10.0))
        ws2 = mgr.create_workspace("Solo", project_path=str(solo_cwd))
        s_solo = build_spec(AgentKind.CLAUDE, "Solo", cwd=str(solo_cwd), pty=True)
        s_solo.session_id = C  # its real conversation A is on disk, newer
        solo = mgr.add_terminal(ws2.id, s_solo, autostart=False)
        solo.worker = type("W", (), {"is_running": lambda s: True})()
        solo._session_started = 1.0
        dirty = {"n": 0}
        mgr.dirty.connect(lambda: dirty.__setitem__("n", dirty["n"] + 1))
        changed_solo = mgr.sync_live_sessions()
        check("recover: a lone agent is tracked to its live conversation",
              s_solo.session_id == A
              and any(c[0] == solo.id for c in changed_solo),
              (s_solo.session_id, changed_solo))
        check("recover: a pin change marks the session dirty (persisted)",
              dirty["n"] >= 1)
        shutil.rmtree(solo_cwd, ignore_errors=True)
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_up is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = old_up
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)


def test_session_hook_tracking():
    """The AUTHORITATIVE live-id path: a Claude SessionStart hook reports each
    agent's real current conversation (keyed by AIHIVE_AGENT_ID), so an in-TUI
    /resume or /clear is captured even in a MULTI-AGENT folder -- the case the
    filesystem cannot disambiguate and which mtime correlation must refuse.
    Regression for the whole class of close/reopen conversation loss.

    The live end-to-end (a real claude firing the hook on an in-TUI switch) was
    proven against Claude 2.1.197 during development; here we lock in the
    plumbing and the reconciliation logic with an injected mapping file."""
    import json
    from app import session_hook, session_sync
    from app.process_worker import AgentKind, build_spec
    from app.workspace_manager import WorkspaceManager

    A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # agent 1 launch pin
    B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # agent 2 launch pin
    N1 = "11111111-1111-1111-1111-111111111111"  # agent 1 switched-to convo
    N2 = "22222222-2222-2222-2222-222222222222"  # agent 2 switched-to convo

    d = Path(tempfile.mkdtemp(prefix="ai-hive-hook-"))
    settings_path = str(d / "aihive_session_hook.json")
    map_path = str(d / "live_sessions.jsonl")

    # -- settings file carries a SessionStart hook pointing at the map --------
    session_hook.write_settings_file(settings_path, map_path)
    s = json.loads(Path(settings_path).read_text(encoding="utf-8"))
    hook_cmd = s.get("hooks", {}).get("SessionStart", [{}])[0].get(
        "hooks", [{}])[0].get("command", "")
    check("hook: --settings file defines a SessionStart command hook",
          "SessionStart" in s.get("hooks", {}) and "session_hook.py" in hook_cmd
          and map_path in hook_cmd, hook_cmd)
    # CRITICAL: the matcher must EXCLUDE 'startup'. A startup firing is
    # redundant (the launch id already equals the pin) AND perturbs the TUI's
    # launch settle so a freshly-spawned agent drops its first task-submit Enter
    # and silently never runs the task (verified live). Only in-TUI switches.
    matcher = s["hooks"]["SessionStart"][0].get("matcher", "")
    check("hook: matcher fires on in-TUI switches but NOT startup",
          "startup" not in matcher and "resume" in matcher and "clear" in matcher,
          matcher)
    # only 'hooks' is present, so --settings adds ours without clobbering the
    # user's other settings (Claude merges hooks across sources -- verified live)
    check("hook: settings file touches ONLY the hooks key",
          list(s.keys()) == ["hooks"], list(s.keys()))

    # -- reset + append + read round-trip, keyed by agent id ------------------
    def write_hook_line(agent_id, session_id):
        old = os.environ.get(session_hook.AGENT_ID_ENV)
        os.environ[session_hook.AGENT_ID_ENV] = agent_id
        try:
            session_hook._append_record(map_path, {
                "session_id": session_id, "transcript_path": f"{session_id}.jsonl",
                "cwd": str(d), "source": "resume",
                "hook_event_name": "SessionStart"})
        finally:
            if old is None:
                os.environ.pop(session_hook.AGENT_ID_ENV, None)
            else:
                os.environ[session_hook.AGENT_ID_ENV] = old

    session_hook.reset_map(map_path)
    check("hook: reset truncates the mapping file",
          session_hook.read_live_map(map_path) == {})
    write_hook_line("AG1", A)      # startup lines
    write_hook_line("AG2", B)
    write_hook_line("AG1", N1)     # AG1 switched conversations in-TUI
    live = session_hook.read_live_map(map_path)
    check("hook: read_live_map returns each agent's LATEST reported id",
          live["AG1"]["session_id"] == N1 and live["AG2"]["session_id"] == B,
          {k: v["session_id"] for k, v in live.items()})

    # -- THE multi-agent case: two agents share ONE folder, and the hook lets
    #    each pin follow its OWN switch with NO guessing and NO swap ----------
    mgr = WorkspaceManager()
    mgr.session_map_path = map_path
    ws = mgr.create_workspace("Hook", project_path=str(d))
    s1 = build_spec(AgentKind.CLAUDE, "A1", cwd=str(d), pty=True)
    s1.session_id = A
    a1 = mgr.add_terminal(ws.id, s1, autostart=False)
    s2 = build_spec(AgentKind.CLAUDE, "A2", cwd=str(d), pty=True)
    s2.session_id = B
    a2 = mgr.add_terminal(ws.id, s2, autostart=False)
    for ag in (a1, a2):
        ag.worker = type("W", (), {"is_running": lambda s: True})()
        ag._session_started = 1.0

    # rewrite the map so each agent's LIVE id is keyed by its real agent id
    session_hook.reset_map(map_path)
    write_hook_line(a1.id, N1)   # agent 1 switched to N1
    write_hook_line(a2.id, N2)   # agent 2 switched to N2
    dirty = {"n": 0}
    mgr.dirty.connect(lambda: dirty.__setitem__("n", dirty["n"] + 1))
    changed = mgr.sync_live_sessions()
    check("hook: multi-agent folder IS tracked when the child reports its id",
          s1.session_id == N1 and s2.session_id == N2,
          (s1.session_id, s2.session_id))
    check("hook: each agent follows its OWN conversation (no swap/orphan)",
          {c[0]: c[2] for c in changed} == {a1.id: N1, a2.id: N2},
          changed)
    check("hook: an authoritative pin change marks the session dirty",
          dirty["n"] >= 1)

    # a second sync with the SAME map is a no-op (pins already match) ----------
    check("hook: re-sync with unchanged map changes nothing",
          mgr.sync_live_sessions() == [])

    # a garbled reported id is ignored -- never becomes a bogus --resume target
    session_hook.reset_map(map_path)
    write_hook_line(a1.id, "not-a-valid-uuid")
    write_hook_line(a2.id, N2)  # unchanged
    check("hook: an invalid reported id is refused",
          mgr.sync_live_sessions() == [] and s1.session_id == N1)

    # an agent the hook has NOT reported in a multi-agent folder is left alone:
    # the filesystem fallback still refuses to guess who owns what
    session_hook.reset_map(map_path)          # nobody reported
    check("hook: no hook report + multi-agent folder -> untouched (no guess)",
          mgr.sync_live_sessions() == []
          and s1.session_id == N1 and s2.session_id == N2)

    # THE SUBTLE one: when one agent is hook-covered and its switch left a hot
    # transcript on disk, its UNCOVERED folder-mate must NOT be mtime-stolen
    # onto that transcript. The fallback must see BOTH agents (a multi-agent
    # folder) and refuse to guess -- dropping the covered agent from the mtime
    # pass would make the sibling look solo and grab the wrong conversation.
    s1.session_id, s2.session_id = A, B  # reset pins for this scenario
    old_home, old_up = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    home = Path(tempfile.mkdtemp(prefix="ai-hive-hookhome-"))
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    try:
        proj = home / ".claude" / "projects" / (
            session_sync.transcripts.encode_project_dir(str(d)))
        proj.mkdir(parents=True)
        (proj / f"{N1}.jsonl").write_text("x" * 50000, encoding="utf-8")  # a1 switch
        (proj / f"{B}.jsonl").write_text("x" * 50000, encoding="utf-8")   # a2 own
        os.utime(proj / f"{B}.jsonl", (10.0, 10.0))       # older
        os.utime(proj / f"{N1}.jsonl", (9999.0, 9999.0))  # newest -> mtime bait
        session_hook.reset_map(map_path)
        write_hook_line(a1.id, N1)   # only agent 1 reports (covered)
        changed = mgr.sync_live_sessions()
        check("hook: a covered agent's hot transcript never mtime-steals a sibling",
              s2.session_id == B, (s2.session_id, changed))
        check("hook: the covered agent still tracks its own reported id",
              s1.session_id == N1)
    finally:
        for _k, _v in (("HOME", old_home), ("USERPROFILE", old_up)):
            if _v is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _v
        shutil.rmtree(home, ignore_errors=True)

    shutil.rmtree(d, ignore_errors=True)


def test_session_hook_arming():
    """Every Claude agent is armed with the shared SessionStart hook BEFORE it
    starts (and re-armed on restore): --settings points at the shared hook file
    and AIHIVE_AGENT_ID == the agent id sync_live_sessions matches on. Non-Claude
    agents are never armed. Runs a real (offscreen) MainWindow like the e2e."""
    from PySide6.QtWidgets import QApplication
    from app import session_hook
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-arm-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)  # first-run: Workspace 1 + a PowerShell agent
    try:
        # the shared hook plumbing is written into the session dir at startup
        check("arm: shared hook settings file written at startup",
              (tmp / "aihive_session_hook.json").is_file())
        check("arm: shared mapping file created at startup",
              (tmp / "live_sessions.jsonl").is_file())

        ws = win.manager.workspaces[0]
        # the first-run PowerShell agent must NOT be armed (not a Claude agent)
        ps = ws.agents[0]
        check("arm: a non-Claude agent gets no hook settings",
              not ps.spec.settings_path
              and session_hook.AGENT_ID_ENV not in ps.spec.env)

        # a Claude agent, added the normal way (arm_agent runs in add_terminal)
        spec = build_spec(AgentKind.CLAUDE, "Armed", cwd=str(tmp), pty=True)
        agent = win.manager.add_terminal(ws.id, spec, autostart=False)
        check("arm: a Claude agent gets --settings for the shared hook file",
              agent.spec.settings_path == win._hook_settings_path
              and "--settings" in agent.spec.effective_args())
        check("arm: AIHIVE_AGENT_ID == the agent id (maps a hook line back)",
              agent.spec.env.get(session_hook.AGENT_ID_ENV) == agent.id)
    finally:
        win.close()
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_picker():
    """The New-Agent dialog offers resumable Claude conversations from the
    workspace folder, excludes any a running agent still holds, and pins the
    chosen one so it launches with --resume <id>."""
    import json
    import uuid

    from PySide6.QtWidgets import QApplication

    from app import session_sync
    from app.widgets.main_window import AddTerminalDialog

    QApplication.instance() or QApplication([])

    # -- _first_user_text: first REAL user message; wrappers/tool lines skipped
    d = Path(tempfile.mkdtemp(prefix="ai-hive-resume-"))

    def convo(name, entries):
        p = d / name
        p.write_text("\n".join(json.dumps(x) for x in entries) + "\n",
                     encoding="utf-8")
        return str(p)

    real = convo("real.jsonl", [
        {"type": "user", "message": {"role": "user",
         "content": "<command-name>/clear</command-name>"}},   # wrapper -> skip
        {"type": "user", "message": {"role": "user",
         "content": "fix the parser bug"}}])
    blocks = convo("blocks.jsonl", [
        {"type": "user", "message": {"content": [
            {"type": "text", "text": "add dark mode"}]}}])
    empty = convo("empty.jsonl", [{"type": "system", "subtype": "init"}])
    check("resume: first user text (string content)",
          session_sync._first_user_text(real) == "fix the parser bug")
    check("resume: first user text (block content)",
          session_sync._first_user_text(blocks) == "add dark mode")
    check("resume: no user message -> empty preview",
          session_sync._first_user_text(empty) == "")
    shutil.rmtree(d, ignore_errors=True)

    # -- conversation_previews + the dialog, against a temp ~/.claude ----------
    home = Path(tempfile.mkdtemp(prefix="ai-hive-rhome-"))
    cwd = Path(tempfile.mkdtemp(prefix="ai-hive-rcwd-"))
    proj = home / ".claude" / "projects" / (
        session_sync.transcripts.encode_project_dir(str(cwd)))
    proj.mkdir(parents=True)
    A, B, BUSY = (str(uuid.UUID(int=1)), str(uuid.UUID(int=2)),
                  str(uuid.UUID(int=3)))
    (proj / f"{A}.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user",
         "content": "resume me please"}}) + "\n", encoding="utf-8")
    (proj / f"{B}.jsonl").write_text("{}\n", encoding="utf-8")  # stub
    (proj / f"{BUSY}.jsonl").write_text(json.dumps(
        {"type": "user", "message": {"role": "user",
         "content": "in use"}}) + "\n", encoding="utf-8")
    os.utime(proj / f"{A}.jsonl", (300, 300))     # newest
    os.utime(proj / f"{B}.jsonl", (200, 200))
    os.utime(proj / f"{BUSY}.jsonl", (100, 100))
    old_home, old_up = os.environ.get("HOME"), os.environ.get("USERPROFILE")
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(home)
    try:
        previews = session_sync.conversation_previews(str(cwd))
        check("resume: previews sorted newest-first",
              [c.session_id for c in previews] == [A, B, BUSY], previews)
        check("resume: preview text parsed off the transcript",
              previews[0].preview == "resume me please", previews[0])

        dlg = AddTerminalDialog("Agent 9", cwd=str(cwd), busy_ids={BUSY})
        ids = [dlg.resume_combo.itemData(i)
               for i in range(dlg.resume_combo.count())]
        check("resume: Claude picker offers New + folder conversations",
              ids and ids[0] == "" and A in ids and B in ids, ids)
        check("resume: a running agent's conversation is excluded",
              BUSY not in ids, ids)
        check("resume: picker is shown for Claude with conversations",
              not dlg.resume_combo.isHidden())

        dlg.resume_combo.setCurrentIndex(ids.index(A))
        spec = dlg.result_spec(cwd=str(cwd))
        check("resume: chosen conversation pins the spec (--resume <id>)",
              spec.resume and spec.session_id == A
              and spec.effective_args()[-2:] == ["--resume", A], spec)

        dlg.resume_combo.setCurrentIndex(0)  # 'New conversation'
        fresh = dlg.result_spec(cwd=str(cwd))
        check("resume: 'New conversation' starts fresh (no resume)",
              not fresh.resume and not fresh.session_id)

        # -- permission-mode (Shift+Tab) picker: Claude-only, default = Normal
        mode_vals = [dlg.mode_combo.itemData(i)
                     for i in range(dlg.mode_combo.count())]
        check("perm-mode: dialog offers the Shift+Tab modes for Claude",
              not dlg.mode_combo.isHidden() and mode_vals[0] == ""
              and "acceptEdits" in mode_vals and "plan" in mode_vals, mode_vals)
        check("perm-mode: default selection carries no flag",
              dlg.result_spec(cwd=str(cwd)).permission_mode == "")
        dlg.mode_combo.setCurrentIndex(mode_vals.index("plan"))
        pm_dlg_spec = dlg.result_spec(cwd=str(cwd))
        check("perm-mode: chosen mode flows into the spec",
              pm_dlg_spec.permission_mode == "plan"
              and pm_dlg_spec.effective_args()[-2:] == ["--permission-mode", "plan"],
              pm_dlg_spec.effective_args())
    finally:
        for k, v in (("HOME", old_home), ("USERPROFILE", old_up)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(home, ignore_errors=True)
        shutil.rmtree(cwd, ignore_errors=True)


def test_wake_and_resume_all():
    """The hive comes back WHOLE on reopen: previously-running agents
    autostart in EVERY workspace (not just the active one), every restored
    Claude agent carries one-shot resume for whenever it next starts, and a
    stopped pty card is never a dead black screen — it shows a wake banner
    and starts on the first keystroke. (The regression: a user switching to a
    non-active workspace found an unlabeled black terminal that ate input.)"""
    import json as _json  # noqa: F401
    import time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_until(pred, timeout_ms=15000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-wake-"))
    term = {"role": "", "cwd": str(tmp), "user_program": "", "user_args": [],
            "pty": False, "provider": "", "model": "", "effort": "",
            "custom_command": "", "font_px": 0, "is_orchestrator": False,
            "task": "", "assignment": "idle", "auto_created": False}
    store = SessionStore(path=tmp / "s.json")
    store.save({"version": 3, "active": "wa", "workspaces": [
        {"id": "wa", "name": "Front", "project_path": str(tmp),
         "layout": "auto", "terminals": [
             {**term, "kind": "cmd", "name": "Stopped", "running": False}]},
        {"id": "wb", "name": "Back", "project_path": str(tmp),
         "layout": "auto", "terminals": [
             {**term, "kind": "cmd", "name": "BgRunner", "running": True},
             {**term, "kind": "claude", "name": "Coder", "provider": "claude",
              "pty": True, "running": False}]}]})

    win = create_main_window(store)
    win.show(); pump(150)
    mgr = win.manager
    back = next(w for w in mgr.workspaces if w.name == "Back")
    front = next(w for w in mgr.workspaces if w.name == "Front")
    bg = next(a for a in back.agents if a.spec.name == "BgRunner")
    coder = next(a for a in back.agents if a.spec.name == "Coder")
    stopped = front.agents[0]

    check("wake: a restored LEGACY unpinned Claude carries one-shot --continue",
          coder.spec.resume and "--continue" in coder.spec.effective_args())
    win.autostart_active_workspace()
    check("wake: running agent in a NON-active workspace autostarts",
          wait_until(lambda: bg.is_running()))
    pump(300)
    check("wake: stopped agents stay stopped (no side-effect runs)",
          not stopped.is_running() and not coder.is_running())
    win.close(); pump(250)

    # stopped pty card: visible banner + press-any-key wake
    if HAS_CONPTY:
        spec = build_spec(AgentKind.POWERSHELL, "Waker", cwd=str(tmp), pty=True)
        ag = TerminalAgent(spec)
        card = TerminalCard(ag)
        card.resize(640, 400); card.show(); pump(150)
        check("wake: stopped pty card shows the wake banner",
              card.overlay.isVisible())
        card._on_key_input("x")  # the first keystroke wakes the terminal
        check("wake: keystroke starts the session",
              wait_until(lambda: ag.is_running()))
        pump(200)
        check("wake: banner hides once running", not card.overlay.isVisible())
        card.detach()
        ag.dispose(); pump(250)
    shutil.rmtree(tmp, ignore_errors=True)


def test_screen_snapshots():
    """A card left STOPPED reopens showing the conversation it had at close,
    not a black rectangle with a banner over it. (The regression: a reopened
    hive read as a wall of dead terminals, because "restore as it was" only
    ever restored the process state, never the screen.)

    The snapshot is keyed on (cwd, pinned conversation) because agent ids are
    minted fresh on every load, and it lives in its own file, never in
    session.json."""
    import pathlib
    import shutil
    import tempfile
    import time

    from app import screen_snapshot
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aihive-screens-"))

    # -- keying -----------------------------------------------------------
    k = screen_snapshot.key_of(str(tmp), "sess-1")
    check("screens: key is stable for the same (cwd, conversation)",
          k == screen_snapshot.key_of(str(tmp), "sess-1"))
    check("screens: a different conversation is a different key",
          k != screen_snapshot.key_of(str(tmp), "sess-2"))
    check("screens: a different folder is a different key",
          k != screen_snapshot.key_of(str(tmp / "other"), "sess-1"))
    check("screens: the key is filesystem-safe",
          k.isalnum() and len(k) <= 24, k)

    # -- round-trip -------------------------------------------------------
    vt = "hello \x1b[32mworld\x1b[0m\r\n> the conversation\r\n"
    check("screens: save reports success", screen_snapshot.save(
        str(tmp), str(tmp), "sess-1", vt))
    check("screens: raw VT round-trips byte for byte",
          screen_snapshot.load(str(tmp), str(tmp), "sess-1") == vt)
    check("screens: a conversation with no snapshot loads empty",
          screen_snapshot.load(str(tmp), str(tmp), "nope") == "")
    check("screens: an empty screen is not written",
          not screen_snapshot.save(str(tmp), str(tmp), "sess-x", ""))
    check("screens: a pin-less agent is never keyed",
          not screen_snapshot.save(str(tmp), str(tmp), "", vt))
    check("screens: the snapshot is NOT in session.json",
          not (tmp / "session.json").exists())

    # oversized screens are capped, and the TAIL is what survives
    big = ("x" * 10) + ("y" * (screen_snapshot.MAX_BYTES + 500))
    screen_snapshot.save(str(tmp), str(tmp), "sess-big", big)
    got = screen_snapshot.load(str(tmp), str(tmp), "sess-big")
    check("screens: an oversized screen is capped",
          len(got) == screen_snapshot.MAX_BYTES, len(got))
    check("screens: the capped screen keeps the TAIL (the recent output)",
          got.endswith("y" * 100) and not got.startswith("x"))

    # a corrupt/unreadable snapshot degrades to an empty card, never a raise
    bad = pathlib.Path(screen_snapshot._path(
        str(tmp), screen_snapshot.key_of(str(tmp), "sess-bad")))
    bad.write_bytes(b"\xff\xfe\x00raw")
    check("screens: an unreadable snapshot degrades to empty, never raises",
          isinstance(screen_snapshot.load(str(tmp), str(tmp), "sess-bad"), str))

    # -- pruning ----------------------------------------------------------
    removed = screen_snapshot.prune(str(tmp), {k})
    check("screens: unclaimed snapshots are pruned",
          removed >= 2 and screen_snapshot.load(
              str(tmp), str(tmp), "sess-big") == "")
    check("screens: a claimed snapshot survives pruning",
          screen_snapshot.load(str(tmp), str(tmp), "sess-1") == vt)

    # -- seeding a restored agent -----------------------------------------
    spec = build_spec(AgentKind.CLAUDE, "Restored", cwd=str(tmp), pty=True)
    spec.session_id = "sess-1"
    agent = TerminalAgent(spec)
    check("screens: keys_for_agents reports the agent's pin",
          screen_snapshot.keys_for_agents([agent]) == {k})
    check("screens: a restored agent is seeded with its last screen",
          agent.seed_pty_replay(vt) and agent.pty_replay() == vt)
    check("screens: seeding never overwrites a screen already there",
          not agent.seed_pty_replay("clobber")
          and agent.pty_replay() == vt)
    check("screens: an empty snapshot seeds nothing",
          not TerminalAgent(build_spec(
              AgentKind.CLAUDE, "Blank", cwd=str(tmp),
              pty=True)).seed_pty_replay(""))
    # restart() is a DELIBERATE fresh session: the old screen must not linger
    agent.worker = type("_W", (), {"restart": lambda s: None,
                                   "is_running": lambda s: False,
                                   "dispose": lambda s: None})()
    agent.restart()
    check("screens: a deliberate restart drops the restored screen",
          agent.pty_replay() == "")
    agent.dispose()

    # -- the card paints it, and the banner gets out of the way ------------
    if HAS_CONPTY:
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication

        from app.widgets.terminal_card import TerminalCard
        QApplication.instance() or QApplication([])

        def pump(ms):
            loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

        def wait_until(pred, timeout_ms=4000, step=50):
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                if pred():
                    return True
                pump(step)
            return pred()

        seeded = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Seeded", cwd=str(tmp), pty=True))
        seeded.seed_pty_replay("the previous conversation\r\n")
        card = TerminalCard(seeded)
        card.resize(640, 400); card.show(); pump(150)
        # the tiling grid resizes the card AFTER it is built, and pyte drops
        # lines off the TOP when it shrinks: without the one-shot re-render,
        # a short restored screen is gone before the user ever sees it
        check("screens: the restored card paints its conversation",
              "the previous conversation" in card.terminal.screen_text())
        check("screens: the re-render is one-shot (a retile cannot repeat it)",
              card._pending_replay == "")
        check("screens: the banner still says the terminal is not live",
              card.overlay.isVisible())
        check("screens: ...as a slim footer, not a box over the conversation",
              card._overlay_compact
              and card.overlay.height() < 40
              and card.overlay.y() > card.terminal.height() // 2)
        card.detach(); seeded.dispose(); pump(150)

        # An AUTOSTARTED agent is already running long before the first
        # settled size arrives: the launch starts agents synchronously right
        # after show(), while TerminalView debounces its resize by 120ms. So
        # gating the re-render on "not running" skipped exactly the cards that
        # needed it, and every restored-and-resumed card came back showing a
        # mangled narrow fragment in the top-left of a full-width terminal.
        from app.process_worker import WorkerState
        line = "R" * 60
        waking = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Waking", cwd=str(tmp), pty=True))
        waking.seed_pty_replay(line + "\r\n")
        card3 = TerminalCard(waking)
        waking.worker.state = WorkerState.RUNNING  # the launch autostart...
        card3.terminal.screen.reset()              # ...before any settled size
        card3._rerender_restored()
        check("screens: an autostarted card still re-renders its restored "
              "screen at the settled size",
              line in card3.terminal.screen_text())
        card3.detach(); waking.dispose(); pump(50)

        # ...but once the child has actually drawn, the screen is its own and
        # a stale re-feed would fight it
        live = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Live", cwd=str(tmp), pty=True))
        live.seed_pty_replay(line + "\r\n")
        card4 = TerminalCard(live)
        live._pty_buffer.append("output from the child\r\n")
        card4.terminal.screen.reset()
        card4._rerender_restored()
        check("screens: ...but never over a child that has since written",
              line not in card4.terminal.screen_text())
        card4.detach(); live.dispose(); pump(50)

        # A card whose agent STARTS before it ever got a real size drops the
        # restored screen entirely. Both launch paths that start an agent
        # (autostart_active_workspace, recover_blocked_at_startup) run
        # synchronously right after show(), ahead of TerminalView's 120ms
        # resize debounce — so this is EVERY agent that comes back running,
        # and leaving the seed there parked each of their cards on a mangled
        # narrow fragment until the TUI finished booting. An empty terminal
        # that fills in a few seconds is the pre-snapshot behavior; the whole
        # point of the feature is the card that stays STOPPED.
        from app.terminal_agent import AgentStatus
        launching = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Launching", cwd=str(tmp), pty=True))
        launching.seed_pty_replay("OLD-CONVERSATION\r\n")
        card5 = TerminalCard(launching)
        check("screens: a restored card starts out showing its conversation",
              "OLD-CONVERSATION" in card5.terminal.screen_text())
        card5._on_status(AgentStatus.STARTING)  # the launch autostart
        check("screens: an agent starting before the first settled size gets "
              "a clean terminal, not a mangled fragment",
              "OLD-CONVERSATION" not in card5.terminal.screen_text())
        check("screens: ...and the seed is dropped on the AGENT too, so a "
              "retile cannot replay it under the child",
              launching.pty_replay() == "")
        card5.detach(); launching.dispose(); pump(50)

        # ...but a card WOKEN by a keystroke has long since re-rendered at its
        # real size, and its conversation must still be there to scroll up out
        # of the way, exactly as a real terminal's would
        woken = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Woken", cwd=str(tmp), pty=True))
        woken.seed_pty_replay("KEPT-CONVERSATION\r\n")
        card6 = TerminalCard(woken)
        card6.resize(640, 400); card6.show(); pump(150)  # settles, re-renders
        check("screens: a settled restored card has consumed its replay",
              card6._pending_replay == "")
        card6._on_status(AgentStatus.STARTING)  # the waking keystroke
        check("screens: waking a stopped card KEEPS the conversation on screen",
              "KEPT-CONVERSATION" in card6.terminal.screen_text()
              and "KEPT-CONVERSATION" in woken.pty_replay())
        card6.detach(); woken.dispose(); pump(50)

        # ...unless that wake RESUMES a conversation, which reprints the whole
        # thing itself. Then the snapshot below is a duplicate, hard-wrapped
        # for the width the card had in the PREVIOUS session -- the half-width
        # scrollback bug arriving by the one door the launch autostart (which
        # drops the seed outright) does not cover.
        resumed = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Resumed", cwd=str(tmp), pty=True))
        resumed.seed_pty_replay("LAST-SESSION-WIDTH\r\n")
        card7 = TerminalCard(resumed)
        card7.resize(640, 400); card7.show(); pump(150)
        check("screens: precondition - the resumed card settled on the seed",
              "LAST-SESSION-WIDTH" in card7.terminal.screen_text()
              and card7._pending_replay == "")
        resumed.spec.resume = "sess-abc"        # this launch reopens a chat
        card7._on_status(AgentStatus.STARTING)
        check("screens: a wake that RESUMES drops the stale-width snapshot "
              "instead of stacking it above the reprint",
              "LAST-SESSION-WIDTH" not in card7.terminal.screen_text()
              and resumed.pty_replay() == "")
        card7.detach(); resumed.dispose(); pump(50)

        # an agent with NOTHING to show keeps the original centred banner:
        # that card really is a dead black screen and must say so
        blank = TerminalAgent(build_spec(
            AgentKind.POWERSHELL, "Blank", cwd=str(tmp), pty=True))
        card2 = TerminalCard(blank)
        card2.resize(640, 400); card2.show(); pump(150)
        check("screens: an empty stopped card keeps the centred wake banner",
              card2.overlay.isVisible() and not card2._overlay_compact
              and "press any key to start" in card2.overlay.text())
        card2.detach(); blank.dispose(); pump(150)

    # -- the whole round-trip, through the real window -------------------
    # This is the user-visible regression: close the app with a card left
    # stopped, reopen, and the conversation is on the card. The pieces above
    # all passed while the feature was still broken end to end.
    if HAS_CONPTY:
        from app.session_store import SessionStore
        from main import create_main_window

        home = pathlib.Path(tempfile.mkdtemp(prefix="aihive-screen-e2e-"))
        store = SessionStore(path=home / "session.json")
        term = {"role": "", "cwd": str(home), "user_program": "",
                "user_args": [], "pty": True, "provider": "", "model": "",
                "effort": "", "custom_command": "", "font_px": 0,
                "is_orchestrator": False, "task": "", "assignment": "idle",
                "auto_created": False, "session_id": "screen-e2e-1"}
        store.save({"version": 3, "active": "w1", "workspaces": [
            {"id": "w1", "name": "Solo", "project_path": str(home),
             "layout": "auto", "terminals": [
                 {**term, "kind": "powershell", "name": "Keeper",
                  "running": False}]}]})

        win = create_main_window(store)
        win.show(); pump(200)
        agent = win.manager.workspaces[0].agents[0]
        # stand in for a conversation the agent had before the app closed
        agent.seed_pty_replay("\r\n" * 4 + "MARKER-FROM-LAST-SESSION\r\n")
        win.close(); pump(300)

        snap = screen_snapshot.load(str(home), str(home), "screen-e2e-1")
        check("screens: closing the app writes the card's screen to disk",
              "MARKER-FROM-LAST-SESSION" in snap)
        check("screens: the screen is a file of its own, not session.json",
              "MARKER-FROM-LAST-SESSION" not in
              (home / "session.json").read_text(encoding="utf-8"))

        win2 = create_main_window(store)
        win2.show(); pump(300)
        agent2 = win2.manager.workspaces[0].agents[0]
        check("screens: reopening seeds the restored agent from disk",
              "MARKER-FROM-LAST-SESSION" in agent2.pty_replay())
        check("screens: ...and it is still NOT running (restored as left)",
              not agent2.is_running())
        card3 = win2._pages[win2.manager.workspaces[0].id].cards[0]
        check("screens: ...and the reopened CARD shows the conversation",
              wait_until(lambda: "MARKER-FROM-LAST-SESSION"
                         in card3.terminal.screen_text(), 4000),
              card3.terminal.screen_text()[-200:])
        check("screens: ...under a slim footer, not a wall of dead terminals",
              card3.overlay.isVisible() and card3._overlay_compact)
        win2.close(); pump(300)

        # ...and the OTHER half of the round-trip, which is the launch the
        # user actually looks at: an agent that was RUNNING comes back to a
        # CLEAN terminal that its child fills in a few seconds, never a
        # fragment of last night's screen. autostart_active_workspace() runs
        # synchronously right after show(), ahead of TerminalView's 120ms
        # resize debounce, so the restored screen never gets a settled size
        # and pyte cannot reflow what was drawn at the pre-layout width.
        store.save({"version": 3, "active": "w1", "workspaces": [
            {"id": "w1", "name": "Solo", "project_path": str(home),
             "layout": "auto", "terminals": [
                 {**term, "kind": "powershell", "name": "Runner",
                  "running": True}]}]})
        win3 = create_main_window(store)
        runner = win3.manager.workspaces[0].agents[0]
        check("screens: a restored RUNNING agent is seeded like any other",
              "MARKER-FROM-LAST-SESSION" in runner.pty_replay())
        run_card = win3._pages[win3.manager.workspaces[0].id].cards[0]
        check("screens: ...and its card starts out showing that screen",
              "MARKER-FROM-LAST-SESSION" in run_card.terminal.screen_text())
        # main.py's exact order: show(), then autostart, with no turn of the
        # event loop in between. Pumping here instead would let the resize
        # settle first and test the WAKE path by accident.
        win3.show(); win3.autostart_active_workspace(); pump(600)
        check("screens: ...but the launch autostart hands it a clean terminal",
              "MARKER-FROM-LAST-SESSION"
              not in run_card.terminal.screen_text(),
              run_card.terminal.screen_text()[:160])
        check("screens: ...and drops the stale seed off the agent as well",
              "MARKER-FROM-LAST-SESSION" not in runner.pty_replay())
        win3.close(); pump(300)
        shutil.rmtree(home, ignore_errors=True)

    # -- a corrupted restored screen degrades, it never crashes the app --
    # The regression: a wide CJK character's leading cell later overwritten
    # (e.g. by an absolute-column cursor jump, common in TUI mockups) orphans
    # pyte's own zero-width stub cell (data=""). pyte's `display` property
    # then does wcwidth(char[0]) on that empty string and raises IndexError.
    # screen_text() is called from TerminalCard.__init__ -> _on_status ->
    # _refresh_overlay for EVERY restored card, so one corrupted .vt snapshot
    # (app/screen_snapshot.py) crashed the whole app on startup, before the
    # window was ever shown -- and silently, since the launch shortcut runs
    # pythonw.exe with no console to print the traceback.
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView
    QApplication.instance() or QApplication([])
    corrupt = TerminalView(rows=3, cols=20)
    corrupt.feed("\x1b[1;1HあX\x1b[1;1HY")
    check("screens: a corrupted pyte buffer degrades screen_text() to "
          "empty instead of crashing the app",
          corrupt.screen_text() == "")

    shutil.rmtree(tmp, ignore_errors=True)


def test_boot_veil():
    """A launching terminal shows a loader, not the child's half-drawn frame.

    The regression: every reopen parked each restored card on a mangled narrow
    fragment in its top-left corner (the child's first frames, drawn at the
    pre-layout width, which pyte cannot reflow) until the conversation finished
    replaying. The veil covers exactly the launch-to-prompt window, and must
    ALWAYS lift again: on readiness, on a keystroke, on the agent stopping, or
    on its own backstop timer.

    It covers a SECOND window for the same reason: a card built over a restored
    .vt snapshot projects an 8 KiB cut of it at the pre-layout width, and
    main.py paints that frame (show()) before autostart_active_workspace raises
    any veil -- reported as "gibberish in the top left corner, then the loader,
    then it looks normal". A REBUILD of a live agent must still paint at once,
    so the gate is TerminalAgent.has_pristine_seed(), not merely "has a
    replay"."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    # -- the agent-side signal --------------------------------------------
    spec = build_spec(AgentKind.CLAUDE, "Booting", cwd=SCRATCH_CWD, pty=True)
    agent = TerminalAgent(spec)
    seen = []
    agent.prompt_ready_changed.connect(seen.append)
    agent._on_pty_output("stdout", "Claude Code is booting")
    check("boot-veil: a booting child is not reported ready",
          seen == [] and not agent.prompt_ready())
    agent._on_pty_output("stdout", "? for shortcuts")
    check("boot-veil: the ready footer announces an interactive prompt",
          seen == [True] and agent.prompt_ready())
    agent._on_pty_output("stdout", "? for shortcuts")
    check("boot-veil: readiness is edge-only, never once per output burst",
          seen == [True])

    class _StubWorker:
        state = None
        def start(self): pass
        def restart(self): pass
        def is_running(self): return False
        def dispose(self): pass
    agent.worker = _StubWorker()
    agent.start()
    check("boot-veil: a (re)start re-arms readiness and says so",
          seen == [True, False] and not agent.prompt_ready())
    agent.dispose()

    # -- the widget --------------------------------------------------------
    from PySide6.QtCore import QAbstractAnimation, QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from app.widgets.ornaments import BootVeil
    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    from PySide6.QtWidgets import QWidget
    host = QWidget()          # a real parent, so raise_() is a stacking op
    host.resize(400, 240)     # rather than an offscreen window request
    veil = BootVeil(host)
    veil.resize(400, 240)
    check("boot-veil: a fresh veil is down and idle", not veil.is_active())
    veil.begin("restoring conversation…")
    check("boot-veil: begin() covers the terminal and starts the sweep",
          veil.is_active() and veil._spin.state() ==
          QAbstractAnimation.State.Running)
    veil.finish()
    pump(500)  # the fade is 260ms
    check("boot-veil: finish() dissolves it and stops animating",
          not veil.is_active()
          and veil._spin.state() != QAbstractAnimation.State.Running)
    veil.begin("starting…")
    veil.dismiss()
    check("boot-veil: dismiss() drops it at once",
          not veil.is_active()
          and veil._spin.state() != QAbstractAnimation.State.Running)
    veil.deleteLater(); host.deleteLater()

    # -- the card ----------------------------------------------------------
    from app.pty_worker import HAS_CONPTY
    if not HAS_CONPTY:
        return
    from app.widgets.terminal_card import BOOT_VEIL_MAX_MS, TerminalCard

    booting = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Boot", cwd=SCRATCH_CWD, pty=True))
    card = TerminalCard(booting)
    card.resize(640, 400); card.show(); pump(150)
    check("boot-veil: a stopped card shows the wake banner, not the loader",
          not card.boot.is_active())
    card._on_status(AgentStatus.STARTING)   # the launch autostart
    check("boot-veil: a launching card is covered while its child boots",
          card.boot.is_active())
    check("boot-veil: ...over the whole terminal, so no fragment shows through",
          card.boot.geometry() == card.terminal.rect())
    check("boot-veil: ...and it never takes the keyboard from the child",
          card.boot.focusPolicy() == Qt.FocusPolicy.NoFocus)
    # the point of the feature, in PIXELS: whatever the booting child paints
    # underneath must not reach the user. Checking a flag would have passed
    # just as happily with the veil sitting at the wrong geometry or behind
    # the terminal.
    from PySide6.QtGui import QColor

    from app.ui_theme import Palette
    card.terminal.feed("BOOT-FRAGMENT-" + "#" * 40 + "\r\n")
    pump(60)
    img = card.terminal.grab().toImage()
    ground = QColor(Palette.BG_CONSOLE).rgb()
    top_rows = [img.pixel(x, y) for y in (3, 6, 9)
                for x in range(0, min(240, img.width()), 3)]
    check("boot-veil: the child's first frames are covered in pixels, not "
          "merely hidden behind a flag",
          top_rows and all(p == ground for p in top_rows))
    booting._set_prompt_ready(True)
    pump(500)
    check("boot-veil: the prompt going live lifts it",
          not card.boot.is_active())

    # typing means the user wants the terminal, whatever the child has said
    from app.process_worker import WorkerState
    booting._set_prompt_ready(False)
    card._on_status(AgentStatus.STARTING)
    booting.worker.state = WorkerState.RUNNING   # the child is live, if quiet
    card._on_key_input("x")
    check("boot-veil: typing drops it immediately",
          not card.boot.is_active() and not card._boot_timer.isActive())

    # a stopped agent hands the screen back to the wake banner
    card._on_status(AgentStatus.STARTING)
    card._on_status(AgentStatus.EXITED_OK)
    check("boot-veil: a stopped agent drops it (the wake banner owns that)",
          not card.boot.is_active() and card.overlay.isVisible())

    # ...and a child that never reports readiness at all still gets its
    # terminal back: the veil is bounded, never a permanent cover
    card._on_status(AgentStatus.STARTING)
    check("boot-veil: the backstop timer is armed while covered",
          card._boot_timer.isActive() and 0 < card._boot_timer.interval()
          <= BOOT_VEIL_MAX_MS)
    card._dismiss_boot_veil()
    check("boot-veil: ...and firing it uncovers the terminal",
          not card.boot.is_active())
    card.detach(); booting.dispose(); pump(100)

    # -- the restored snapshot, which is painted BEFORE any child exists ---
    # main.py seeds every pty agent from screen_snapshot and only then shows
    # the window, so this frame reaches the user unless the constructor itself
    # covers it.
    from app.ui_theme import Palette as _Pal

    restored = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Restored", cwd=SCRATCH_CWD, pty=True))
    snapshot = "RESTORED-SNAPSHOT-" + "=" * 60 + "\r\n"
    check("boot-veil: a restored snapshot seeds the agent's buffer",
          restored.seed_pty_replay(snapshot) and restored.has_pristine_seed())
    rcard = TerminalCard(restored)
    check("boot-veil: a card built over a restored snapshot is covered from "
          "its constructor, before the window ever paints",
          rcard.boot.is_active() and rcard._boot_seed)
    # before the view's resize debounce settles, on purpose: this models the
    # window's FIRST paint, which is the frame main.py used to leak. The
    # debounce is held open here rather than raced (a pump(30) under a 120ms
    # timer failed whenever the event loop stalled), and released below.
    rcard.resize(640, 400); rcard.show()
    rcard.terminal._resize_timer.setInterval(60000)   # restarts it, held
    pump(30)
    # the constructor's own _on_status(IDLE) runs the not-running branch, which
    # used to dismiss the veil unconditionally -- the wake banner may only own
    # the screen once there is something readable under it
    check("boot-veil: ...and the wake banner does not take it away",
          rcard.boot.is_active())
    rimg = rcard.terminal.grab().toImage()
    rground = QColor(_Pal.BG_CONSOLE).rgb()
    rrows = [rimg.pixel(x, y) for y in (3, 6, 9)
             for x in range(0, min(240, rimg.width()), 3)]
    check("boot-veil: ...in pixels: the mangled seed never reaches the user",
          rrows and all(p == rground for p in rrows))
    # release the debounce: the resize lands NOW, as a settled layout's does
    rcard.terminal._resize_timer.setInterval(120)
    rcard.terminal.flush_resize()
    # the settled-width projection IS the stopped card's final picture
    pump(500)  # past the 260ms fade
    check("boot-veil: the settled-width projection dissolves it",
          not rcard.boot.is_active() and not rcard._boot_seed)
    check("boot-veil: ...revealing the conversation it was holding back",
          "RESTORED-SNAPSHOT" in rcard.terminal.screen_text())
    rcard.detach(); restored.dispose(); pump(50)

    # an agent that AUTOSTARTS hands the veil straight to the booting child,
    # with no gap: drop_restored_screen clears the seed, _on_status re-raises
    auto = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Auto", cwd=SCRATCH_CWD, pty=True))
    auto.seed_pty_replay(snapshot)
    acard = TerminalCard(auto)
    acard.resize(640, 400); acard.show(); pump(50)
    acard.drop_restored_screen()
    check("boot-veil: dropping the seed hands ownership to the child branch",
          not acard._boot_seed)
    acard._on_status(AgentStatus.STARTING)
    check("boot-veil: ...and the launching child keeps the terminal covered",
          acard.boot.is_active())
    auto._set_prompt_ready(True)
    pump(500)
    check("boot-veil: ...until its prompt goes live",
          not acard.boot.is_active())
    acard.detach(); auto.dispose(); pump(50)

    # the regression guard for the OTHER half: a retile rebuilds cards over a
    # LIVE agent's buffer, which must paint instantly and never flash a loader
    live = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Live", cwd=SCRATCH_CWD, pty=True))
    live._on_pty_output("pty", "LIVE-CHILD-OUTPUT\r\n")   # a live conversation
    check("boot-veil: a buffer a child wrote is not a restored snapshot",
          live.pty_replay() and not live.has_pristine_seed())
    lcard = TerminalCard(live)
    lcard.resize(640, 400); lcard.show(); pump(150)
    check("boot-veil: a card rebuilt over a live buffer raises no loader",
          not lcard.boot.is_active() and not lcard._boot_seed)
    check("boot-veil: ...and paints its conversation immediately",
          "LIVE-CHILD-OUTPUT" in lcard.terminal.screen_text())
    lcard.detach(); live.dispose(); pump(50)


def test_resume_fallback():
    """A resume (--continue) launch that dies before the interactive prompt ever
    comes up (Claude prints 'No conversation found to continue' and exits) must
    relaunch ONCE, fresh — so the terminal is never left black. The regression
    behind a resumed Claude card that opened all-black and non-interactive."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    spec = build_spec(AgentKind.CLAUDE, "Resumed", cwd=SCRATCH_CWD,
                      pty=True)
    spec.resume = True
    agent = TerminalAgent(spec)

    calls = {"start": 0}

    class _StubWorker:
        def start(self): calls["start"] += 1
        def is_running(self): return False
        def dispose(self): pass
    agent.worker = _StubWorker()

    agent.start()  # models the restore launch (--continue)
    check("resume-fallback: initial launch invoked", calls["start"] == 1)
    check("resume-fallback: attempt flagged", agent._resume_attempt is True)
    check("resume-fallback: one-shot resume flag cleared", spec.resume is False)

    # the process exits fast, before the TUI prompt (no ESC[?2004h seen)
    agent._prompt_ready = False
    agent.status = AgentStatus.RUNNING     # a natural exit, not a user Stop
    agent._on_finished(1, False)
    check("resume-fallback: relaunched fresh after fast fail", calls["start"] == 2)
    check("resume-fallback: fallback marked done", agent._resume_fallback_done is True)

    # a second death must NOT loop forever
    agent._on_finished(1, False)
    check("resume-fallback: no infinite relaunch loop", calls["start"] == 2)

    # a resume that DID reach the prompt, then exits, must NOT relaunch
    spec2 = build_spec(AgentKind.CLAUDE, "Coder", cwd=SCRATCH_CWD, pty=True)
    spec2.resume = True
    a2 = TerminalAgent(spec2)
    c2 = {"start": 0}
    a2.worker = type("W", (), {"start": lambda s: c2.__setitem__("start", c2["start"] + 1),
                               "is_running": lambda s: False, "dispose": lambda s: None})()
    a2.start()
    a2._prompt_ready = True               # the interactive UI came up fine
    a2.status = AgentStatus.RUNNING
    a2._on_finished(0, False)
    check("resume-fallback: healthy session not relaunched on exit", c2["start"] == 1)

    # a user Stop (STOPPING) of a not-yet-ready resume must NOT relaunch either
    spec3 = build_spec(AgentKind.CLAUDE, "Stopped", cwd=SCRATCH_CWD, pty=True)
    spec3.resume = True
    a3 = TerminalAgent(spec3)
    c3 = {"start": 0}
    a3.worker = type("W", (), {"start": lambda s: c3.__setitem__("start", c3["start"] + 1),
                               "is_running": lambda s: False, "dispose": lambda s: None})()
    a3.start()
    a3._prompt_ready = False
    a3.status = AgentStatus.STOPPING      # user asked it to stop
    a3._on_finished(0, False)
    check("resume-fallback: user-stopped resume not relaunched", c3["start"] == 1)


def test_agent_kind_is_always_an_enum():
    """`build_spec` coerces `kind`, so an agent can always be serialized.

    QComboBox.currentData() round-trips a value through QVariant and hands a
    str-mixin enum back as a PLAIN STR, so every agent built from the New Agent
    dialog carried kind="claude". Nothing notices until `AgentSpec.to_dict`
    reaches `self.kind.value` — on every save, for the life of the process —
    and the agent degrades to a minimal record that loses its model, effort,
    permission mode, role and task on the next restore. Observed live: 832
    SAVE-DEGRADE lines in one session."""
    from PySide6.QtWidgets import QApplication, QComboBox
    from app.process_worker import AgentKind, build_spec

    app = QApplication.instance() or QApplication([])

    combo = QComboBox()
    combo.addItem("Claude Code", AgentKind.CLAUDE)
    from_qt = combo.currentData()
    check("agent-kind: Qt really does hand back a plain str (the trap)",
          type(from_qt) is str and not isinstance(from_qt, AgentKind))

    spec = build_spec(from_qt, "Agent 1", cwd=SCRATCH_CWD)
    check("agent-kind: build_spec coerces it back to the enum",
          spec.kind is AgentKind.CLAUDE)
    check("agent-kind: so the agent serializes instead of degrading",
          spec.to_dict()["kind"] == "claude")
    check("agent-kind: an enum in still comes out unchanged",
          build_spec(AgentKind.CMD, "S", cwd=SCRATCH_CWD).kind is AgentKind.CMD)


def test_gemini_session_pinning():
    """Gemini (agy) agents pin their conversation like Claude ones: a pinned
    id resumes with --conversation <id>, only an unpinned one falls back to
    --continue. Plus the model/effort, permission-mode and title parsers the
    card header reads. Folded in from the old tests/test_gemini_session_
    pinning.py, which nothing ran, minus its checks that needed a live Gemini
    session in the repo folder."""
    import json as _json
    from app import providers, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.workspace_manager import WorkspaceManager

    sid = "da3a8077-8c47-4e46-92a4-f40637e999ad"
    spec = build_spec(AgentKind.GEMINI, "G", cwd=SCRATCH_CWD,
                      model="Gemini 3.6 Flash (High)")
    spec.session_id, spec.resume = sid, True
    args = spec.effective_args()
    check("gemini-pin: a pinned agent resumes with --conversation <id>",
          "--conversation" in args
          and args[args.index("--conversation") + 1] == sid
          and "--continue" not in args, args)
    spec.session_id = ""
    args = spec.effective_args()
    check("gemini-pin: an unpinned agent falls back to --continue",
          "--continue" in args and "--conversation" not in args, args)

    cases = [("Gemini 3.8 Flash (High)", ("Gemini 3.8 Flash", "high")),
             ("gemini-3.8-flash-medium", ("Gemini 3.8 Flash", "medium")),
             ("Claude Sonnet 4.6 (Thinking)", ("Claude Sonnet 4.6", "thinking"))]
    for raw, want in cases:
        got = transcripts.parse_gemini_model_effort(raw)
        check(f"gemini-pin: {raw} parses to {want}", tuple(got) == want, got)
    check("gemini-pin: a control variant keeps its family",
          transcripts.parse_gemini_model_effort(
              "gemini-3.7-flash-control")[0] == "Gemini 3.7 Flash")

    low = build_spec(AgentKind.GEMINI, "L", cwd=SCRATCH_CWD,
                     model="Gemini 3.8 Flash (Low)")
    check("gemini-pin: build_spec reads the effort out of the model label "
          "and passes the label as ONE argv entry",
          low.effort == "low" and low.args == ["--model",
                                               "Gemini 3.8 Flash (Low)"],
          (low.effort, low.args))

    check("gemini-pin: permission modes display as the card's words",
          [providers.gemini_permission_mode_display(m)
           for m in ("accept-edits", "always-proceed", "plan", "")]
          == ["auto", "bypass", "plan", "manual"])

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-gemtitle-"))
    db = tmp / "conversations.json"
    db.write_text(_json.dumps({"conversations": {
        "s1": {"summary": {"ID": "s1", "Title": "Custom Title",
                           "Preview": "Preview Text"}},
        "s2": {"summary": {"ID": "s2", "Title": "",
                           "Preview": "Preview Fallback"}}}}),
        encoding="utf-8")
    check("gemini-pin: the title is the conversation's Title",
          transcripts._read_gemini_ai_title(str(db), "s1") == "Custom Title")
    check("gemini-pin: ...falling back to its Preview when untitled",
          transcripts._read_gemini_ai_title(str(db), "s2")
          == "Preview Fallback")

    wm = WorkspaceManager()
    ws = wm.create_workspace("G", SCRATCH_CWD)
    agent = wm.add_terminal(ws.id, build_spec(
        AgentKind.GEMINI, "Badge", cwd=SCRATCH_CWD,
        model="Gemini 3.7 Flash (High)"), autostart=False)
    check("gemini-pin: the header badge is seeded from the chosen model",
          "Gemini 3.7 Flash" in agent.model_badge()
          and "high" in agent.model_badge(), agent.model_badge())
    shutil.rmtree(tmp, ignore_errors=True)


def test_multi_agent_session_isolation():
    """Ensure two agents in the same workspace never share a session ID, and
    Gemini session sync isolates multi-agent folders properly."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-iso-test-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Test", str(tmp))

    spec1 = build_spec(AgentKind.GEMINI, "Gemini 1", cwd=str(tmp))
    spec1.session_id = "11111111-1111-1111-1111-111111111111"
    agent1 = mgr.add_terminal(ws.id, spec1, autostart=False)

    check("iso: agent1 has its pinned session_id",
          agent1.spec.session_id == "11111111-1111-1111-1111-111111111111")
    check("iso: sibling_session_ids of agent1 is empty",
          mgr.sibling_session_ids(agent1) == set())

    # Add a second Gemini agent with a DUPLICATE session_id
    spec2 = build_spec(AgentKind.GEMINI, "Gemini 2", cwd=str(tmp))
    spec2.session_id = "11111111-1111-1111-1111-111111111111"  # duplicate!
    agent2 = mgr.add_terminal(ws.id, spec2, autostart=False)

    check("iso: sibling_session_ids of agent2 sees agent1 pin",
          mgr.sibling_session_ids(agent2) == {"11111111-1111-1111-1111-111111111111"})
    check("iso: agent2 duplicate pin was disallocated on wire",
          agent2.spec.session_id != "11111111-1111-1111-1111-111111111111")

    # Verify sync_live_sessions does NOT cross-pin multi-agent Gemini folder
    mgr.sync_live_sessions()
    check("iso: sync_live_sessions keeps agent1 and agent2 distinct",
          agent1.spec.session_id != agent2.spec.session_id)
