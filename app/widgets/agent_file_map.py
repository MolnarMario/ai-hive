"""Agent / File Map: an interactive diagram of who is working on what.

A separate, resizable top-level window (movable to another monitor) that draws a
single **Tree** view of the active workspace: a VSCode-style file hierarchy
(folders/subfolders) on the left, the agents as bubbles on the right VERTICALLY
CENTERED against the tree (a "middle-right" overview so connectors fan both up
and down, not all down from a top-corner cluster), and curved connectors from
each agent to the file rows it touched — solid/gold for files it EDITED,
thin/dashed for files it only READ. Each file row is prefixed with a small
type icon (image / code / config / doc / …) keyed on its extension.

The header **Write** / **Read** toggles hide the edited or read-only edges
independently — for when you only care about one kind of activity. It is a
paint-only filter (the file rows stay put; just their connectors vanish), and
the legend greys the hidden kind so the state reads at a glance.

Data comes from `file_activity.activity_for_agent`, which reads each Claude
agent's pinned transcript (the only reliable per-agent file attribution AI Hive
has — see file_activity.py). Claude Task/Agent SUB-AGENTS are shown as small
satellite nodes on their parent hub (labeled with their type), but with NO file
edges: their file ops are not recorded in any transcript AI Hive can read (files
roll up to the parent). Non-Claude agents appear as hubs with no files.

Interactions: drag an agent (or its sub-agent satellites) to rearrange it
(placement persists across the live refresh; the file tree is structural and not
movable), drag empty space to pan, Ctrl+wheel or +/-/0 to zoom, single-click an
agent to jump to its terminal card, double-click a file to open it with the OS
default program, right-click a file for Open / Open with… / Reveal in folder /
Copy path.

The window is a TRANSIENT view: it only reads model state and must never mark
the session dirty. Custom painting reads `Palette.*` at paint time so it follows
the active skin; MainWindow._change_theme repaints it (it is parented there).
"""

import math
import os
from collections import defaultdict
from dataclasses import dataclass, field

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath,
                           QPen)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu,
                               QPushButton, QScrollArea, QToolTip, QVBoxLayout,
                               QWidget)

from .. import file_activity, fsopen
from ..filetypes import DEFAULT_ICON as _DEFAULT_ICON
from ..filetypes import EMOJI_FONT as _EMOJI_FONT
from ..filetypes import file_icon as _file_icon
from ..terminal_agent import AgentStatus
from ..ui_theme import Palette

_ERROR = {AgentStatus.EXITED_ERR, AgentStatus.CRASHED, AgentStatus.FAILED}

# --- geometry (scene coords; the paint transform applies zoom) ---
_M = 30
_HUB_R = 38
_SUB_R = 13

# --- tree-view geometry ---
_TREE_LEFT = 20
_TREE_TOP = 74
_TREE_ROW = 26
_INDENT = 16
_TREE_W = 380          # x of the connector-anchor column (right edge of tree)
_AGENT_GAP = 96        # gap between the tree column and the agents column

_MIN_SCALE, _MAX_SCALE = 0.4, 3.0
_DRAG_SLOP = 4

# File-type icons + the OS "open" helpers now live in app.filetypes / app.fsopen
# (shared with the sidebar file explorer); imported at module top as _file_icon /
# _EMOJI_FONT / _DEFAULT_ICON and fsopen.* respectively.


@dataclass
class _FileVis:
    path: str
    basename: str
    edited: bool
    owners: list = field(default_factory=list)   # [(agent_index, edited)]


@dataclass
class _AgentVis:
    agent_id: str
    name: str
    role: str
    state: str
    is_claude: bool
    truncated: bool
    subs: list = field(default_factory=list)      # list[(type, desc)]
    center: QPointF = None
    sub_pts: list = field(default_factory=list)


@dataclass
class _TreeRow:
    depth: int
    name: str
    is_dir: bool
    file: object = None       # _FileVis when a file row, else None
    rect: QRectF = None       # hit/paint rect
    anchor: QPointF = None    # connector attach point (file rows only)


def _agent_state(agent) -> str:
    if agent.status in _ERROR:
        return "error"
    if agent.is_busy():
        return "working"
    if agent.is_running():
        return "idle"
    return "empty"


_STATE_COLOR = {
    "working": lambda: Palette.GREEN,
    "idle": lambda: Palette.YELLOW,
    "error": lambda: Palette.RED,
    "empty": lambda: Palette.TEXT_DIM,
}


class AgentFileMapCanvas(QWidget):
    agentActivated = Signal(str)   # agent id (single-click on a hub)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._agents: list[_AgentVis] = []
        self._files_all: list[_FileVis] = []   # union of touched files
        # line filters: hide edited ("write") or read-only edges independently,
        # for when you only care about one kind of activity
        self._show_edited = True
        self._show_read = True
        self._empty_msg = ""
        self._scale = 1.0
        self._overrides: dict[str, QPointF] = {}
        self._scroll = None
        self._tree_rows: list[_TreeRow] = []
        # hit lists, rebuilt each layout
        self._hit_agents: list = []   # (center, radius, agent_vis)
        self._hit_subs: list = []     # (center, radius, (av, j))
        self._hit_files: list = []    # (rect, file_vis)
        # interaction state
        self._drag_id = None
        self._drag_grab = None
        self._press_pos = None
        self._moved = False
        self._panning = False
        self._pan_start = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def set_scroll(self, scroll) -> None:
        self._scroll = scroll

    def set_line_visibility(self, edited=None, read=None) -> None:
        """Toggle whether edited ("write") and/or read-only edges are drawn.
        Purely a paint concern — the model and layout are unchanged, so a
        hidden line's file row stays put; only the connector disappears."""
        if edited is not None:
            self._show_edited = bool(edited)
        if read is not None:
            self._show_read = bool(read)
        self.update()

    def _edge_visible(self, edited: bool) -> bool:
        return self._show_edited if edited else self._show_read

    def reset_view(self) -> None:
        self._overrides = {}
        self._scale = 1.0

    def set_model(self, agents, files_all=None, empty_msg="") -> None:
        self._agents = agents
        self._files_all = files_all or []
        self._empty_msg = empty_msg
        try:
            self._relayout()
        finally:
            self.update()   # ALWAYS repaint, even if layout somehow raised

    # ------------------------------------------------------------- layout ---

    def _relayout(self) -> None:
        self._hit_agents, self._hit_subs, self._hit_files = [], [], []
        self._tree_rows = []
        rows = _build_tree_rows(self._files_all)
        for i, row in enumerate(rows):
            y = _TREE_TOP + i * _TREE_ROW
            x = _TREE_LEFT + row.depth * _INDENT
            row.rect = QRectF(x, y, _TREE_W - x, _TREE_ROW)
            if not row.is_dir and row.file is not None:
                row.anchor = QPointF(float(_TREE_W), y + _TREE_ROW / 2)
                self._hit_files.append((row.rect, row.file))
        self._tree_rows = rows
        tree_bottom = _TREE_TOP + len(rows) * _TREE_ROW

        # Agents sit in a column on the right, but VERTICALLY CENTERED against
        # the tree's extent (a "middle-right" overview) rather than pinned to the
        # top — so connectors fan out both up and down instead of all diving down
        # from a top-corner cluster. The stack keeps a fixed inter-hub step (the
        # gap accounts for the hub, its caption, and any sub-agent satellites, so
        # bubbles never overlap); it only slides down to the middle. If the stack
        # is taller than the tree it stays clamped at the top and grows downward.
        agents_x = _TREE_W + _AGENT_GAP + _HUB_R
        step = 2 * _HUB_R + 60
        n = len(self._agents)
        floor_y = _TREE_TOP + _HUB_R + 10
        block = (n - 1) * step
        start_y = max(floor_y, (_TREE_TOP + tree_bottom) / 2 - block / 2)
        for i, av in enumerate(self._agents):
            default = QPointF(float(agents_x), start_y + i * step)
            av.center = self._resolve(_aid(av), default)   # honor drag placement
            av.sub_pts = self._sub_defaults(av)
            for j in range(len(av.subs)):
                av.sub_pts[j] = self._resolve(_sid(av, j), av.sub_pts[j])
        self._register_hits()
        agents_bottom = (self._agents[-1].center.y() + _HUB_R
                         if self._agents else _TREE_TOP)
        self._apply_size(agents_x + _HUB_R, max(tree_bottom, agents_bottom))

    def _resolve(self, nid, default):
        return self._overrides.get(nid, default)

    def _register_hits(self) -> None:
        for av in self._agents:
            self._hit_agents.append((av.center, _HUB_R, av))
            for j, sp in enumerate(av.sub_pts):
                self._hit_subs.append((sp, _SUB_R, (av, j)))

    def _sub_defaults(self, av) -> list:
        c, k = av.center, len(av.subs)
        pts, span = [], min(1.6, 0.5 * len(av.subs))
        start, reach = -math.pi / 2 - span / 2, _HUB_R + 34
        for j in range(k):
            ang = start + (span * (j / (k - 1)) if k > 1 else span / 2)
            pts.append(QPointF(c.x() + reach * math.cos(ang),
                               c.y() + reach * math.sin(ang)))
        return pts

    def _apply_size(self, w, h) -> None:
        self.setMinimumSize(int((w + _M) * self._scale), int((h + _M) * self._scale))

    # -------------------------------------------------------------- paint ---

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(Palette.BG_ROOT))
        if self._empty_msg:
            p.setPen(QColor(Palette.TEXT_DIM))
            f = QFont(); f.setPixelSize(14); p.setFont(f)
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._empty_msg)
            p.end()
            return
        self._draw_legend(p)
        p.scale(self._scale, self._scale)
        self._paint_tree(p)
        p.end()

    def _paint_tree(self, p) -> None:
        if not self._tree_rows:   # agents but no attributed files yet
            p.setPen(QColor(Palette.TEXT_DIM))
            f = QFont(); f.setPixelSize(12); p.setFont(f)
            p.drawText(QRectF(_TREE_LEFT, _TREE_TOP, _TREE_W - _TREE_LEFT, 40),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "No files attributed yet. Agents appear on the right\n"
                       "once they read or edit files.")
        # connectors first (under the hubs, which are opaque)
        for row in self._tree_rows:
            if row.is_dir or row.file is None:
                continue
            for (idx, edited) in row.file.owners:
                if self._edge_visible(edited) and 0 <= idx < len(self._agents):
                    start = self._agents[idx].center - QPointF(_HUB_R, 0)
                    self._draw_curve(p, start, row.anchor, edited)
        # the file hierarchy
        f = QFont(); f.setPixelSize(12); p.setFont(f)
        fm = QFontMetrics(f)
        for row in self._tree_rows:
            tx = row.rect.left()
            cy = row.rect.center().y()
            if row.is_dir:
                p.setPen(QPen(QColor(Palette.ACCENT_GOLD_DIM), 1.2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRect(QRectF(tx, cy - 5, 11, 9))    # folder glyph
                p.setPen(QColor(Palette.TEXT))
                f.setBold(True); p.setFont(f)
                p.drawText(QPointF(tx + 18, cy + 4), row.name)
                f.setBold(False); p.setFont(f)
            else:
                edited = row.file.edited
                # file-type icon (emoji) so the kind reads at a glance; the pen
                # colour is the monochrome-fallback tint (edited=bright)
                p.setPen(QColor(Palette.TEXT if edited else Palette.TEXT_DIM))
                fi = QFont(_EMOJI_FONT); fi.setPixelSize(13); p.setFont(fi)
                p.drawText(QRectF(tx - 1, cy - 8, 16, 16),
                           Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                           _file_icon(row.name))
                p.setFont(f)
                p.setPen(QColor(Palette.TEXT if edited else Palette.TEXT_DIM))
                f.setBold(edited); p.setFont(f)
                p.drawText(QPointF(tx + 18, cy + 4),
                           fm.elidedText(row.name, Qt.TextElideMode.ElideMiddle,
                                         int(_TREE_W - tx - 26)))
                f.setBold(False); p.setFont(f)
                # anchor dot
                p.setBrush(QColor(Palette.ACCENT_GOLD if edited else Palette.TEXT_DIM))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(row.anchor, 2.4, 2.4)
        for av in self._agents:
            self._draw_subs(p, av)
        for av in self._agents:
            self._draw_hub(p, av)

    def _draw_curve(self, p, a, b, edited) -> None:
        # a smooth S-curve from the agent (right) to the file anchor (left)
        path = QPainterPath(a)
        dx = max(40.0, abs(a.x() - b.x()) * 0.45)
        path.cubicTo(QPointF(a.x() - dx, a.y()), QPointF(b.x() + dx, b.y()), b)
        p.setPen(_edge_pen(edited))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawPath(path)

    def _draw_hub(self, p, av) -> None:
        c, r = av.center, _HUB_R
        color = QColor(_STATE_COLOR[av.state]())
        p.setBrush(_opaque_tint(color))   # OPAQUE so edges don't show through
        pen = QPen(color); pen.setWidthF(2.0); p.setPen(pen)
        p.drawEllipse(c, r, r)
        p.setPen(QColor(Palette.TEXT))
        f = QFont(); f.setPixelSize(12); f.setBold(True); p.setFont(f)
        fm = QFontMetrics(f)
        p.drawText(QRectF(c.x() - r, c.y() - 14, 2 * r, 16),
                   Qt.AlignmentFlag.AlignCenter,
                   fm.elidedText(av.name, Qt.TextElideMode.ElideRight, int(2 * r - 6)))
        sub = av.role or ("claude" if av.is_claude else "no transcript")
        p.setPen(QColor(Palette.TEXT_DIM))
        f2 = QFont(); f2.setPixelSize(9); p.setFont(f2)
        fm2 = QFontMetrics(f2)
        p.drawText(QRectF(c.x() - r, c.y() + 2, 2 * r, 14),
                   Qt.AlignmentFlag.AlignCenter,
                   fm2.elidedText(sub, Qt.TextElideMode.ElideRight, int(2 * r - 6)))
        if av.truncated:
            p.setPen(QColor(Palette.YELLOW))
            p.drawText(QRectF(c.x() - r, c.y() + 15, 2 * r, 12),
                       Qt.AlignmentFlag.AlignCenter, "(partial)")

    def _draw_subs(self, p, av) -> None:
        for j, (stype, _desc) in enumerate(av.subs):
            sp = QPointF(av.sub_pts[j])
            pen = QPen(QColor(Palette.TEXT_FAINT)); pen.setWidthF(1.0)
            p.setPen(pen)
            p.drawLine(av.center, sp)
            p.setBrush(_opaque_tint(QColor(Palette.TEXT_DIM), 0.30))
            p.setPen(QPen(QColor(Palette.TEXT_DIM), 1.2))
            p.drawEllipse(sp, _SUB_R, _SUB_R)
            # a readable caption under the satellite (these are Task sub-agents)
            p.setPen(QColor(Palette.TEXT_DIM))
            f = QFont(); f.setPixelSize(9); p.setFont(f)
            fm = QFontMetrics(f)
            label = stype or "sub-agent"
            p.drawText(QRectF(sp.x() - 34, sp.y() + _SUB_R + 1, 68, 12),
                       Qt.AlignmentFlag.AlignHCenter,
                       fm.elidedText(label, Qt.TextElideMode.ElideRight, 66))

    def _draw_legend(self, p) -> None:
        # a hidden line-type is greyed in the legend so the toggle state reads
        # at a glance ("off" == faint, not merely absent)
        x, y = 14, 12
        f = QFont(); f.setPixelSize(10); p.setFont(f)
        edited_c = QColor(Palette.ACCENT_GOLD if self._show_edited
                          else Palette.TEXT_FAINT)
        pen = QPen(edited_c); pen.setWidthF(2.2); p.setPen(pen)
        p.drawLine(QPointF(x, y + 6), QPointF(x + 26, y + 6))
        p.setPen(QColor(Palette.TEXT_DIM if self._show_edited else Palette.TEXT_FAINT))
        p.drawText(QPointF(x + 32, y + 10), "edited")
        read_c = QColor(Palette.TEXT_DIM if self._show_read else Palette.TEXT_FAINT)
        pen = QPen(read_c); pen.setWidthF(1.3)
        pen.setStyle(Qt.PenStyle.DashLine); p.setPen(pen)
        p.drawLine(QPointF(x + 84, y + 6), QPointF(x + 110, y + 6))
        p.setPen(read_c); p.drawText(QPointF(x + 116, y + 10), "read")

    # ---------------------------------------------------------- hit-test ---

    def _scene(self, wpos) -> QPointF:
        s = self._scale or 1.0
        return QPointF(wpos.x() / s, wpos.y() / s)

    def _hit(self, sp):
        for center, r, av in self._hit_agents:
            if _dist(sp, center) <= r:
                return ("agent", av)
        for center, r, (av, j) in self._hit_subs:
            if _dist(sp, center) <= r:
                return ("sub", av, j)
        for rect, fv in self._hit_files:
            if rect.contains(sp):
                return ("file", fv)
        return None

    def _hit_id(self, kind, obj, extra=None):
        if kind == "agent":
            return _aid(obj)
        return _sid(obj, extra)   # "sub"

    def _node_center(self, hit) -> QPointF:
        if hit[0] == "sub":
            return QPointF(hit[1].sub_pts[hit[2]])
        return QPointF(hit[1].center)

    # -------------------------------------------------------- interaction ---

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        self._press_pos = event.position()
        self._moved = False
        hit = self._hit(self._scene(event.position()))
        kind = hit[0] if hit else None
        # agents + their sub-agent satellites drag; the file tree is structural
        # (its rows are not movable — only hit for tooltip / open / context menu)
        if kind in ("agent", "sub"):
            self._drag_id = self._hit_id(*hit)
            self._drag_grab = self._scene(event.position()) - self._node_center(hit)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        elif hit is None and self._scroll is not None:
            self._panning = True
            self._pan_start = (event.position(),
                               self._scroll.horizontalScrollBar().value(),
                               self._scroll.verticalScrollBar().value())
            self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event):
        pos = event.position()
        if self._press_pos is not None and (
                abs(pos.x() - self._press_pos.x()) > _DRAG_SLOP
                or abs(pos.y() - self._press_pos.y()) > _DRAG_SLOP):
            self._moved = True
        if self._drag_id is not None and self._moved:
            self._overrides[self._drag_id] = self._scene(pos) - self._drag_grab
            self._relayout(); self.update()
            return
        if self._panning and self._pan_start is not None:
            start, h0, v0 = self._pan_start
            self._scroll.horizontalScrollBar().setValue(int(h0 - (pos.x() - start.x())))
            self._scroll.verticalScrollBar().setValue(int(v0 - (pos.y() - start.y())))
            return
        hit = self._hit(self._scene(pos))
        if hit and hit[0] == "file":
            QToolTip.showText(event.globalPosition().toPoint(), hit[1].path, self)
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif hit and hit[0] == "agent":
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        elif hit and hit[0] == "sub":
            stype, desc = hit[1].subs[hit[2]]
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"sub-agent: {stype}\n{desc}".strip(), self)
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            QToolTip.hideText()
            self.setCursor(Qt.CursorShape.OpenHandCursor)

    def mouseReleaseEvent(self, event):
        was_click, panning = (not self._moved, self._panning)
        self._drag_id = self._drag_grab = self._press_pos = self._pan_start = None
        self._panning = False
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        if was_click and not panning:
            hit = self._hit(self._scene(event.position()))
            if hit and hit[0] == "agent" and hit[1].agent_id:
                self.agentActivated.emit(hit[1].agent_id)

    def mouseDoubleClickEvent(self, event):
        hit = self._hit(self._scene(event.position()))
        if hit and hit[0] == "file":
            fsopen.open_path(hit[1].path)

    def contextMenuEvent(self, event):
        hit = self._hit(self._scene(event.position()))
        if not hit or hit[0] != "file":
            return
        path = hit[1].path
        menu = QMenu(self)
        menu.addAction("Open", lambda: fsopen.open_path(path))
        menu.addAction("Open with…", lambda: fsopen.open_with(path))
        menu.addAction("Reveal in folder", lambda: fsopen.reveal_in_folder(path))
        menu.addAction("Copy path", lambda: QApplication.clipboard().setText(path))
        menu.exec(event.globalPos())

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            d = event.angleDelta().y()
            if d:
                self._set_scale(self._scale * (1.1 if d > 0 else 1 / 1.1))
            event.accept()
        else:
            event.ignore()

    def keyPressEvent(self, event):
        k = event.key()
        if k in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
            self._set_scale(self._scale * 1.1)
        elif k in (Qt.Key.Key_Minus, Qt.Key.Key_Underscore):
            self._set_scale(self._scale / 1.1)
        elif k == Qt.Key.Key_0:
            self._set_scale(1.0)
        else:
            super().keyPressEvent(event)

    def _set_scale(self, s) -> None:
        s = max(_MIN_SCALE, min(_MAX_SCALE, s))
        if abs(s - self._scale) < 1e-3:
            return
        self._scale = s
        self._relayout()
        self.update()


# ------------------------------------------------------------ tree builder ---

def _build_tree_rows(files) -> list:
    """Turn a flat list of touched _FileVis into an ordered, indented row list
    (folders + files) rooted at their common directory — the VSCode-explorer
    shape. Only touched files and their ancestor folders appear.

    Bulletproof: real projects contain awkward shapes (an extension-less file
    sharing a name with a folder, mixed drives, relative paths) that must never
    raise — a throw here would land on the layout slot and silently freeze the
    view. Anything unexpected degrades to a FLAT file list."""
    if not files:
        return []
    try:
        norm = [(os.path.normpath(f.path), f) for f in files]
        paths = [p for p, _ in norm]
        try:
            root = os.path.commonpath(paths)
        except ValueError:          # different drives — no shared root
            root = ""
        if root and os.path.isfile(root):    # single file: root is its folder
            root = os.path.dirname(root)

        tree = {}   # name -> dict (folder) | _FileVis (file)
        for path, fv in norm:
            rel = os.path.relpath(path, root) if root else path
            parts = [p for p in rel.replace("\\", "/").split("/")
                     if p and p not in (".", "..")]
            if not parts:
                parts = [os.path.basename(path) or path]
            node = tree
            ok = True
            for part in parts[:-1]:
                nxt = node.get(part)
                if nxt is None:
                    nxt = {}
                    node[part] = nxt
                elif not isinstance(nxt, dict):
                    ok = False        # a file already claims this name; skip it
                    break
                node = nxt
            if ok and isinstance(node, dict):
                node[parts[-1]] = fv

        rows: list[_TreeRow] = []
        if root:
            rows.append(_TreeRow(depth=0, name=os.path.basename(root) or root,
                                 is_dir=True))
        base_depth = 1 if root else 0

        def walk(node, depth):
            dirs = sorted(k for k, v in node.items() if isinstance(v, dict))
            fns = sorted(k for k, v in node.items() if not isinstance(v, dict))
            for d in dirs:
                rows.append(_TreeRow(depth=depth, name=d, is_dir=True))
                walk(node[d], depth + 1)
            for fn in fns:
                rows.append(_TreeRow(depth=depth, name=fn, is_dir=False,
                                     file=node[fn]))

        walk(tree, base_depth)
        return rows
    except Exception:   # never freeze the view — degrade to a flat list
        return [_TreeRow(depth=0, name=f.basename, is_dir=False, file=f)
                for f in files]


# ---------------------------------------------------- small paint helpers ---

def _edge_pen(edited) -> QPen:
    if edited:
        pen = QPen(QColor(Palette.ACCENT_GOLD)); pen.setWidthF(2.2)
    else:
        pen = QPen(QColor(Palette.TEXT_DIM)); pen.setWidthF(1.3)
        pen.setStyle(Qt.PenStyle.DashLine)
    return pen


def _opaque_tint(color, t=0.22) -> QColor:
    """An OPAQUE colour: the status colour blended `t` of the way over the
    root background, so the hub hides any edges passing beneath it while
    staying dark enough for legible text."""
    bg = QColor(Palette.BG_ROOT)
    return QColor(int(bg.red() + (color.red() - bg.red()) * t),
                  int(bg.green() + (color.green() - bg.green()) * t),
                  int(bg.blue() + (color.blue() - bg.blue()) * t))


def _aid(av) -> str:
    return "agent:" + (av.name or av.agent_id)


def _sid(av, j) -> str:
    return f"sub:{av.name}:{j}"


def _dist(a: QPointF, b: QPointF) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())


class AgentFileMapWindow(QWidget):
    """Top-level window hosting the interactive canvas for ONE workspace,
    live-refreshing on a timer while visible. Reused across opens."""

    REFRESH_MS = 2000
    agentActivated = Signal(str, str)   # ws_id, agent_id

    _HINT = ("Write/Read toggle lines · click agent → its card · "
             "double-click file → open · drag agent to move · Ctrl+wheel zoom")

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setObjectName("AgentFileMapWindow")
        self.setWindowTitle("Agent / File Map")
        self.resize(940, 660)
        self._workspace = None
        self._ws_id = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        header = QFrame(self)
        header.setObjectName("WorkspaceHeader")
        header.setFixedHeight(38)
        hl = QHBoxLayout(header)
        hl.setContentsMargins(12, 0, 8, 0)
        hl.setSpacing(8)
        self.title = QLabel("Agent / File Map", header)
        self.title.setObjectName("HeaderPath")
        self.hint = QLabel(self._HINT, header)
        self.hint.setObjectName("HeaderPath")
        # line filters: hide edited / read-only edges independently
        self.write_btn = QPushButton("✎ Write", header)
        self.write_btn.setObjectName("MapWriteToggle")
        self.write_btn.setToolTip("Show lines to files agents EDITED")
        self.read_btn = QPushButton("↴ Read", header)
        self.read_btn.setObjectName("MapReadToggle")
        self.read_btn.setToolTip("Show lines to files agents only READ")
        for b in (self.write_btn, self.read_btn):
            b.setCheckable(True)
            b.setChecked(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh = QPushButton("↻ Refresh", header)
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.refresh)
        hl.addWidget(self.title)
        hl.addWidget(self.hint, 1)
        hl.addWidget(self.write_btn)
        hl.addWidget(self.read_btn)
        hl.addWidget(refresh)
        root.addWidget(header)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.canvas = AgentFileMapCanvas(self.scroll)
        self.canvas.set_scroll(self.scroll)
        # wire the filters AFTER the canvas exists (toggled can fire on setup)
        self.write_btn.toggled.connect(
            lambda on: self.canvas.set_line_visibility(edited=on))
        self.read_btn.toggled.connect(
            lambda on: self.canvas.set_line_visibility(read=on))
        self.canvas.agentActivated.connect(self._on_agent_activated)
        self.scroll.setWidget(self.canvas)
        root.addWidget(self.scroll, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(self.REFRESH_MS)
        self._timer.timeout.connect(self.refresh)

    def _on_agent_activated(self, agent_id: str) -> None:
        if self._ws_id:
            self.agentActivated.emit(self._ws_id, agent_id)

    def set_workspace(self, workspace) -> None:
        ws_id = getattr(workspace, "id", None) if workspace else None
        if ws_id != self._ws_id:
            self.canvas.reset_view()
        self._workspace = workspace
        self._ws_id = ws_id
        name = getattr(workspace, "name", "") if workspace else ""
        self.setWindowTitle(f"Agent / File Map: {name}" if name
                            else "Agent / File Map")
        self.title.setText(name or "Agent / File Map")
        self.refresh()

    def refresh(self) -> None:
        ws = self._workspace
        agents = list(getattr(ws, "agents", []) or []) if ws else []
        if not agents:
            self.canvas.set_model([], [], empty_msg=(
                "No agents in this workspace yet." if ws else "No workspace."))
            return

        acts = [(a, file_activity.activity_for_agent(a)) for a in agents]
        # every touched file, with the (agent_index, edited) owners that touched
        # it — a file touched by 2+ agents simply carries multiple owners and so
        # gets a connector from each (no separate "shared band" any more)
        key_owners = defaultdict(list)
        for idx, (_agent, act) in enumerate(acts):
            if not act:
                continue
            for k, fa in act.files.items():
                key_owners[k].append((idx, fa))

        avs: list[_AgentVis] = []
        for agent, act in acts:
            spec = agent.spec
            avs.append(_AgentVis(
                agent_id=getattr(agent, "id", ""),
                name=spec.name,
                role=spec.role or "",
                state=_agent_state(agent),
                is_claude=(spec.provider == "claude"),
                truncated=bool(act and act.truncated),
                subs=[(s.subagent_type, s.description)
                      for s in (act.subagents if act else [])]))

        files_all: list[_FileVis] = []
        for k in sorted(key_owners):
            owners = key_owners[k]
            rep = owners[0][1]
            owner_pairs = [(idx, fa.edited) for idx, fa in owners]
            any_edit = any(e for _, e in owner_pairs)
            files_all.append(_FileVis(path=rep.path, basename=rep.basename,
                                      edited=any_edit, owners=owner_pairs))

        self.canvas.set_model(avs, files_all)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def closeEvent(self, event):
        self._timer.stop()
        super().closeEvent(event)
