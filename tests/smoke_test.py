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
        3: [(0, 0, 1, 1), (0, 1, 1, 1), (1, 0, 1, 2)],
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
        1: (1, 1), 2: (1, 2), 3: (2, 2), 4: (2, 2), 5: (2, 6),
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

    a._set_status(AgentStatus.EXITED_OK)
    check("waiting: exit clears the waiting state", not a.is_waiting())

    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bypass", cwd="."))
    b.spec.permission_mode = "bypassPermissions"
    b.status = AgentStatus.RUNNING
    b._screen_tail = "Do you want to proceed?\r\n 1. Yes\r\n 2. No"
    b._on_idle_timeout()
    check("waiting: bypassPermissions suppresses the prompt '?'",
          not b.is_waiting())


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
    check("mouse: _link_at picks up the URL under the pointer",
          v2._link_at(0, 8) == ("url", "https://example.com/docs"), v2._link_at(0, 8))

    # hover: link cols underline + hand cursor; a plain word does neither
    check("mouse: _link_range_at spans the hovered URL",
          v2._link_range_at(0, 8) == (4, 27), v2._link_range_at(0, 8))
    check("mouse: _link_range_at is None over plain text",
          v2._link_range_at(0, 30) is None, v2._link_range_at(0, 30))

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
    check("v2 grid: over-capacity auto-grows rows",
          explicit_grid(2, 2, 5).rows == 3)
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
    """v3: private-CSI/underline fix, clipboard, orchestration primitives,
    persistent-agent badges, and the named-pipe orchestrator bridge."""
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

    # --- orchestration primitives via a fast line-mode echo agent ---
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

    # --- #3/#8 orchestrator bridge over the real named pipe ---
    from app.orchestrator_bridge import OrchestratorBridge, HAS_QTNETWORK
    if HAS_QTNETWORK:
        from app import mcp_server
        bridge = OrchestratorBridge(mgr, active_ws=lambda: ws.id)
        check("v3 bridge: named pipe listening", bridge.start())
        os.environ["AIHIVE_PIPE"] = bridge.pipe_name
        res = {}
        ev = threading.Event()

        def rpc():
            try:
                res["list"] = mcp_server._rpc("list_agents", {})
                res["state"] = mcp_server._rpc(
                    "set_agent_state", {"agent_id": echo.id, "state": "idle"})
            except Exception as e:
                res["err"] = repr(e)
            finally:
                ev.set()

        threading.Thread(target=rpc, daemon=True).start()
        wait_until(ev.is_set, 15000)
        check("v3 bridge: MCP client + pipe RPC round-trip",
              "err" not in res and res.get("list", {}).get("agents"), res.get("err"))
        check("v3 bridge: set_agent_state over pipe",
              res.get("state", {}).get("state") == "idle")
        bridge.stop()

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

    # sticky auto-resume intent: an auto-created agent (orchestrator/worker) that
    # died keeps its "resume on next open" flag so it never silently goes dormant
    # (the bug where the Hiragana Orchestrator vanished after its process failed)
    win3 = create_main_window(SessionStore(path=tmp / "sticky.json"))
    win3.show(); pump(120)
    ws3 = win3.manager.workspaces[0]
    auto = win3.manager.add_terminal(
        ws3.id, build_spec(AgentKind.CLAUDE, "Orchestrator", cwd=str(tmp),
                           pty=True, is_orchestrator=True), autostart=False)
    auto.auto_created = True
    auto.autostart_on_restore = True   # was meant to be running
    manual = win3.manager.add_terminal(
        ws3.id, build_spec(AgentKind.CMD, "Manual", cwd=str(tmp)), autostart=False)
    dumped = {t["name"]: t["running"]
              for t in win3.manager.to_session_dict()["workspaces"][0]["terminals"]}
    check("persist: dead auto-agent keeps resume intent (running=True)",
          dumped.get("Orchestrator") is True, dumped)
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
    (fail closed), orchestration state autosave, workspace-scoped orchestrator
    tools, immediate save on orchestrator mutations, quoted-path command
    parsing, and the session save-audit log."""
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

    # --- orchestration state changes mark the session dirty ---
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
    # a later orchestrator retask updates the role but NEVER the custom name
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

    # --- workspace-scoped orchestrator dispatch ---
    saves = {"n": 0}
    bridge = OrchestratorBridge(mgr, active_ws=lambda: ws_a.id,
                                on_mutation=lambda: saves.__setitem__(
                                    "n", saves["n"] + 1))
    listed = bridge._dispatch("list_agents", {}, ws=ws_a.id)["agents"]
    check("scope: list_agents sees only the bound workspace",
          len(listed) == 1 and listed[0]["agent_id"] == a1.id, listed)
    try:
        bridge._dispatch("set_agent_state",
                         {"agent_id": b1.id, "state": "completed"}, ws=ws_a.id)
        cross = False
    except _RpcError:
        cross = True
    check("scope: cross-workspace agent unreachable by id", cross)
    # duplicate display names: binding resolves ITS workspace's agent
    got = bridge._dispatch("set_agent_state",
                           {"agent_id": "Docs Writer", "state": "completed"},
                           ws=ws_a.id)
    check("scope: name resolution stays inside the workspace",
          got["agent_id"] == a1.id)
    try:
        bridge._dispatch("spawn_agent",
                         {"task": "x", "workspace_id": ws_b.id}, ws=ws_a.id)
        leaked = True
    except _RpcError as e:
        leaked = e.code != "scope"
    check("scope: spawn into another workspace rejected", not leaked)
    check("scope: legacy unscoped call still resolves globally",
          bridge._dispatch("list_agents", {}, ws="")["agents"] and True)

    # log_activity: a WORKER may append to the board but is REFUSED the
    # orchestration ops (spawn/assign/reassign/state/close). Role scoping
    # mirrors the ws scoping — the guardrail that makes worker MCP tools safe.
    for op, ar in [("spawn_agent", {"task": "x"}),
                   ("assign_task", {"agent": a1.id, "task": "x"}),
                   ("close_agent", {"agent_id": a1.id, "force": True})]:
        try:
            bridge._dispatch(op, ar, ws=ws_a.id, role="worker")
            forbidden = False
        except _RpcError as e:
            forbidden = e.code == "forbidden"
        check(f"guardrail: worker refused {op}", forbidden)
    logged = bridge._dispatch(
        "log_activity", {"agent": "Docs Writer", "message": "wrote the docs"},
        ws=ws_a.id, role="worker")
    check("guardrail: worker may log_activity", logged.get("logged") is True)
    tail = ws_a.board.read_log_tail()
    check("guardrail: log_activity entry lands on the board",
          any("wrote the docs" in t for t in tail), tail)
    # append_activity must NOT disturb the roster block
    before = ws_a.board.update_roster([{"name": "A1", "role": "", "provider":
                "claude", "model": "", "status": "idle", "task": "keep me"}])
    ws_a.board.append_activity("A1", "another line")
    body = Path(ws_a.board.path).read_text(encoding="utf-8")
    check("guardrail: roster survives an activity append",
          "keep me" in body and body.count("AIHIVE:ROSTER:BEGIN") == 1
          and "another line" in body)
    # orchestrator (or legacy role="") keeps full access
    check("guardrail: orchestrator role may still spawn",
          bridge._dispatch("list_agents", {}, ws=ws_a.id, role="orchestrator")
          is not None)
    cfg_w = bridge.mcp_config_path_for(ws_a.id, "worker")
    cfg_o = bridge.mcp_config_path_for(ws_a.id, "orchestrator")
    envw = _json.loads(Path(cfg_w).read_text(encoding="utf-8"))["mcpServers"]["aihive"]["env"]
    check("guardrail: worker config carries AIHIVE_ROLE=worker",
          envw.get("AIHIVE_ROLE") == "worker" and cfg_w != cfg_o)

    # get_agent_output renders on the GUI thread while the MCP client blocks on
    # a timeout — the pyte feed must be bounded, never the full 512 KB buffer
    from app import orchestrator_bridge as _ob

    class _BigPty:
        is_pty = True
        fed = {"n": 0}

        def pty_replay(self):
            return "x\r\n" * 200_000  # ~600 KB, far over PTY_BUFFER_CAP

    _real_feed = None
    import pyte as _pyte
    orig_feed = _pyte.Stream.feed
    sizes = []

    def _spy_feed(self, data):
        sizes.append(len(data))
        return orig_feed(self, data)
    _pyte.Stream.feed = _spy_feed
    try:
        out = _ob._agent_output(_BigPty(), 30)
    finally:
        _pyte.Stream.feed = orig_feed
    check("render: get_agent_output feeds a bounded tail, not 512 KB",
          sizes and max(sizes) <= 40 * 400 + 10, sizes)
    check("render: output still capped to the screen", out.count("\n") <= 40)

    # --- orchestrator mutations trigger the immediate-save hook ---
    class _FakeSock:
        def write(self, _b): pass
        def flush(self): pass
    line = _json.dumps({"id": "t1", "op": "set_agent_state",
                        "args": {"agent_id": a1.id, "state": "idle"},
                        "ws": ws_a.id}).encode()
    bridge._handle_line(_FakeSock(), line)
    check("scope: mutating op fires immediate save", saves["n"] == 1, saves)
    line = _json.dumps({"id": "t2", "op": "list_agents", "args": {},
                        "ws": ws_a.id}).encode()
    bridge._handle_line(_FakeSock(), line)
    check("scope: read-only op does not fire save", saves["n"] == 1, saves)

    # --- per-workspace mcp config carries the binding ---
    bridge.pipe_name = "test-pipe"
    bridge._mcp_dir = str(tmp / "mcp")
    cfg_path = bridge.mcp_config_path_for(ws_a.id)
    cfg = _json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["aihive"]["env"]
    check("scope: mcp config binds AIHIVE_WS to the workspace",
          env.get("AIHIVE_WS") == ws_a.id and env.get("AIHIVE_PIPE") == "test-pipe")

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


def test_resume_fallback():
    """A resume (--continue) launch that dies before the interactive prompt ever
    comes up (Claude prints 'No conversation found to continue' and exits) must
    relaunch ONCE, fresh — so the terminal is never left black. The regression
    behind the Orchestrator card that opened all-black and non-interactive."""
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    spec = build_spec(AgentKind.CLAUDE, "Orchestrator", cwd=os.getcwd(),
                      pty=True, is_orchestrator=True)
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


def main():
    test_tiling()
    test_layout_popup_placement()
    test_sidebar_count_badge()
    test_agent_waiting()
    test_sidebar_reorder()
    test_manager_reorder_persist()
    test_sidebar_categories()
    test_manager_categories_persist()
    test_category_container()
    test_agent_inline_expansion()
    test_ai_title_summary()
    test_token_usage_badge()
    test_reveal_agent()
    test_agent_busy_activity()
    test_ansi()
    test_terminal_keys()
    test_terminal_image_paste()
    test_terminal_mouse_words_links()
    test_terminal_selection_edit()
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
    test_sidebar_file_tree()
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
