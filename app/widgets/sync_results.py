"""The results window a clicked Sync with GitHub opens when a folder needs
attention (`MainWindow._show_workspace_sync_results`).

It used to be a warning box with one text line per workspace, every line
in the same color whatever happened, so the one folder that needed the
user hid among the ones that were fine. This window sorts the results
into sections, worst first, each with its own color:

- Needs attention: git failed, or GitHub has commits this folder could
  not take (`workspace_sync.needs_attention`). One row each, red.
- Updated: what the sync pulled. One row each, green.
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

from PySide6.QtCore import Qt
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
    ("skipped", "Not synced", "•"),
    ("current", "Up to date", "✓"),
)
_FAILED_GLYPH = "✗"
_MONO = "font-family: Consolas, 'Cascadia Mono', monospace;"


def section_of(res) -> str:
    if workspace_sync.needs_attention(res):
        return "attention"
    return {Outcome.UPDATED: "updated", Outcome.UP_TO_DATE: "current"}.get(
        res.outcome, "skipped")


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


class SyncResultsDialog(QDialog):
    """Non-modal, deletes itself on close. Build it with the sync's results
    (None when the sync stopped unexpectedly) and `show()` it."""

    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.setObjectName("SyncResults")
        self.setWindowTitle(TITLE)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setMinimumWidth(540)
        self._results = results
        self._groups = group(results) if results is not None else None
        self._scroll = self._body = None
        self._fitted = False

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        root.addLayout(self._header())

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
            root.addWidget(scroll, 1)

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
        row = QHBoxLayout()
        row.setSpacing(14)
        row.addWidget(self._badge("!" if bad else "✓",
                                  "attention" if bad else "current", True),
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
        return frame

    # --- actions -----------------------------------------------------------
    def plain_text(self) -> str:
        return plain_text(self._results, self._groups)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.plain_text())
        self.copy_btn.setText("Copied")
