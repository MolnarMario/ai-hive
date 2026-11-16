"""The results window a clicked Sync with GitHub opens when a folder needs
attention (`MainWindow._show_workspace_sync_results`).

It used to be a warning box with one text line per workspace, every line
in the same color whatever happened, so the one folder that needed the
user hid among the ones that were fine. This window sorts the results
into sections, worst first, each with its own color:

- Needs attention: git failed, or GitHub has commits this folder could
  not take (`workspace_sync.needs_attention`). One row each, red.
- Updated: what the sync pulled. One row each, green.
- Not connected: folders with no repository or no remote, which the sync
  leaves out (`workspace_sync.Outcome.NOT_CONNECTED`). One row each, in
  the accent, with a Connect button: the workspace header's Connect to
  repo dialog, then a sync of that folder alone. The row says what is
  happening meanwhile, and the result replaces it in place
  (`replace_results`), so the user connects one folder after another
  without the window closing.
- Not synced: folders with nothing to track (not a repository, a branch
  with no upstream). One row each, muted, because there is nothing to do.
- Up to date: one wrapped line of names, since every reason would read
  "Up to date." A folder with commits GitHub lacks (`Result.ahead`) keeps
  its own row, because its sentence carries news.

The section counts are the header's pills and the Copy report's summary
line, so the two never disagree. A row's tooltip is its folder. Names
are plain text: nothing renames a workspace except the user, so it shows
as typed. The branch and upstream the result names are set in the
console font, found by their exact text, never by guessing from quotes.

The look lives in `ui_theme.build_qss` under the Sync* object names, each
section's color picked by the `section` property, so a skin change while
the window is open recolors it with the rest of the app.

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

import html

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import workspace_sync
from ..workspace_sync import Outcome

TITLE = "Sync with GitHub"
MAX_SCREEN_SHARE = 0.6      # the list scrolls past this much of the screen

# section key -> (heading, glyph); the order is the order on screen
SECTIONS = (
    ("attention", "Needs attention", "!"),
    ("updated", "Updated", "↑"),
    ("unconnected", "Not connected", "○"),
    ("skipped", "Not synced", "•"),
    ("current", "Up to date", "✓"),
)
_FAILED_GLYPH = "✗"
CONNECT_TEXT = "Connect"
CONNECT_TIP = ("Connect this workspace's folder to a GitHub repository, "
               "then sync it")
_MONO = "font-family: Consolas, 'Cascadia Mono', monospace;"


def section_of(res) -> str:
    if workspace_sync.needs_attention(res):
        return "attention"
    return {Outcome.UPDATED: "updated", Outcome.UP_TO_DATE: "current",
            Outcome.NOT_CONNECTED: "unconnected"}.get(res.outcome, "skipped")


def group(results) -> dict:
    """Section key -> its results, in the order they came (sidebar order)."""
    out = {key: [] for key, _h, _g in SECTIONS}
    for res in results or ():
        out[section_of(res)].append(res)
    return out


def headline(groups) -> str:
    """The big line at the top, from `group()` (None: the sync stopped)."""
    if groups is None:
        return "The sync stopped unexpectedly."
    n = len(groups["attention"])
    if n:
        return ("1 workspace needs your attention" if n == 1
                else f"{n} workspaces need your attention")
    if groups["unconnected"] and not any(
            groups[k] for k, _h, _g in SECTIONS if k != "unconnected"):
        return "No workspace is connected to GitHub yet"
    return "Sync finished"


def count_text(key: str, n: int) -> str:
    """One section's count, as its pill and in the report's summary."""
    if key == "attention":
        return f"{n} needs attention" if n == 1 else f"{n} need attention"
    heading = dict((k, h) for k, h, _g in SECTIONS)[key]
    return f"{n} {heading.lower()}"


def counts(groups) -> list:
    """[(section key, its count text)] for each section with results."""
    return [(key, count_text(key, len(groups[key])))
            for key, _h, _g in SECTIONS if groups[key]]


def _name(res) -> str:
    return ", ".join(res.names) or res.folder or "?"


def plain_text(results, groups=None) -> str:
    """The whole report as text, for Copy (and for reading in a test)."""
    if results is None:
        return headline(None)
    if groups is None:
        groups = group(results)
    lines = [headline(groups), ", ".join(t for _k, t in counts(groups))]
    for key, heading, _g in SECTIONS:
        rows = groups[key]
        if rows:
            lines += ["", f"{heading} ({len(rows)})"]
            lines += [f"  {_name(r)}: {r.detail}" for r in rows]
    return "\n".join(lines)


def detail_html(res) -> str:
    """The reason, escaped, with the result's branch and upstream set in the
    console font where the sentence quotes them, so a long branch name reads
    as a name and not as part of the sentence."""
    text = res.detail or ""
    marks = [f"'{n}'" for n in (res.branch, res.upstream) if n]
    out, i = [], 0
    while True:
        hits = [(text.find(m, i), m) for m in marks]
        hits = [(at, m) for at, m in hits if at >= 0]
        if not hits:
            break
        at, mark = min(hits)
        out.append(html.escape(text[i:at]))
        out.append(f"<span style=\"{_MONO}\">"
                   f"{html.escape(mark[1:-1])}</span>")
        i = at + len(mark)
    out.append(html.escape(text[i:]))
    return "".join(out)


def _themed(widget, name: str, **props):
    """Name a widget for the app QSS and set its selector properties."""
    widget.setObjectName(name)
    for key, value in props.items():
        widget.setProperty(key, value)
    return widget


def _plain_label(text: str, name: str) -> QLabel:
    label = _themed(QLabel(text), name)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


def merged(old, new) -> list:
    """`old` results with each of `new` in place of the old ones it shares a
    workspace with (the first one's place), the rest of `new` at the end."""
    out = list(old or ())
    for res in new or ():
        ids = set(res.ids)
        at = next((i for i, r in enumerate(out) if ids & set(r.ids)), None)
        out = [r for r in out if not ids & set(r.ids)]
        if at is None:
            out.append(res)
        else:
            out.insert(min(at, len(out)), res)
    return out


class SyncResultsDialog(QDialog):
    """Non-modal, deletes itself on close. Build it with the sync's results
    (None when the sync stopped unexpectedly) and `show()` it.
    `connectRequested(ids)` asks the window to connect a Not connected
    row's folder (the ids of the workspaces on it); `set_row_busy` and
    `replace_results` report back."""

    connectRequested = Signal(object)       # [ws id, ...]

    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setObjectName("SyncResults")
        self.setWindowTitle(TITLE)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(540)
        self._results = results
        self._groups = None
        self._scroll = self._body = self._content = None
        self._fitted = False
        # the Connect button per Not connected row, by its first ws id
        self.connect_buttons: dict = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        self._root = root
        self._populate()

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy this report as text")
        self.copy_btn.setAutoDefault(False)
        self.copy_btn.clicked.connect(self._copy)
        self.close_btn = QPushButton("Close")
        self.close_btn.setDefault(True)
        self.close_btn.clicked.connect(self.close)
        buttons.addWidget(self.copy_btn)
        buttons.addWidget(self.close_btn)
        root.addLayout(buttons)

    def _populate(self) -> None:
        """The header and the sections, from `self._results`. Called again
        by `replace_results`: the old content goes, the buttons stay."""
        results = self._results
        self._groups = group(results) if results is not None else None
        busy = {k: (b.text(), b.isEnabled())
                for k, b in self.connect_buttons.items()}
        self.connect_buttons = {}
        if self._content is not None:
            self._root.removeWidget(self._content)
            self._content.hide()
            self._content.deleteLater()
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)
        lay.addLayout(self._header())
        self._scroll = self._body = None
        if self._groups is not None and results:
            body = _themed(QWidget(), "SyncBody")
            col = QVBoxLayout(body)
            col.setContentsMargins(0, 0, 6, 0)
            col.setSpacing(14)
            for key, heading, glyph in SECTIONS:
                rows = self._groups[key]
                if rows:
                    col.addWidget(self._section(key, heading, glyph, rows))
            col.addStretch(1)
            scroll = _themed(QScrollArea(), "SyncScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(body)
            self._scroll, self._body = scroll, body
            lay.addWidget(scroll, 1)
        self._content = content
        self._root.insertWidget(0, content, 1)
        # a row still connecting keeps saying so after a rebuild
        for key, (text, enabled) in busy.items():
            btn = self.connect_buttons.get(key)
            if btn is not None and not enabled:
                btn.setText(text)
                btn.setEnabled(False)

    # --- what the window reports back --------------------------------------
    def results(self) -> list:
        return list(self._results or ())

    def replace_results(self, new) -> None:
        """A sync of some folders finished (one the user just connected):
        their rows are replaced in place and the sections re-sorted."""
        if self._results is None:
            self._results = list(new or ())
        else:
            self._results = merged(self._results, new)
        for res in new or ():
            for ws_id in res.ids:
                self.connect_buttons.pop(ws_id, None)
        self._populate()

    def set_row_busy(self, ids, text: str = "") -> None:
        """A Not connected row's button while its folder connects or syncs:
        greyed with `text`. An empty `text` gives the button back."""
        for ws_id in ids or ():
            btn = self.connect_buttons.get(ws_id)
            if btn is not None:
                btn.setText(text or CONNECT_TEXT)
                btn.setEnabled(not text)
                return

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if self._fitted or self._scroll is None:
            return
        # The wrapped reasons only know their height once the window has
        # its width. Resize to fit that, up to a share of the screen, so a
        # short report has no empty band and a long one scrolls. A resize,
        # not a minimum: the user can still make the window smaller.
        self._fitted = True
        lay = self._body.layout()
        width = self._scroll.viewport().width()
        want = (lay.heightForWidth(width) if lay.hasHeightForWidth()
                else self._body.sizeHint().height())
        screen = self.screen() or QGuiApplication.primaryScreen()
        tall = screen.availableGeometry().height() if screen else 900
        fit = min(want + 2, int(tall * MAX_SCREEN_SHARE))
        self.resize(self.width(),
                    self.height() - self._scroll.height() + fit)

    # --- pieces ------------------------------------------------------------
    def _header(self) -> QHBoxLayout:
        bad = self._groups is None or bool(self._groups["attention"])
        glyph, section = ("!", "attention") if bad else ("✓", "current")
        if not bad and headline(self._groups) != "Sync finished":
            glyph, section = "○", "unconnected"     # nothing is connected
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(self._badge(glyph, section, True),
                      0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        self.headline_label = _plain_label(headline(self._groups),
                                           "SyncHeadline")
        text.addWidget(self.headline_label)
        if self._groups is not None:
            pills = QHBoxLayout()
            pills.setSpacing(6)
            for key, words in counts(self._groups):
                pill = _themed(QLabel(words), "SyncPill", section=key)
                pill.setTextFormat(Qt.TextFormat.PlainText)
                pills.addWidget(pill)
            pills.addStretch(1)
            text.addLayout(pills)
        else:
            text.addWidget(_plain_label(
                "Nothing was changed. Try Sync with GitHub again.",
                "SyncRowDetail"))
        row.addLayout(text, 1)
        return row

    @staticmethod
    def _badge(glyph: str, section: str, big: bool = False) -> QLabel:
        b = _themed(QLabel(glyph), "SyncBadge", section=section, big=big)
        b.setTextFormat(Qt.TextFormat.PlainText)
        px = 34 if big else 22
        b.setFixedSize(px, px)
        b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return b

    def _section(self, key: str, heading: str, glyph: str, rows) -> QWidget:
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        col.addWidget(_themed(QLabel(f"{heading.upper()}  {len(rows)}"),
                              "SyncSectionTitle", section=key))
        if key == "current":
            plain = [r for r in rows if not r.ahead]
            if plain:
                names = _plain_label("   ·   ".join(_name(r) for r in plain),
                                     "SyncNames")
                names.setWordWrap(True)
                names.setToolTip("<br>".join(
                    html.escape(f"{_name(r)}: {r.folder}") for r in plain))
                col.addWidget(names)
            rows = [r for r in rows if r.ahead]
        for res in rows:
            mark = (_FAILED_GLYPH if res.outcome is Outcome.FAILED
                    else glyph)
            col.addWidget(self._row(res, mark, key))
        return box

    def _row(self, res, glyph: str, section: str) -> QFrame:
        frame = _themed(QFrame(), "SyncRow")
        frame.setToolTip(html.escape(res.folder))
        line = QHBoxLayout(frame)
        line.setContentsMargins(10, 8, 12, 8)
        line.setSpacing(10)
        line.addWidget(self._badge(glyph, section), 0,
                       Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(_plain_label(_name(res), "SyncRowName"))
        if res.detail:
            detail = _themed(QLabel(detail_html(res)), "SyncRowDetail")
            detail.setTextFormat(Qt.TextFormat.RichText)
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            text.addWidget(detail)
        line.addLayout(text, 1)
        if res.outcome is Outcome.NOT_CONNECTED and res.ids:
            btn = QPushButton(CONNECT_TEXT)
            btn.setObjectName("SyncConnect")
            btn.setToolTip(CONNECT_TIP)
            btn.setAutoDefault(False)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            ids = list(res.ids)
            btn.clicked.connect(lambda _=False, i=ids:
                                self.connectRequested.emit(list(i)))
            line.addWidget(btn, 0, Qt.AlignmentFlag.AlignVCenter)
            for ws_id in ids:
                self.connect_buttons[ws_id] = btn
        return frame

    # --- actions -----------------------------------------------------------
    def plain_text(self) -> str:
        return plain_text(self._results, self._groups)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.plain_text())
        self.copy_btn.setText("Copied")
