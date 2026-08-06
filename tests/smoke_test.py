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
    when empty. A right-edge spinner mirrors the working count (hidden at 0).
    Folder/delete show ONLY on hover, never merely because the row is active."""
    from PySide6.QtCore import QAbstractAnimation
    from PySide6.QtWidgets import QApplication
    from app.widgets.ornaments import AgentCountBadge
    from app.ui_theme import Palette
    from app.widgets.sidebar import WorkspaceRow

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

    # folder/delete are hover-only: activating the row must NOT reveal them.
    # Use isHidden() (explicit show/hide intent) not isVisible() — the row has
    # no shown ancestor here, so isVisible() would be False either way.
    row.set_active(True)
    check("row: active row does not show folder/delete (hover-only)",
          row.folder_btn.isHidden() and row.delete_btn.isHidden(),
          (row.folder_btn.isHidden(), row.delete_btn.isHidden()))
    row._update_hover_buttons(hovered=True)
    check("row: hover reveals folder/delete",
          not row.folder_btn.isHidden() and not row.delete_btn.isHidden())

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


def test_chime_persistence():
    """The top-bar chime toggle flips its glyph + emits soundToggled, and the
    on/off preference round-trips through the session ui state."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import TopBar

    QApplication.instance() or QApplication([])
    bar = TopBar()
    check("chime toggle: defaults to ON (bell glyph)",
          bar._sound_on and bar.sound_btn.text() == "\U0001F514")
    emitted = []
    bar.soundToggled.connect(emitted.append)
    bar.sound_btn.click()
    check("chime toggle: click mutes + emits False + shows muted glyph",
          emitted == [False] and not bar._sound_on
          and bar.sound_btn.text() == "\U0001F515", (emitted, bar._sound_on))
    bar.sound_btn.click()
    check("chime toggle: click again re-enables + emits True",
          emitted == [False, True] and bar._sound_on, emitted)
    # set_sound_enabled reflects state WITHOUT re-emitting (restore path)
    bar.set_sound_enabled(False)
    check("chime toggle: set_sound_enabled updates glyph, no emit",
          not bar._sound_on and emitted == [False, True])
    bar.deleteLater()


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
    """The card header says which model and effort the agent is ACTUALLY on.
    Both change mid-session (/model, /effort in the terminal), so the reading
    comes from the transcript: assistant records carry message.model + a
    top-level effort, and a /model or /effort pick writes a local-command-stdout
    record the instant the user chooses. Last in file order wins. Transient like
    the AI title: it never touches spec and never saves."""
    import json as _json
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
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
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high"),
          transcripts._read_model_effort(str(tpath)))

    # a /model pick AFTER the last turn is the newer truth (the whole point:
    # an idle agent's switch must show without waiting for another turn)
    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions")])
    check("model: a /model pick after the last turn wins",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "high"),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _pick("Set effort level to max (this session only): Maximum "
                 "capability with deepest reasoning.")])
    check("model: a /effort pick changes only the effort",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "max"),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mOpus 4.8 (1M context)\x1b[22m and saved "
                 "as your default for new sessions with \x1b[1mhigh\x1b[22m "
                 "effort")])
    check("model: a pick that names an effort applies both",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8 (1M)", "high"),
          transcripts._read_model_effort(str(tpath)))

    # a turn AFTER a pick wins again (ordering is file order, not kind)
    write([_pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _turn(model="claude-fable-5", effort="max")])
    check("model: a turn after a pick wins again",
          transcripts._read_model_effort(str(tpath)) == ("Fable 5", "max"),
          transcripts._read_model_effort(str(tpath)))

    # a sub-agent's model is a different context; <synthetic> is not a model
    write([_turn(), _turn(model="claude-haiku-4-5", effort="low", side=True),
           {"type": "assistant", "message": {"model": "<synthetic>"}}])
    check("model: sidechain and synthetic records never win",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high"),
          transcripts._read_model_effort(str(tpath)))

    check("model: missing file reads as unknown",
          transcripts.latest_model_effort(str(tmp), "nope") == ("", ""))

    # the reader only touches the tail, so it must still find evidence that sits
    # behind a long stretch of unrelated records (full-scan fallback)
    filler = [{"type": "user", "text": "x" * 400} for _ in range(400)]
    write([_turn(model="claude-opus-4-8", effort="xhigh")] + filler)
    check("model: falls back to a full scan when the tail has no evidence",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8", "xhigh"),
          transcripts._read_model_effort(str(tpath)))

    # --- agent-side badge: transient, emits only on a real change ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd=".",
                                 model="opus", effort="high"))
    seen = []
    a.model_changed.connect(seen.append)
    check("model: badge seeded from the launch flags",
          a.model_badge() == "Opus · high", a.model_badge())
    a.set_live_model("Sonnet 5", "max")
    check("model: badge follows the live reading",
          a.model_badge() == "Sonnet 5 · max" and seen[-1] == "Sonnet 5 · max",
          (a.model_badge(), seen))
    n = len(seen)
    a.set_live_model("Sonnet 5", "max")
    check("model: no signal when the reading is unchanged", len(seen) == n)
    a.set_live_model("", "")
    check("model: an empty reading never blanks a good label",
          a.model_badge() == "Sonnet 5 · max" and len(seen) == n)
    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bare", cwd="", model="",
                                 effort=""))
    b._live_model = ""      # no launch flag and no saved user default
    check("model: badge hidden when nothing is known", b.model_badge() == "")
    b.set_live_model("Opus 5", "")
    check("model: model alone renders without an effort",
          b.model_badge() == "Opus 5", b.model_badge())

    # --- header: the chip shows/hides with the badge ---
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(80)
    check("model: card chip shows the live model and effort",
          card.model_label.isVisible()
          and card.model_label.text() == "Sonnet 5 · max",
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
          live.model_badge() == "Sonnet 5 · low", live.model_badge())
    check("model: a reading never marks the session dirty", not dirtied, dirtied)
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


# ------------------------------------------------------------ ansi parser ---

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
    check("ws: breadcrumb shows active workspace",
          "Alpha" in win.top_bar.breadcrumb.text())

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

    # -- 11. restore round-trip: nothing auto-starts, saved run state kept --
    win2 = create_main_window(store)
    win2.resize(1600, 900)
    win2.show()
    pump(150)
    mgr2 = win2.manager
    alpha2 = next(w for w in mgr2.workspaces if w.name == "Alpha")
    running_flags = [a.autostart_on_restore for a in alpha2.agents]
    check("restore: run state loaded per agent",
          any(running_flags) and not all(running_flags), running_flags)
    pump(500)
    check("restore: no agent auto-starts on launch (manual wake only)",
          all(not a.is_running() for a in alpha2.agents))
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

    from app.process_worker import AgentKind, build_spec
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

    agent.start()
    check("pty: agent reaches running", wait_until(agent.is_running, 15000))
    check("pty: interactive prompt renders in the grid",
          wait_until(lambda: "PS" in card.terminal.screen_text()
                     and ">" in card.terminal.screen_text(), 20000),
          card.terminal.screen_text()[-160:])

    agent.write("echo ('grid'+'ok')\r")
    check("pty: typed command output appears in the grid",
          wait_until(lambda: "gridok" in
                     card.terminal.screen_text().replace(" ", ""), 12000))

    # Ctrl+C interrupts a running loop (real signal, not available in line mode)
    agent.write("ping -t 127.0.0.1\r")
    wait_until(lambda: "127.0.0.1" in card.terminal.screen_text(), 15000)
    pump(500)
    agent.write("\x03")
    a = card.terminal.screen_text()
    pump(2500)
    b = card.terminal.screen_text()
    check("pty: Ctrl+C stopped the loop",
          a.count("Reply") == b.count("Reply") or "PS" in b[-80:], b[-160:])

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
    _, gargs = providers.build_invocation("gemini", model="Gemini 3.1 Pro (High)")
    check("v2 providers: multiword gemini model stays ONE argument",
          gargs == ["--model", "Gemini 3.1 Pro (High)"], gargs)
    check("v2 providers: gemini detection returns bool (env-independent)",
          isinstance(providers.detected("gemini"), bool))
    gspec = build_spec(AgentKind.GEMINI, "G", cwd=str(proj_a),
                       model="Gemini 3.5 Flash (Low)")
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
    check("v2 grid: 3x1 and 1x3 both offered",
          {"3x1", "1x3"} <= offered, offered)

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
    agent2.start()  # nothing auto-starts restored agents anymore; wake it
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
    """Reopening the app auto-starts NOTHING, in any workspace — a restored
    agent (however it was left) waits for a manual wake, except the separate
    plan-limit recovery path tested elsewhere. Every restored Claude agent
    still carries one-shot resume for whenever it next starts, and a stopped
    pty card is never a dead black screen — it shows a wake banner and starts
    on the first keystroke."""
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
    pump(300)
    check("wake: nothing auto-starts on launch, in ANY workspace",
          not bg.is_running() and not stopped.is_running()
          and not coder.is_running())
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
    app.processEvents()
    badge = win.top_bar.usage_badge

    check("plan-usage: polling is opt-in, so the suite never fetches",
          not win._usage_timer.isActive() and win.plan_usage() is None)
    check("plan-usage: badge hidden until a reading arrives",
          not badge.isVisible() and not badge.has_reading())

    good = cu.Usage(limits=(limit("five_hour", 21.0, now + 4800),),
                    fetched_at=now, plan="pro")
    win._on_usage_ready(good)
    app.processEvents()
    check("plan-usage: reading shows the badge with the full line",
          badge.isVisible() and badge._text.startswith("21% used, resets in"))
    check("plan-usage: tooltip carries plan, every window, and the age",
          "Pro plan" in badge.toolTip() and "Current session" in badge.toolTip()
          and "Updated" in badge.toolTip())
    check("plan-usage: plan_usage() exposes the reading",
          win.plan_usage() is good)

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
          badge._text.startswith("limit reached, resets in"))
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
          badge._text.startswith("21% used") and badge._stale)

    # visibility preference persists; toggling it IS a save (a UI preference)
    win._on_usage_visibility(False)
    app.processEvents()
    check("plan-usage: hiding removes the badge but keeps polling",
          not badge.isVisible())
    check("plan-usage: a later reading cannot resurrect a hidden badge",
          (win._on_usage_ready(good), app.processEvents(),
           not badge.isVisible())[-1])
    payload_ui = win._session_payload()["ui"]
    check("plan-usage: preference persisted under ui.usage_visible",
          payload_ui["usage_visible"] is False)
    win.close()

    win2 = create_main_window(store)
    win2.show()
    app.processEvents()
    check("plan-usage: preference restored on reopen",
          win2.top_bar.usage_visible() is False)
    check("plan-usage: default is ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")).top_bar.usage_visible())
    win2.close()

    # no Claude login at all: hide for good rather than show an empty pill
    win3 = create_main_window(SessionStore(path=tmp / "noauth.json"))
    win3.show()
    win3._usage_timer.start()
    win3._on_usage_ready(cu.Usage(error="no-auth"))
    app.processEvents()
    check("plan-usage: no-auth hides the badge and stops polling",
          not win3.top_bar.usage_badge.isVisible()
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
          and b4._text.startswith("21% used") and not b4._unreadable)
    # an error is not a reason to force the readout back onto a bar the user
    # deliberately cleared
    win4._on_usage_visibility(False)
    win4.top_bar.note_usage_error("http 429")
    app.processEvents()
    check("plan-usage: a hidden readout stays hidden when a poll fails",
          not b4.isVisible())
    win4.close()


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


def main():
    test_tiling()
    test_layout_popup_placement()
    test_sidebar_count_badge()
    test_agent_waiting()
    test_notification_chime()
    test_limit_blocked_workspace_stats()
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
    test_no_em_dashes_in_visible_text()
    test_reveal_agent()
    test_agent_busy_activity()
    test_ansi()
    test_terminal_keys()
    test_terminal_image_paste()
    test_terminal_mouse_words_links()
    test_terminal_mouse_tracking_click()
    test_terminal_selection_edit()
    test_terminal_input_editor()
    test_session_migration()
    test_app()
    test_pty()
    test_v2_features()
    test_v2_review_fixes()
    test_v3_features()
    test_persistence_resume()
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
    test_agent_file_map()
    test_fsopen_helpers()
    test_filetypes_icons()
    test_terminal_relative_link()
    test_terminal_block_glyphs()
    test_terminal_link_underline()
    test_sidebar_file_tree()
    test_sidebar_search()
    test_plan_usage()
    test_limit_ledger()
    test_auto_continue_on_limit_reset()
    test_startup_limit_recovery()
    test_lifecycle_e2e()  # slowest last: launches a real claude once
    print(f"\nRESULT: {PASS} passed, {FAIL} failed", flush=True)
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        print(f"\nRESULT: {PASS} passed, {FAIL + 1} failed (crash)", flush=True)
        sys.exit(1)
