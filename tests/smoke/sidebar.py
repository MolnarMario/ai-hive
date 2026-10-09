"""Sidebar, workspace page and agent-card layout: tiling, badges,
reordering, categories, the file tree and search."""

import os
import shutil
import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check


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
    # the Agent / File Map hubs use the sidebar's colours (they were swapped)
    from app.widgets import agent_file_map as afm
    check("map: hub idle/working colours match the sidebar badge",
          all(afm._STATE_COLOR[s]() == AgentCountBadge._STATE_COLOR[s]()
              for s in ("idle", "working")),
          {s: afm._STATE_COLOR[s]() for s in ("idle", "working")})

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


def test_row_name_fades_under_badges():
    """The badge stack is painted OVER the name (it owns no layout width, so
    it can never be squeezed), which used to leave a long workspace name
    printing its letters through the icons - reported live as a gear and a
    spinner sitting inside "Video Production", unreadable either way.

    The name now fades out just before the badges: no elision, no reserved
    width, and nothing at all changes on a row with no badges lit."""
    from PySide6.QtGui import QImage, QPainter, QColor
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication
    from app.widgets.ornaments import FadingLabel
    from app.widgets.sidebar import WorkspaceRow, SIDEBAR_WIDTH, ROW_HEIGHT

    QApplication.instance() or QApplication([])
    row = WorkspaceRow("w1", "Video Production", "C:/proj")
    row.setGeometry(0, 0, SIDEBAR_WIDTH, ROW_HEIGHT)

    quiet = {"total": 2, "active": 2, "busy": 0, "error": 0, "waiting": 0,
             "idle": 2, "limit_blocked": 0, "scheduled": 0, "bg_shell": 0}
    row.set_stats(quiet)
    row.layout().activate()
    row._position_icon_stack()
    check("row name: nothing lit -> no fade at all (plain label, full name)",
          row.name_label.fade_x() is None, row.name_label.fade_x())

    lit = dict(quiet, busy=1, bg_shell=1)
    row.set_stats(lit)
    row.layout().activate()
    row._icon_stack.layout().activate()
    row._position_icon_stack()
    want = max(0, row._icon_stack.x() - row.name_label.x()
               - WorkspaceRow._ICON_TEXT_GAP)
    check("row name: badges lit -> fade starts just before the badge stack",
          row.name_label.fade_x() == want,
          (row.name_label.fade_x(), want, row._icon_stack.x(),
           row.name_label.x()))
    check("row name: the name itself is never truncated to fit the badges",
          row.name_label.text() == "Video Production", row.name_label.text())

    # ...and the fade is real ink, not just bookkeeping: paint the label at a
    # width its text overflows and measure how far right the ink reaches.
    def ink_extent(fade_x):
        lab = FadingLabel("Video Production Workspace Folder")
        lab.setStyleSheet("color: rgb(240,240,240); background: transparent;")
        lab.resize(240, 30)
        lab.set_fade_x(fade_x)
        img = QImage(240, 30, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        p = QPainter(img)
        lab.render(p, QPoint())
        p.end()
        cols = [x for x in range(240)
                if max(img.pixelColor(x, y).alpha() for y in range(30)) > 30]
        lab.deleteLater()
        return cols[-1] if cols else -1

    # the cover point is taken from the measured ink, never a pixel constant:
    # how long the text paints depends on the installed fonts
    full = ink_extent(None)
    cover = full // 2
    faded = ink_extent(cover)
    check("row name: with no fade the text paints (some ink at all)",
          full > 40, full)
    check("row name: with a fade the ink stops at the badge edge",
          0 < faded <= cover, (faded, cover, full))
    check("row name: a cover at the label's own edge leaves no ink at all",
          ink_extent(0) == -1, ink_extent(0))
    row.deleteLater()


def test_working_pulses_share_one_phase():
    """The count badges and the collapsed rail each run their own frame clock,
    but the breath they paint comes from one shared clock. Two pulses started
    0.4 s apart (one workspace starts working later than another) must still
    be at the same point of the breath."""
    from PySide6.QtCore import QEventLoop, QObject, QTimer
    from PySide6.QtWidgets import QApplication

    from app.widgets import ornaments

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    period = ornaments.PULSE_PERIOD_S
    check("pulse: the breath runs 0 -> 1 -> 0 over one period",
          abs(ornaments.pulse_phase(0.0)) < 1e-9
          and abs(ornaments.pulse_phase(period / 2) - 1.0) < 1e-9
          and abs(ornaments.pulse_phase(period)) < 1e-9)
    owner = QObject()
    got = {"a": [], "b": []}
    a = ornaments.working_pulse(owner, lambda v: got["a"].append(v))
    a.start()
    pump(400)
    b = ornaments.working_pulse(owner, lambda v: got["b"].append(v))
    b.start()
    pump(150)
    a.stop(); b.stop()
    now = ornaments.pulse_phase()
    check("pulse: a pulse started later is in step with an earlier one",
          got["a"] and got["b"] and abs(got["a"][-1] - got["b"][-1]) < 0.15
          and abs(got["b"][-1] - now) < 0.25,
          (got["a"][-1:], got["b"][-1:], now))
    owner.deleteLater()


def test_collapsed_sidebar_rail():
    """A collapsed sidebar leaves a thin rail of per-workspace strips in
    sidebar order, coloured like each row's count badge. A click opens that
    workspace, and the rail shows exactly while the sidebar is at width 0,
    including after a restart with the sidebar saved collapsed."""
    from PySide6.QtCore import QAbstractAnimation, QEventLoop, QPoint, Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from app.session_store import SessionStore
    from app.widgets.sidebar import Sidebar, WorkspaceRail, ws_state
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    sb = Sidebar()
    rail = WorkspaceRail()
    rail.resize(WorkspaceRail.WIDTH, 400)
    sb.add_row("a", "Alpha", "")
    sb.add_row("b", "Bravo", "")
    sb.add_row("c", "Charlie", "")
    sb.set_rail(rail)
    sb.set_active_row("b")
    sb.set_stats("a", {"total": 2, "active": 2, "busy": 1, "error": 0})
    sb.set_stats("b", {"total": 1, "active": 1, "busy": 0, "error": 0,
                       "waiting": 1})
    sb.set_stats("c", {"total": 1, "active": 0, "busy": 0, "error": 1})

    check("rail: one entry per workspace, in sidebar order",
          [e[0] for e in rail._entries] == ["a", "b", "c"], rail._entries)
    check("rail: strips share the count badge's states",
          [ws_state(e[2]) for e in rail._entries]
          == ["working", "idle", "error"],
          [ws_state(e[2]) for e in rail._entries])
    check("rail: the active workspace's strip is the wide one",
          rail.strip_rect(1).width() > rail.strip_rect(0).width()
          and rail.strip_rect(0).width() == rail.strip_rect(2).width(),
          [rail.strip_rect(i).width() for i in range(3)])
    check("rail: tooltip names the workspace and who is waiting",
          rail.tooltip_for(1).startswith("Bravo\n")
          and "1 waiting for you" in rail.tooltip_for(1), rail.tooltip_for(1))
    check("rail: no pulse while the rail is hidden",
          rail._anim.state() != QAbstractAnimation.State.Running)

    picked = []
    rail.workspaceSelected.connect(picked.append)
    mid = rail.strip_rect(2).center()
    QTest.mouseClick(rail, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(mid.x(), mid.y()))
    QTest.mouseClick(rail, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                     QPoint(5, 395))
    check("rail: a strip click selects its workspace, empty rail does not",
          picked == ["c"], picked)

    sb.apply_layout([{"type": "category", "id": "k", "name": "K",
                      "collapsed": True, "children": ["c"]},
                     {"type": "workspace", "id": "a"},
                     {"type": "workspace", "id": "b"}])
    sb.set_row_name("a", "Alpha 2")
    check("rail: follows reorders (collapsed categories included) and renames",
          [(e[0], e[1]) for e in rail._entries]
          == [("c", "Charlie"), ("a", "Alpha 2"), ("b", "Bravo")],
          rail._entries)
    rail.resize(WorkspaceRail.WIDTH, 40)
    check("rail: a short rail squeezes strips so every workspace keeps one",
          rail.strip_rect(2).bottom() < 40 and rail.strip_rect(2).height() >= 3,
          rail.strip_rect(2))
    sb.remove_row("c")
    check("rail: a removed workspace loses its strip",
          [e[0] for e in rail._entries] == ["a", "b"], rail._entries)

    # --- in the window: visibility follows the collapse, clicks switch ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-rail-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.resize(1200, 700)
    win.show()
    pump(100)
    mgr = win.manager
    wa = mgr.workspaces[0]
    wb = mgr.create_workspace("Bravo")
    mgr.set_active(wa.id)
    check("rail: hidden while the sidebar is open", win.ws_rail.isHidden())
    win._toggle_sidebar()
    pump(50)
    check("rail: shown once the sidebar collapses",
          win.body_split.sizes()[0] == 0 and win.ws_rail.isVisible(),
          win.body_split.sizes())
    i = [e[0] for e in win.ws_rail._entries].index(wb.id)
    mid = win.ws_rail.strip_rect(i).center()
    QTest.mouseClick(win.ws_rail, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier, QPoint(mid.x(), mid.y()))
    check("rail: clicking a strip opens that workspace, sidebar stays shut",
          mgr.active_id == wb.id and win.body_split.sizes()[0] == 0,
          (mgr.active_id, win.body_split.sizes()))
    win._save_now()
    win.close()
    pump(100)

    win2 = create_main_window(store)
    check("rail: a sidebar saved collapsed restores with the rail showing",
          not win2.ws_rail.isHidden() and win2.body_split.sizes()[0] == 0,
          win2.body_split.sizes())
    win2._toggle_sidebar()
    check("rail: reopening the sidebar hides the rail",
          win2.ws_rail.isHidden(), win2.body_split.sizes())
    win2.close()
    pump(100)
    shutil.rmtree(tmp, ignore_errors=True)


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
    """The workspace header bar owns Open folder / Change folder / Delete (not
    duplicated as sidebar hover buttons any more) — delete sits LEFT of open,
    matching the sidebar's old left-to-right order — and the path shows the
    FULL text whenever it fits, eliding only the overflow the header's own
    buttons would otherwise be squeezed by."""
    from PySide6.QtGui import QFontMetrics
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

    # one gap between every header button: a spacer item between Change and
    # Layout once made that gap 20px against the 6px everywhere else. The
    # lanes box is shown so its two gaps are measured too.
    page.set_lanes_state(False, available=True)
    page.resize(1400, 600)
    page.show()
    app.processEvents()
    row = order[order.index(page.delete_btn):]
    gaps = [b.geometry().left() - a.geometry().right() - 1
            for a, b in zip(row, row[1:])]
    spacing = page._header_lay.spacing()
    check("workspace header: every button from Delete to Activity sits one "
          "layout spacing from the next",
          len(row) == 8 and all(w.isVisible() for w in row)
          and gaps == [spacing] * (len(row) - 1),
          (spacing, gaps, [w.isVisible() for w in row]))
    page.set_lanes_state(False, available=False)
    app.processEvents()
    check("workspace header: the path gets exactly the width the row leaves "
          "it when the lanes box is hidden",
          page.path_label.width() == page._path_budget(),
          (page.path_label.width(), page._path_budget()))
    page.hide()

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
    shown = QFontMetrics(page.path_label.font()).horizontalAdvance(
        page.path_label.text())
    check("workspace header: the elided path fits the label it sits in",
          shown <= page.path_label.width(),
          (shown, page.path_label.width()))

    # growing back out restores the full path (not stuck at the smaller
    # elision like the old self-referential width computation)
    page.resize(1600, 200)
    app.processEvents()
    check("workspace header: the full path comes back once there is room "
          "again",
          page.path_label.text() == long_path, page.path_label.text())
    page.deleteLater()


def test_workspace_header_trash_and_repo_seam():
    """The folder and delete button left of Open repo are painted line icons
    at the same weight as Layout/Map (the 🗀/🗑 font glyphs drew too thin),
    the trash still turns red on hover, the path matches the buttons' text,
    and hovering Open
    repo lights the shared edge on the dropdown half, or its hover box had
    no right side."""
    from PySide6.QtCore import QCoreApplication, QEvent, Qt
    from PySide6.QtWidgets import QApplication
    from app.widgets.workspace_page import WorkspacePage
    from app.workspace_manager import Workspace
    from main import setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    page = WorkspacePage(Workspace(id="w1", name="WS",
                                   project_path="C:/proj/ai-hive"))
    page.resize(1300, 200)
    page.show()
    app.processEvents()

    from app.widgets.header_icons import IconLabel, IconToolButton
    trash = page.delete_btn
    check("header trash: a painted line icon like Layout/Map, no glyph text",
          isinstance(trash, IconToolButton)
          and trash._icon_name == "trash" and trash.text() == "",
          (type(trash).__name__, trash.text()))

    def reddish(widget):
        img = widget.grab().toImage()
        return sum(1 for x in range(img.width()) for y in range(img.height())
                   if (c := img.pixelColor(x, y)).red() > c.green() + 60
                   and c.red() > c.blue() + 60)

    rest_red = reddish(trash)
    trash.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    QCoreApplication.sendEvent(trash, QEvent(QEvent.Type.Enter))
    app.processEvents()
    hover_red = reddish(trash)
    trash.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    QCoreApplication.sendEvent(trash, QEvent(QEvent.Type.Leave))
    app.processEvents()
    check("header trash: hovering turns the painted icon red",
          rest_red == 0 and hover_red > 10, (rest_red, hover_red))

    folder = page.header.findChildren(IconLabel)
    check("header path: the folder is the painted line icon",
          any(w._icon_name == "folder" for w in folder),
          [w._icon_name for w in folder])
    path, repo = page.path_label, page.repo_btn
    check("header path: same font size and ink as the buttons",
          path.font().pointSizeF() == repo.font().pointSizeF()
          and path.font().pixelSize() == repo.font().pixelSize()
          and path.palette().windowText().color()
          == repo.palette().buttonText().color(),
          (path.font().pixelSize(), repo.font().pixelSize(),
           path.palette().windowText().color().name(),
           repo.palette().buttonText().color().name()))
    check("header trash: no taller than the Open repo button beside it",
          trash.height() <= page.repo_btn.height() + 1,
          (trash.height(), page.repo_btn.height()))

    repo, drop = page.repo_btn, page.repo_activity_btn
    seam = drop.mapTo(page, drop.rect().topLeft())
    top = repo.mapTo(page, repo.rect().topLeft())

    def pixel(x, y):
        return page.grab().toImage().pixelColor(x, y).name()

    mid_y = seam.y() + drop.height() // 2
    rest = pixel(seam.x(), mid_y)
    repo.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, True)
    QCoreApplication.sendEvent(repo, QEvent(QEvent.Type.Enter))
    app.processEvents()
    lit = pixel(seam.x(), mid_y)
    edge = pixel(top.x() + repo.width() // 2, top.y())
    check("repo seam: hovering Open repo lights the dropdown's left edge",
          drop.property("seamLit") is True and lit != rest,
          (drop.property("seamLit"), rest, lit))
    check("repo seam: the lit edge matches Open repo's hover border",
          lit == edge, (lit, edge))
    repo.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
    QCoreApplication.sendEvent(repo, QEvent(QEvent.Type.Leave))
    app.processEvents()
    check("repo seam: leaving Open repo puts the edge back",
          drop.property("seamLit") is False
          and pixel(seam.x(), mid_y) == rest,
          (drop.property("seamLit"), pixel(seam.x(), mid_y), rest))
    page.deleteLater()


def test_repo_dropdown_second_click_closes():
    """A second click on the dropdown arrow beside Open repo closes the
    activity menu. The popup grab hands that press to the menu, which closes
    as for any outside click, and Qt then replayed the press to the arrow,
    whose click opened the menu again. QTest clicks bypass the grab, so the
    check feeds the menu the press it would get."""
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from app.widgets.workspace_page import WorkspacePage
    from app.workspace_manager import Workspace
    from main import setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    page = WorkspacePage(Workspace(id="w1", name="WS",
                                   project_path="C:/proj/ai-hive"))
    page.resize(1300, 200)
    page.show()
    app.processEvents()
    drop, menu = page.repo_activity_btn, page.repo_activity_menu
    no_replay = Qt.WidgetAttribute.WA_NoMouseReplay

    def press_at(widget):
        g = widget.mapToGlobal(widget.rect().center())
        ev = QMouseEvent(QEvent.Type.MouseButtonPress,
                         QPointF(menu.mapFromGlobal(g)), QPointF(g),
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(menu, ev)
        app.processEvents()

    drop.click()
    app.processEvents()
    check("repo dropdown: clicking the arrow opens the menu",
          menu.isVisible())
    press_at(drop)
    check("repo dropdown: a press on the arrow closes the open menu",
          not menu.isVisible())
    check("repo dropdown: ...and is not replayed to the arrow to reopen it",
          menu.testAttribute(no_replay))
    drop.click()
    app.processEvents()
    check("repo dropdown: opening again re-arms the replay for other clicks",
          menu.isVisible() and not menu.testAttribute(no_replay))
    press_at(page.repo_btn)
    check("repo dropdown: a press elsewhere closes it and still replays there",
          not menu.isVisible() and not menu.testAttribute(no_replay))
    menu.hide()
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
    check("cat persist: saved under the current SESSION_VERSION",
          data["version"] == SESSION_VERSION)
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
    sb.deleteLater()


def test_category_row_click_toggles():
    """A click anywhere on a category row toggles it, not only the caret. A
    click on the name text toggles after the double-click interval, and a
    double-click there renames without toggling. A drag never toggles."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from app.widgets.sidebar import Sidebar

    QApplication.instance() or QApplication([])
    sb = Sidebar()
    sb.resize(240, 220)
    sb.add_row("w", "Web", "p")
    cid = sb._add_category("Work")
    row = sb._cat_widgets[cid]
    row._end_rename()
    sb.show()
    QApplication.processEvents()
    collapsed = lambda: sb._cat_node(cid)["collapsed"]  # noqa: E731
    left = Qt.MouseButton.LeftButton

    # the empty stretch between the name text and the count chip
    blank = QPoint(row.count_label.geometry().left() - 4, row.height() // 2)
    check("cat click: test point is off the name text",
          not row._on_name_text(blank))
    QTest.mouseClick(row, left, pos=blank)
    check("cat click: a click on the row body collapses it", collapsed())
    QTest.mouseClick(row, left, pos=blank)
    check("cat click: a second click expands it again", not collapsed())

    name_pt = row.name_label.geometry().topLeft() + QPoint(3, 6)
    check("cat click: test point is on the name text",
          row._on_name_text(name_pt))
    QTest.mouseClick(row, left, pos=name_pt)
    check("cat click: a name click waits for a possible double-click",
          not collapsed() and row._name_click.isActive())
    QTest.qWait(QApplication.doubleClickInterval() + 150)
    check("cat click: the name click toggles once the interval passes",
          collapsed())
    sb._on_cat_toggled(cid)  # back to expanded
    row = sb._cat_widgets[cid]

    QTest.mouseClick(row, left, pos=name_pt)
    QTest.mouseDClick(row, left, pos=name_pt)
    check("cat click: a double-click on the name renames",
          row._renaming and not row._name_click.isActive())
    QTest.qWait(QApplication.doubleClickInterval() + 150)
    check("cat click: the rename double-click never toggles", not collapsed())
    row._end_rename()

    QTest.mousePress(row, left, pos=blank)
    row._press_pos = None  # what mouseMoveEvent does when a drag starts
    QTest.mouseRelease(row, left, pos=blank)
    check("cat click: a press that became a drag does not toggle",
          not collapsed())

    # a fast pair whose first click is on the name and second is off it:
    # Qt delivers press, release, double-click, release. The release toggles
    # once; the name click's pending toggle must not fire on top of it
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    def send(kind, pos, buttons):
        QApplication.sendEvent(row, QMouseEvent(
            kind, QPointF(pos), QPointF(row.mapToGlobal(pos)), left,
            buttons, Qt.KeyboardModifier.NoModifier))

    flips = []
    row.toggled.connect(flips.append)
    none = Qt.MouseButton.NoButton
    send(QEvent.Type.MouseButtonPress, name_pt, left)
    send(QEvent.Type.MouseButtonRelease, name_pt, none)
    send(QEvent.Type.MouseButtonDblClick, blank, left)
    send(QEvent.Type.MouseButtonRelease, blank, none)
    QTest.qWait(QApplication.doubleClickInterval() + 150)
    check("cat click: name then off-name double-click toggles exactly once",
          len(flips) == 1 and collapsed(), flips)
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
    a1 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Backend", cwd=SCRATCH_CWD))
    a2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Frontend", cwd=SCRATCH_CWD))
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
    # Regression: an argv list made subprocess quote the WHOLE
    # "/select,<path>" token whenever the path had a space, Explorer ignored
    # the quoted switch and opened Documents instead of the file's folder.
    spaced = os.path.join(tempfile.gettempdir(), "AI Projects", "a b.txt")
    cmd = fsopen.explorer_select_cmdline(spaced)
    check("fsopen: reveal cmdline leaves /select, unquoted",
          cmd.startswith('explorer /select,"') and '"/select' not in cmd)
    check("fsopen: reveal cmdline quotes the full spaced path",
          cmd.endswith(f'"{os.path.normpath(spaced)}"'))


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
    a1 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Backend", cwd=SCRATCH_CWD))
    a1.status = AgentStatus.RUNNING
    a1.current_task = "implement the payments webhook"
    a2 = TerminalAgent(build_spec(AgentKind.CLAUDE, "Frontend", cwd=SCRATCH_CWD))
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


def test_new_agent_count():
    """The New Agent dialog's [-] n [+] stepper opens n agents at once: it
    starts at 1 with minus disabled, caps at the workspace's free slots, is
    pinned to 1 while a past conversation is being resumed (two agents on one
    transcript destroy it), and Enter still means OK, not a step."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QDialog
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets.main_window import AddTerminalDialog
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    dlg = AddTerminalDialog("Agent 4", cwd=str(SCRATCH_CWD), max_count=3)
    check("count: defaults to 1", dlg.count() == 1
          and dlg.count_value.text() == "1")
    check("count: minus is disabled at 1", not dlg.count_minus.isEnabled())
    check("count: plus is enabled at 1", dlg.count_plus.isEnabled())
    check("count: minus never goes below 1", (dlg._set_count(0),
                                              dlg.count())[1] == 1)
    dlg.count_plus.click()
    dlg.count_plus.click()
    check("count: plus steps the value", dlg.count() == 3
          and dlg.count_value.text() == "3")
    check("count: minus is enabled above 1", dlg.count_minus.isEnabled())
    check("count: plus is disabled at the free-slot cap",
          not dlg.count_plus.isEnabled())
    dlg.count_plus.click()
    check("count: plus can't pass the cap", dlg.count() == 3)
    check("count: stepper buttons are not autoDefault (Enter = OK)",
          not dlg.count_minus.autoDefault() and not dlg.count_plus.autoDefault())

    specs = dlg.result_specs(cwd=str(SCRATCH_CWD))
    check("count: one spec per agent", len(specs) == 3, len(specs))
    check("count: names count on from the default",
          [s.name for s in specs] == ["Agent 4", "Agent 5", "Agent 6"],
          [s.name for s in specs])
    check("count: specs are distinct objects of the chosen kind",
          len({id(s) for s in specs}) == 3
          and all(s.kind == specs[0].kind for s in specs))
    dlg.name_edit.setText("Reviewer")
    check("count: a custom name gets numbered copies",
          [s.name for s in dlg.result_specs(cwd=str(SCRATCH_CWD))]
          == ["Reviewer", "Reviewer 2", "Reviewer 3"])
    dlg.count_minus.click()
    check("count: minus steps down", dlg.count() == 2)

    # resuming a past conversation pins the count to 1
    dlg.resume_combo.clear()
    dlg.resume_combo.addItem("New conversation", "")
    dlg.resume_combo.addItem("old chat", "11111111-1111-1111-1111-111111111111")
    dlg.resume_combo.show()
    dlg.resume_combo.setCurrentIndex(1)
    check("count: a resumed conversation pins the count to 1",
          dlg.count() == 1 and not dlg.count_plus.isEnabled())
    check("count: resume yields a single spec",
          len(dlg.result_specs(cwd=str(SCRATCH_CWD))) == 1)
    dlg.resume_combo.setCurrentIndex(0)
    check("count: back to a new conversation re-enables plus",
          dlg.count_plus.isEnabled())
    dlg.deleteLater()

    full = AddTerminalDialog("Agent 1", cwd=str(SCRATCH_CWD), max_count=0)
    check("count: a full workspace still shows 1 with both buttons off",
          full.count() == 1 and not full.count_plus.isEnabled()
          and not full.count_minus.isEnabled())
    full.deleteLater()

    # the window opens every requested agent, revealing the first
    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-count-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    pump(150)
    ws = win.manager.workspaces[0]
    before = len(ws.agents)

    def accept_three(self):
        self.count_plus.click()
        self.count_plus.click()
        return QDialog.DialogCode.Accepted

    orig_exec = AddTerminalDialog.exec
    orig_result_spec = AddTerminalDialog.result_spec
    AddTerminalDialog.exec = accept_three
    AddTerminalDialog.result_spec = lambda self, cwd="": build_spec(
        AgentKind.CMD, "Shell 1", cwd=cwd, pty=True)
    try:
        win._on_add_terminal_clicked(ws.id)
    finally:
        AddTerminalDialog.exec = orig_exec
        AddTerminalDialog.result_spec = orig_result_spec
    pump(200)
    new = ws.agents[before:]
    check("count: the window opened 3 agents", len(new) == 3, len(new))
    check("count: the opened agents are numbered",
          [a.spec.name for a in new] == ["Shell 1", "Shell 2", "Shell 3"],
          [a.spec.name for a in new])
    first_card = win._pages[ws.id].card_for(new[0].id) if new else None
    check("count: the first new agent is focused",
          first_card is not None and win._focused_card is first_card)
    win.close()
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


# -------------------------------------------------- GitHub workspace clone ----

def test_workspace_clone_default_branch():
    """New Workspace from a GitHub URL clones the remote's DEFAULT branch,
    whatever it is named. It used to pass `--branch main`, so a repo whose
    default is `master` failed with "Remote branch main not found". Local
    bare repos stand in for GitHub so this runs offline."""
    import subprocess
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import NewWorkspaceDialog, clone_default_branch

    QApplication.instance() or QApplication([])
    tmp = tempfile.mkdtemp(prefix="aihive-clone-")

    def git(*args, cwd=None):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "init.defaultBranch=scratch", *args],
            cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()

    def bare_repo(tag, branches, default):
        """A bare remote holding `branches` (each with its own file), whose
        HEAD points at `default`."""
        work = os.path.join(tmp, tag + "-work")
        git("init", work)
        for branch in branches:
            git("checkout", "-q", "-b", branch, cwd=work)
            Path(work, branch + ".txt").write_text(branch)
            git("add", ".", cwd=work)
            git("commit", "-q", "-m", branch, cwd=work)
        bare = os.path.join(tmp, tag + ".git")
        git("clone", "-q", "--bare", work, bare)
        git("symbolic-ref", "HEAD", "refs/heads/" + default, cwd=bare)
        return bare

    try:
        # A `master`-default repo: the reported failure.
        dest = os.path.join(tmp, "master-clone")
        err = clone_default_branch(bare_repo("master", ["master"], "master"), dest)
        check("clone: a master-default repo clones", err == "", err)
        check("clone: master repo checks out master",
              os.path.isdir(dest) and git("branch", "--show-current", cwd=dest) == "master")

        # Default `trunk` while a `main` exists too: follow the remote's HEAD,
        # not the naming convention.
        dest = os.path.join(tmp, "trunk-clone")
        err = clone_default_branch(
            bare_repo("trunk", ["main", "trunk"], "trunk"), dest)
        check("clone: an unusually named default branch clones", err == "", err)
        check("clone: checks out the remote default, not main",
              os.path.isdir(dest)
              and git("branch", "--show-current", cwd=dest) == "trunk"
              and os.path.isfile(os.path.join(dest, "trunk.txt")))
        remote_branches = (git("branch", "-r", "--format=%(refname)", cwd=dest)
                           .split() if os.path.isdir(dest) else [])
        check("clone: fetches only the default branch",
              "refs/remotes/origin/trunk" in remote_branches
              and "refs/remotes/origin/main" not in remote_branches,
              remote_branches)

        # A plain `main` repo keeps working.
        dest = os.path.join(tmp, "main-clone")
        err = clone_default_branch(bare_repo("main", ["main"], "main"), dest)
        check("clone: a main-default repo still clones",
              err == "" and git("branch", "--show-current", cwd=dest) == "main", err)

        # A failure reports git's text and leaves no folder behind.
        dest = os.path.join(tmp, "missing-clone")
        err = clone_default_branch(os.path.join(tmp, "no-such.git"), dest)
        check("clone: a bad remote returns git's error", bool(err), err)
        check("clone: a failed clone leaves no folder", not os.path.exists(dest))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    dlg = NewWorkspaceDialog(str(SCRATCH_CWD))
    note = dlg.repo_note.text()
    check("clone: dialog says it clones the default branch",
          "default branch" in note and "main branch" not in note, note)
    dlg.deleteLater()
