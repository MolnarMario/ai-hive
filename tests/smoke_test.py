"""AI Hive end-to-end smoke test.

Run with the project venv, no display needed:
    .venv\\Scripts\\python.exe tests\\smoke_test.py

Drives the REAL application (create_main_window) on the offscreen Qt
platform with real child processes; prints [PASS]/[FAIL] per check,
saves screenshots to tests/screenshots/, exits nonzero on any failure.
Every wait has a timeout, so a hang reads as a deterministic FAIL.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
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

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
SHOTS = ROOT / "tests" / "screenshots"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    cond = bool(cond)
    line = ("[PASS] " if cond else "[FAIL] ") + name
    if detail and not cond:
        line += " :: " + str(detail)
    print(line, flush=True)
    PASS += cond
    FAIL += not cond


# ---------------------------------------------------------------- tiling ----

def test_tiling():
    from app.tiling import Cell, compute_grid

    expected = {
        1: [(0, 0, 1, 1)],
        2: [(0, 0, 1, 1), (0, 1, 1, 1)],
        3: [(0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1)],
        4: [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 1), (1, 1, 1, 1)],
        5: [(0, 0, 1, 2), (0, 2, 1, 2), (0, 4, 1, 2), (1, 0, 1, 3), (1, 3, 1, 3)],
        6: [(0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1),
            (1, 0, 1, 1), (1, 1, 1, 1), (1, 2, 1, 1)],
        7: [(0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1),
            (1, 0, 1, 1), (1, 1, 1, 1), (1, 2, 1, 1), (2, 0, 1, 3)],
        8: [(0, 0, 1, 2), (0, 2, 1, 2), (0, 4, 1, 2),
            (1, 0, 1, 2), (1, 2, 1, 2), (1, 4, 1, 2), (2, 0, 1, 3), (2, 3, 1, 3)],
        9: [(0, 0, 1, 1), (0, 1, 1, 1), (0, 2, 1, 1),
            (1, 0, 1, 1), (1, 1, 1, 1), (1, 2, 1, 1),
            (2, 0, 1, 1), (2, 1, 1, 1), (2, 2, 1, 1)],
    }
    expected_dims = {  # n: (rows, vcols)
        1: (1, 1), 2: (1, 2), 3: (1, 3), 4: (2, 2), 5: (2, 6),
        6: (2, 3), 7: (3, 3), 8: (3, 6), 9: (3, 3),
    }

    check("tiling: n=0 is empty", compute_grid(0) == ([], 0, 0))
    for n, cells in expected.items():
        plan = compute_grid(n)
        want = [Cell(*c) for c in cells]
        check(f"tiling: n={n} cells", plan.cells == want, f"got {plan.cells}")
        check(f"tiling: n={n} dims", (plan.rows, plan.vcols) == expected_dims[n],
              f"got rows={plan.rows} vcols={plan.vcols}")
    for n in range(1, 13):
        plan = compute_grid(n)
        by_row = {}
        for c in plan.cells:
            by_row[c.row] = by_row.get(c.row, 0) + c.col_span
        check(f"tiling: n={n} rows fully covered",
              all(v == plan.vcols for v in by_row.values()),
              f"row spans {by_row}, vcols={plan.vcols}")

    # Regression: a third terminal in an AUTO workspace must land beside the
    # other two (3x1), not as a full-width straggler under a 2-card row. The
    # 2026-07-31 rebalance only covered FIXED grids, so an auto workspace kept
    # producing the lopsided "2 over 1" (reported live on a real workspace).
    p3 = compute_grid(3)
    check("tiling: auto n=3 is one row of three",
          p3.rows == 1 and p3.vcols == 3
          and [c.row for c in p3.cells] == [0, 0, 0]
          and [c.col_span for c in p3.cells] == [1, 1, 1],
          f"got {p3}")
    from app.tiling import explicit_grid as _eg
    check("tiling: auto n=3 matches the fixed-grid overflow shape",
          (p3.rows, p3.vcols) == (_eg(1, 2, 3).rows, _eg(1, 2, 3).cols),
          f"auto {(p3.rows, p3.vcols)} vs fixed {_eg(1, 2, 3)[2:]}")


def test_layout_popup_placement():
    """The Layout button sits at the right end of the header, so left-anchoring
    its wide palette spills past the app window's right edge (the reported bug:
    the popup overflows the window even when there's screen to the right). The
    popup must stay inside the window AND on-screen — right-aligning under the
    button so it grows leftward into the window."""
    from PySide6.QtCore import QSize
    from PySide6.QtWidgets import QApplication, QWidget
    from app.widgets.grid_selector import GridButton

    app = QApplication.instance() or QApplication([])
    avail = app.primaryScreen().availableGeometry()

    # a normal window well away from the screen edges, Layout button at its
    # right end — the case my first (screen-only) clamp missed
    win = QWidget()
    win.setGeometry(avail.x() + 40, avail.y() + 40, 700, 500)
    btn = GridButton(win)
    btn.resize(96, 28)
    btn.move(700 - btn.width() - 8, 8)
    win.show(); app.processEvents()

    size = QSize(360, 260)  # wider than the gap from the button to window-right
    p = btn._popup_position(size)
    wg = win.geometry()
    win_right = wg.x() + wg.width()
    check("layout popup: stays within the app window's right edge",
          wg.x() <= p.x() and p.x() + size.width() <= win_right,
          (p.x(), size.width(), win_right))
    check("layout popup: also stays on-screen",
          avail.y() <= p.y() and p.y() + size.height() <= avail.y() + avail.height(),
          (p.y(), size.height()))
    win.deleteLater()


def test_sidebar_count_badge():
    """The workspace row leads with an agent-count badge that also signals
    status by colour: green when agents are alive but idle, pulsing amber when
    any agent works (amber wins if something also errors), red on error, dim
    when empty. A right-edge spinner mirrors the working count (hidden at 0)."""
    from PySide6.QtCore import QAbstractAnimation
    from PySide6.QtWidgets import QApplication
    from app.widgets.ornaments import AgentCountBadge
    from app.ui_theme import Palette
    from app.widgets.sidebar import WorkspaceRow, SIDEBAR_WIDTH, ROW_HEIGHT

    QApplication.instance() or QApplication([])
    row = WorkspaceRow("w1", "Alpha", "C:/proj")

    def badge(total, running, busy, error):
        row.set_stats({"total": total, "active": running, "busy": busy,
                       "error": error, "idle": total - running})
        return row.count_badge

    b = badge(0, 0, 0, 0)
    check("badge: empty workspace -> empty state, count 0",
          b._state == "empty" and b._count == 0, (b._state, b._count))
    # 3 agents running but none producing output => idle/standby (now GREEN)
    b = badge(3, 3, 0, 0)
    check("badge: running but not busy -> idle (green), count 3",
          b._state == "idle" and b._count == 3, (b._state, b._count))
    b = badge(3, 3, 1, 0)
    check("badge: one busy -> working (amber), pulsing",
          b._state == "working"
          and b._anim.state() == QAbstractAnimation.State.Running,
          (b._state, b._anim.state()))
    b = badge(1, 0, 0, 1)
    check("badge: idle with an error -> error (red), not pulsing",
          b._state == "error"
          and b._anim.state() != QAbstractAnimation.State.Running,
          (b._state, b._anim.state()))
    b = badge(2, 2, 1, 1)
    check("badge: busy + error -> working wins (amber)",
          b._state == "working", b._state)

    # lock the flipped colour mapping: idle=green, working=amber/yellow
    check("badge: idle maps to GREEN, working maps to YELLOW (amber)",
          AgentCountBadge._STATE_COLOR["idle"]() == Palette.GREEN
          and AgentCountBadge._STATE_COLOR["working"]() == Palette.YELLOW,
          (AgentCountBadge._STATE_COLOR["idle"](),
           AgentCountBadge._STATE_COLOR["working"]()))

    # right-edge spinner: hidden + stopped at 0 working, shown + spinning + count
    # when working (isHidden() reflects explicit show/hide intent, ancestor-free)
    badge(3, 3, 0, 0)
    check("spinner: no agents working -> hidden, animation stopped",
          row.work_spinner.isHidden() and row.work_spinner._count == 0
          and row.work_spinner._anim.state()
          != QAbstractAnimation.State.Running,
          (row.work_spinner.isHidden(), row.work_spinner._count))
    badge(3, 3, 2, 0)
    check("spinner: 2 agents working -> shown, count 2, spinning",
          not row.work_spinner.isHidden() and row.work_spinner._count == 2
          and row.work_spinner._anim.state()
          == QAbstractAnimation.State.Running,
          (row.work_spinner.isHidden(), row.work_spinner._count))

    # even with every status badge lit at once, none of them may be hidden to
    # make room — only the name label may shrink (down to 0 width; it has no
    # minimum), so icons are never squeezed out or collapsed behind a "..."
    # overflow.
    row.set_active(True)
    row.set_stats({"total": 3, "active": 3, "busy": 2, "error": 0,
                   "waiting": 1, "idle": 0, "limit_blocked": 1,
                   "scheduled": 1, "bg_shell": 1})
    check("row: name label has no minimum width (can shrink to 0 for icons)",
          row.name_label.minimumWidth() == 0, row.name_label.minimumWidth())
    check("row: every badge stays visible",
          not row.q_badge.isHidden() and not row.limit_badge.isHidden()
          and not row.sched_badge.isHidden() and not row.bg_badge.isHidden()
          and not row.work_spinner.isHidden(),
          (row.q_badge.isHidden(), row.limit_badge.isHidden(),
           row.sched_badge.isHidden(), row.bg_badge.isHidden(),
           row.work_spinner.isHidden()))

    # the actual regression: forced into the real (narrow) sidebar column via
    # setGeometry -- exactly what QTreeWidget.setItemWidget does -- a shared
    # QHBoxLayout crushes every fixed-size icon down toward its floor, and a
    # QToolButton whose ALLOCATED width lands below its text's natural width
    # gets its own label auto-elided by Qt's style into a bare "...". Living
    # in their own untouched layout (_icon_stack), each badge's width must be
    # INDEPENDENT of the row's width -- squeezing the row into the real
    # sidebar column must not change it at all. Compare against the same
    # badges laid out with the row given plenty of room, rather than against
    # sizeHint() directly, since sizeHint() and the post-layout width are not
    # bit-identical on every platform/DPI -- what must hold is that the row
    # being narrow changes nothing.
    badges = (row.sched_badge, row.limit_badge, row.bg_badge)
    row.setGeometry(0, 0, 2000, ROW_HEIGHT)
    row.layout().activate()
    row._position_icon_stack()
    row._icon_stack.layout().activate()
    roomy_widths = {b.objectName(): b.width() for b in badges}

    row.setGeometry(0, 0, SIDEBAR_WIDTH, ROW_HEIGHT)
    row.layout().activate()
    row._position_icon_stack()
    row._icon_stack.layout().activate()
    for btn in badges:
        roomy_w = roomy_widths[btn.objectName()]
        check(f"row: {btn.objectName()} keeps its full width when the row "
              "is squeezed to the real sidebar width (never elided to '...')",
              btn.width() == roomy_w, (btn.objectName(), btn.width(), roomy_w))

    # "?" waiting indicator: hidden when nobody waits, shown otherwise; clicking
    # it opens the agent dropdown (agentsRequested)
    row.set_stats({"total": 3, "active": 3, "busy": 1, "error": 0,
                   "waiting": 0, "idle": 2})
    check("q-badge: nobody waiting -> hidden", row.q_badge.isHidden())
    row.set_stats({"total": 3, "active": 3, "busy": 1, "error": 0,
                   "waiting": 2, "idle": 2})
    check("q-badge: an agent waiting -> shown", not row.q_badge.isHidden())
    asked = []
    row.agentsRequested.connect(asked.append)
    row.q_badge.click()
    check("q-badge: click asks for the agent list (open dropdown)",
          asked == ["w1"], asked)

    # hourglass + count: agent(s) stopped by the plan usage limit, not yet
    # resumed. Hidden at 0 (mirrors "?"); shown with the count once positive,
    # and must disappear again the instant the count drops back to 0 (a
    # resume, manual or auto-continue) -- this is signal-driven, not polled.
    row.set_stats({"total": 3, "active": 3, "busy": 1, "error": 0,
                   "waiting": 0, "limit_blocked": 0, "idle": 2})
    check("limit-badge: nobody blocked -> hidden", row.limit_badge.isHidden())
    row.set_stats({"total": 3, "active": 3, "busy": 1, "error": 0,
                   "waiting": 0, "limit_blocked": 2, "idle": 2})
    check("limit-badge: 2 agents blocked -> shown with the count",
          not row.limit_badge.isHidden()
          and row.limit_badge.text() == "⏳2", row.limit_badge.text())
    row.set_stats({"total": 3, "active": 3, "busy": 1, "error": 0,
                   "waiting": 0, "limit_blocked": 0, "idle": 2})
    check("limit-badge: back to 0 -> hidden again (live, not stuck on)",
          row.limit_badge.isHidden())
    asked2 = []
    row.agentsRequested.connect(asked2.append)
    row.set_stats({"total": 1, "active": 1, "busy": 0, "error": 0,
                   "waiting": 0, "limit_blocked": 1, "idle": 1})
    row.limit_badge.click()
    check("limit-badge: click asks for the agent list (open dropdown)",
          asked2 == ["w1"], asked2)
    row.deleteLater()


def test_agent_waiting():
    """A settled Claude prompt/question flags the agent as waiting-for-input;
    fresh output clears it, idle-at-prompt does not flag, exit clears it, and
    bypassPermissions suppresses it."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Ask", cwd="."))
    a.status = AgentStatus.RUNNING
    events = []
    a.waiting_changed.connect(events.append)

    prompt = ("\x1b[1mDo you want to make this edit to app.py?\x1b[0m\r\n"
              " \x1b[2m1.\x1b[0m Yes\r\n"
              " 2. Yes, and don't ask again this session\r\n"
              " 3. No, and tell Claude what to do differently\r\n"
              "\x1b[7m> 1. Yes\x1b[0m\r\n? for shortcuts")
    a._on_pty_output("pty", prompt)
    check("waiting: not flagged while output is still fresh (busy)",
          not a.is_waiting())
    a._on_idle_timeout()                 # simulate the 2 s settle
    check("waiting: settled permission prompt -> waiting + signal",
          a.is_waiting() and events and events[-1] is True,
          (a.is_waiting(), events))

    a._on_pty_output("pty", "Bash(ls) running...\r\n")   # movement resumes
    check("waiting: fresh output clears the waiting state", not a.is_waiting())

    a._screen_tail = "esc to interrupt\n? for shortcuts"  # idle at prompt
    a._on_idle_timeout()
    check("waiting: idle-at-prompt (no question) is NOT waiting",
          not a.is_waiting())

    a._screen_tail = ("which approach?\r\n 1. rewrite it\r\n 2. patch it\r\n"
                      " 3. leave as-is\r\n> 1. rewrite it")
    a._on_idle_timeout()
    check("waiting: an option menu (2+ numbered options) flags waiting",
          a.is_waiting())

    # regression: an agent's OWN prose with a numbered list (and even a
    # question / "proceed?"-style phrasing) but NO selection caret must NOT
    # flag waiting — this false-fired the "?" + chime on ordinary output
    a._on_pty_output("pty", "working...\r\n")   # clear, then settle on prose
    a._screen_tail = ("Do you want the summary? here's what I did:\r\n"
                      " 1. committed the change\r\n 2. pushed to main\r\n"
                      " 3. merged and closed the PRs\r\nall done.")
    a._on_idle_timeout()
    check("waiting: a plain numbered list (no selection caret) is NOT waiting",
          not a.is_waiting())

    a._set_status(AgentStatus.EXITED_OK)
    check("waiting: exit clears the waiting state", not a.is_waiting())

    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bypass", cwd="."))
    b.spec.permission_mode = "bypassPermissions"
    b.status = AgentStatus.RUNNING
    b._screen_tail = "Do you want to proceed?\r\n 1. Yes\r\n 2. No"
    b._on_idle_timeout()
    check("waiting: bypassPermissions suppresses the prompt '?'",
          not b.is_waiting())


def test_notification_chime():
    """The notification chime: the synthesiser writes a valid WAV, and the
    manager announces the RISING edge of an agent's waiting state via
    agentWaiting (so the UI can ring) without ever marking the session dirty.
    Playback itself is a non-blocking, degrade-to-silent no-op — not exercised
    here so the headless suite stays quiet."""
    import wave as _wave
    from PySide6.QtWidgets import QApplication
    from app import chime
    from app.terminal_agent import AgentStatus
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- synthesiser: a real, playable 16-bit mono WAV lands in temp ---
    path = chime._ensure_chime()
    check("chime: WAV synthesised to a temp file", path and os.path.exists(path))
    with _wave.open(path, "rb") as w:
        params_ok = (w.getnchannels() == 1 and w.getsampwidth() == 2
                     and w.getframerate() == chime._SAMPLE_RATE
                     and w.getnframes() > 0)
    check("chime: WAV is 16-bit mono at the expected rate with frames",
          params_ok)
    check("chime: available() reflects winsound presence (True on Windows)",
          chime.available() == (chime.winsound is not None))

    # --- manager announces the waiting rising edge, transient (no dirty) ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-chime-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Chime", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Ask",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    rings = []
    dirtied = []
    mgr.agentWaiting.connect(lambda wid, aid: rings.append((wid, aid)))
    mgr.dirty.connect(lambda: dirtied.append(True))

    agent._screen_tail = ("which approach?\r\n 1. rewrite it\r\n 2. patch it\r\n"
                          " 3. leave as-is\r\n> 1. rewrite it")
    agent._on_idle_timeout()   # settle -> waiting rising edge
    check("chime: agentWaiting emitted with (ws_id, agent_id) on rising edge",
          rings == [(ws.id, agent.id)], rings)
    check("chime: waiting rising edge never marks the session dirty",
          not dirtied, dirtied)

    # fresh output clears waiting; re-settling on the SAME prompt fires again
    agent._on_pty_output("pty", "Bash(ls) running...\r\n")
    check("chime: fresh output clears waiting", not agent.is_waiting())
    agent._on_idle_timeout()
    check("chime: re-entering waiting rings again (edge, not level)",
          len(rings) == 2, rings)


def test_limit_blocked_workspace_stats():
    """workspace_stats()'s limit_blocked count -- and the workspaceStatsChanged
    signal it rides on -- must update LIVE on both edges (an agent gets cut off
    AND an agent resumes), not just the rising one: the sidebar's hourglass
    badge is signal-driven, not polled, exactly like the "?" badge. Separately,
    agentLimitBlocked (which feeds the cut-off ledger/audit trail) stays
    rising-edge-only -- it records the cut-off itself, not its resolution."""
    from PySide6.QtWidgets import QApplication
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limitstats-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("LimitStats", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Stuck",
                                               cwd=str(tmp)), autostart=False)
    check("limit stats: nobody blocked yet",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 0)

    stats_events = []
    blocked_events = []
    mgr.workspaceStatsChanged.connect(
        lambda wid, s: stats_events.append(dict(s)) if wid == ws.id else None)
    mgr.agentLimitBlocked.connect(lambda wid, aid: blocked_events.append((wid, aid)))

    agent.mark_limit_blocked(None, from_startup=False)
    check("limit stats: count is 1 once an agent is latched",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 1)
    check("limit stats: workspaceStatsChanged fired live with the new count",
          stats_events and stats_events[-1]["limit_blocked"] == 1, stats_events)
    check("limit stats: agentLimitBlocked announced the rising edge",
          blocked_events == [(ws.id, agent.id)], blocked_events)

    agent.clear_limit_block()
    check("limit stats: count drops back to 0 the instant the agent resumes",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 0)
    check("limit stats: workspaceStatsChanged fired again on the falling "
          "edge too (the sidebar badge must not be stuck on)",
          stats_events[-1]["limit_blocked"] == 0, stats_events)
    check("limit stats: agentLimitBlocked does NOT fire on the falling edge "
          "(it feeds the cut-off ledger, not the resolution)",
          blocked_events == [(ws.id, agent.id)], blocked_events)


def test_winjob_process_count():
    """WinJob.process_count() reports the live process count for a job -- the
    detection primitive behind poll_bg_shell(). Windows-only; skipped
    everywhere else since job objects don't exist there."""
    if sys.platform != "win32":
        check("winjob: skipped on non-Windows (job objects are Windows-only)",
              True)
        return
    from app.process_worker import WinJob

    job = WinJob()
    check("winjob: a fresh job object gets a real handle on Windows",
          job._handle is not None)
    check("winjob: process_count on an empty (unassigned) job is 0",
          job.process_count() == 0)

    procs = [subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(5)"])
             for _ in range(2)]
    try:
        for p in procs:
            check(f"winjob: assign succeeds for a live child (pid {p.pid})",
                  job.assign(p.pid))
        check("winjob: process_count reflects both assigned processes",
              job.process_count() == 2, job.process_count())
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()
    check("winjob: process_count after close is 0 (no handle)",
          job.process_count() == 0)


def test_winjob_process_ids_and_kill():
    """process_ids() is the identity-carrying sibling of process_count() --
    what kill_bg_shell_extras() needs to know WHICH processes are extra, not
    just how many there are. kill_extra_processes() (identical on
    ProcessWorker and PtyWorker) must kill everything in the job except the
    ids it's told to keep, and leave the kept one alone. Windows-only."""
    if sys.platform != "win32":
        check("winjob kill: skipped on non-Windows (job objects are "
              "Windows-only)", True)
        return
    import types
    from app.process_worker import (CREATE_NO_WINDOW, WinJob, ProcessWorker,
                                    describe_pid)

    job = WinJob()
    # CREATE_NO_WINDOW avoids spawning a console host (conhost.exe) of our
    # own; membership checks below are still subset (<=), not equality --
    # a dev shell already nested inside its own job (this suite can run
    # inside a live AI Hive agent's own terminal) can add incidental extra
    # members that have nothing to do with the primitives under test.
    procs = [subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(20)"],
                              creationflags=CREATE_NO_WINDOW)
             for _ in range(3)]
    try:
        for p in procs:
            check(f"winjob kill: assign succeeds for pid {p.pid}",
                  job.assign(p.pid))
        ids = job.process_ids()
        check("winjob kill: process_ids reports every assigned pid",
              {p.pid for p in procs} <= set(ids), (ids, [p.pid for p in procs]))

        label = describe_pid(procs[0].pid)
        check("winjob kill: describe_pid names a real live process "
              "(python's own executable), not the bare-pid fallback",
              label.lower() != f"pid {procs[0].pid}"
              and label.lower().startswith("python"), label)
        check("winjob kill: describe_pid falls back to a bare label for an "
              "unreachable/invalid pid",
              describe_pid(0) == "pid 0")

        worker = types.SimpleNamespace(_job=job, job_process_ids=job.process_ids)
        keep_pid = procs[0].pid
        killed = ProcessWorker.kill_extra_processes(worker, {keep_pid})
        # a subset check, not exact equality: this test's own nested-job dev
        # environment (a live shell spawning python which spawns python, all
        # already inside another job) can add incidental extra members
        # (conhost.exe, stray interpreter helpers) that have nothing to do
        # with the primitive under test -- what matters is that the two
        # deliberately spawned targets ARE killed and the kept one NEVER is
        check("winjob kill: kills every pid except the one told to keep",
              keep_pid not in killed
              and {procs[1].pid, procs[2].pid} <= set(killed), killed)

        for p in procs[1:]:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        check("winjob kill: the kept process is still alive",
              procs[0].poll() is None)
        check("winjob kill: the killed processes are gone",
              procs[1].poll() is not None and procs[2].poll() is not None)
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()


def test_bg_shell_workspace_stats():
    """poll_bg_shell()/workspace_stats()'s bg_shell count must: debounce a
    transient process blip (git/rg-style, never flags), never flag a BUSY
    agent (the amber "working" indicator already covers that case), flag
    once a job's process count sits above its learned baseline for
    BG_SHELL_DEBOUNCE_S while quiet, clear the instant the extra process is
    gone, and reset on exit -- transient like busy/waiting, never dirty."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import AgentStatus, BG_SHELL_DEBOUNCE_S
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShell", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    check("bg shell stats: nobody flagged yet",
          mgr.workspace_stats(ws.id)["bg_shell"] == 0)

    stats_events = []
    dirtied = []
    mgr.workspaceStatsChanged.connect(
        lambda wid, s: stats_events.append(dict(s)) if wid == ws.id else None)
    mgr.dirty.connect(lambda: dirtied.append(True))

    fake_now = [1000.0]
    orig_time = terminal_agent_mod.time.time
    orig_count = agent.worker.job_process_count
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: count[0]
    try:
        agent.poll_bg_shell()   # learns the baseline (1)
        check("bg shell: the baseline alone never flags",
              not agent.is_bg_shell_busy())

        count[0] = 0   # a failed/unsupported query must never flap state
        agent.poll_bg_shell()
        check("bg shell: a count<=0 (query failed) leaves the state alone",
              not agent.is_bg_shell_busy())
        count[0] = 1

        count[0] = 2   # an extra process appears
        agent.poll_bg_shell()
        check("bg shell: an extra process alone (before the debounce "
              "elapses) does not flag yet", not agent.is_bg_shell_busy())

        fake_now[0] += 1.0   # a blip: gone before the debounce elapses
        count[0] = 1
        agent.poll_bg_shell()
        check("bg shell: a process that goes away before the debounce "
              "elapses never flags (no flicker on git/rg-style blips)",
              not agent.is_bg_shell_busy() and agent._bg_extra_since is None)

        count[0] = 2
        agent.poll_bg_shell()             # extra_since starts again
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell: an extra process sustained past the debounce flags "
              "it", agent.is_bg_shell_busy())
        check("bg shell stats: count is 1 once flagged",
              mgr.workspace_stats(ws.id)["bg_shell"] == 1)
        check("bg shell stats: workspaceStatsChanged fired live",
              stats_events and stats_events[-1]["bg_shell"] == 1, stats_events)
        check("bg shell: never marks the session dirty (transient)",
              not dirtied, dirtied)

        # a genuinely busy agent must never be flagged, even with an extra
        # process present -- the amber "working" indicator already covers it
        agent._busy = True
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: a busy agent is never flagged",
              not agent.is_bg_shell_busy())
        check("bg shell stats: count drops back to 0 while busy",
              mgr.workspace_stats(ws.id)["bg_shell"] == 0)
        agent._busy = False

        # re-flag once busy clears and it's sustained again (the extra-since
        # clock restarted while busy, so this needs its own poll to start,
        # then a later one past the debounce), then clear the instant the
        # extra process itself is gone
        agent.poll_bg_shell()             # extra_since starts now
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: re-flags once busy clears and it's sustained again",
              agent.is_bg_shell_busy())
        count[0] = 1
        agent.poll_bg_shell()
        check("bg shell: clears the instant the extra process is gone",
              not agent.is_bg_shell_busy())
        check("bg shell stats: workspaceStatsChanged fired on the falling "
              "edge too", stats_events[-1]["bg_shell"] == 0, stats_events)

        # exit resets the latch AND the learned baseline
        count[0] = 2
        agent.poll_bg_shell()             # extra_since starts now
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: sanity flag before exit", agent.is_bg_shell_busy())
        agent._set_status(AgentStatus.EXITED_OK)
        check("bg shell: exit clears the flag and its learned baseline",
              not agent.is_bg_shell_busy() and agent._bg_baseline is None)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_count = orig_count


def test_bg_shell_settle_relearn():
    """Live-reported: right after a cold boot, the log_activity MCP bridge's
    own double-fork can still be missing when the FIRST post-warmup sample is
    taken, so a job whose real steady state is 3 processes got a baseline of
    1 -- and since baseline only ratchets down, the gear badge stayed on for
    that agent's entire remaining life (session.log showed count=3
    baseline=1 repeatedly, never clearing). BG_SHELL_SETTLE_S fixes this: for
    a further window after warmup, baseline tracks the latest sample outright
    (up or down) instead of only ratcheting down, so a late-arriving steady
    process is absorbed as normal. Once that settle window elapses too, the
    original ratchet-down-only behavior must still catch a genuine background
    job started later -- the settle window must not weaken that guarantee."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-settle-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellSettle", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING

    fake_now = [1000.0]
    agent._session_started = fake_now[0]   # simulate a just-launched agent
    orig_time = terminal_agent_mod.time.time
    orig_count = agent.worker.job_process_count
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: count[0]
    try:
        agent.poll_bg_shell()   # still within warmup: ignored entirely
        check("bg shell settle: a sample during warmup is ignored",
              agent._bg_baseline is None)

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1   # warmup just elapsed
        count[0] = 1   # the too-early low sample (bridge not forked yet)
        agent.poll_bg_shell()
        check("bg shell settle: first post-warmup sample seeds baseline",
              agent._bg_baseline == 1, agent._bg_baseline)
        check("bg shell settle: never flags while still settling",
              not agent.is_bg_shell_busy())

        fake_now[0] += 2.0   # still within the settle window
        count[0] = 3   # the bridge's child finally forked -- real steady state
        agent.poll_bg_shell()
        check("bg shell settle: baseline RISES to the real steady state "
              "instead of staying locked on the too-early low sample",
              agent._bg_baseline == 3, agent._bg_baseline)
        check("bg shell settle: still doesn't flag mid-settle even though "
              "count rose above the old baseline",
              not agent.is_bg_shell_busy())

        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1   # sustained, but still settling
        agent.poll_bg_shell()
        check("bg shell settle: a sustained-but-unchanged count never flags "
              "while the settle window is still open",
              not agent.is_bg_shell_busy())

        # settle window fully elapsed: ratchet-down-only resumes, and the
        # now-correct baseline must not spuriously flag a steady count
        fake_now[0] = 1000.0 + BG_SHELL_WARMUP_S + BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell settle: once settled, an unchanged steady count "
              "never flags (this is the bug: it used to flag forever)",
              not agent.is_bg_shell_busy())
        check("bg shell settle: baseline holds at the learned steady state",
              agent._bg_baseline == 3, agent._bg_baseline)

        # a GENUINE background job starting after settle must still be
        # caught -- the settle window must not weaken the real detection
        count[0] = 5
        agent.poll_bg_shell()             # extra_since starts
        check("bg shell settle: a fresh extra process doesn't flag before "
              "the debounce elapses", not agent.is_bg_shell_busy())
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell settle: a genuine background job started after "
              "settle still flags once sustained", agent.is_bg_shell_busy())
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_count = orig_count


def test_bg_shell_kill_extras():
    """kill_bg_shell_extras() is a no-op unless the gear is actually lit (a
    stray click on a just-cleared marker must not kill anything), and once
    lit it must ask the worker to kill everything EXCEPT the agent's own
    root process and whatever was seen while the baseline was learned (the
    mcp bridge, ConPTY's own conhost/OpenConsole helper) -- never the whole
    job, which would also take down the interactive session. A successful
    kill clears the latch immediately rather than waiting for the next poll,
    and is recorded to the audit trail."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-kill-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellKill", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    audit_lines = []
    agent.audit = audit_lines.append

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_count = agent.worker.job_process_count
    orig_ids = agent.worker.job_process_ids
    orig_pid = agent.worker.pid
    orig_kill = agent.worker.kill_extra_processes
    pids = [100, 101]           # root(100) + the mcp bridge(101), the baseline
    kill_calls = []
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: len(pids)
    agent.worker.job_process_ids = lambda: list(pids)
    agent.worker.pid = lambda: 100
    agent.worker.kill_extra_processes = (
        lambda keep: kill_calls.append(keep) or [p for p in pids
                                                  if p not in keep])
    try:
        killed = agent.kill_bg_shell_extras()
        check("bg shell kill: a no-op when nothing is flagged",
              killed == [] and not kill_calls, killed)

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1   # seed the baseline: {100,101}
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1    # settle elapses, still {100,101}
        agent.poll_bg_shell()
        check("bg shell kill: baseline pids learned during settle",
              agent._bg_baseline_pids == {100, 101}, agent._bg_baseline_pids)

        pids.append(103)   # a genuine extra shows up (e.g. a Gradle daemon)
        agent.poll_bg_shell()             # extra_since starts
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell kill: flags once the extra is sustained",
              agent.is_bg_shell_busy())

        killed = agent.kill_bg_shell_extras()
        check("bg shell kill: asks the worker to keep the baseline pids "
              "(root + mcp bridge), never the whole job",
              kill_calls and kill_calls[-1] == {100, 101}, kill_calls)
        check("bg shell kill: returns exactly the pid(s) the worker killed",
              killed == [103], killed)
        check("bg shell kill: clears the latch immediately, not on the "
              "next poll", not agent.is_bg_shell_busy())
        check("bg shell kill: the kill is recorded to the audit trail",
              any("BG-SHELL-KILL" in line and "103" in line
                  for line in audit_lines), audit_lines)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_count = orig_count
        agent.worker.job_process_ids = orig_ids
        agent.worker.pid = orig_pid
        agent.worker.kill_extra_processes = orig_kill


def test_chime_persistence():
    """The chime switch flips its glyph + emits soundToggled, and the on/off
    preference round-trips through the session ui state.

    The switch lives in the Options panel as a real track-and-thumb
    `ToggleSwitch` (green/slid-right when armed, grey/slid-left when off),
    label at `sound_label` and switch at `sound_btn`, `isChecked()` mirroring
    the state the thumb is drawn in. The bell/muted-bell glyph is still in
    the label, so the state is readable two ways."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import TopBar

    QApplication.instance() or QApplication([])
    bar = TopBar()
    check("chime toggle: defaults to ON (bell glyph, lit)",
          bar._sound_on and bar.sound_btn.isChecked()
          and "\U0001F514" in bar.sound_label.text()
          and "Notification chime" in bar.sound_label.text(),
          bar.sound_label.text())
    emitted = []
    bar.soundToggled.connect(emitted.append)
    bar.sound_btn.click()
    check("chime toggle: click mutes + emits False + shows muted glyph",
          emitted == [False] and not bar._sound_on
          and "\U0001F515" in bar.sound_label.text()
          and not bar.sound_btn.isChecked(), (emitted, bar._sound_on))
    bar.sound_btn.click()
    check("chime toggle: click again re-enables + emits True",
          emitted == [False, True] and bar._sound_on, emitted)
    # set_sound_enabled reflects state WITHOUT re-emitting (restore path)
    bar.set_sound_enabled(False)
    check("chime toggle: set_sound_enabled updates glyph, no emit",
          not bar._sound_on and emitted == [False, True]
          and not bar.sound_btn.isChecked())
    bar.deleteLater()


def test_taskbar_badge():
    """The Windows taskbar overlay is the ONLY 'agents are working' signal that
    reaches the user in another application, so its state table is the feature.

    Windows allows exactly one overlay icon, fixed to the corner of the taskbar
    button, so the working COUNT and the "someone is asking" flag have to share
    one ~16px square: the digit is the count, the fill colour is the question.
    An idle hive must show NO overlay - that absence is the readout.

    Also checks the two rules a transient indicator in this app always has to
    obey: the count NEVER marks the session dirty (this recomputes every time an
    agent's output starts or stops, so a save here would rewrite session.json
    all day), and the push is edge-guarded on a rendered key (each push builds
    an HICON and crosses a COM boundary, and workspaceStatsChanged fires every
    couple of seconds per busy agent).
    """
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets import ornaments
    from app.widgets.main_window import TopBar
    from app import taskbar_overlay
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    # --- the painter: real pixels, whatever the shell asks for -------------
    size = taskbar_overlay.overlay_size()
    check("taskbar: overlay_size is a sane icon size",
          8 <= size <= 256, size)
    w, h, raw = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_WORKING, 16)
    check("taskbar: badge is a 16x16 BGRA buffer of the right length",
          (w, h) == (16, 16) and len(raw) == 16 * 16 * 4, (w, h, len(raw)))
    # a disc, not a square: the corners stay transparent so it reads as a badge
    # on whatever colour the user's taskbar happens to be
    corner = raw[0:4]
    centre = raw[((8 * 16) + 8) * 4:((8 * 16) + 8) * 4 + 4]
    check("taskbar: the badge is a disc (corner transparent, centre opaque)",
          corner[3] < 40 and centre[3] > 200, (corner[3], centre[3]))
    amber = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_WORKING, 16)[2]
    blue = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_ASKING, 16)[2]
    check("taskbar: the working and asking fills are visibly different",
          amber != blue)
    check("taskbar: a two-character count still renders",
          len(ornaments.taskbar_badge_bgra(
              "9+", ornaments.TASKBAR_WORKING, 16)[2]) == 16 * 16 * 4)

    # --- the state table --------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-taskbar-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    app.processEvents()
    mgr = win.manager
    ws = mgr.create_workspace("Taskbar", str(tmp))
    agents = [mgr.add_terminal(ws.id,
                               build_spec(AgentKind.CLAUDE, f"A{i}",
                                          cwd=str(tmp)), autostart=False)
              for i in range(3)]

    def spec():
        return win._taskbar_badge_spec()

    key, text, fill, note = spec()
    check("taskbar: an idle hive gets NO overlay at all",
          text is None and key == "0|0|0", (key, text))

    agents[0]._busy = True        # what _mark_busy sets on an output burst
    key, text, fill, note = spec()
    check("taskbar: one agent working shows an amber 1",
          (key, text, fill) == ("1|0|0", "1", ornaments.TASKBAR_WORKING),
          (key, text, fill))
    check("taskbar: the description names the count", note == "1 working", note)

    agents[1]._busy = True
    agents[2]._busy = True
    check("taskbar: the count is every working agent across ALL workspaces",
          spec()[1] == "3", spec())

    # the "?" rides the SAME square as a colour swap, since there is no second
    # overlay slot to put it in
    agents[2]._waiting = True
    key, text, fill, note = spec()
    check("taskbar: an agent with a question turns the disc blue, count intact",
          (key, text, fill) == ("3|1|0", "3", ornaments.TASKBAR_ASKING),
          (key, text, fill))
    check("taskbar: the description says someone is waiting",
          "waiting for you" in note, note)

    for a in agents:
        a._busy = False
    key, text, fill, note = spec()
    check("taskbar: nothing working but a question pending shows a blue '?'",
          (key, text, fill) == ("0|1|0", "?", ornaments.TASKBAR_ASKING),
          (key, text, fill))

    agents[2]._waiting = False
    check("taskbar: everything quiet again clears the overlay",
          spec()[1] is None, spec())

    # --- the edge guard: identical states collapse to ONE key -------------
    for a in agents:
        a._busy = True
    first = spec()[0]
    check("taskbar: an unchanged state renders an unchanged key",
          spec()[0] == first, (first, spec()[0]))
    many = [mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, f"B{i}",
                                               cwd=str(tmp)), autostart=False)
            for i in range(9)]
    for a in many:
        a._busy = True
    key, text, _f, _n = spec()
    check("taskbar: past 9 the disc says 9+ (a bigger number is unreadable "
          "at this size) and every such state collapses to one key",
          (key, text) == ("10|0|0", "9+"), (key, text))

    # --- transient: the count must NEVER reach the session ----------------
    win._save_timer.stop()
    win._taskbar_key = None
    win._push_taskbar_badge()
    check("taskbar: pushing the badge never marks the session dirty",
          not win._save_timer.isActive())
    check("taskbar: the push is a no-op off a real taskbar (offscreen suite)",
          win._taskbar_key is None, win._taskbar_key)

    # --- the toggle: a preference, so it DOES save, and it round-trips -----
    bar = TopBar()
    check("taskbar toggle: defaults to ON",
          bar._taskbar_badge and bar.taskbar_btn.isChecked())
    emitted = []
    bar.taskbarBadgeToggled.connect(emitted.append)
    bar.taskbar_btn.click()
    check("taskbar toggle: a click turns it off and emits False",
          emitted == [False] and not bar._taskbar_badge, emitted)
    bar.set_taskbar_badge(True)   # restore path: reflect without re-emitting
    check("taskbar toggle: set_taskbar_badge does not re-emit",
          bar._taskbar_badge and emitted == [False])
    bar.deleteLater()

    win._save_timer.stop()
    win._on_taskbar_badge_toggled(False)
    check("taskbar: flipping the PREFERENCE does mark the session dirty",
          win._save_timer.isActive())
    check("taskbar: switched off, a fully working hive still shows nothing",
          spec()[1] is None, spec())
    payload = win._session_payload()
    check("taskbar: the preference is persisted under ui",
          payload["ui"]["taskbar_badge"] is False, payload["ui"])
    win._restore_ui_state({"ui": {"taskbar_badge": True}})
    check("taskbar: the preference is restored onto the window and the button",
          win._taskbar_badge and win.top_bar._taskbar_badge)
    win._restore_ui_state({"ui": {}})
    check("taskbar: a session that predates the feature defaults it ON",
          win._taskbar_badge)

    # ...and the same thing through a REAL close/reopen, which is the only way
    # to catch the default being assigned after _restore_ui_state has run (it
    # was, and it silently switched the badge back on at every launch).
    win._taskbar_badge = False
    win.top_bar.set_taskbar_badge(False)
    win._save_session()
    win.close()
    app.processEvents()
    again = create_main_window(SessionStore(path=tmp / "session.json"))
    check("taskbar: OFF survives a close and reopen",
          not again._taskbar_badge and not again.top_bar._taskbar_badge,
          again._taskbar_badge)
    check("taskbar: reopening starts with no badge pushed yet",
          again._taskbar_key is None, again._taskbar_key)
    again._save_timer.stop()
    again.close()
    app.processEvents()


def test_bg_shell_taskbar_state():
    """The taskbar's third state: no agent is busy or asking, but one or more
    are idle with a background command still running -- previously invisible
    everywhere, including the taskbar. It must surface ONLY when neither busy
    nor asking is true (both outrank it), and the edge-guard key must still
    coalesce an unchanged state."""
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets import ornaments
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-taskbar-bg-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    app.processEvents()
    mgr = win.manager
    ws = mgr.create_workspace("TaskbarBg", str(tmp))
    agents = [mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, f"A{i}",
                                                 cwd=str(tmp)), autostart=False)
              for i in range(2)]

    def spec():
        return win._taskbar_badge_spec()

    key, text, fill, note = spec()
    check("taskbar bg: idle hive with nothing flagged shows no overlay",
          text is None, (key, text))

    agents[0]._bg_shell = True
    key, text, fill, note = spec()
    check("taskbar bg: one agent idle-on-a-shell shows a violet 1",
          (text, fill) == ("1", ornaments.TASKBAR_BG_SHELL), (text, fill))
    check("taskbar bg: the description names the count",
          "background command" in note, note)

    agents[1]._bg_shell = True
    check("taskbar bg: the count is every bg-shell agent",
          spec()[1] == "2", spec())

    # busy anywhere outranks bg-shell, even elsewhere in the hive
    agents[1]._busy = True
    key, text, fill, note = spec()
    check("taskbar bg: a busy agent elsewhere wins over bg-shell (amber, "
          "busy count only)",
          (text, fill) == ("1", ornaments.TASKBAR_WORKING), (text, fill))
    agents[1]._busy = False

    # asking outranks both
    agents[1]._waiting = True
    key, text, fill, note = spec()
    check("taskbar bg: asking wins over bg-shell too",
          fill == ornaments.TASKBAR_ASKING, (text, fill))
    agents[1]._waiting = False

    # unchanged bg-shell state -> unchanged key (the push's edge guard)
    first_key = spec()[0]
    check("taskbar bg: an unchanged bg-shell state renders an unchanged key",
          spec()[0] == first_key, (first_key, spec()[0]))

    agents[0]._bg_shell = False
    agents[1]._bg_shell = False
    check("taskbar bg: clearing every flag clears the overlay",
          spec()[1] is None, spec())

    win._save_timer.stop()
    win.close()
    app.processEvents()


def test_hook_prompt_events():
    """The Claude-hook script is the AUTHORITATIVE 'needs the user' signal for
    the prompts the screen scrape cannot see. Verify: the shared settings file
    carries PreToolUse/PostToolUse/Stop hooks with the right matchers; the
    dispatch turns each event into the right edge record (AskUserQuestion/
    ExitPlanMode -> tool set/clear; Stop -> tool clear + turn set/clear on the
    'ends with ?' test; a non-waiting tool is ignored); and the incremental
    reader consumes only complete lines and advances its offset."""
    import json as _json
    from app import session_hook as sh

    tmpd = Path(tempfile.mkdtemp(prefix="ai-hive-hook-"))
    settings = str(tmpd / "settings.json")
    mapping = str(tmpd / "map.jsonl")
    events = str(tmpd / "events.jsonl")

    # --- settings file: the new hooks are present + correctly scoped ---
    sh.write_settings_file(settings, mapping, events, python_exe="py")
    with open(settings, encoding="utf-8") as fh:
        hooks = _json.load(fh)["hooks"]
    check("hook: PreToolUse matches AskUserQuestion|ExitPlanMode",
          hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode")
    check("hook: PostToolUse matches the same waiting tools",
          hooks["PostToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode")
    check("hook: Stop hook present (turn-end / free-text question)",
          "Stop" in hooks and hooks["Stop"][0]["hooks"])
    check("hook: the command carries BOTH the map and events paths",
          events in hooks["PreToolUse"][0]["hooks"][0]["command"])
    # SessionStart is unchanged and still excludes `startup` (launch-timing)
    check("hook: SessionStart still matches only resume|clear|compact",
          hooks["SessionStart"][0]["matcher"] == "resume|clear|compact")
    # omitting events_path degrades to SessionStart-only (back-compat)
    sh.write_settings_file(settings, mapping, python_exe="py")
    with open(settings, encoding="utf-8") as fh:
        only = _json.load(fh)["hooks"]
    check("hook: no events path -> SessionStart-only (no prompt hooks)",
          set(only) == {"SessionStart"})

    # --- dispatch: one payload -> the right edge record(s) ---
    os.environ[sh.AGENT_ID_ENV] = "agent-xyz"
    try:
        def dispatch(payload):
            sh._dispatch(mapping, events, payload)

        sh.reset_events(events)
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"})
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "Bash"})  # ignored
        dispatch({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"})
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode"})
        dispatch({"hook_event_name": "Stop",
                  "last_assistant_message": "Which of these do you prefer?"})
        dispatch({"hook_event_name": "Stop",
                  "last_assistant_message": "All done and pushed."})

        recs, off = sh.read_prompt_events(events, 0)
        kinds = [r["kind"] for r in recs]
        check("hook: AskUserQuestion PreToolUse -> tool_set (Bash ignored)",
              kinds[0] == sh.EV_TOOL_SET and sh.EV_TOOL_SET not in kinds[1:2],
              kinds)
        check("hook: every record is attributed to the env agent id",
              all(r["agent_id"] == "agent-xyz" for r in recs))
        check("hook: PostToolUse -> tool_clear",
              sh.EV_TOOL_CLEAR in kinds)
        check("hook: ExitPlanMode PreToolUse -> tool_set",
              kinds.count(sh.EV_TOOL_SET) == 2)
        # Stop on a question: tool_clear THEN turn_set; on a statement: turn_clear
        check("hook: Stop ending in '?' -> tool_clear + turn_set",
              sh.EV_TURN_SET in kinds
              and kinds.index(sh.EV_TOOL_CLEAR) < kinds.index(sh.EV_TURN_SET))
        check("hook: Stop ending in a statement -> turn_clear (not set)",
              sh.EV_TURN_CLEAR in kinds)

        # --- incremental read: offset advances, no line re-delivered ---
        recs2, off2 = sh.read_prompt_events(events, off)
        check("hook: reading again from the offset yields nothing new",
              recs2 == [] and off2 == off, (recs2, off2, off))
        # a half-written trailing line is NOT consumed until it completes
        with open(events, "a", encoding="utf-8") as fh:
            fh.write('{"agent_id":"agent-xyz","kind":"tool_set"')  # no newline
        recs3, off3 = sh.read_prompt_events(events, off)
        check("hook: a partial trailing line is held back until newline arrives",
              recs3 == [] and off3 == off)
        with open(events, "a", encoding="utf-8") as fh:
            fh.write(',"ts":1}\n')  # complete it
        recs4, off4 = sh.read_prompt_events(events, off)
        check("hook: the completed line is delivered exactly once",
              len(recs4) == 1 and recs4[0]["kind"] == sh.EV_TOOL_SET
              and off4 > off)
    finally:
        os.environ.pop(sh.AGENT_ID_ENV, None)

    # _ends_with_question tolerates trailing wrapping punctuation
    check("hook: question detector strips trailing quotes/parens",
          sh._ends_with_question('Do you want that?"')
          and sh._ends_with_question("Shall I proceed?)")
          and not sh._ends_with_question("Done.")
          and not sh._ends_with_question("I asked: why? Then I fixed it."))


def test_agent_hook_waiting():
    """TerminalAgent combines three waiting sources. The hook-driven ones catch
    what the screen scrape cannot: a tool prompt (AskUserQuestion/ExitPlanMode)
    stays flagged THROUGH its own output (sticky), while a free-text turn
    question clears the moment fresh output arrives (the user engaged)."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Ask", cwd="."))
    a.status = AgentStatus.RUNNING
    events = []
    a.waiting_changed.connect(events.append)

    # tool prompt: set by the hook, and NOT cleared by the tool's own UI output
    a.set_tool_waiting(True)
    check("agent-hook: tool prompt flags waiting + emits True",
          a.is_waiting() and events and events[-1] is True)
    a._on_pty_output("pty", "rendering the question box...\r\n")
    check("agent-hook: a tool prompt survives its own output (sticky)",
          a.is_waiting())
    a.set_tool_waiting(False)
    check("agent-hook: PostToolUse clear drops waiting", not a.is_waiting())

    # free-text turn question: cleared by the next output burst
    a.set_turn_waiting(True)
    check("agent-hook: a free-text turn question flags waiting", a.is_waiting())
    a._on_pty_output("pty", "sure, doing it now...\r\n")
    check("agent-hook: fresh output clears a turn question (user engaged)",
          not a.is_waiting())

    # a tool prompt OR-ed with the scrape: clearing one leaves the other
    a.set_tool_waiting(True)
    a._screen_tail = ("which?\r\n 1. a\r\n 2. b\r\n> 1. a")
    a._on_idle_timeout()  # scrape also True now
    a.set_tool_waiting(False)
    check("agent-hook: clearing the tool source leaves scrape-waiting intact",
          a.is_waiting())

    a._set_status(AgentStatus.EXITED_OK)
    check("agent-hook: exit resets every waiting source", not a.is_waiting())


def test_manager_prompt_events_sync():
    """End-to-end: the manager reads the hook events file incrementally and
    drives each agent's waiting state, ringing the chime on the rising edge.
    This is the regression for the reported miss: an AskUserQuestion prompt
    (which renders no numbered menu) now lights the '?' and rings. A stale
    turn-question edge is ignored while the agent is already producing output."""
    from PySide6.QtWidgets import QApplication
    from app import session_hook as sh
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-psync-"))
    events = str(tmp / "events.jsonl")
    sh.reset_events(events)
    mgr = WorkspaceManager()
    mgr.prompt_events_path = events
    ws = mgr.create_workspace("Sync", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Ask",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    rings = []
    dirtied = []
    mgr.agentWaiting.connect(lambda wid, aid: rings.append((wid, aid)))
    mgr.dirty.connect(lambda: dirtied.append(True))

    def emit(kind):
        os.environ[sh.AGENT_ID_ENV] = agent.id
        try:
            sh._append_event(events, kind)
        finally:
            os.environ.pop(sh.AGENT_ID_ENV, None)

    # AskUserQuestion opens -> tool_set line -> agent waits + chime rings
    emit(sh.EV_TOOL_SET)
    mgr.sync_prompt_events()
    check("psync: AskUserQuestion (tool_set) lights '?' + rings the chime",
          agent.is_waiting() and rings == [(ws.id, agent.id)], (rings,))
    check("psync: the hook waiting edge never marks the session dirty",
          not dirtied, dirtied)

    # the tool's own render must NOT drop it (sticky), then PostToolUse clears
    agent._on_pty_output("pty", "question box...\r\n")
    check("psync: tool prompt survives its own output through the manager",
          agent.is_waiting())
    emit(sh.EV_TOOL_CLEAR)
    mgr.sync_prompt_events()
    check("psync: PostToolUse (tool_clear) drops waiting", not agent.is_waiting())

    # a turn_set that arrives while the agent is BUSY is stale -> ignored
    agent._busy = True
    emit(sh.EV_TURN_SET)
    mgr.sync_prompt_events()
    check("psync: a turn question is ignored while the agent is busy (stale)",
          not agent.is_waiting())
    # once quiet, a fresh turn question is honored
    agent._busy = False
    emit(sh.EV_TURN_SET)
    mgr.sync_prompt_events()
    check("psync: a turn question while idle flags waiting + rings again",
          agent.is_waiting() and len(rings) == 2, (rings,))

    # nothing new on the next poll (edges applied exactly once)
    before = len(rings)
    mgr.sync_prompt_events()
    check("psync: re-polling with no new lines is a no-op",
          agent.is_waiting() and len(rings) == before)


def test_input_echo_not_busy():
    """The pty echoes the user's OWN keystrokes back as output; that echo must
    NOT light the sidebar 'working' pulse (typing at an idle prompt made the row
    animate as if the agent were thinking). Genuine agent output still does."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Echo", cwd="."))
    a.status = AgentStatus.RUNNING
    acts = []
    a.activity_changed.connect(acts.append)

    # write() records the user-input time even though the unstarted worker no-ops
    before = a._last_input_ts
    a.write("h")
    check("echo: write() records the user-input timestamp",
          a._last_input_ts > before)

    # keystroke echo (output right after the user typed) does NOT flag busy
    a.write("i")
    a._on_pty_output("pty", "i")   # the pty echoing the typed char back
    check("echo: typing at the prompt does NOT flag the agent busy",
          not a.is_busy() and acts == [], acts)

    # genuine agent output (no recent keystroke) DOES flag busy + emits
    a._last_input_ts = 0.0   # as if the user hasn't typed in a long while
    a._on_pty_output("pty", "Thinking... running the task\r\n")
    check("echo: real agent output (no recent input) flags busy + emits",
          a.is_busy() and acts == [True], acts)

    # a keystroke arriving mid-work does not DROP an already-lit pulse here...
    a.write("x")
    a._on_pty_output("pty", "x")
    check("echo: an echo burst leaves an existing pulse untouched",
          a.is_busy() and acts == [True], acts)
    # ...but the idle timer (armed by the last REAL output, not the echo) still
    # drops it once output truly falls quiet
    a._on_idle_timeout()
    check("echo: the pulse drops when output settles", not a.is_busy())


def test_sidebar_reorder():
    """Drag-reorder: the sidebar's drop handler re-sequences its node model and
    emits the new top-to-bottom ws-id order; a rebuild keeps every row."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar

    QApplication.instance() or QApplication([])
    sb = Sidebar()
    sb.add_row("a", "Alpha", "A")
    sb.add_row("b", "Bravo", "B")
    sb.add_row("c", "Charlie", "C")
    seen = []
    sb.reordered.connect(lambda ids: seen.append(list(ids)))

    check("reorder: initial order a,b,c",
          sb._ws_node_ids() == ["a", "b", "c"], sb._ws_node_ids())
    sb._on_node_dropped("workspace", "c", "a", "above")   # c above a
    check("reorder: c dropped above a -> c,a,b",
          sb._ws_node_ids() == ["c", "a", "b"], sb._ws_node_ids())
    check("reorder: reordered signal carried the new order",
          seen and seen[-1] == ["c", "a", "b"], seen)
    sb._on_node_dropped("workspace", "c", "b", "below")   # c below b
    check("reorder: c dropped below b -> a,b,c",
          sb._ws_node_ids() == ["a", "b", "c"], sb._ws_node_ids())
    check("reorder: rebuild kept all three rows",
          set(sb._ws_widgets) == {"a", "b", "c"}, set(sb._ws_widgets))
    order_before = sb._ws_node_ids()
    sb._on_node_dropped("workspace", "b", "b", "below")   # dropped on itself
    check("reorder: dropping a row on itself is a no-op",
          sb._ws_node_ids() == order_before, sb._ws_node_ids())
    sb.deleteLater()


def test_manager_reorder_persist():
    """WorkspaceManager.reorder_workspaces re-sequences workspaces, fires
    sidebarLayoutChanged, and the order survives a session round-trip."""
    from PySide6.QtWidgets import QApplication
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-reorder-"))
    mgr = WorkspaceManager()
    a = mgr.create_workspace("Alpha", str(tmp))
    b = mgr.create_workspace("Bravo", str(tmp))
    c = mgr.create_workspace("Charlie", str(tmp))
    fired = []
    mgr.sidebarLayoutChanged.connect(lambda: fired.append(True))

    mgr.reorder_workspaces([c.id, a.id, b.id])
    check("mgr reorder: workspaces now c,a,b",
          [w.id for w in mgr.workspaces] == [c.id, a.id, b.id])
    check("mgr reorder: sidebarLayoutChanged fired", bool(fired))

    data = mgr.to_session_dict()
    check("mgr reorder: session persists the new order",
          [w["id"] for w in data["workspaces"]] == [c.id, a.id, b.id])
    mgr2 = WorkspaceManager()
    mgr2.load_session_dict(data)
    check("mgr reorder: reload keeps c,a,b order",
          [w.id for w in mgr2.workspaces] == [c.id, a.id, b.id])


def test_agent_card_reorder():
    """Drag-to-reorder agent cards within a workspace: WorkspacePage._reorder_to
    re-sequences cards + emits reorderCommitted, _drop_index maps a cursor to an
    insertion index, the header gesture starts a drag only past a threshold, the
    drag is gated off in solo / single-card, and WorkspaceManager.reorder_agents
    re-sequences + persists the agent order."""
    from PySide6.QtCore import QEvent, QPointF, QRect, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.workspace_page import WorkspacePage
    from app.widgets.terminal_card import CARD_REORDER_MIME
    from app.workspace_manager import WorkspaceManager, Workspace
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    QApplication.instance() or QApplication([])

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-cardreorder-"))
    ws = Workspace(id="w1", name="WS", project_path=str(tmp))
    for nm in ("A", "B", "C"):
        ws.agents.append(TerminalAgent(build_spec(AgentKind.CLAUDE, nm,
                                                  cwd=str(tmp))))
    page = WorkspacePage(ws)
    for a in ws.agents:
        page.add_agent(a)
    names = lambda: [c.agent.spec.name for c in page.cards]
    check("reorder: cards start in A,B,C order", names() == ["A", "B", "C"])

    committed = []
    page.reorderCommitted.connect(lambda wid, ids: committed.append((wid, ids)))
    page._reorder_to(page.cards[0], 2)   # move A to the end
    check("reorder: moving A to index 2 yields B,C,A", names() == ["B", "C", "A"])
    check("reorder: reorderCommitted emits (ws_id, new id order)",
          committed and committed[0][0] == "w1"
          and committed[0][1] == [c.agent.id for c in page.cards])

    committed.clear()
    page._reorder_to(page.cards[1], 1)   # already at index 1 → no change
    check("reorder: a no-op move does not re-emit", committed == [])

    # _drop_index maps a cursor (grid-host coords) to an insertion index in
    # reading order; drive it with hand-set geometries (headless has no layout)
    page._drag_card = None
    # cards clamp to a 300x180 minimum, so lay the synthetic row out above that
    for i, c in enumerate(page.cards):
        c.setGeometry(QRect(i * 300, 0, 300, 180))   # centers at 150, 450, 750
    check("reorder: cursor before the first card -> index 0",
          page._drop_index(QPointF(20, 90).toPoint()) == 0)
    check("reorder: cursor over the 2nd card's right half -> index 2",
          page._drop_index(QPointF(500, 90).toPoint()) == 2)
    check("reorder: cursor past the last card -> index 3 (append)",
          page._drop_index(QPointF(900, 90).toPoint()) == 3)

    # gating: not our mime / solo / single card must be rejected
    class _Mime:
        def __init__(self, ok): self._ok = ok
        def hasFormat(self, f): return self._ok and f == CARD_REORDER_MIME
    class _Evt:
        def __init__(self, ok): self._m = _Mime(ok)
        def mimeData(self): return self._m
    check("reorder: a foreign drag is not reorderable",
          not page._reorderable(_Evt(False)))
    check("reorder: reorderable with 2+ cards, not soloed",
          page._reorderable(_Evt(True)))
    page._solo_card = page.cards[0]
    check("reorder: disabled while a card is maximized (solo)",
          not page._reorderable(_Evt(True)))
    page._solo_card = None

    # header gesture: a drag past the slop starts a reorder; a tiny move doesn't
    card = page.cards[0]
    fired = []
    card._begin_reorder_drag = lambda: fired.append(1)   # avoid blocking QDrag
    hdr = card.header
    press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(40, 10),
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier)
    move_big = QMouseEvent(QEvent.Type.MouseMove, QPointF(80, 12),
                           Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                           Qt.KeyboardModifier.NoModifier)
    hdr.mousePressEvent(press)
    hdr.mouseMoveEvent(move_big)
    check("reorder: header drag past threshold starts the reorder drag",
          fired == [1])
    fired.clear()
    move_tiny = QMouseEvent(QEvent.Type.MouseMove, QPointF(43, 11),
                            Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
    hdr.mousePressEvent(press)
    hdr.mouseMoveEvent(move_tiny)
    check("reorder: a sub-threshold move does NOT drag (click/rename safe)",
          fired == [])

    # manager side: reorder_agents re-sequences ws.agents + marks dirty, and the
    # new order is what serializes (persistence is by list position)
    mgr = WorkspaceManager()
    w = mgr.create_workspace("WS", str(tmp))
    ags = [TerminalAgent(build_spec(AgentKind.CLAUDE, n, cwd=str(tmp)))
           for n in ("A", "B", "C")]
    w.agents.extend(ags)
    dirty = []
    mgr.dirty.connect(lambda: dirty.append(1))
    mgr.reorder_agents(w.id, [ags[2].id, ags[0].id, ags[1].id])
    check("mgr reorder_agents: ws.agents now C,A,B",
          [a.spec.name for a in w.agents] == ["C", "A", "B"])
    check("mgr reorder_agents: marked the session dirty", bool(dirty))
    data = mgr.to_session_dict()
    terms = data["workspaces"][0]["terminals"]
    check("mgr reorder_agents: serialized order matches (persist by position)",
          [t["name"] for t in terms] == ["C", "A", "B"])
    dirty.clear()
    mgr.reorder_agents(w.id, [ags[2].id, ags[0].id, ags[1].id])  # same order
    check("mgr reorder_agents: an unchanged order does not re-dirty",
          dirty == [])


def test_workspace_header():
    """The workspace header bar owns Open folder / Change… / Delete (not
    duplicated as sidebar hover buttons any more) — delete sits LEFT of open,
    matching the sidebar's old left-to-right order — and the path shows the
    FULL text whenever it fits, eliding only the overflow the header's own
    buttons would otherwise be squeezed by."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.workspace_page import WorkspacePage
    from app.workspace_manager import Workspace

    app = QApplication.instance() or QApplication([])
    long_path = "C:/Users/someone/Documents/Really/Deeply/Nested/Project/ai-hive"
    ws = Workspace(id="w1", name="WS", project_path=long_path)
    page = WorkspacePage(ws)

    order = [page._header_lay.itemAt(i).widget()
             for i in range(page._header_lay.count())]
    order = [w for w in order if w is not None]
    idx_delete = order.index(page.delete_btn)
    idx_open = order.index(page.open_btn)
    idx_change = order.index(page.change_btn)
    check("workspace header: delete sits left of open folder, open left of "
          "change",
          idx_delete < idx_open < idx_change,
          (idx_delete, idx_open, idx_change))

    deleted = []
    page.deleteRequested.connect(deleted.append)
    page.delete_btn.click()
    check("workspace header: delete button emits deleteRequested(ws_id)",
          deleted == ["w1"], deleted)

    # a wide window has room for the full path plus every button
    page.resize(1600, 200)
    page.show()
    app.processEvents()
    check("workspace header: a wide window shows the FULL path, not "
          "truncated to a stub with empty space beside it",
          page.path_label.text() == long_path, page.path_label.text())

    # a narrow window does not have room for the buttons AND the full path;
    # the path must give way rather than overlap/squeeze the buttons
    page.resize(260, 200)
    app.processEvents()
    check("workspace header: a narrow window elides the path instead of "
          "overlapping the buttons",
          page.path_label.text() != long_path, page.path_label.text())

    # growing back out restores the full path (not stuck at the smaller
    # elision like the old self-referential width computation)
    page.resize(1600, 200)
    app.processEvents()
    check("workspace header: the full path comes back once there is room "
          "again",
          page.path_label.text() == long_path, page.path_label.text())
    page.deleteLater()


def test_sidebar_categories():
    """Categories: create one, drag workspaces into/out of it, collapse it, and
    delete it (its workspaces spill back out in place)."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar

    QApplication.instance() or QApplication([])
    sb = Sidebar()
    for i in ("a", "b", "c"):
        sb.add_row(i, i.upper(), i)
    emits = []
    sb.layoutChanged.connect(lambda nodes: emits.append(nodes))

    cid = sb._add_category("Work")
    check("cat: category node created", sb._cat_node(cid) is not None)
    sb._on_node_dropped("workspace", "a", cid, "on")
    sb._on_node_dropped("workspace", "b", cid, "on")
    check("cat: a,b are children of the category",
          sb._cat_node(cid)["children"] == ["a", "b"],
          sb._cat_node(cid)["children"])
    check("cat: c stays top-level; all three still present",
          sb._ws_node_ids() == ["c", "a", "b"], sb._ws_node_ids())

    sb._on_node_dropped("workspace", "a", "c", "above")  # a back out
    check("cat: a moved out; category keeps only b",
          sb._cat_node(cid)["children"] == ["b"],
          sb._cat_node(cid)["children"])

    sb._on_cat_toggled(cid)
    check("cat: toggle collapses the category",
          sb._cat_node(cid)["collapsed"] is True)

    sb._on_cat_deleted(cid)
    check("cat: delete removes category; child b spills back out",
          sb._cat_node(cid) is None and set(sb._ws_node_ids()) == {"a", "b", "c"},
          sb._ws_node_ids())
    check("cat: layoutChanged fired on each structural change", len(emits) >= 5)
    sb.deleteLater()


def test_manager_categories_persist():
    """A category layout (collapse state + membership + order) survives a v4
    session round-trip, and a v3 session migrates to all-uncategorized."""
    from PySide6.QtWidgets import QApplication
    from app.workspace_manager import WorkspaceManager, SESSION_VERSION

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-cat-"))
    mgr = WorkspaceManager()
    a = mgr.create_workspace("Alpha", str(tmp))
    b = mgr.create_workspace("Bravo", str(tmp))
    c = mgr.create_workspace("Charlie", str(tmp))
    mgr.apply_sidebar_layout([
        {"type": "workspace", "id": a.id},
        {"type": "category", "id": "cat1", "name": "Work",
         "collapsed": True, "children": [c.id, b.id]},
    ])
    check("cat persist: flattened order is a, c, b",
          [w.id for w in mgr.workspaces] == [a.id, c.id, b.id])

    data = mgr.to_session_dict()
    check("cat persist: session version is 4", data["version"] == SESSION_VERSION)
    check("cat persist: sidebar key holds the category",
          any(n["type"] == "category" and n["children"] == [c.id, b.id]
              for n in data["sidebar"]), data["sidebar"])

    mgr2 = WorkspaceManager()
    mgr2.load_session_dict(data)
    lay = mgr2.sidebar_layout()
    cat = next((n for n in lay if n["type"] == "category"), None)
    check("cat persist: reload restores the category + collapse + children",
          cat is not None and cat["collapsed"] is True
          and cat["children"] == [c.id, b.id], cat)
    check("cat persist: reload keeps flattened order a, c, b",
          [w.id for w in mgr2.workspaces] == [a.id, c.id, b.id])

    # a v3 session (no "sidebar" key) migrates to all-uncategorized in order
    v3 = {"version": 3, "active": "",
          "workspaces": [{"id": "x", "name": "X", "project_path": str(tmp),
                          "layout": "auto", "terminals": []},
                         {"id": "y", "name": "Y", "project_path": str(tmp),
                          "layout": "auto", "terminals": []}]}
    mgr3 = WorkspaceManager()
    mgr3.load_session_dict(v3)
    check("cat persist: v3 migrates to uncategorized in load order",
          [n["id"] for n in mgr3.sidebar_layout()] == ["x", "y"]
          and all(n["type"] == "workspace" for n in mgr3.sidebar_layout()))


def test_category_container():
    """The category container (a tinted box behind a category + its members)
    keys off row-group membership: the header and its child workspaces belong to
    the group; a top-level workspace outside does not. It paints headlessly."""
    from PySide6.QtGui import QPixmap
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar

    QApplication.instance() or QApplication([])
    sb = Sidebar()
    sb.resize(240, 220)
    sb.add_row("top", "Standalone", "p")
    sb.add_row("w", "Web", "p")
    cid = sb._add_category("Work")
    sb._cat_widgets[cid]._end_rename()
    sb._on_node_dropped("workspace", "w", cid, "on")
    tree = sb.tree
    cat_idx = tree.indexFromItem(sb._cat_items[cid])
    w_idx = tree.indexFromItem(sb._ws_items["w"])
    top_idx = tree.indexFromItem(sb._ws_items["top"])
    check("container: category header is its own group + header",
          tree._row_category(cat_idx) == cid
          and tree._is_category_header(cat_idx))
    check("container: a workspace filed in the category joins the group",
          tree._row_category(w_idx) == cid, tree._row_category(w_idx))
    check("container: a top-level workspace is in NO category group",
          tree._row_category(top_idx) == "", tree._row_category(top_idx))
    sb.show()
    QApplication.processEvents()
    pm = QPixmap(sb.size())
    sb.render(pm)   # drawRow container painting must not raise
    check("container: sidebar with a category paints headlessly", True)
    sb.deleteLater()


def test_agent_inline_expansion():
    """Clicking a workspace's count badge expands its agents INLINE in the
    sidebar (folder-tree style, not a popup): each agent shows its name on the
    left with the task summary beside it and a "?" when waiting; clicking an
    agent relays (ws_id, agent_id) to reveal it; clicking the badge again
    collapses. Expanding must not select/switch the workspace."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a1 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Backend", cwd="."))
    a2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Frontend", cwd="."))
    a1.status = AgentStatus.RUNNING
    a1.current_task = "implementing the payments webhook"
    sb = Sidebar()
    sb.resize(230, 300)
    roster = {"w1": [a1, a2]}
    sb.agents_provider = lambda ws_id: roster.get(ws_id, [])
    sb.add_row("w1", "Alpha", "p")

    asked, selected = [], []
    sb.agentsRequested.connect(asked.append)
    sb._ws_widgets["w1"].selected.connect(selected.append)
    sb._ws_widgets["w1"].count_badge.clicked.emit()      # expand
    check("inline: badge click asks for the agent list", asked == ["w1"], asked)
    check("inline: badge click did NOT select/switch the workspace",
          selected == [], selected)
    check("inline: workspace expanded", "w1" in sb._expanded_ws)
    check("inline: one inline agent row per agent (keyed by id)",
          set(sb._agent_rows) == {a1.id, a2.id}, set(sb._agent_rows))
    check("inline: agent name shown on the left",
          sb._agent_rows[a1.id].name.text() == "Backend",
          sb._agent_rows[a1.id].name.text())
    check("inline: task summary shown beside the name when present",
          sb._agent_rows[a1.id].summary.text() != ""
          and sb._agent_rows[a2.id].summary.text() == "",
          (sb._agent_rows[a1.id].summary.text(),
           sb._agent_rows[a2.id].summary.text()))

    # the row's status dot shows WORK, not liveness (mirrors the workspace
    # badge): a RUNNING-but-quiet agent is green; actively producing output
    # flips it amber; exit clears amber regardless of busy.
    check("inline: RUNNING-but-quiet agent dot is green",
          sb._agent_rows[a1.id].dot.text() == "🟢",
          sb._agent_rows[a1.id].dot.text())
    a1._busy = True
    sb._agent_rows[a1.id].refresh(a1)
    check("inline: a busy (working) agent dot is amber",
          sb._agent_rows[a1.id].dot.text() == "🟡",
          sb._agent_rows[a1.id].dot.text())
    a1.status = AgentStatus.EXITED_OK
    sb._agent_rows[a1.id].refresh(a1)
    check("inline: busy flag never overrides a non-RUNNING status",
          sb._agent_rows[a1.id].dot.text() == "⚪",
          sb._agent_rows[a1.id].dot.text())
    a1.status = AgentStatus.RUNNING
    a1._busy = False
    sb._agent_rows[a1.id].refresh(a1)

    relayed = []
    sb.agentActivated.connect(lambda w, ag: relayed.append((w, ag)))
    QTest.mouseClick(sb._agent_rows[a1.id], Qt.MouseButton.LeftButton)
    check("inline: clicking an agent relays (ws_id, agent_id)",
          relayed == [("w1", a1.id)], relayed)

    # closing an agent must update the inline list (the earlier check missed
    # removals). set_stats fires on terminal add/remove -> immediate sync.
    roster["w1"] = [a2]
    sb.set_stats("w1", {"total": 1, "active": 1, "busy": 0, "error": 0,
                        "waiting": 0, "idle": 1})
    check("inline: removing an agent drops its row immediately",
          set(sb._agent_rows) == {a2.id}, set(sb._agent_rows))
    # adding one back is reflected too (via the live timer sync)
    roster["w1"] = [a2, a1]
    sb._sync_expanded()
    check("inline: adding an agent re-shows it",
          set(sb._agent_rows) == {a1.id, a2.id}, set(sb._agent_rows))

    sb._ws_widgets["w1"].count_badge.clicked.emit()      # collapse
    check("inline: badge click again collapses the agent list",
          "w1" not in sb._expanded_ws and not sb._agent_rows,
          (sb._expanded_ws, list(sb._agent_rows)))
    sb.deleteLater()
    a1.deleteLater()
    a2.deleteLater()


def test_fsopen_helpers():
    """The shared OS-open helpers must no-op safely on a missing path (never
    raise, never launch anything)."""
    import os
    import tempfile
    import app.fsopen as fsopen
    missing = os.path.join(tempfile.gettempdir(), "aihive_no_such_file_9271.xyz")
    check("fsopen: open_path on a missing file no-ops (False)",
          fsopen.open_path(missing) is False)
    check("fsopen: open_path on empty path no-ops (False)",
          fsopen.open_path("") is False)
    fsopen.open_with(missing)          # must not raise
    fsopen.reveal_in_folder(missing)   # must not raise
    check("fsopen: open_with / reveal_in_folder no-op on missing path", True)


def test_filetypes_icons():
    """File-type icons map by extension; folder icon is distinct; unknown/no
    extension falls back to the generic document icon."""
    from app import filetypes as ft
    check("filetypes: image extensions share one icon",
          ft.file_icon("a.PNG") == ft.file_icon("b.jpg") != ft.DEFAULT_ICON)
    check("filetypes: python has its own icon (not generic code)",
          ft.file_icon("m.py") not in (ft.DEFAULT_ICON, ft.file_icon("x.js")))
    check("filetypes: unknown / no extension -> generic icon",
          ft.file_icon("weird.zzz") == ft.DEFAULT_ICON
          and ft.file_icon("Makefile") == ft.DEFAULT_ICON)
    check("filetypes: folder icon is distinct from any file icon",
          ft.FOLDER_ICON not in ft.FILE_ICONS.values()
          and ft.FOLDER_ICON != ft.DEFAULT_ICON)


def test_terminal_relative_link():
    """Ctrl+click a path in a conversation opens it: absolute paths work, and a
    RELATIVE path resolves against the agent cwd set via set_base_dir (the
    common case, since Claude prints repo-relative paths). Opening a file emits
    fileActivated(abspath) so the app can reveal it in the sidebar tree."""
    import os
    import tempfile
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y, TerminalView)
    QApplication.instance() or QApplication([])

    base = tempfile.mkdtemp(prefix="aihive_link_")
    os.makedirs(os.path.join(base, "app", "widgets"), exist_ok=True)
    rel = os.path.join("app", "widgets", "sidebar.py")
    absf = os.path.join(base, rel)
    with open(absf, "w", encoding="utf-8") as fh:
        fh.write("x = 1\n")

    tv = TerminalView(rows=6, cols=80)
    tv.resize(700, 200)
    # no base dir yet -> a relative path is NOT openable
    check("link: relative path is not classified without a base dir",
          tv._classify_link("app/widgets/sidebar.py") is None)
    tv.set_base_dir(base)
    check("link: relative path resolves against the base dir",
          tv._classify_link("app/widgets/sidebar.py")
          == ("file", os.path.abspath(absf)))
    check("link: relative path + :line[:col] suffix still resolves",
          tv._classify_link("app/widgets/sidebar.py:42:7")
          == ("file", os.path.abspath(absf)))
    check("link: absolute existing path still classifies as a file",
          tv._classify_link(absf) == ("file", os.path.abspath(absf)))
    check("link: a URL still classifies as a url",
          tv._classify_link("https://example.com")[0] == "url")
    check("link: a non-existent relative path is not openable",
          tv._classify_link("does/not/exist.py") is None)

    # Ctrl+left-click over the path emits fileActivated (open suppressed so the
    # test never launches a program)
    tv._open_target = lambda *_a, **_k: None
    tv.feed("app/widgets/sidebar.py\r\n")
    got = []
    tv.fileActivated.connect(got.append)
    x = CELL_PAD_X + int(3 * tv._cell_w)   # a cell inside the path token
    y = CELL_PAD_Y + int(0 * tv._cell_h) + 1
    ev = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(x, y),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.ControlModifier)
    tv.mousePressEvent(ev)
    check("link: Ctrl+click a path emits fileActivated(abspath)",
          got == [os.path.abspath(absf)], got)
    tv.deleteLater()


def test_terminal_block_glyphs():
    """Block Elements (U+2580-U+259F) are painted GEOMETRICALLY on the cell
    grid, not handed to the font.

    Regression for Claude Code's welcome mascot rendering visibly broken. The
    mascot is full blocks plus QUADRANTS (U+2598/259B/259C/259D), and Consolas
    has no quadrant glyphs: Qt fell back to Segoe UI Symbol, whose advance is
    12px where the cell is 7. Because paintEvent batches a run of cells into
    one drawText, that single glyph dragged the rest of the row ~5px right and
    the mascot's rows sheared apart; the fallback ink was also the wrong shape
    for the cell, notching every corner. Separately, Consolas' own full block
    is 6.7px of ink in a 7.0px advance, leaving a hairline of background at
    each cell boundary that striped solid artwork.

    So: exact fills on a shared rounded grid (no seam, no overlap, no drift),
    and any glyph the font lacks is drawn ALONE so it cannot shear its row."""
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import (CELL_PAD_X, _BLOCK_RECTS,
                                           _BLOCK_SHADES, TerminalView)
    QApplication.instance() or QApplication([])

    tv = TerminalView(rows=8, cols=40)
    tv.resize(500, 220)

    # --- the grid: adjacent cells must share an edge exactly ---------------
    check("blocks: cells tile horizontally with no seam or overlap",
          all(tv.cell_bounds(c, 0)[2] == tv.cell_bounds(c + 1, 0)[0]
              for c in range(39)))
    check("blocks: cells tile vertically with no seam or overlap",
          all(tv.cell_bounds(0, r)[3] == tv.cell_bounds(0, r + 1)[1]
              for r in range(7)))
    # a fractional cell width is the case that breaks a width-per-cell scheme
    # (rounding error accumulates across the row) - deriving both edges from
    # the same expression cannot drift
    tv._cell_w, tv._cell_h = 7.4, 15.6
    check("blocks: fractional cell size tiles seamlessly and never drifts",
          all(tv.cell_bounds(c, 0)[2] == tv.cell_bounds(c + 1, 0)[0]
              for c in range(39))
          and tv.cell_bounds(39, 0)[2] == round(CELL_PAD_X + 40 * 7.4))
    tv.deleteLater()

    # --- pixel coverage ----------------------------------------------------
    FG = QColor(255, 255, 255).rgb()   # white: max contrast, never re-tinted

    def render(text, cols=12, rows=4):
        v = TerminalView(rows=rows, cols=cols)
        v.resize(260, 120)
        v.feed("\x1b[38;2;255;255;255m" + text)
        return v, v.grab().toImage()

    def lit(img, v, col, row, u, w):
        """Is the cell's fractional point (u, w) painted foreground?"""
        x0, y0, x1, y1 = v.cell_bounds(col, row)
        return QColor(img.pixel(x0 + int((x1 - x0) * u),
                                y0 + int((y1 - y0) * w))).rgb() == FG

    # full block: every pixel of the cell, and no gap where two blocks meet
    v, img = render("███")
    x0, y0, _, y1 = v.cell_bounds(0, 0)
    x1 = v.cell_bounds(2, 0)[2]
    check("blocks: full block fills its cell edge to edge (no hairline seam)",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(x0, x1) for y in range(y0, y1)))
    v.deleteLater()

    # halves and quadrants: boundaries at 1/2 round cleanly, so assert the
    # exact quarter-by-quarter mask the Unicode chart specifies
    QUARTERS = ((0.25, 0.25), (0.75, 0.25), (0.25, 0.75), (0.75, 0.75))
    exact = []
    for data in ("▀", "▄", "▌", "▐", "▖", "▗",
                 "▘", "▙", "▚", "▛", "▜", "▝",
                 "▞", "▟", "█"):
        v, img = render(data)
        for (u, w) in QUARTERS:
            want = any(fx0 <= u < fx1 and fy0 <= w < fy1
                       for (fx0, fy0, fx1, fy1) in _BLOCK_RECTS[data])
            if lit(img, v, 0, 0, u, w) != want:
                exact.append((hex(ord(data)), u, w))
        v.deleteLater()
    check("blocks: every half/quadrant covers exactly its quarters of the cell",
          not exact, exact)

    # eighth blocks: ink on the correct side, nothing on the opposite half
    eighths = []
    for data, inside, outside in (("▁", (0.5, 0.97), (0.5, 0.25)),
                                  ("▔", (0.5, 0.02), (0.5, 0.75)),
                                  ("▏", (0.02, 0.5), (0.75, 0.5)),
                                  ("▕", (0.97, 0.5), (0.25, 0.5))):
        v, img = render(data)
        if not lit(img, v, 0, 0, *inside) or lit(img, v, 0, 0, *outside):
            eighths.append(hex(ord(data)))
        v.deleteLater()
    check("blocks: eighth blocks hug their own edge of the cell", not eighths,
          eighths)

    # shades are the full cell at partial opacity - present but not solid
    v, img = render("░▒▓")
    shades = [QColor(img.pixel(*[(v.cell_bounds(c, 0)[0]
                                  + v.cell_bounds(c, 0)[2]) // 2,
                                 (v.cell_bounds(c, 0)[1]
                                  + v.cell_bounds(c, 0)[3]) // 2])).lightness()
              for c in range(3)]
    check("blocks: the three shades render at increasing density",
          shades[0] < shades[1] < shades[2] < QColor(FG).lightness(), shades)
    check("blocks: shade table covers exactly the three shade codepoints",
          set(_BLOCK_SHADES) == {"░", "▒", "▓"})
    check("blocks: geometry table covers the rest of U+2580-U+259F",
          set(_BLOCK_RECTS) | set(_BLOCK_SHADES)
          == {chr(c) for c in range(0x2580, 0x25A0)})
    v.deleteLater()

    # vertical neighbours must meet: lower half over upper half is solid
    v, img = render("▄\r\n▀")
    bx0, _, bx1, seam = v.cell_bounds(0, 0)
    check("blocks: a lower half over an upper half leaves no horizontal seam",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(bx0, bx1) for y in range(seam - 2, seam + 2)))
    v.deleteLater()

    # --- shear guard: an off-grid glyph must not move its neighbours -------
    v = TerminalView(rows=4, cols=12)
    check("blocks: an ASCII glyph is grid-safe", v._is_grid_glyph("M"))
    off = [c for c in "▘▛✳⏸"
           if not v._is_grid_glyph(c)]
    check("blocks: glyphs missing from the terminal font are flagged off-grid",
          off, "none of the probes fell back - font coverage changed?")
    v.deleteLater()

    # the mascot itself: legs sit on exact cell boundaries, body is solid
    v, img = render("▝▜█████▛▘"
                    "\r\n  ▘▘ ▝▝", cols=14)
    body_x0, body_y0, _, body_y1 = v.cell_bounds(2, 0)
    body_x1 = v.cell_bounds(6, 0)[2]
    check("blocks: mascot body renders solid (no shear gaps, no stripes)",
          all(QColor(img.pixel(x, y)).rgb() == FG
              for x in range(body_x0, body_x1)
              for y in range(body_y0, body_y1)))
    legs = [c for c in range(14)
            if lit(img, v, c, 1, 0.25, 0.25) or lit(img, v, c, 1, 0.75, 0.25)]
    check("blocks: mascot legs land on their own cells (no fallback drift)",
          legs == [2, 3, 5, 6], legs)
    v.deleteLater()


def test_terminal_link_underline():
    """Every clickable URL/path on the visible screen is scanned + underlined
    (not just the hovered one), so links stand out in the body text; the scan
    is content-guarded (no rescan when nothing changed); hover reads the cached
    spans; plain words are pre-filtered out (no filesystem stat)."""
    import os
    import tempfile
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import TerminalView
    QApplication.instance() or QApplication([])

    base = tempfile.mkdtemp(prefix="aihive_ul_")
    os.makedirs(os.path.join(base, "app", "widgets"), exist_ok=True)
    open(os.path.join(base, "app", "widgets", "sidebar.py"), "w").close()

    tv = TerminalView(rows=6, cols=90)
    tv.resize(820, 220)
    tv.set_base_dir(base)
    tv.feed("See https://example.com and app/widgets/sidebar.py here\r\n")
    tv.grab()   # force a paint -> content-guarded rescan

    hist, off = tv._view_state()
    tokens = ["".join(tv._visible_line(r, hist, off)[i].data
                      for i in range(c0, c1 + 1))
              for (r, c0, c1) in tv._link_spans]
    check("underline: both a URL and a relative path are scanned as links",
          any(t.startswith("https://example.com") for t in tokens)
          and any(t.endswith("sidebar.py") for t in tokens)
          and len(tv._link_spans) == 2)
    check("underline: plain words are pre-filtered (no fs stat)",
          not tv._maybe_link("here") and not tv._maybe_link("and"))

    # the scan is content-guarded: same content -> same cached span object
    sig_before = tv._link_sig
    spans_obj = tv._link_spans
    tv.grab()
    check("underline: no rescan when the screen content is unchanged",
          tv._link_sig == sig_before and tv._link_spans is spans_obj)

    # hover reads the cached spans (no filesystem work): a cell inside a link
    # resolves to its span; a blank cell resolves to nothing
    r, c0, c1 = tv._link_spans[0]
    check("underline: _span_at finds the link under a cell", tv._span_at(r, c0) == (c0, c1))
    check("underline: _span_at is None off any link", tv._span_at(r, c1 + 1) is None)

    # new content re-scans (the URL is gone, so no links remain)
    tv.feed("\x1b[2J\x1b[Hjust plain text now\r\n")
    tv.grab()
    check("underline: rescans when content changes (links cleared)",
          tv._link_spans == [])
    tv.deleteLater()


def test_sidebar_file_tree():
    """The sidebar's inline file explorer: the ▸ toggle expands a lazily-built
    file/folder tree under the workspace row; clicking a folder expands it;
    clicking a file emits fileActivated; reveal_file scrolls to + highlights a
    file (and no-ops when the tree is closed); the open/closed set round-trips
    for persistence. All of it is transient except the open/closed set."""
    import os
    import tempfile
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar, TreeEntryRow
    QApplication.instance() or QApplication([])

    root = tempfile.mkdtemp(prefix="aihive_tree_")
    os.makedirs(os.path.join(root, "pkg", "sub"), exist_ok=True)
    open(os.path.join(root, "readme.md"), "w").close()
    open(os.path.join(root, "pkg", "mod.py"), "w").close()
    deep = os.path.join(root, "pkg", "sub", "deep.py")
    open(deep, "w").close()

    sb = Sidebar()
    sb.resize(230, 400)
    sb.files_root_provider = lambda ws_id: root if ws_id == "w1" else ""
    sb.add_row("w1", "Alpha", "p")

    def tree_rows():
        return [r for r in sb._file_rows.values()]

    check("tree: closed by default (no file rows)", not tree_rows())
    toggled = []
    sb.filesToggled.connect(lambda: toggled.append(1))
    sb._ws_widgets["w1"].tree_btn.clicked.emit()      # open
    check("tree: toggle opens the file tree", "w1" in sb._expanded_files)
    check("tree: open emits filesToggled (persist trigger)", toggled == [1])
    names = {r.name.text() or r._full for r in sb._file_rows.values()}
    check("tree: top-level entries are listed (dirs + files)",
          any(r.is_dir and r._full == "pkg" for r in sb._file_rows.values())
          and any(not r.is_dir and r._full == "readme.md"
                  for r in sb._file_rows.values()))
    check("tree: nested dir NOT populated until expanded",
          not any(r._full == "mod.py" for r in sb._file_rows.values()))

    # expand the "pkg" directory
    sb._on_dir_toggled("w1", "pkg")
    check("tree: expanding a dir lists its children",
          any(r._full == "mod.py" for r in sb._file_rows.values()))
    check("tree: grandchild still hidden (lazy)",
          not any(r._full == "deep.py" for r in sb._file_rows.values()))

    # clicking a file row emits fileActivated(ws_id, abspath)
    fired = []
    sb.fileActivated.connect(lambda w, p: fired.append((w, p)))
    modrow = next(r for r in sb._file_rows.values() if r._full == "mod.py")
    from PySide6.QtTest import QTest
    from PySide6.QtCore import Qt as _Qt
    QTest.mouseClick(modrow, _Qt.MouseButton.LeftButton)
    check("tree: clicking a file emits fileActivated(ws_id, abspath)",
          fired == [("w1", os.path.abspath(os.path.join(root, "pkg", "mod.py")))],
          fired)

    # reveal_file expands ancestors + highlights, even a deep file
    sb.reveal_file("w1", deep)
    drow = next((r for r in sb._file_rows.values() if r._full == "deep.py"), None)
    check("tree: reveal_file expands ancestors to show the file", drow is not None)
    check("tree: revealed row is highlighted",
          drow is not None and bool(drow.property("revealed")))

    # persistence round-trip
    check("tree: open_file_trees reports the open set",
          sb.open_file_trees() == ["w1"])
    sb.set_open_file_trees([])
    check("tree: set_open_file_trees([]) closes it",
          "w1" not in sb._expanded_files and not sb._file_rows)
    sb.set_open_file_trees(["w1", "ghost"])   # unknown id dropped
    check("tree: set_open_file_trees restores known ids, drops unknown",
          sb._expanded_files == {"w1"})

    # closing drops the per-directory expansion (transient)
    sb._ws_widgets["w1"].tree_btn.clicked.emit()      # close
    check("tree: closing clears expansion + rows",
          "w1" not in sb._expanded_files
          and not sb._expanded_dirs.get("w1")
          and not sb._file_rows)
    check("tree: reveal_file no-ops when the tree is closed (no raise)",
          sb.reveal_file("w1", deep) is None)
    sb.deleteLater()


def test_sidebar_search():
    """The header search: the magnifier reveals an overlay field over the
    title+count; typing highlights matching workspaces, agents, and agent
    summaries (auto-expanding a workspace to reveal a matching agent); Esc /
    toggling closes it and restores the pre-search expansion. All transient."""
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec
    QApplication.instance() or QApplication([])
    a1 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Backend", cwd="."))
    a1.status = AgentStatus.RUNNING
    a1.current_task = "implement the payments webhook"
    a2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Frontend", cwd="."))
    sb = Sidebar()
    sb.resize(230, 400)
    sb.agents_provider = lambda w: {"w1": [a1, a2]}.get(w, [])
    sb.add_row("w1", "Alpha Project", "p")
    sb.add_row("w2", "Beta", "p")

    check("search: hidden until toggled",
          sb.search_edit.isHidden() and not sb._search_active)
    sb._toggle_search()
    check("search: toggle opens the field + hides the title/count",
          sb._search_active and not sb.search_edit.isHidden()
          and sb.title.isHidden() and sb.count_label.isHidden())

    sb.search_edit.setText("alpha")
    check("search: workspace NAME match highlights that workspace",
          sb._search_ws_hits == {"w1"}
          and bool(sb._ws_widgets["w1"].property("search_hit"))
          and not bool(sb._ws_widgets["w2"].property("search_hit")))

    sb.search_edit.setText("webhook")   # matches a1's summary only
    check("search: agent SUMMARY match highlights the agent + expands its ws",
          sb._search_agent_hits == {a1.id} and "w1" in sb._expanded_ws
          and bool(sb._agent_rows[a1.id].property("search_hit"))
          and not bool(sb._agent_rows[a2.id].property("search_hit")))
    check("search: the matching agent's workspace is highlighted too",
          bool(sb._ws_widgets["w1"].property("search_hit")))

    sb.search_edit.setText("Frontend")  # matches a2 by name
    check("search: agent NAME match highlights the right agent",
          sb._search_agent_hits == {a2.id})

    sb.search_edit.setText("zzz-no-match")
    check("search: no match clears all highlights",
          sb._search_ws_hits == set() and sb._search_agent_hits == set())

    esc = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                    Qt.KeyboardModifier.NoModifier)
    sb.eventFilter(sb.search_edit, esc)
    check("search: Esc closes the field + restores the header",
          not sb._search_active and sb.search_edit.isHidden()
          and not sb.title.isHidden() and sb._expanded_ws == set())

    # a pre-search expansion is preserved across a whole search session
    sb._expanded_ws = {"w1"}
    sb._toggle_search()
    sb.search_edit.setText("beta")       # matches w2 by name, no agent match
    check("search: does not collapse a pre-expanded workspace",
          "w1" in sb._expanded_ws)
    sb._toggle_search()                  # close
    check("search: closing restores exactly the pre-search expansion",
          sb._expanded_ws == {"w1"})
    sb.deleteLater()


def test_ai_title_summary():
    """The per-agent summary is the assigned task, else Claude's latest
    AI-generated conversation title read from the transcript (the same title
    shown in /resume). latest-title parsing takes the LAST ai-title record and
    re-reads when the file changes; task always wins over the AI title."""
    import json as _json
    from PySide6.QtWidgets import QApplication
    from app import transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- transcript parsing: latest ai-title wins, refreshes on change ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-title-"))
    tpath = tmp / "conv.jsonl"
    recs = [{"type": "user", "text": "hi"},
            {"type": "ai-title", "aiTitle": "First guess", "sessionId": "s"},
            {"type": "assistant", "text": "working"},
            {"type": "ai-title", "aiTitle": "Recolor the badge", "sessionId": "s"}]
    tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                     encoding="utf-8")
    check("ai-title: returns the LAST title record",
          transcripts._read_latest_ai_title(str(tpath)) == "Recolor the badge",
          transcripts._read_latest_ai_title(str(tpath)))
    with open(tpath, "a", encoding="utf-8") as fh:   # conversation evolves
        fh.write(_json.dumps({"type": "ai-title", "aiTitle": "Add the spinner",
                              "sessionId": "s"}) + "\n")
    check("ai-title: re-reads when the transcript changes",
          transcripts._read_latest_ai_title(str(tpath)) == "Add the spinner")
    check("ai-title: missing file -> empty string",
          transcripts._read_latest_ai_title(str(tmp / "nope.jsonl")) == "")

    # --- summary precedence on the agent ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd="."))
    seen = []
    a.summary_changed.connect(seen.append)
    check("summary: empty with no task and no title", a.summary() == "")
    a.set_ai_title("Recolor the badge")
    check("summary: falls back to the AI title", a.summary() == "Recolor the badge"
          and seen[-1] == "Recolor the badge", (a.summary(), seen))
    a.set_task("Fix the parser")
    check("summary: assigned task overrides the AI title",
          a.summary() == "Fix the parser" and seen[-1] == "Fix the parser")
    n = len(seen)
    a.set_ai_title("A newer title")   # title changes but task still wins
    check("summary: no signal when the DISPLAYED summary is unchanged",
          a.summary() == "Fix the parser" and len(seen) == n, (a.summary(), seen))
    a.set_task("")                    # task cleared -> falls back to AI title
    check("summary: clearing the task reveals the AI title",
          a.summary() == "A newer title" and seen[-1] == "A newer title",
          (a.summary(), seen))
    a.deleteLater()


def test_token_usage_badge():
    """The card header shows a compact context-window usage badge (e.g.
    "20% of 1M") read from the transcript's last assistant usage record.
    Transient like the AI title: computed from input+cache+output tokens,
    sized to the model's window, sidechains skipped, never persisted."""
    import json as _json
    from PySide6.QtWidgets import QApplication
    from app import transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- context-window sizing per model ---
    check("tokens: opus-4.x sized to the 1M window",
          transcripts.context_window_for("claude-opus-4-8") == 1_000_000)
    check("tokens: unknown model falls back to 200K",
          transcripts.context_window_for("some-old-model") == 200_000)

    # --- transcript parsing: last main-turn usage, sidechains skipped ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tokens-"))
    tpath = tmp / "conv.jsonl"

    def _asst(inp, cc, cr, out, model="claude-opus-4-8", side=False):
        return {"type": "assistant", "isSidechain": side,
                "message": {"model": model, "role": "assistant",
                            "usage": {"input_tokens": inp,
                                      "cache_creation_input_tokens": cc,
                                      "cache_read_input_tokens": cr,
                                      "output_tokens": out}}}

    recs = [{"type": "user", "text": "hi"},
            _asst(10, 0, 100, 50),                 # early main turn
            _asst(5, 0, 999999, 5, side=True),     # sub-agent: must be ignored
            _asst(100, 900, 199000, 1000)]         # latest main turn -> 201000
    tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                     encoding="utf-8")
    used, window = transcripts._read_latest_token_usage(str(tpath))
    check("tokens: sums the LAST main-conversation usage record",
          used == 201000, used)
    check("tokens: sidechain usage never wins", used == 201000)
    check("tokens: window bumped to 1M when a turn exceeds 200K",
          window == 1_000_000, window)
    check("tokens: missing file -> (0, 0)",
          transcripts._read_latest_token_usage(str(tmp / "nope.jsonl")) == (0, 0))

    # re-reads when the transcript grows
    with open(tpath, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(_asst(1, 0, 299999, 0)) + "\n")  # -> 300000
    used2, _ = transcripts._read_latest_token_usage(str(tpath))
    check("tokens: re-reads when the transcript changes", used2 == 300000, used2)

    # --- agent badge formatting + transient emission ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd="."))
    seen = []
    a.tokens_changed.connect(seen.append)
    check("tokens: badge empty before any usage", a.token_badge() == "")
    a.set_token_usage(200_000, 1_000_000)
    check("tokens: badge formats as '20% of 1M'",
          a.token_badge() == "20% of 1M" and seen[-1] == "20% of 1M",
          (a.token_badge(), seen))
    a.set_token_usage(50_000, 200_000)
    check("tokens: 200K window renders as 'K'", a.token_badge() == "25% of 200K")
    n = len(seen)
    a.set_token_usage(50_000, 200_000)   # unchanged -> no re-emit
    check("tokens: no signal when usage is unchanged", len(seen) == n)
    a.set_token_usage(0, 0)              # cleared -> badge hidden again
    check("tokens: clearing usage empties the badge", a.token_badge() == "")
    a.deleteLater()


def test_live_model_effort():
    """The card header says which model, effort and permission mode the agent
    is ACTUALLY on. All three change mid-session (/model, /effort, Shift+Tab in
    the terminal), so the reading comes from the transcript: assistant records
    carry message.model + a top-level effort, a /model or /effort pick writes a
    local-command-stdout record the instant the user chooses, and the mode rides
    on every prompt plus a dedicated permission-mode record. Last in file order
    wins. Model and effort are transient (they never touch spec and never save);
    the MODE is written back, because it is the flag that reopens the agent in
    the same mode."""
    import json as _json
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import providers, transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    # --- model id normalization ---
    cases = [("claude-opus-5", "Opus 5"),
             ("claude-opus-4-8", "Opus 4.8"),
             ("claude-haiku-4-5-20251001", "Haiku 4.5"),
             ("claude-sonnet-5[1m]", "Sonnet 5 (1M)"),
             ("opus", "Opus"),
             ("Opus 4.8 (1M context)", "Opus 4.8 (1M)"),
             ("", "")]
    for raw, want in cases:
        check(f"model: {raw or 'empty'} displays as {want or 'empty'}",
              transcripts.model_display(raw) == want,
              transcripts.model_display(raw))

    # --- transcript parsing ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-model-"))
    tpath = tmp / "conv.jsonl"

    def _turn(model="claude-opus-5", effort="high", side=False):
        return {"type": "assistant", "isSidechain": side, "effort": effort,
                "message": {"model": model, "role": "assistant"}}

    def _pick(text):
        return {"type": "user",
                "message": {"role": "user",
                            "content": f"<local-command-stdout>{text}"
                                       f"</local-command-stdout>"}}

    def write(recs):
        tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                         encoding="utf-8")

    write([{"type": "user", "text": "hi"}, _turn()])
    check("model: a turn reports its model and effort",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # a /model pick AFTER the last turn is the newer truth (the whole point:
    # an idle agent's switch must show without waiting for another turn)
    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions")])
    check("model: a /model pick after the last turn wins",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _pick("Set effort level to max (this session only): Maximum "
                 "capability with deepest reasoning.")])
    check("model: a /effort pick changes only the effort",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "max", ""),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mOpus 4.8 (1M context)\x1b[22m and saved "
                 "as your default for new sessions with \x1b[1mhigh\x1b[22m "
                 "effort")])
    check("model: a pick that names an effort applies both",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8 (1M)", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # a turn AFTER a pick wins again (ordering is file order, not kind)
    write([_pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _turn(model="claude-fable-5", effort="max")])
    check("model: a turn after a pick wins again",
          transcripts._read_model_effort(str(tpath)) == ("Fable 5", "max", ""),
          transcripts._read_model_effort(str(tpath)))

    # a sub-agent's model is a different context; <synthetic> is not a model
    write([_turn(), _turn(model="claude-haiku-4-5", effort="low", side=True),
           {"type": "assistant", "message": {"model": "<synthetic>"}}])
    check("model: sidechain and synthetic records never win",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    check("model: missing file reads as unknown",
          transcripts.latest_model_effort(str(tmp), "nope") == ("", "", ""))

    # the reader only touches the tail, so it must still find evidence that sits
    # behind a long stretch of unrelated records (full-scan fallback)
    filler = [{"type": "user", "text": "x" * 400} for _ in range(400)]
    write([_turn(model="claude-opus-4-8", effort="xhigh")] + filler)
    check("model: falls back to a full scan when the tail has no evidence",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8", "xhigh", ""),
          transcripts._read_model_effort(str(tpath)))

    # --- the permission mode (Shift+Tab), read off the same transcript ---
    def _mode(mode):    # the record Claude appends the instant the mode changes
        return {"type": "permission-mode", "permissionMode": mode}

    write([_turn(), _mode("plan")])
    check("mode: a permission-mode record is the live mode",
          transcripts._read_model_effort(str(tpath))[2] == "plan",
          transcripts._read_model_effort(str(tpath)))
    write([_mode("plan"), _mode("auto")])
    check("mode: the LAST mode record wins",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))
    # an ordinary prompt carries the mode it was submitted under, which is what
    # covers a CLI build that writes no dedicated record
    write([_mode("plan"),
           {"type": "user", "permissionMode": "auto",
            "message": {"role": "user", "content": "go"}}])
    check("mode: a user prompt's own permissionMode counts too",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))
    write([_mode("auto"),
           {"type": "user", "isSidechain": True, "permissionMode": "plan",
            "message": {"role": "user", "content": "sub"}}])
    check("mode: a sidechain's mode is a sub-agent's, never this one's",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))

    # translation: the transcript's names are not all launch flags
    check("mode: 'default' is spelled by omitting the flag",
          providers.normalize_permission_mode("default") == ""
          and providers.normalize_permission_mode("manual") == "")
    check("mode: a real CLI token passes through",
          providers.normalize_permission_mode("auto") == "auto"
          and providers.normalize_permission_mode("plan") == "plan")
    check("mode: an unknown token never reaches the command line",
          providers.normalize_permission_mode("wat") == "")
    check("mode: 'default' reads as manual on the card",
          providers.permission_mode_display("default") == "manual"
          and providers.permission_mode_display("") == "manual")
    # ...and a mode adopted from a conversation must be launchable, even though
    # the New Agent dropdown never offers it
    _, auto_args = providers.build_invocation("claude", permission_mode="auto")
    check("mode: 'auto' survives into the launch flags",
          auto_args == ["--permission-mode", "auto"], auto_args)
    _, bogus_args = providers.build_invocation("claude", permission_mode="wat")
    check("mode: a bogus mode is dropped rather than launched", bogus_args == [])

    # the spec REBUILDS its args, or the new mode would persist while every
    # launch in this process kept using the old flag
    mspec = build_spec(AgentKind.CLAUDE, "Mode", cwd=str(tmp))
    check("mode: a fresh spec carries no --permission-mode",
          "--permission-mode" not in mspec.args, mspec.args)
    check("mode: adopting a mode reports the change",
          mspec.set_permission_mode("plan")
          and not mspec.set_permission_mode("plan"))
    check("mode: adopting a mode rebuilds the launch args",
          mspec.args[-2:] == ["--permission-mode", "plan"], mspec.args)
    check("mode: the adopted mode round-trips through the session file",
          AgentSpec.from_dict(mspec.to_dict()).permission_mode == "plan"
          and "--permission-mode" in AgentSpec.from_dict(mspec.to_dict()).args)
    mspec.set_permission_mode("")
    check("mode: going back to the default drops the flag again",
          "--permission-mode" not in mspec.args, mspec.args)

    # --- agent-side badge: transient, emits only on a real change ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd=".",
                                 model="opus", effort="high"))
    seen = []
    a.model_changed.connect(seen.append)
    check("model: badge seeded from the launch flags",
          a.model_badge() == "Opus · high · manual", a.model_badge())
    a.set_live_model("Sonnet 5", "max")
    check("model: badge follows the live reading",
          a.model_badge() == "Sonnet 5 · max · manual"
          and seen[-1] == "Sonnet 5 · max · manual",
          (a.model_badge(), seen))
    n = len(seen)
    a.set_live_model("Sonnet 5", "max")
    check("model: no signal when the reading is unchanged", len(seen) == n)
    a.set_live_model("", "")
    check("model: an empty reading never blanks a good label",
          a.model_badge() == "Sonnet 5 · max · manual" and len(seen) == n)
    a.set_live_model("", "", "plan")
    check("model: badge follows the live permission mode",
          a.model_badge() == "Sonnet 5 · max · plan"
          and seen[-1] == "Sonnet 5 · max · plan", (a.model_badge(), seen))
    a.set_live_model("Sonnet 5", "max", "auto")
    check("model: the mode label is the CLI's own name",
          a.model_badge() == "Sonnet 5 · max · auto", a.model_badge())
    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bare", cwd="", model="",
                                 effort=""))
    b._live_model = ""      # no launch flag and no saved user default
    check("model: badge hidden when nothing is known", b.model_badge() == "")
    b.set_live_model("Opus 5", "")
    check("model: model alone renders without an effort",
          b.model_badge() == "Opus 5 · manual", b.model_badge())
    sh = TerminalAgent(build_spec(AgentKind.POWERSHELL, "Shell", cwd=""))
    check("model: a shell has no permission mode to show",
          sh.permission_mode_label() == "" and sh.model_badge() == "",
          (sh.permission_mode_label(), sh.model_badge()))

    # --- header: the chip shows/hides with the badge ---
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(80)
    check("model: card chip shows the live model, effort and mode",
          card.model_label.isVisible()
          and card.model_label.text() == "Sonnet 5 · max · auto",
          card.model_label.text())
    card2 = TerminalCard(b)
    b._live_model = ""
    card2._on_model(b.model_badge())
    card2.resize(900, 300); card2.show(); pump(60)
    check("model: card chip hidden when the model is unknown",
          not card2.model_label.isVisible())

    # --- the manager's poll adopts it, and never saves for it ---
    from app.workspace_manager import WorkspaceManager
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Models", str(tmp))
    live = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Live",
                                              cwd=str(tmp)), autostart=False)
    live.spec.session_id = "11111111-2222-3333-4444-555555555555"
    live.is_running = lambda: True    # stand in for a launched process
    conv = Path(transcripts.transcript_path(str(tmp), live.spec.session_id))
    conv.parent.mkdir(parents=True, exist_ok=True)
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n", encoding="utf-8")
    dirtied = []
    mgr.dirty.connect(lambda: dirtied.append(True))
    mgr.refresh_model_effort()
    check("model: the manager poll adopts the transcript's model/effort",
          live.model_badge() == "Sonnet 5 · low · manual", live.model_badge())
    check("model: a reading never marks the session dirty", not dirtied, dirtied)

    # ...except the permission mode, which IS the launch flag for next time:
    # the CLI does not carry a mode across --resume, so an agent the user put
    # in plan mode came back ask-each-time on every reopen
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n" + _json.dumps(_mode("plan")) + "\n",
                    encoding="utf-8")
    mgr.refresh_model_effort()
    check("mode: the manager poll shows the live mode",
          live.model_badge() == "Sonnet 5 · low · plan", live.model_badge())
    check("mode: the live mode is written back as the next launch flag",
          live.spec.permission_mode == "plan"
          and live.spec.args[-2:] == ["--permission-mode", "plan"],
          (live.spec.permission_mode, live.spec.args))
    check("mode: adopting a mode marks the session dirty", dirtied)
    check("mode: the adopted mode reaches the persisted session record",
          mgr.to_session_dict()["workspaces"][0]["terminals"][0].get(
              "permission_mode") == "plan")
    dirtied.clear()
    mgr.refresh_model_effort()
    check("mode: an unchanged mode never re-saves", not dirtied, dirtied)
    # the ask-each-time mode is written "default" in the transcript and has no
    # flag spelling, so it must clear the flag rather than launch a bogus one
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n" + _json.dumps(_mode("default")) + "\n",
                    encoding="utf-8")
    mgr.refresh_model_effort()
    check("mode: going back to ask-each-time clears the flag",
          live.spec.permission_mode == ""
          and "--permission-mode" not in live.spec.args, live.spec.args)
    check("mode: clearing it reaches the persisted session record too",
          mgr.to_session_dict()["workspaces"][0]["terminals"][0].get(
              "permission_mode", "-") == "")
    shutil.rmtree(conv.parent, ignore_errors=True)

    # --- the summary uses the width it was actually given ---
    long_task = ("Rework the pty worker so grandchildren die with the job "
                 "object and the graceful stop stays an stdin EOF, then check "
                 "the restart path keeps its pinned conversation")
    a.set_task(long_task)
    pump(80)
    shown = card.task_summary.text()
    check("summary: fits the real header width, not a fixed character count",
          len(shown) > 60, (len(shown), shown))
    check("summary: keeps the untruncated text on hover",
          card.task_summary.toolTip() == long_task)
    card.resize(420, 300); pump(80)
    narrow = card.task_summary.text()
    check("summary: re-fits when the card narrows",
          len(narrow) < len(shown), (len(narrow), len(shown)))
    a.set_task("")
    pump(40)
    check("summary: empty task clears the label",
          card.task_summary.text() == "")

    card.detach(); card2.detach()
    card.close(); card2.close()
    a.deleteLater(); b.deleteLater()
    shutil.rmtree(tmp, ignore_errors=True)


def test_limit_blocked_live_ui():
    """The card header's hourglass and the sidebar's inline agent-row hourglass
    both say "stopped by the usage limit" -- and both must clear the instant
    the agent resumes (auto-continue or a manual restart), not sit there
    stale. The card is signal-driven (limit_blocked_changed), the sidebar
    AgentRow is polled (refresh() reads is_limit_blocked() directly) -- this
    exercises both paths end to end through the real clear_limit_block()."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.sidebar import AgentRow

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Stuck", cwd="."))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    check("limit UI: hourglass hidden before any cut-off",
          not card.limit_mark.isVisible())

    a.mark_limit_blocked(None, from_startup=False)
    pump(30)
    check("limit UI: card hourglass shows once the agent latches blocked",
          card.limit_mark.isVisible())

    row = AgentRow("w1", a)
    check("limit UI: a freshly built sidebar agent row also shows it",
          not row.limit_mark.isHidden())

    a.clear_limit_block()
    pump(30)
    check("limit UI: card hourglass disappears LIVE on resume (the bug this "
          "fixes: clear_limit_block used to never emit the falling edge)",
          not card.limit_mark.isVisible())
    row.refresh(a)
    check("limit UI: sidebar agent row also clears on its next poll",
          row.limit_mark.isHidden())

    card.detach(); card.close()
    row.deleteLater()
    a.deleteLater()


def test_bg_shell_live_ui():
    """The card header's gear mark and the sidebar's inline agent-row gear
    mark both say "idle, but a background command it started is still
    running" -- and both must clear the instant it isn't true anymore. The
    card is signal-driven (bg_shell_changed), the sidebar AgentRow is polled
    (refresh() reads is_bg_shell_busy() directly) -- this exercises both
    paths end to end."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.sidebar import AgentRow

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Shelled", cwd="."))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    check("bg shell UI: gear hidden before anything is flagged",
          not card.bg_mark.isVisible())

    a._bg_shell = True
    a.bg_shell_changed.emit(True)
    pump(30)
    check("bg shell UI: card gear shows once the agent is flagged",
          card.bg_mark.isVisible())

    row = AgentRow("w1", a)
    check("bg shell UI: a freshly built sidebar agent row also shows it",
          not row.bg_mark.isHidden())

    a._bg_shell = False
    a.bg_shell_changed.emit(False)
    pump(30)
    check("bg shell UI: card gear disappears LIVE once cleared",
          not card.bg_mark.isVisible())
    row.refresh(a)
    check("bg shell UI: sidebar agent row also clears on its next poll",
          row.bg_mark.isHidden())

    # clicking either opens a menu of individual processes to kill, rather
    # than an all-or-nothing kill on the click itself -- an accidental click
    # near the badge must not risk killing something an agent is actually
    # waiting on. bg_shell_extra_pids() is empty here (no baseline was ever
    # learned in this test), so both handlers must take the early-return
    # "nothing to show" path rather than opening a real (blocking) QMenu --
    # this is what proves a click can never fall back to killing everything.
    a._bg_shell = True
    a.bg_shell_changed.emit(True)
    pump(30)
    check("bg shell UI: nothing to kill yet (no baseline learned)",
          a.bg_shell_extra_pids() == [])
    card.bg_mark.click()          # must NOT hang on a QMenu.exec() or crash
    check("bg shell UI: clicking the card gear with nothing resolvable is a "
          "safe no-op, not a kill-everything fallback", a.is_bg_shell_busy())

    row.refresh(a)
    row.bg_mark.click()           # same early-return path, same guarantee
    check("bg shell UI: clicking the sidebar gear is the same safe no-op",
          a.is_bg_shell_busy())

    card.detach(); card.close()
    row.deleteLater()
    a.deleteLater()


def test_bg_shell_extra_pids_and_kill_pid():
    """bg_shell_extra_pids() is what the kill menu lists, and
    kill_bg_shell_pid() is its per-item action -- letting the user kill one
    process at a time instead of the all-or-nothing kill_bg_shell_extras().
    kill_bg_shell_pid() must refuse anything that isn't CURRENTLY a genuine
    extra (re-checked, not trusted from a menu built a moment ago), and must
    only clear the latch once EVERY extra is gone, not on the first kill."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-pids-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellPids", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    kill_calls = []

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_count = agent.worker.job_process_count
    orig_ids = agent.worker.job_process_ids
    orig_pid = agent.worker.pid
    orig_kill_pid = agent.worker.kill_pid
    pids = [100, 101]   # root(100) + mcp bridge(101): the baseline
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: len(pids)
    agent.worker.job_process_ids = lambda: list(pids)
    agent.worker.pid = lambda: 100
    agent.worker.kill_pid = lambda p: (kill_calls.append(p),
                                       pids.remove(p) if p in pids else None)
    try:
        check("bg shell pids: nothing before a baseline is learned",
              agent.bg_shell_extra_pids() == [])

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell pids: still nothing once settled with no extras",
              agent.bg_shell_extra_pids() == [])

        pids.extend([102, 103])   # e.g. Gradle daemon + Kotlin daemon
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell pids: lists exactly the extras, not root/baseline",
              agent.bg_shell_extra_pids() == [102, 103],
              agent.bg_shell_extra_pids())

        check("bg shell pids: refuses to kill a baseline pid (not extra)",
              agent.kill_bg_shell_pid(101) is False and not kill_calls)
        check("bg shell pids: refuses to kill the agent's own root process",
              agent.kill_bg_shell_pid(100) is False and not kill_calls)

        ok = agent.kill_bg_shell_pid(102)
        check("bg shell pids: kills a genuine extra", ok is True)
        check("bg shell pids: the worker was asked to kill exactly that pid",
              kill_calls == [102], kill_calls)
        check("bg shell pids: one extra remains, so the badge stays lit",
              agent.is_bg_shell_busy() and agent.bg_shell_extra_pids() == [103])

        ok = agent.kill_bg_shell_pid(103)
        check("bg shell pids: kills the last remaining extra", ok is True)
        check("bg shell pids: the badge clears once EVERY extra is gone",
              not agent.is_bg_shell_busy())
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_count = orig_count
        agent.worker.job_process_ids = orig_ids
        agent.worker.pid = orig_pid
        agent.worker.kill_pid = orig_kill_pid


def test_no_em_dashes_in_visible_text():
    """No em dash reaches the reader. The app's visible strings (labels,
    tooltips, dialog copy, terminal notices, the board markdown) are checked by
    parsing every module and looking at string literals that are NOT
    docstrings; comments and docstrings are prose for us, not for the user, and
    are deliberately out of scope."""
    import ast

    root = Path(__file__).resolve().parent.parent
    targets = [root / "main.py"] + sorted((root / "app").rglob("*.py"))
    offenders = []
    for path in targets:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and "—" in node.value:
                offenders.append(f"{path.name}:{node.lineno}")
    check("text: no em dash in any user-visible string",
          not offenders, ", ".join(offenders[:8]))


def test_reveal_agent():
    """_reveal_agent (shared by the map + the dropdown) switches to the agent's
    workspace and finds its terminal card."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-reveal-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    pump(150)
    mgr = win.manager
    ws1 = mgr.workspaces[0]
    ws2 = mgr.create_workspace("Two", str(tmp))
    a2 = mgr.add_terminal(ws2.id, build_spec(AgentKind.CMD, "Agent X",
                                             cwd=str(tmp)), autostart=False)
    pump(120)
    mgr.set_active(ws1.id)
    pump(60)
    check("reveal: precondition active is ws1", mgr.active_id == ws1.id)
    win._reveal_agent(ws2.id, a2.id)
    pump(80)
    check("reveal: switched to the agent's workspace", mgr.active_id == ws2.id)
    check("reveal: the agent's card was located",
          win._pages[ws2.id].card_for(a2.id) is not None)
    win.close()


def test_new_agent_autofocus():
    """Opening a new agent (header '+', empty-slot '+', or Ctrl+Shift+T — all
    three funnel into _on_add_terminal_clicked) reveals its card right away:
    the workspace becomes active, the card scrolls into view, and keyboard
    focus lands in its terminal, so the user can start typing without an
    extra click. Uses pty=True (the same TerminalView path Claude agents use)
    since that's the case the feature targets."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QDialog
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets.main_window import AddTerminalDialog
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-autofocus-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    pump(150)
    mgr = win.manager
    ws1 = mgr.workspaces[0]
    ws2 = mgr.create_workspace("Two", str(tmp))
    pump(60)
    mgr.set_active(ws1.id)
    pump(60)
    check("autofocus: precondition active is ws1", mgr.active_id == ws1.id)

    # stand in for the modal "New Agent" dialog: accept immediately with a
    # lightweight interactive shell (no network/auth, unlike a Claude agent)
    orig_exec = AddTerminalDialog.exec
    orig_result_spec = AddTerminalDialog.result_spec
    AddTerminalDialog.exec = lambda self: QDialog.DialogCode.Accepted
    AddTerminalDialog.result_spec = lambda self, cwd="": build_spec(
        AgentKind.CMD, "AutoFocus", cwd=cwd, pty=True)
    try:
        win._on_add_terminal_clicked(ws2.id)
    finally:
        AddTerminalDialog.exec = orig_exec
        AddTerminalDialog.result_spec = orig_result_spec
    pump(200)

    agent = ws2.agents[-1]
    card = win._pages[ws2.id].card_for(agent.id)
    check("autofocus: switched to the new agent's workspace",
          mgr.active_id == ws2.id)
    check("autofocus: the new agent's card was located", card is not None)
    check("autofocus: the card is the focused card", win._focused_card is card)
    check("autofocus: keyboard focus landed in the terminal",
          card is not None and card.terminal is not None
          and card.terminal.hasFocus())
    win.close()


def test_agent_busy_activity():
    """is_busy() tracks OUTPUT ACTIVITY, not process-alive: an interactive
    agent idling at its prompt is running but NOT busy, so the sidebar badge
    can't falsely pulse green (the reported bug). Output marks it busy; a quiet
    spell — or exit — drops it back to standby."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Busy", cwd="."))
    a.status = AgentStatus.RUNNING  # pretend the process is up (no real child)
    check("busy: running but no output yet -> standby", not a.is_busy())
    a._on_pty_output("", "generating tokens...")
    check("busy: streaming output -> busy (working)", a.is_busy())
    a._on_idle_timeout()  # simulate the quiet window elapsing
    check("busy: output goes quiet -> back to standby", not a.is_busy())
    a._on_pty_output("", "more output")
    check("busy: output resumes -> busy again", a.is_busy())
    a._set_status(AgentStatus.EXITED_OK)  # exit clears busy at once
    check("busy: process exit clears busy immediately", not a.is_busy())
    a.dispose()


def test_reply_settle_skips_resume_replay():
    """A --resume launch replays the WHOLE past conversation as real output
    before it ever goes quiet, so the FIRST busy -> idle settle of a resumed
    launch is that replay finishing, not a fresh reply -- live-reported bug:
    reopening the app always showed the CURRENT time next to the last reply
    (both the header badge and an inline mark), never the actual historical
    one, because that replay settle stamped "now" unconditionally. Only that
    one settle is skipped; the very next one (a genuine new reply) stamps
    normally, and a non-resumed launch -- nothing to replay -- is never
    suppressed at all."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "ResumeSettle", cwd=".",
                                 pty=True))
    a.status = AgentStatus.RUNNING
    a._resume_attempt = True          # simulate a --resume launch
    a._settled_once = False

    # the resume replay arrives as real output, then falls quiet
    a._on_pty_output("pty", "...replayed conversation...")
    a._on_idle_timeout()
    check("reply-settle: a resume's replay settle mints no inline milestone",
          a.reply_marks() == [], a.reply_marks())
    check("reply-settle: ...but 'settled once' is now true", a._settled_once)

    # the NEXT settle is a genuine reply and stamps normally
    a._on_pty_output("pty", "a real new reply")
    before = time.time()
    a._on_idle_timeout()
    check("reply-settle: the settle AFTER the replay records one milestone",
          len(a.reply_marks()) == 1, a.reply_marks())
    check("reply-settle: ...stamped with a recent walltime",
          before - 1 <= a.reply_marks()[0].ts <= time.time() + 1)

    # a non-resumed launch has nothing to replay, so its first settle is real
    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "FreshSettle", cwd=".",
                                 pty=True))
    b.status = AgentStatus.RUNNING
    check("reply-settle: a fresh (non-resume) launch is never suppressed",
          not b._resume_attempt)
    b._on_pty_output("pty", "first reply ever")
    b._on_idle_timeout()
    check("reply-settle: ...so its first settle stamps immediately",
          len(b.reply_marks()) == 1, b.reply_marks())

    a.dispose()
    b.dispose()


def test_transcript_reply_times():
    """transcripts.reply_times reads when each reply ACTUALLY finished out of
    the conversation on disk -- the only source that survives a restart, since
    the live stamp is minted from the clock at a settle this process watched.

    The shape that matters: a turn narrates BETWEEN its tool calls, so only the
    LAST assistant text before the next real user turn is a finished reply, and
    a tool RESULT (a user record) sits inside a turn rather than ending one."""
    import datetime as _dt
    import json
    import tempfile

    from app import transcripts

    def rec(**kw):
        return json.dumps(kw)

    stamp = "2026-08-19T12:%02d:00.000Z"

    def at(minute):
        return _dt.datetime.fromisoformat(
            (stamp % minute).replace("Z", "+00:00")).timestamp()

    lines = [
        # turn 1: a prompt, a mid-turn narration, a tool call/result, the reply
        rec(type="user", timestamp=stamp % 0, promptSource="typed",
            message={"role": "user", "content": "do the thing"}),
        rec(type="assistant", timestamp=stamp % 1,
            message={"content": [{"type": "text", "text": "Looking now."}]}),
        rec(type="user", timestamp=stamp % 2,
            message={"content": [{"type": "tool_result", "content": "ok"}]}),
        rec(type="assistant", timestamp=stamp % 3,
            message={"content": [{"type": "text", "text": "Done, all green."}]}),
        # a sub-agent's own turn must never count as this conversation's reply
        rec(type="assistant", timestamp=stamp % 4, isSidechain=True,
            message={"content": [{"type": "text", "text": "sidechain noise"}]}),
        # turn 2
        rec(type="user", timestamp=stamp % 5, promptSource="typed",
            message={"role": "user", "content": "and again"}),
        rec(type="assistant", timestamp=stamp % 6,
            message={"content": [{"type": "text", "text": "Second reply."}]}),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "conv.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        got = transcripts._read_reply_times(path)

    check("reply-times: one entry per FINISHED reply, not per assistant text",
          len(got) == 2, got)
    check("reply-times: a mid-turn narration is not a reply ending",
          [t for _, t in got] == ["Done, all green.", "Second reply."], got)
    check("reply-times: each carries the record's OWN timestamp",
          [w for w, _ in got] == [at(3), at(6)], got)
    check("reply-times: a tool result does not end a turn",
          got[0][0] == at(3), got[0])
    check("reply-times: a sidechain turn is skipped",
          all("sidechain" not in t for _, t in got), got)

    # a missing / unreadable transcript is "" rather than an exception
    check("reply-times: no transcript -> empty, never raises",
          transcripts.reply_times("C:/nope", "no-such-id") == [])


def test_ansi():
    from app.ansi_parser import AnsiSgrParser

    p = AnsiSgrParser()
    segs = p.feed("\x1b[31mred\x1b[0m plain")
    check("ansi: SGR color applied",
          len(segs) == 2 and segs[0][0].fg is not None and segs[1][0].fg is None,
          segs)

    p = AnsiSgrParser()
    a = p.feed("start\x1b[3")      # escape split across chunks
    b = p.feed("2mgreen")
    check("ansi: partial escape carried across chunks",
          "".join(t for _, t in a) == "start"
          and "".join(t for _, t in b) == "green"
          and b[0][0].fg is not None, (a, b))

    p = AnsiSgrParser()
    segs = p.feed("\x1b]0;window title\x07visible")
    check("ansi: OSC stripped", "".join(t for _, t in segs) == "visible", segs)

    p = AnsiSgrParser()
    p.feed("\x1b[38;5;196m")
    segs = p.feed("bright")
    check("ansi: 256-color mode", segs and segs[0][0].fg == "#ff0000", segs)

    # regression: an unterminated OSC must not swallow the stream forever
    p = AnsiSgrParser()
    p.feed("\x1b]0;title-that-never-terminates")
    for _ in range(30):
        p.feed("x" * 500)  # would grow the carry unboundedly if uncapped
    segs = p.feed("recovered")
    check("ansi: unterminated OSC recovers (carry capped)",
          any("recovered" in t for _, t in segs), segs[-1:])


# ------------------------------------------------------------ application ---

def test_app():
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtCore import QProcess
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.tiling import compute_grid
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    check("app: stylesheet applied", "TerminalCard" in app.styleSheet())

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_until(pred, timeout_ms=10000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    def snap(widget, name):
        widget.grab().save(str(SHOTS / f"{name}.png"))

    # isolated session dir + two project dirs for the cwd checks
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-smoke-"))
    proj_alpha = tmp / "proj-alpha"
    proj_bravo = tmp / "proj-bravo"
    proj_alpha.mkdir()
    proj_bravo.mkdir()
    store = SessionStore(path=tmp / "session.json")

    # -- 1. boot ----------------------------------------------------------
    win = create_main_window(store)
    win.resize(1600, 900)
    win.show()
    pump(200)
    mgr = win.manager
    check("boot: window shown", win.isVisible())
    check("boot: first-run default workspace exists", len(mgr.workspaces) == 1)
    check("boot: first-run default agent is idle",
          len(mgr.workspaces[0].agents) == 1
          and not mgr.workspaces[0].agents[0].is_running())
    snap(win, "01_boot")

    # -- 3. workspaces (model API + real widget wiring) --------------------
    alpha = mgr.workspaces[0]
    mgr.rename_workspace(alpha.id, "Alpha")
    alpha.project_path = str(proj_alpha)
    bravo = mgr.create_workspace("Beta", str(proj_bravo))
    pump(100)
    check("ws: sidebar has 2 rows", len(win.sidebar._rows) == 2)
    check("ws: rename reflected in sidebar",
          win.sidebar._rows[alpha.id][1].name_label.text() == "Alpha")
    mgr.rename_workspace(bravo.id, "Bravo")
    pump(50)
    check("ws: second rename reflected",
          win.sidebar._rows[bravo.id][1].name_label.text() == "Bravo")
    check("ws: Alpha row is active",
          win.sidebar._rows[alpha.id][1].property("active") is True
          and win.sidebar._rows[bravo.id][1].property("active") is False)

    # remove the default idle agent so the tiling count starts at 0
    mgr.remove_terminal(alpha.id, alpha.agents[0].id)
    pump(100)
    page = win._pages[alpha.id]
    check("ws: empty state visible with 0 terminals", page.empty.isVisible())
    snap(win, "02_two_workspaces")

    # -- 4. incremental tiling 1..6 against the applied layout -------------
    def assert_layout(page, n):
        plan = compute_grid(n)
        ok = len(page.cards) == n
        detail = ""
        for i, card in enumerate(page.cards):
            got = page.grid.getItemPosition(page.grid.indexOf(card))
            want = tuple(plan.cells[i])
            if got != want:
                ok = False
                detail = f"card {i}: got {got}, want {want}"
                break
        for c in range(plan.vcols, page.grid.columnCount()):
            if page.grid.columnStretch(c) != 0:
                ok = False
                detail = f"stale column stretch at col {c}"
        for r in range(plan.rows, page.grid.rowCount()):
            if page.grid.rowStretch(r) != 0:
                ok = False
                detail = f"stale row stretch at row {r}"
        check(f"tiling applied: n={n}", ok, detail)

    idle_ids = []
    for n in range(1, 7):
        agent = mgr.add_terminal(
            alpha.id, build_spec(AgentKind.CMD, f"Tile {n}",
                                 cwd=str(proj_alpha)), autostart=False)
        idle_ids.append(agent.id)
        pump(30)
        assert_layout(page, n)
        if n in (1, 3, 5, 6):
            snap(win, f"03_tiles_{n}")
    check("tiling: sidebar stats show 6 total",
          mgr.workspace_stats(alpha.id)["total"] == 6)

    # shrink 6 -> 3 exercises the stale-stretch reset (vcols 3 -> 2)
    for agent_id in idle_ids[3:]:
        mgr.remove_terminal(alpha.id, agent_id)
    pump(50)
    assert_layout(page, 3)
    snap(win, "04_shrink_3")

    # -- 4b. maximize / restore a single card (solo view) ------------------
    # solo is a transient VIEW toggle: it must never persist or touch siblings'
    # processes, and restoring must reproduce the prior tiling verbatim. Every
    # branch below leaves the original 3 idle cards intact for section 5.
    solo_dirty = {"n": 0}
    _solo_conn = mgr.dirty.connect(
        lambda: solo_dirty.__setitem__("n", solo_dirty["n"] + 1))
    page.toggle_solo(page.cards[0])
    pump(30)
    max_pos = page.grid.getItemPosition(page.grid.indexOf(page.cards[0]))
    check("maximize: soloed card spans full grid at (0,0)",
          page.cards[0].isVisible() and max_pos == (0, 0, 1, 1), max_pos)
    check("maximize: sibling cards hidden (processes untouched)",
          not page.cards[1].isVisible() and not page.cards[2].isVisible())
    check("maximize: button shows Restore glyph",
          page.cards[0].btn_max.text() == "⤡")
    check("maximize: no stale row/col stretch while soloed",
          page.grid.columnStretch(1) == 0 and page.grid.rowStretch(1) == 0)
    snap(win, "04b_maximized")

    page.toggle_solo(page.cards[0])   # restore
    pump(30)
    assert_layout(page, 3)            # exact prior arrangement reproduced
    check("restore: all cards visible again",
          all(c.isVisible() for c in page.cards))
    check("restore: button shows Maximize glyph",
          page.cards[0].btn_max.text() == "⤢")
    check("maximize: solo toggling never marks the session dirty",
          solo_dirty["n"] == 0, solo_dirty["n"])
    mgr.dirty.disconnect(_solo_conn)

    # adding an agent while maximized exits solo so the new card is visible
    page.toggle_solo(page.cards[0])
    pump(20)
    solo_tmp = mgr.add_terminal(
        alpha.id, build_spec(AgentKind.CMD, "Solo Tmp",
                             cwd=str(proj_alpha)), autostart=False)
    pump(30)
    tmp_card = page.card_for(solo_tmp.id)
    check("maximize: adding an agent exits solo",
          page._solo_card is None and tmp_card.isVisible())

    # closing the maximized card auto-restores (never strands a blank area);
    # close the throwaway card so section 5 still sees the original 3 idle cards
    page.toggle_solo(tmp_card)
    pump(20)
    mgr.remove_terminal(alpha.id, solo_tmp.id)
    pump(30)
    check("maximize: closing the soloed card exits solo",
          page._solo_card is None)
    assert_layout(page, 3)

    # -- 4c. context-usage badge renders on the card header ----------------
    tok_card = page.cards[0]
    check("tokens: card badge hidden before any usage",
          not tok_card.token_label.isVisible())
    tok_card.agent.set_token_usage(200_000, 1_000_000)
    pump(20)
    check("tokens: card badge shows the percentage",
          tok_card.token_label.isVisible()
          and tok_card.token_label.text() == "20% of 1M",
          tok_card.token_label.text())
    tok_card.agent.set_token_usage(0, 0)   # reset so nothing leaks downstream
    pump(20)
    check("tokens: card badge hides again when usage clears",
          not tok_card.token_label.isVisible())

    # -- 5. live streaming --------------------------------------------------
    ticker = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Ticker", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c",
              "import time\nfor i in range(2000): print(f'tick {i}', flush=True); time.sleep(0.05)"]))
    ticker_card = page.card_for(ticker.id)
    check("stream: live output reaches console",
          wait_until(lambda: "tick 5" in ticker_card.console.toPlainText(), 15000),
          ascii(ticker_card.console.toPlainText()[-200:]))
    snap(win, "05_streaming")

    # -- 6. stdin round-trip via the real input line ------------------------
    echo = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Echo", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c",
              "import sys\nfor line in sys.stdin: print('echo:'+line.strip(), flush=True)"]))
    echo_card = page.card_for(echo.id)
    wait_until(lambda: echo.is_running(), 8000)
    echo_card.input.setText("hello hive")
    QTest.keyClick(echo_card.input, Qt.Key.Key_Return)
    check("stdin: round-trip through input line",
          wait_until(lambda: "echo:hello hive" in echo_card.console.toPlainText(),
                     10000),
          ascii(echo_card.console.toPlainText()[-200:]))
    check("stdin: input line cleared after submit", echo_card.input.text() == "")
    check("stdin: local echo rendered",
          "> hello hive" in echo_card.console.toPlainText())

    # -- 7. terminal cwd == workspace project folder ------------------------
    cwd_agent = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Cwd", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c", "import os; print('CWD=' + os.getcwd(), flush=True)"]))
    cwd_card = page.card_for(cwd_agent.id)
    check("cwd: terminal opens in the workspace project folder",
          wait_until(lambda: f"CWD={proj_alpha}" in cwd_card.console.toPlainText(),
                     10000),
          ascii(cwd_card.console.toPlainText()[-200:]))
    mgr.remove_terminal(alpha.id, cwd_agent.id)
    mgr.remove_terminal(alpha.id, echo.id)
    pump(100)
    assert_layout(page, 4)  # 3 idle + ticker

    # -- 8. background retention across workspace switch --------------------
    bravo_row = win.sidebar._rows[bravo.id][1]
    QTest.mouseClick(bravo_row, Qt.MouseButton.LeftButton)  # real click
    pump(100)
    check("retention: Bravo is active after row click",
          mgr.active_id == bravo.id)
    check("retention: ticker card hidden", not ticker_card.isVisible())
    before = len(ticker_card.console.toPlainText())
    pump(1500)
    after = len(ticker_card.console.toPlainText())
    check("retention: hidden terminal kept streaming",
          after > before and not ticker_card.isVisible(),
          f"before={before} after={after}")
    check("retention: process still running while hidden", ticker.is_running())
    QTest.mouseClick(win.sidebar._rows[alpha.id][1], Qt.MouseButton.LeftButton)
    pump(100)
    check("retention: back to Alpha", mgr.active_id == alpha.id)
    snap(win, "06_after_switch")

    # -- 8b. review-fix regressions -----------------------------------------
    # rename: Escape must cancel, not commit
    alpha_row = win.sidebar._rows[alpha.id][1]
    alpha_row.start_rename()
    alpha_row.rename_edit.setText("ShouldNotStick")
    QTest.keyClick(alpha_row.rename_edit, Qt.Key.Key_Escape)
    pump(50)
    check("rename: Escape cancels instead of committing",
          mgr.workspace(alpha.id).name == "Alpha"
          and alpha_row.name_label.text() == "Alpha",
          mgr.workspace(alpha.id).name)
    # rename: Enter commits exactly once
    alpha_row.start_rename()
    alpha_row.rename_edit.setText("AlphaPrime")
    QTest.keyClick(alpha_row.rename_edit, Qt.Key.Key_Return)
    pump(50)
    check("rename: Enter commits", mgr.workspace(alpha.id).name == "AlphaPrime")
    mgr.rename_workspace(alpha.id, "Alpha")
    pump(50)

    # card header: inline agent-name rename (mirrors the workspace-row UX)
    rc = page.cards[0]
    orig_name = rc.agent.spec.name
    orig_role = rc.agent.spec.role
    QTest.mouseDClick(rc.title, Qt.MouseButton.LeftButton)
    check("card rename: double-click opens the editor",
          rc._renaming and rc.title_edit.isVisible())
    # Escape cancels without changing the name
    rc.title_edit.setText("NopeName")
    QTest.keyClick(rc.title_edit, Qt.Key.Key_Escape)
    pump(50)
    check("card rename: Escape cancels",
          not rc._renaming and rc.agent.spec.name == orig_name
          and rc.title.text() == orig_name, rc.agent.spec.name)
    # Enter commits: set_name updates the name+title, role stays, name is custom
    rc.start_rename()
    rc.title_edit.setText("MyCoder")
    QTest.keyClick(rc.title_edit, Qt.Key.Key_Return)
    pump(50)
    check("card rename: Enter commits the new name",
          rc.agent.spec.name == "MyCoder" and rc.title.text() == "MyCoder"
          and rc.agent.spec.role == orig_role and rc.agent.spec.custom_name,
          f"name={rc.agent.spec.name} role={rc.agent.spec.role}")

    # task summary: the header shows what the agent is working on (its task) to
    # the right of the name, so several agents are tellable apart at a glance
    long_task = "Refactor the authentication module and add integration tests"
    rc.agent.set_task(long_task)
    pump(20)
    check("task summary: header shows the current task",
          rc.task_summary.isVisible()
          and rc.task_summary.toolTip() == long_task
          and rc.task_summary.text().startswith("Refactor"),
          rc.task_summary.text())
    rc.agent.set_task("")
    pump(20)
    # the label stays in the layout (it carries the header's stretch, keeping
    # the name left-aligned) but shows NO text when there is no task
    check("task summary: blank when there is no task",
          rc.task_summary.text() == "", rc.task_summary.text())

    # history: draft survives an accidental Up
    any_card = page.cards[0]
    any_card._history = ["old command"]
    any_card._hist_idx = 1
    any_card.input.setText("my draft")
    QTest.keyClick(any_card.input, Qt.Key.Key_Up)
    check("history: Up recalls previous", any_card.input.text() == "old command")
    QTest.keyClick(any_card.input, Qt.Key.Key_Down)
    check("history: Down restores draft", any_card.input.text() == "my draft")
    any_card.input.clear()

    # TTY-only commands get a local hint; cls/clear clears the console
    any_card.agent.send_command("claude")
    check("hint: bare claude explains the line-mode limitation",
          "can't run here" in any_card.console.toPlainText()
          and 'Claude Code (interactive)' in any_card.console.toPlainText())
    any_card.agent.send_command('claude -p "hello"')
    check("hint: claude -p passes without the hint",
          any_card.console.toPlainText().count("can't run here") == 1)
    any_card.agent.send_command("vim notes.txt")
    check("hint: vim flagged as full-screen app",
          "full-screen terminal app" in any_card.console.toPlainText())
    any_card.agent.send_command("cls")
    check("clear: cls empties the console locally",
          any_card.console.toPlainText() == ""
          and len(any_card.agent.log) == 0)

    # session dict carries per-terminal run state (lazy-restore correctness)
    payload = mgr.to_session_dict()
    alpha_terms = next(w for w in payload["workspaces"]
                       if w["id"] == alpha.id)["terminals"]
    check("session: per-terminal run state persisted",
          any(t["running"] for t in alpha_terms)
          and any(not t["running"] for t in alpha_terms), alpha_terms)

    # per-workspace numbering: seeded from THIS workspace's agents, and each
    # workspace counts independently (Feature 1)
    mgr.add_terminal(alpha.id, build_spec(AgentKind.CMD, "Agent 7",
                                          cwd=str(proj_alpha)), autostart=False)
    check("naming: proposal seeded from existing agents",
          mgr.next_agent_name(alpha.id) == "Agent 8", mgr.next_agent_name(alpha.id))
    check("naming: other workspace resets to Agent 1",
          mgr.next_agent_name(bravo.id) == "Agent 1", mgr.next_agent_name(bravo.id))
    mgr.remove_terminal(alpha.id, alpha.agents[-1].id)
    pump(50)

    # -- 9. close a card via its real ✕ button ------------------------------
    proc = ticker.worker.process()
    QTest.mouseClick(page.card_for(ticker.id).btn_close,
                     Qt.MouseButton.LeftButton)
    check("close: process terminated",
          wait_until(lambda: proc.state() == QProcess.ProcessState.NotRunning,
                     8000))
    pump(100)
    check("close: card removed from page",
          page.card_for(ticker.id) is None and len(page.cards) == 3)
    assert_layout(page, 3)
    snap(win, "07_after_close")

    # -- 10. clean shutdown: no zombies, session saved -----------------------
    zombie = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CMD, "Zombie", cwd=str(proj_alpha)))
    wait_until(lambda: zombie.is_running(), 10000)
    zombie.send_command("ping -t 127.0.0.1")
    wait_until(lambda: "Reply" in page.card_for(zombie.id).console.toPlainText()
               or "127.0.0.1" in page.card_for(zombie.id).console.toPlainText(),
               10000)
    all_agents = mgr.all_agents()
    win.close()
    procs = [a.worker.process() for a in all_agents if a.worker.process()]
    check("shutdown: every child process terminated",
          wait_until(lambda: all(
              p.state() == QProcess.ProcessState.NotRunning for p in procs), 6000))
    pump(300)
    ping = subprocess.run(["tasklist", "/FI", "IMAGENAME eq PING.EXE"],
                          capture_output=True, text=True)
    check("shutdown: no orphan PING.EXE (job tree kill)",
          "PING.EXE" not in ping.stdout)
    check("shutdown: session persisted",
          (tmp / "session.json").exists()
          and "Bravo" in (tmp / "session.json").read_text(encoding="utf-8"))

    # -- 11. restore round-trip: lazy start honors saved run state ----------
    win2 = create_main_window(store)
    win2.resize(1600, 900)
    win2.show()
    pump(150)
    mgr2 = win2.manager
    alpha2 = next(w for w in mgr2.workspaces if w.name == "Alpha")
    running_flags = [a.autostart_on_restore for a in alpha2.agents]
    check("restore: run state loaded per agent",
          any(running_flags) and not all(running_flags), running_flags)
    win2.autostart_active_workspace()
    to_start = [a for a in alpha2.agents if a.autostart_on_restore]
    to_stay = [a for a in alpha2.agents if not a.autostart_on_restore]
    check("restore: previously-running agents autostart",
          wait_until(lambda: all(a.is_running() for a in to_start), 15000))
    pump(500)
    check("restore: stopped agents stay idle (no side-effect re-runs)",
          all(not a.is_running() for a in to_stay))
    win2.close()
    procs2 = [a.worker.process() for a in mgr2.all_agents()
              if a.worker.process()]
    check("restore: second shutdown clean",
          wait_until(lambda: all(
              p.state() == QProcess.ProcessState.NotRunning for p in procs2),
              6000))
    pump(200)

    shutil.rmtree(tmp, ignore_errors=True)


def test_terminal_keys():
    """Focused terminal maps keys to the right VT sequences (Shift+Tab etc.)."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    view = TerminalView(rows=24, cols=80)

    K = Qt.Key
    cases = {
        # (key, ctrl, shift, alt, text) -> expected VT bytes
        "Shift+Tab cycles modes (CSI Z)": ((K.Key_Backtab, False, True, False, ""), "\x1b[Z"),
        "Tab completes": ((K.Key_Tab, False, False, False, "\t"), "\t"),
        "Ctrl+C interrupts": ((K.Key_C, True, False, False, ""), "\x03"),
        "Ctrl+R reverse-search": ((K.Key_R, True, False, False, ""), "\x12"),
        "Ctrl+D EOF": ((K.Key_D, True, False, False, ""), "\x04"),
        "Escape": ((K.Key_Escape, False, False, False, ""), "\x1b"),
        "Enter submits": ((K.Key_Return, False, False, False, "\r"), "\r"),
        "Shift+Enter newlines": ((K.Key_Return, False, True, False, "\r"), "\x1b\r"),
        "Ctrl+Enter newlines": ((K.Key_Return, True, False, False, ""), "\n"),
        "Up arrow": ((K.Key_Up, False, False, False, ""), "\x1b[A"),
        "Ctrl+Left word-jump": ((K.Key_Left, True, False, False, ""), "\x1b[1;5D"),
        "Alt+key sends meta": ((K.Key_B, False, False, True, "b"), "\x1bb"),
        "plain letter": ((K.Key_A, False, False, False, "a"), "a"),
    }
    for name, (args, expected) in cases.items():
        got = view._sequence_for(*args)
        check(f"keys: {name}", got == expected, f"got {got!r}, want {expected!r}")

    check("keys: terminal refuses focus traversal (owns Tab)",
          view.focusNextPrevChild(True) is False)

    # Windows-editor shortcuts: Ctrl+A highlights the input line (Backspace/Del
    # clears it), Ctrl+Shift+A selects all, Ctrl+C smart-copies, and Ctrl+C with
    # no selection still sends the interrupt.
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QGuiApplication, QKeyEvent

    def press(k, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, ""))

    # Claude's input box opens with a '>' prompt, and transcript can butt right
    # against it (no blank line). Ctrl+A must stop AT the prompt row, never
    # climb into the transcript above -- the over-highlight regression.
    view.feed("recap line one\r\nrecap line two\r\n> my prompt")
    sent = []
    view.keyInput.connect(sent.append)
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A highlights the input line",
          view.selected_text() == "> my prompt", view.selected_text())
    check("keys: Ctrl+A stops at the '>' prompt, not the transcript above",
          "recap" not in view.selected_text(), view.selected_text())
    check("keys: Ctrl+A is visual-only (nothing to the pty)", sent == [], sent)
    press(K.Key_Backspace)
    check("keys: Backspace on the highlight clears input via double-Esc",
          sent == ["\x1b\x1b"], sent)
    check("keys: clearing drops the highlight", view.selected_text() == "")
    sent.clear()
    press(K.Key_Backspace)
    check("keys: plain Backspace (no highlight) forwards to the child (0x7f)",
          sent == ["\x7f"], sent)

    # Typing a printable character over the Ctrl+A highlight REPLACES the input,
    # like any editor: the child's prompt is cleared (double-Esc) THEN the typed
    # character is sent, so it becomes the fresh input rather than appending.
    def type_char(ch):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, K.Key_X,
                                     Qt.KeyboardModifier.NoModifier, ch))
    view.feed("recap line one\r\n> my prompt")
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A re-highlights the input", view.selected_text() == "> my prompt")
    sent.clear()
    type_char("x")
    check("keys: typing over the highlight clears then sends the char",
          sent == ["\x1b\x1b", "x"], sent)
    check("keys: typing over the highlight drops the highlight",
          view.selected_text() == "")

    # a navigation key over the highlight only collapses it -- no clear, no char
    press(K.Key_A, ctrl=True)
    sent.clear()
    press(K.Key_Left)
    check("keys: arrow over the highlight collapses it without clearing",
          "\x1b\x1b" not in sent and view.selected_text() == "", sent)

    # Ctrl+C over the Ctrl+A highlight COPIES the input rather than falling
    # through to the 0x03 interrupt (which cleared the child's input -- the
    # reported "Ctrl+C deletes my text instead of copying" bug).
    view.feed("recap\r\n> copy me")
    press(K.Key_A, ctrl=True)
    check("keys: Ctrl+A re-highlights for the copy case",
          view.selected_text() == "> copy me", view.selected_text())
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C over the highlight copies it",
          QGuiApplication.clipboard().text() == "> copy me",
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+C over the highlight sends NO interrupt (no 0x03)",
          "\x03" not in sent, sent)
    check("keys: Ctrl+C over the highlight drops the highlight",
          view.selected_text() == "")

    # Ctrl+X over the highlight copies too, THEN clears the child's input (the
    # double-Esc clear-prompt gesture) -- a cut.
    view.feed("recap\r\n> cut me")
    press(K.Key_A, ctrl=True)
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_X, ctrl=True)
    check("keys: Ctrl+X over the highlight copies it",
          QGuiApplication.clipboard().text() == "> cut me",
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+X over the highlight clears input (double-Esc)",
          sent == ["\x1b\x1b"], sent)

    # a multi-line prompt (the '>' first line + continuation) highlights in full
    # but still excludes the output above it
    mv = TerminalView(rows=24, cols=80)
    mv.feed("output above\r\n> line one\r\nline two")
    mv.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, K.Key_A,
                               Qt.KeyboardModifier.ControlModifier, ""))
    msel = mv.selected_text()
    check("keys: Ctrl+A spans a multi-line prompt",
          "line one" in msel and "line two" in msel and "\n" in msel, msel)
    check("keys: multi-line Ctrl+A excludes the output above",
          "output above" not in msel, msel)

    # Ctrl+Shift+A selects all; Ctrl+C then copies it and sends nothing to pty
    sent.clear()
    press(K.Key_A, ctrl=True, shift=True)
    check("keys: Ctrl+Shift+A selects all (incl. transcript)",
          "recap line one" in view.selected_text(), view.selected_text())
    check("keys: Ctrl+Shift+A sends nothing to the pty", sent == [], sent)
    sent.clear()
    QGuiApplication.clipboard().clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C copies the selection",
          "recap line one" in QGuiApplication.clipboard().text(),
          QGuiApplication.clipboard().text())
    check("keys: Ctrl+C-copy sends nothing to the pty", sent == [], sent)
    check("keys: Ctrl+C clears selection so next Ctrl+C interrupts",
          view.selected_text() == "")

    # with the selection gone, Ctrl+C is the interrupt again
    sent.clear()
    press(K.Key_C, ctrl=True)
    check("keys: Ctrl+C with no selection interrupts (0x03)",
          sent == ["\x03"], sent)


def test_terminal_image_paste():
    """A clipboard image is spilled to a temp PNG and its path is pasted.

    Native-Windows Claude can't take a raw clipboard image; it reads images by
    path. So pasting an image must write a file and paste the path, while a
    text clipboard still pastes text unchanged."""
    import os

    from PySide6.QtGui import QGuiApplication, QImage
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=40)

    img = QImage(4, 4, QImage.Format.Format_RGB32)
    img.fill(0xFFFF0000)
    QGuiApplication.clipboard().setImage(img)
    if not QGuiApplication.clipboard().mimeData().hasImage():
        check("image-paste: SKIP (offscreen clipboard has no image support)", True)
        return

    sent = []
    v.keyInput.connect(sent.append)
    v.paste_clipboard()
    check("image-paste: emitted exactly one paste", len(sent) == 1, sent)
    pasted = sent[0].strip('"') if sent else ""
    check("image-paste: pasted a .png path", pasted.lower().endswith(".png"), pasted)
    check("image-paste: the PNG was actually written on disk",
          os.path.isfile(pasted), pasted)
    check("image-paste: file lands in the aihive-paste temp dir",
          "aihive-paste" in pasted, pasted)

    # a text clipboard still pastes text, never a file path
    QGuiApplication.clipboard().setText("plain text")
    sent.clear()
    v.paste_clipboard()
    check("image-paste: text clipboard still pastes text",
          sent == ["plain text"], sent)

    try:
        os.remove(pasted)
    except OSError:
        pass


def test_terminal_mouse_words_links():
    """Double-click selects a word; middle-click classifies URLs / files."""
    import os

    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=80)
    v.feed("the quick brown fox")

    # word run is whitespace-delimited; a blank cell has no word
    check("mouse: _word_at finds the word range", v._word_at(0, 11) == (10, 14),
          v._word_at(0, 11))
    check("mouse: _word_at on a space is None", v._word_at(0, 9) is None)

    # a real double-click event selects that word, ready for Ctrl+C
    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * v._cell_w,
                       CELL_PAD_Y + (row + 0.5) * v._cell_h)
    v.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, pos(0, 11),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("mouse: double-click selects the word", v.selected_text() == "brown",
          v.selected_text())

    # link classification
    check("mouse: https URL classified", v._classify_link("https://example.com/x")
          == ("url", "https://example.com/x"))
    check("mouse: www URL gets an http scheme",
          v._classify_link("www.example.com/y") == ("url", "http://www.example.com/y"))
    check("mouse: wrapping paren/comma stripped from a URL",
          v._classify_link("(https://example.com),")[1] == "https://example.com")
    check("mouse: a bare word is not a link", v._classify_link("brown") is None)
    check("mouse: a relative path is not opened (no base dir)",
          v._classify_link("app/foo.py") is None)

    this = os.path.abspath(__file__)
    check("mouse: an existing absolute file is classified",
          v._classify_link(this) == ("file", this), v._classify_link(this))
    check("mouse: a file:line[:col] suffix is stripped",
          v._classify_link(this + ":42:7") == ("file", this))
    check("mouse: a nonexistent absolute path is None",
          v._classify_link(os.path.join(os.path.dirname(this), "nope_xyz.zzz")) is None)

    # _link_at reads the token straight off the painted line
    v2 = TerminalView(rows=6, cols=80)
    v2.feed("see https://example.com/docs for details")
    v2.grab()   # paint once so the full-screen link scan caches the spans
    check("mouse: _link_at picks up the URL under the pointer",
          v2._link_at(0, 8) == ("url", "https://example.com/docs"), v2._link_at(0, 8))

    # every link is underlined (scanned up front); hover reads the cached span
    check("mouse: _span_at spans the scanned URL",
          v2._span_at(0, 8) == (4, 27), v2._span_at(0, 8))
    check("mouse: _span_at is None over plain text",
          v2._span_at(0, 30) is None, v2._span_at(0, 30))

    def move(view, row, col):
        from PySide6.QtCore import QEvent, QPointF
        from PySide6.QtGui import QMouseEvent
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        view.mouseMoveEvent(QMouseEvent(
            QEvent.Type.MouseMove, p, Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))

    move(v2, 0, 8)  # over the URL
    check("mouse: hovering a link sets the hover span",
          v2._hover_link == (0, 4, 27), v2._hover_link)
    check("mouse: hovering a link shows the hand cursor",
          v2.cursor().shape() == Qt.CursorShape.PointingHandCursor)
    move(v2, 0, 30)  # over plain text
    check("mouse: leaving a link clears the hover", v2._hover_link is None)
    check("mouse: cursor reverts to I-beam off a link",
          v2.cursor().shape() == Qt.CursorShape.IBeamCursor)

    # Ctrl+left-click a link opens it (route to _open_target, without actually
    # launching a browser); a plain left-click selects instead of opening
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    opened = []
    v2._open_target = opened.append

    def click(row, col, ctrl=False, button=Qt.MouseButton.LeftButton):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v2._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v2._cell_h)
        mods = (Qt.KeyboardModifier.ControlModifier if ctrl
                else Qt.KeyboardModifier.NoModifier)
        v2.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, p,
                                       button, button, mods))

    click(0, 8, ctrl=True)  # Ctrl+left-click ON the URL
    check("mouse: Ctrl+click opens the link under the pointer",
          opened == [("url", "https://example.com/docs")], opened)
    opened.clear()
    click(0, 8, ctrl=False)  # plain left-click does NOT open
    check("mouse: plain left-click does not open a link", opened == [], opened)
    opened.clear()
    click(0, 30, ctrl=True)  # Ctrl+click on plain text -> nothing to open
    check("mouse: Ctrl+click off a link opens nothing", opened == [], opened)
    opened.clear()
    click(0, 8, button=Qt.MouseButton.MiddleButton)  # middle-click still works
    check("mouse: middle-click still opens the link",
          opened == [("url", "https://example.com/docs")], opened)

    # plain left-click places the caret: send Left/Right arrows to the child so
    # its input cursor lands on the clicked column (exact on the caret's line)
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("hello")                      # child caret ends at column 5, row 0
    moves = []
    v3.keyInput.connect(moves.append)

    def caret_click(row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v3._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v3._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        v3.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v3.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    caret_click(0, 2)   # 3 columns left of the caret
    check("mouse: click left of the caret sends Left arrows",
          moves == ["\x1b[D" * 3], moves)
    moves.clear()
    caret_click(0, 7)   # 2 columns right of the caret (pty is inert in-test)
    check("mouse: click right of the caret sends Right arrows",
          moves == ["\x1b[C" * 2], moves)
    moves.clear()
    caret_click(3, 4)   # a row that isn't the caret's line
    check("mouse: click off the caret's line never nudges it", moves == [], moves)
    moves.clear()
    caret_click(0, 5)   # exactly on the caret -> nothing to send
    check("mouse: click on the caret sends nothing", moves == [], moves)

    # Multi-line prompt: clicking a DIFFERENT line of the input box repositions
    # the caret best-effort -- Up/Down to the row, then Left/Right to the
    # predicted landing column. Previously any off-caret-row click did nothing.
    v4 = TerminalView(rows=8, cols=80)
    v4.feed("> line one\r\nline two")   # prompt row 0, caret row 1 col 8
    m2 = []
    v4.keyInput.connect(m2.append)

    def caret_click4(row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * v4._cell_w,
                    CELL_PAD_Y + (row + 0.5) * v4._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        v4.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v4.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    caret_click4(0, 2)   # up one row (caret col 8 -> land col 8), left to col 2
    check("mouse: click a line above moves up then left",
          m2 == ["\x1b[A", "\x1b[D" * 6], m2)
    m2.clear()
    caret_click4(0, 40)  # past the row's end -> clamp to line end (col 10)
    check("mouse: click past a line's end clamps to its content",
          m2 == ["\x1b[A", "\x1b[C" * 2], m2)
    m2.clear()
    caret_click4(6, 4)   # a row outside the input box -> untouched
    check("mouse: click outside the input box never nudges the caret",
          m2 == [], m2)
    m2.clear()
    caret_click4(1, 3)   # the caret's own row is still exact (col 8 -> 3)
    check("mouse: click on the caret's own multi-line row stays exact",
          m2 == ["\x1b[D" * 5], m2)

    # moving DOWN a row: put the child's caret on the top line (feed real CUU),
    # then click a lower line of a 3-line prompt.
    v5 = TerminalView(rows=8, cols=80)
    v5.feed("> a\r\nbb\r\nccc\x1b[2A")   # 3 lines, caret pushed up to row 0
    m3 = []
    v5.keyInput.connect(m3.append)
    p = QPointF(CELL_PAD_X + 1.5 * v5._cell_w, CELL_PAD_Y + 2.5 * v5._cell_h)
    a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
         Qt.KeyboardModifier.NoModifier)
    v5.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
    v5.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))
    check("mouse: click a line below moves down then corrects the column",
          m3 == ["\x1b[B" * 2, "\x1b[D" * 2], m3)


def test_terminal_mouse_tracking_click():
    """When the child app requested mouse tracking (Claude Code holds it on for
    its whole session), a stationary left CLICK is forwarded as a mouse report so
    the app selects the option under the pointer -- NOT swallowed for local caret
    repositioning, which injected stray arrows into modal menus (AskUserQuestion
    / plan approval) and made the question vanish unanswered. But a DRAG (or a
    double-click) is a local text SELECTION and is never forwarded, so the
    transcript stays copyable even while the child holds mouse tracking on."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=8, cols=80)
    # the app enables mouse tracking + SGR encoding (exactly what Claude does)
    v.feed("\x1b[?1000h\x1b[?1006h")
    # put selectable text on screen row 3 (1-based row 4): "hello world"
    v.feed("\x1b[4;1Hhello world")
    check("mouse-track: DECSET turned on tracking", v._mouse_tracking)
    check("mouse-track: DECSET turned on SGR encoding", v._mouse_sgr)

    out = []
    v.keyInput.connect(out.append)

    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * v._cell_w,
                       CELL_PAD_Y + (row + 0.5) * v._cell_h)

    def click(row, col, mods=Qt.KeyboardModifier.NoModifier):
        a = (pos(row, col), Qt.MouseButton.LeftButton,
             Qt.MouseButton.LeftButton, mods)
        v.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        v.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    # a stationary click at row 3, col 5 -> deferred, then forwarded on release
    # as SGR press+release (1-based coords), no arrows
    click(3, 5)
    check("mouse-track: stationary left-click forwards SGR press+release",
          out == ["\x1b[<0;6;4M", "\x1b[<0;6;4m"], out)
    check("mouse-track: forwarding clears the pending-forward state",
          v._pending_fwd is None)
    out.clear()

    # a DRAG selects text locally and forwards NOTHING -- this is the regression
    # guard: forwarding on press used to make every drag a mouse report so the
    # transcript never selected. Press at "hello", drag across it, release.
    a0 = (pos(3, 0), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
          Qt.KeyboardModifier.NoModifier)
    v.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a0))
    v.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, pos(3, 4), Qt.MouseButton.NoButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos(3, 4), Qt.MouseButton.LeftButton,
        Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    check("mouse-track: drag selects locally, forwards no report", out == [], out)
    check("mouse-track: drag while tracking still yields a copyable selection",
          v.selected_text() == "hello", repr(v.selected_text()))
    check("mouse-track: drag left no pending forward", v._pending_fwd is None)
    out.clear()

    # a double-click word-selects locally (a selection gesture), forwards nothing
    v.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, pos(3, 8),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    v.mouseReleaseEvent(QMouseEvent(
        QEvent.Type.MouseButtonRelease, pos(3, 8),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("mouse-track: double-click word-selects locally, forwards nothing",
          out == [] and v.selected_text() == "world", (out, v.selected_text()))
    out.clear()

    # Shift is the escape hatch: Shift+click selects locally, forwards nothing
    click(3, 5, mods=Qt.KeyboardModifier.ShiftModifier)
    check("mouse-track: Shift+click selects locally, forwards no report",
          out == [], out)
    out.clear()

    # legacy X10 encoding (no ?1006): press byte = 32+button, release = 32+3
    v2 = TerminalView(rows=8, cols=80)
    v2.feed("\x1b[?1000h")
    check("mouse-track: X10 tracking on, SGR off",
          v2._mouse_tracking and not v2._mouse_sgr)
    out2 = []
    v2.keyInput.connect(out2.append)
    a = (QPointF(CELL_PAD_X + 5.5 * v2._cell_w, CELL_PAD_Y + 3.5 * v2._cell_h),
         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
         Qt.KeyboardModifier.NoModifier)
    v2.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
    v2.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))
    check("mouse-track: X10 stationary click forwards press(0) then release(3)",
          out2 == ["\x1b[M" + chr(32) + chr(38) + chr(36),
                   "\x1b[M" + chr(35) + chr(38) + chr(36)], out2)


def test_terminal_click_menu_guard():
    """Regression guard for the "AskUserQuestion vanishes on click" bug,
    reopened once the classic/"default" TUI renderer became the default
    (ui.terminal_scrollback=True): that renderer never negotiates mouse
    tracking, so a click on a menu falls through to local caret-repositioning
    (_reposition_cursor), which sends raw arrow/backspace bytes and corrupts
    an interactive menu that has no readline caret for them to land on. The
    fix is set_waiting_probe(agent.is_waiting) -- while it reports True, a
    click (or a selection edit) must send NOTHING to the child."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])

    def click(view, row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        a = (p, Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
             Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(QMouseEvent(QEvent.Type.MouseButtonPress, *a))
        view.mouseReleaseEvent(QMouseEvent(QEvent.Type.MouseButtonRelease, *a))

    # no mouse tracking (the classic-renderer case): a click 3 columns off the
    # caret normally sends Right arrows -- confirm that still works by default
    # (waiting_probe defaults to "never waiting"), then confirm it stops dead
    # the moment the probe reports an interactive menu is open.
    v = TerminalView(rows=6, cols=80)
    v.feed("hello")   # caret at row 0, col 5
    moves = []
    v.keyInput.connect(moves.append)
    click(v, 0, 2)
    check("click-guard: default probe still allows normal caret placement",
          moves == ["\x1b[D" * 3], moves)
    moves.clear()

    v.set_waiting_probe(lambda: True)
    click(v, 0, 2)
    check("click-guard: a click sends nothing while waiting_probe is True",
          moves == [], moves)
    moves.clear()
    # clicking the exact caret cell (the row Claude parks its cursor on while
    # showing a highlighted menu option) is the scenario that actually
    # corrupted the menu -- must be inert too, not just off-caret clicks
    click(v, 0, 7)
    check("click-guard: a click on the caret's own row is inert while waiting",
          moves == [], moves)
    moves.clear()

    v.set_waiting_probe(lambda: False)
    click(v, 0, 2)
    check("click-guard: caret placement resumes once the probe clears",
          moves == ["\x1b[D" * 3], moves)

    # independent, narrower fix: _input_block_span() now runs before the
    # same-row fast path, so a click on the cursor's own row is rejected when
    # that row is itself BLANK (previously it fired arrows regardless).
    v2 = TerminalView(rows=8, cols=80)
    v2.feed("hello\r\n")   # caret moves to row 1 col 0 -- a blank row
    blanks = []
    v2.keyInput.connect(blanks.append)
    click(v2, 1, 5)
    check("click-guard: a click on the caret's own blank row sends nothing",
          blanks == [], blanks)

    # _delete_selection carries its own copy of the guard (it sends real
    # Backspace bytes unconditionally after repositioning, so suppressing only
    # the reposition would still corrupt the menu with stray deletes).
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("hello")
    v3._sel_anchor, v3._sel_end = (0, 0), (0, 4)  # select "hell"
    v3.set_waiting_probe(lambda: True)
    check("click-guard: _delete_selection refuses while waiting_probe is True",
          v3._delete_selection() is False)
    dels = []
    v3.keyInput.connect(dels.append)
    check("click-guard: _delete_selection sent nothing while waiting",
          dels == [], dels)
    v3.set_waiting_probe(lambda: False)
    check("click-guard: _delete_selection works again once the probe clears",
          v3._delete_selection() is True)


def test_terminal_selection_edit():
    """A mouse selection (double-click word / drag) is editable like an editor
    selection: Backspace/Del deletes it, Ctrl+X cuts it, Ctrl+C copies it.
    Delete/cut drive the child's caret + Backspace, so they act ONLY on a
    single-row selection on the caret's live-screen line; off the input line
    the key is swallowed and only the selection is dropped."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QGuiApplication, QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    K = Qt.Key

    def dbl(view, row, col):
        p = QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                    CELL_PAD_Y + (row + 0.5) * view._cell_h)
        view.mouseDoubleClickEvent(QMouseEvent(
            QEvent.Type.MouseButtonDblClick, p, Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))

    def press(view, k, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, ""))

    # "hello world": child caret ends at column 11. Double-click "hello".
    v = TerminalView(rows=6, cols=80)
    v.feed("hello world")
    sent = []
    v.keyInput.connect(sent.append)
    dbl(v, 0, 2)
    check("seledit: double-click selects the word", v.selected_text() == "hello",
          v.selected_text())
    press(v, K.Key_Backspace)
    # caret parks just past the span (col 5): 6 Left arrows from col 11, then
    # Backspace over the 5 selected chars
    check("seledit: Backspace deletes the selection (arrows + backspaces)",
          sent == ["\x1b[D" * 6, "\x7f" * 5], sent)
    check("seledit: deleting drops the selection", v.selected_text() == "")

    # Ctrl+X cuts: the word lands on the clipboard AND is deleted from input
    v2 = TerminalView(rows=6, cols=80)
    v2.feed("hello world")
    sent2 = []
    v2.keyInput.connect(sent2.append)
    QGuiApplication.clipboard().clear()
    dbl(v2, 0, 8)   # "world" (cols 6-10)
    press(v2, K.Key_X, ctrl=True)
    check("seledit: Ctrl+X copies the selection to the clipboard",
          QGuiApplication.clipboard().text() == "world",
          QGuiApplication.clipboard().text())
    # caret parks just past "world" (col 11 == caret): 0 arrows, 5 backspaces
    check("seledit: Ctrl+X deletes the cut span", sent2 == ["\x7f" * 5], sent2)
    check("seledit: Ctrl+X drops the selection", v2.selected_text() == "")

    # Ctrl+X with NOTHING selected falls through to the 0x18 control byte
    sent2.clear()
    press(v2, K.Key_X, ctrl=True)
    check("seledit: Ctrl+X with no selection forwards 0x18",
          sent2 == ["\x18"], sent2)

    # a selection OFF the caret's line can't be edited: the key is swallowed
    # (no child bytes, no caret nudge) and only the selection is dropped
    v3 = TerminalView(rows=6, cols=80)
    v3.feed("aaa\r\nbbb")            # caret now on row 1
    sent3 = []
    v3.keyInput.connect(sent3.append)
    dbl(v3, 0, 1)                    # "aaa" on row 0, not the caret's row
    check("seledit: word on another row is selected", v3.selected_text() == "aaa",
          v3.selected_text())
    press(v3, K.Key_Backspace)
    check("seledit: Backspace off the input line sends nothing", sent3 == [], sent3)
    check("seledit: off-line Backspace still clears the selection",
          v3.selected_text() == "")


def test_terminal_input_editor():
    """Desktop-text-area conveniences layered on the passthrough terminal:
    keyboard text-selection (Shift+Arrow/Home/End, Ctrl for word), Shift+click
    extend, triple-click line-select, multi-row selection delete inside the
    input box, and an approximate whole-input undo/redo on Ctrl+Z/Y."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent
    from PySide6.QtWidgets import QApplication

    from app.widgets.terminal_view import (CELL_PAD_X, CELL_PAD_Y,
                                            TerminalView)

    QApplication.instance() or QApplication([])
    K = Qt.Key

    def press(view, k, ctrl=False, shift=False, text=""):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, k, mods, text))

    def pos(row, col):
        return QPointF(CELL_PAD_X + (col + 0.5) * view._cell_w,
                       CELL_PAD_Y + (row + 0.5) * view._cell_h)

    def mpress(view, row, col, shift=False):
        mods = (Qt.KeyboardModifier.ShiftModifier if shift
                else Qt.KeyboardModifier.NoModifier)
        view.mousePressEvent(QMouseEvent(
            QEvent.Type.MouseButtonPress, pos(row, col),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods))

    def mdbl(view, row, col):
        view.mouseDoubleClickEvent(QMouseEvent(
            QEvent.Type.MouseButtonDblClick, pos(row, col),
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))

    # --- keyboard selection: Ctrl+Shift+Left word-selects "world", no bytes ---
    view = TerminalView(rows=6, cols=80)
    view.feed("hello world")            # child caret at col 11
    sent = []
    view.keyInput.connect(sent.append)
    press(view, K.Key_Left, ctrl=True, shift=True)
    check("kbdsel: Ctrl+Shift+Left selects the word 'world'",
          view.selected_text() == "world", view.selected_text())
    check("kbdsel: building a selection sends nothing to the child",
          sent == [], sent)
    # a plain (unshifted) arrow collapses the selection, then forwards
    press(view, K.Key_Left)
    check("kbdsel: plain arrow collapses the selection",
          view._selection_range() is None)
    check("kbdsel: the collapsing arrow still forwards to the child",
          sent == ["\x1b[D"], sent)

    # --- Shift+click extends; triple-click selects the whole line -------------
    view = TerminalView(rows=6, cols=80)
    view.feed("hello world")
    mpress(view, 0, 0)                  # anchor at col 0
    mpress(view, 0, 5, shift=True)      # Shift+click extends to col 5
    check("kbdsel: Shift+click extends the selection",
          view.selected_text() == "hello", view.selected_text())

    view = TerminalView(rows=6, cols=80)
    view.feed("aaa bbb ccc")
    mdbl(view, 0, 1)                    # double-click primes the triple
    mpress(view, 0, 1)                  # third press at the same cell
    check("kbdsel: triple-click selects the whole line",
          view.selected_text() == "aaa bbb ccc", view.selected_text())

    # --- multi-row delete inside the input box -------------------------------
    view = TerminalView(rows=6, cols=80)
    view.feed("> line one\r\nline two")  # 2-row input box, caret row1 col8
    out = []
    view.keyInput.connect(out.append)
    view._sel_anchor = (0, 2)            # just after the '> ' prompt
    view._sel_end = (1, 7)              # last char of "line two"
    view._delete_selection()
    # "line one"(8) + one newline + "line two"(8) = 17 backspaces; caret already
    # sits at the selection end (row1 col8) so no arrows are emitted
    check("multidel: multi-row input selection backspaces the whole span",
          out == ["\x7f" * 17], out)

    # a selection that escapes the input box (output row) touches nothing
    view = TerminalView(rows=6, cols=80)
    view.feed("output line\r\n> prompt")  # row0 output, row1 input box
    out = []
    view.keyInput.connect(out.append)
    view._sel_anchor = (0, 0)
    view._sel_end = (0, 5)
    check("multidel: off-input-box selection returns False",
          view._delete_selection() is False)
    check("multidel: off-input-box delete sends nothing", out == [], out)

    # --- approximate undo / redo --------------------------------------------
    view = TerminalView(rows=6, cols=80)
    view.feed("> ")                     # empty prompt
    out = []
    view.keyInput.connect(out.append)
    view.feed("hi")                     # child echoes the typed input -> "> hi"
    view._snapshot_input()              # the debounced burst snapshot fires
    check("undo: a typed burst snapshots the prior (empty) input",
          view._undo_stack == [""] and view._undo_last == "hi",
          (view._undo_stack, view._undo_last))
    press(view, K.Key_Z, ctrl=True)     # undo -> restore ""
    check("undo: Ctrl+Z clears the prompt (empty restore = just the clear)",
          out == ["\x1b\x1b"], out)
    check("undo: undo pushes the current input onto the redo stack",
          view._redo_stack == ["hi"], view._redo_stack)
    out.clear()
    press(view, K.Key_Y, ctrl=True)     # redo -> restore "hi"
    check("undo: Ctrl+Y re-applies the undone input (clear + paste)",
          out == ["\x1b\x1b", "hi"], out)

    # empty stacks fall through to the legacy control bytes
    view = TerminalView(rows=6, cols=80)
    view.feed("> ")
    out = []
    view.keyInput.connect(out.append)
    press(view, K.Key_Z, ctrl=True)
    check("undo: Ctrl+Z with nothing to undo forwards 0x1a", out == ["\x1a"], out)
    out.clear()
    press(view, K.Key_Y, ctrl=True)
    check("undo: Ctrl+Y with nothing to redo forwards 0x19", out == ["\x19"], out)

    # a submit (bare Enter) forgets the history so undo never crosses messages
    view = TerminalView(rows=6, cols=80)
    view.feed("> hi")
    view._snapshot_input()
    view._undo_stack.append("stale")   # pretend prior history exists
    press(view, K.Key_Return)
    check("undo: submit resets the undo/redo history",
          view._undo_stack == [] and view._undo_last == "",
          (view._undo_stack, view._undo_last))


def test_input_gap_self_heal():
    """Claude's classic renderer can scroll the screen for a transient
    dropdown and never scroll back on dismissal, stranding the input box
    above a dead run of blank rows (see TerminalView._check_input_gap).
    _on_input_settled is what _snap_timer's 600ms debounce fires; called
    directly here rather than pumping a real timer, matching the existing
    _snapshot_input direct-call pattern above."""
    from app.widgets.terminal_view import TerminalView

    # a tall terminal with the box parked near the TOP and nothing below --
    # exactly the shape left once a dropdown's rows are erased but the
    # viewport is never scrolled back down
    view = TerminalView(rows=20, cols=40)
    view.feed("> \r\n? for shortcuts")
    fired = []
    view.staleLayoutDetected.connect(lambda: fired.append(1))
    view._on_input_settled()
    check("input-gap: a footer stranded far from the bottom fires once",
          fired == [1], fired)

    # settling again with nothing changed must NOT refire -- this is what
    # keeps a legitimately short conversation free of a repeated resize blip
    view._on_input_settled()
    check("input-gap: an unchanged gap does not refire",
          fired == [1], fired)

    # the footer sits right at the bottom (one row of normal padding) --
    # within tolerance, so nothing is wrong and nothing fires
    view2 = TerminalView(rows=3, cols=40)
    view2.feed("> \r\n? for shortcuts")
    fired2 = []
    view2.staleLayoutDetected.connect(lambda: fired2.append(1))
    view2._on_input_settled()
    check("input-gap: a footer within tolerance of the bottom never fires",
          fired2 == [] and view2._input_gap_row is None,
          (fired2, view2._input_gap_row))

    # no footer line at all (mid-typing) still uses the box's own bottom row
    # as the edge, and repeated settles with nothing changed still fire once
    view3 = TerminalView(rows=20, cols=40)
    view3.feed("hello\r\n> ")
    fired3 = []
    view3.staleLayoutDetected.connect(lambda: fired3.append(1))
    view3._on_input_settled()
    view3._on_input_settled()
    view3._on_input_settled()
    check("input-gap: a stable gap with no footer fires once, not per settle",
          fired3 == [1], fired3)


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


def test_pty():
    """ConPTY backend through the real card + TerminalView widget."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    import ctypes
    from ctypes import wintypes

    from app import pty_worker
    from app.process_worker import AgentKind, build_spec, describe_pid
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard

    if not HAS_CONPTY:
        check("pty: pywinpty available", False, "pywinpty not installed")
        return

    app = QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_until(pred, timeout_ms=15000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    home = os.path.expanduser("~")
    spec = build_spec(AgentKind.POWERSHELL, "Pty", cwd=home, pty=True)
    check("pty: interactive PowerShell spec is pty", spec.pty)
    agent = TerminalAgent(spec)
    check("pty: agent selects pty backend", agent.is_pty)
    card = TerminalCard(agent)
    card.resize(820, 420)
    card.show()
    check("pty: card hosts a TerminalView (no line console)",
          card.terminal is not None and card.console is None)

    # Hand the spawn the WORST case rather than whatever this suite happens to
    # have been launched with: set the ignore-Ctrl+C ConsoleFlag ON, exactly as
    # a harness that spawns with CREATE_NEW_PROCESS_GROUP does. Children capture
    # it at spawn, so unless PtyWorker.start() clears it, the interrupt check
    # below fails -- which is what makes that check a test of OUR fix and not of
    # the launching terminal.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    k32.SetConsoleCtrlHandler(None, True)
    enable_calls = []
    real_enable = pty_worker.enable_ctrl_c_for_children

    def counting_enable():
        enable_calls.append(1)
        return real_enable()

    pty_worker.enable_ctrl_c_for_children = counting_enable
    try:
        agent.start()
    finally:
        pty_worker.enable_ctrl_c_for_children = real_enable
    check("pty: the spawn clears the inherited ignore-Ctrl+C flag",
          enable_calls == [1], enable_calls)
    check("pty: agent reaches running", wait_until(agent.is_running, 15000))
    check("pty: interactive prompt renders in the grid",
          wait_until(lambda: "PS" in card.terminal.screen_text()
                     and ">" in card.terminal.screen_text(), 20000),
          card.terminal.screen_text()[-160:])

    agent.write("echo ('grid'+'ok')\r")
    check("pty: typed command output appears in the grid",
          wait_until(lambda: "gridok" in
                     card.terminal.screen_text().replace(" ", ""), 12000))

    # Ctrl+C interrupts a running child (a real console control event, not
    # available in line mode). Measured on the PROCESS, never on screen text:
    # the old check compared a screen snapshot taken before the pending output
    # had even flushed against one 2.5s later, counted the English-only string
    # "Reply", and fell back to "PS" in the last 80 chars -- which pyte pads to
    # full width, so that arm could never fire. It reported FAIL on a working
    # interrupt and PASS on a broken one, and was written off as flaky for
    # months while Ctrl+C was genuinely dead (see enable_ctrl_c_for_children).
    def ping_pids():
        return [p for p in agent.worker.job_process_ids()
                if describe_pid(p).lower().startswith("ping")]

    agent.write("ping -t 127.0.0.1\r")
    check("pty: the child command is running",
          wait_until(lambda: bool(ping_pids()), 15000),
          agent.worker.job_process_ids())
    agent.write("\x03")
    check("pty: Ctrl+C interrupted the running child",
          wait_until(lambda: not ping_pids(), 10000), ping_pids())
    # an INTERRUPT, not a kill: the shell survives its child being stopped
    check("pty: Ctrl+C left the shell alive", agent.is_running())

    # background retention while hidden
    agent.write("1..8 | %{ $_; Start-Sleep -Milliseconds 100 }\r")
    pump(150)
    card.hide()
    before = card.terminal.screen_text()
    pump(1300)
    check("pty: hidden terminal kept updating",
          card.terminal.screen_text() != before and not card.isVisible())

    pid = agent.worker.pid()
    card.detach()
    agent.dispose()
    check("pty: process terminated on dispose",
          wait_until(lambda: not agent.is_running(), 6000))
    pump(300)
    tl = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                        capture_output=True, text=True)
    check("pty: no orphan process after dispose", str(pid) not in tl.stdout)


def test_v2_features():
    """v2: providers/model/effort, per-ws numbering, stats, grid, folder,
    fonts, and shared-board awareness — driven through the real widgets."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import providers, ui_theme
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.tiling import Cell, explicit_grid, parse_layout
    from app.workspace_manager import WorkspaceManager
    from app.widgets.terminal_view import TerminalView
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_until(pred, timeout_ms=10000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v2-"))
    proj_a, proj_b = tmp / "a", tmp / "b"
    proj_a.mkdir(); proj_b.mkdir()

    # ---- providers / model / effort (#4) --------------------------------
    prog, args = providers.build_invocation("claude", model="opus", effort="high")
    check("v2 providers: claude --model/--effort flags",
          args == ["--model", "opus", "--effort", "high"], args)
    spec = build_spec(AgentKind.CLAUDE, "C", cwd=str(proj_a), model="sonnet", effort="max")
    check("v2 providers: build_spec routes claude + pty",
          spec.provider == "claude" and spec.pty
          and "--model" in spec.effective_args() and "max" in spec.effective_args())
    # template expansion must not depend on whether the CLI is installed on
    # this machine (Codex may legitimately exist here) — detection is only
    # asserted to be a bool, expansion is asserted exactly
    check("v2 providers: openai template expands",
          "gpt-5.1" in providers.build_invocation("openai", model="gpt-5.1")[1])
    check("v2 providers: detection returns bool (env-independent)",
          isinstance(providers.detected("openai"), bool))
    prog, args = providers.build_invocation(
        "openai", custom_command='"C:\\Program Files\\OpenAI\\codex.exe" --full-auto')
    check("v2 providers: quoted Windows path unwrapped for launch",
          prog == r"C:\Program Files\OpenAI\codex.exe" and args == ["--full-auto"],
          (prog, args))
    # Gemini rides the Antigravity CLI (agy): its model values are multiword
    # display strings and MUST stay one argv entry, and its spec shares
    # Claude's --continue resume + --add-dir board access (agy 1.0.16)
    _, gargs = providers.build_invocation("gemini", model="Gemini 3.6 Flash (High)", effort="high")
    check("v2 providers: gemini model flag builds correctly",
          gargs == ["--model", "Gemini 3.6 Flash (High)"], gargs)
    check("v2 providers: gemini provider has native_flags enabled",
          providers.get("gemini").native_flags is True)
    check("v2 providers: gemini efforts omitted (included in model choice)",
          providers.get("gemini").efforts == ())
    check("v2 providers: gemini detection returns bool (env-independent)",
          isinstance(providers.detected("gemini"), bool))
    gspec = build_spec(AgentKind.GEMINI, "G", cwd=str(proj_a),
                       model="Gemini 3.6 Flash (High)", effort="high")
    gspec.resume = True
    gspec.extra_dirs = [str(proj_a)]
    check("v2 providers: gemini resumes with --continue + board --add-dir",
          gspec.provider == "gemini" and gspec.pty
          and "--continue" in gspec.effective_args()
          and "--add-dir" in gspec.effective_args())
    check("v2 providers: resume providers = claude + gemini + grok",
          set(providers.RESUME_PROVIDERS) == {"claude", "gemini", "grok"})

    # Grok rides the xAI CLI (grok 0.2.93): single-token model ids expand
    # through -m, it's a template provider (no MCP/system-prompt wiring), and
    # its spec resumes with --continue like Claude/Gemini.
    _, xargs = providers.build_invocation("grok", model="grok-build")
    check("v2 providers: grok template expands -m {model}",
          xargs == ["-m", "grok-build"], xargs)
    _, xdef = providers.build_invocation("grok", model="")
    check("v2 providers: grok default omits -m flag", xdef == [], xdef)
    check("v2 providers: grok detection returns bool (env-independent)",
          isinstance(providers.detected("grok"), bool))
    check("v2 providers: grok is a template provider (no native flags)",
          providers.get("grok").native_flags is False)
    xspec = build_spec(AgentKind.GROK, "X", cwd=str(proj_a), model="grok-build")
    check("v2 providers: build_spec routes grok + pty",
          xspec.provider == "grok" and xspec.pty
          and "-m" in xspec.effective_args())
    xspec.resume = True
    check("v2 providers: grok resumes with --continue",
          "--continue" in xspec.effective_args())
    # grok has no --add-dir (it uses --cwd), so extra_dirs must never leak in
    xspec2 = build_spec(AgentKind.GROK, "X2", cwd=str(proj_a))
    xspec2.resume = True
    xspec2.extra_dirs = [str(proj_a)]
    check("v2 providers: grok never emits --add-dir",
          "--add-dir" not in xspec2.effective_args())

    # ---- explicit grid (#7) ---------------------------------------------
    ep = explicit_grid(2, 2, 2)
    check("v2 grid: 2x2/2 agents fills TL,TR + 2 empty",
          ep.agent_cells == [Cell(0, 0, 1, 1), Cell(0, 1, 1, 1)]
          and len(ep.empty_cells) == 2)
    check("v2 grid: over-capacity rebalances squarish (2x2/5 -> 3 cols x 2 rows)",
          explicit_grid(2, 2, 5).rows == 2 and explicit_grid(2, 2, 5).cols == 3)
    # overflow of a horizontal strip must widen/wrap naturally, not stack a
    # sparse extra row (the reported bug): 2x1 + a 3rd agent -> 3x1 (not 2x2),
    # 3x1 + a 4th -> 2x2 (not 3x2).
    e21 = explicit_grid(1, 2, 3)     # was 2x1, now 3 agents
    check("v2 grid: 2x1 + 3rd agent -> 3x1 (not 2x2)",
          (e21.rows, e21.cols) == (1, 3) and len(e21.empty_cells) == 0)
    e31 = explicit_grid(1, 3, 4)     # was 3x1, now 4 agents
    check("v2 grid: 3x1 + 4th agent -> 2x2 (not 3x2)",
          (e31.rows, e31.cols) == (2, 2) and len(e31.empty_cells) == 0)
    # a deliberate vertical stack stays vertical on overflow (orientation bias)
    e12 = explicit_grid(2, 1, 3)     # was 1x2, now 3 agents
    check("v2 grid: 1x2 + 3rd agent stays vertical -> 1x3",
          (e12.rows, e12.cols) == (3, 1))
    # layout strings read WIDTH x HEIGHT (like screen resolutions): "3x1" is
    # three cards side by side, "1x3" three stacked. Parsing them rows-first
    # made every non-square layout apply TRANSPOSED vs its palette diagram.
    check("v2 grid: parse_layout is width x height",
          parse_layout("auto") is None
          and parse_layout("3x2") == (2, 3)      # 3 across, 2 down
          and parse_layout("3x1") == (1, 3)      # horizontal strip
          and parse_layout("1x3") == (3, 1))     # vertical stack
    from app.widgets.grid_selector import LAYOUTS
    mismatched = [(s, r, c, parse_layout(s))
                  for (_lbl, s, r, c) in LAYOUTS
                  if s != "auto" and parse_layout(s) != (r, c)]
    check("v2 grid: every palette swatch matches its applied layout (WYSIWYG)",
          not mismatched, mismatched)
    offered = {s for _lbl, s, _r, _c in LAYOUTS}
    check("v2 grid: 3x1, 1x3, 4x1, 1x4 offered",
          {"3x1", "1x3", "4x1", "1x4"} <= offered, offered)

    # ---- font (#5) ------------------------------------------------------
    tv = TerminalView(rows=20, cols=60)
    base = tv.font_size()
    tv.set_font_size(base + 4)
    check("v2 font: TerminalView.set_font_size changes size", tv.font_size() == base + 4)

    # ---- through the real app: numbering, stats, folder, board ----------
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.resize(1500, 900)
    win.show()
    pump(150)
    mgr = win.manager
    wa = mgr.workspaces[0]
    mgr.rename_workspace(wa.id, "Alpha")
    mgr.set_workspace_path(wa.id, str(proj_a))
    wb = mgr.create_workspace("Beta", str(proj_b))

    # per-workspace numbering (#1)
    mgr.remove_terminal(wa.id, wa.agents[0].id)
    mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, mgr.next_agent_name(wa.id),
                                       cwd=str(proj_a)), autostart=False)
    check("v2 numbering: Alpha next is Agent 2", mgr.next_agent_name(wa.id) == "Agent 2")
    check("v2 numbering: Beta resets to Agent 1", mgr.next_agent_name(wb.id) == "Agent 1")

    # dashboard stats (#2) reach the sidebar row's count badge
    row = win.sidebar._rows[wa.id][1]
    check("v2 dashboard: count badge shows the agent tally",
          row.count_badge._count == 1, row.count_badge._count)
    check("v2 dashboard: one idle agent -> idle (amber) state",
          row.count_badge._state == "idle", row.count_badge._state)

    # folder change (#3) updates model + new agents' cwd
    win.manager.set_workspace_path(wa.id, str(proj_b))
    na = mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, "X", cwd=""), autostart=False)
    check("v2 folder: new agent inherits changed path", na.spec.cwd == str(proj_b))
    win.manager.set_workspace_path(wa.id, str(proj_a))

    # grid layout persists + applies (#7) through the page
    page = win._pages[wa.id]
    page.set_layout("3x2")
    check("v2 grid: page applies fixed layout with empty slots",
          len(page._empty_slots) >= 1)
    win.manager.set_layout(wa.id, "3x2")
    check("v2 grid: layout persisted", mgr.to_session_dict()["workspaces"][0]["layout"] == "3x2"
          if mgr.workspaces[0].id == wa.id else True)

    # shared board (#8): a Claude agent creates the board with a roster
    cl = mgr.add_terminal(wa.id, build_spec(AgentKind.CLAUDE, "Claude", cwd=str(proj_a)),
                          autostart=False)
    board = proj_a / ".aihive" / "board.md"
    check("v2 board: Claude agent creates shared board", board.is_file())
    check("v2 board: launch injects --add-dir + system prompt",
          "--add-dir" in cl.spec.effective_args()
          and "--append-system-prompt" in cl.spec.effective_args())
    cl.set_task("wiring auth")
    pump(30)
    check("v2 board: current task written to roster",
          "wiring auth" in board.read_text(encoding="utf-8"))
    check("v2 board: Beta workspace stays isolated",
          not (proj_b / ".aihive" / "board.md").is_file()
          or "Claude" not in (proj_b / ".aihive" / "board.md").read_text(encoding="utf-8"))

    # activity panel (#8) opens and lists the roster
    win._toggle_activity(wa.id)
    check("v2 activity: panel opens", win.activity_panel.is_open())
    check("v2 activity: roster lists agents", len(win.activity_panel._items) >= 1)
    win._toggle_activity(wa.id)
    pump(250)  # let the conceal animation finish
    check("v2 activity: panel closes", not win.activity_panel.is_open())

    win.close()
    pump(200)
    ui_theme.CONSOLE_FONT_PX = ui_theme.DEFAULT_CONSOLE_PX
    shutil.rmtree(tmp, ignore_errors=True)


def test_v2_review_fixes():
    """Regressions for the confirmed v2 review findings."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import ui_theme
    from app.coordination import git_changed_files
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v2fix-"))
    pa, pb = tmp / "a", tmp / "b"
    pa.mkdir(); pb.mkdir()

    # git on a non-repo returns [] fast (islice-capped, short timeout)
    check("fix git: non-repo returns empty", git_changed_files(str(pa)) == [])

    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.resize(1500, 900)
    win.show()
    pump(120)
    mgr = win.manager
    wa = mgr.workspaces[0]
    mgr.set_workspace_path(wa.id, str(pa))
    mgr.remove_terminal(wa.id, wa.agents[0].id)

    # --- set_workspace_path repoints existing Claude agent + scaffolds board ---
    cl = mgr.add_terminal(wa.id, build_spec(AgentKind.CLAUDE, "Claude", cwd=str(pa)),
                          autostart=False)
    mgr.set_workspace_path(wa.id, str(pb))
    check("fix path: existing agent cwd follows new folder", cl.spec.cwd == str(pb))
    check("fix path: coordination re-injected to new board",
          any(str(pb) in d for d in cl.spec.extra_dirs), cl.spec.extra_dirs)
    check("fix path: new board scaffolded immediately",
          (pb / ".aihive" / "board.md").is_file())

    # --- line-mode per-agent font uses a per-widget stylesheet (QSS-proof) ---
    line = mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, "Line", cwd=str(pb), pty=False),
                            autostart=False)
    lcard = win._pages[wa.id].card_for(line.id)
    lcard._font_delta(+3)
    want = ui_theme.CONSOLE_FONT_PX + 3
    check("fix font: line card applies per-widget stylesheet",
          f"{want}px" in lcard.console.styleSheet(), lcard.console.styleSheet())
    check("fix font: current_font_px reflects override", lcard.current_font_px() == want)
    # a global font change must NOT wipe the per-agent override
    win._change_global_font(-1)
    check("fix font: global change preserves line override",
          f"{want}px" in lcard.console.styleSheet(), lcard.console.styleSheet())

    # --- activity button sync across workspace switch ---
    wb = mgr.create_workspace("B", str(pb))
    win._toggle_activity(wa.id)
    check("fix activity: A button lit when open on A",
          win._pages[wa.id].activity_btn.isChecked())
    mgr.set_active(wb.id)
    pump(30)
    check("fix activity: A button cleared after switching away",
          not win._pages[wa.id].activity_btn.isChecked()
          and win._pages[wb.id].activity_btn.isChecked())
    win._toggle_activity(wb.id)
    pump(30)
    check("fix activity: all buttons cleared when closed",
          not win._pages[wa.id].activity_btn.isChecked()
          and not win._pages[wb.id].activity_btn.isChecked())

    # --- layoutChanged from the model drives the page ---
    mgr.set_layout(wa.id, "2x2")
    check("fix layout: model set_layout updates the page",
          win._pages[wa.id]._layout == "2x2")

    win.close()
    pump(200)
    ui_theme.CONSOLE_FONT_PX = ui_theme.DEFAULT_CONSOLE_PX
    shutil.rmtree(tmp, ignore_errors=True)


def test_v3_features():
    """v3: private-CSI/underline fix, clipboard, task-assignment primitives,
    persistent-agent badges, and the named-pipe board bridge."""
    import threading

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop, QTimer

    from app import orchestration, providers
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.terminal_agent import AssignmentState, TerminalAgent
    from app.widgets.terminal_view import TerminalView
    from app.workspace_manager import WorkspaceManager
    from main import setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_until(pred, timeout_ms=10000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v3-"))

    # --- #7 the underline root-cause fix (private CSI stripped) ---
    tv = TerminalView(rows=4, cols=30)
    tv.feed("\x1b[>4;2m\x1b[38;2;255;193;7mGOLD")  # modifyOtherKeys + truecolor
    cell = next(tv.screen.buffer[0][c] for c in range(30)
                if tv.screen.buffer[0][c].data.strip())
    check("v3 render: modifyOtherKeys no longer forces underline",
          not cell.underscore and cell.fg == "ffc107")

    # --- #1 clipboard: selection + paste on the terminal ---
    tv2 = TerminalView(rows=4, cols=30)
    tv2.feed("hello world")
    tv2._sel_anchor, tv2._sel_end = (0, 0), (0, 4)
    check("v3 clipboard: selection text", tv2.selected_text() == "hello", tv2.selected_text())

    # --- #5 model selection + #4 roles (pure) ---
    check("v3 model: trivial to haiku", orchestration.select_model_effort("fix a typo") == ("haiku", "low"))
    check("v3 model: architecture to opus", orchestration.select_model_effort("design the auth architecture") == ("opus", "high"))
    check("v3 model: explicit override wins",
          orchestration.resolve_model_effort("x", model="opus", effort="max") == ("opus", "max"))
    check("v3 roles: infer testing", orchestration.infer_role("add pytest coverage") == "Testing Agent")
    _, a = providers.build_invocation("claude", effort="ultracode")
    check("v3 ultracode: never a launch flag", "--effort" not in a)

    # --- permission mode (Shift+Tab modes) startup flag (Claude) ------------
    _, pm_default = providers.build_invocation("claude")
    check("perm-mode: default omits the flag (today's behavior)",
          "--permission-mode" not in pm_default, pm_default)
    _, pm_plan = providers.build_invocation("claude", permission_mode="plan")
    check("perm-mode: chosen mode becomes --permission-mode <mode>",
          pm_plan == ["--permission-mode", "plan"], pm_plan)
    _, pm_bad = providers.build_invocation("claude", permission_mode="bogus")
    check("perm-mode: an unknown mode never reaches the CLI",
          "--permission-mode" not in pm_bad, pm_bad)
    pm_spec = build_spec(AgentKind.CLAUDE, "PM", cwd=str(tmp),
                         permission_mode="acceptEdits")
    check("perm-mode: build_spec bakes it into args + persists round-trip",
          "--permission-mode" in pm_spec.effective_args()
          and "acceptEdits" in pm_spec.effective_args()
          and AgentSpec.from_dict(pm_spec.to_dict()).permission_mode
          == "acceptEdits", pm_spec.to_dict())

    # --- task-assignment primitives via a fast line-mode echo agent ---
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("W", str(tmp))
    echo = mgr.add_terminal(ws.id, build_spec(
        AgentKind.CUSTOM, "Echo", cwd=str(tmp), program=sys.executable,
        args=["-u", "-c", "import sys\nfor l in sys.stdin: print('did:'+l.strip(),flush=True)"]),
        autostart=True)
    seen = []
    echo.output_segment.connect(lambda s, t: seen.append(t))
    wait_until(lambda: echo.is_running(), 8000)
    mgr.assign_task(ws.id, echo.id, "build the parser")
    check("v3 assign: WORKING + delivered", echo.assignment is AssignmentState.WORKING
          and wait_until(lambda: any("did:build the parser" in t for t in seen), 8000))
    mgr.set_assignment_state(echo.id, AssignmentState.COMPLETED)
    check("v3 assign: set_assignment_state", echo.assignment is AssignmentState.COMPLETED)
    check("v3 roles: collision numbering",
          mgr.assign_role_name(ws.id, echo.spec.name) == f"{echo.spec.name} 2")
    mgr.reassign_agent(echo.id, "now write the tests")
    check("v3 reassign: delivered to existing session",
          wait_until(lambda: any("did:now write the tests" in t for t in seen), 8000))

    # --- #3/#8 board bridge over the real named pipe: log_activity round-trips
    from app.orchestrator_bridge import OrchestratorBridge, HAS_QTNETWORK
    if HAS_QTNETWORK:
        from app import mcp_server
        bridge = OrchestratorBridge(mgr, active_ws=lambda: ws.id)
        check("v3 bridge: named pipe listening", bridge.start())
        os.environ["AIHIVE_PIPE"] = bridge.pipe_name
        os.environ["AIHIVE_WS"] = ws.id
        res = {}
        ev = threading.Event()

        def rpc():
            try:
                res["log"] = mcp_server._rpc(
                    "log_activity",
                    {"agent": "Echo", "message": "wired the parser"})
            except Exception as e:
                res["err"] = repr(e)
            finally:
                ev.set()

        threading.Thread(target=rpc, daemon=True).start()
        wait_until(ev.is_set, 15000)
        check("v3 bridge: MCP client + pipe RPC round-trip",
              "err" not in res and res.get("log", {}).get("logged") is True,
              res.get("err"))
        check("v3 bridge: log_activity note lands on the board",
              "wired the parser" in "\n".join(ws.board.read_log_tail()))
        bridge.stop()
        os.environ.pop("AIHIVE_WS", None)

    echo.dispose()
    pump(200)
    shutil.rmtree(tmp, ignore_errors=True)


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

    # a Claude agent that was running resumes with --continue on reopen
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
    check("persist: running Claude agent resumes (--continue)",
          ag.spec.resume and "--continue" in ag.spec.effective_args())
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


def test_scrollback():
    """Wheel scrollback must survive continuous TUI repaints. The old code used
    pyte's prev_page/next_page, but HistoryScreen snaps to the bottom on ANY
    subsequent screen event — Claude Code repaints many times a second, so the
    view was yanked back instantly ('scroll doesn't work'). Now scrollback is a
    view offset that pyte never sees."""
    import re
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])
    v = TerminalView(rows=10, cols=40)
    for i in range(50):
        v.feed(f"scroll-{i}\r\n")
    check("scroll: history accumulates", len(v.screen.history.top) >= 30,
          len(v.screen.history.top))
    check("scroll: live view shows the tail", "scroll-49" in v.visible_text())

    v.scroll_by(15)
    seen = v.visible_text()
    check("scroll: wheel-up reveals older lines", "scroll-26" in seen, seen)
    check("scroll: tail off-screen while scrolled", "scroll-49" not in seen)

    # the regression: new output must NOT snap the view back (old prev_page
    # behavior); the view stays anchored to the content being read
    for i in range(50, 56):
        v.feed(f"scroll-{i}\r\n")
    after = v.visible_text()
    check("scroll: new output does not snap view back", "scroll-26" in after,
          after)
    check("scroll: offset grew to stay content-anchored", v.scroll_offset() > 15,
          v.scroll_offset())

    # copying while scrolled back copies what's on screen (history), not live
    v.select_all()
    check("scroll: copy sees scrolled-back content",
          "scroll-26" in v.selected_text())
    v._sel_anchor = v._sel_end = None

    # typing any key snaps back to live
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_A,
                              Qt.KeyboardModifier.NoModifier, "a"))
    check("scroll: keystroke snaps back to live", v.scroll_offset() == 0)
    check("scroll: live tail visible again", "scroll-55" in v.visible_text())

    # Shift+PageUp/PageDown page the view without touching the app
    got = []
    v.keyInput.connect(got.append)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_PageUp,
                              Qt.KeyboardModifier.ShiftModifier, ""))
    check("scroll: Shift+PageUp pages back", v.scroll_offset() == 9,
          v.scroll_offset())
    check("scroll: paging sends nothing to the pty", got == [], got)
    v.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_PageDown,
                              Qt.KeyboardModifier.ShiftModifier, ""))
    check("scroll: Shift+PageDown pages forward", v.scroll_offset() == 0)

    # bounds: can't scroll past the oldest line or below the live screen
    v.scroll_by(99999)
    check("scroll: clamped at oldest history line",
          v.scroll_offset() == len(v.screen.history.top))
    v.scroll_by(-99999)
    check("scroll: clamped at live bottom", v.scroll_offset() == 0)

    # ---- wheel routing for fullscreen apps (the Claude Code case) ----
    # Claude Code v2 enters the ALTERNATE SCREEN and enables mouse tracking
    # (?1049h ?1000/1002/1003h ?1006h, verified against v2.1.197): there is no
    # terminal scrollback there — the wheel must be FORWARDED so the app
    # scrolls its own transcript. That's why history-offset scrolling alone
    # read as 'scroll still not working' in Claude terminals.
    from PySide6.QtCore import QPoint, QPointF
    from PySide6.QtGui import QWheelEvent

    def wheel(view, dy):
        return QWheelEvent(QPointF(50, 30), QPointF(50, 30), QPoint(0, 0),
                           QPoint(0, dy), Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier,
                           Qt.ScrollPhase.NoScrollPhase, False)

    sent = []
    v2 = TerminalView(rows=10, cols=40)
    v2.keyInput.connect(sent.append)

    # altscreen WITHOUT mouse tracking (vim/less): wheel -> arrow keys
    v2.feed("\x1b[?1049h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: altscreen wheel-up sends arrow keys",
          sent and sent[-1] == "\x1b[A" * 3, sent[-1:])
    v2.wheelEvent(wheel(v2, -120))
    check("scroll: altscreen wheel-down sends down arrows",
          sent[-1] == "\x1b[B" * 3, sent[-1:])

    # DECCKM application cursor keys switch the arrow encoding
    v2.feed("\x1b[?1h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: DECCKM arrows use SS3 form", sent[-1] == "\x1bOA" * 3,
          sent[-1:])
    v2.feed("\x1b[?1l")

    # mouse tracking + SGR (Claude Code): wheel -> SGR mouse reports
    v2.feed("\x1b[?1000;1006h")
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: tracked wheel sends SGR mouse reports",
          re.fullmatch(r"(?:\x1b\[<64;\d+;\d+M){3}", sent[-1]) is not None,
          repr(sent[-1]))
    v2.wheelEvent(wheel(v2, -120))
    check("scroll: SGR wheel-down uses button 65",
          sent[-1].startswith("\x1b[<65;"), repr(sent[-1]))

    # leaving altscreen + tracking restores history-offset scrolling
    v2.feed("\x1b[?1000;1006l\x1b[?1049l")
    for i in range(30):
        v2.feed(f"back-{i}\r\n")
    before = len(sent)
    v2.wheelEvent(wheel(v2, 120))
    check("scroll: normal buffer scrolls locally again (nothing sent)",
          len(sent) == before and v2.scroll_offset() == 3,
          (len(sent) - before, v2.scroll_offset()))

    # focus reporting (?1004): Claude Code wants focus in/out events
    v2.feed("\x1b[?1004h")
    sent.clear()
    from PySide6.QtGui import QFocusEvent
    v2.focusInEvent(QFocusEvent(QEvent.Type.FocusIn))
    v2.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
    check("scroll: focus reporting forwards ESC[I / ESC[O",
          sent == ["\x1b[I", "\x1b[O"], sent)


def test_themes():
    """Winamp-style skin registry: apply_theme rewrites Palette/ANSI/fonts in
    place (so all Palette.X reads follow the skin), Scriptorium Dark stays the
    shipped palette, build_qss reflects the active theme, and the choice
    persists across a save/restore."""
    from PySide6.QtWidgets import QApplication
    from app import ui_theme
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    check("themes: registry has the expected skins",
          set(ui_theme.THEMES) == {"scriptorium-dark", "illuminated-manuscript",
                                   "obsidian", "adeptus-mechanicus"},
          list(ui_theme.THEMES))
    check("themes: ids match their keys and names are unique",
          all(k == t.id for k, t in ui_theme.THEMES.items())
          and len({t.name for t in ui_theme.THEMES.values()})
          == len(ui_theme.THEMES))

    # apply_theme mutates the SAME Palette/ANSI_16 objects in place
    ansi_obj = ui_theme.ANSI_16
    ui_theme.apply_theme("scriptorium-dark")
    check("themes: Scriptorium Dark keeps the shipped palette",
          ui_theme.Palette.BG_ROOT == "#12100c"
          and ui_theme.Palette.ACCENT_GOLD == "#c9a227")
    dark_qss = ui_theme.build_qss()
    ui_theme.apply_theme("illuminated-manuscript")
    check("themes: apply_theme mutates Palette in place (same refs update)",
          ui_theme.Palette.BG_ROOT == "#e6d7b1"
          and ui_theme.Palette.CARDHEAD_FG == "#f0d777")
    check("themes: ANSI_16 is refreshed in the SAME list object",
          ui_theme.ANSI_16 is ansi_obj and ui_theme.ANSI_16[0] == "#5a4636")
    ms_qss = ui_theme.build_qss()
    check("themes: build_qss reflects the active skin",
          "#e6d7b1" in ms_qss and "#e6d7b1" not in dark_qss
          and "#12100c" in dark_qss)
    check("themes: unknown id falls back to default",
          ui_theme.apply_theme("nope").id == ui_theme.DEFAULT_THEME_ID)

    # persistence: a saved theme restores and drives the dropdown
    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-theme-"))
    store = SessionStore(path=tmp / "s.json")
    store.save({"version": 3, "active": "w", "workspaces": [
        {"id": "w", "name": "W", "project_path": str(tmp), "layout": "auto",
         "terminals": []}], "ui": {"theme": "illuminated-manuscript"}})
    win = create_main_window(store)
    check("themes: saved skin restored on launch",
          win._theme_id == "illuminated-manuscript"
          and ui_theme.Palette.BG_ROOT == "#e6d7b1")
    check("themes: dropdown reflects the restored skin",
          win.top_bar.theme_select.currentData() == "illuminated-manuscript")
    # Phase B ornament: the illuminated page border shows + opens root margins
    # only for the manuscript skin; hidden and flush for the others
    # isHidden(), not isVisible(): the window isn't shown in this headless test,
    # so isVisible() is always False; isHidden() reflects the explicit setVisible
    check("themes: manuscript shows the illuminated border",
          not win._page_border.isHidden()
          and win._root_layout.contentsMargins().top() > 0)
    win._change_theme("obsidian")
    check("themes: live switch updates palette + dropdown",
          ui_theme.Palette.BG_ROOT == "#0f1117"
          and win.top_bar.theme_select.currentData() == "obsidian")
    check("themes: non-manuscript skin hides the border (flush)",
          win._page_border.isHidden()
          and win._root_layout.contentsMargins().top() == 0)
    check("themes: switch is written to the session payload",
          win._session_payload()["ui"]["theme"] == "obsidian")
    win.close()

    # the ornament widgets paint without error under both illuminated and
    # plain themes (exercises the QSvgRenderer + QPainter paths)
    from app.widgets.ornaments import DropCap, LogoRoundel, PageBorder
    for tid in ("illuminated-manuscript", "adeptus-mechanicus", "scriptorium-dark"):
        ui_theme.apply_theme(tid)
        for W in (DropCap, LogoRoundel, PageBorder):
            w = W("A") if W is DropCap else W()
            w.resize(200, 160)
            w.grab()  # runs paintEvent; raises if the paint path is broken
            w.deleteLater()
    check("themes: ornament widgets paint under every skin", True)

    # SHAPED-ICON GUARANTEE. The artwork is exported flat on a near-black
    # ground; shipping that unkeyed put a hard black square in the title bar,
    # the taskbar, Alt+Tab and the Start tile (all of which can be light), and
    # the same bitmap backs LogoRoundel, where it read as a cold black tile on
    # the warm panel. generate_app_icon.py keys the ground out and trims to the
    # mark, so both assets must arrive with see-through corners; a future flat
    # re-export that skipped that step would fail here rather than quietly
    # reinstating the square.
    from PySide6.QtGui import QImage
    icons = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "app", "assets", "icons")
    shaped, opaque_corner = [], []
    for asset in ("app_logo.png", "app_icon.ico"):
        img = QImage(os.path.join(icons, asset))
        if img.isNull():
            opaque_corner.append(f"{asset}: missing")
            continue
        w, h = img.width(), img.height()
        alphas = [img.pixelColor(x, y).alpha() for x, y in
                  ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
        (shaped if not any(alphas) else opaque_corner).append(
            f"{asset}: {alphas}")
        # and the mark itself has to still be there — a key that ate the
        # artwork would also leave the corners clear. A RATIO, because the
        # .ico hands QImage whichever single frame it likes (a 16px one here),
        # so an absolute sample count would only measure that choice.
        step_x, step_y = max(1, w // 40), max(1, h // 40)
        pts = [(x, y) for y in range(0, h, step_y) for x in range(0, w, step_x)]
        ink = sum(1 for x, y in pts if img.pixelColor(x, y).alpha() > 200)
        if ink < len(pts) * 0.25:
            opaque_corner.append(
                f"{asset}: only {ink}/{len(pts)} samples are opaque")
    check("themes: shipped icon assets are shaped, not opaque squares",
          len(shaped) == 2 and not opaque_corner, opaque_corner)

    # CONTRAST GUARANTEE (chrome): every skin's text tokens must clearly read
    # on their own backgrounds — the fix behind the white-on-vellum report.
    from PySide6.QtGui import QColor
    from app.widgets.terminal_view import (contrast_ratio, legible_color,
                                           LEGIBLE_TARGET)
    fails = []
    for t in ui_theme.THEMES.values():
        pairs = [  # (fg, bg, min-ratio, label)
            (t.text, t.bg_panel, 4.5, "text/panel"),
            (t.text, t.bg_card, 4.5, "text/card"),
            (t.text, t.bg_root, 4.5, "text/root"),
            (t.console_fg, t.bg_console, 4.5, "console"),
            (t.cardhead_fg, t.bg_cardhead, 4.5, "cardhead"),
            (t.appname_fg, t.bg_panel, 4.0, "appname"),
            (t.input_echo, t.bg_input, 4.0, "input-echo"),
            (t.text_dim, t.bg_panel, 3.0, "text_dim"),
            (t.cardhead_sub, t.bg_cardhead, 3.0, "cardhead_sub"),
            (t.selection_fg, t.selection, 3.0, "selection"),
        ]
        for fg, bg, mn, label in pairs:
            r = contrast_ratio(QColor(fg), QColor(bg))
            if r < mn:
                fails.append(f"{t.id}:{label}={r:.2f}<{mn}")
    check("themes: all chrome text meets contrast in every skin", not fails, fails)

    # the checked-checkbox tick must be clearly visible on the accent fill in
    # every skin (the near-invisible-default-tick report). _check_icon_path
    # picks black/white for max contrast; assert the chosen tick clears the 3:1
    # graphical-contrast target on each accent.
    ck_fails = []
    for t in ui_theme.THEMES.values():
        path = ui_theme._check_icon_path(t.accent_gold)
        if not path or not os.path.isfile(path):
            ck_fails.append(f"{t.id}:no-icon")
            continue
        with open(path, encoding="utf-8") as f:
            stroke = f.read().split("stroke='")[1].split("'")[0]
        r = contrast_ratio(QColor(stroke), QColor(t.accent_gold))
        if r < 3.0:
            ck_fails.append(f"{t.id}:tick={r:.2f}")
    check("themes: checkbox tick contrasts with its accent fill", not ck_fails,
          ck_fails)

    # CONTRAST GUARANTEE (terminal): whatever color a child emits, the glyph is
    # forced readable on its actual background (the invisible-Claude-on-vellum
    # bug); text that is already readable is left untouched.
    vellum, ink = QColor("#fbf4df"), QColor("#0d0d0d")
    check("legible: white-on-vellum rescued to readable",
          contrast_ratio(legible_color(QColor("#ffffff"), vellum), vellum)
          >= LEGIBLE_TARGET - 0.05)
    check("legible: light-grey-on-vellum rescued to readable",
          contrast_ratio(legible_color(QColor("#c9d1d9"), vellum), vellum)
          >= LEGIBLE_TARGET - 0.05)
    check("legible: dark-on-dark rescued to readable",
          contrast_ratio(legible_color(QColor("#222222"), ink), ink)
          >= LEGIBLE_TARGET - 0.05)
    already = QColor("#c9d1d9")
    check("legible: already-readable text is left unchanged",
          legible_color(already, ink) == already)
    check("legible: hue is preserved when rescuing (blue stays blueish)",
          legible_color(QColor("#88bbff"), vellum).hue()
          in range(QColor("#88bbff").hue() - 25, QColor("#88bbff").hue() + 25))

    ui_theme.apply_theme("scriptorium-dark")  # leave the default active
    shutil.rmtree(tmp, ignore_errors=True)


def test_review_fixes():
    """Regressions for the Codex-review findings: single-instance mutex guard
    (fail closed), task-assignment state autosave, workspace-bound board
    log_activity, quoted-path command parsing, and the session save-audit
    log."""
    import json as _json
    from PySide6.QtWidgets import QApplication

    from app.orchestrator_bridge import OrchestratorBridge, _RpcError
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import AssignmentState
    from app.workspace_manager import WorkspaceManager
    from main import _single_instance_guard

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-review-"))

    # --- single-instance guard: kernel mutex, second acquisition refused ---
    # (own mutex name: the REAL app may legitimately be running during a
    # test run, and colliding with its lock is not this test's business)
    mtx = f"Local\\ai-hive-test-{os.getpid()}"
    first = _single_instance_guard(mtx)
    check("guard: first instance acquires the mutex", bool(first))
    second = _single_instance_guard(mtx)
    check("guard: second instance is refused (fail closed)", second is None)
    if sys.platform == "win32" and first:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(first)  # release for the real app

    # --- task-assignment state changes mark the session dirty ---
    mgr = WorkspaceManager()
    ws_a = mgr.create_workspace("ScopeA", project_path=str(tmp))
    ws_b = mgr.create_workspace("ScopeB", project_path=str(tmp))
    a1 = mgr.add_terminal(ws_a.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    b1 = mgr.add_terminal(ws_b.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    dirty_count = {"n": 0}
    mgr.dirty.connect(lambda: dirty_count.__setitem__("n", dirty_count["n"] + 1))
    a1.set_task("write the docs")
    check("autosave: task change marks dirty", dirty_count["n"] >= 1,
          dirty_count["n"])
    before = dirty_count["n"]
    a1.set_assignment(AssignmentState.COMPLETED)
    check("autosave: assignment change marks dirty", dirty_count["n"] > before)
    before = dirty_count["n"]
    a1.set_role("Docs Writer")
    check("autosave: role change marks dirty", dirty_count["n"] > before)

    # --- manual rename (set_name) decouples the display name from the role ---
    # isolated in its own workspace so it never disturbs the scope tests below,
    # which resolve a1/b1 by their names.
    ws_c = mgr.create_workspace("ScopeC", project_path=str(tmp))
    r1 = mgr.add_terminal(ws_c.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    r1.set_role("Docs Writer")
    names_seen = []
    r1.name_changed.connect(lambda n: names_seen.append(n))
    before = dirty_count["n"]
    r1.set_name("Scribe")
    check("rename: set_name changes only the display name",
          r1.spec.name == "Scribe" and r1.spec.role == "Docs Writer",
          f"name={r1.spec.name} role={r1.spec.role}")
    check("rename: set_name flags the name custom", r1.spec.custom_name)
    check("rename: set_name emits name_changed", names_seen == ["Scribe"],
          names_seen)
    check("rename: set_name marks dirty", dirty_count["n"] > before)
    # a later retask (set_role) updates the role but NEVER the custom name
    r1.set_role("Backend Architect")
    check("rename: set_role keeps a custom name, updates the role",
          r1.spec.name == "Scribe" and r1.spec.role == "Backend Architect",
          f"name={r1.spec.name} role={r1.spec.role}")
    # a NON-custom agent still renames both (no regression to the old coupling)
    r2 = mgr.add_terminal(ws_c.id, build_spec(AgentKind.CMD, "Agent 2",
                                              cwd=str(tmp)), autostart=False)
    r2.set_role("Tester")
    check("rename: set_role renames both when name is not custom",
          r2.spec.name == "Tester" and r2.spec.role == "Tester"
          and not r2.spec.custom_name,
          f"name={r2.spec.name} custom={r2.spec.custom_name}")
    # persistence round-trip: custom_name survives to_dict/from_dict
    restored = AgentSpec.from_dict(r1.spec.to_dict())
    check("rename: custom_name round-trips through to_dict/from_dict",
          restored.name == "Scribe" and restored.custom_name is True,
          f"name={restored.name} custom={restored.custom_name}")
    # degraded save path keeps the flag too (a malformed agent must not lose it)
    safe = mgr._agent_dict_safe(r1)
    check("rename: custom_name present for serialize", "custom_name" in safe)
    mgr.remove_workspace(ws_c.id)

    # --- workspace-bound board log_activity via the bridge dispatch ---
    bridge = OrchestratorBridge(mgr, active_ws=lambda: ws_a.id)
    logged = bridge._dispatch(
        "log_activity", {"agent": "Docs Writer", "message": "wrote the docs"},
        ws=ws_a.id)
    check("board: agent may log_activity", logged.get("logged") is True)
    check("board: log_activity binds to the caller's workspace",
          logged.get("workspace_id") == ws_a.id)
    tail = ws_a.board.read_log_tail()
    check("board: log_activity entry lands on the board",
          any("wrote the docs" in t for t in tail), tail)
    # a note for one workspace never crosses onto another's board (distinct
    # project folders => distinct board.md files)
    ws_d = mgr.create_workspace("ScopeD", project_path=str(tmp / "d"))
    bridge._dispatch("log_activity", {"agent": "X", "message": "note for D"},
                     ws=ws_d.id)
    check("board: a note lands only on its own workspace board",
          not any("note for D" in t for t in ws_a.board.read_log_tail())
          and any("note for D" in t for t in ws_d.board.read_log_tail()))
    # the removed orchestration ops are simply unknown now
    try:
        bridge._dispatch("spawn_agent", {"task": "x"}, ws=ws_a.id)
        unknown_ok = False
    except _RpcError as e:
        unknown_ok = e.code == "bad_args"
    check("board: removed orchestration ops are unknown", unknown_ok)
    # append_activity must NOT disturb the roster block
    ws_a.board.update_roster([{"name": "A1", "role": "", "provider":
                "claude", "model": "", "status": "idle", "task": "keep me"}])
    ws_a.board.append_activity("A1", "another line")
    body = Path(ws_a.board.path).read_text(encoding="utf-8")
    check("board: roster survives an activity append",
          "keep me" in body and body.count("AIHIVE:ROSTER:BEGIN") == 1
          and "another line" in body)

    # --- per-workspace mcp config carries the binding (WS only, no role) ---
    bridge.pipe_name = "test-pipe"
    bridge._mcp_dir = str(tmp / "mcp")
    cfg_path = bridge.mcp_config_path_for(ws_a.id)
    cfg = _json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["aihive"]["env"]
    check("board: mcp config binds AIHIVE_WS to the workspace",
          env.get("AIHIVE_WS") == ws_a.id and env.get("AIHIVE_PIPE") == "test-pipe")
    check("board: mcp config no longer carries a role",
          "AIHIVE_ROLE" not in env)

    # --- session save-audit log (forensics for any future clobber) ---
    sp = tmp / "audit" / "s.json"
    store = SessionStore(path=sp)
    store.save({"version": 3, "workspaces": [{"terminals": [1, 2]}]})
    log = sp.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: save writes a forensic line with pid",
          "SAVE ws=1 terminals=[2]" in log and f"pid={os.getpid()}" in log, log)
    sp.write_text("{corrupt", encoding="utf-8")
    store.load()
    log = sp.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: load failure is recorded before .bak rename",
          "LOAD-FAIL" in log, log)

    # a save that fails must be VISIBLE in the log (an instance once went
    # silent — no saves, no errors — while the user kept working), and every
    # successful save keeps a rolling previous generation for recovery
    sp2 = tmp / "audit" / "roll.json"
    store2 = SessionStore(path=sp2)
    store2.save({"version": 3, "gen": 1, "workspaces": []})
    store2.save({"version": 3, "gen": 2, "workspaces": []})
    prev = _json.loads(sp2.with_suffix(".json.1").read_text(encoding="utf-8"))
    cur = _json.loads(sp2.read_text(encoding="utf-8"))
    check("audit: rolling previous-generation session backup",
          prev.get("gen") == 1 and cur.get("gen") == 2, (prev, cur))
    ok = store2.save({"bad": object()})  # non-serializable -> save fails
    log2 = sp2.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: failed save is recorded, not silent",
          ok is False and "SAVE-FAIL" in log2, log2[-200:])
    check("audit: failed save never corrupts the session file",
          _json.loads(sp2.read_text(encoding="utf-8")).get("gen") == 2)

    # --- mojibake armor: JSON can carry lone surrogates (MCP tool calls,
    # session files); strict-UTF-8 sinks refuse them. One such task string
    # crashed the app at startup while rewriting the board roster.
    from app import coordination
    bad = "reconcile the docs \udc81 with reality"
    a1.set_task(bad)
    check("mojibake: set_task strips lone surrogates",
          "\udc81" not in a1.current_task and a1.current_task.startswith("reconcile"),
          ascii(a1.current_task))
    board = coordination.WorkspaceBoard(str(tmp / "boardws"))
    ok = board.update_roster([{"name": "X", "role": "", "provider": "claude",
                               "model": "", "status": "idle", "task": bad}])
    check("mojibake: board roster write survives surrogates", ok)
    body = Path(board.path).read_text(encoding="utf-8")  # strict decode
    check("mojibake: board file remains valid strict UTF-8", "| X |" in body)

    for a in mgr.all_agents():
        a.dispose()
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


def test_agent_file_map():
    """The Agent/File Map visualizer: (1) the transcript parser attributes
    edited vs read files and detects Task sub-agents while skipping malformed
    lines; (2) the window builds its Tree-view node model (file hierarchy +
    agent hubs + owner connectors) and paints headlessly; (3) the workspace-
    header button opens the window. Tree is the ONLY view (Bubble was removed)."""
    import json as _json

    from app import file_activity, transcripts
    from app.terminal_agent import AgentStatus
    from app.widgets.agent_file_map import AgentFileMapWindow
    from app.workspace_manager import Workspace

    def line(name, **inp):
        return _json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": name, "input": inp}]}})

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-map-"))
    conv_a = tmp / "a.jsonl"
    conv_b = tmp / "b.jsonl"
    shared_fp = str(tmp / "shared.py")
    only_a_fp = str(tmp / "only_a.py")
    only_b_fp = str(tmp / "only_b.py")
    conv_a.write_text("\n".join([
        line("Edit", file_path=shared_fp),
        line("Read", file_path=only_a_fp),
        line("Read", file_path=only_a_fp),           # repeat -> deduped
        line("Agent", subagent_type="Explore", description="scan the code"),
        "{ this is not valid json",                  # malformed -> skipped
    ]) + "\n", encoding="utf-8")
    conv_b.write_text("\n".join([
        line("Write", file_path=shared_fp),          # shared, edited by B too
        line("Edit", file_path=only_b_fp),
    ]) + "\n", encoding="utf-8")

    # -- 1. parser --------------------------------------------------------
    act = file_activity.parse_transcript(str(conv_a))
    check("map: parser skips malformed line without crashing", act is not None)
    check("map: edited file detected", any(
        f.edited and f.basename == "shared.py" for f in act.files.values()))
    check("map: read-only file detected", len(act.read_only_files()) == 1
          and act.read_only_files()[0].basename == "only_a.py")
    check("map: repeated read deduped to one entry",
          [f.basename for f in act.files.values()].count("only_a.py") == 1)
    check("map: Task/Agent sub-agent captured",
          len(act.subagents) == 1 and act.subagents[0].subagent_type == "Explore")
    check("map: missing transcript -> empty activity (no raise)",
          file_activity.parse_transcript(str(tmp / "nope.jsonl")).files == {})

    # stub agents whose transcript_path maps to the fixtures above
    class _Spec:
        def __init__(self, name, sid):
            self.name = name; self.role = ""; self.provider = "claude"
            self.cwd = str(tmp); self.session_id = sid

    class _Agent:
        def __init__(self, name, sid):
            self.spec = _Spec(name, sid); self.status = AgentStatus.RUNNING
        def is_busy(self): return False
        def is_running(self): return True

    real_tp = transcripts.transcript_path
    transcripts.transcript_path = lambda cwd, sid: (
        str(conv_a) if sid == "aaa" else str(conv_b))
    try:
        act_agent = file_activity.activity_for_agent(_Agent("Agent 1", "aaa"))
        check("map: activity_for_agent parses a Claude agent's transcript",
              act_agent is not None and len(act_agent.files) == 2)

        from PySide6.QtWidgets import QApplication
        QApplication.instance() or QApplication([])
        ws = Workspace(id="mapws", name="Map WS", project_path=str(tmp),
                       agents=[_Agent("Agent 1", "aaa"), _Agent("Agent 2", "bbb")])

        # -- 2. window model + paint (Tree is the only view) ------------
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor
        from app.fsopen import open_with as _open_with
        from app.widgets import agent_file_map as afm
        from app.widgets.agent_file_map import _opaque_tint
        win = AgentFileMapWindow()
        win.set_workspace(ws)
        canvas = win.canvas
        check("map: two agent hubs in the model", len(canvas._agents) == 2)
        a1 = next(a for a in canvas._agents if a.name == "Agent 1")
        check("map: Agent 1 shows its sub-agent satellite", len(a1.subs) == 1)
        # tree view builds a folder+file hierarchy from the union of touched files
        rows = canvas._tree_rows
        check("map: tree builds a folder+file hierarchy",
              any(r.is_dir for r in rows) and any(not r.is_dir for r in rows))
        shared_row = next((r for r in rows if not r.is_dir and r.file
                           and r.file.basename == "shared.py"), None)
        check("map: a shared file is one row with an owner per agent",
              shared_row is not None and len(shared_row.file.owners) == 2)
        check("map: only_a.py appears as its own row",
              any((not r.is_dir) and r.file and r.file.basename == "only_a.py"
                  for r in rows))
        check("map: canvas paints headlessly without error",
              not canvas.grab().isNull())

        # -- file-type icons (a glyph before each filename by extension) --
        from app.filetypes import DEFAULT_ICON as _DEFAULT_ICON
        from app.filetypes import file_icon as _file_icon
        check("map: image extension maps to a distinct icon",
              _file_icon("photo.PNG") == _file_icon("x.jpg")
              and _file_icon("photo.PNG") != _DEFAULT_ICON)
        check("map: code extensions share the code icon",
              _file_icon("a.js") == _file_icon("b.rs") != _DEFAULT_ICON)
        check("map: python has its own icon distinct from generic code",
              _file_icon("m.py") not in (_DEFAULT_ICON, _file_icon("a.js")))
        check("map: config/doc/image icons are all distinct kinds",
              len({_file_icon("c.json"), _file_icon("d.md"),
                   _file_icon("i.png"), _file_icon("s.css")}) == 4)
        check("map: unknown extension falls back to the generic icon",
              _file_icon("weird.zzz") == _DEFAULT_ICON
              and _file_icon("NoExtension") == _DEFAULT_ICON)

        # -- interactions (agents drag; the file tree is structural) -----
        a1v = next(a for a in canvas._agents if a.name == "Agent 1")
        # drag: an override placement survives the next model refresh
        canvas._overrides[afm._aid(a1v)] = QPointF(700, 320)
        win.refresh()
        a1v = next(a for a in win.canvas._agents if a.name == "Agent 1")
        check("map: dragged agent position persists across refresh",
              a1v.center.x() == 700 and a1v.center.y() == 320)
        check("map: hit-test finds the agent hub at its center",
              canvas._hit(a1v.center)[0] == "agent")
        shared_row = next(r for r in canvas._tree_rows if not r.is_dir
                          and r.file and r.file.basename == "shared.py")
        check("map: hit-test finds a file row",
              canvas._hit(shared_row.rect.center())[0] == "file")
        # zoom changes the scale and grows the scrollable canvas
        before = canvas.minimumSize().width()
        canvas._set_scale(2.0)
        check("map: Ctrl+wheel zoom scales the canvas",
              canvas._scale == 2.0 and canvas.minimumSize().width() > before)
        # clicking an agent relays agentActivated up with the workspace id
        relayed = []
        win.agentActivated.connect(lambda w, a: relayed.append((w, a)))
        win._on_agent_activated("agent-7")
        check("map: agent click relays (ws_id, agent_id) to the app",
              relayed == [("mapws", "agent-7")])
        # switching workspace resets manual placement
        canvas.reset_view()
        check("map: reset_view clears manual overrides", canvas._overrides == {})
        check("map: agent hub fill is opaque",
              _opaque_tint(QColor(0, 255, 0)).alpha() == 255)
        _open_with(str(tmp / "nope.py"))   # missing file -> must not raise
        check("map: open-with no-ops safely on a missing file", True)

        # regression: a file whose name collides with a folder name once threw
        # in the tree builder, and because the throw landed on the toggle's slot
        # (after the mode flip, before repaint) the view silently FROZE — the
        # "clicking Tree does nothing" bug. The builder must never raise.
        from app.widgets.agent_file_map import _build_tree_rows, _FileVis
        collide = [_FileVis(path=r"C:\proj\a\mod", basename="mod", edited=True),
                   _FileVis(path=r"C:\proj\a\mod\sub.py", basename="sub.py", edited=False),
                   _FileVis(path=r"C:\proj\z\other.py", basename="other.py", edited=True)]
        try:
            crows = _build_tree_rows(collide)
            crash = False
        except Exception:
            crash, crows = True, []
        check("map: tree builder survives file/folder name collision",
              not crash and len(crows) >= 3)

        # -- Write/Read line filters (header toggles) ---------------------
        check("map: edges default to both edited+read visible",
              canvas._show_edited and canvas._show_read)
        win.read_btn.setChecked(False)
        check("map: unchecking Read hides read-only edges only",
              canvas._show_read is False and canvas._show_edited is True
              and canvas._edge_visible(True) and not canvas._edge_visible(False))
        win.write_btn.setChecked(False)
        check("map: unchecking Write hides edited edges too (independent)",
              not canvas._edge_visible(True) and not canvas._edge_visible(False))
        check("map: canvas still paints with lines filtered off",
              not canvas.grab().isNull())
        win.read_btn.setChecked(True); win.write_btn.setChecked(True)
        check("map: re-checking restores both line kinds",
              canvas._edge_visible(True) and canvas._edge_visible(False))
        win.close()

        # -- tree agents are vertically centered (middle-right) -----------
        from app.widgets.agent_file_map import AgentFileMapCanvas, _AgentVis
        c2 = AgentFileMapCanvas()
        many = [_FileVis(path=os.path.join(str(tmp), "pkg", f"f{i}.py"),
                         basename=f"f{i}.py", edited=(i % 2 == 0),
                         owners=[(0, i % 2 == 0)]) for i in range(16)]
        avs2 = [_AgentVis(agent_id="x", name="A", role="", state="idle",
                          is_claude=True, truncated=False),
                _AgentVis(agent_id="y", name="B", role="", state="idle",
                          is_claude=True, truncated=False)]
        c2.set_model(avs2, many)
        ys = sorted(a.center.y() for a in c2._agents)
        tree_bottom = afm._TREE_TOP + len(c2._tree_rows) * afm._TREE_ROW
        mid_agents = (ys[0] + ys[-1]) / 2
        mid_tree = (afm._TREE_TOP + tree_bottom) / 2
        check("map: tree agents are vertically centered, not pinned top-right",
              abs(mid_agents - mid_tree) < 1.0
              and ys[0] > afm._TREE_TOP + afm._HUB_R + 10)
        step = ys[1] - ys[0]
        check("map: stacked agent bubbles do not overlap (gap > hub diameter)",
              step > 2 * afm._HUB_R)
        c2.deleteLater()

        # -- 3. header button wiring -------------------------------------
        from app.widgets.workspace_page import WorkspacePage
        page = WorkspacePage(ws)
        fired = []
        page.mapRequested.connect(fired.append)
        page.map_btn.click()
        check("map: header button emits mapRequested with the ws id",
              fired == ["mapws"])
        page.deleteLater()
    finally:
        transcripts.transcript_path = real_tp
    shutil.rmtree(tmp, ignore_errors=True)


def test_lifecycle_e2e():
    """THE journey that kept losing user data, end to end with a REAL Claude
    agent: talk -> graceful close -> reopen -> the SAME conversation is back
    on the same card (pinned --resume), and a transcript backup exists.
    Skipped (not failed) when the claude CLI isn't installed."""
    import uuid as _uuid
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import providers, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    if not (HAS_CONPTY and providers.detected("claude")):
        print("[SKIP] lifecycle e2e: claude CLI or ConPTY unavailable")
        return

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_until(pred, timeout_ms, step=100):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-e2e-"))
    store = SessionStore(path=tmp / "s.json")
    marker = f"PINEAPPLE-{_uuid.uuid4().hex[:8]}"

    import re as _re
    _csi = _re.compile(r"\x1b\[[0-9;?<>=]*[@-~]|\x1b[()][AB0]|\x1b\][^\x07\x1b]*\x07?")

    def screen_text(a):
        # raw stream positions words individually; strip escapes to match text
        return _csi.sub("", a.pty_replay()).lower()

    # --- session 1: real claude, say the marker --------------------------
    win = create_main_window(store)
    win.show(); pump(150)
    ws = win.manager.workspaces[0]
    spec = build_spec(AgentKind.CLAUDE, "E2E", cwd=str(tmp), pty=True,
                      model="haiku")
    agent = win.manager.add_terminal(ws.id, spec, autostart=True)
    sid = agent.spec.session_id
    check("e2e: launch minted a pinned session id", bool(sid))
    # a brand-new folder shows the trust dialog first — accept it; the task
    # is queued and must NOT be typed into the dialog
    agent.deliver_task(
        f"Reply with exactly the word {marker} and nothing else.")
    check("e2e: trust dialog appeared for the fresh folder",
          wait_until(lambda: "trust" in screen_text(agent)
                     and "folder" in screen_text(agent), 45000))
    check("e2e: queued task not typed into the trust dialog",
          not agent._prompt_ready)
    agent.write("\r")  # accept "Yes, I trust this folder"
    check("e2e: task delivered once the real prompt was ready",
          wait_until(lambda: agent._prompt_ready, 45000))
    check("e2e: claude answered the marker prompt",
          wait_until(lambda: screen_text(agent).count(marker.lower()) >= 2,
                     120000),
          screen_text(agent)[-300:])
    # the transcript flush lags the on-screen answer (file first, content
    # later) — wait until the marker is IN the file before closing, or the
    # close-time backup snapshots a partial conversation
    tpath = Path(transcripts.transcript_path(str(tmp), sid))

    def transcript_has_marker():
        try:
            return marker in tpath.read_text(encoding="utf-8",
                                             errors="replace")
        except OSError:
            return False
    check("e2e: transcript persisted under the pinned id",
          wait_until(transcript_has_marker, 45000), str(tpath))
    win.close(); pump(600)  # graceful close: save + transcript backup

    on_disk = (tmp / "s.json").read_text(encoding="utf-8")
    check("e2e: close persisted the pinned id + running state",
          sid in on_disk and '"running": true' in on_disk)
    check("e2e: close snapshotted the transcript",
          (tmp / "transcripts" / f"{sid}.jsonl").exists()
          and marker in (tmp / "transcripts" / f"{sid}.jsonl")
          .read_text(encoding="utf-8", errors="replace"))

    # --- session 2: reopen -> the SAME conversation returns --------------
    win2 = create_main_window(store)
    win2.show(); pump(150)
    agent2 = win2.manager.workspaces[0].agents[-1]
    check("e2e: reopened agent kept its pin", agent2.spec.session_id == sid)
    check("e2e: reopened agent resumes, not --continue",
          agent2.spec.resume
          and "--resume" in agent2.spec.effective_args())
    win2.autostart_active_workspace()
    check("e2e: THE SAME conversation came back on the card",
          wait_until(lambda: marker.lower() in screen_text(agent2), 90000),
          screen_text(agent2)[-300:])
    check("e2e: no silent fresh-fallback (pin unchanged)",
          agent2.spec.session_id == sid)
    win2.close(); pump(600)

    # tidy: remove the claude-side project dir this test created
    proj = os.path.join(os.path.expanduser("~"), ".claude", "projects",
                        transcripts.encode_project_dir(str(tmp)))
    shutil.rmtree(proj, ignore_errors=True)
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

    spec = build_spec(AgentKind.CLAUDE, "Pin", cwd=os.getcwd(), pty=True)
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
    legacy = build_spec(AgentKind.CLAUDE, "Legacy", cwd=os.getcwd(), pty=True)
    legacy.session_id = ""
    legacy.resume = True
    check("pin: legacy unpinned resume falls back to --continue",
          "--continue" in legacy.effective_args())

    # a failed resume (conversation gone) relaunches fresh under a NEW id
    gone = "11111111-1111-4111-8111-111111111111"
    spec2 = build_spec(AgentKind.CLAUDE, "Fb", cwd=os.getcwd(), pty=True)
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
    spec3 = build_spec(AgentKind.CLAUDE, "Trust", cwd=os.getcwd(), pty=True)
    a3 = TerminalAgent(spec3)
    a3._on_pty_output("", "Do you trust\x1b[20Gthis\x1b[26Gfolder?\x1b[?2004h")
    check("pin: trust dialog does not trip prompt-ready",
          not a3._prompt_ready)
    a3._on_pty_output("", "\x1b[2G? for shortcuts \x1b[38;2;1;2;3m- more")
    check("pin: input-box footer marks Claude readiness", a3._prompt_ready)
    # split across chunks: the rolling tail must still assemble the footer
    a5 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Split", cwd=os.getcwd(),
                                  pty=True))
    a5._on_pty_output("", "\x1b[?2004h? for sho")
    a5._on_pty_output("", "rtcuts")
    check("pin: footer split across chunks still detected", a5._prompt_ready)
    # non-Claude ptys (PSReadLine etc.) keep the paste-enable signal
    a4 = TerminalAgent(build_spec(AgentKind.POWERSHELL, "Sh", cwd=os.getcwd(),
                                  pty=True))
    a4._on_pty_output("", "\x1b[?2004h")
    check("pin: non-claude pty still ready on paste-enable", a4._prompt_ready)


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


def test_review_hardening_fixes():
    """Regressions for the six defects a whole-app code review surfaced:
      #1 the SessionStart hook is re-armed on restore even when the orchestrator
         bridge is disabled (the hook is bridge-independent);
      #2 terminal_view.feed caps _esc_carry so an unterminated OSC can't swallow
         the stream / grow memory without bound;
      #3 the orchestrator pipe drops a client that streams bytes with no newline;
      #4 the 350 ms task-submit Enter is generation-guarded so a restart in the
         window can't fire a stray CR into a fresh TUI;
      #5 a scrolled-back terminal view stays anchored after history saturates;
      #6 spawn_worker persists task/assignment immediately (no debounce loss)."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import session_hook
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import AssignmentState, TerminalAgent
    from app.workspace_manager import WorkspaceManager
    from app.widgets.terminal_view import (TerminalView, HISTORY_LINES,
                                           _CountingDeque, _MAX_ESC_CARRY)
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def stub_worker():
        writes = []
        w = type("W", (), {
            "write": lambda s, d: (writes.append(d), True)[1],
            "is_running": lambda s: True, "start": lambda s: None,
            "restart": lambda s: None, "dispose": lambda s: None,
            "send_line": lambda s, t: True})()
        return w, writes

    # -- #1: hook re-armed on restore even with the bridge disabled -----------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-harden-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    try:
        ws = win.manager.workspaces[0]
        spec = build_spec(AgentKind.CLAUDE, "Restored", cwd=str(tmp), pty=True)
        agent = win.manager.add_terminal(ws.id, spec, autostart=False)
        win.bridge.enabled = False          # simulate no Qt network / listen fail
        agent.spec.settings_path = ""        # simulate an un-armed restored agent
        agent.spec.env.pop(session_hook.AGENT_ID_ENV, None)
        win._rearm_agent_configs()
        check("harden: hook re-armed on restore even when the bridge is disabled",
              agent.spec.settings_path == win._hook_settings_path
              and agent.spec.env.get(session_hook.AGENT_ID_ENV) == agent.id)
    finally:
        win.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # -- #2: _esc_carry is bounded -------------------------------------------
    v = TerminalView()
    v.feed("\x1b[")  # a short, genuine partial escape is carried
    check("harden: a short partial escape is still carried",
          v._esc_carry == "\x1b[")
    v.feed("m")      # completes it
    v._esc_carry = ""
    v.feed("\x1b]0;" + "A" * (_MAX_ESC_CARRY + 100))  # unterminated OSC
    check("harden: an over-long unterminated OSC is not carried unbounded",
          len(v._esc_carry) <= _MAX_ESC_CARRY)
    v.feed("B" * 8000)
    check("harden: esc_carry stays bounded across chunks",
          len(v._esc_carry) <= _MAX_ESC_CARRY)

    # -- #3: oversized no-newline pipe request is dropped ---------------------
    from app.orchestrator_bridge import OrchestratorBridge, _MAX_REQUEST_BYTES
    br_mgr = WorkspaceManager()
    bridge = OrchestratorBridge(br_mgr, active_ws=lambda: "", parent=None)

    class _FakeBA:
        def __init__(self, d): self._d = d
        def data(self): return self._d

    class _FakeSock:
        def __init__(self, chunk): self._chunk = chunk; self.aborted = False
        def readAll(self):
            d, self._chunk = self._chunk, b""
            return _FakeBA(d)
        def abort(self): self.aborted = True

    sock = _FakeSock(b"x" * (_MAX_REQUEST_BYTES + 16))  # no newline, runaway
    bridge._buffers[sock] = bytearray()
    bridge._on_ready(sock)
    check("harden: an oversized newline-less pipe request is dropped",
          sock.aborted and sock not in bridge._buffers)

    # -- #4: task-submit Enter is generation-guarded --------------------------
    spec4 = build_spec(AgentKind.CLAUDE, "Submit", cwd=str(Path(tempfile.gettempdir())),
                       pty=True)
    a4 = TerminalAgent(spec4)
    w, writes = stub_worker()
    a4.worker = w
    a4._prompt_ready = True
    a4._write_task_to_pty("hello")   # schedules the delayed Enter
    pump(500)
    check("harden: the task-submit Enter fires in the normal case",
          "\r" in writes)
    writes.clear()
    a4._prompt_ready = True
    a4._write_task_to_pty("world")   # schedule again...
    a4.restart()                      # ...then restart inside the 350 ms window
    pump(500)
    check("harden: a restart in the submit window suppresses the stray Enter",
          "\r" not in writes)

    # -- #5: scrolled-back view stays anchored past the history cap -----------
    v5 = TerminalView()
    for i in range(HISTORY_LINES + 200):
        v5.feed(f"line{i}\r\n")
    top = v5.screen.history.top
    check("harden: terminal history uses the counting deque",
          isinstance(top, _CountingDeque))
    check("harden: history saturated at the cap (the drift-prone case)",
          len(top) == HISTORY_LINES)
    v5._scroll_offset = 40           # user scrolled back
    pushed_before = top.pushed
    v5.feed("A\r\nB\r\nC\r\n")       # more lines scroll into a full history
    grown = v5.screen.history.top.pushed - pushed_before
    check("harden: scrolled-back offset tracks pushes even after saturation",
          grown >= 1 and v5._scroll_offset == 40 + grown,
          (grown, v5._scroll_offset))

    # -- #6: spawn_worker persists immediately (no debounce loss window) ------
    tmp6 = Path(tempfile.mkdtemp(prefix="ai-hive-spawn-"))
    mgr = WorkspaceManager()
    saves = {"n": 0}
    mgr.save_now = lambda: saves.__setitem__("n", saves["n"] + 1)
    ws6 = mgr.create_workspace("W", project_path=str(tmp6))

    def fake_add(ws_id, spec, autostart=True):  # avoid launching a real claude
        a = TerminalAgent(spec)
        sw, _ = stub_worker()
        sw.is_running = lambda: False  # so deliver_task queues instead of typing
        a.worker = sw
        ws6.agents.append(a)
        return a

    mgr.add_terminal = fake_add
    spawned = mgr.spawn_worker(ws6.id, "do the thing", role="coder")
    check("harden: spawn_worker persists immediately via save_now",
          saves["n"] >= 1)
    check("harden: the spawned worker has its task + assignment set",
          spawned.current_task == "do the thing"
          and spawned.assignment == AssignmentState.WORKING)
    mgr2 = WorkspaceManager()   # fallback: no save_now -> debounced dirty
    dirty2 = {"n": 0}
    mgr2.dirty.connect(lambda: dirty2.__setitem__("n", dirty2["n"] + 1))
    mgr2._persist_now()
    check("harden: _persist_now falls back to dirty when no save_now is wired",
          dirty2["n"] >= 1)
    shutil.rmtree(tmp6, ignore_errors=True)


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

    check("wake: restored Claude carries one-shot resume (--continue)",
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


def test_pty_width_at_launch():
    """Every pty child is SPAWNED at its card's real width, in every workspace.

    The regression this guards, reported as "reopen AI Hive, scroll up in a
    restored conversation and the text is half width": a terminal's column
    count is not a cosmetic detail that can be corrected afterwards. The child
    WRAPS ITS OWN TEXT to whatever the pseudo-console reports, and a `--resume`
    launch dumps the whole past conversation the instant it boots, so lines
    broken for the wrong width stay broken for it forever -- pyte cannot reflow
    them, and neither can `TerminalCard._reproject_on_size`, which only
    re-projects the raw stream the child already hard-wrapped.

    Three separate holes let that happen at launch, all measured on this
    suite's own repro before the fix:

      * `PtyWorker` spawned at DEFAULT_COLS (100) and heard the real width
        ~250ms later, because `TerminalView` debounces its resize by 120ms;
      * `main()` calls `autostart_active_workspace()` the instant `show()`
        returns, where a window restoring MAXIMIZED still reports its
        restore-down geometry (1249x662 measured there, 1536x793 one
        `processEvents` later);
      * a workspace the user has not opened is NEVER laid out by
        `QStackedLayout`, so six of eight agents in a four-workspace hive ran
        their entire resumed conversation at 100 columns and only found out
        the truth when the user first clicked that workspace.

    `MainWindow.settle_layout` closes all three, and the width a child is given
    must then never change again."""
    import json
    import shutil
    import tempfile

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import pty_worker
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    if not HAS_CONPTY:
        check("pty width: SKIP (no pywinpty)", True)
        return

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-ptywidth-"))
    store = SessionStore(path=tmp / "session.json")

    # every width the pseudo-console is ever told, and the width each child is
    # SPAWNED at -- the two numbers the bug lived between
    spawned: list[tuple] = []
    widths: dict[str, list] = {}
    orig_resize = pty_worker.PtyWorker.resize
    orig_start = pty_worker.PtyWorker.start

    def traced_resize(self, rows, cols):
        before = (self.rows, self.cols)
        orig_resize(self, rows, cols)
        if (self.rows, self.cols) != before:
            widths.setdefault(id(self), []).append(self.cols)

    def traced_start(self):
        spawned.append((self.spec.name, self.cols))
        widths.setdefault(id(self), []).append(self.cols)
        return orig_start(self)

    pty_worker.PtyWorker.resize = traced_resize
    pty_worker.PtyWorker.start = traced_start
    try:
        # -- session 1: four workspaces, two pty agents each, all running ----
        win = create_main_window(store)
        win.show()
        pump(400)
        mgr = win.manager
        wss = [mgr.workspaces[0]]
        for nm in ("Two", "Three", "Four"):
            wss.append(mgr.create_workspace(nm, str(tmp)))
        for ws in wss:
            while len(ws.agents) < 2:
                mgr.add_terminal(ws.id, build_spec(
                    AgentKind.POWERSHELL, "A%d" % (len(ws.agents) + 1),
                    cwd=str(tmp), pty=True), autostart=False)
            for a in ws.agents:      # all eight RUNNING, so all eight restore
                if not a.is_running():
                    a.start()
        mgr.set_active(wss[0].id)
        pump(1200)
        win.close()
        pump(400)

        # a maximized window whose restore-down geometry is much smaller: the
        # shape that makes show() report a width the card will not keep
        data = json.loads((tmp / "session.json").read_text(encoding="utf-8"))
        data.setdefault("ui", {})["window"] = {"w": 900, "h": 600,
                                               "maximized": True}
        (tmp / "session.json").write_text(json.dumps(data), encoding="utf-8")

        spawned.clear()
        widths.clear()

        # -- session 2: main()'s exact order, no event loop in between -------
        win2 = create_main_window(SessionStore(path=tmp / "session.json"))
        win2.show()
        win2.autostart_active_workspace()
        pump(2500)

        default_cols = pty_worker.DEFAULT_COLS
        check("pty width: every restored child was actually started",
              len(spawned) == 8, spawned)
        check("pty width: none was spawned at the placeholder default",
              all(cols != default_cols for _n, cols in spawned), spawned)

        active = win2.manager.active_id
        check("pty width: the on-screen workspace is honest",
              all(c.terminal.screen.columns == c.agent.worker.cols
                  for c in win2._pages[active].cards),
              [(c.terminal.screen.columns, c.agent.worker.cols)
               for c in win2._pages[active].cards])
        # the half of the bug that never corrected itself: Qt lays out the
        # CURRENT page only, so these six used to sit at DEFAULT_COLS forever
        hidden = [c for ws in win2.manager.workspaces if ws.id != active
                  for c in win2._pages[ws.id].cards]
        check("pty width: ...and so is every workspace still off screen",
              hidden and all(c.terminal.screen.columns == c.agent.worker.cols
                             and c.terminal.screen.columns != default_cols
                             for c in hidden),
              [(c.agent.spec.name, c.terminal.screen.columns,
                c.agent.worker.cols) for c in hidden])
        check("pty width: no child was ever told two different widths",
              all(len(set(seen)) == 1 for seen in widths.values()),
              {k: v for k, v in widths.items() if len(set(v)) > 1})

        # -- and opening a workspace must not move its width ----------------
        others = [ws for ws in win2.manager.workspaces if ws.id != active]
        before = [(c.agent.id, c.agent.worker.cols)
                  for c in win2._pages[others[0].id].cards]
        win2.manager.set_active(others[0].id)
        pump(600)
        after = [(c.agent.id, c.agent.worker.cols)
                 for c in win2._pages[others[0].id].cards]
        check("pty width: opening a workspace does not re-wrap its terminals",
              before == after, (before, after))

        win2.close()
        pump(400)
    finally:
        pty_worker.PtyWorker.resize = orig_resize
        pty_worker.PtyWorker.start = orig_start
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
    on its own backstop timer."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    # -- the agent-side signal --------------------------------------------
    spec = build_spec(AgentKind.CLAUDE, "Booting", cwd=os.getcwd(), pty=True)
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
        AgentKind.POWERSHELL, "Boot", cwd=os.getcwd(), pty=True))
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


def test_resume_fallback():
    """A resume (--continue) launch that dies before the interactive prompt ever
    comes up (Claude prints 'No conversation found to continue' and exits) must
    relaunch ONCE, fresh — so the terminal is never left black. The regression
    behind a resumed Claude card that opened all-black and non-interactive."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    spec = build_spec(AgentKind.CLAUDE, "Resumed", cwd=os.getcwd(),
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
    spec2 = build_spec(AgentKind.CLAUDE, "Coder", cwd=os.getcwd(), pty=True)
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
    spec3 = build_spec(AgentKind.CLAUDE, "Stopped", cwd=os.getcwd(), pty=True)
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


def test_plan_usage():
    """The top-bar Claude plan-usage readout: parsing, the badge line, the
    limit-reached edges other features hook into, and the two rules that keep
    it cheap — a reading NEVER marks the session dirty, and polling is opt-in
    so this suite never touches the network or the user's real account."""
    import json as _json
    import time as _time
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu

    QApplication.instance() or QApplication([])
    now = _time.time()

    def limit(key, pct, resets=None):
        return cu.Limit(key=key, label=cu._LABELS[key], short=cu._SHORT[key],
                        percent=pct, resets_at=resets)

    # --- parsing the real payload shape (captured from /api/oauth/usage) ---
    payload = {
        "five_hour": {"utilization": 21.0,
                      "resets_at": "2026-08-02T12:29:59.983234+00:00",
                      "limit_dollars": None},
        "seven_day": None,               # Pro has no weekly window
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": False},
        "limits": [{"kind": "session", "percent": 21}],
    }
    lims = cu.parse_utilization(payload)
    check("plan-usage: parses five_hour utilization", len(lims) == 1
          and lims[0].key == "five_hour" and lims[0].percent == 21.0)
    check("plan-usage: resets_at parsed to epoch seconds",
          abs(lims[0].resets_at - 1785673799.983234) < 1.0)
    check("plan-usage: null windows skipped, not reported as 0%",
          all(l.key != "seven_day" for l in lims))
    check("plan-usage: garbage payload yields no limits",
          cu.parse_utilization({"five_hour": "nonsense"}) == ()
          and cu.parse_utilization({}) == ())
    check("plan-usage: epoch resets_at also accepted",
          cu.parse_utilization(
              {"five_hour": {"utilization": 5, "resets_at": 1785673799}}
          )[0].resets_at == 1785673799.0)

    # --- headline picks the most-constrained window ---
    multi = cu.Usage(limits=(limit("five_hour", 21.0), limit("seven_day", 64.0)))
    check("plan-usage: headline is the highest-utilization window",
          cu.headline(multi).key == "seven_day")
    check("plan-usage: headline of an empty reading is None",
          cu.headline(cu.Usage()) is None and cu.headline(None) is None)

    # --- five_hour/weekly: the two SEPARATE selectors the two pills use.
    # Unlike headline, neither ever falls back to the other window - that
    # would let one pill silently show the other's number.
    check("plan-usage: five_hour never falls back to the 7d window",
          cu.five_hour(multi).key == "five_hour")
    check("plan-usage: weekly never falls back to the 5h window",
          cu.weekly(multi).key == "seven_day")
    max_plan = cu.Usage(limits=(limit("five_hour", 10.0),
                                limit("seven_day_opus", 30.0),
                                limit("seven_day_sonnet", 55.0)))
    check("plan-usage: weekly picks the most-constrained 7d window on Max",
          cu.weekly(max_plan).key == "seven_day_sonnet")
    check("plan-usage: five_hour/weekly of an empty reading are None",
          cu.five_hour(cu.Usage()) is None and cu.weekly(cu.Usage()) is None
          and cu.five_hour(None) is None and cu.weekly(None) is None)
    check("plan-usage: five_hour is None when the plan has no 5h window",
          cu.five_hour(cu.Usage(limits=(limit("seven_day", 10.0),))) is None)
    check("plan-usage: weekly is None when the plan has no 7d window",
          cu.weekly(cu.Usage(limits=(limit("five_hour", 10.0),))) is None)

    # --- the badge line: countdown FIRST, then wall-clock, in local time ---
    line = cu.format_limit(limit("five_hour", 21.0, now + 4800), now=now)
    check("plan-usage: line reads '21% used, resets in 1h20m at HH:MM'",
          line.startswith("21% used, resets in 1h20m at ")
          and len(line.split(" at ")[1]) == 5, line)
    check("plan-usage: local wall-clock, not UTC",
          line.endswith(_time.strftime("%H:%M", _time.localtime(now + 4800))))
    check("plan-usage: window named only when the plan has several",
          cu.format_limit(limit("seven_day", 64.0, now + 600), now=now,
                          with_label=True).startswith("7d 64% used"))
    check("plan-usage: a spent window spells out 'limit reached'",
          cu.format_limit(limit("five_hour", 100.0, now + 600),
                          now=now).startswith("limit reached, resets in 10m"))
    check("plan-usage: no reset time degrades to the bare percent",
          cu.format_limit(limit("five_hour", 21.0)) == "21% used")
    check("plan-usage: countdown formats scale",
          (cu.format_countdown(4800), cu.format_countdown(600),
           cu.format_countdown(30)) == ("1h20m", "10m", "30s"))
    # --- format_countdown_dh: days+hours only, for the 7-day pill ---
    check("plan-usage: dh countdown drops minutes at every scale",
          (cu.format_countdown_dh(6 * 86400 + 23 * 3600 + 45 * 60),
           cu.format_countdown_dh(6 * 86400),
           cu.format_countdown_dh(13 * 3600 + 45 * 60),
           cu.format_countdown_dh(1800))
          == ("6d23h", "6d", "13h", "<1h"))
    check("plan-usage: format_limit(days_only=True) uses the dh countdown",
          cu.format_limit(limit("seven_day", 40.0, now + 6 * 86400 + 3600),
                          now=now, days_only=True).startswith(
                              "40% used, resets in 6d1h at "))
    check("plan-usage: format_limit(days_only=False) keeps minutes",
          cu.format_limit(limit("seven_day", 40.0, now + 4800), now=now,
                          days_only=False).startswith(
                              "40% used, resets in 1h20m at "))
    check("plan-usage: age formats scale",
          (cu.format_since(2), cu.format_since(42), cu.format_since(180),
           cu.format_since(7200)) == ("just now", "42s ago", "3m ago", "2h ago"))

    # --- blocked/resets_at: the hook other features build on ---
    spent = cu.Usage(limits=(limit("five_hour", 100.0, now + 900),))
    check("plan-usage: blocked reports the spent window",
          spent.blocked is not None and spent.blocked.key == "five_hour")
    check("plan-usage: blocked carries when it frees up",
          spent.resets_at == now + 900)
    check("plan-usage: headroom means not blocked",
          cu.Usage(limits=(limit("five_hour", 99.0, now),)).blocked is None)

    # --- credentials are read-only, and a dead token never hits the wire ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-usage-"))
    creds = tmp / ".credentials.json"
    creds.write_text(_json.dumps({"claudeAiOauth": {
        "accessToken": "not-a-real-token", "subscriptionType": "pro",
        "expiresAt": int((now - 3600) * 1000)}}), encoding="utf-8")
    before = creds.read_bytes()
    old_env = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    try:
        expired = cu.fetch()
        check("plan-usage: expired token short-circuits (no request)",
              expired.error == "expired" and not expired.limits)
        check("plan-usage: credentials file never rewritten",
              creds.read_bytes() == before)
        creds.unlink()
        check("plan-usage: missing credentials report no-auth, never raise",
              cu.fetch().error == "no-auth")
        check("plan-usage: missing cache returns None, never raises",
              cu.read_cached() is None)
        # --- the on-disk cache is parsed by the very same parser ---
        (tmp / ".claude.json").write_text(_json.dumps({
            "cachedUsageUtilization": {"fetchedAtMs": int((now - 90) * 1000),
                                       "utilization": payload}}),
            encoding="utf-8")
        cached = cu.read_cached()
        check("plan-usage: cache seed parsed, marked as cached",
              cached is not None and cached.source == "cache"
              and cached.limits[0].percent == 21.0)
        check("plan-usage: cache keeps Claude's timestamp, not now",
              abs(cached.fetched_at - (now - 90)) < 2.0)
    finally:
        if old_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_env

    # --- window wiring: badge, edges, persistence ---
    from main import create_main_window, setup_application
    from app.session_store import SessionStore
    app = QApplication.instance()
    setup_application(app)
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    # wide enough that the top bar's overflow collapse never kicks in - this
    # test is about pill content/visibility logic, not the responsive layout
    win.resize(2400, 900)
    app.processEvents()
    badge = win.top_bar.usage_badge
    weekly_badge = win.top_bar.usage_weekly_badge

    check("plan-usage: polling is opt-in, so the suite never fetches",
          not win._usage_timer.isActive() and win.plan_usage() is None)
    check("plan-usage: badge hidden until a reading arrives",
          not badge.isVisible() and not badge.has_reading())

    good = cu.Usage(limits=(limit("five_hour", 21.0, now + 4800),),
                    fetched_at=now, plan="pro")
    win._on_usage_ready(good)
    app.processEvents()
    check("plan-usage: reading shows the badge with the full line, labelled "
          "5h so it's tellable apart from the 7d pill beside it",
          badge.isVisible()
          and badge._text.startswith("5h 21% used, resets in"))
    check("plan-usage: tooltip carries plan, every window, and the age",
          "Pro plan" in badge.toolTip() and "Current session" in badge.toolTip()
          and "Updated" in badge.toolTip())
    check("plan-usage: plan_usage() exposes the reading",
          win.plan_usage() is good)
    check("plan-usage: the 5h reading has no 7d window, so the weekly pill "
          "stays empty",
          not weekly_badge.has_reading())

    # --- the separate 7d pill: its own window, its own days+hours format ---
    weekly_reading = cu.Usage(
        limits=(limit("five_hour", 21.0, now + 4800),
               limit("seven_day", 40.0, now + 6 * 86400 + 3600)),
        fetched_at=now, plan="max")
    win._on_usage_ready(weekly_reading)
    app.processEvents()

    # Not an exact-string match: the widget formats against the REAL clock
    # (unlike the pure format_limit() checks above, which pin `now`), and
    # this whole test function runs long enough that a minute can genuinely
    # tick over between the reading landing and the assertion running. So
    # this checks the SHAPE (5h keeps minutes, 7d drops them), which is the
    # thing days_only actually controls, rather than the exact digits.
    def _countdown(text):
        return text.split("resets in ")[1].split(" at ")[0]

    five_cd = _countdown(badge._text)
    weekly_cd = _countdown(weekly_badge._text)
    check("plan-usage: the 5h pill still shows the 5h window, to the minute",
          badge._text.startswith("5h 21% used, resets in")
          and five_cd.endswith("m"))
    check("plan-usage: the 7d pill shows the 7d window, days+hours only "
          "(no minutes)",
          weekly_badge._text.startswith("7d 40% used, resets in")
          and "m" not in weekly_cd
          and ("d" in weekly_cd or weekly_cd.endswith("h")
               or weekly_cd == "<1h"))
    check("plan-usage: the two pills never share a window",
          badge.window == "five_hour" and weekly_badge.window == "weekly")
    win._on_usage_ready(good)
    app.processEvents()

    # a reading is TRANSIENT: it must never schedule a save (this polls every
    # minute forever; wiring it to dirty would thrash session.json)
    win._save_timer.stop()
    win._on_usage_ready(cu.Usage(limits=(limit("five_hour", 22.0, now + 4700),),
                                 fetched_at=now, plan="pro"))
    check("plan-usage: a reading never marks the session dirty",
          not win._save_timer.isActive())

    # edges: rising once, level-stable, falling once
    seen = {"hit": 0, "clear": 0, "limit": None}
    win.planLimitReached.connect(
        lambda l: seen.update(hit=seen["hit"] + 1, limit=l))
    win.planLimitCleared.connect(lambda: seen.update(clear=seen["clear"] + 1))
    blocked = cu.Usage(limits=(limit("five_hour", 100.0, now + 120),),
                       fetched_at=now, plan="pro")
    win._on_usage_ready(blocked)
    win._on_usage_ready(blocked)          # level, not a new edge
    check("plan-usage: planLimitReached fires once on the rising edge",
          seen["hit"] == 1 and seen["clear"] == 0)
    check("plan-usage: the edge carries the reset time",
          seen["limit"] is not None and seen["limit"].resets_at == now + 120)
    check("plan-usage: blocked badge reads 'limit reached'",
          badge._text.startswith("5h limit reached, resets in"))
    check("plan-usage: an extra poll is armed for just after the reset",
          win._usage_reset_timer.isActive()
          and win._usage_reset_timer.remainingTime() > 120000)
    win._on_usage_ready(good)
    check("plan-usage: planLimitCleared fires once on the falling edge",
          seen["clear"] == 1 and seen["hit"] == 1)
    check("plan-usage: reset poll disarmed once there is headroom",
          not win._usage_reset_timer.isActive())
    # a weekly window can reset days out; that is the minute poll's job, not a
    # multi-day QTimer (whose interval is 32-bit anyway)
    win._on_usage_ready(cu.Usage(limits=(limit("seven_day", 100.0,
                                               now + 3 * 86400),),
                                 fetched_at=now, plan="max"))
    check("plan-usage: a far-off reset is left to the ordinary poll",
          not win._usage_reset_timer.isActive())
    win._on_usage_ready(good)

    # --- the danger zone: nearly spent AND work under way => poll faster ---
    # A minute of blindness at 95% is a minute of agents parked on a banner
    # nobody noticed, because every reaction to a cut-off starts from a READING.
    from app.widgets.main_window import USAGE_POLL_MS, USAGE_URGENT_POLL_MS
    from app.process_worker import AgentKind, build_spec
    hot = cu.Usage(limits=(limit("five_hour", 95.0, now + 600),),
                   fetched_at=now, plan="pro")
    win._on_usage_ready(hot)
    check("plan-usage: nearly spent but nobody working stays on the slow poll",
          win._usage_timer.interval() == USAGE_POLL_MS)

    ws_hot = win.manager.create_workspace("usage-hot", str(tmp))
    agent_hot = win.manager.add_terminal(
        ws_hot.id, build_spec(AgentKind.CMD, "Hot", cwd=str(tmp)),
        autostart=False)
    agent_hot._busy = True            # what _mark_busy sets on an output burst
    check("plan-usage: nearly spent + an agent working polls faster",
          (win._on_usage_ready(hot),
           win._usage_timer.interval() == USAGE_URGENT_POLL_MS)[-1])
    check("plan-usage: the tick follows the work without a fetch",
          (setattr(agent_hot, "_busy", False), win._tick_usage(),
           win._usage_timer.interval() == USAGE_POLL_MS)[-1])
    agent_hot._busy = True
    win._tick_usage()
    check("plan-usage: below the urgent mark the work doesn't matter",
          (win._on_usage_ready(good),
           win._usage_timer.interval() == USAGE_POLL_MS)[-1])
    # a spent window is the reset poll's job, not a reason to hammer the endpoint
    win._on_usage_ready(cu.Usage(limits=(limit("five_hour", 100.0, now + 120),),
                                 fetched_at=now, plan="pro"))
    check("plan-usage: an already-spent window drops back to the slow poll",
          win._usage_timer.interval() == USAGE_POLL_MS)
    # and being rate-limited must still win over the urgent rate
    win._on_usage_ready(hot)
    win._on_usage_ready(cu.Usage(error="http 429"))
    check("plan-usage: a 429 backs off even in the danger zone",
          win._usage_timer.interval() > USAGE_URGENT_POLL_MS
          and win._usage_backoff == 1)
    win._on_usage_ready(hot)
    check("plan-usage: a good reading clears the backoff back to urgent",
          win._usage_timer.interval() == USAGE_URGENT_POLL_MS)
    agent_hot._busy = False
    win.manager.remove_workspace(ws_hot.id)
    win._on_usage_ready(good)
    win._save_timer.stop()

    # a failed poll keeps the last good number on screen, greyed
    win._on_usage_ready(cu.Usage(error="urlerror"))
    check("plan-usage: a failed poll keeps the last number, marked stale",
          badge._text.startswith("5h 21% used") and badge._stale)

    # visibility preference persists; toggling it IS a save (a UI preference)
    win._on_usage_tracker_toggled("claude_five_hour", False)
    app.processEvents()
    check("plan-usage: closing the pill removes it from the bar",
          not badge.isVisible())
    check("plan-usage: a later reading cannot resurrect a closed pill",
          (win._on_usage_ready(good), app.processEvents(),
           not badge.isVisible())[-1])
    payload_ui = win._session_payload()["ui"]
    check("plan-usage: preference persisted under ui.usage_trackers",
          payload_ui["usage_trackers"]["claude_five_hour"] is False
          and payload_ui["usage_trackers"]["gemini_weekly"] is True)
    check("plan-usage: the legacy usage_visible mirror is derived, not stale",
          payload_ui["usage_visible"] is True)
    win.close()

    win2 = create_main_window(store)
    win2.show()
    app.processEvents()
    check("plan-usage: preference restored on reopen",
          win2.top_bar.usage_trackers()["claude_five_hour"] is False)
    check("plan-usage: default is ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")
          ).top_bar.usage_trackers()["claude_five_hour"])
    win2.close()

    # no Claude login at all: hide for good rather than show an empty pill
    win3 = create_main_window(SessionStore(path=tmp / "noauth.json"))
    win3.show()
    win3._usage_timer.start()
    win3._on_usage_ready(cu.Usage(error="no-auth"))
    app.processEvents()
    check("plan-usage: no-auth hides both Claude pills and stops polling",
          not win3.top_bar.usage_badge.isVisible()
          and not win3.top_bar.usage_weekly_badge.isVisible()
          and not win3._usage_timer.isActive())
    win3.close()

    # A poll that fails with NO earlier reading used to leave a hole in the bar
    # — reported live as "did you delete the usage readout?" after a restart hit
    # an http 429 (and CLI 2.1.220 no longer writes the cachedUsageUtilization
    # seed that used to paint a number instantly). It must say so instead.
    win4 = create_main_window(SessionStore(path=tmp / "unreadable.json"))
    win4.show()
    win4._usage_timer.start()
    b4 = win4.top_bar.usage_badge
    win4._on_usage_ready(cu.Usage(error="http 429"))
    app.processEvents()
    check("plan-usage: a failure with no reading shows the can't-read pill",
          b4.isVisible() and "unreadable" in b4._text
          and "click to refresh" in b4._text)
    check("plan-usage: the can't-read pill is not mistaken for a reading",
          not b4.has_reading() and b4.has_content()
          and win4.plan_usage() is None)
    check("plan-usage: its tooltip names the failure and the way out",
          "http 429" in b4.toolTip() and "Click to try again" in b4.toolTip())
    check("plan-usage: the can't-read pill paints without a limit to draw",
          not b4.grab().isNull())
    check("plan-usage: polling continues (only no-auth is terminal)",
          win4._usage_timer.isActive())
    # a click is the user asking NOW: it must not be left parked behind the
    # backoff a run of 429s just wound up to
    check("plan-usage: repeated 429s back the poll off",
          win4._usage_timer.interval() > 60000)
    win4._on_usage_refresh()
    check("plan-usage: a manual refresh clears the 429 backoff",
          win4._usage_backoff == 0 and win4._usage_timer.interval() == 60000)
    # and a real number supersedes the error pill entirely
    win4._on_usage_ready(good)
    app.processEvents()
    check("plan-usage: a later reading replaces the can't-read pill",
          b4.isVisible() and b4.has_reading()
          and b4._text.startswith("5h 21% used") and not b4._unreadable)
    # an error is not a reason to force the readout back onto a bar the user
    # deliberately cleared
    win4._on_usage_tracker_toggled("claude_five_hour", False)
    win4.top_bar.note_usage_error("http 429")
    app.processEvents()
    check("plan-usage: a closed readout stays closed when a poll fails",
          not b4.isVisible())
    win4.close()


def test_usage_trackers_preference():
    """The per-pill usage picker: the X that closes one readout, the + that
    brings it back, and the preference that remembers.

    The bar used to carry one boolean for all readouts, on a right-click item.
    A user who runs only Claude had to look at two Gemini pills that can never
    say anything (and pay a ~3s subprocess a minute for them), or lose the
    Claude ones too. The preference is now per pill (Claude 5h, Claude 7d,
    Gemini 5h, Gemini 7d - four in all), and the + button is the single
    control - a master toggle sitting on top of four checkboxes is two
    controls for one setting, and a pill checked in one but hidden by the other
    is not explainable.
    """
    import json as _json
    import pathlib
    import tempfile
    import time as _time
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu
    from app.session_store import SessionStore
    from app.widgets.main_window import (USAGE_TRACKER_KEYS,
                                         USAGE_TRACKER_LABELS)
    from main import create_main_window

    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-trackers-"))
    now = _time.time()
    good = cu.Usage(limits=(cu.Limit(key="five_hour",
                                     label=cu._LABELS["five_hour"],
                                     short=cu._SHORT["five_hour"],
                                     percent=21.0, resets_at=now + 4800),),
                    fetched_at=now, plan="pro")

    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show()
    # wide enough that the top bar's overflow collapse never kicks in - this
    # test is about the pill picker's content/visibility logic, not layout
    win.resize(2400, 900)
    app = QApplication.instance()
    app.processEvents()
    bar = win.top_bar

    check("usage-trackers: every readout is on by default",
          all(bar.usage_trackers()[k] for k in USAGE_TRACKER_KEYS))
    check("usage-trackers: ...but no pill is on the bar without content",
          not bar.usage_badge.isVisible()
          and not bar.usage_weekly_badge.isVisible()
          and not bar.gemini_badge.isVisible()
          and not bar.gemini_weekly_badge.isVisible())
    check("usage-trackers: the + picker is on the bar",
          bar.usage_add_btn.isVisible())

    # the menu mirrors the preference and is rebuilt per click, so it can never
    # show a stale checkmark
    menu = bar.build_tracker_menu()
    acts = menu.actions()
    check("usage-trackers: one checkable entry per readout",
          len(acts) == 4 and all(a.isCheckable() for a in acts)
          and [a.text() for a in acts]
          == [USAGE_TRACKER_LABELS[k] for k in USAGE_TRACKER_KEYS])
    check("usage-trackers: the entries start checked",
          all(a.isChecked() for a in acts))

    # put content in all four so visibility is decided by the preference alone
    win._on_usage_ready(good)
    bar.mark_usage_loading()
    app.processEvents()
    check("usage-trackers: loading counts as content, so the bar fills at once",
          bar.gemini_badge.isVisible() and bar.gemini_weekly_badge.isVisible()
          and bar.usage_badge.isVisible() and bar.usage_weekly_badge.isVisible())

    # the X closes exactly one pill
    seen = []
    bar.usageTrackerToggled.connect(lambda k, on: seen.append((k, on)))
    bar.gemini_weekly_badge.close_btn.click()
    app.processEvents()
    check("usage-trackers: the X emits (its own key, False)",
          seen == [("gemini_weekly", False)], seen)
    check("usage-trackers: it closes that pill and no other",
          not bar.gemini_weekly_badge.isVisible()
          and bar.gemini_badge.isVisible() and bar.usage_badge.isVisible())
    check("usage-trackers: a later reading cannot resurrect a closed pill",
          (bar.mark_usage_loading(), app.processEvents(),
           not bar.gemini_weekly_badge.isVisible())[-1])

    # ...and closing IS a save, while a reading still is NOT
    win._save_timer.stop()
    win._on_usage_tracker_toggled("gemini_five_hour", False)
    check("usage-trackers: closing a pill schedules a save (a UI preference)",
          win._save_timer.isActive())
    win._save_timer.stop()
    win._on_usage_ready(good)
    check("usage-trackers: a reading still never marks the session dirty",
          not win._save_timer.isActive())

    win._on_usage_tracker_toggled("claude_weekly", False)
    check("usage-trackers: the + survives every pill being closed",
          (win._on_usage_tracker_toggled("claude_five_hour", False),
           app.processEvents(),
           bar.usage_add_btn.isVisible()
           and not bar.usage_badge.isVisible()
           and not bar.usage_weekly_badge.isVisible())[-1])
    check("usage-trackers: the menu now shows all four unchecked",
          not any(a.isChecked() for a in bar.build_tracker_menu().actions()))

    # no Claude login hides the recovery switches, but NEVER the picker: a
    # Gemini-only user would otherwise have no control at all
    bar.set_recovery_available(False)
    # isVisibleTo(options_panel), NOT isVisible(): the two switches live in
    # the Options popup, which is closed here, so isVisible() is False for
    # every one of them and the assertion would pass while testing nothing.
    check("usage-trackers: no-Claude hides the recovery rows, not the picker",
          not bar.recover_btn.isVisibleTo(bar.options_panel)
          and not bar.resume_btn.isVisibleTo(bar.options_panel)
          and bar.usage_add_btn.isVisible())
    bar.set_recovery_available(True)

    # re-checking brings it back, in its loading state rather than as a gap
    win._on_usage_tracker_toggled("claude_five_hour", True)
    app.processEvents()
    check("usage-trackers: re-checking restores the pill, showing loading",
          bar.usage_badge.isVisible() and bar.usage_badge.has_content()
          and "reading" in bar.usage_badge._text)
    check("usage-trackers: its sibling stays closed, closing one leaves the "
          "other alone",
          not bar.usage_weekly_badge.isVisible())

    check("usage-trackers: the old master toggle is gone",
          not hasattr(bar, "usageVisibilityToggled")
          and not hasattr(bar, "set_usage_visible"))
    win._save_session()
    win.close()

    saved = _json.loads((tmp / "s.json").read_text(encoding="utf-8-sig"))
    check("usage-trackers: persisted per key under ui.usage_trackers",
          saved["ui"]["usage_trackers"]["gemini_weekly"] is False
          and saved["ui"]["usage_trackers"]["claude_five_hour"] is True
          and saved["ui"]["usage_trackers"]["claude_weekly"] is False)

    win2 = create_main_window(SessionStore(path=tmp / "s.json"))
    check("usage-trackers: restored per key on reopen",
          win2.top_bar.usage_trackers() == saved["ui"]["usage_trackers"])
    win2.close()

    # MIGRATION off the older single boolean. A user who hid the whole readout
    # must not have it put back; and the newer key wins when both are present.
    saved["ui"].pop("usage_trackers")
    saved["ui"]["usage_visible"] = False
    (tmp / "legacy.json").write_text(_json.dumps(saved), encoding="utf-8")
    win3 = create_main_window(SessionStore(path=tmp / "legacy.json"))
    check("usage-trackers: legacy usage_visible=False closes every pill",
          not any(win3.top_bar.usage_trackers().values()))
    win3.close()

    saved["ui"]["usage_visible"] = True
    (tmp / "legacy_on.json").write_text(_json.dumps(saved), encoding="utf-8")
    win4 = create_main_window(SessionStore(path=tmp / "legacy_on.json"))
    check("usage-trackers: legacy usage_visible=True opens every pill",
          all(win4.top_bar.usage_trackers().values()))
    win4.close()

    saved["ui"]["usage_trackers"] = {"claude_five_hour": False,
                                     "claude_weekly": False,
                                     "gemini_five_hour": True,
                                     "gemini_weekly": True}
    saved["ui"]["usage_visible"] = True          # deliberately contradictory
    (tmp / "both.json").write_text(_json.dumps(saved), encoding="utf-8")
    win5 = create_main_window(SessionStore(path=tmp / "both.json"))
    check("usage-trackers: the per-key preference wins over the legacy mirror",
          win5.top_bar.usage_trackers()["claude_five_hour"] is False)
    win5.close()


def test_limit_ledger():
    """The durable record of cut-offs: who was stopped, when, and how it ended.

    Everything else about a cut-off is transient by design, so this file is the
    only thing that can answer "what happened last night" after a restart — and
    it is what decides, on the next launch, whether a cut-off still needs
    acting on."""
    import time as _time
    from app import limit_ledger as L

    tmp = str(Path(tempfile.mkdtemp(prefix="ai-hive-ledger-")))
    now = _time.time()

    check("ledger: an absent file reads as no records, never raises",
          L.read_all(tmp) == [] and L.open_cut_offs(tmp) == [])

    rec = L.record_cut_off(tmp, cwd="C:/proj", session_id="sid-a", at=now,
                           reset_at=now + 3600, ws_id="w1", ws_name="Hive",
                           agent_name="Agent 5", task="fix the parser",
                           window="session", banner="You've hit...",
                           source="live")
    stored = L.read_all(tmp)[0]
    check("ledger: a cut-off round-trips with workspace, agent, task and window",
          stored["ws_name"] == "Hive" and stored["agent_name"] == "Agent 5"
          and stored["task"] == "fix the parser"
          and stored["window"] == "session")
    check("ledger: it carries a readable LOCAL date and time, not just an epoch",
          stored["at_local"][:2] == "20" and ":" in stored["at_local"]
          and stored["reset_local"])

    # THE identity problem: TerminalAgent.id is a fresh uuid on every load, so
    # a cut-off has to be named by things that survive the restart this file
    # exists for -- the folder, the pinned conversation, and which window
    # stopped it.
    same = L.key_of("C:/proj", "sid-a", now + 3600, now + 5)
    check("ledger: the key is stable across runs (agent ids are not)",
          same == rec["key"])
    later = L.key_of("C:/proj", "sid-a", now + 3600 + 5 * 3600, now + 5 * 3600)
    check("ledger: a LATER cut-off of the same conversation is a new record",
          later != rec["key"])
    clockless = L.key_of("C:/proj", "sid-a", 0.0, now)
    check("ledger: a banner with no clock still yields a usable key",
          clockless[2] and clockless != rec["key"])

    check("ledger: a fresh cut-off is open", len(L.open_cut_offs(tmp)) == 1)
    L.record_cut_off(tmp, cwd="C:/proj", session_id="sid-a", at=now + 3,
                     reset_at=now + 3600, source="transcript")
    check("ledger: the same cut-off filed twice (seen live, then read off "
          "disk) is ONE open record", len(L.open_cut_offs(tmp)) == 1)
    L.record_outcome(tmp, rec["key"], L.RESUMED, tries=1)
    check("ledger: an outcome closes it for good",
          L.open_cut_offs(tmp) == []
          and tuple(rec["key"]) in L.closed_keys(L.read_all(tmp)))

    # written at the moment the app may be killed, so a half-line is expected
    with open(L.path_for(tmp), "a", encoding="utf-8") as fh:
        fh.write('{"event":"cut_off","at":  \n')
    check("ledger: a torn last line is skipped, not fatal",
          len(L.read_all(tmp)) == 3)

    # --- grouping: which cut-offs belong to the MOST RECENT window ----------
    def cut(at, reset):
        return {"event": L.CUT_OFF, "at": at, "reset_at": reset}

    old, new1, new2 = cut(now - 6 * 3600, now - 3600), \
        cut(now - 600, now + 1800), cut(now - 900, now + 1800 + 20)
    group = L.latest_window([old, new1, new2])
    check("ledger: agents stopped by one window group by their shared reset",
          group == [new1, new2] or group == [new2, new1])
    check("ledger: an earlier window is not in the group", old not in group)
    check("ledger: no cut-offs means no group", L.latest_window([]) == [])
    # a clockless cut-off can still be the newest one, so the anchor is WHEN it
    # happened -- never the reset it failed to state
    bare_new, bare_old = cut(now, 0.0), cut(now - 20 * 3600, 0.0)
    check("ledger: clockless cut-offs group by how far apart they were",
          L.latest_window([bare_old, bare_new]) == [bare_new])


def test_auto_continue_on_limit_reset():
    """When the plan limit resets, the agents it CUT OFF go back to work by
    themselves: Esc to close the limit's options menu, then "Continue".

    The point is unattended overnight recovery — and, because the first message
    after a window expires is what starts the next one, resuming at 4am also
    rolls the 5-hour clock over before morning. Guards matter as much as the
    action: the account-wide reading that fires the edge cannot say WHICH agents
    were mid-turn, so anything not parked on the limit banner is left alone."""
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu
    from app import limit_ledger
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.widgets.main_window import AUTO_CONTINUE_TEXT
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()

    # long enough to cover the Esc->type beat plus the delayed submit CR
    AUTO_CONTINUE_SETTLE_MS = 1200

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    BANNER = ("You've hit your session limit \xb7 resets 4:40am "
              "(Europe/Bucharest)\n")
    # the NEXT window's cut-off. Its clock differs because successive 5-hour
    # windows never end at the same wall time -- which is what makes the banner
    # line the identity of a particular cut-off (see _scrape_limit).
    NEXT_BANNER = ("You've hit your session limit \xb7 resets 9:40am "
                   "(Europe/Bucharest)\n")
    # what Claude actually parks the agent on. Unlike the banner (ordinary
    # scrollback, which the rolling tail evicts) this MENU stays up for as long
    # as the agent is stuck, so it is the primary live signal and the "did it
    # resume?" test.
    MENU = ("What do you want to do?\n"
            "> 1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n"
            "Enter to confirm \xb7 Esc to cancel\n")
    PARKED = BANNER + MENU

    # --- the per-agent "I was cut off" scrape -------------------------------
    def mk(name="Coder", provider="claude", pty=True):
        spec = build_spec(AgentKind.CLAUDE if provider == "claude"
                          else AgentKind.CMD, name, cwd=os.getcwd(), pty=pty)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d), True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    writes: dict = {}

    def settle(agent, screen):
        """Feed a settled screen through the real idle-timer path."""
        agent._screen_tail = screen
        agent._on_idle_timeout()

    a = mk()
    settle(a, "")
    check("auto-continue: a clean screen is not limit-blocked",
          not a.is_limit_blocked())
    settle(a, "Approaching session limit \xb7 resets 4:40am\n")
    check("auto-continue: an 'Approaching' warning is NOT a cut-off",
          not a.is_limit_blocked())
    settle(a, "You've used 62% of your session limit \xb7 resets 4:40am\n")
    check("auto-continue: a 'You've used N%' warning is NOT a cut-off",
          not a.is_limit_blocked())
    settle(a, BANNER)
    check("auto-continue: the exhausted banner IS a cut-off",
          a.is_limit_blocked())
    check("auto-continue: the banner's own reset time is latched with it",
          a.limit_resets_at() is not None)

    # the menu alone is enough — it is what survives on screen when the banner
    # above it has scrolled out of the rolling tail
    menu_only = mk("MenuOnly")
    settle(menu_only, MENU)
    check("auto-continue: the parked-on menu alone IS a cut-off",
          menu_only.is_limit_blocked())

    # --- WHY a banner did not latch has to be on the record -----------------
    # Everything after a latch is audited; the decision NOT to latch was not,
    # and that is the one that strands work. An agent was found parked on a
    # spent limit with no trace anywhere of why the scrape had passed over it,
    # and the cause could only be narrowed by elimination. These lines close
    # that gap -- while staying bounded, since _scrape_limit runs on EVERY
    # output burst.
    skips: list = []
    quiet = mk("Quiet")
    quiet.audit = skips.append
    quiet._prompt_ready = False
    settle(quiet, BANNER)          # a banner during a resume replay
    check("no-latch: a banner ignored as replay says so in the log",
          any("NO-LATCH" in m and "Quiet" in m and "prompt not ready" in m
              for m in skips), skips)
    check("no-latch: ...and it did not latch", not quiet.is_limit_blocked())
    before = len(skips)
    for _ in range(20):            # the same frame repainted many times over
        settle(quiet, BANNER)
    check("no-latch: an unchanged rejection is recorded ONCE, not per burst",
          len(skips) == before, skips[before:])

    # the echo guard is the other silent path: a banner still on screen after
    # a resume, with the menu torn down
    echo = mk("Echo")
    echo.audit = skips.append
    settle(echo, BANNER)                       # first sighting latches
    echo.clear_limit_block()                   # ...resumed off it
    n = len(skips)
    settle(echo, BANNER)                       # same line, no menu -> echo
    check("no-latch: a suppressed banner echo names the reason",
          any("NO-LATCH" in m and "Echo" in m and "already produced a latch"
              in m for m in skips[n:]), skips[n:])
    check("no-latch: ...and the echo still does not re-latch",
          not echo.is_limit_blocked())

    # and it must stay silent when there is nothing to say
    mute: list = []
    ordinary = mk("Ordinary")
    ordinary.audit = mute.append
    settle(ordinary, "building the index...\ndone in 4.1s\n")
    latched = mk("Latched")
    latched.audit = mute.append
    settle(latched, BANNER)
    check("no-latch: ordinary output and a successful latch log nothing",
          mute == [] and latched.is_limit_blocked(), mute)

    # An agent WRITING ABOUT the limit is not stopped by it. Observed live: an
    # agent working on this feature quoted the banner in its own output and was
    # armed for a resume it never needed. A real banner is a short line of its
    # own; prose that mentions it is not.
    talker = mk("Talker")
    settle(talker,
           "The sign an agent shows looks like this: " + BANNER.strip()
           + " -- and that phrase is what we match against the screen "
             "buffer to work out that it stopped.\n")
    check("auto-continue: an agent QUOTING the banner in prose is not a "
          "cut-off", not talker.is_limit_blocked())

    # a cut-off with no clock anywhere must still be resumable -- `unknown`
    # once stranded an agent for five hours
    noclock = mk("NoClock")
    settle(noclock, MENU)                       # the menu carries no time
    check("auto-continue: a menu-only cut-off has no reset time of its own",
          noclock.limit_resets_at() is None)
    noclock.set_limit_reset(now + 1800)         # ...supplied by the account
    check("auto-continue: a reset time can be supplied from the account "
          "reading", noclock.limit_resets_at() == now + 1800)
    noclock.set_limit_reset(now + 9999)
    check("auto-continue: a known reset time is never overwritten",
          noclock.limit_resets_at() == now + 1800)

    # THE REGRESSION that cost a night's work: the banner is latched when it is
    # DRAWN, because _screen_tail is a rolling buffer — by reset time the agent
    # has idled for hours and its own redraws have evicted the banner. A
    # re-scrape at that point sees only the bottom of a frame and resumes
    # nobody.
    settle(a, "\xe2\x94\x82 > \xe2\x94\x82\n  ? for shortcuts\n")
    check("auto-continue: the latch SURVIVES the banner scrolling out of the "
          "rolling screen tail", a.is_limit_blocked())
    # the card header's hourglass and the sidebar's blocked-count badge are
    # both signal-driven, not polled -- clearing the latch must emit the
    # FALLING edge or the marker sits there forever after a real resume
    edge_events = []
    a.limit_blocked_changed.connect(edge_events.append)
    a.clear_limit_block()
    check("auto-continue: clearing the latch forgets the reset time too",
          not a.is_limit_blocked() and a.limit_resets_at() is None)
    check("auto-continue: clearing the latch emits limit_blocked_changed(False)",
          edge_events == [False], edge_events)
    edge_events.clear()
    a.clear_limit_block()   # start()/restart() call this unconditionally
    check("auto-continue: clearing an already-clear latch stays silent "
          "(never blocked -> nothing changed)", edge_events == [], edge_events)

    # a --resume replay redraws the OLD conversation, banner and all; that is
    # history, not a live cut-off, and latching it would schedule a phantom
    # Continue. The input-box footer ends the replay, so pre-prompt output is
    # excluded.
    replay = mk()
    replay._prompt_ready = False
    replay._on_pty_output("pty", BANNER)
    check("auto-continue: a banner replayed before the prompt is ready is "
          "history, not a cut-off", not replay.is_limit_blocked())
    replay._prompt_ready = True
    replay._on_pty_output("pty", BANNER)
    check("auto-continue: the same banner once live DOES latch",
          replay.is_limit_blocked())

    # Prompt readiness must accept the WHOLE rotating footer-hint family, not
    # just "? for shortcuts". Keying on that one member left a restored agent
    # permanently "not ready" -- the watchdog logged WAIT every minute and
    # never nudged it (verified live), and a delivered task would have hung in
    # _pending_task just as long.
    for hint in ("? for shortcuts",
                 "auto mode on(shift+tab to cycle) \xb7 ctrl+t to show tasks "
                 "\xb7 ← for agents"):
        r = mk("Ready")
        r._prompt_ready = False
        r._on_pty_output("pty", "\x1b[?2004h" + hint + "\n")
        check(f"auto-continue: footer hint {hint.split(chr(183))[0][:24]!r} "
              f"marks the prompt ready", r.prompt_ready())
    notready = mk("NotReady")
    notready._prompt_ready = False
    notready._on_pty_output("pty", "Do you trust the files in this folder?\n")
    check("auto-continue: the trust dialog is NOT mistaken for a live prompt",
          not notready.prompt_ready())

    # the latch must not wait for the idle-timer settle: the banner arrives
    # right after the user hits Enter, which is exactly when _mark_busy treats
    # output as keystroke echo and never arms that timer (a live miss)
    burst = mk()
    burst.write("hi")                 # stamps _last_input_ts -> echo window
    burst._on_pty_output("pty", BANNER)
    check("auto-continue: latched straight off the output burst, with no "
          "idle-timer settle", burst.is_limit_blocked())

    curly = mk()
    settle(curly, "You’ve hit your weekly limit \xb7 resets 3am\n")
    check("auto-continue: curly apostrophe + weekly window also detected",
          curly.is_limit_blocked())
    non_claude = mk(name="Shell", provider="cmd", pty=True)
    settle(non_claude, BANNER)
    check("auto-continue: a non-Claude agent is never limit-blocked",
          not non_claude.is_limit_blocked())

    # --- the banner's clock -> the next occurrence of that wall time ---------
    from app.terminal_agent import parse_reset_clock
    base = _time.mktime((2026, 8, 2, 23, 50, 0, 0, 0, -1))   # 23:50 local
    at = parse_reset_clock("\xb7 resets 4:40am (Europe/Bucharest)", base)
    lt = _time.localtime(at)
    check("auto-continue: a small-hours reset read late at night rolls over "
          "to tomorrow",
          (lt.tm_hour, lt.tm_min) == (4, 40) and at > base
          and at - base < 6 * 3600)
    noon = _time.mktime((2026, 8, 2, 12, 0, 0, 0, 0, -1))
    lt2 = _time.localtime(parse_reset_clock("resets 8:30pm", noon))
    check("auto-continue: pm is read as afternoon, same day",
          (lt2.tm_hour, lt2.tm_min) == (20, 30))
    lt3 = _time.localtime(parse_reset_clock("resets 12:15am", noon))
    check("auto-continue: 12:15am is after midnight, not noon",
          (lt3.tm_hour, lt3.tm_min) == (0, 15))
    check("auto-continue: a banner with no time yields no reset",
          parse_reset_clock("You've hit your session limit") is None)

    # --- nudge() must not disturb any PERSISTED metadata --------------------
    n = mk()
    n.set_task("write the parser")
    before = (n.current_task, n.assignment, n.spec.role)
    check("auto-continue: nudge() delivers when the prompt is ready",
          n.nudge("Continue") is True)
    check("auto-continue: nudge() leaves task/assignment/role untouched",
          (n.current_task, n.assignment, n.spec.role) == before)
    check("auto-continue: nudge() typed the text",
          "Continue" in "".join(writes.get(id(n.worker), [])))
    check("auto-continue: nudge() does not stamp the user-input clock "
          "(resumed work still pulses the sidebar)",
          n._last_input_ts == 0.0)
    n._prompt_ready = False
    check("auto-continue: nudge() refuses when the TUI isn't prompt-ready",
          n.nudge("Continue") is False)

    # --- the wiring: planLimitCleared -> resume the blocked ones ------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-autocont-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show(); pump(50)
    mgr = win.manager
    ws = mgr.workspaces[0]

    cut_off, busy, fine = mk("CutOff"), mk("Busy"), mk("Fine")
    settle(cut_off, BANNER)
    settle(busy, BANNER)
    busy._busy = True                    # already moving again
    settle(fine, "all done\n")           # was never cut off
    ws.agents.extend([cut_off, busy, fine])

    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    sent = lambda ag: "".join(writes.get(id(ag.worker), []))
    check("auto-continue: the cut-off agent got Esc then Continue",
          sent(cut_off).startswith("\x1b") and "Continue" in sent(cut_off))
    check("auto-continue: an agent that is busy again is left alone",
          sent(busy) == "")
    check("auto-continue: an agent that was never cut off is left alone",
          sent(fine) == "")
    check("auto-continue: the card carries an audit notice",
          any("auto-continued" in t for _s, t in cut_off.log))

    # the toggle actually gates it
    writes.clear()
    win._on_auto_continue(False)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: nothing is typed while the toggle is off",
          sent(cut_off) == "")
    payload_ui = win._session_payload()["ui"]
    check("auto-continue: preference persisted under ui.auto_continue",
          payload_ui["auto_continue"] is False)

    # the real trigger is the plan-limit falling edge, not a timer
    writes.clear()
    win._on_auto_continue(True)
    cut_off.clear_limit_block()    # a FRESH cut-off in the next window
    settle(cut_off, NEXT_BANNER)   # ...which states the next window's clock
    win._plan_blocked = True
    win._on_usage_ready(cu.Usage(
        limits=(cu.Limit(key="five_hour", label=cu._LABELS["five_hour"],
                         short=cu._SHORT["five_hour"], percent=12.0,
                         resets_at=now + 4700),),
        fetched_at=now, plan="pro"))
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: the planLimitCleared edge resumes cut-off agents",
          "Continue" in sent(cut_off))
    # The latch is NOT dropped on the nudge itself: a resume can land while the
    # window is still shut, and clearing here burned the only attempt and left
    # the agent parked for good. It clears on VERIFICATION instead.
    check("auto-continue: the latch survives the nudge, pending verification",
          cut_off.is_limit_blocked())
    check("auto-continue: a nudged agent is not re-nudged a minute later",
          not cut_off.limit_retry_ready(300, 4))
    cut_off._screen_tail = PARKED           # menu still up: it didn't take
    check("auto-continue: still parked on the menu -> stays latched for a retry",
          cut_off.recheck_limit() is True)
    # The banner LINGERS in the tail after a successful resume, so verification
    # must key on the menu alone — using the banner would report a false
    # "still blocked" forever and burn every retry.
    cut_off._screen_tail = BANNER + "\nWorking on it...\n  ? for shortcuts\n"
    check("auto-continue: menu gone -> resumed, even with the banner still in "
          "the scrollback",
          cut_off.recheck_limit() is False and not cut_off.is_limit_blocked())
    check("auto-continue: attempts reset with the latch",
          cut_off.limit_attempts() == 0)

    # --- the network-free watchdog: the banner's own reset time -------------
    # The API edge is NOT enough on its own. It only fires if this same process
    # also saw the blocked state first, and the endpoint 429s intermittently --
    # a missed edge silently costs a whole night, which is what happened live.
    writes.clear()
    late = mk("Late")
    settle(late, BANNER)
    ws.agents.append(late)
    late._limit_resets_at = now + 3600          # not due yet
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an agent whose reset is still in the future waits",
          sent(late) == "" and late.is_limit_blocked())
    # a reset read OFF THE SCREEN gets slack before it is acted on: the banner
    # names a minute, not an instant, and a nudge into a window that is still
    # shut spends one of very few retries
    from app.widgets.main_window import LIMIT_RESET_GRACE_S
    late._limit_resets_at = now - 30             # just past, inside the grace
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a reset that has only just passed waits out the grace",
          sent(late) == "" and late.is_limit_blocked())
    late._limit_resets_at = now - LIMIT_RESET_GRACE_S - 60   # grace elapsed
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: the watchdog resumes it with NO usage reading at all",
          AUTO_CONTINUE_TEXT in sent(late))

    # a WEEKLY window is not readable off the screen: its banner prints a bare
    # wall clock for a reset that can be days out, so acting on that clock
    # would nudge days early. It waits for the account reading instead.
    writes.clear()
    weekly = mk("Weekly")
    settle(weekly, "You've hit your weekly limit \xb7 resets 4:40am\n")
    ws.agents.append(weekly)
    check("auto-continue: the weekly window is identified from its banner",
          weekly.limit_window() == "weekly")
    weekly._limit_resets_at = now - 2 * LIMIT_RESET_GRACE_S   # clock says go
    saved_usage, win._usage = win._usage, None                # ...API silent
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a weekly cut-off is NOT resumed on its bare clock",
          sent(weekly) == "" and weekly.is_limit_blocked())
    win._usage = cu.Usage(                                   # account is clear
        limits=(cu.Limit(key="seven_day", label=cu._LABELS["seven_day"],
                         short=cu._SHORT["seven_day"], percent=8.0,
                         resets_at=now + 90000),),
        fetched_at=now, plan="max")
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: ...but IS once the account reading says it cleared",
          AUTO_CONTINUE_TEXT in sent(weekly))
    win._usage = saved_usage

    # a banner with no parseable time has no due date -- it must NOT count as
    # "due now", which would fire a pointless Continue into a still-blocked
    # agent and then drop the latch, missing the real reset
    writes.clear()
    timeless = mk("Timeless")
    settle(timeless, "You've hit your session limit\n")
    ws.agents.append(timeless)
    check("auto-continue: a banner with no time still latches the cut-off",
          timeless.is_limit_blocked() and timeless.limit_resets_at() is None)
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an unknown reset time is NOT treated as due now",
          sent(timeless) == "" and timeless.is_limit_blocked())

    # ...but it must not wait FOREVER either. With no clock from the screen and
    # none from the API, fall back to the longest a window can last, so a latch
    # can never become permanent for want of a timestamp.
    from app.widgets.main_window import LIMIT_UNKNOWN_WAIT_S
    timeless._limit_at = now - LIMIT_UNKNOWN_WAIT_S - 60
    timeless._limit_resets_at = None             # nothing supplied a clock
    saved_usage, win._usage = win._usage, None   # not even the account
    win._check_limit_resets()
    win._usage = saved_usage
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a clockless cut-off is resumed once no window could "
          "still be open", "Continue" in sent(timeless))

    # the banner is not bottom-anchored: its options menu, the input box and
    # the footer all render below it, so the scrape window must be wider than
    # the selection-menu scrape's 18 lines
    deep = mk("Deep")
    settle(deep, BANNER + "\n".join(
        ["  1. Upgrade", "  2. Team plan", "  3. Extra usage", "  4. Cancel"]
        + [f"filler {i}" for i in range(14)]
        + ["│ > │", "  ? for shortcuts"]))
    check("auto-continue: the banner is found above a full frame of menu/input",
          deep.is_limit_blocked())

    # --- the banner still on screen must not re-latch a RESUMED agent -------
    # Observed live on 2026-08-04: an agent was re-BLOCKED 13 s after RESUMED,
    # and another five hours after its resume, both with a reset time exactly
    # 24 h ahead -- the signature of parse_reset_clock re-reading a banner whose
    # clock had already passed. The banner is ordinary output that stays in view
    # (and is redrawn with every frame), so clearing the latch let the next
    # burst latch the very same line again. That muted the agent's "?" chime and
    # later typed a stray Continue into an agent that was working fine.
    relatch = mk("Relatch")
    settle(relatch, PARKED)
    check("auto-continue: the parked agent latches in the first place",
          relatch.is_limit_blocked())
    relatch._screen_tail = BANNER + "\nWorking on it...\n  ? for shortcuts\n"
    check("auto-continue: menu gone -> the resume is verified",
          relatch.recheck_limit() is False)
    relatch._on_pty_output("pty", BANNER + "still working\n")
    check("auto-continue: the SAME banner lingering after a resume does NOT "
          "re-latch the agent", not relatch.is_limit_blocked())
    relatch._on_pty_output("pty", NEXT_BANNER)
    check("auto-continue: the NEXT window's banner (a different clock) does "
          "latch", relatch.is_limit_blocked())

    # the guard is the missing MENU, not the text alone: a genuine cut-off
    # renders its menu directly below the banner, so an identical line WITH the
    # menu is a real new cut-off (the same wall clock can come round again on a
    # weekly window) and must still latch.
    same = mk("SameClock")
    settle(same, PARKED)
    same.clear_limit_block()
    same._on_pty_output("pty", PARKED)
    check("auto-continue: an identical banner accompanied by the parked-on "
          "menu IS a new cut-off", same.is_limit_blocked())
    # ...and a restart starts the screen over, so nothing is an echo any more
    restarted = mk("Restarted")
    settle(restarted, BANNER)
    restarted.clear_limit_block()
    restarted._limit_last_banner = ""    # what start()/restart() do
    restarted._on_pty_output("pty", BANNER)
    check("auto-continue: a (re)start forgets the echo guard",
          restarted.is_limit_blocked())

    # --- the two triggers must not double-nudge -----------------------------
    # The attempt counter only advances when the nudge actually goes out, up to
    # a full stagger later, so a second trigger inside that window re-scheduled
    # everyone downstream of the first agent: on 2026-08-04 the API's cleared
    # edge and the minute watchdog both fired at 05:30 and typed Continue TWICE
    # into three of four agents.
    from app.widgets.main_window import AUTO_CONTINUE_STAGGER_MS
    writes.clear()
    dup = mk("Dup")
    settle(dup, BANNER)
    ws.agents.append(dup)
    win._on_auto_continue(True)
    win._resume_blocked_agents()          # the planLimitCleared edge...
    win._resume_blocked_agents()          # ...racing the minute watchdog
    # long enough that a duplicate would land even a full stagger behind
    pump(AUTO_CONTINUE_SETTLE_MS + AUTO_CONTINUE_STAGGER_MS)
    check("auto-continue: two overlapping triggers nudge each agent ONCE",
          sent(dup).count("Continue") == 1 and dup.limit_attempts() == 1)
    check("auto-continue: the claim is released once the nudge has gone out",
          dup.id not in win._resume_pending)

    # --- a latch the TRANSCRIPT refutes is dropped, not nudged --------------
    # The screen said this agent was cut off; the conversation on disk says it
    # carried on. A banner stays in view (and is redrawn) long after the agent
    # moved on, so the screen alone can be describing history -- and a stray
    # "Continue" into an agent that is working is both an interruption and real
    # quota spent.
    import json as _json
    from app import transcripts as _tr

    def write_convo(cwd_, sid, records):
        """A real Claude transcript for `cwd_`/`sid` (records = [(text, at)])."""
        path = _tr.transcript_path(cwd_, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for text, at in records:
                fh.write(_json.dumps({
                    "type": "assistant",
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                _time.gmtime(at)),
                    "message": {"content": [{"type": "text",
                                             "text": text}]}}) + "\n")

    writes.clear()
    ph_cwd = str(tmp / "phantom")
    phantom = mk("Phantom")
    phantom.spec.cwd, phantom.spec.session_id = ph_cwd, "sid-moved-on"
    write_convo(ph_cwd, "sid-moved-on",
                [(BANNER.strip(), now - 7200), ("carried on", now - 3600)])
    settle(phantom, BANNER)
    ws.agents.append(phantom)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a latch the transcript refutes is dropped, never "
          "nudged", sent(phantom) == "" and not phantom.is_limit_blocked())
    check("auto-continue: the dismissal is recorded as an outcome",
          any(r.get("event") == limit_ledger.DISMISSED
              for r in limit_ledger.read_all(str(tmp))))

    # ...but a transcript that cannot be READ is no evidence either way. A
    # drifted pin, or a conversation Claude has not flushed, must never strand
    # a genuinely parked agent.
    writes.clear()
    unknown = mk("Unknown")
    unknown.spec.cwd, unknown.spec.session_id = ph_cwd, "sid-no-such-file"
    settle(unknown, BANNER)
    ws.agents.append(unknown)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an unreadable transcript does not veto the latch",
          AUTO_CONTINUE_TEXT in sent(unknown))

    # --- a trivial background-task cut-off is dismissed EARLY, before the ---
    # --- watchdog ever gets a chance to nudge it -----------------------------
    # The screen renders the identical "Stop and wait for limit to reset"
    # menu whether the interrupted turn was real work or Claude Code's own
    # background-command-completion notification auto-continuing on its own
    # -- so the live latch (correctly) cannot tell them apart, and the
    # hourglass shows either way. `_dismiss_if_phantom` re-checks the
    # transcript a few seconds after the latch and clears it once the
    # evidence says nothing of the agent's actual work was lost, instead of
    # waiting for reset time to find out via a wasted nudge.
    from app.widgets.main_window import LIMIT_PHANTOM_CHECK_MS

    def write_raw(cwd_, sid, records):
        path = _tr.transcript_path(cwd_, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(rec) + "\n")

    def ts(at):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(at))

    writes.clear()
    bg_cwd = str(tmp / "bgnotify")
    bg = mk("BgNotify")
    bg.spec.cwd, bg.spec.session_id = bg_cwd, "sid-bg-notify"
    write_raw(bg_cwd, "sid-bg-notify", [
        {"type": "assistant", "timestamp": ts(now - 3600),
         "message": {"content": [{"type": "text", "text": "Done, merged."}]}},
        {"type": "user", "timestamp": ts(now - 30),
         "message": {"content": "<task-notification>\n<task-id>t1</task-id>\n"
                                "<status>completed</status>\n"
                                "</task-notification>"}},
        {"type": "assistant", "timestamp": ts(now - 30),
         "message": {"content": [{"type": "text", "text": BANNER.strip()}]}},
    ])
    settle(bg, BANNER)
    ws.agents.append(bg)
    check("auto-continue: a trivial background-task cut-off still latches "
          "live (the screen alone can't tell it apart from a real one)",
          bg.is_limit_blocked())
    win._on_agent_limit_blocked(ws.id, bg.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: ...but the early phantom check clears it once the "
          "transcript shows the interrupted turn wasn't real work",
          not bg.is_limit_blocked())
    check("auto-continue: it is never nudged", sent(bg) == "")
    check("auto-continue: the early dismissal is recorded as an outcome too",
          any(r.get("event") == limit_ledger.DISMISSED
              and "background-task" in r.get("detail", "")
              for r in limit_ledger.read_all(str(tmp))))

    # ...and a GENUINE cut-off latched the same way must survive that same
    # early check untouched.
    writes.clear()
    real_cwd = str(tmp / "realask")
    real_ask = mk("RealAsk")
    real_ask.spec.cwd, real_ask.spec.session_id = real_cwd, "sid-real-ask"
    write_raw(real_cwd, "sid-real-ask", [
        {"type": "assistant", "timestamp": ts(now - 3600),
         "message": {"content": [{"type": "text", "text": "Done, merged."}]}},
        {"type": "user", "timestamp": ts(now - 30),
         "message": {"content": "can you add Chess960 castling support?"}},
        {"type": "assistant", "timestamp": ts(now - 30),
         "message": {"content": [{"type": "text", "text": BANNER.strip()}]}},
    ])
    settle(real_ask, BANNER)
    ws.agents.append(real_ask)
    win._on_agent_limit_blocked(ws.id, real_ask.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: a genuine cut-off survives the early phantom check",
          real_ask.is_limit_blocked())

    # THE RACE the early check has to lose safely: it runs seconds after the
    # banner was DRAWN, and Claude may not have written that record yet. A
    # running agent's transcript always exists, so an unflushed banner does
    # NOT read as "no transcript" -- it reads as a conversation that carried
    # on. Dismissing on that would clear a genuine latch outright (nothing
    # re-latches a silently parked agent), so the early check gates on
    # positive `synthetic` evidence instead.
    writes.clear()
    slow_cwd = str(tmp / "unflushed")
    slow = mk("Unflushed")
    slow.spec.cwd, slow.spec.session_id = slow_cwd, "sid-unflushed"
    write_raw(slow_cwd, "sid-unflushed", [
        {"type": "user", "timestamp": ts(now - 60),
         "message": {"content": "please refactor the parser"}},
        {"type": "assistant", "timestamp": ts(now - 50),
         "message": {"content": [{"type": "text", "text": "Working on it."}]}},
    ])
    settle(slow, BANNER)
    ws.agents.append(slow)
    win._on_agent_limit_blocked(ws.id, slow.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: a latch whose banner has not been written to the "
          "transcript yet is NOT dismissed early", slow.is_limit_blocked())

    # ...and a slash command is the user's own work, however much its record
    # looks like plumbing.
    writes.clear()
    slash_cwd = str(tmp / "slashcmd")
    slash = mk("SlashCmd")
    slash.spec.cwd, slash.spec.session_id = slash_cwd, "sid-slash-cmd"
    write_raw(slash_cwd, "sid-slash-cmd", [
        {"type": "user", "timestamp": ts(now - 30),
         "message": {"content": "<command-name>/security-review</command-name>"
                                "\n<command-message>go</command-message>"}},
        {"type": "assistant", "timestamp": ts(now - 30),
         "message": {"content": [{"type": "text", "text": BANNER.strip()}]}},
    ])
    settle(slash, BANNER)
    ws.agents.append(slash)
    win._on_agent_limit_blocked(ws.id, slash.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: a cut-off during a USER-typed slash command is not "
          "dismissed as plumbing", slash.is_limit_blocked())

    # --- the cut-off is visible on the card ---------------------------------
    marked = mk("Marked")
    settle(marked, BANNER)
    check("auto-continue: an interrupted agent describes its cut-off",
          "usage limit" in marked.limit_summary()
          and "resets" in marked.limit_summary())
    check("auto-continue: ...and says a resume is pending",
          "waiting to auto-continue" in marked.limit_summary())
    marked.note_limit_attempt()
    check("auto-continue: ...then how many resumes have been tried",
          "tried 1x" in marked.limit_summary())
    marked.clear_limit_block()
    check("auto-continue: an agent that is not cut off describes nothing",
          marked.limit_summary() == "")

    win._on_auto_continue(False)   # what the reopen below must find
    win.close()

    win2 = create_main_window(store)
    win2.show(); pump(50)
    check("auto-continue: preference restored on reopen",
          win2.top_bar.auto_continue() is False)
    check("auto-continue: default is ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")).top_bar.auto_continue())
    win2.close()

    # the limit banner must NOT ring the chime — nothing the sleeper can answer
    rung = {"n": 0}
    import app.chime as _chime
    real_play, _chime.play = _chime.play, lambda: rung.__setitem__("n", rung["n"] + 1)
    try:
        win3 = create_main_window(SessionStore(path=tmp / "chime.json"))
        win3.show(); pump(50)
        w3 = win3.manager.workspaces[0]
        quiet, loud = mk("Quiet"), mk("Loud")
        settle(quiet, BANNER)
        settle(loud, "1. Yes\n❯ 2. No\n")
        w3.agents.extend([quiet, loud])
        win3._on_agent_waiting(w3.id, quiet.id)
        check("auto-continue: a limit-blocked agent does not ring the chime",
              rung["n"] == 0)
        win3._on_agent_waiting(w3.id, loud.id)
        check("auto-continue: an ordinary prompt still rings the chime",
              rung["n"] == 1)
        win3.close()
    finally:
        _chime.play = real_play


def test_gemini_limit_detection():
    """A Gemini agent cut off by its quota is seen, shown and resumed too.

    The whole recovery path was gated on `provider == "claude"`, so an agy agent
    that ran out of quota sat there silently: no hourglass, no ledger entry, no
    auto-continue (reported live, 2026-08-09, on Agent 10 of the AI Hive
    workspace). Detecting it is not a matter of one more pattern -- agy's
    cut-off is a different SHAPE, and each difference is checked here:

      * no interactive menu, so the identity guard carries the whole weight;
      * a RELATIVE countdown ("Resets in 1h40m21s") rather than a wall clock;
      * the message WRAPS, so its identity is not on the matched line;
      * no conversation on disk, so there is no second source to agree with.
    """
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import limit_banner as lb
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.widgets.main_window import AUTO_CONTINUE_TEXT
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()
    AUTO_CONTINUE_SETTLE_MS = 1200

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    # exactly as agy renders it, wrapped across three rows, warning glyph and
    # all. The Error ID's trailing counter differs per message.
    def quota(countdown="1h40m21s", tag="266"):
        return ("⚠Individual quota reached. Please upgrade your "
                "subscription to increase your\n"
                f"limits. Resets in {countdown}.\n"
                f"Error ID: 6a22d054-3666-4717-b0ac-7b359153c647-{tag}\n")

    FOOTER = "\n> \n? for shortcuts                    Gemini 3.6 Flash \xb7 high\n"

    # --- the patterns, in isolation -----------------------------------------
    line = lb.gemini_banner_line(quota())
    check("gemini-limit: the wrapped message is read as one banner",
          line.startswith("Individual quota reached") and "1h40m21s" in line
          and line.endswith("-266"), line)
    check("gemini-limit: the LAST message wins (a retry describes the state "
          "now)", "-273" in lb.gemini_banner_line(quota() + quota("1h39m56s",
                                                                  "273")))
    check("gemini-limit: two cut-offs are told apart by their own text",
          lb.gemini_banner_line(quota()) != lb.gemini_banner_line(
              quota("1h39m56s", "273")))
    check("gemini-limit: an agent DISCUSSING a quota is not cut off by one",
          lb.gemini_banner_line("note that the quota reached its cap "
                                "yesterday, resets in 3h") == "")
    check("gemini-limit: Claude's banner is not read as a Gemini one, or the "
          "reverse",
          lb.gemini_banner_line("You've hit your session limit - resets 3am")
          == "" and lb.banner_line(quota()) == "")
    check("gemini-limit: the relative countdown resolves against NOW",
          lb.gemini_reset_at(quota(), 1000.0) == 1000.0 + 3600 + 40 * 60 + 21)
    for text, secs in (("resets in 45m", 2700), ("Resets in 30s", 30),
                       ("resets in 2h", 7200)):
        check(f"gemini-limit: {text!r} -> {secs}s",
              lb.gemini_reset_at(text, 0.0) == secs)
    check("gemini-limit: a message with no countdown yields no reset time",
          lb.gemini_reset_at("Individual quota reached.") is None)
    # the printed text is frozen; only the moment it is READ can date it, which
    # is why the epoch is resolved once at latch time and then kept
    check("gemini-limit: the same text read later resolves later (read once)",
          lb.gemini_reset_at(quota(), 2000.0)
          - lb.gemini_reset_at(quota(), 1000.0) == 1000.0)

    # --- the live latch ------------------------------------------------------
    writes: dict = {}

    def mk(name="Gem", kind=AgentKind.GEMINI):
        spec = build_spec(kind, name, cwd=os.getcwd(), pty=True)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d), True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    sent = lambda ag: "".join(writes.get(id(ag.worker), []))

    g = mk()
    g._on_pty_output("pty", "running the suite...\n" + FOOTER)
    check("gemini-limit: ordinary output is not a cut-off",
          not g.is_limit_blocked())
    g._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: the quota message IS a cut-off", g.is_limit_blocked())
    check("gemini-limit: its countdown is latched as a real reset time",
          g.limit_resets_at() is not None
          and 6000 < g.limit_resets_at() - _time.time() < 6100)
    # "quota" and not "weekly": agy states a DURATION, which is equally correct
    # for a window days out, so it never has the bare-wall-clock ambiguity that
    # makes a Claude weekly banner unusable
    check("gemini-limit: the window is ordinary and due on its own clock",
          g.limit_window() == "quota")

    # No menu exists, so the message alone must not re-latch an agent that has
    # already been resumed off it -- the guard Claude gets from the torn-down
    # menu, Gemini gets entirely from the message's identity.
    g.clear_limit_block()
    g._on_pty_output("pty", quota() + "\ncarrying on\n" + FOOTER)
    check("gemini-limit: the same message lingering after a resume does NOT "
          "re-latch", not g.is_limit_blocked())
    g._on_pty_output("pty", quota("58m12s", "301") + FOOTER)
    check("gemini-limit: a genuinely NEW refusal (its own Error ID) does latch",
          g.is_limit_blocked())

    notready = mk("NotReady")
    notready._prompt_ready = False
    notready._on_pty_output("pty", quota())
    check("gemini-limit: a message drawn before the prompt is ready is history",
          not notready.is_limit_blocked())

    # --- recovery after a restart, off the REPLAY ---------------------------
    # This IS Gemini's startup recovery: the live latch is never persisted and
    # there is no conversation on disk, but `agy --continue` redraws the
    # conversation it ended on, so the message is right there. Verified live
    # (2026-08-09, Agent 10): it latched 5 s after launch off the replay.
    #
    # THE COUNTDOWN IN IT IS STALE, and that is the defect this pins down. The
    # text says "1h39m56s" forever, so resolving it against the clock at launch
    # dated the reset 1h49m too late -- a quota that came back at 19:05 would
    # not have been touched until 21:54. It is discarded and the agent probed
    # at once; a quota that is still spent says so with an exact countdown.
    replay = mk("Replay")
    replay._session_started = _time.time()          # ...just launched
    replay._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a cut-off replayed at launch is recovered",
          replay.is_limit_blocked())
    check("gemini-limit: ...probed NOW, not on the replay's frozen countdown",
          replay.limit_resets_at() is not None
          and abs(replay.limit_resets_at() - _time.time()) < 5)
    check("gemini-limit: ...and it answers to the startup-recovery toggle",
          replay.limit_from_startup() is True)

    # a message with real work after it belongs to a cut-off the conversation
    # already recovered from -- continuing that would interrupt finished work
    carried = mk("CarriedOn")
    carried._session_started = _time.time()
    carried._on_pty_output("pty", quota()
                           + "".join(f"  Edit(file_{i}.py)\n" for i in range(14))
                           + "All 1324 tests passed cleanly!\n" + FOOTER)
    check("gemini-limit: a replayed message with work after it is NOT a "
          "live cut-off", not carried.is_limit_blocked())
    # ...and that judgement has to OUTLAST the launch window: the message sits
    # in the rolling tail long after, so without arming the echo guard the next
    # repaint would read it as live and latch its stale countdown
    carried._session_started = _time.time() - 3600
    carried._on_pty_output("pty", "  ? for shortcuts\n")
    check("gemini-limit: ...and a later repaint does not latch it either",
          not carried.is_limit_blocked())

    # once the launch window has passed, the printed countdown is current again
    live = mk("Live")
    live._session_started = _time.time() - 3600      # long since settled
    live._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a refusal outside the launch window keeps its own "
          "countdown", live.is_limit_blocked()
          and live.limit_resets_at() - _time.time() > 3000)
    check("gemini-limit: ...and is owned by the live auto-continue toggle",
          live.limit_from_startup() is False)

    # a provider with no patterns of its own is untouched by either CLI's
    shell = mk("Shell", kind=AgentKind.CMD)
    shell._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a plain shell agent is never limit-blocked",
          not shell.is_limit_blocked())

    # --- verification: agy answers a still-spent quota with a NEW message ----
    v = mk("Verify")
    v._on_pty_output("pty", quota() + FOOTER)
    first_reset = v.limit_resets_at()
    v._screen_tail = quota() + "\nContinue\n" + quota("59m1s", "500") + FOOTER
    check("gemini-limit: a fresh refusal after the nudge means STILL blocked",
          v.recheck_limit() is True and v.is_limit_blocked())
    check("gemini-limit: ...and the retry waits out the NEW countdown, not the "
          "spent one", v.limit_resets_at() != first_reset
          and v.limit_resets_at() - _time.time() > 3000)
    # ...and now nothing newer appears: the messages are still in the
    # scrollback (they never go away, there being no menu to tear down), but the
    # newest is the one already accounted for, so the agent is going again
    v._screen_tail = (quota() + "\nContinue\n" + quota("59m1s", "500")
                      + "\nworking on it...\n" + FOOTER)
    check("gemini-limit: the message merely lingering means it RESUMED",
          v.recheck_limit() is False and not v.is_limit_blocked())

    # --- the wiring ----------------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-gemlimit-"))
    win = create_main_window(SessionStore(path=tmp / "s.json"))
    win.show(); pump(50)
    ws = win.manager.workspaces[0]

    gem = mk("GemCut")
    gem._on_pty_output("pty", quota() + FOOTER)
    ws.agents.append(gem)
    check("gemini-limit: the workspace's blocked count sees it (hourglass)",
          win.manager.workspace_stats(ws.id)["limit_blocked"] == 1)

    # The account reading behind the no-`due` trigger is CLAUDE's plan, which
    # says nothing at all about a Gemini quota -- so that shortcut must not
    # reach a Gemini agent.
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: the Claude account's cleared edge does NOT resume a "
          "Gemini agent", sent(gem) == "" and gem.is_limit_blocked())

    # its own countdown does, through the network-free watchdog
    gem._limit_resets_at = now + 3600
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: an agent whose countdown has not run out waits",
          sent(gem) == "")
    gem._limit_resets_at = now - 600
    # ...and with NO usage reading of any kind in the app. The Gemini pill and
    # this path share nothing: the resume is driven entirely by the countdown
    # the agent's own message stated, so a usage readout that is missing, stale
    # or wrong can never be the reason a Continue fails to go out.
    check("gemini-limit: the resume path holds no usage reading at all",
          win.plan_usage() is None)
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: the watchdog resumes it once the countdown has passed",
          AUTO_CONTINUE_TEXT in sent(gem))
    # Esc closes CLAUDE's options menu. agy has none, and an Esc there would
    # clear whatever the user had half-typed instead.
    check("gemini-limit: no stray Esc is sent (there is no menu to dismiss)",
          "\x1b" not in sent(gem))
    win.close()


def test_startup_limit_recovery():
    """Agents the plan limit stopped BEFORE the app opened are found and armed.

    The live latch cannot see these: after a restart each agent's screen shows
    a REPLAYED conversation, which _scrape_limit deliberately ignores as
    history. The transcript is the durable record — it still ends exactly where
    the limit stopped it. The gate the user asked for is strict: no stoppage
    message, no action."""
    import json as _json
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import limit_banner, limit_ledger, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-startup-rec-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def iso(epoch):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(epoch))

    def write_transcript(cwd, sid, records):
        d = Path(transcripts.transcript_path(cwd, sid)).parent
        d.mkdir(parents=True, exist_ok=True)
        with open(transcripts.transcript_path(cwd, sid), "w",
                  encoding="utf-8") as fh:
            for r in records:
                fh.write(_json.dumps(r) + "\n")

    def assistant(text, at):
        return {"type": "assistant", "timestamp": iso(at),
                "message": {"content": [{"type": "text", "text": text}]}}

    def limit_banner_key(cwd_, sid, at):
        """The ledger identity of a cut-off written at `at` -- keyed on the
        window's RESET, the one number the live screen and the transcript both
        resolve identically."""
        reset = limit_banner.banner_reset_at(BANNER, at) or 0.0
        return limit_ledger.key_of(cwd_, sid, reset, at)

    now = _time.time()
    cwd = str(tmp)
    BANNER = "You've hit your session limit \xb7 resets 3am (Europe/Bucharest)"

    # --- reading the durable record -----------------------------------------
    # The MOST RECENT 02:42 local: the wall time matters (it is what makes
    # "resets 3am" land 18 minutes later, the anchoring this test is about) but
    # the DATE must not. Pinned to a fixed calendar day this test passed for a
    # while and then began failing on its own, once that day fell outside
    # STARTUP_RECOVERY_MAX_AGE_S and recovery correctly skipped it as stale.
    _lt = _time.localtime(now)
    cut_at = _time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday,
                           2, 42, 0, 0, 0, -1))
    if cut_at > now:
        cut_at -= 86400
    write_transcript(cwd, "sid-cut", [assistant("working", cut_at - 600),
                                      assistant(BANNER, cut_at)])
    hit, when, resets = transcripts.ended_on_limit(cwd, "sid-cut")
    check("startup-recovery: a transcript ending on the banner is a cut-off",
          hit and abs(when - cut_at) < 2)
    lt = _time.localtime(resets)
    check("startup-recovery: the reset is anchored to WHEN THE BANNER WAS "
          "WRITTEN, not to now (02:42 + 'resets 3am' -> 03:00 THAT day)",
          (lt.tm_hour, lt.tm_min) == (3, 0) and 0 < resets - cut_at < 3600)

    # the same banner resolved against a later clock lands a full day out --
    # the bug this anchoring exists to prevent
    naive = limit_banner.parse_reset_clock(BANNER, cut_at + 6 * 3600)
    check("startup-recovery: un-anchored parsing would stall a day",
          naive - resets > 80000)

    write_transcript(cwd, "sid-went-on", [assistant(BANNER, cut_at),
                                          assistant("carrying on", cut_at + 60)])
    hit2, _, _ = transcripts.ended_on_limit(cwd, "sid-went-on")
    check("startup-recovery: a banner followed by real output is history, "
          "not a cut-off", not hit2)
    check("startup-recovery: no transcript at all is not a cut-off",
          transcripts.ended_on_limit(cwd, "sid-missing")[0] is False)

    # A cut-off is only real when the turn it stopped was something the user
    # (or a delivered task) actually asked for. Claude Code can turn a
    # background command's own completion into a brand-new turn with NO input
    # from anyone -- if the account is exhausted right then, that turn hits
    # the identical banner a real interruption would, but nothing of the
    # agent's actual work was lost. Observed live: an agent long done with its
    # assigned task got auto-nudged over a stray background Playwright lookup
    # finishing hours later.
    def user_msg(content, at):
        return {"type": "user", "timestamp": iso(at),
                "message": {"content": content}}

    write_transcript(cwd, "sid-bg-notify", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("<task-notification>\n<task-id>abc</task-id>\n"
                 "<status>completed</status>\n</task-notification>", cut_at),
        assistant(BANNER, cut_at)])
    hit3, _, _ = transcripts.ended_on_limit(cwd, "sid-bg-notify")
    check("startup-recovery: a banner behind a <task-notification> (no real "
          "input from the user or AI Hive) is not a cut-off worth resuming",
          not hit3)

    write_transcript(cwd, "sid-real-ask", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("can you also add Chess960 castling support?", cut_at),
        assistant(BANNER, cut_at)])
    hit4, _, _ = transcripts.ended_on_limit(cwd, "sid-real-ask")
    check("startup-recovery: a banner behind a REAL user prompt is still a "
          "genuine cut-off", hit4)

    write_transcript(cwd, "sid-tool-result", [
        assistant("Done, merged and clean.", cut_at - 3600),
        {"type": "user", "timestamp": iso(cut_at),
         "message": {"content": [{"type": "tool_result",
                                  "content": [{"type": "text",
                                               "text": "exit 0"}]}]}},
        assistant(BANNER, cut_at)])
    hit5, _, _ = transcripts.ended_on_limit(cwd, "sid-tool-result")
    check("startup-recovery: a banner behind an ordinary tool-result reply "
          "(mid-turn, not synthetic) is still a genuine cut-off", hit5)

    # A slash command READS like plumbing -- a bare `<command-name>` string,
    # same shape as a <task-notification> -- but the user typed it, so the
    # work behind it is theirs and a cut-off there is as real as any other.
    # Keying "synthetic" off the leading "<" alone swept these in and would
    # have silently dropped the cut-off (real transcripts do carry a
    # `<command-name>` record followed straight by the assistant turn).
    write_transcript(cwd, "sid-slash-cmd", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("<command-name>/security-review</command-name>\n"
                 "<command-message>security-review</command-message>", cut_at),
        assistant(BANNER, cut_at)])
    hit6, _, _ = transcripts.ended_on_limit(cwd, "sid-slash-cmd")
    check("startup-recovery: a banner behind a USER-typed slash command is a "
          "genuine cut-off, not plumbing", hit6)

    # `synthetic` is positive evidence and must never stand in for "no banner
    # found": an early caller gates on it precisely because a banner Claude
    # has drawn but not yet WRITTEN reads as cut_off False on an existing
    # transcript, which is not evidence of anything.
    write_transcript(cwd, "sid-unflushed", [
        user_msg("please refactor the parser", cut_at - 60),
        assistant("Working on it now.", cut_at - 50)])
    unflushed = transcripts.limit_cut_off(cwd, "sid-unflushed")
    check("startup-recovery: an unwritten banner is NOT flagged synthetic "
          "(absence of evidence is not evidence)",
          unflushed is not None and not unflushed["cut_off"]
          and not unflushed["synthetic"], unflushed)
    check("startup-recovery: a real plumbing cut-off IS flagged synthetic",
          transcripts.limit_cut_off(cwd, "sid-bg-notify")["synthetic"])
    check("startup-recovery: a genuine cut-off is not flagged synthetic",
          not transcripts.limit_cut_off(cwd, "sid-real-ask")["synthetic"])

    # --- arming from it ------------------------------------------------------
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show(); pump(50)
    ws = win.manager.workspaces[0]

    def agent_for(name, sid):
        spec = build_spec(AgentKind.CLAUDE, name, cwd=cwd, pty=True)
        spec.session_id = sid
        a = TerminalAgent(spec)
        a.worker = type("W", (), {"is_running": lambda s: True,
                                  "write": lambda s, d: True,
                                  "start": lambda s: None,
                                  "dispose": lambda s: None})()
        a._prompt_ready = True
        ws.agents.append(a)
        return a

    cut = agent_for("Cut", "sid-cut")
    went_on = agent_for("WentOn", "sid-went-on")
    check("startup-recovery: armed exactly the cut-off agent",
          win.recover_blocked_at_startup() == 1)
    check("startup-recovery: the cut-off agent is latched, with its reset time",
          cut.is_limit_blocked() and cut.limit_resets_at() is not None)
    check("startup-recovery: the latch is marked as coming from startup",
          cut.limit_from_startup() is True)
    check("startup-recovery: an agent without the stoppage message is untouched",
          not went_on.is_limit_blocked())

    # A card left stopped is STARTED here, unlike an ordinary restore: it was
    # the limit that stopped this agent, not the user, so leaving it alone
    # would drop exactly the work this feature exists to rescue. It must come
    # back as a RESUME -- a fresh start would mint a new session id and abandon
    # the transcript that proved the cut-off.
    stopped = agent_for("Stopped", "sid-cut")
    started = []
    stopped.worker = type("W", (), {"is_running": lambda s: False,
                                    "write": lambda s, d: True,
                                    "start": lambda s: started.append(1),
                                    "dispose": lambda s: None})()
    win.recover_blocked_at_startup()
    check("startup-recovery: an agent the LIMIT stopped is started, not left "
          "stopped", stopped.is_limit_blocked() and started == [1])
    check("startup-recovery: it is started as a resume, keeping its pinned "
          "conversation", stopped.spec.session_id == "sid-cut")

    # --- only the most recent window is acted on ----------------------------
    # This replaced a fixed age bound, which asked the wrong question (how OLD
    # is this) instead of the one that decides (was it the LAST thing that
    # happened) -- and had begun silently skipping real cut-offs once its
    # 36-hour window elapsed. Agents stopped by one window all state the same
    # reset clock, so they group; an earlier window is recorded, not revived.
    older_at, newer_at = now - 30 * 3600, now - 2 * 3600
    write_transcript(cwd, "sid-win-old", [assistant(BANNER, older_at)])
    write_transcript(cwd, "sid-win-new", [assistant(BANNER, newer_at)])
    w_old = agent_for("WinOld", "sid-win-old")
    w_new = agent_for("WinNew", "sid-win-new")
    win.recover_blocked_at_startup()
    check("startup-recovery: the most recent window is armed",
          w_new.is_limit_blocked())
    check("startup-recovery: an earlier window is left alone",
          not w_old.is_limit_blocked())

    # ...but it IS recorded: the ledger is complete even where the action is
    # selective, because the record is what the next feature reads
    entries = limit_ledger.read_all(str(tmp))
    filed = {r.get("session_id") for r in entries
             if r.get("event") == limit_ledger.CUT_OFF}
    check("startup-recovery: every cut-off found is filed in the ledger, "
          "including the ones not acted on",
          {"sid-cut", "sid-win-old", "sid-win-new"} <= filed)
    newest = next(r for r in entries if r.get("session_id") == "sid-win-new")
    check("startup-recovery: the record names workspace, agent and local time",
          newest["ws_name"] and newest["agent_name"] == "WinNew"
          and newest["at_local"][:4].isdigit()
          and abs(newest["at"] - newer_at) < 2)

    # a cut-off already resolved in an EARLIER run is never revived: without
    # this an abandoned conversation whose transcript still ends on the banner
    # would be resumed afresh on every launch
    limit_ledger.record_outcome(
        str(tmp), limit_banner_key(cwd, "sid-win-new", newer_at),
        limit_ledger.RESUMED, tries=1)
    again = agent_for("Again", "sid-win-new")
    win.recover_blocked_at_startup()
    check("startup-recovery: a cut-off already resolved is not revived",
          not again.is_limit_blocked())

    # The `is_pty` gate above is unreachable for a claude agent, and that is
    # load-bearing rather than incidental: CLAUDE is in PTY_ONLY_KINDS, so
    # `build_spec` forces pty=True -- including inside `AgentSpec.from_dict`,
    # which is what makes a session record that has LOST its pty field (see
    # the degraded-save fallback in test_v3_features) still restore as a real
    # terminal instead of a line-mode card that this feature would skip.
    from app.process_worker import AgentSpec as _Spec
    check("startup-recovery: a claude agent is pty even when asked not to be",
          build_spec(AgentKind.CLAUDE, "X", cwd=cwd, pty=False).pty is True)
    check("startup-recovery: ...and a record with no pty field restores as one",
          _Spec.from_dict({"kind": "claude", "name": "X", "cwd": cwd,
                           "provider": "claude", "session_id": "s"}).pty is True)

    # --- the toggle owns its own latches ------------------------------------
    win._startup_recovery = False
    fresh = agent_for("Fresh", "sid-cut")
    check("startup-recovery: scanning is skipped entirely while the toggle is "
          "off", win.recover_blocked_at_startup() == 0
          and not fresh.is_limit_blocked())

    # a startup latch must NOT be resumed by the OTHER toggle
    writes = []
    cut.worker = type("W", (), {
        "is_running": lambda s: True,
        "write": lambda s, d: (writes.append(d), True)[1],
        "start": lambda s: None, "dispose": lambda s: None})()
    cut._limit_resets_at = now - 3600  # its reset is well past the grace
    win._auto_continue = True          # the other switch is on...
    win._check_limit_resets()          # ...and must not act on a startup latch
    pump(1200)
    check("startup-recovery: resume-on-reset does NOT resume a startup latch",
          writes == [])
    win._startup_recovery = True
    win._check_limit_resets()
    pump(1200)
    check("startup-recovery: its own toggle DOES resume it",
          any("Continue" in d for d in writes))

    # --- both preferences persist -------------------------------------------
    win._on_startup_recovery(False)
    win._on_auto_continue(True)
    ui = win._session_payload()["ui"]
    check("startup-recovery: preference persisted under ui.startup_recovery",
          ui["startup_recovery"] is False and ui["auto_continue"] is True)
    win.close()

    win2 = create_main_window(store)
    win2.show(); pump(50)
    check("startup-recovery: both toggles restore independently",
          win2.top_bar.startup_recovery() is False
          and win2.top_bar.auto_continue() is True)
    check("startup-recovery: defaults are ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")).top_bar.startup_recovery())
    win2.close()


def test_limit_recovery_reliability():
    """The four ways a real cut-off (2026-08-07, CVsummer2026) went unrecovered.

    Every one of them is silent by construction — the agent simply sits there —
    so each gets a check that reproduces the exact screen or timing shape that
    defeated it. See app/terminal_agent.py `_tail_lines` and `mark_limit_blocked`
    for the reasoning behind the fixes."""
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import (REPAINT_RESTORE_MS, AgentStatus,
                                    TerminalAgent)
    from app.widgets.main_window import LIMIT_REPAINT_AFTER_WAITS
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limit-rel-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    BANNER = ("You've hit your session limit \xb7 resets 9:30pm "
              "(Europe/Bucharest)")
    # a LATER window's cut-off: successive 5-hour windows never end at the same
    # wall time, which is what makes the banner line a cut-off's identity
    NEXT = ("You've hit your session limit \xb7 resets 2:30am "
            "(Europe/Bucharest)")
    MENU = ("What do you want to do?\n"
            "> 1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n")
    # Claude's TUI pads its frame with blank rows, so the banner ends up far
    # above the bottom in LINES while being close to it in CONTENT. Measured on
    # real screen snapshots: a raw [-40:] slice spanned 432 chars / 5 non-blank
    # lines. This is the frame that lost a genuine cut-off.
    PADDED = BANNER + "\n" + "\n" * 60 + "> try \"fix the tests\"\n  ? for shortcuts\n"

    def mk(name="Coder"):
        a = TerminalAgent(build_spec(AgentKind.CLAUDE, name, cwd=os.getcwd()))
        a._prompt_ready = True
        return a

    # --- 1. the scrape window counts CONTENT, not padding -------------------
    a = mk()
    a._screen_tail = PADDED
    a._scrape_limit()
    check("limit-scrape: a banner above the TUI's blank padding still latches",
          a.is_limit_blocked())
    check("limit-scrape: ...and it carries the reset clock the banner stated",
          a.limit_resets_at() is not None)

    # the two callers that deliberately keep RAW-line windows must not have
    # been widened along with it: recheck_limit reaching further back would
    # find a torn-down menu forever and never report a resume
    b = mk()
    b.mark_limit_blocked(_time.time() + 60, from_startup=False, banner=BANNER)
    b._screen_tail = MENU + "\n" * 60 + "  ? for shortcuts\n"
    check("limit-scrape: recheck_limit still uses raw lines (a menu pushed "
          "past 40 raw lines reads as resumed)",
          b.recheck_limit() is False)

    # --- 2. a disk-recovered latch arms the banner-echo guard ---------------
    # The live re-latch this prevents was observed one SECOND after a verified
    # resume, and dated its phantom cut-off a full day out.
    c = mk()
    c.mark_limit_blocked(1786127400.0, from_startup=True,
                         cut_off_at=1786127061.0, window="session",
                         banner=BANNER)
    c.clear_limit_block()          # what a successful resume does
    c._screen_tail = PADDED        # the same banner, still on screen
    c._scrape_limit()
    check("limit-echo: a startup-armed latch is not re-raised by its own "
          "banner after the resume",
          not c.is_limit_blocked())
    c._screen_tail = NEXT + "\n" + "\n" * 60 + "  ? for shortcuts\n"
    c._scrape_limit()
    check("limit-echo: ...but a genuinely NEW cut-off still latches",
          c.is_limit_blocked())

    # --- 3. readiness is re-checked once the screen settles -----------------
    # _on_pty_output decides readiness against a 600-char tail, once per burst.
    # A footer followed by more than that in the same burst is never seen, and
    # a child that then falls quiet is never looked at again.
    d = mk()
    d._prompt_ready = False
    d.status = AgentStatus.RUNNING
    d._on_pty_output("pty", "welcome\n  ? for shortcuts\n"
                     + ("x" * 400 + "\n") * 3)
    check("prompt-ready: a footer buried in its own burst is missed per-burst",
          not d.prompt_ready())
    d._on_idle_timeout()
    check("prompt-ready: ...and recovered when the screen settles",
          d.prompt_ready())

    e = mk()
    e._prompt_ready = False
    e.status = AgentStatus.RUNNING
    e._on_pty_output("pty", "booting" + "y" * 2000)
    e._on_idle_timeout()
    check("prompt-ready: a settle with no footer at all stays not-ready",
          not e.prompt_ready())

    # --- 4. request_repaint, for a card that never gets a resizeEvent -------
    f = mk()
    check("repaint: refused when the child isn't running", not f.request_repaint())
    resizes: list = []
    f.worker.rows, f.worker.cols = 30, 100
    f.worker.is_running = lambda: True
    f.worker.resize = lambda r, c: resizes.append((r, c))
    check("repaint: accepted for a live pty agent", f.request_repaint())
    check("repaint: narrows first", resizes == [(30, 99)])
    pump(REPAINT_RESTORE_MS + 120)
    check("repaint: and gives the width straight back",
          resizes == [(30, 99), (30, 100)])

    # --- 5. the watchdog stops waiting forever ------------------------------
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    ws = win.manager.create_workspace("W", str(tmp))
    agent = win.manager.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "A",
                                                       cwd=str(tmp)))
    agent._prompt_ready = False    # a card the user has never opened
    asked: list = []
    agent.request_repaint = lambda: (asked.append(1), True)[1]
    agent.worker.is_running = lambda: True
    agent.mark_limit_blocked(_time.time() - 60, from_startup=True,
                             banner=BANNER)
    for _ in range(LIMIT_REPAINT_AFTER_WAITS - 1):
        win._auto_continue_agent(agent)
    check("limit-wait: a booting TUI is left alone at first", not asked)
    win._auto_continue_agent(agent)
    check("limit-wait: after a few quiet ticks the TUI is asked to redraw",
          len(asked) == 1)
    win._auto_continue_agent(agent)
    check("limit-wait: and it is asked exactly once, not every tick",
          len(asked) == 1)
    check("limit-wait: a refused resume still consumes no attempt",
          agent.limit_attempts() == 0)
    agent._prompt_ready = True
    win._auto_continue_agent(agent)
    check("limit-wait: the counter resets once the prompt is live",
          win._limit_wait_ticks.get(agent.id) is None)
    win.close()


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

    spec = build_spec(from_qt, "Agent 1", cwd=os.getcwd())
    check("agent-kind: build_spec coerces it back to the enum",
          spec.kind is AgentKind.CLAUDE)
    check("agent-kind: so the agent serializes instead of degrading",
          spec.to_dict()["kind"] == "claude")
    check("agent-kind: an enum in still comes out unchanged",
          build_spec(AgentKind.CMD, "S", cwd=os.getcwd()).kind is AgentKind.CMD)


def test_scheduled_send():
    """A message the user writes now and has typed in LATER.

    Ctrl+Shift+Enter in a terminal is "Enter, but on a countdown" - the gesture
    that makes chaining agents possible while away from the machine. The rules
    that matter are the ones about NOT sending: a scheduled message is a nudge
    and never an assignment, and one that came due while the app was closed is
    surfaced as missed rather than fired hours late into a conversation that has
    moved on."""
    import time as _time
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from app import scheduled_send as ss
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import AssignmentState, TerminalAgent
    from app.widgets.main_window import SCHEDULE_GIVE_UP_S
    from app.workspace_manager import WorkspaceManager
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()

    # --- parsing: what a person types into a "send in" box ------------------
    delays = {"45": 2700, "45m": 2700, "30 min": 1800, "1h": 3600,
              "1h30": 5400, "1h30m": 5400, "1.5h": 5400, "90s": 90,
              "2:15": 8100, "1h 30m 10s": 5410}
    bad = ["", "   ", "abc", "0", "45x", "-5", "200h"]  # 200h > the week cap
    check("schedule: delays parse (bare number = minutes, 1h30, 90s, 2:15)",
          all(ss.parse_delay(k) == v for k, v in delays.items()),
          {k: ss.parse_delay(k) for k, v in delays.items()
           if ss.parse_delay(k) != v})
    check("schedule: junk and non-positive delays are rejected",
          all(ss.parse_delay(b) is None for b in bad),
          [b for b in bad if ss.parse_delay(b) is not None])
    # a bare clock has no date, so a time already past today means tomorrow --
    # the rollover that matters when scheduling late at night for the morning
    anchor = _time.mktime((2026, 8, 7, 14, 0, 0, 0, 0, -1))
    later = ss.parse_clock("15:30", anchor)
    tomorrow = ss.parse_clock("03:30", anchor)
    check("schedule: a clock still ahead today resolves to today",
          later is not None and 0 < later - anchor < 86400
          and _time.localtime(later).tm_mday == 7)
    check("schedule: a clock already past resolves to TOMORROW",
          tomorrow is not None and _time.localtime(tomorrow).tm_mday == 8)
    check("schedule: pm/am clocks parse",
          ss.parse_clock("3pm", anchor) == ss.parse_clock("15:00", anchor))
    check("schedule: an impossible clock is rejected",
          ss.parse_clock("25:00", anchor) is None)
    check("schedule: countdowns format h:mm:ss / m:ss and clamp at zero",
          (ss.format_countdown(3862), ss.format_countdown(724),
           ss.format_countdown(9), ss.format_countdown(-5))
          == ("1:04:22", "12:04", "0:09", "0:00"))

    # a malformed persisted row must never cost an agent its other messages
    check("schedule: an unusable persisted row decodes to None, not a crash",
          ss.ScheduledMessage.from_dict({"text": "", "due_ts": 1}) is None
          and ss.ScheduledMessage.from_dict({"text": "x"}) is None
          and ss.ScheduledMessage.from_dict({"text": "x", "due_ts": "no"})
          is None)

    # --- the queue on the agent --------------------------------------------
    writes: dict = {}

    def mk(name="Coder", pty=True):
        spec = build_spec(AgentKind.CLAUDE, name, cwd=os.getcwd(), pty=pty)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d),
                                   True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    def sent(agent):
        return "".join(writes.get(id(agent.worker), []))

    a = mk()
    edges = []
    a.scheduled_changed.connect(lambda: edges.append(1))
    msg = a.schedule_message("run the smoke suite", now + 600)
    check("schedule: a queued message is held and announced once",
          msg is not None and len(a.pending_scheduled()) == 1 and edges == [1])
    check("schedule: an empty message is refused",
          a.schedule_message("   ", now + 60) is None)
    check("schedule: the soonest message is the one shown",
          a.schedule_message("later", now + 9000) is not None
          and a.next_scheduled().text == "run the smoke suite")
    check("schedule: nothing is due before its time", a.due_scheduled(now) == [])
    check("schedule: it is due at its time",
          [m.text for m in a.due_scheduled(now + 601)]
          == ["run the smoke suite"])
    for i in range(ss.MAX_PER_AGENT):
        a.schedule_message(f"filler {i}", now + 4000 + i)
    check("schedule: an agent caps how many it will hold",
          len(a.pending_scheduled()) == ss.MAX_PER_AGENT)
    a._scheduled = [m for m in a._scheduled if not m.text.startswith("filler")]

    # --- editing an already-queued message in place, instead of cancel+ ----
    # recreate (which would silently lose its spot in the queue)
    edges.clear()
    ok = a.reschedule(msg.id, "run the smoke suite twice", now + 1200)
    check("schedule: reschedule edits text and due_ts in place, same id",
          ok and msg.text == "run the smoke suite twice"
          and msg.due_ts == now + 1200 and edges == [1])
    check("schedule: an unknown id is refused",
          not a.reschedule("no-such-id", "x", now + 60))
    check("schedule: empty text is refused, existing message unchanged",
          not a.reschedule(msg.id, "   ", now + 1)
          and msg.text == "run the smoke suite twice")
    stale = a.schedule_message("stale", now - 10)
    a.mark_scheduled_missed(stale.id)
    check("schedule: a missed message given a future time revives to PENDING",
          a.reschedule(stale.id, "stale", now + 300)
          and stale.state == ss.PENDING)
    a._scheduled = [m for m in a._scheduled if m.id != stale.id]

    # --- delivery is a NUDGE, never an assignment --------------------------
    # deliver_task overwrites the persisted current_task, flips the assignment
    # to WORKING and re-infers the role. The user pressed a deferred Enter; they
    # did not assign anything, so none of that may move.
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-sched-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show()
    mgr = win.manager
    ws = mgr.workspaces[0]

    d = mk("Deliver")
    d.set_task("the original task")
    d.set_assignment(AssignmentState.COMPLETED)
    d.spec.role = "Reviewer"
    before = (d.current_task, d.assignment, d.spec.role)
    due = d.schedule_message("please continue", now - 1)
    ws.agents.append(d)
    win._tick_schedules()
    check("schedule: a due message is typed into the agent",
          "please continue" in sent(d))
    check("schedule: delivery leaves task/assignment/role untouched "
          "(nudge, not deliver_task)",
          (d.current_task, d.assignment, d.spec.role) == before,
          (d.current_task, d.assignment, d.spec.role))
    check("schedule: a sent message is dropped from the queue",
          d.scheduled_messages() == [])
    check("schedule: delivery does not stamp the user-input clock "
          "(the work it starts still pulses the sidebar)",
          d._last_input_ts == 0.0)

    # --- a refusal is retried, then given up on as MISSED -------------------
    r = mk("NotReady")
    r._prompt_ready = False
    late = r.schedule_message("go", now - 5)
    ws.agents.append(r)
    win._tick_schedules()
    check("schedule: an agent whose prompt is not ready is not typed into",
          sent(r) == "")
    check("schedule: ...and the message stays queued for the next tick",
          late.is_pending() and late.attempts == 1)
    late.due_ts = now - SCHEDULE_GIVE_UP_S - 1
    win._tick_schedules()
    check("schedule: past the give-up window it becomes MISSED, not sent",
          late.state == ss.MISSED and sent(r) == "")
    check("schedule: a missed message is KEPT so the user can see it",
          [m.state for m in r.scheduled_messages()] == [ss.MISSED])

    # an agent parked on the plan limit must not be typed into: the text would
    # land in the limit's options menu, not the prompt underneath it
    b = mk("Blocked")
    b.mark_limit_blocked(now + 3600)
    b.schedule_message("go", now - 1)
    ws.agents.append(b)
    win._tick_schedules()
    check("schedule: an agent parked on the plan limit is not typed into",
          sent(b) == "" and b.pending_scheduled())

    # --- the countdown tick must never touch the session file ---------------
    saves = []
    mgr.dirty.connect(lambda: saves.append(1))
    t = mk("Ticker")
    ws.agents.append(t)
    mgr._wire_agent(ws, t)
    t.schedule_message("soon", now + 3600)
    queued_saves = len(saves)
    check("schedule: queueing a message DOES mark the session dirty "
          "(the queue is persisted)", queued_saves >= 1)
    for _ in range(5):
        win._tick_schedules()
    check("schedule: the per-second tick marks the session dirty ZERO times",
          len(saves) == queued_saves, len(saves) - queued_saves)
    t.cancel_scheduled(t.next_scheduled().id)
    check("schedule: cancelling drops it and marks dirty",
          not t.scheduled_messages() and len(saves) > queued_saves)

    # the tick only RUNS while something is queued, so a hive with nothing
    # scheduled pays nothing for the feature
    for agent in (d, r, b, t):
        agent._scheduled.clear()
    win._sync_schedule_timer()
    check("schedule: the tick timer stops when nothing is queued",
          not win._schedule_timer.isActive())
    t.schedule_message("wake up", now + 60)
    win._sync_schedule_timer()
    check("schedule: the tick timer runs while something is queued",
          win._schedule_timer.isActive())
    # QTimer.start() RESTARTS a running timer, and this is called from
    # workspaceStatsChanged (which fires every couple of seconds per busy
    # agent) -- an unconditional start would reset the countdown forever
    win._schedule_timer.setInterval(50000)
    win._sync_schedule_timer()
    check("schedule: re-syncing an already-running tick does not restart it",
          win._schedule_timer.remainingTime() <= 50000)
    win._schedule_timer.setInterval(1000)

    check("schedule: workspace stats count agents holding a message",
          mgr.workspace_stats(ws.id)["scheduled"] == 1)

    # ...and the whole thing runs on its OWN timer. Every check above drives
    # _tick_schedules by hand, which proves the logic but not the feature: this
    # one queues a message, touches nothing, and waits for it to arrive.
    from PySide6.QtCore import QEventLoop, QTimer

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    live = mk("Live")
    ws.agents.append(live)
    live.schedule_message("wake up and work", _time.time() + 0.2)
    win._sync_schedule_timer()
    pump(1600)
    check("schedule: the countdown fires on its own timer and delivers",
          "wake up and work" in sent(live) and not live.scheduled_messages(),
          sent(live))

    # --- persistence, and the rule about coming back late -------------------
    m2 = WorkspaceManager()
    ahead, behind = now + 7200, now - 7200
    keeper = mk("Keeper")
    keeper.schedule_message("still ahead", ahead)
    keeper.schedule_message("long overdue", behind)
    ws.agents.append(keeper)
    data = mgr.to_session_dict()
    rows = [t_ for w in data["workspaces"] for t_ in w["terminals"]
            if t_.get("name") == "Keeper"]
    check("schedule: pending messages are persisted with the agent",
          len(rows) == 1 and len(rows[0].get("scheduled", [])) == 2,
          rows)
    m2.load_session_dict(data)
    back = next((x for x in m2.all_agents() if x.spec.name == "Keeper"), None)
    states = {m.text: m.state for m in (back.scheduled_messages() if back else [])}
    check("schedule: a message still ahead comes back PENDING",
          states.get("still ahead") == ss.PENDING, states)
    # THE RULE: a 3am message the app was closed for must NOT fire at 10am into
    # a conversation that has moved on. It comes back visible, not delivered.
    check("schedule: a message that came due while the app was closed comes "
          "back MISSED, never sent", states.get("long overdue") == ss.MISSED,
          states)
    check("schedule: a session with no queue restores cleanly",
          m2.load_session_dict({"workspaces": [{"id": "w", "name": "W",
                                                "project_path": os.getcwd(),
                                                "terminals": []}]}) is None)
    # the degraded save path keeps the queue too: it exists so a malformed
    # agent loses as little as possible, and a dropped hand-off is a real loss
    broken = mk("Broken")
    broken.schedule_message("survive the degrade", ahead)
    broken.spec.kind = "not-an-enum"       # AgentSpec.to_dict raises on .value
    degraded = mgr._agent_dict_safe(broken)
    check("schedule: the degraded save record still carries the queue",
          len(degraded.get("scheduled", [])) == 1, degraded)

    # --- the gesture --------------------------------------------------------
    from app.widgets.terminal_view import TerminalView
    view = TerminalView(rows=24, cols=80)
    seen, keys = [], []
    view.scheduleRequested.connect(seen.append)
    view.keyInput.connect(keys.append)

    def press(key, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods, "\r"))

    view.feed("> run the tests")
    press(Qt.Key.Key_Return, ctrl=True, shift=True)
    check("schedule: Ctrl+Shift+Enter asks for a countdown, carrying what is "
          "typed", seen == ["run the tests"], seen)
    check("schedule: ...and sends NOTHING to the child (no submit, no clear)",
          keys == [], keys)
    # Ctrl+Enter is NOT available for this: it inserts a newline, which is how
    # multi-line input works in Claude Code
    press(Qt.Key.Key_Return, ctrl=True)
    check("schedule: plain Ctrl+Enter still inserts a newline",
          keys == ["\n"] and len(seen) == 1, (keys, seen))
    view.deleteLater()

    # a genuine wrapped/multi-line message (no prompt glyph on continuation
    # rows) must still capture in full
    view2 = TerminalView(rows=24, cols=80)
    seen2 = []
    view2.scheduleRequested.connect(seen2.append)
    view2.feed("> line one\r\nline two")
    view2.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: a real wrapped continuation line is captured in full",
          seen2 == ["line one\nline two"], seen2)
    view2.deleteLater()

    # Claude Code paints its footer hint directly under the box with NO
    # blank line in between, then repositions the caret back onto the input
    # row (a full-screen TUI redraw, not a plain linefeed) -- that hint row
    # (and anything under it) must never be swept into the captured message
    view3 = TerminalView(rows=24, cols=80)
    seen3 = []
    view3.scheduleRequested.connect(seen3.append)
    view3.feed("> send this only" "\x1b[2;1H? for shortcuts" "\x1b[1;17H")
    view3.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: the footer hint under the box is not swept into the "
          "captured message", seen3 == ["send this only"], seen3)
    view3.deleteLater()

    # Claude Code also paints a plain divider/box-border row between the
    # input and its footer hint, again with no blank line -- that must not
    # be swept in either (it showed up literally as a line of dashes in a
    # scheduled message's prefill)
    view4 = TerminalView(rows=24, cols=80)
    seen4 = []
    view4.scheduleRequested.connect(seen4.append)
    view4.feed("> send this only" + "\x1b[2;1H" + ("─" * 40)
               + "\x1b[3;1H? for shortcuts" + "\x1b[1;17H")
    view4.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: a divider row under the box is not swept into the "
          "captured message", seen4 == ["send this only"], seen4)
    view4.deleteLater()

    # --- the composer -------------------------------------------------------
    from PySide6.QtWidgets import QDialog, QDialogButtonBox
    from app.widgets.main_window import ScheduleMessageDialog

    comp = mk("Composer")
    dlg = ScheduleMessageDialog(comp, parent=win, prefill="deploy the thing")
    ok_btn = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    check("schedule: the composer opens prefilled with what was typed",
          dlg.text_edit.toPlainText() == "deploy the thing")
    check("schedule: it opens on a usable default delay",
          ok_btn.isEnabled() and dlg.result_message() is not None)
    # a preset must be written in the DELAY vocabulary, never the countdown
    # one: "5:00" reads back as five HOURS, not five minutes
    preset_bad = []
    for label, seconds in ScheduleMessageDialog.PRESETS:
        dlg._set_preset(seconds)
        got = ss.parse_delay(dlg.delay_edit.text())
        if got != seconds:
            preset_bad.append(f"{label}: {dlg.delay_edit.text()!r}={got}")
    check("schedule: every preset button means what its label says",
          not preset_bad, preset_bad)
    dlg.delay_edit.setText("90m")
    dlg._revalidate()
    text, when = dlg.result_message()
    check("schedule: a custom delay resolves to a fire time",
          text == "deploy the thing" and 5300 < when - _time.time() < 5500)
    check("schedule: the composer says exactly when it will fire",
          "in 1:29" in dlg.when_label.text(), dlg.when_label.text())
    # the two time fields are alternatives: there must never be a hidden second
    # answer deciding the fire time
    dlg.clock_edit.setText("03:30")
    dlg._on_time_edited(False)
    check("schedule: typing a clock clears the delay field (one answer only)",
          dlg.delay_edit.text() == ""
          and dlg.result_message()[1] == ss.parse_clock("03:30"))
    dlg.text_edit.setPlainText("   ")
    check("schedule: an empty message cannot be scheduled",
          not ok_btn.isEnabled() and dlg.result_message() is None)
    dlg.text_edit.setPlainText("ok")
    dlg.clock_edit.setText("nonsense")
    dlg._on_time_edited(False)
    check("schedule: an unparseable time cannot be scheduled",
          not ok_btn.isEnabled() and dlg.result_message() is None)
    comp.schedule_message("already queued", now + 60)
    dlg.refresh_pending()
    check("schedule: the composer lists what is already queued",
          dlg.pending_box.count() > 0)

    # --- editing an existing entry in place, via its row's ✏ button --------
    target = comp.next_scheduled()
    dlg._start_edit(target)
    check("schedule: edit loads the message's text into the form",
          dlg.text_edit.toPlainText() == "already queued"
          and dlg.editing_id == target.id)
    check("schedule: the OK button reads Save while editing",
          ok_btn.text() == "Save")
    dlg._cancel(target)
    check("schedule: cancelling the row you're editing exits edit mode",
          dlg.editing_id is None and ok_btn.text() == "Schedule")
    dlg.deleteLater()

    # editing end-to-end through the popup updates the SAME entry in place --
    # not a second one alongside it
    editable = mk("Editable")
    ws.agents.append(editable)
    original = editable.schedule_message("first draft", now + 500)
    real_exec2 = ScheduleMessageDialog.exec
    try:
        def fake_edit_exec(self):
            self._start_edit(self.agent.next_scheduled())
            self.text_edit.setPlainText("revised draft")
            self.delay_edit.setText("15m")
            self._revalidate()
            return QDialog.DialogCode.Accepted
        ScheduleMessageDialog.exec = fake_edit_exec
        win._on_schedule_message(editable.id)
    finally:
        ScheduleMessageDialog.exec = real_exec2
    check("schedule: editing through the popup rewrites the entry in place",
          [m.text for m in editable.scheduled_messages()] == ["revised draft"]
          and editable.next_scheduled().id == original.id,
          [m.text for m in editable.scheduled_messages()])

    # confirming from the TERMINAL gesture also clears the child's input box:
    # the text now lives in AI Hive, so a copy left in the prompt would be
    # submitted a second time the moment the user pressed Enter
    gest = mk("Gesture")
    ws.agents.append(gest)
    real_exec = ScheduleMessageDialog.exec
    try:
        ScheduleMessageDialog.exec = lambda self: (
            self.text_edit.setPlainText("scheduled from the terminal"),
            self.delay_edit.setText("10m"), self._revalidate(),
            QDialog.DialogCode.Accepted)[-1]
        win._on_schedule_message(gest.id, "scheduled from the terminal")
    finally:
        ScheduleMessageDialog.exec = real_exec
    check("schedule: confirming queues the message",
          [m.text for m in gest.pending_scheduled()]
          == ["scheduled from the terminal"])
    check("schedule: ...and clears the child's input box (double-Escape), so "
          "the text is never submitted twice", sent(gest) == "\x1b\x1b")

    # --- the card chip ------------------------------------------------------
    page = win._pages[ws.id]
    chip_agent = mgr.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "Chip", cwd=os.getcwd()),
        autostart=False)
    card = page.card_for(chip_agent.id)
    check("schedule: a card with nothing queued shows no countdown chip",
          card is not None and not card.sched_mark.isVisible())
    chip_agent.schedule_message("later", now + 724)
    check("schedule: the chip appears with the countdown to the soonest one",
          card.sched_mark.isVisible() and "12:0" in card.sched_mark.text(),
          card.sched_mark.text())
    chip_agent.mark_scheduled_missed(chip_agent.next_scheduled().id)
    check("schedule: a missed message flips the chip to its warning state",
          card.sched_mark.property("missed") is True
          and "missed" in card.sched_mark.text())
    chip_agent.cancel_scheduled(chip_agent.scheduled_messages()[0].id)
    check("schedule: cancelling the last message hides the chip again",
          not card.sched_mark.isVisible())

    # --- the sidebar's own clock, next to the agent in its inline row -------
    from app.widgets.sidebar import AgentRow

    sb_agent = mgr.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "SidebarSched", cwd=os.getcwd()),
        autostart=False)
    row = AgentRow(ws.id, sb_agent)
    check("schedule: the sidebar row's clock is hidden with nothing queued",
          row.sched_mark.isHidden())
    sb_agent.schedule_message("ping later", now + 300)
    row.refresh(sb_agent)
    check("schedule: it shows once something is queued",
          not row.sched_mark.isHidden())
    sched_hits, act_hits = [], []
    row.schedRequested.connect(lambda w, a: sched_hits.append((w, a)))
    row.activated.connect(lambda w, a: act_hits.append((w, a)))
    row.sched_mark.click()
    check("schedule: clicking it emits schedRequested(ws_id, agent_id)",
          sched_hits == [(ws.id, sb_agent.id)], sched_hits)
    check("schedule: ...and the click is CONSUMED, not also a row-wide "
          "'reveal the card' activation", act_hits == [], act_hits)
    row.deleteLater()

    win.close()


def test_projection_happens_once():
    """The scrollback is projected ONCE, at the card's settled width.

    pyte does not reflow, so a card built before the tiling grid sizes it has
    to project again at the real width. The cost of that is paid on the GUI
    thread per card, and the card used to do the FULL projection twice -- once
    at a width that never reached the screen -- plus a full transcript read
    each time. Measured on real captures: ~80ms + 90-165ms per card, per
    projection, which is what made launch and every retile visibly freeze."""
    import pathlib
    import tempfile

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets import terminal_card as tc

    if not HAS_CONPTY:
        return
    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aihive-project-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    check("project: the seed cap is far smaller than the full cap",
          tc.REPLAY_SEED_CAP < tc.REPLAY_PROJECT_CAP // 4,
          (tc.REPLAY_SEED_CAP, tc.REPLAY_PROJECT_CAP))

    # a buffer much larger than the seed cap, with a marker line right at the
    # start so we can tell a seed-sized projection from a full one
    head = "HEADLINE-" + "h" * 40
    body = "".join(f"body line {i} " + "b" * 40 + "\r\n" for i in range(4000))
    big = head + "\r\n" + body

    # -- an ordinary rebuild (retile / workspace switch): the buffer IS the
    #    live conversation, so the settled projection must project it in full
    live = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Live", cwd=str(tmp), pty=True))
    live._pty_buffer.append(big)
    live._pty_bytes = len(big)
    live._pty_total = len(big)
    card = tc.TerminalCard(live)
    seeded_hist = len(card.terminal.screen.history.top)
    check("project: the constructor projects only a screenful, not the cap",
          0 < seeded_hist < 400, seeded_hist)
    check("project: ...and defers the real one", bool(card._pending_replay))

    card.terminal.resize(900, 500)
    card._rerender_restored()
    full_hist = len(card.terminal.screen.history.top)
    check("project: the settled projection is the FULL one",
          full_hist > seeded_hist * 3, (seeded_hist, full_hist))
    check("project: ...and it projects the LIVE buffer, so a busy agent's "
          "rebuilt card is not left holding only the seed",
          full_hist > 1000, full_hist)
    check("project: it records the width it projected at, so the very next "
          "resize check is not a redundant third projection",
          card._proj_cols == card.terminal.screen.columns,
          (card._proj_cols, card.terminal.screen.columns))
    check("project: the pending marker is consumed, so a resize storm "
          "cannot project again", card._pending_replay == "")
    card.detach(); live.dispose(); pump(30)

    # -- a restored snapshot a child has since drawn over is NOT replayed
    #    under it (an agent that comes back running gets a clean terminal)
    seed = "PREVIOUS-RUN-SCREEN\r\n"
    over = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Over", cwd=str(tmp), pty=True))
    over.seed_pty_replay(seed)
    card2 = tc.TerminalCard(over)
    check("project: a seed no child has touched is not 'written over'",
          not over.seed_written_over())
    over._pty_buffer.append("the child's own output\r\n")
    check("project: ...but it is once the child writes",
          over.seed_written_over())
    card2.terminal.screen.reset()
    card2._rerender_restored()
    check("project: the previous run's screen is never projected under a "
          "live child", "PREVIOUS-RUN-SCREEN" not in card2.terminal.screen_text())
    card2.detach(); over.dispose(); pump(30)

    # -- the backstop: TerminalView._apply_resize returns EARLY when rows/cols
    #    are unchanged, so sizeChanged is not guaranteed to arrive
    quiet = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Quiet", cwd=str(tmp), pty=True))
    quiet._pty_buffer.append(big)
    quiet._pty_bytes = quiet._pty_total = len(big)
    card3 = tc.TerminalCard(quiet)
    check("project: a backstop timer is armed for the settled projection",
          card3._settle_timer.isActive())
    pump(tc.REPLAY_SETTLE_MS + 250)
    check("project: ...and it projects even though sizeChanged never fired",
          card3._pending_replay == ""
          and len(card3.terminal.screen.history.top) > 1000,
          len(card3.terminal.screen.history.top))
    card3.detach(); quiet.dispose(); pump(30)

    # -- dropping the restored screen must cancel the backstop too, or it
    #    fires a moment later and puts back what was just dropped
    dropped = TerminalAgent(build_spec(
        AgentKind.POWERSHELL, "Dropped", cwd=str(tmp), pty=True))
    dropped.seed_pty_replay(seed)
    card4 = tc.TerminalCard(dropped)
    card4.drop_restored_screen()
    check("project: dropping the restored screen stops the backstop",
          not card4._settle_timer.isActive())
    pump(tc.REPLAY_SETTLE_MS + 150)
    check("project: ...so the dropped screen stays dropped",
          "PREVIOUS-RUN-SCREEN" not in card4.terminal.screen_text())
    card4.detach(); dropped.dispose(); pump(30)


def test_recovered_prompts_are_cached():
    """_recover_marks must not re-read the transcript on every projection.

    It runs on every projection (card build, and every width change), the read
    is O(whole transcript), and transcripts' own (mtime,size) cache misses for
    exactly the agents that matter -- a LIVE agent rewrites its transcript
    continuously. Measured at 90-165ms on the user's real 50MB transcripts, on
    the GUI thread, per card. Re-reading within one conversation cannot find
    anything the card doesn't already know: it watched those prompts being
    typed, so they carry live marks that outrank a recovered one."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import transcripts
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets import terminal_card as tc

    if not HAS_CONPTY:
        return
    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aihive-recover-"))

    calls = []
    real = transcripts.typed_prompts

    def counting(cwd, sid):
        calls.append(sid)
        return ["count the reads"]

    transcripts.typed_prompts = counting
    try:
        agent = TerminalAgent(build_spec(
            AgentKind.CLAUDE, "Cached", cwd=str(tmp), pty=True))
        agent.spec.session_id = "conv-1"
        agent._pty_buffer.append("count the reads\r\n" + "x\r\n" * 200)
        agent._pty_bytes = agent._pty_total = 1
        card = tc.TerminalCard(agent)
        check("recover: the constructor's seed projection does NOT read the "
              "transcript at all", calls == [], calls)

        card._rerender_restored()
        first = len(calls)
        check("recover: the settled projection reads it once", first == 1, calls)

        for _ in range(5):
            card._recover_marks()
        check("recover: ...and further projections of the same conversation "
              "reuse it", len(calls) == first, calls)

        agent.spec.session_id = "conv-2"
        card._recover_marks()
        check("recover: a NEW conversation re-reads (a /clear or a pin change "
              "means different prompts)", len(calls) == first + 1, calls)
        card.detach(); agent.dispose()
    finally:
        transcripts.typed_prompts = real


def test_gemini_usage_polling_is_offthread_and_optin():
    """The Gemini readout must never block the GUI thread, and must never poll
    unless main.py asks for it.

    `gemini_usage.fetch()` shells out to `agy --print /usage` and MEASURES ~3.0
    seconds. It was being called inline, in MainWindow.__init__ and again on
    every timer tick -- and TWICE per tick, because the retune fetched its own
    copy. That froze the whole app for ~6s a minute (worse at the 10s urgent
    rate) with the CPU idle, since it is blocked on a subprocess rather than
    computing. It also made this suite shell out to the user's real CLI and
    added ~3s to every window it builds, which is what started breaking the
    elapsed-time-sensitive checks elsewhere. Same rule as the Claude readout:
    off-thread, and OPT-IN via start_usage_polling."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import gemini_usage
    from app.session_store import SessionStore
    from main import create_main_window

    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-gemusage-"))

    calls = []
    real = gemini_usage.fetch
    gemini_usage.fetch = lambda *a, **k: (calls.append(1), None)[1]
    try:
        win = create_main_window(SessionStore(path=tmp / "s.json"))
        check("gemini-usage: building a window does NOT shell out to the CLI",
              calls == [], len(calls))
        check("gemini-usage: ...and does not arm the poll timer either",
              not win._gemini_usage_timer.isActive())

        # the retune must use the reading it is given, never fetch its own
        win._retune_gemini_usage_poll(None)
        check("gemini-usage: retuning never fetches", calls == [], len(calls))

        # in-flight guard: a 6s CLI timeout is longer than the urgent interval,
        # so a stacking timer must not launch a thread per tick
        win._gemini_usage_inflight = True
        win._poll_gemini_usage()
        check("gemini-usage: a poll already in flight is not stacked",
              calls == [], len(calls))

        win._gemini_usage_inflight = False
        win._on_gemini_usage_ready(None)
        check("gemini-usage: a finished poll clears the in-flight guard",
              not win._gemini_usage_inflight)
        check("gemini-usage: building a window puts no pill on the bar",
              not win.top_bar.gemini_badge.isVisible()
              and not win.top_bar.usage_badge.isVisible())
        # a failed read must SAY so rather than vanish: the badge used to hide
        # itself, and a blanket try/except hid that it had, so with agy absent
        # the readout simply ceased to exist with no way to ask it to retry
        check("gemini-usage: a failed read shows the can't-read pill",
              (win._on_gemini_usage_ready(
                  gemini_usage.GeminiUsage(error="no-data")),
               win.top_bar.gemini_badge.has_content()
               and not win.top_bar.gemini_badge.has_reading())[-1])
        win.close()

        # With both Gemini pills closed nothing consumes the reading, so the
        # ~3s subprocess is skipped entirely - that is the point of letting a
        # Claude-only user close them. The CLAUDE poll is NOT gated this way:
        # planLimitReached/planLimitCleared/plan_usage() hang off it.
        import app.claude_usage as _cu
        real_claude = _cu.fetch
        _cu.fetch = lambda *a, **k: _cu.Usage(error="no-data")
        try:
            win2 = create_main_window(SessionStore(path=tmp / "off.json"))
            win2._on_usage_tracker_toggled("gemini_five_hour", False)
            win2._on_usage_tracker_toggled("gemini_weekly", False)
            calls.clear()
            win2.start_usage_polling()
            check("gemini-usage: both trackers off means the poll never arms",
                  not win2._gemini_usage_timer.isActive() and calls == [],
                  len(calls))
            check("gemini-usage: ...but the Claude poll keeps running",
                  win2._usage_timer.isActive())
            win2._on_usage_tracker_toggled("gemini_weekly", True)
            check("gemini-usage: re-enabling arms the timer and fetches at once",
                  win2._gemini_usage_timer.isActive() and len(calls) == 1,
                  len(calls))
            check("gemini-usage: a re-enabled pill shows loading, not a gap",
                  win2.top_bar.gemini_weekly_badge.has_content())
            win2.close()

            # ...and a window that never opted in must not shell out even when
            # a tracker is switched on at runtime
            win3 = create_main_window(SessionStore(path=tmp / "noopt.json"))
            win3._on_usage_tracker_toggled("gemini_five_hour", False)
            calls.clear()
            win3._on_usage_tracker_toggled("gemini_five_hour", True)
            check("gemini-usage: a window that never opted in never fetches",
                  calls == [] and not win3._gemini_usage_timer.isActive(),
                  len(calls))
            win3.close()
        finally:
            _cu.fetch = real_claude
    finally:
        gemini_usage.fetch = real


def test_gemini_usage_poll_is_slower_than_claudes():
    """Gemini rides its OWN, much slower poll clock, because each tick spawns a
    process rather than making a request.

    `gemini_usage.fetch()` runs a whole CLI (`agy --print /usage`), which costs
    seconds of a background thread where a Claude tick costs one request. The
    console window that used to flash on ~1 poll in 20 is fixed at source now
    (see test_gemini_usage_reads_via_pseudoconsole), so the process cost is what
    this cadence rations -- and rationing it is nearly free, because only the two
    pills consume this reading (a Gemini cut-off recovers on its own printed
    countdown, never on the account reading).

    This check exists so nobody "tidies" the Gemini timer back onto
    USAGE_POLL_MS, which is answerable to planLimitReached and the reset poll
    it arms -- neither of which exists for Gemini."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import gemini_usage
    from app.session_store import SessionStore
    from app.widgets.main_window import (GEMINI_USAGE_POLL_MS,
                                         GEMINI_USAGE_URGENT_POLL_MS,
                                         USAGE_POLL_MS, USAGE_URGENT_PCT)
    from main import create_main_window

    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-gempoll-"))

    check("gemini-poll: the calm rate is well slower than Claude's",
          GEMINI_USAGE_POLL_MS >= 5 * USAGE_POLL_MS,
          (GEMINI_USAGE_POLL_MS, USAGE_POLL_MS))
    check("gemini-poll: even the urgent rate never goes below Claude's calm one",
          GEMINI_USAGE_URGENT_POLL_MS >= USAGE_POLL_MS,
          GEMINI_USAGE_URGENT_POLL_MS)

    calls = []
    real = gemini_usage.fetch
    gemini_usage.fetch = lambda *a, **k: (calls.append(1), None)[1]
    try:
        win = create_main_window(SessionStore(path=tmp / "s.json"))
        check("gemini-poll: the timer is built on the Gemini interval",
              win._gemini_usage_timer.interval() == GEMINI_USAGE_POLL_MS,
              win._gemini_usage_timer.interval())
        check("gemini-poll: ...and Claude's timer is left alone",
              win._usage_timer.interval() == USAGE_POLL_MS,
              win._usage_timer.interval())

        def reading(pct):
            return gemini_usage.GeminiUsage(limits=(
                gemini_usage.GeminiLimit(key="five_hour", label="5h", short="5h",
                                         percent=pct, resets_at=None),))

        # a calm window stays on the slow clock even with agents working
        win._gemini_agents_working = lambda: True
        win._retune_gemini_usage_poll(reading(10.0))
        check("gemini-poll: a calm window keeps the slow rate",
              win._gemini_usage_timer.interval() == GEMINI_USAGE_POLL_MS,
              win._gemini_usage_timer.interval())

        # the danger zone speeds up, but only to the Gemini urgent rate
        win._retune_gemini_usage_poll(reading(USAGE_URGENT_PCT + 1))
        check("gemini-poll: a nearly spent window uses the Gemini urgent rate",
              win._gemini_usage_timer.interval() == GEMINI_USAGE_URGENT_POLL_MS,
              win._gemini_usage_timer.interval())

        # ...and drops back, rather than latching fast for the rest of the run
        win._retune_gemini_usage_poll(reading(5.0))
        check("gemini-poll: it drops back to the slow rate afterwards",
              win._gemini_usage_timer.interval() == GEMINI_USAGE_POLL_MS,
              win._gemini_usage_timer.interval())

        # no agents working means nothing is moving the number, so no rush
        win._gemini_agents_working = lambda: False
        win._retune_gemini_usage_poll(reading(99.0))
        check("gemini-poll: no working agents means no urgent rate",
              win._gemini_usage_timer.interval() == GEMINI_USAGE_POLL_MS,
              win._gemini_usage_timer.interval())

        check("gemini-poll: retuning still never fetches", calls == [], len(calls))
        win.close()
    finally:
        gemini_usage.fetch = real


def test_gemini_usage_reads_via_pseudoconsole():
    """The usage poll must not spawn a child with a console of its own.

    MEASURED from a console-less pythonw parent (the app's own shape): a plain
    subprocess gives the child its own console every run, and Windows 11 can
    hand a newly created console to the default terminal app -- observed as a
    real, visible Windows Terminal frame on roughly one poll in twenty, which is
    the terminal window the user kept seeing flash over the desktop. No creation
    flag prevents it (CREATE_NO_WINDOW asks for a windowless console and the
    handoff happens anyway; DETACHED_PROCESS measured worse), so the read runs
    under a pseudo-console instead: a ConPTY client never has a console
    allocated for it, so there is nothing to hand off.

    That change has a consequence worth guarding: on a terminal the CLI
    pretty-prints its quota table with padded columns instead of the
    tab-separated one a pipe gets, so a parser that only knew tabs would leave
    every Gemini pill unreadable. Both samples below are real captured output."""
    import shutil
    import subprocess

    from app import gemini_usage

    piped = ("Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-25T10:58:17Z\n"
             "Gemini Models\tFive Hour Limit Remaining\t42%\t2026-08-20T16:52:27Z\n"
             "Claude and GPT models\tWeekly Limit Remaining\t100%\t"
             "2026-08-27T11:52:27Z\n")
    terminal = gemini_usage._ESCAPES.sub("", (
        "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001hQuota:\r\n"
        "Gemini Models          Weekly Limit Remaining     100%  "
        "2026-08-25T10:58:17Z\r\n"
        "Gemini Models          Five Hour Limit Remaining   42%  "
        "2026-08-20T16:52:27Z\r\n"
        "Claude and GPT models  Weekly Limit Remaining     100%  "
        "2026-08-27T11:52:27Z\r\n")).replace("\r\n", "\n")

    real_read, real_which = gemini_usage._read_usage, shutil.which
    shutil.which = lambda name: "agy.exe"
    try:
        for label, text in (("piped", piped), ("terminal", terminal)):
            gemini_usage._read_usage = lambda exe, t, _t=text: _t
            usage = gemini_usage.fetch_cli()
            five = next((l for l in (usage.limits if usage else ())
                         if l.key == "five_hour"), None)
            week = next((l for l in (usage.limits if usage else ())
                         if l.key == "seven_day"), None)
            check("gemini-console: " + label + " output reads 58% of the 5h window used",
                  five is not None and abs(five.percent - 58.0) < 0.01, five)
            check("gemini-console: " + label + " output reads the weekly window too",
                  week is not None and abs(week.percent - 0.0) < 0.01, week)
    finally:
        gemini_usage._read_usage, shutil.which = real_read, real_which

    if os.name != "nt":
        return

    # ...and on Windows the read never goes near a plain subprocess, which is
    # the whole point: that is the shape Windows gives a console of its own
    spawned = []

    def refuse(*a, **k):
        spawned.append(a)
        raise AssertionError("the usage read must not spawn a plain subprocess")

    real_run, real_popen = subprocess.run, subprocess.Popen
    subprocess.run, subprocess.Popen = refuse, refuse
    try:
        gemini_usage._read_usage("definitely-not-a-real-binary.exe", 0.2)
    except Exception:
        pass  # spawning a missing binary fails; only WHAT it tried matters here
    finally:
        subprocess.run, subprocess.Popen = real_run, real_popen
    check("gemini-console: the Windows read never uses a plain subprocess",
          spawned == [], spawned)


def test_history_screen_wrapper_removed():
    """_FastHistoryScreen drops pyte's per-event wrapper without changing what
    is rendered.

    pyte routes every attribute access on a HistoryScreen through a Python
    __getattribute__ that re-wraps each event in before_event/after_event, both
    of which only serve prev_page/next_page. AI Hive never pages, so they are
    no-ops -- but not free ones: taking the scrollback back made pyte's scroll
    path hot (it shuffles every buffer row per scrolled line), and the tax was
    43% of feed time. This asserts the wrapper is gone AND that its removal is
    invisible, which is the only thing that makes the speedup safe."""
    import pyte
    from app.widgets import terminal_view as tv

    fast = tv._new_history_screen(40, 12)
    check("pyte: the view's screen bypasses HistoryScreen.__getattribute__",
          type(fast).__getattribute__ is object.__getattribute__,
          type(fast).__getattribute__)

    # a stream that scrolls well past the screen, hides/shows the cursor
    # (DECTCEM -- what after_event also maintained), wraps, uses SGR, and
    # finally wipes history with ED 3
    parts = ["\x1b[?25l"]
    for i in range(60):
        parts.append(f"\x1b[3{i % 8}mline {i} " + "x" * (i % 50) + "\r\n")
        if i % 7 == 0:
            parts.append("\x1b[?25h" if i % 14 == 0 else "\x1b[?25l")
    parts.append("\x1b[2;5Hmid-screen\x1b[0m")
    data = "".join(parts)

    def drive(screen):
        stream = pyte.Stream(screen)
        for i in range(0, len(data), 64):     # split mid-sequence deliberately
            stream.feed(data[i:i + 64])
        return screen

    stock = drive(pyte.HistoryScreen(40, 12, history=tv.HISTORY_LINES,
                                     ratio=0.25))
    drive(fast)

    def htext(sc):
        return ["".join(l[x].data for x in sorted(l)) for l in sc.history.top]

    check("pyte: unwrapped screen renders an identical live screen",
          list(fast.display) == list(stock.display),
          (list(fast.display)[:2], list(stock.display)[:2]))
    check("pyte: ...and identical scrollback history",
          htext(fast) == htext(stock),
          (len(htext(fast)), len(htext(stock))))
    check("pyte: ...and identical cursor, incl. DECTCEM hidden state",
          (fast.cursor.x, fast.cursor.y, fast.cursor.hidden)
          == (stock.cursor.x, stock.cursor.y, stock.cursor.hidden),
          (fast.cursor.hidden, stock.cursor.hidden))
    check("pyte: history actually filled (otherwise this proves nothing)",
          len(htext(fast)) > 40, len(htext(fast)))
    check("pyte: pushed still counts every scrolled-off line",
          getattr(fast.history.top, "pushed", 0) == len(htext(stock)),
          getattr(fast.history.top, "pushed", None))

    # ED 3 must still reach _reset_history through the unwrapped call
    pyte.Stream(fast).feed("\x1b[3J")
    check("pyte: ED 3 still wipes history without the wrapper",
          len(fast.history.top) == 0, len(fast.history.top))

    # paging is the one thing the wrapper made safe, so it must fail loudly
    for name in ("prev_page", "next_page"):
        try:
            getattr(fast, name)()
            raised = False
        except NotImplementedError:
            raised = True
        check(f"pyte: {name} raises rather than silently paging away",
              raised, raised)


def test_terminal_scrollbar():
    """The terminal scrollbar and its prompt milestones.

    Covers the coordinate identity everything rests on, the capture point (a
    bare Enter and nothing else), the overlay's no-stolen-columns contract, and
    every path that has to wipe a milestone."""
    from PySide6.QtCore import QEvent, QPoint, Qt
    from PySide6.QtGui import QKeyEvent, QMouseEvent, QPixmap
    from PySide6.QtWidgets import QApplication

    import json

    from app import session_hook, ui_theme
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import PROMPT_MARK_CAP, TerminalAgent
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.terminal_scrollbar import WIDTH, TerminalScrollBar
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])

    def enter(view, mods=Qt.KeyboardModifier.NoModifier):
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress,
                                     Qt.Key.Key_Return, mods))

    # ---- viewChanged: fires on real changes, silent on no-ops -----------
    v = TerminalView(rows=10, cols=40)
    hits = []
    v.viewChanged.connect(lambda: hits.append(1))
    for i in range(30):
        v.feed(f"line {i}\r\n")
    v._view_notify_timer.timeout.emit()      # drive the coalescer directly
    check("scrollbar: viewChanged fires as history grows", hits, hits)
    n = len(hits)
    v.scroll_by(-1)                          # already live: clamped no-op
    check("scrollbar: a clamped no-op scroll emits nothing", len(hits) == n)
    v.scroll_by(5)
    check("scrollbar: scrolling back emits", len(hits) > n)
    n = len(hits)
    v.feed("")                               # nothing pushed, nothing changed
    check("scrollbar: an empty feed emits nothing", len(hits) == n)

    # ---- the absolute-line identity -------------------------------------
    def row_text(view, r):
        hist, off = view._view_state()
        return "".join(view._visible_line(r, hist, off)[c].data or " "
                       for c in range(view.screen.columns)).strip()
    check("scrollbar: abs id identifies the line under it, scrolled back",
          row_text(v, 0) == f"line {v.abs_line_at_row(0)}",
          (row_text(v, 0), v.abs_line_at_row(0)))
    target = v.abs_line_at_row(0)
    v.scroll_to_abs(target, lead=0)
    check("scrollbar: scroll_to_abs(lead=0) puts the line on row 0",
          row_text(v, 0) == f"line {target}", row_text(v, 0))
    oldest, newest = v.history_span()
    check("scrollbar: history_span brackets every live id",
          oldest <= v.abs_line_at_row(0) <= newest, (oldest, newest))

    # ---- history wipe: pushed SURVIVES, ids stay consistent -------------
    cleared = []
    v.historyCleared.connect(lambda: cleared.append(1))
    before = v.history_pushed()
    v.feed("\x1b[3J")
    check("scrollbar: ED 3 fires historyCleared", cleared == [1], cleared)
    check("scrollbar: ...and empties history", len(v.screen.history.top) == 0)
    check("scrollbar: ...and snaps back to live", v.scroll_offset() == 0)
    check("scrollbar: ...but `pushed` is deliberately NOT reset",
          v.history_pushed() == before, (v.history_pushed(), before))
    v.feed("after the wipe\r\n" * 12)
    check("scrollbar: ids stay self-consistent across a wipe",
          row_text(v, 0) == "after the wipe"
          or v.abs_line_at_row(0) >= before, v.abs_line_at_row(0))

    # ---- capture point: a bare Enter, and nothing else -------------------
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "Marks", cwd=".",
                                     pty=True))
    card = TerminalCard(agent)
    card.resize(640, 420)
    t = card.terminal
    cols_without_bar = t.screen.columns

    t.feed("> refactor the parser")
    enter(t)
    check("scrollbar: a bare Enter records a milestone",
          [m.text for m in agent.prompt_marks()] == ["refactor the parser"],
          agent.prompt_marks())
    check("scrollbar: ...and the view is given it in view coordinates",
          len(t.marks()) == 1 and t.marks()[0][0] == card._mark_lines[
              agent.prompt_marks()[0].uid], t.marks())

    # the classic renderer parks the caret on a BLANK row below the box, which
    # is what made the span reading come back empty and record nothing
    below = TerminalView(rows=20, cols=40)
    below.feed("output\r\n" * 14 + "─" * 30 + "\r\n> find the leak\x1b[19;3H")
    check("scrollbar: input is still found when the caret sits below the box",
          below._submitted_input() == (15, "find the leak"),
          below._submitted_input())
    below2 = TerminalView(rows=20, cols=40)
    below2.feed("output\r\n" * 14 + "─" * 30 + "\r\n\x1b[19;3H")
    check("scrollbar: ...but an empty box behind that rule records nothing",
          below2._submitted_input() is None, below2._submitted_input())
    below3 = TerminalView(rows=20, cols=40)
    below3.feed("conversation output\r\n\r\n")
    check("scrollbar: ...and a caret up in the conversation records nothing",
          below3._submitted_input() is None, below3._submitted_input())

    for mods, label in ((Qt.KeyboardModifier.ShiftModifier, "Shift"),
                        (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
                        (Qt.KeyboardModifier.AltModifier, "Alt")):
        t.feed("\r\n> a newline, not a submit")
        enter(t, mods)
        check(f"scrollbar: {label}+Enter is a newline, never a milestone",
              len(agent.prompt_marks()) == 1, agent.prompt_marks())

    t.feed("\r\n> ")
    enter(t)
    check("scrollbar: an empty input records nothing",
          len(agent.prompt_marks()) == 1)

    t.feed("\r\n> 1. Yes, proceed")
    enter(t)
    check("scrollbar: a numbered menu row is an ANSWER, not a milestone",
          len(agent.prompt_marks()) == 1, agent.prompt_marks())

    # AI Hive's own writes must never look like the user typing
    agent.write("\r")
    agent.nudge("Continue")
    agent.deliver_task("go and do the thing")
    check("scrollbar: write/nudge/deliver_task record no milestones",
          len(agent.prompt_marks()) == 1, agent.prompt_marks())

    # ---- uid is never an id() ------------------------------------------
    evicted = agent.prompt_marks()[0].uid
    for i in range(PROMPT_MARK_CAP + 5):
        t.feed(f"\r\n> prompt {i}")
        enter(t)
    uids = [m.uid for m in agent.prompt_marks()]
    check("scrollbar: the milestone list is FIFO-capped",
          len(uids) == PROMPT_MARK_CAP, len(uids))
    check("scrollbar: a surviving uid never reuses an evicted one",
          evicted not in uids and len(set(uids)) == len(uids))
    card._refresh_marks()
    check("scrollbar: an evicted milestone is not still painted",
          all(uid in {m.uid for m in agent.prompt_marks()}
              for uid in {m.uid for m in agent.prompt_marks()}))

    # ---- re-anchoring across a card rebuild ----------------------------
    agent2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Replay", cwd=".",
                                      pty=True))
    c1 = TerminalCard(agent2)
    c1.resize(640, 420)
    for k in range(4):
        agent2._on_pty_output("pty", f"> prompt {k}\r\n")
        c1.terminal.feed(f"> prompt {k}")
        enter(c1.terminal)
        for i in range(20):
            agent2._on_pty_output("pty", f"  reply {k} line {i}\r\n")
    c2 = TerminalCard(agent2)          # what a retile does
    c2.resize(640, 420)
    check("scrollbar: a rebuilt card re-derives the SAME milestone lines",
          c1._mark_lines == c2._mark_lines,
          (c1._mark_lines, c2._mark_lines))
    check("scrollbar: every milestone is anchored inside the new history",
          all(c2.terminal.history_span()[0] <= line
              <= c2.terminal.history_span()[1]
              for line in c2._mark_lines.values()))
    agent2._pty_dropped = 10 ** 9      # everything has aged out of the buffer
    check("scrollbar: a milestone whose bytes aged out is dropped, not clamped",
          agent2.replay_marks() == [], agent2.replay_marks())

    # ---- the overlay contract ------------------------------------------
    check("scrollbar: it is a child of the terminal, not a layout sibling",
          card.scroll_bar.parent() is t)
    check("scrollbar: it never takes focus off the terminal",
          card.scroll_bar.focusPolicy() == Qt.FocusPolicy.NoFocus)
    check("scrollbar: it steals no terminal columns",
          t.screen.columns == cols_without_bar,
          (t.screen.columns, cols_without_bar))
    bare = TerminalView(rows=10, cols=40)
    bar = TerminalScrollBar(bare, bare)
    bar.refresh()
    check("scrollbar: hidden while there is no scrollback",
          not bar.isVisibleTo(bare))
    for i in range(40):
        bare.feed(f"line {i}\r\n")
    bar.refresh()
    check("scrollbar: visible once there is", bar.isVisibleTo(bare))
    check("scrollbar: its range is the history depth",
          bar.maximum() == len(bare.screen.history.top), bar.maximum())
    check("scrollbar: maximum means live (offset 0)",
          bar.value() == bar.maximum() - bare.scroll_offset())
    card._place_overlay()
    geo = card.scroll_bar.geometry()
    check("scrollbar: pinned to the right edge, full height",
          geo.right() >= t.width() - 2 and geo.height() == t.height()
          and geo.width() == WIDTH, geo)

    # ---- markers paint, and a click on one jumps -------------------------
    bare.resize(WIDTH, 200)
    bar.resize(WIDTH, 200)
    oldest, newest = bare.history_span()
    marks = [(oldest + 2, "first prompt"), (oldest + 20, "second prompt")]
    bare.set_marks(marks)
    bar.refresh()
    pm = QPixmap(bar.size())
    pm.fill()
    bar.render(pm)
    img = pm.toImage()
    accent = ui_theme.Palette.ACCENT_ORANGE
    want = (int(accent[1:3], 16), int(accent[3:5], 16), int(accent[5:7], 16))

    def band_has_accent(y):
        for dy in range(-3, 4):
            yy = y + dy
            if not (0 <= yy < img.height()):
                continue
            for x in range(img.width()):
                c = img.pixelColor(x, yy)
                if (abs(c.red() - want[0]) < 60 and abs(c.green() - want[1]) < 60
                        and abs(c.blue() - want[2]) < 60):
                    return True
        return False
    check("scrollbar: a milestone is painted at its own position",
          band_has_accent(int(bar._y_for(marks[0][0]))))
    check("scrollbar: ...and so is the second one",
          band_has_accent(int(bar._y_for(marks[1][0]))))

    jumped = []
    bar.markActivated.connect(jumped.append)
    bar.markActivated.connect(bare.scroll_to_abs)
    y = bar._y_for(marks[1][0])
    bar.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, QPoint(int(WIDTH / 2), int(y)),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    check("scrollbar: clicking a milestone activates THAT milestone",
          jumped == [marks[1][0]], jumped)
    check("scrollbar: ...and the view lands on it",
          bare.abs_line_at_row(2) == marks[1][0], bare.abs_line_at_row(2))

    # ---- a fresh milestone is drawable BEFORE it scrolls off ------------
    live = TerminalView(rows=10, cols=40)
    livebar = TerminalScrollBar(live, live)
    livebar.resize(WIDTH, 200)
    for i in range(30):
        live.feed(f"line {i}\r\n")
    livebar.refresh()
    fresh = live.history_pushed() + 3          # still on the live screen
    live.set_marks([(fresh, "just typed")])
    oldest, span = livebar._span()
    check("scrollbar: a milestone on the LIVE screen is inside the track",
          oldest <= fresh <= oldest + span, (oldest, span, fresh))
    pm2 = QPixmap(livebar.size())
    pm2.fill()
    livebar.render(pm2)
    img2 = pm2.toImage()
    found = False
    for dy in range(-4, 5):
        yy = int(livebar._y_for(fresh)) + dy
        if not (0 <= yy < img2.height()):
            continue
        for x in range(img2.width()):
            c = img2.pixelColor(x, yy)
            if (abs(c.red() - want[0]) < 60 and abs(c.green() - want[1]) < 60
                    and abs(c.blue() - want[2]) < 60):
                found = True
    check("scrollbar: ...and is actually painted, not waiting to scroll off",
          found)

    # ---- width change re-projects the scrollback -------------------------
    wide = TerminalAgent(build_spec(AgentKind.CLAUDE, "Reflow", cwd=".",
                                    pty=True))
    wcard = TerminalCard(wide)
    wt = wcard.terminal
    wt.screen.resize(20, 30)                   # a pre-layout narrow card
    wcard._proj_cols = 30
    wide._on_pty_output("pty", ("A very long line of conversation text that "
                                "must wrap at thirty columns\r\n") * 20)
    narrow_lines = len(wt.screen.history.top)
    wt.screen.resize(20, 100)                  # the tiling grid widens it
    wcard._reproject_on_size(20, 100)
    check("scrollbar: widening re-projects the scrollback instead of "
          "leaving it wrapped for a screen that is gone",
          len(wt.screen.history.top) < narrow_lines,
          (narrow_lines, len(wt.screen.history.top)))
    joined = "".join(
        "".join(ln[c].data or " " for c in range(100)).rstrip() + "\n"
        for ln in wt.screen.history.top)
    check("scrollbar: ...and the re-projected lines use the full width",
          any(len(l) > 40 for l in joined.splitlines()),
          max((len(l) for l in joined.splitlines()), default=0))
    before_cols = len(wt.screen.history.top)
    wcard._reproject_on_size(30, 100)          # height-only change
    check("scrollbar: a height-only change re-projects nothing",
          len(wt.screen.history.top) == before_cols)
    wcard.deleteLater()

    # ---- milestones recovered for a conversation we did not watch --------
    from app import transcripts
    rtmp = Path(tempfile.mkdtemp(prefix="ai-hive-marks-"))
    conv = rtmp / "conv.jsonl"
    typed = ["refactor the session pinning logic",
             "now write the regression tests for it",
             "explain why the anchor drifts by one line"]
    recs = []
    for i, text in enumerate(typed):
        recs.append(json.dumps({
            "type": "user", "promptSource": "typed",
            "origin": {"kind": "human"}, "isSidechain": False,
            "timestamp": f"2026-08-09T1{i}:00:00.000Z",
            "message": {"role": "user", "content": text}}))
    # noise that must NOT become a milestone
    recs.append(json.dumps({
        "type": "user", "promptSource": "typed", "isSidechain": True,
        "message": {"role": "user", "content": "a sub-agent turn"}}))
    recs.append(json.dumps({
        "type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "x"}]}}))
    recs.append(json.dumps({
        "type": "user", "promptSource": "typed", "message": {
            "role": "user",
            "content": "<local-command-stdout>Set model to Opus</local-command-stdout>"}}))
    conv.write_text("\n".join(recs) + "\n", encoding="utf-8")

    real_tp = transcripts.transcript_path
    transcripts.transcript_path = lambda cwd, sid: str(conv)
    try:
        got = transcripts.typed_prompts(str(rtmp), "sid")
        check("scrollbar: the transcript yields exactly the typed prompts",
              got == typed, got)

        rag = TerminalAgent(build_spec(AgentKind.CLAUDE, "Recover",
                                       cwd=str(rtmp), pty=True))
        rag.spec.session_id = "sid"
        rcard = TerminalCard(rag)
        rcard.terminal.screen.resize(24, 80)
        # a conversation replayed from disk: the prompts are echoed in the
        # scrollback, but no keystroke was ever seen by this process
        stream = ""
        for text in typed:
            stream += f"> {text}\r\n"
            stream += "".join(f"  reply line {i}\r\n" for i in range(9))
        rag._on_pty_output("pty", stream)
        rcard._recover_marks()
        rcard._refresh_marks()
        found = [t for _line, t in rcard._recovered]
        check("scrollbar: every earlier prompt is recovered from the "
              "scrollback", found == typed, found)
        check("scrollbar: ...and none of them came from a live keystroke",
              rag.prompt_marks() == [])
        rv = rcard.terminal
        hist_r = list(rv.screen.history.top)
        all_rows = hist_r + [rv.screen.buffer[r] for r in range(rv.screen.lines)]
        oldest_r = rv.history_pushed() - len(hist_r)
        ok = True
        for line, text in rcard._recovered:
            idx = line - oldest_r
            if not (0 <= idx < len(all_rows)):
                ok = False
                continue
            row = "".join(all_rows[idx][c].data or " " for c in range(80))
            if text[:20] not in row:
                ok = False
        check("scrollbar: ...and each dot sits on that prompt's own line", ok)
        check("scrollbar: recovered milestones reach the view",
              len(rcard.terminal.marks()) == len(typed),
              rcard.terminal.marks())

        # a prompt the transcript does not contain is never marked
        rag2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "NoMatch",
                                        cwd=str(rtmp), pty=True))
        rag2.spec.session_id = "sid"
        rcard2 = TerminalCard(rag2)
        rcard2.terminal.screen.resize(24, 80)
        rag2._on_pty_output("pty", "> something nobody ever typed\r\n" * 30)
        rcard2._recover_marks()
        check("scrollbar: a line that matches no typed prompt gets no dot",
              rcard2._recovered == [], rcard2._recovered)
        rcard.deleteLater()
        rcard2.deleteLater()
    finally:
        transcripts.transcript_path = real_tp

    # ---- resets ---------------------------------------------------------
    replaced = []
    agent2.conversation_replaced.connect(lambda: replaced.append(1))
    agent2.note_conversation_replaced()
    check("scrollbar: a replaced conversation drops every milestone",
          agent2.prompt_marks() == [] and replaced == [1])
    c2.terminal.clear_history()
    check("scrollbar: ...and its scrollback, so the bar goes away",
          len(c2.terminal.screen.history.top) == 0
          and c2._mark_lines == {} and c2.terminal.marks() == [])
    agent.restart()
    check("scrollbar: a restart clears milestones and stream coordinates",
          agent.prompt_marks() == [] and agent._pty_total == 0
          and agent._pty_dropped == 0)

    # ---- the tui renderer setting --------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tui-"))
    sp, mp, ep = (str(tmp / "s.json"), str(tmp / "m.jsonl"),
                  str(tmp / "e.jsonl"))
    session_hook.write_settings_file(sp, mp, ep, tui="default")
    data = json.loads(Path(sp).read_text(encoding="utf-8"))
    check("scrollbar: tui='default' selects the classic renderer",
          data.get("tui") == "default", data.get("tui"))
    check("scrollbar: the renderer key never disturbs the hook matcher",
          data["hooks"]["SessionStart"][0]["matcher"] == "resume|clear|compact",
          data["hooks"]["SessionStart"][0]["matcher"])
    session_hook.write_settings_file(sp, mp, ep)
    data = json.loads(Path(sp).read_text(encoding="utf-8"))
    check("scrollbar: omitted when AI Hive is not owning the scrollback",
          "tui" not in data, data)
    check("scrollbar: ...and the hooks are still intact",
          data["hooks"]["SessionStart"][0]["matcher"] == "resume|clear|compact")

    card.deleteLater()
    c1.deleteLater()
    c2.deleteLater()


def test_reply_marks_recovered_from_transcript():
    """A reply mark is minted from a busy -> idle settle, so a conversation
    restored from disk comes back with NONE -- and since a resume's replay
    settle is deliberately suppressed, a reopened hive showed no reply time
    anywhere. These are recovered by matching each transcript reply's CLOSING
    line against the scrollback.

    MEASURED, and it is why the anchor is what it is: Claude's own "for Ns"
    footer (what a LIVE mark anchors to) does not survive per turn into the
    scrollback -- across seven real captured screens at five widths, at most
    ONE was still present in 2000 lines. And matching a reply's HEAD found a
    stale narrower re-render first (pyte does not reflow, so a card resized
    mid-session keeps both) and stamped the middle of a paragraph."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import (TerminalCard, _format_reply_stamp,
                                           _last_content_line, _norm_reply_line,
                                           _reply_end_row)

    QApplication.instance() or QApplication([])

    # ---- the normalizer drops what the renderer paints rather than prints --
    check("reply-recover: markdown syntax is dropped from both sides",
          _norm_reply_line("**Rebuilt `dist/x.zip`** from the *good* files")
          == "rebuilt dist/x.zip from the good files",
          _norm_reply_line("**Rebuilt `dist/x.zip`** from the *good* files"))
    check("reply-recover: the closing line is the last one with content on it",
          _last_content_line("first\n\nlast line\n\n  \n") == "last line")

    # ---- the anchor: tail on the row, blank row underneath ----------------
    rows = ["a stale truncated copy of the same rep",   # 0: no tail, no blank
            "more of the stale copy",                   # 1
            "the reply, wrapping over",                 # 2: real copy starts
            "two rows and ending here.",                # 3: the tail lives here
            "",                                         # 4: <- the anchor
            "> next prompt"]                            # 5
    check("reply-recover: anchors the blank row under the reply's last row",
          _reply_end_row(rows, "ending here.", 0) == 4,
          _reply_end_row(rows, "ending here.", 0))
    check("reply-recover: a row with no blank beneath is not a reply ending",
          _reply_end_row(["tail here", "still going"], "tail here", 0) is None)
    check("reply-recover: nothing matching -> no stamp rather than a guess",
          _reply_end_row(rows, "never written", 0) is None)

    # ---- end to end through a real card ----------------------------------
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "ReplyRecover", cwd=".",
                                     pty=True))
    card = TerminalCard(agent)
    card.resize(640, 420)

    when = time.time() - 3600
    # pretend the transcript said this, bypassing the per-conversation cache
    # read (the reader itself is covered by test_transcript_reply_times)
    card._recover_key = (agent.spec.cwd, agent.spec.session_id)
    card._recover_replies = [(when, "All four suites pass now.")]
    agent._on_pty_output("pty", "All four suites pass now.\r\n\r\n> ")
    card._recover_reply_marks()

    check("reply-recover: the past reply is located in the scrollback",
          len(card._recovered_replies) == 1, card._recovered_replies)
    line, stamped = card._recovered_replies[0]
    check("reply-recover: ...stamped with the TRANSCRIPT's time, not now",
          stamped == when, (stamped, when))
    check("reply-recover: ...on the blank row under the reply",
          line == agent_row_after(card, "All four suites pass now."), line)

    card._refresh_reply_marks()
    check("reply-recover: the view carries it as an inline stamp",
          (line, _format_reply_stamp(when)) in card.terminal.reply_marks(),
          card.terminal.reply_marks())

    # ---- the same placement when the footer IS in the scrollback ---------
    from app.widgets.terminal_view import is_reply_footer
    check("reply-recover: Claude's turn footer is recognised",
          is_reply_footer("✻ Worked for 16m 36s")
          and is_reply_footer("✻ Cooked for 8m 2s · 1 shell still running"))
    check("reply-recover: ...and the input box's own hints are not",
          not is_reply_footer("? for shortcuts")
          and not is_reply_footer("← for agents"))
    footered = ["the reply ends here.", "", "✻ Worked for 3m 13s", "", "> "]
    check("reply-recover: a footer moves the anchor down BELOW it",
          _reply_end_row(footered, "reply ends here.", 0) == 3,
          _reply_end_row(footered, "reply ends here.", 0))

    # a reply that is NOT on screen is skipped, never invented
    card._recovered_replies = []
    card._recover_replies = [(when, "a reply that scrolled away long ago")]
    card._recover_reply_marks()
    check("reply-recover: a reply not in the scrollback yields no stamp",
          card._recovered_replies == [], card._recovered_replies)

    # a live mark outranks a recovered one on the same line
    card._recovered_replies = [(line, when)]
    card._reply_mark_lines = {}
    mark = agent.note_reply_settled()
    card._reply_mark_lines[mark.uid] = line
    card._refresh_reply_marks()
    check("reply-recover: a live capture wins the line over a recovered one",
          card.terminal.reply_marks() == [(line, _format_reply_stamp(mark.ts))],
          card.terminal.reply_marks())

    card.deleteLater()
    agent.dispose()


def agent_row_after(card, text):
    """Absolute line of the blank row directly under `text` in a card's
    terminal -- the row test_reply_marks_recovered_from_transcript expects a
    recovered stamp to land on."""
    from app.widgets.terminal_card import _norm_reply_line

    oldest, raw = card._scrollback_rows()
    lines = [_norm_reply_line(t) for t in raw]
    needle = _norm_reply_line(text)
    for i, line in enumerate(lines):
        if needle and needle in line and i + 1 < len(lines) and not lines[i + 1]:
            return oldest + i + 1
    return -1


def test_reply_marks_inline():
    """Reply-finished milestones drawn INLINE in the terminal content -- a dim
    date-and-time stamp on the blank row directly UNDER Claude's own "for Ns"
    footer. This is the only reply-time surface there is: the card header's
    #CardReplyTime badge was removed at the user's request, along with the
    agent-side reply clock that fed it. Covers the anchor scan
    (TerminalView.reply_anchor_line), the shared stamp formatter, a card
    rebuild re-deriving the same anchor, and every reset path that must wipe
    a reply mark alongside a prompt mark."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import REPLY_MARK_CAP, TerminalAgent
    from app.widgets.terminal_card import TerminalCard, _format_reply_stamp
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])

    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "ReplyMarks", cwd=".",
                                     pty=True))
    card = TerminalCard(agent)
    card.resize(640, 420)
    t = card.terminal

    # A settled turn in the shape Claude Code REALLY leaves on screen: the
    # reply, a blank, the "for Ns" footer, a blank, then the input box --
    # whose TOP BORDER (a rule) sits directly above the prompt row. That
    # border is the point of this fixture. An earlier version fed only
    # "footer, blank, > " with no border, so the anchor scan never met the
    # rule it stopped dead on in real use, and the test passed while the
    # live stamp silently never appeared. Do NOT simplify this back.
    # Fed through _on_pty_output (not terminal.feed directly) so it lands in
    # BOTH the live view (via the connected pty_output signal) and the
    # agent's own replay buffer -- real usage does the same, and the
    # rebuilt-card check below needs the replay half.
    agent._on_pty_output("pty", "● Hi!\r\n\r\n✳ Crunched for 58s"
                         "\r\n\r\n" + "─" * 40 + "\r\n> ")
    mark = agent.note_reply_settled()
    check("reply-mark: note_reply_settled records a mark",
          mark is not None and agent.reply_marks() == [mark])
    check("reply-mark: the fixture really has the box border in the way",
          t._row_is_rule(4) and t._input_block_span() == (5, 5),
          (t._input_block_span(),))
    # UNDER the footer, on the blank separator below it -- not beside the
    # footer, not above it wedged between the reply and its own footer
    # (where the user reported finding it), and not skipped because the
    # box border got in the way of the scan
    under_footer = t.abs_line_at_row(3)
    check("reply-mark: the card anchors it UNDER the footer row",
          card._reply_mark_lines.get(mark.uid) == under_footer,
          (card._reply_mark_lines, under_footer))
    check("reply-mark: the view carries exactly one inline stamp",
          t.reply_marks() == [(under_footer, _format_reply_stamp(mark.ts))],
          t.reply_marks())
    import datetime as _dt
    check("reply-mark: the stamp carries the DATE as well as the time",
          _format_reply_stamp(mark.ts) == _dt.datetime.fromtimestamp(
              mark.ts).strftime("%b %d, %H:%M"), _format_reply_stamp(mark.ts))

    # ---- a rule directly above the box (no footer line) anchors nothing --
    ruled = TerminalView(rows=10, cols=40)
    ruled.feed("─" * 20 + "\r\n> ")
    check("reply-mark: a rule row above the box is never mistaken for a footer",
          ruled.reply_anchor_line() is None)

    # ---- no live input box at all (e.g. settled on a menu) -> no anchor --
    menu = TerminalView(rows=10, cols=40)
    menu.feed("some output\r\n")
    check("reply-mark: no input box in view -> nothing to anchor to",
          menu.reply_anchor_line() is None)

    # ---- FIFO cap -----------------------------------------------------
    for _ in range(REPLY_MARK_CAP + 5):
        agent.note_reply_settled()
    check("reply-mark: the mark list is FIFO-capped",
          len(agent.reply_marks()) == REPLY_MARK_CAP, len(agent.reply_marks()))

    # ---- a rebuilt card re-derives the SAME anchor from the pty replay ---
    card2 = TerminalCard(agent)
    card2.resize(640, 420)
    latest = agent.reply_marks()[-1]
    check("reply-mark: a rebuilt card recovers the latest mark's anchor",
          latest.uid in card2._reply_mark_lines, card2._reply_mark_lines)
    card2.deleteLater()

    # ---- resets: restart and history-clear wipe reply marks too ---------
    card.terminal.clear_history()
    check("reply-mark: clearing history wipes the card's reply-mark lines",
          card._reply_mark_lines == {} and card.terminal.reply_marks() == [])
    check("reply-mark: ...and the agent's own mark list",
          agent.reply_marks() == [])

    agent.note_reply_settled()
    agent.restart()
    check("reply-mark: a restart clears reply marks too",
          agent.reply_marks() == [])

    card.deleteLater()


def main():
    test_tiling()
    test_layout_popup_placement()
    test_sidebar_count_badge()
    test_agent_waiting()
    test_notification_chime()
    test_limit_blocked_workspace_stats()
    test_winjob_process_count()
    test_winjob_process_ids_and_kill()
    test_bg_shell_workspace_stats()
    test_bg_shell_settle_relearn()
    test_bg_shell_kill_extras()
    test_chime_persistence()
    test_hook_prompt_events()
    test_agent_hook_waiting()
    test_manager_prompt_events_sync()
    test_input_echo_not_busy()
    test_sidebar_reorder()
    test_manager_reorder_persist()
    test_agent_card_reorder()
    test_sidebar_categories()
    test_manager_categories_persist()
    test_category_container()
    test_agent_inline_expansion()
    test_ai_title_summary()
    test_token_usage_badge()
    test_live_model_effort()
    test_limit_blocked_live_ui()
    test_bg_shell_live_ui()
    test_bg_shell_extra_pids_and_kill_pid()
    test_no_em_dashes_in_visible_text()
    test_reveal_agent()
    test_new_agent_autofocus()
    test_agent_busy_activity()
    test_reply_settle_skips_resume_replay()
    test_transcript_reply_times()
    test_ansi()
    test_terminal_keys()
    test_terminal_image_paste()
    test_terminal_mouse_words_links()
    test_terminal_mouse_tracking_click()
    test_terminal_click_menu_guard()
    test_terminal_selection_edit()
    test_terminal_input_editor()
    test_input_gap_self_heal()
    test_session_migration()
    test_app()
    test_pty()
    test_v2_features()
    test_v2_review_fixes()
    test_v3_features()
    test_persistence_resume()
    test_boot_veil()
    test_resume_fallback()
    test_scrollback()
    test_themes()
    test_review_fixes()
    test_wake_and_resume_all()
    test_session_pinning()
    test_session_recovery()
    test_session_hook_tracking()
    test_session_hook_arming()
    test_review_hardening_fixes()
    test_resume_picker()
    test_transcript_backups()
    test_pty_width_at_launch()
    test_screen_snapshots()
    test_agent_file_map()
    test_fsopen_helpers()
    test_filetypes_icons()
    test_terminal_relative_link()
    test_terminal_block_glyphs()
    test_terminal_link_underline()
    test_sidebar_file_tree()
    test_sidebar_search()
    test_plan_usage()
    test_usage_trackers_preference()
    test_taskbar_badge()
    test_bg_shell_taskbar_state()
    test_limit_ledger()
    test_agent_kind_is_always_an_enum()
    test_limit_recovery_reliability()
    test_auto_continue_on_limit_reset()
    test_gemini_limit_detection()
    test_startup_limit_recovery()
    test_terminal_scrollbar()
    test_reply_marks_inline()
    test_reply_marks_recovered_from_transcript()
    test_history_screen_wrapper_removed()
    test_gemini_usage_polling_is_offthread_and_optin()
    test_gemini_usage_poll_is_slower_than_claudes()
    test_gemini_usage_reads_via_pseudoconsole()
    test_projection_happens_once()
    test_recovered_prompts_are_cached()
    test_multi_agent_session_isolation()
    test_usage_pill_geometry_and_close()
    test_usage_pill_never_truncates()
    test_options_panel()
    test_topbar_extras_autosize()
    test_topbar_extras_grow_with_window()
    test_scheduled_send()
    test_cli_auto_update()
    test_cli_native_migration()
    test_lifecycle_e2e()  # slowest last: launches a real claude once
    print(f"\nRESULT: {PASS} passed, {FAIL} failed", flush=True)
    return 1 if FAIL else 0


def test_usage_pill_geometry_and_close():
    """Every usage pill is sized by ONE formula, and carries a hover X.

    The Gemini pills used to be pinned to a hardcoded 315px and elide into it,
    which reserved 630px of the bar for two readouts whose real content is
    ~215px, truncated anything longer, and disagreed with the Claude pill
    sitting immediately beside them. Width is now the text's width, measured
    identically for every pill, which is what these checks pin down.

    The X is a child button rather than a rect hit-tested in mousePressEvent,
    so closing can never be mistaken for the click-to-refresh affordance - and
    its width is reserved even while it is hidden, because growing the pill on
    hover would shove the whole right-hand cluster of the bar sideways.
    """
    import os
    import tempfile
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor, QFont, QFontMetrics
    from app.widgets.gemini_usage_badge import GeminiUsageBadge
    from app.widgets.ornaments import PlanUsageBadge, UsagePillBadge
    from app import gemini_usage

    QApplication.instance() or QApplication([])
    badge = GeminiUsageBadge(window="five_hour")
    weekly_badge = GeminiUsageBadge(window="weekly")
    plan_badge = PlanUsageBadge(window="five_hour")

    check("usage-pill: no content before a reading arrives",
          badge.has_content() is False)
    check("usage-pill: the loading state IS content, so the bar is never blank",
          (badge.mark_loading(), badge.has_content() is True)[-1])
    check("usage-pill: the loading line names its own tracker",
          "5h" in badge._text and "reading" in badge._text
          and "7d" in (weekly_badge.mark_loading(), weekly_badge._text)[-1])
    check("usage-pill: the loading pill paints with no number to draw",
          not badge.grab().isNull())

    lim_five_hour = gemini_usage.GeminiLimit(
        key="five_hour", label="Five Hour Limit (5h)", short="5h",
        percent=13.0, resets_at=time.time() + 3600.0)
    lim_weekly = gemini_usage.GeminiLimit(
        key="seven_day", label="Weekly Limit (all models)", short="7d",
        percent=2.5, resets_at=time.time() + 86400.0)

    reading = gemini_usage.GeminiUsage(limits=(lim_five_hour, lim_weekly))
    badge.set_usage(reading)
    weekly_badge.set_usage(reading)

    check("usage-pill: 5h badge shows the 5h limit text",
          "5h Gemini 13%" in badge._text)
    check("usage-pill: weekly badge shows the 7d limit text",
          "7d Gemini 2%" in weekly_badge._text)
    check("usage-pill: has_content is True once a reading is in",
          badge.has_content() is True)

    # the formula, and that BOTH classes use the same one. Bound to the
    # instance (the paint device `paintEvent` itself uses), same reason
    # `_measure_width` is: an unbound QFontMetrics(font) resolves against the
    # primary screen and can disagree with what actually gets painted.
    def want(instance, text):
        fm = QFontMetrics(instance._text_font(), instance)
        return instance._chrome_width() + fm.horizontalAdvance(text)

    check("usage-pill: width is ring + pads + text, and nothing else",
          badge.width() == want(badge, badge._text))
    check("usage-pill: the X reserves NO width - it floats over the text",
          badge.width()
          == badge._PAD * 2 + badge._RING + badge._GAP + badge._TEXT_SLACK
          + QFontMetrics(badge._text_font(), badge).horizontalAdvance(
              badge._text))
    # ONE formula, not a per-subclass copy. The two pills are given the same
    # font first, deliberately: a measurement follows the pill's OWN font now
    # (the fix for pills that measured one face and painted another), so two
    # widgets in different polish states measuring differently is correct
    # behaviour and would make this a test of nothing.
    same_font = QFont(plan_badge.font())
    plan_badge.setFont(same_font)
    badge.setFont(same_font)
    check("usage-pill: Claude and Gemini measure an identical string alike",
          plan_badge._measure_width("21% used, resets in 1h20m at 14:49")
          == badge._measure_width("21% used, resets in 1h20m at 14:49")
          and GeminiUsageBadge._measure_width is UsagePillBadge._measure_width
          and PlanUsageBadge._measure_width is UsagePillBadge._measure_width)
    check("usage-pill: the fixed 315px width is gone",
          not hasattr(GeminiUsageBadge, "_FIXED_WIDTH")
          and badge.width() != weekly_badge.width())
    check("usage-pill: the full text fits, so nothing is ever truncated",
          badge.width() - (badge._PAD + badge._RING + badge._GAP) - badge._PAD
          >= QFontMetrics(badge._text_font(), badge).horizontalAdvance(
              badge._text))
    check("usage-pill: the X sits inside the text's own run",
          badge.close_btn.x() < badge.width() - badge._PAD
          and badge.close_btn.x() + badge._CLOSE_W
          <= badge.width() - badge._PAD + 1)

    # the hover X (isVisibleTo, not isVisible: these badges are never shown,
    # so a real isVisible() would be False either way and prove nothing)
    check("usage-pill: the X is hidden until the pill is hovered",
          not badge.close_btn.isVisibleTo(badge))
    w_before = badge.width()
    badge.enterEvent(None)
    check("usage-pill: hovering reveals the X",
          badge.close_btn.isVisibleTo(badge))
    check("usage-pill: hovering does NOT change the width",
          badge.width() == w_before)
    check("usage-pill: the hovered pill paints its fade under the X",
          badge._hovering and not badge.grab().isNull())
    badge.leaveEvent(None)
    check("usage-pill: leaving hides the X again",
          not badge.close_btn.isVisibleTo(badge))

    closed, refreshed = [], []
    badge.closeRequested.connect(lambda: closed.append(1))
    badge.refreshRequested.connect(lambda: refreshed.append(1))
    badge.close_btn.click()
    check("usage-pill: the X closes, and does NOT trigger a refresh",
          closed == [1] and refreshed == [])

    col = badge._color()
    check("usage-pill: _color returns a QColor for a live reading",
          isinstance(col, QColor))

    # a can't-read pill is content too, on the Gemini side as well as Claude's
    blank = GeminiUsageBadge(window="five_hour")
    blank.mark_unreadable("no-data")
    check("usage-pill: an unreadable Gemini pill says so and offers a refresh",
          blank.has_content() and not blank.has_reading()
          and "click to refresh" in blank._text
          and not blank.grab().isNull())
    blank.deleteLater()

    check("usage-pill: the shared base owns the geometry",
          issubclass(GeminiUsageBadge, UsagePillBadge)
          and issubclass(PlanUsageBadge, UsagePillBadge))

    # THE DISK CACHE IS GONE. A stored number goes stale exactly where it
    # matters most (a 5-hour window is routinely spent and reopened between
    # launches), and nothing on the bar tells a restored figure from a live one.
    check("gemini-usage: the disk cache is removed entirely",
          not hasattr(gemini_usage, "read_cached")
          and not hasattr(gemini_usage, "write_cached")
          and not hasattr(gemini_usage, "cache_path"))

    real_cli = gemini_usage.fetch_cli
    with tempfile.TemporaryDirectory() as tmpdir:
        old_env = os.environ.get("GEMINI_CONFIG_DIR")
        try:
            os.environ["GEMINI_CONFIG_DIR"] = tmpdir
            gemini_usage.fetch_cli = lambda *a, **k: None
            failed = gemini_usage.fetch()
            check("gemini-usage: a failed CLI read reports no-data, never raises",
                  failed.error == "no-data" and failed.limits == ()
                  and not failed.ok)
            check("gemini-usage: a fetch writes nothing to disk",
                  list(Path(tmpdir).iterdir()) == [])
        finally:
            gemini_usage.fetch_cli = real_cli
            if old_env is None:
                os.environ.pop("GEMINI_CONFIG_DIR", None)
            else:
                os.environ["GEMINI_CONFIG_DIR"] = old_env

    badge.deleteLater()
    weekly_badge.deleteLater()


def test_usage_pill_never_truncates():
    """A pill is as wide as the text it PAINTS, whatever the font turns out
    to be, and it repairs itself if it ever is not.

    Reported live, twice, with screenshots: pills reading "5h 86% used,
    resets n..." and "5h 0% us..." in a bar with hundreds of free pixels. The
    mechanism was a font the measurement never saw. `_text_font` used to
    return a bare `QFont()`, whose family is UNSET, and the two consumers of
    that font resolve an unset family differently: `QFontMetrics` falls back
    to the application font, while `QPainter.setFont` resolves it against the
    widget's own (whatever QSS put there). They agree only while the chrome
    family IS the application default - and `setup_application` deliberately
    prefers "Inter" whenever it is installed, so on such a machine every pill
    measured one face and painted a wider one, and `paintEvent`'s elide (a
    safety net, never meant to fire) truncated the reading.

    Two independent guarantees are checked here: the measurement now follows
    the widget's font, and a pill that finds itself too narrow at paint time
    widens itself using THE PAINTER'S OWN metrics - which is what makes the
    repair work even when the measurement is the thing that is wrong.
    """
    from PySide6.QtCore import QTimer, QEventLoop
    from PySide6.QtGui import QFont, QFontMetrics, QPainter
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    from app import claude_usage
    from app.widgets.ornaments import PlanUsageBadge

    app = QApplication.instance() or QApplication([])

    def spin(ms=60):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def painted_advance(pill):
        """What the pill's own painting metrics say the line needs."""
        return QFontMetrics(pill._text_font(), pill).horizontalAdvance(
            pill._text)

    def text_room(pill):
        return pill.width() - (pill._PAD + pill._RING + pill._GAP) - pill._PAD

    host = QWidget()
    lay = QVBoxLayout(host)
    pill = PlanUsageBadge(host, window="five_hour")
    lay.addWidget(pill)
    host.resize(900, 60)
    host.show()
    pill.set_usage(claude_usage.Usage(
        limits=(claude_usage.Limit(key="five_hour", label="5h", short="5h",
                                   percent=86.0,
                                   resets_at=time.time() + 4800),),
        fetched_at=time.time(), plan="max"))
    spin()

    check("usage-pill: the text font follows the widget's own family",
          pill._text_font().family() == pill.font().family()
          and pill._text_font().pixelSize() == pill._TEXT_PX)
    check("usage-pill: a fresh reading fits with room to spare",
          text_room(pill) >= painted_advance(pill))

    # a font swapped under a line that has NOT changed: `_set_text` would
    # early-return, so only `changeEvent` can keep the width honest
    before = pill.width()
    wide = QFont("Courier New")
    wide.setPixelSize(13)
    pill.setFont(wide)
    spin()
    widened = pill.width()
    check("usage-pill: a font change re-measures the pill",
          widened > before and text_room(pill) >= painted_advance(pill))

    # ...and the same the other way, so a narrower face gives the bar its
    # pixels back rather than leaving a padded pill behind. Measured against
    # the WIDE width, not the original: what a bare QFont() resolves to
    # depends on the widget's polish state, which the suite's earlier tests
    # can have moved - the invariant here is that the pill follows its font
    # in both directions, not what the default face happens to be.
    narrow = QFont("Segoe UI")
    narrow.setPixelSize(11)
    pill.setFont(narrow)
    spin()
    check("usage-pill: a narrower font shrinks the pill back",
          pill.width() < widened
          and text_room(pill) >= painted_advance(pill))

    # THE MEASUREMENT ITSELF IS WRONG: the exact shape of the reported bug.
    # Re-running it would repeat the error, so the repair has to come from
    # the painter's metrics.
    class Undersizing(PlanUsageBadge):
        def _measure_width(self, text):
            return int(super()._measure_width(text) * 0.62)

    broken = Undersizing(host, window="five_hour")
    lay.addWidget(broken)
    broken.set_usage(claude_usage.Usage(
        limits=(claude_usage.Limit(key="five_hour", label="5h", short="5h",
                                   percent=86.0,
                                   resets_at=time.time() + 4800),),
        fetched_at=time.time(), plan="max"))
    check("usage-pill: a broken measurement starts out too narrow",
          text_room(broken) < painted_advance(broken))
    broken.grab()      # one paint is all the repair needs
    spin()
    healed = broken.width()
    check("usage-pill: painting too narrow widens the pill instead of eliding",
          text_room(broken) >= painted_advance(broken))
    broken.grab()
    spin()
    check("usage-pill: the repair settles - it never oscillates",
          broken.width() == healed)

    # and the repair is bounded: a pill that cannot be helped asks once
    calls = []
    real = broken._reassert_width
    broken._reassert_width = lambda: calls.append(1) or real()
    for _ in range(3):
        broken.grab()
        spin()
    check("usage-pill: a settled pill asks for no further repair",
          calls == [])

    broken.deleteLater()
    pill.deleteLater()
    host.deleteLater()


def test_topbar_extras_autosize():
    """The top bar's non-essential controls live in a QScrollArea (so the
    window's minimum width is a small constant, not the sum of everything the
    bar could show - see the responsive-layout fix). Built with
    `setWidgetResizable(False)`, which means Qt does NOT automatically resize
    the content widget when ITS OWN layout's sizeHint changes later.

    Reported live: a usage pill starts hidden and only gains its real (much
    wider) size once a reading arrives. Without `_AutoSizingScrollContent`,
    `_extras` stayed frozen at the narrower size it had when it was last laid
    out, and its own QHBoxLayout crammed the newly-widened pill into that
    stale rect - two pills drawing on top of each other, garbled and
    unreadable. `_AutoSizingScrollContent` catches the `QEvent.LayoutRequest`
    Qt already sends `_extras` whenever its layout invalidates and resizes it
    to match, so this covers ANY future cause of a size change, not just
    usage pills.
    """
    import time as _time
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import TopBar
    from app import claude_usage as cu

    QApplication.instance() or QApplication([])
    bar = TopBar()
    bar.show()
    bar.resize(1920, 42)
    QApplication.instance().processEvents()

    def limit(key, pct, resets):
        return cu.Limit(key=key, label=cu._LABELS[key], short=cu._SHORT[key],
                        percent=pct, resets_at=resets)

    now = _time.time()
    good = cu.Usage(limits=(limit("five_hour", 21.0, now + 4800),
                            limit("seven_day", 64.0, now + 400000)),
                    fetched_at=now, plan="pro")
    bar.set_usage(good)
    QApplication.instance().processEvents()

    check("topbar-autosize: both Claude pills became visible",
          bar.usage_badge.isVisible() and bar.usage_weekly_badge.isVisible())
    check("topbar-autosize: _extras grew to fit its now-wider content",
          bar._extras.width() >= bar._extras.layout().sizeHint().width())
    check("topbar-autosize: the two pills do not overlap",
          not bar.usage_badge.geometry().intersects(
              bar.usage_weekly_badge.geometry()),
          (bar.usage_badge.geometry(), bar.usage_weekly_badge.geometry()))

    # The MINIMUM is what makes a squeeze impossible rather than unlikely: a
    # QHBoxLayout with less room than its children's minimums shrinks them
    # PAST those minimums, and any moment where this widget is narrower than
    # its row would truncate a pill that has the pixels to spare. `resize()`
    # is clamped to `minimumWidth`, so nothing can put it in that state.
    check("topbar-autosize: the row's minimum is its real content width",
          bar._extras.minimumWidth()
          == bar._extras.layout().sizeHint().width())
    bar._extras.resize(120, bar._extras.height())
    check("topbar-autosize: the row refuses to be squeezed below its content",
          bar._extras.width() >= bar._extras.layout().sizeHint().width())
    for pill in (bar.usage_badge, bar.usage_weekly_badge):
        room = (pill.width() - (pill._PAD + pill._RING + pill._GAP)
                - pill._PAD)
        check("topbar-autosize: a squeezed row still fits each pill's text",
              room >= QFontMetrics(pill._text_font(),
                                   pill).horizontalAdvance(pill._text))
    bar.deleteLater()


def test_options_panel():
    """Every top-bar SETTING lives in one anchored Options popup.

    The bar is a 42px strip and it lost the argument with its own contents.
    Five successive attempts rearranged the same fourteen widgets inside it and
    it was still too small; the scrolling row's answer to running out of room
    is to slide a control out of view with no affordance saying it did. So the
    settings moved out, and only the glanceable readouts and the two primary
    actions keep permanent space.

    What is checked here is the whole contract of that move: the bar keeps
    exactly the right widgets, the panel owns the rest, every switch still
    emits its own signal and every `set_*` reflector still reflects WITHOUT
    emitting (the restore path depends on that - eight saves would fire at
    launch otherwise), and the placement is clamped so the panel cannot open
    off the edge of the window or the screen.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QToolButton
    from app import ui_theme
    from app.widgets.main_window import TopBar
    from app.widgets.ornaments import anchored_popup_pos

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(ui_theme.build_qss())
    bar = TopBar()
    bar.resize(1400, 42)
    bar.show()
    app.processEvents()
    panel = bar.options_panel

    # --- 1. what stayed on the bar, and what left ------------------------
    row = bar._extras.layout()
    on_row = [row.itemAt(i).widget() for i in range(row.count())]
    check("options: the bar's scrolling row is now the four pills and the +",
          on_row == [bar.usage_badge, bar.usage_weekly_badge, bar.gemini_badge,
                     bar.gemini_weekly_badge, bar.usage_add_btn],
          [w.objectName() or type(w).__name__ for w in on_row])
    moved = [bar.recover_btn, bar.resume_btn, bar.sound_btn, bar.taskbar_btn,
             bar.auto_update_btn, bar.updates_manage_btn, bar.install_label,
             bar.update_pill, bar.theme_select, bar.font_dec_btn,
             bar.font_inc_btn]
    check("options: every setting is parented to the panel, not the bar",
          all(w.parent() is panel for w in moved),
          [type(w).__name__ for w in moved if w.parent() is not panel])
    check("options: the two essential actions stay pinned on the bar",
          bar.toggle_btn.parent() is bar
          and bar.add_terminal_btn.parent() is bar
          and bar.options_btn.parent() is bar)

    # --- 2. the panel is a real popup, built once ------------------------
    # NOT a QMenu: a QWidgetAction DELETES its reparented widget on release,
    # which crashed the earlier overflow design, and a widget parked in an
    # unopened menu genuinely is not isVisible(), which broke every visibility
    # assertion in this suite. A plain Qt.Popup has neither problem.
    check("options: the panel is a Qt.Popup window",
          bool(panel.windowFlags() & Qt.WindowType.Popup))
    check("options: it is not delete-on-close (its children carry state)",
          not panel.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose))
    bar.options_btn.click()
    app.processEvents()
    check("options: clicking the button opens it", panel.isVisible())
    same = bar.options_panel
    panel.hide()
    app.processEvents()
    bar.options_btn.click()
    app.processEvents()
    check("options: reopening reuses the SAME panel, never a rebuild",
          bar.options_panel is same and panel.isVisible())
    panel.hide()

    # --- 3. every switch: click emits, set_* reflects and stays silent ----
    # Each row is a real track-and-thumb ToggleSwitch (green/slid-right when
    # armed, grey/slid-left when off) plus a separate plain-words QLabel -
    # this replaced a whole-row button whose own text carried an LED glyph,
    # which read as a clickable link rather than a switch.
    cases = [
        ("startup recovery", bar.recover_btn, bar.recover_label,
         bar.startupRecoveryToggled, bar.set_startup_recovery,
         bar.startup_recovery, True),
        ("auto continue", bar.resume_btn, bar.resume_label,
         bar.autoContinueToggled, bar.set_auto_continue,
         bar.auto_continue, True),
        ("chime", bar.sound_btn, bar.sound_label,
         bar.soundToggled, bar.set_sound_enabled,
         lambda: bar._sound_on, True),
        ("taskbar count", bar.taskbar_btn, bar.taskbar_label,
         bar.taskbarBadgeToggled, bar.set_taskbar_badge,
         lambda: bar._taskbar_badge, True),
        ("cli updates", bar.auto_update_btn, bar.auto_update_label,
         bar.autoUpdateToggled, bar.set_auto_update,
         bar.auto_update, False),
    ]
    for name, btn, _label, signal, setter, getter, default in cases:
        seen = []
        signal.connect(seen.append)
        check(f"options: {name} starts at its documented default",
              getter() is default and btn.isChecked() is default,
              (getter(), btn.isChecked()))
        btn.click()
        check(f"options: clicking {name} flips it and emits the new value",
              seen == [not default] and getter() is (not default)
              and btn.isChecked() is (not default), (name, seen, getter()))
        setter(default)
        check(f"options: set_* for {name} reflects without re-emitting",
              seen == [not default] and getter() is default
              and btn.isChecked() is default, (name, seen))
        signal.disconnect(seen.append)

    # every row says what it does in words, not in a glyph the tooltip
    # explains - that was the whole reason for leaving the 42px strip
    labels = ["Recover at start-up", "Resume on usage reset",
              "Notification chime", "Taskbar count",
              "Check for CLI updates at start-up"]
    check("options: every switch is labelled in plain words",
          all(lab in label.text() for lab, (_n, _b, label, *_r)
              in zip(labels, cases)),
          [label.text() for _n, _b, label, *_r in cases])

    # --- 4. the appearance rows still drive the same signals -------------
    themed = []
    bar.themeChanged.connect(themed.append)
    ids = [bar.theme_select.itemData(i)
           for i in range(bar.theme_select.count())]
    other = [i for i in ids if i != bar.theme_select.currentData()][0]
    bar.theme_select.setCurrentIndex(bar.theme_select.findData(other))
    check("options: the theme row still emits themeChanged", themed == [other],
          themed)
    bar.set_theme(ids[0])
    check("options: set_theme reflects without re-emitting",
          themed == [other] and bar.theme_select.currentData() == ids[0])
    deltas = []
    bar.globalFontDelta.connect(deltas.append)
    bar.font_dec_btn.click()
    bar.font_inc_btn.click()
    check("options: the font steppers still emit -1 / +1", deltas == [-1, 1],
          deltas)

    # --- 5. no Claude login hides the two recovery rows, nothing else -----
    bar.set_recovery_available(False)
    check("options: no-Claude hides the recovery rows inside the panel",
          not bar.recover_btn.isVisibleTo(panel)
          and not bar.resume_btn.isVisibleTo(panel)
          and bar.sound_btn.isVisibleTo(panel)
          and bar.usage_add_btn.isVisible())
    bar.set_recovery_available(True)

    # --- 6. the placement is clamped, same helper the layout palette uses -
    # The Options button sits at the RIGHT end of a possibly-ultrawide bar, so
    # left-anchoring a panel under it spills off the window on a narrow window
    # and off the display on a wide one.
    panel.adjustSize()
    pos = anchored_popup_pos(bar.options_btn, panel.size())
    screen = bar.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    win = bar.window().geometry()
    check("options: the panel opens inside the app window",
          pos.x() >= min(win.x(), avail.x())
          and pos.x() + panel.width()
          <= max(win.x() + win.width(), avail.x() + avail.width()),
          (pos.x(), panel.width(), win))
    check("options: ...and on the screen",
          pos.y() >= avail.y()
          and pos.y() + panel.height() <= avail.y() + avail.height(),
          (pos.y(), panel.height(), avail))

    # --- 7. the bar can now shrink far further than it could -------------
    check("options: the bar's minimum width is a small constant",
          bar.minimumSizeHint().width() < 1000,
          bar.minimumSizeHint().width())

    # --- 8. one control per setting: no duplicate lives on the bar -------
    bar_buttons = [w for w in bar.findChildren(QToolButton)
                   if w.parent() is bar]
    check("options: the bar itself carries exactly three buttons",
          set(bar_buttons) == {bar.toggle_btn, bar.add_terminal_btn,
                               bar.options_btn},
          [w.objectName() for w in bar_buttons])
    bar.deleteLater()


def test_topbar_extras_grow_with_window():
    """The extras row must take every pixel a wider window can give it.

    `QAbstractScrollArea.sizeHint()` is a small constant unrelated to what is
    inside it, so with the breadcrumb holding the layout's stretch the row
    stayed frozen at that constant no matter how wide the window got -
    identically on a 1280px laptop and a 3440px ultrawide, with most of the
    bar's controls parked off-screen behind a permanent scrollbar (reported
    live on a 1440p monitor). `_HWheelScrollArea.sizeHint()` reports the
    content's real width instead, so the QHBoxLayout satisfies it before
    handing the leftover to the breadcrumb.

    Two properties are checked together because either one alone is
    satisfiable by the wrong fix: the row GROWS with the window (a fixed-width
    row fails), and the WINDOW's minimum stays a small constant (a row that
    simply demands its full width fails - that is the >2000px minimum the
    scroll area was introduced to remove).
    """
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
    app = QApplication.instance() or QApplication([])
    from app.widgets.main_window import TopBar

    host = QWidget()
    v = QVBoxLayout(host)
    v.setContentsMargins(0, 0, 0, 0)
    bar = TopBar(host)
    v.addWidget(bar)
    host.show()

    # The row starts EMPTY - every usage pill is hidden until it has something
    # to say - and an empty row is narrower than the scroll area's own floor,
    # so nothing about growth is observable on it. Put all four pills into
    # their loading state first, which is the state the bar is genuinely in a
    # second after launch.
    bar.mark_usage_loading()
    app.processEvents()

    # Sized RELATIVE to the row's own natural width rather than at fixed pixel
    # widths: font metrics differ between the offscreen platform and a real
    # screen, and a fixed 1280px is already roomy enough here that the row
    # would sit at its full width for every sample and "growth" could not be
    # observed at all.
    # ...and OFFSET by the bar's own minimum, because the pinned chrome (the
    # sidebar toggle, the identity block, Options, Add Terminal) is served
    # before the row is: sampling below that floor measures three windows that
    # all leave the row at its minimum, which says nothing about growth.
    natural = bar._extras.sizeHint().width()
    base = bar.minimumSizeHint().width()
    narrow, mid, wide = (base + natural // 3, base + (natural * 2) // 3,
                         base + natural * 2)

    widths = {}
    for w in (narrow, mid, wide):
        host.resize(w, 120)
        app.processEvents()
        widths[w] = bar._extras_scroll.width()

    check("topbar-grow: the extras row widens as the window does",
          widths[narrow] < widths[mid] < widths[wide], widths)
    check("topbar-grow: a wide window fits the row's whole natural width",
          widths[wide] >= natural, (widths[wide], natural))
    check("topbar-grow: ...and then no scrollbar is needed",
          not bar._extras_scroll.horizontalScrollBar().isVisible())

    # the scroll area's own minimum is what decides how narrow the WINDOW may
    # be; it must stay a small constant rather than tracking the content
    check("topbar-grow: the row's minimum stays a small constant",
          bar._extras_scroll.minimumSizeHint().width() <= 120,
          bar._extras_scroll.minimumSizeHint().width())
    check("topbar-grow: so the whole bar still fits a laptop width",
          bar.minimumSizeHint().width() < 1000,
          bar.minimumSizeHint().width())
    host.deleteLater()


class _FakeCli:
    """A recording `Runner` for the CLI-update gate. Every check below drives
    the real `run_gate` through one of these; NO test ever shells out, which is
    the same rule that keeps the suite off the network and off the user's real
    Claude account."""

    def __init__(self, versions=("2.1.224",), available="2.1.224", alive=0,
                 upgrade=(0, ""), update_out=(0, "already on the latest"),
                 timeout_on=(), paths=None, paths_rc=0):
        self.calls = []                 # every argv, in order
        self.versions = list(versions)  # one per `--version`, last repeats
        self.available = available
        self.alive = alive
        self.upgrade = upgrade
        self.update_out = update_out
        self.timeout_on = tuple(timeout_on)
        # what the path pass reports. None = every `alive` process IS the
        # target (the tests' exes are `C:\fake\<image name>`), which is the
        # shape every check written before the desktop-app collision assumed.
        self.paths = paths
        self.paths_rc = paths_rc

    def argvs(self):
        return [list(a) for a, _t in self.calls]

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append((argv, timeout))
        joined = " ".join(argv).lower()
        if any(token in joined for token in self.timeout_on):
            from app import cli_update
            return cli_update.RC_TIMEOUT, ""
        if argv[0] == "tasklist":
            name = argv[2].split()[-1]
            if self.alive < 0:
                return 1, "ERROR"
            body = "\n".join(f"{name}   {1000 + i} Console  1  285,000 K"
                             for i in range(self.alive))
            return 0, body or ("INFO: No tasks are running which match the "
                               "specified criteria.")
        if argv[0] == "powershell":
            if self.paths_rc != 0:
                return self.paths_rc, "ERROR"
            name = joined.split("name='")[1].split("'")[0]
            lines = self.paths if self.paths is not None else \
                [rf"C:\fake\{name}"] * self.alive
            return 0, "\n".join(lines)
        if argv[-1] == "--version":
            out = self.versions[0]
            if len(self.versions) > 1:
                out = self.versions.pop(0)
            return 0, out
        if argv[0] == "winget" and argv[1] == "show":
            return 0, f"Found Claude Code [Anthropic.ClaudeCode]\n" \
                      f"Version: {self.available}\n"
        if argv[0] == "winget" and argv[1] == "upgrade":
            return self.upgrade
        return self.update_out          # `agy update`


def test_cli_auto_update():
    """The startup CLI auto-update gate.

    Root cause it exists for: `claude.exe` is a single self-contained binary
    and Windows cannot overwrite a running one, while every AI Hive agent IS a
    claude.exe child. An upgrade run with agents up cannot replace the file,
    but winget records the new version in its database anyway, after which the
    old binary nags forever and winget insists there is nothing to do. So the
    gate runs BEFORE any window or agent exists, it decides success by reading
    `--version` off the resolved binary rather than by believing the installer,
    and it refuses to run an upgrade at all while a target process is alive.

    Everything here is driven through an injected runner: the suite must never
    upgrade the user's CLI."""
    from PySide6.QtWidgets import QApplication
    from app import cli_update
    from app.cli_update import Status
    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from app.workspace_manager import SESSION_VERSION
    from main import create_main_window

    app = QApplication.instance() or QApplication([])

    claude = cli_update.Target(
        key="claude", label="Claude Code", exe=r"C:\fake\claude.exe",
        process_names=("claude.exe",), winget_id="Anthropic.ClaudeCode")
    agy = cli_update.Target(
        key="gemini", label="Gemini (agy)", exe=r"C:\fake\agy.exe",
        process_names=("agy.exe",), self_update=("update",))

    def upgrade_calls(runner):
        """Every argv that could MUTATE anything."""
        return [a for a in runner.argvs()
                if "upgrade" in a or "install" in a or "update" in a]

    # --- 1. parse_version reads all three real output shapes ---------------
    check("cli-update: parses the Claude CLI's own version line",
          cli_update.parse_version("2.1.224 (Claude Code)") == "2.1.224")
    check("cli-update: parses agy's bare version",
          cli_update.parse_version("1.1.11") == "1.1.11")
    check("cli-update: prefers winget's Version: line over other numbers",
          cli_update.parse_version(
              "Found Claude Code [Anthropic.ClaudeCode]\n"
              "Version: 2.1.231\nRelease Notes Url: https://x/v1.2.3")
          == "2.1.231")
    check("cli-update: garbage parses to nothing",
          cli_update.parse_version("no version here") == "")
    # ...and an unparseable version can never be the REASON to install: a parse
    # failure fails open in both directions
    check("cli-update: an unparseable version never triggers an install",
          not cli_update.needs_update("garbage", "2.1.231")
          and not cli_update.needs_update("2.1.224", "garbage")
          and cli_update.needs_update("2.1.224", "2.1.231"))
    check("cli-update: version ordering is numeric, not lexical",
          cli_update.version_tuple("2.1.99") < cli_update.version_tuple("2.1.224"))

    # --- 2. the toggle off means the machine is never touched --------------
    runner = _FakeCli()
    outs = cli_update.run_gate([claude, agy], runner, enabled=False)
    check("cli-update: switched off, not one command is run",
          runner.calls == [] and [o.status for o in outs]
          == [Status.DISABLED, Status.DISABLED], runner.argvs())
    check("cli-update: a disabled target writes no audit line",
          cli_update.audit_lines(outs[0]) == [])

    # --- 3. already current: the upgrade command is never issued -----------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: installed == available is UP_TO_DATE",
          out.status is Status.UP_TO_DATE and out.before == "2.1.224", out)
    check("cli-update: nothing to do means no upgrade command at all",
          upgrade_calls(runner) == [], runner.argvs())
    check("cli-update: the manifest is read with `winget show`, never `upgrade`",
          ["winget", "show", "--id", "Anthropic.ClaudeCode", "--exact",
           "--accept-source-agreements"] in runner.argvs(), runner.argvs())

    # --- 4. a real update, decided by the FILE both times ------------------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a version that actually moved is UPDATED",
          out.status is Status.UPDATED and (out.before, out.after)
          == ("2.1.224", "2.1.231"), out)
    check("cli-update: the transition is audited",
          "UPDATE claude 2.1.224 -> 2.1.231" in cli_update.audit_lines(out),
          cli_update.audit_lines(out))
    check("cli-update: the check itself is audited with both versions",
          "UPDATE-CHECK claude installed=2.1.224 available=2.1.231"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))
    check("cli-update: success is read back off the binary, not off winget",
          runner.argvs().count([r"C:\fake\claude.exe", "--version"]) == 2,
          runner.argvs())

    # --- 5. THE REPORTED BUG: winget claims success, the file is unchanged --
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      upgrade=(0, "Successfully installed"))
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: an installer that reports success but changes nothing "
          "is REPORTED_BUT_UNCHANGED",
          out.status is Status.REPORTED_BUT_UNCHANGED, out)
    check("cli-update: that is audited precisely",
          any(line.startswith("UPDATE-UNCHANGED claude")
              for line in cli_update.audit_lines(out)),
          cli_update.audit_lines(out))
    check("cli-update: and it earns the pill",
          "Claude Code" in cli_update.pill_text([out]),
          cli_update.pill_text([out]))

    # --- 6. THE CAUSE: a live claude.exe means no upgrade command runs -----
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      alive=3)
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a target process alive blocks the update",
          out.status is Status.BLOCKED_PROCESSES and "3 claude.exe" in out.detail,
          out)
    check("cli-update: blocked means NO winget command at all, so its database "
          "can never be poisoned by us",
          not any(a[0] == "winget" for a in runner.argvs()), runner.argvs())
    check("cli-update: the skip names the count",
          "UPDATE-SKIP claude (3 claude.exe alive)"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))
    # ...and if we cannot even ask, we still refuse (better a missed update
    # than an upgrade run against a file we cannot prove is unlocked)
    runner = _FakeCli(alive=-1, available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: an unanswerable process check blocks too",
          out.status is Status.BLOCKED_PROCESSES
          and not any(a[0] == "winget" for a in runner.argvs()), out)

    # --- 6b. an image NAME is not an identity (live: the gate counted the
    # Claude DESKTOP app's Claude.exe as a locked CLI and skipped forever) ---
    desktop = [r"C:\Program Files\WindowsApps\Claude_1.26832.0.0_x64__p\app"
               r"\Claude.exe"] * 8
    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231", alive=8, paths=desktop)
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: eight processes sharing the image name but NOT the "
          "path do not block the update",
          out.status is Status.UPDATED, out)
    check("cli-update: the cheap name pass runs first and the path pass only "
          "when it found something",
          [a[0] for a in runner.argvs()][:2] == ["tasklist", "powershell"],
          runner.argvs())
    # the same eight, plus one that really is ours: still blocked
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      alive=9, paths=desktop + [r"C:\fake\claude.exe"])
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: one genuine CLI among them still blocks",
          out.status is Status.BLOCKED_PROCESSES and "1 claude.exe" in out.detail,
          out)
    # nothing by that name at all: no reason to pay for the path pass
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    cli_update.run_gate([claude], runner)
    check("cli-update: nothing running means the path pass is never run",
          not any(a[0] == "powershell" for a in runner.argvs()),
          runner.argvs())
    # every ambiguity leans towards over-counting: a skipped update is
    # reportable, an upgrade against a locked file poisons the database
    for label, kwargs in (
            ("a path query that fails", dict(paths_rc=1)),
            ("a path that cannot be read", dict(paths=["?"] * 3)),
            ("a path pass that sees less than the name pass", dict(paths=[]))):
        runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                          available="2.1.231", alive=3, **kwargs)
        out = cli_update.run_gate([claude], runner)[0]
        check(f"cli-update: {label} still blocks",
              out.status is Status.BLOCKED_PROCESSES
              and not any(a[0] == "winget" for a in runner.argvs()), out)
    # two spellings of one file must agree: the live paths differed in case
    # alone (`claude.EXE` from the resolver, `claude.exe` from the listing)
    check("cli-update: path identity ignores case",
          cli_update._canonical(r"C:\Fake\CLAUDE.EXE")
          == cli_update._canonical(r"c:\fake\claude.exe"),
          cli_update._canonical(r"C:\Fake\CLAUDE.EXE"))
    # winget also installs a symlink shim, and a session launched through it
    # reports the LINK -- a plain string compare would call it somebody else's
    tmp = tempfile.mkdtemp(prefix="aihive-cliupd-")
    real = os.path.join(tmp, "claude.exe")
    link = os.path.join(tmp, "link-claude.exe")
    with open(real, "wb") as fh:
        fh.write(b"x")
    linked = True
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError, AttributeError):
        linked = False          # Windows without developer mode / admin
    if linked:
        shim_target = cli_update.Target(
            key="claude", label="Claude Code", exe=real,
            process_names=("claude.exe",), winget_id="Anthropic.ClaudeCode")
        runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                          available="2.1.231", alive=1, paths=[link])
        out = cli_update.run_gate([shim_target], runner)[0]
        check("cli-update: a process launched through the winget shim is "
              "recognised as the same binary",
              out.status is Status.BLOCKED_PROCESSES, out)
    shutil.rmtree(tmp, ignore_errors=True)

    # --- 7. the poisoned database, reported but never forced --------------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      upgrade=(0, "No available upgrade found."))
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: behind the manifest but 'no upgrade found' is DB_STALE",
          out.status is Status.DB_STALE, out)
    check("cli-update: DB_STALE reports and does NOT force a reinstall",
          not any("--force" in a for a in runner.argvs()), runner.argvs())
    check("cli-update: the stale record is audited with both versions",
          any("2.1.224 < 2.1.231" in line
              for line in cli_update.audit_lines(out)),
          cli_update.audit_lines(out))
    check("cli-update: the pill carries the one command to run by hand",
          "winget install --id Anthropic.ClaudeCode --exact --force"
          in cli_update.pill_tooltip([out]), cli_update.pill_tooltip([out]))

    # --- 8. a hung check fails OPEN to launch ------------------------------
    runner = _FakeCli(timeout_on=("winget show",), available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a check that outruns its budget is TIMEOUT",
          out.status is Status.TIMEOUT, out)
    check("cli-update: a timeout is audited and shows no pill (launch wins)",
          cli_update.audit_lines(out) == [
              "UPDATE-CHECK claude installed=2.1.224",
              "UPDATE-TIMEOUT claude check exceeded 5s"]
          and cli_update.pill_text([out]) == "",
          cli_update.audit_lines(out))
    check("cli-update: the check phase is bounded, so no upgrade followed",
          upgrade_calls(runner) == [], runner.argvs())

    # --- 9. serial: two multi-hundred-MB installers never overlap ----------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    outs = cli_update.run_gate([claude, agy], runner)
    seen = [a[0] for a in runner.argvs() if a[0] != "tasklist"]
    first_agy = next(i for i, a in enumerate(runner.argvs())
                     if "agy.exe" in a[0])
    claude_after = [i for i, a in enumerate(runner.argvs())
                    if a[0] == "winget" or "claude.exe" in a[0]]
    check("cli-update: agy's commands never interleave with Claude's",
          all(i < first_agy for i in claude_after), (first_agy, claude_after))
    check("cli-update: both targets are reported, in order",
          [o.target for o in outs] == ["claude", "gemini"], outs)
    # a self-updating CLI has no dry run, so `agy update` IS the check, and an
    # unchanged version afterwards means it was already current, NOT a failure
    check("cli-update: agy self-updates unconditionally (there is no dry run)",
          [r"C:\fake\agy.exe", "update"] in runner.argvs(), runner.argvs())
    check("cli-update: an unchanged agy after a clean self-update is up to date",
          outs[1].status is Status.UP_TO_DATE and cli_update.pill_text(outs) == "",
          outs[1])
    runner = _FakeCli(versions=("1.1.11", "1.2.0"), update_out=(0, "updated"))
    out = cli_update.run_gate([agy], runner)[0]
    check("cli-update: an agy version that moved is UPDATED",
          out.status is Status.UPDATED and out.after == "1.2.0", out)
    runner = _FakeCli(versions=("1.1.11",), update_out=(1, "network down"))
    out = cli_update.run_gate([agy], runner)[0]
    check("cli-update: a failed self-update is FAILED, with the rc audited",
          out.status is Status.FAILED
          and "UPDATE-FAIL gemini rc=1 network down"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))

    # a target that is not installed is skipped in silence
    missing = cli_update.Target(key="claude", label="Claude Code", exe="")
    out = cli_update.run_gate([missing], _FakeCli())[0]
    check("cli-update: an uninstalled CLI is skipped without any command",
          out.status is Status.NOT_INSTALLED
          and cli_update.pill_text([out]) == "", out)

    # --- 11. the pill speaks for exactly four statuses ---------------------
    def one(status):
        return cli_update.Outcome("claude", status, before="2.1.224",
                                  after="2.1.224", available="2.1.231",
                                  detail="rc=1 boom", label="Claude Code")

    speaks = [s for s in Status if cli_update.pill_text([one(s)])]
    check("cli-update: only the four reporting statuses show the pill",
          set(speaks) == {Status.BLOCKED_PROCESSES, Status.DB_STALE,
                          Status.REPORTED_BUT_UNCHANGED, Status.FAILED},
          speaks)
    check("cli-update: a clean gate says nothing at all",
          cli_update.pill_text([one(Status.UPDATED), one(Status.UP_TO_DATE),
                                one(Status.TIMEOUT), one(Status.DISABLED),
                                one(Status.NOT_INSTALLED)]) == "")

    # --- 12. no em dash reaches the reader (the global check covers the
    # module; these are the strings it actually composes at runtime) --------
    composed = cli_update.pill_text([one(Status.DB_STALE)], ("gemini",)) + \
        cli_update.pill_tooltip([one(Status.DB_STALE)], ("gemini",))
    bar = TopBar()
    bar.set_auto_update(True)
    on_tip = bar.auto_update_btn.toolTip()
    bar.set_auto_update(False)
    off_tip = bar.auto_update_btn.toolTip()
    check("cli-update: no em dash in the pill or either tooltip",
          "\u2014" not in composed + on_tip + off_tip)
    check("cli-update: both tooltips say what arming this does",
          "installs it BEFORE any agent launches" in on_tip
          and "changes installed software" in off_tip)

    # --- the switch: default OFF, persisted, and its own Manage door ------
    # On the bar there was room for ONE update control, so the button had to be
    # the door to the Updates panel and the preference lived on a checkbox
    # inside it. The Options panel has room for both, so the switch is a switch
    # and `updates_manage_btn` is the door. That is not two controls for one
    # setting: `open_updates_panel` drives BOTH from the panel's own signal.
    check("cli-update toggle: defaults to OFF (it installs software)",
          not bar.auto_update() and not bar.auto_update_btn.isChecked())
    emitted, opened = [], []
    bar.autoUpdateToggled.connect(emitted.append)
    bar.updatesPanelRequested.connect(lambda: opened.append(True))
    bar.auto_update_btn.click()
    check("cli-update toggle: the switch arms the gate and emits True",
          emitted == [True] and opened == [] and bar.auto_update()
          and bar.auto_update_btn.isChecked(), (emitted, opened))
    bar.updates_manage_btn.click()
    check("cli-update toggle: Manage opens the panel and changes no preference",
          opened == [True] and emitted == [True] and bar.auto_update())
    bar.set_auto_update(False)
    check("cli-update toggle: set_auto_update does not re-emit",
          emitted == [True] and not bar.auto_update()
          and not bar.auto_update_btn.isChecked())
    bar.note_update_pending("")
    check("cli-update pill: hidden when there is nothing to report",
          not bar.update_pill.isVisibleTo(bar.options_panel)
          and not bar.options_btn.property("attention"))
    bar.note_update_pending("something", "the long form")
    check("cli-update pill: shown with its tooltip when there is",
          bar.update_pill.text() == "something"
          and bar.update_pill.toolTip() == "the long form"
          and bar.update_pill.isVisibleTo(bar.options_panel))
    check("cli-update pill: it also lights the Options button, so a warning "
          "behind a closed panel is not a secret",
          bar.options_btn.property("attention") is True
          and "something" in bar.options_btn.toolTip())
    # the detected install method is stated in full under the switch, not
    # crammed into a tooltip
    bar.note_install_state("Claude Code, WinGet package")
    check("cli-update: the install method is named under the switch",
          bar.install_label.text() == "Claude Code, WinGet package"
          and bar.install_label.isVisibleTo(bar.options_panel))
    bar.deleteLater()

    # --- 10. ui.auto_update round-trips, defaults False, no version bump ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-cliupdate-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win._save_timer.stop()
    check("cli-update: a session that predates the feature defaults it OFF",
          not win._auto_update and not win.top_bar.auto_update())
    check("cli-update: the preference is persisted under ui",
          win._session_payload()["ui"]["auto_update"] is False)
    win._on_auto_update_toggled(True)
    check("cli-update: flipping the preference marks the session dirty",
          win._save_timer.isActive() and win._auto_update)
    check("cli-update: ...and it is what gets written",
          win._session_payload()["ui"]["auto_update"] is True)
    win._restore_ui_state({"ui": {"auto_update": True}})
    check("cli-update: the preference restores onto the window and the button",
          win._auto_update and win.top_bar.auto_update())
    win._restore_ui_state({"ui": {}})
    check("cli-update: a missing key still means OFF",
          not win._auto_update and not win.top_bar.auto_update())
    check("cli-update: the key is additive, so SESSION_VERSION is unchanged",
          SESSION_VERSION == 4, SESSION_VERSION)

    # a real close/reopen, which is the only way to catch a default assigned
    # AFTER _restore_ui_state has run (that exact bug hit the taskbar toggle)
    win._auto_update = True
    win.top_bar.set_auto_update(True)
    win._save_session()
    win.close()
    app.processEvents()
    again = create_main_window(SessionStore(path=tmp / "session.json"))
    check("cli-update: ON survives a close and reopen",
          again._auto_update and again.top_bar.auto_update())

    # --- the report, and the outcomes never touching session state ---------
    again._save_timer.stop()
    blocked = cli_update.Outcome("claude", Status.BLOCKED_PROCESSES,
                                 detail="3 claude.exe alive", label="Claude Code")
    again.note_update_outcomes([blocked])
    check("cli-update: the gate's report reaches the top-bar pill",
          again.top_bar.update_pill.isVisibleTo(
              again.top_bar.options_panel)
          and "Claude Code" in again.top_bar.update_pill.text(),
          again.top_bar.update_pill.text())
    check("cli-update: reporting an outcome NEVER marks the session dirty "
          "(outcomes are transient, only the preference persists)",
          not again._save_timer.isActive())
    check("cli-update: the window keeps the outcomes so the Updates panel can "
          "show them after the splash has closed",
          again._update_outcomes == (blocked,)
          and "Claude Code" in cli_update.last_check_summary(
              again._update_outcomes))
    check("cli-update: ...and keeping them is transient too",
          not again._save_timer.isActive()
          and "update_outcomes" not in str(again._session_payload()))
    again.note_update_outcomes([cli_update.Outcome("claude", Status.UP_TO_DATE)])
    check("cli-update: a clean gate leaves the pill hidden",
          not again.top_bar.update_pill.isVisibleTo(
              again.top_bar.options_panel))

    # --- 6.1: skipping while an install runs holds those agents back -------
    # `start` is stubbed rather than really called: the question is which
    # agents the autostart DECIDES to launch, and no test should spawn a CLI
    # whose binary this feature is notionally rewriting.
    from app.process_worker import AgentKind, build_spec
    ws = again.manager.create_workspace("CliUpdate", str(tmp))
    gemini_agent = again.manager.add_terminal(
        ws.id, build_spec(AgentKind.GEMINI, "G1", cwd=str(tmp)),
        autostart=False)
    claude_agent = again.manager.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "C1", cwd=str(tmp)),
        autostart=False)
    started = []
    for a in again.manager.all_agents():
        a.autostart_on_restore = True
        a.start = (lambda agent=a: started.append(agent.spec.name))

    again.note_update_outcomes([blocked], installing=("gemini",))
    check("cli-update: the pill says which agents are being held back",
          "Gemini (agy)" in again.top_bar.update_pill.text(),
          again.top_bar.update_pill.text())
    again.autostart_active_workspace()
    check("cli-update: an agent whose CLI is mid-install is NOT started "
          "(it could execute a half written binary)",
          "G1" not in started, started)
    check("cli-update: ...while every other agent starts as usual",
          "C1" in started, started)
    started.clear()
    again.note_update_outcomes([blocked])   # install finished / normal launch
    again.autostart_active_workspace()
    check("cli-update: and with nothing installing, the deferral is gone",
          again._update_installing == () and "G1" in started, started)
    again.close()
    app.processEvents()

    # --- 13. the factory the suite shares does NO update work --------------
    import inspect
    import main as main_module
    src = inspect.getsource(main_module.create_main_window)
    check("cli-update: create_main_window contains no update work at all",
          "update_gate" not in src and "cli_update" not in src)
    real_runner, real_gate = cli_update.subprocess_runner, cli_update.run_gate
    touched = []
    cli_update.subprocess_runner = lambda *a, **k: touched.append(a) or (0, "")
    cli_update.run_gate = lambda *a, **k: touched.append(a) or []
    try:
        armed = SessionStore(path=tmp / "armed.json")
        armed.save({"ui": {"auto_update": True}})
        spare = create_main_window(armed)
        spare._save_timer.stop()
        check("cli-update: building a window with the toggle ON still runs "
              "nothing (the gate is opted into from main.py alone)",
              touched == [] and spare._auto_update, touched)
        spare.close()
        app.processEvents()
    finally:
        cli_update.subprocess_runner = real_runner
        cli_update.run_gate = real_gate

    # --- the splash + worker thread, end to end, with a fake runner --------
    from app.widgets.update_splash import UpdateSplash, run_update_gate
    audits = []

    class _Audits:
        def audit(self, line):
            audits.append(line)

    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231")
    result = run_update_gate(_Audits(), targets=[claude], runner=runner,
                             auto_close_ms=0)
    check("cli-update splash: the gate runs off the GUI thread and returns "
          "its outcomes",
          [o.status for o in result.outcomes] == [Status.UPDATED], result)
    check("cli-update splash: nothing was still installing, so no deferral",
          result.installing == ())
    check("cli-update splash: the audit trail reached session.log",
          any(line.startswith("UPDATE claude") for line in audits), audits)
    splash = UpdateSplash([claude, agy])
    splash.set_state("claude", "checking", True)
    check("cli-update splash: a row shows what it is doing",
          splash.row_state("claude") == "checking")
    splash.set_state("claude", "up to date (2.1.224)", False)
    check("cli-update splash: the spinner stops once nothing is busy",
          splash._spin.state() != splash._spin.State.Running)
    splash.close()
    splash.deleteLater()

    # --- 14. what a skipped check MEANS depends on who updates the binary ---
    # Live report: after the native migration the splash flashed "took too
    # long, skipped" for under a second on a launch where nothing was wrong,
    # the CLI updated itself a minute later, and no surface afterwards could
    # say so. A self-updating target that we failed to check is a non event;
    # the same status on a package managed one is a genuinely missed update.
    from app.widgets.update_splash import LINGER_CLOSE_MS, subtitle_for

    native_claude = cli_update.Target(
        key="claude", label="Claude Code", exe=r"C:\fake\claude.exe",
        process_names=("claude.exe",), self_update=("update",))

    def outcome(status, self_updating, **kw):
        return cli_update.Outcome("claude", status, label="Claude Code",
                                  self_updating=self_updating, **kw)

    self_late = outcome(Status.TIMEOUT, True, before="2.1.228",
                        detail="check exceeded 5s")
    winget_late = outcome(Status.TIMEOUT, False, before="2.1.224",
                          detail="check exceeded 5s")
    gemini_ok = cli_update.Outcome("gemini", Status.UP_TO_DATE, before="1.1.12",
                                   label="Gemini (agy)", self_updating=True)

    check("cli-update words: a self-updating CLI we could not check is left to "
          "its own updater, not reported as skipped",
          cli_update.state_text(self_late) == "left to its own updater")
    check("cli-update words: the same status on a package managed CLI still "
          "says the update was missed (nothing else will fetch one)",
          cli_update.state_text(winget_late) == "took too long, skipped")
    check("cli-update words: the shape is stamped on the outcome by check(), "
          "so no caller has to remember to set it",
          cli_update.check(native_claude,
                           _FakeCli(timeout_on=("--version",))).self_updating
          and not cli_update.check(claude,
                                   _FakeCli(timeout_on=("--version",))
                                   ).self_updating)

    check("cli-update pill: a self-updating CLI raises no nag, because every "
          "instruction the nag carries would be unnecessary",
          cli_update.pill_text([self_late, gemini_ok]) == ""
          and cli_update.pill_tooltip([self_late]) == "")
    self_busy = outcome(Status.BLOCKED_PROCESSES, True,
                        detail="3 claude.exe alive")
    winget_busy = outcome(Status.BLOCKED_PROCESSES, False,
                          detail="3 claude.exe alive")
    check("cli-update pill: ...and that holds for a locked file too, since a "
          "self-updating CLI does not need the user to close anything",
          cli_update.pill_text([self_busy]) == ""
          and "Claude Code" in cli_update.pill_text([winget_busy]))
    check("cli-update pill: the splash and the pill can never disagree, so an "
          "outcome the splash calls a non event never raises a nag",
          all(not cli_update.needs_pill(o)
              for o in (self_late, winget_late, self_busy, winget_busy)
              if cli_update.state_text(o)
              in cli_update._SELF_UPDATING_TEXT.values())
          and cli_update.needs_pill(winget_busy))

    check("cli-update linger: a missed update earns a moment to be read",
          cli_update.worth_reading([winget_late])
          and cli_update.worth_reading([winget_busy]))
    check("cli-update linger: ...and a non event does not, or the pause would "
          "manufacture the concern the wording removes",
          not cli_update.worth_reading([self_late, gemini_ok])
          and not cli_update.worth_reading([]))

    # the durable copy. The pill speaks only for what the user can act on, so
    # without this a status glimpsed on the splash has nowhere to be re-read.
    summary = cli_update.last_check_summary([self_late, gemini_ok])
    check("cli-update panel: the last check reports EVERY outcome, including "
          "the ones the pill deliberately withholds",
          "Claude Code: left to its own updater" in summary
          and "Gemini (agy): up to date (1.1.12)" in summary, summary)
    check("cli-update panel: ...and names anything still installing",
          "still installing" in
          cli_update.last_check_summary([self_late], installing=("gemini",)))

    check("cli-update audit: the timeout line records which shape it describes",
          cli_update.audit_lines(self_late)[-1].endswith(
              "(self-updating, left to the CLI)")
          and cli_update.audit_lines(winget_late)[-1].endswith("exceeded 5s"),
          cli_update.audit_lines(self_late))

    check("cli-update splash: the subtitle drops the locked-file claim when "
          "every target updates itself (the native install never touches the "
          "running file)",
          "not in use" not in subtitle_for([native_claude, agy])
          and "before agents start" in subtitle_for([native_claude, agy]))
    check("cli-update splash: ...and keeps it while any target is package "
          "managed, where it is both true and the reason to wait",
          subtitle_for([claude, agy]) ==
          "Now is the only moment these files are not in use.")

    # the linger, end to end through the real event loop
    for target, expect_wait in ((claude, True), (native_claude, False)):
        started = time.time()
        run_update_gate(None, targets=[target],
                        runner=_FakeCli(timeout_on=("--version",)),
                        auto_close_ms=0, linger_ms=400, show=True)
        waited = time.time() - started
        check("cli-update splash: a missed update holds the window open"
              if expect_wait else
              "cli-update splash: ...and a non event closes as fast as a "
              "clean launch",
              (waited >= 0.35) is expect_wait, (target.key, waited))
    check("cli-update splash: the linger is long enough to actually read",
          LINGER_CLOSE_MS >= 2000)


class _FakeInstall:
    """A recording `Runner` for the install-method control.

    Same rule as `_FakeCli`: no check below shells out, installs anything, or
    touches the user's real `~/.claude/settings.json`. `versions` maps a
    canonical exe path to what `--version` prints; `lands` is merged in when the
    installer runs, which is how "the installer reported success but the file
    did not change" is expressed."""

    def __init__(self, versions=None, lands=None, install=(0, "installed"),
                 alive=0, paths=None, winget=(0, "")):
        from app.cli_update import _canonical
        self.calls = []
        self._canon = _canonical
        self.versions = {_canonical(k): v for k, v in (versions or {}).items()}
        self.lands = {_canonical(k): v for k, v in (lands or {}).items()}
        self.install = install
        self.alive = alive
        self.paths = paths
        self.winget = winget

    def argvs(self):
        return [list(a) for a in self.calls]

    def mutating(self):
        """Every argv that could change installed software."""
        return [a for a in self.argvs()
                if any(t in " ".join(a).lower()
                       for t in ("install", "uninstall", "upgrade", "irm "))]

    def __call__(self, argv, timeout=0):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[0] == "tasklist":
            name = argv[2].split()[-1]
            body = "\n".join(f"{name}   {1000 + i} Console  1  285,000 K"
                             for i in range(max(0, self.alive)))
            return 0, body or "INFO: No tasks are running"
        if argv[0] == "powershell" and "Get-CimInstance" in joined:
            lines = self.paths if self.paths is not None else []
            return 0, "\n".join(lines)
        if argv[0] == "powershell":            # the documented installer
            self.versions.update(self.lands)
            return self.install
        if argv[-1] == "--version":
            found = self.versions.get(self._canon(argv[0]), "")
            return (0, found) if found else (1, "not found")
        if argv[0] == "winget":
            return self.winget
        return 0, ""


def test_cli_native_migration():
    """The consented switch onto the self-updating native install.

    Root cause it exists for: a package-manager Claude Code does not update
    itself and no setting fixes that, while model aliases resolve CLIENT-SIDE
    from a table baked into the installed binary, so a stale file silently
    cannot launch newer models. The install method IS the behaviour, hence a
    migration rather than a preference.

    Everything is driven through injected runners and temporary directories:
    no check installs anything, shells out, or touches the real settings file.
    """
    import json as _json
    import threading
    from PySide6.QtWidgets import QApplication
    from app import cli_install, cli_update, providers
    from app.cli_install import InstallKind, UpdateState
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.widgets.update_panel import ConsentDialog, UpdatePanel
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-migrate-"))
    winget_exe = str(tmp / "Microsoft" / "WinGet" / "Packages"
                     / "Anthropic.ClaudeCode_x" / "claude.exe")
    native_exe = str(tmp / "home" / ".local" / "bin" / "claude.exe")

    # --- 1. classify_install: the PATH is the only thing that tells the two
    # installs apart (identical binary, version string and process name) ------
    check("cli-install: a WinGet-Packages path classifies as WINGET",
          cli_install.classify_install(
              r"C:\Users\x\AppData\Local\Microsoft\WinGet\Packages"
              r"\Anthropic.ClaudeCode_y\claude.exe") is InstallKind.WINGET)
    check("cli-install: a .local\\bin path classifies as NATIVE",
          cli_install.classify_install(r"C:\Users\x\.local\bin\claude.exe")
          is InstallKind.NATIVE)
    check("cli-install: an npm global path classifies as NPM",
          cli_install.classify_install(r"C:\Users\x\AppData\Roaming\npm\claude.cmd")
          is InstallKind.NPM
          and cli_install.classify_install(
              r"C:\x\node_modules\.bin\claude.exe") is InstallKind.NPM)
    check("cli-install: no binary at all is MISSING",
          cli_install.classify_install("") is InstallKind.MISSING
          and cli_install.classify_install("claude.exe") is InstallKind.MISSING)
    # case and symlink: winget also installs a Links shim, so a plain string
    # compare would read the user's own session as somebody else's program
    real = tmp / "packages" / "Microsoft" / "WinGet" / "Packages" / "A_x"
    real.mkdir(parents=True, exist_ok=True)
    (real / "claude.exe").write_text("x", encoding="utf-8")
    check("cli-install: classification ignores case",
          cli_install.classify_install(
              str(real / "claude.exe").upper()) is InstallKind.WINGET)
    link = tmp / "Links" / "claude.exe"
    link.parent.mkdir(parents=True, exist_ok=True)
    linked = True
    try:
        os.symlink(real / "claude.exe", link)
    except (OSError, NotImplementedError, AttributeError):
        linked = False       # unprivileged Windows cannot create a symlink
    check("cli-install: a symlink shim resolves to its target's install kind",
          (not linked) or cli_install.classify_install(str(link))
          is InstallKind.WINGET)

    # --- 2. detect: one state per machine, and never a boolean --------------
    settings = tmp / "settings.json"
    settings.write_text(_json.dumps({"model": "opus", "permissions": {"a": 1}}),
                        encoding="utf-8")

    def situation(exe, policies=()):
        return cli_install.detect(exe, str(settings), policies=list(policies))

    got = situation(winget_exe)
    check("cli-install: a winget install with no policy offers the migration",
          got.state is UpdateState.MANAGED and got.actionable(), got)
    got = situation(native_exe)
    check("cli-install: a native install with no disabling key is SELF_ACTIVE",
          got.state is UpdateState.SELF_ACTIVE and got.paused_key == "", got)
    settings.write_text(_json.dumps(
        {"model": "opus", "env": {"DISABLE_AUTOUPDATER": "1"}}),
        encoding="utf-8")
    got = situation(native_exe)
    check("cli-install: DISABLE_AUTOUPDATER reads as SELF_PAUSED, and the key "
          "is named",
          got.state is UpdateState.SELF_PAUSED
          and got.paused_key == "DISABLE_AUTOUPDATER", got)
    settings.write_text(_json.dumps({"env": {"DISABLE_UPDATES": "1"}}),
                        encoding="utf-8")
    check("cli-install: DISABLE_UPDATES also reads as SELF_PAUSED",
          situation(native_exe).state is UpdateState.SELF_PAUSED
          and situation(native_exe).paused_key == "DISABLE_UPDATES")
    settings.write_text(_json.dumps({}), encoding="utf-8")
    got = situation(r"C:\Users\x\AppData\Roaming\npm\claude.cmd")
    check("cli-install: an npm install already auto-updates, so nothing is "
          "offered",
          got.state is UpdateState.NOT_APPLICABLE and not got.actionable(), got)
    got = situation(native_exe, policies=[{"autoUpdatesChannel": "stable"}])
    check("cli-install: managed settings enforcing updates lock the control, "
          "with the reason shown",
          got.state is UpdateState.LOCKED_BY_POLICY and not got.actionable()
          and "managed settings" in got.detail, got)
    check("cli-install: a policy pinning the env key locks it too",
          situation(native_exe,
                    policies=[{"env": {"DISABLE_UPDATES": "1"}}]).state
          is UpdateState.LOCKED_BY_POLICY)
    check("cli-install: the managed path read is the documented one, not a "
          "guess",
          cli_install.managed_settings_path().endswith(
              os.path.join("ClaudeCode", "managed-settings.json")),
          cli_install.managed_settings_path())

    # --- 3. the installer line is the documented one, and only that ---------
    argv = cli_install.install_argv()
    check("cli-install: the installer is Anthropic's documented one",
          argv[0] == "powershell" and "irm https://claude.ai/install.ps1 | iex"
          in " ".join(argv), argv)
    check("cli-install: ...and it never runs winget",
          "winget" not in " ".join(argv).lower(), argv)
    # CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE is deliberately unsupported: it
    # makes a RUNNING Claude Code invoke `winget upgrade` on itself, which is
    # the exact act that writes the false winget database record cli_update.py
    # exists to prevent. It is named in the module docstring as a decision and
    # must never become code, so this asserts the module sets no env at all.
    module_src = Path("app/cli_install.py").read_text(encoding="utf-8")
    panel_src = Path("app/widgets/update_panel.py").read_text(encoding="utf-8")
    check("cli-install: no code path ever sets an environment variable, so "
          "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE can never be turned on",
          "os.environ[" not in module_src and "putenv" not in module_src
          and "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE" not in panel_src)

    # --- 4. migrate decides success by RE-READING THE FILE ------------------
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: a migration whose file really moved is ok",
          out.ok and out.after == "2.1.231", out)
    check("cli-install: the transition is audited",
          cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          cli_install.audit_lines(out))
    runner = _FakeInstall()          # installer says rc 0, nothing landed
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: an installer that reports success while the file did "
          "not change is a FAILURE",
          not out.ok and not out.after, out)
    check("cli-install: ...and the failure is audited, not the success",
          cli_install.audit_lines(out)[0].startswith("CLI-MIGRATE-FAIL"))
    runner = _FakeInstall(lands={native_exe: "2.1.200"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: a native build OLDER than the one you had is refused",
          not out.ok and "older" in out.detail, out)
    slow = _FakeInstall(install=(cli_update.RC_TIMEOUT, ""))
    out = cli_install.migrate(slow, launcher=native_exe, before="2.1.224")
    check("cli-install: an installer that never finished claims no version",
          not out.ok and out.after == "" and out.before == "2.1.224", out)

    # --- 4.1 a successful migrate fixes the USER'S OWN terminal's PATH too --
    # `path_updater` is injected exactly like `runner`: the suite must never
    # touch the real Windows User PATH registry key, so these drive FAKE
    # updaters and never call the real `ensure_native_on_path`.
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: with no path_updater given, nothing is added and "
          "nothing is audited about PATH (the default the suite exercises)",
          out.path_added is False
          and cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          (out, cli_install.audit_lines(out)))
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=lambda: True)
    check("cli-install: a path_updater that changed PATH is reflected on the "
          "result and audited",
          out.ok and out.path_added is True
          and cli_install.audit_lines(out) == [
              "CLI-MIGRATE-OK 2.1.224 -> 2.1.231",
              "CLI-MIGRATE-PATH added .local\\bin to the User PATH"],
          (out, cli_install.audit_lines(out)))
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=lambda: False)
    check("cli-install: a path_updater reporting 'already there' adds nothing",
          out.ok and out.path_added is False
          and cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          out)

    def _boom():
        raise OSError("registry is locked")

    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=_boom)
    check("cli-install: a PATH nicety that raises never takes the migration "
          "itself down with it",
          out.ok and out.after == "2.1.231" and out.path_added is False, out)

    # `_compute_updated_path` is the pure decision `ensure_native_on_path`
    # makes before ever touching the registry, and is tested directly for
    # that reason.
    check("cli-install: an empty PATH becomes just the target",
          cli_install._compute_updated_path("", r"C:\u\.local\bin")
          == r"C:\u\.local\bin")
    check("cli-install: the target is appended after existing entries",
          cli_install._compute_updated_path(
              r"C:\a;C:\b", r"C:\u\.local\bin")
          == r"C:\a;C:\b;C:\u\.local\bin")
    check("cli-install: an exact match already on PATH changes nothing",
          cli_install._compute_updated_path(
              r"C:\a;C:\u\.local\bin;C:\b", r"C:\u\.local\bin") is None)
    check("cli-install: matching is case- and trailing-backslash-insensitive, "
          "like Windows PATH lookups are",
          cli_install._compute_updated_path(
              r"C:\a;" + r"C:\U\.LOCAL\BIN" + "\\", r"C:\u\.local\bin") is None)

    # --- 5. resolve_claude prefers the NATIVE launcher (the §4.1 trap) ------
    # MEASURED: the winget package directory is on PATH directly, and
    # %USERPROFILE%\.local\bin is not, so `shutil.which` would keep answering
    # with the stale copy and the migration would appear to do nothing.
    home = tmp / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    Path(native_exe).write_text("native", encoding="utf-8")
    real_which, real_home = providers.shutil.which, os.environ.get("USERPROFILE")
    try:
        providers.shutil.which = lambda name: winget_exe
        os.environ["USERPROFILE"] = str(home)
        check("cli-install: resolve_claude prefers the native launcher even "
              "when PATH would answer with the winget copy",
              cli_update._canonical(providers.resolve_claude())
              == cli_update._canonical(native_exe),
              providers.resolve_claude())
        os.environ["USERPROFILE"] = str(tmp / "nowhere")
        check("cli-install: ...and falls back to PATH when there is no native "
              "install",
              providers.resolve_claude() == winget_exe)
    finally:
        providers.shutil.which = real_which
        if real_home is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = real_home

    # --- 6. the settings file: other writers, and they are OURS -------------
    doc = {"model": "opus", "effortLevel": "high",
           "hooks": {"Stop": [{"x": 1}]}, "somethingWeNeverHeardOf": [1, 2]}
    settings.write_text(_json.dumps(doc, indent=2), encoding="utf-8")
    out = cli_install.set_paused(str(settings), True)
    after = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: pausing writes exactly one key and preserves every "
          "other, including unknown ones",
          out.ok and after["env"] == {"DISABLE_AUTOUPDATER": "1"}
          and {k: after[k] for k in doc} == doc, after)
    check("cli-install: a settings write keeps one .bak generation",
          (tmp / "settings.json.bak").is_file())
    out = cli_install.set_paused(str(settings), False)
    check("cli-install: resuming restores the original document shape",
          out.ok and _json.loads(settings.read_text(encoding="utf-8")) == doc,
          settings.read_text(encoding="utf-8"))
    settings.write_text(_json.dumps({"env": {"DISABLE_UPDATES": "1",
                                             "FOO": "bar"}}), encoding="utf-8")
    cli_install.set_paused(str(settings), False)
    check("cli-install: resuming clears the STRICT key too, and leaves the "
          "user's own env alone",
          _json.loads(settings.read_text(encoding="utf-8"))
          == {"env": {"FOO": "bar"}})

    broken = tmp / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    before_bytes = broken.read_bytes()
    out = cli_install.set_paused(str(broken), True)
    check("cli-install: an unparseable settings.json is REFUSED, never "
          "rewritten or repaired",
          not out.ok and broken.read_bytes() == before_bytes
          and not (tmp / "broken.json.bak").exists(), out)

    # THE LOST-UPDATE CHECK. `~/.claude/settings.json` has other writers and
    # they are ours: every agent's `/model` and `/config` writes it, and the
    # panel can sit open for a minute between the read and the write.
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    stale, _reason = cli_install.read_settings(str(settings))
    settings.write_text(_json.dumps({"model": "opus", "savedByAnAgent": True}),
                        encoding="utf-8")
    cli_install.set_paused(str(settings), True)
    landed = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: write_settings RE-READS, so a key an agent saved while "
          "the panel was open survives",
          landed.get("savedByAnAgent") is True
          and landed["env"]["DISABLE_AUTOUPDATER"] == "1"
          and "savedByAnAgent" not in stale, landed)

    guard = tmp / "guard.json"
    guard.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    real_read = cli_install.read_settings
    reads = {"n": 0}

    def racing_read(path):
        reads["n"] += 1
        if reads["n"] == 2:            # the re-read inside write_settings
            guard.write_text("{ broken now", encoding="utf-8")
        return real_read(path)

    cli_install.read_settings = racing_read
    try:
        cli_install.read_settings(str(guard))         # the caller's read
        out = cli_install.set_paused(str(guard), True)
    finally:
        cli_install.read_settings = real_read
    check("cli-install: a re-read that no longer parses ABORTS the write",
          not out.ok and guard.read_text(encoding="utf-8") == "{ broken now",
          out)

    # --- 7. the channel is NEVER written without its floor ------------------
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "stable", "2.1.226")
    doc = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: stable writes the channel AND a minimumVersion floor",
          out.ok and doc == {"model": "opus", "autoUpdatesChannel": "stable",
                             "minimumVersion": "2.1.226"}, doc)
    check("cli-install: the channel change is audited with its floor",
          cli_install.audit_lines(out)
          == ["CLI-MIGRATE-CHANNEL stable floor=2.1.226"])
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "stable", "")
    check("cli-install: a channel with no readable floor is REFUSED and "
          "writes nothing (stable on its own can move you BACKWARDS)",
          not out.ok
          and _json.loads(settings.read_text(encoding="utf-8"))
          == {"model": "opus"}, out)
    settings.write_text(_json.dumps(
        {"autoUpdatesChannel": "stable", "minimumVersion": "2.1.226",
         "model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "latest", "2.1.231")
    doc = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: going back to latest REMOVES the floor, so the pin "
          "cannot outlive the reason for it",
          out.ok and "minimumVersion" not in doc
          and doc["autoUpdatesChannel"] == "latest", doc)
    body = Path("app/cli_install.py").read_text(encoding="utf-8")
    check("cli-install: requiredMinimumVersion is never WRITTEN (it stops "
          "Claude Code starting at all, and is not ours to set)",
          'doc["requiredMinimumVersion"]' not in body
          and "requiredMinimumVersion\"] =" not in body
          and all("requiredM" not in str(v)
                  for v in _json.loads(
                      settings.read_text(encoding="utf-8")).keys()))

    # --- 8. the update TARGET follows the install method --------------------
    native_target = cli_update._claude_target(native_exe)
    winget_target = cli_update._claude_target(winget_exe)
    check("cli-install: a native Claude Code is a self-updating target",
          native_target.self_update == ("update",)
          and native_target.winget_id is None, native_target)
    check("cli-install: ...and nothing about it mentions winget",
          "winget" not in " ".join(
              cli_update.upgrade_argv(native_target)
              + cli_update.check_argv(native_target)).lower())
    check("cli-install: a winget Claude Code still goes through winget",
          winget_target.winget_id == "Anthropic.ClaudeCode"
          and winget_target.self_update is None, winget_target)
    stale_runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                            update_out=(0, "No available upgrade found"))
    out = cli_update.run_gate([native_target], stale_runner)[0]
    check("cli-install: DB_STALE is structurally unreachable on a native "
          "install (there is no package database to go stale)",
          out.status is cli_update.Status.UP_TO_DATE, out)

    # --- 9. cleanup counts against the RECORDED WinGet path -----------------
    desktop = r"C:\Users\x\AppData\Local\AnthropicClaude\app-1.0\claude.exe"
    runner = _FakeInstall(alive=1, paths=[winget_exe])
    out = cli_install.cleanup(runner, winget_exe)
    check("cli-install: a live session on the WINGET binary blocks cleanup",
          not out.ok and "still using" in out.detail, out)
    check("cli-install: ...and no kill command is ever issued",
          not any("taskkill" in " ".join(a).lower() or "stop-process"
                  in " ".join(a).lower() for a in runner.argvs())
          and runner.mutating() == [], runner.argvs())
    runner = _FakeInstall(alive=1, paths=[desktop])
    out = cli_install.cleanup(runner, winget_exe)
    check("cli-install: the Claude DESKTOP app shares the image name but not "
          "the path, so it does not block cleanup",
          out.ok, out)
    check("cli-install: ...which is the one command that removes the package",
          runner.mutating() == [["winget", "uninstall", "--id",
                                 "Anthropic.ClaudeCode", "--exact"]],
          runner.mutating())
    runner = _FakeInstall(alive=1, paths=[native_exe])
    out = cli_install.cleanup(runner, native_exe)
    check("cli-install: cleanup counts the path it was GIVEN, so passing the "
          "resolved (native) binary is what would under-count",
          not out.ok and out.after == native_exe, out)
    check("cli-install: the cleanup line names the file it was about",
          cli_install.audit_lines(out)[0].endswith("exe=" + native_exe),
          cli_install.audit_lines(out))

    # --- 9b. the FULL revert, whose ORDER is not interchangeable ------------
    # The WinGet copy goes back and is verified FIRST, so a failed reinstall
    # leaves the user with the working native install rather than with nothing.
    rev = tmp / "revert"
    pkg = (rev / "local" / "Microsoft" / "WinGet" / "Packages"
           / "Anthropic.ClaudeCode_z")
    pkg.mkdir(parents=True, exist_ok=True)
    native_bin = rev / "home" / ".local" / "bin"
    native_bin.mkdir(parents=True, exist_ok=True)
    (native_bin / "claude.exe").write_text("native", encoding="utf-8")
    share = rev / "home" / ".local" / "share" / "claude" / "versions"
    share.mkdir(parents=True, exist_ok=True)
    (share / "2.1.231").write_text("payload", encoding="utf-8")
    real_home, real_local = (os.environ.get("USERPROFILE"),
                             os.environ.get("LOCALAPPDATA"))
    try:
        os.environ["USERPROFILE"] = str(rev / "home")
        os.environ["LOCALAPPDATA"] = str(rev / "local")
        runner = _FakeInstall(alive=1, paths=[str(native_bin / "claude.exe")])
        out = cli_install.revert(runner)
        check("cli-install: a revert is refused while a native session is "
              "alive, and never kills one",
              not out.ok and runner.mutating() == []
              and (native_bin / "claude.exe").is_file(), out)
        runner = _FakeInstall(alive=0)
        out = cli_install.revert(runner)
        check("cli-install: a WinGet copy that did not come back leaves the "
              "native install in place",
              not out.ok and (native_bin / "claude.exe").is_file(), out)
        (pkg / "claude.exe").write_text("winget", encoding="utf-8")
        runner = _FakeInstall(alive=0,
                              versions={str(pkg / "claude.exe"): "2.1.224"})
        out = cli_install.revert(runner)
        check("cli-install: a verified revert puts WinGet back and removes the "
              "native install",
              out.ok and out.after == "2.1.224"
              and not (native_bin / "claude.exe").exists()
              and not share.parent.exists()
              and (pkg / "claude.exe").is_file(), out)
        check("cli-install: the revert is audited",
              cli_install.audit_lines(out) == ["CLI-MIGRATE-REVERT ok 2.1.224"])
    finally:
        for name, value in (("USERPROFILE", real_home),
                            ("LOCALAPPDATA", real_local)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # --- 10. the live-spec rebuild (§5 step 5) ------------------------------
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win._save_timer.stop()
    ws = win.manager.create_workspace("Migrate", str(tmp))
    real_resolve = providers.resolve_claude
    try:
        providers.resolve_claude = lambda: winget_exe
        spec = build_spec(AgentKind.CLAUDE, "C1", cwd=str(tmp), model="opus",
                          effort="high", permission_mode="plan")
        agent = win.manager.add_terminal(ws.id, spec, autostart=False)
        check("cli-install: a spec built before the migration carries the "
              "WinGet path",
              agent.spec.program == winget_exe, agent.spec.program)
        disturbed = []
        agent.start = lambda *a, **k: disturbed.append("start")
        agent.stop = lambda *a, **k: disturbed.append("stop")
        agent.restart = lambda *a, **k: disturbed.append("restart")
        providers.resolve_claude = lambda: native_exe
        win._save_timer.stop()
        moved = win.rebind_claude_specs()
        check("cli-install: the rebuild repoints every live Claude spec at the "
              "new binary",
              moved == 1 and agent.spec.program == native_exe,
              agent.spec.program)
        check("cli-install: ...preserving everything the user chose",
              agent.spec.model == "opus" and agent.spec.effort == "high"
              and agent.spec.permission_mode == "plan"
              and "--permission-mode" in agent.spec.args
              and agent.spec.user_program == "" and agent.spec.user_args == [])
        check("cli-install: ...without stopping or restarting a running agent",
              disturbed == [], disturbed)
        check("cli-install: ...and without marking the session dirty "
              "(program/args are derived, never persisted)",
              not win._save_timer.isActive()
              and "program" not in agent.spec.to_dict())
        check("cli-install: the rebuild is idempotent, so a second migration "
              "attempt is harmless",
              win.rebind_claude_specs() == 0)
    finally:
        providers.resolve_claude = real_resolve

    # --- 11. the panel: state in, one action out ----------------------------
    panel = UpdatePanel(situation(winget_exe), auto_update=False, runner=None,
                        winget_exe=winget_exe, settings_file=str(settings),
                        parent=win)
    check("cli-install panel: a winget machine is offered the migration, and "
          "the cleanup/revert controls that belong to a native install are "
          "not shown",
          panel.action_btn.text() == "Enable automatic updates"
          and not panel.revert_btn.isVisibleTo(panel)
          and not panel.cleanup_btn.isVisibleTo(panel))
    check("cli-install panel: with no runner armed it can show but not act",
          not panel.action_btn.isEnabled())
    check("cli-install panel: with no gate report it claims no check happened",
          not panel.last_check_label.isVisibleTo(panel)
          and panel.last_check_label.text() == "")
    strings = _dialog_strings(panel)
    panel.close()
    panel.deleteLater()

    # the splash is the most fleeting surface in the app: it closes itself and
    # leaves nothing behind, so this is the one place an outcome can be read
    # again afterwards
    reported = UpdatePanel(situation(winget_exe), runner=None,
                           winget_exe=winget_exe, settings_file=str(settings),
                           last_check="Claude Code: left to its own updater",
                           parent=win)
    check("cli-install panel: the startup gate's report is readable here long "
          "after the splash has gone",
          reported.last_check_label.isVisibleTo(reported)
          and "left to its own updater" in reported.last_check_label.text())
    reported.close()
    reported.deleteLater()

    blocker = threading.Event()
    released = []

    def never_returns(argv, timeout=0):
        released.append(list(argv))
        blocker.wait(10)
        return 0, ""

    native_panel = UpdatePanel(situation(native_exe), runner=never_returns,
                               winget_exe=winget_exe,
                               settings_file=str(settings), parent=win)
    check("cli-install panel: a native machine is offered pause, not another "
          "install, plus the two acts about the OLD copy",
          native_panel.action_btn.text() == "Pause automatic updates"
          and native_panel.revert_btn.isVisibleTo(native_panel)
          and native_panel.cleanup_btn.isVisibleTo(native_panel))
    # the install NEVER runs on the GUI thread: CLAUDE.md records what an
    # inline ~3.0s subprocess did to this app, and an install can run minutes
    started = time.time()
    native_panel._on_cleanup()
    check("cli-install panel: a command that never returns does not block the "
          "GUI thread, and the panel says so by disabling its actions",
          time.time() - started < 1.0 and not native_panel.action_btn.isEnabled()
          and not native_panel.cleanup_btn.isEnabled())
    native_panel.close()      # Cancel/Close is the escape hatch, never a kill
    check("cli-install panel: closing mid-command stops the poll and claims "
          "no outcome",
          not native_panel._poll.isActive()
          and not native_panel.log.toPlainText().strip().endswith("removed."))
    blocker.set()
    native_panel.deleteLater()

    locked = UpdatePanel(situation(native_exe,
                                   policies=[{"requiredMinimumVersion": "1"}]),
                         runner=None, settings_file=str(settings), parent=win)
    check("cli-install panel: a policy-locked machine is offered nothing, "
          "with the reason shown",
          not locked.action_btn.isVisibleTo(locked)
          and "managed settings" in locked.state_label.text())
    locked.close()
    locked.deleteLater()

    consent = ConsentDialog(winget_exe, win)
    check("cli-install consent: the action is unavailable until the checkbox "
          "is ticked",
          not consent.action_btn.isEnabled())
    consent.agree.setChecked(True)
    check("cli-install consent: ticking it enables the action",
          consent.action_btn.isEnabled())
    check("cli-install consent: the exact command is shown, and both levels "
          "of undo are stated BEFORE agreeing",
          any("irm https://claude.ai/install.ps1 | iex" in s
              for s in _dialog_strings(consent))
          and any("Full revert" in s for s in _dialog_strings(consent))
          and any("rollback" in s for s in _dialog_strings(consent)))
    strings += _dialog_strings(consent)
    check("cli-install: no em dash in any panel or modal string",
          not any("\u2014" in s for s in strings),
          [s for s in strings if "\u2014" in s])
    consent.close()
    consent.deleteLater()
    check("cli-install: the panel writes nothing to session.json",
          "auto_update" in win._session_payload()["ui"]
          and not any(k.startswith("install") or k.startswith("cli_")
                      for k in win._session_payload()["ui"]),
          list(win._session_payload()["ui"]))
    win.close()
    app.processEvents()

    # --- 12. the factory the suite shares does none of this -----------------
    import inspect
    import main as main_module
    src = inspect.getsource(main_module.create_main_window)
    check("cli-install: create_main_window contains no install work at all",
          "cli_install" not in src and "arm_cli_install" not in src)
    check("cli-install: ...and the real runner is armed from main.py alone",
          "arm_cli_install" in inspect.getsource(main_module.main))


def _dialog_strings(widget) -> list:
    """Every user-visible string a dialog composes at runtime."""
    out = []
    for child in widget.findChildren(object):
        for attr in ("text", "toolTip"):
            fn = getattr(child, attr, None)
            if callable(fn):
                try:
                    out.append(str(fn()))
                except TypeError:
                    pass
    return out


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




if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print(f"\nRESULT: {PASS} passed, {FAIL + 1} failed (crash)", flush=True)
        sys.exit(1)
