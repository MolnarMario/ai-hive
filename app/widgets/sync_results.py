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
  "Up to date." A folder that is up to date with unpushed commits keeps
  its own row, because that sentence carries news.

A row's tooltip is its folder, and Copy puts the whole report on the
clipboard as plain text (`plain_text`). Colors come from the active
skin's `Palette` when the window opens.

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

import html
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QDialog, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .. import workspace_sync
from ..ui_theme import Palette, _hex_rgb
from ..workspace_sync import Outcome

TITLE = "Sync with GitHub"
UP_TO_DATE_DETAIL = "Up to date."

# section key -> (heading, glyph); the order is the order on screen
SECTIONS = (
    ("attention", "Needs attention", "!"),
    ("updated", "Updated", "↑"),
    ("skipped", "Not synced", "•"),
    ("current", "Up to date", "✓"),
)
_FAILED_GLYPH = "✗"


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


def headline(results) -> str:
    """The big line at the top: what the user has to do, if anything."""
    if results is None:
        return "The sync stopped unexpectedly."
    n = len(group(results)["attention"])
    if n:
        return ("1 workspace needs your attention" if n == 1
                else f"{n} workspaces need your attention")
    return "Sync finished"


def _count_text(key: str, n: int, heading: str) -> str:
    if key == "attention":
        return f"{n} needs attention" if n == 1 else f"{n} need attention"
    return f"{n} {heading.lower()}"


def _name(res) -> str:
    return ", ".join(res.names) or res.folder or "?"


def plain_text(results) -> str:
    """The whole report as text, for Copy (and for reading in a test)."""
    if results is None:
        return headline(None)
    lines = [headline(results), workspace_sync.summary(results)]
    for key, heading, _g in SECTIONS:
        rows = group(results)[key]
        if rows:
            lines += ["", f"{heading} ({len(rows)})"]
            lines += [f"  {_name(r)}: {r.detail}" for r in rows]
    return "\n".join(lines)


def _rgba(hex_color: str, alpha: float) -> str:
    r, g, b = _hex_rgb(hex_color)
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def _detail_html(detail: str) -> str:
    """The reason with any 'quoted' branch name set in the console font, so
    a long branch name reads as a name and not as part of the sentence."""
    out, last = [], 0
    for m in re.finditer(r"'([^'\s][^']*)'", detail):
        out.append(html.escape(detail[last:m.start()]))
        out.append(f"<span style=\"font-family: Consolas, 'Cascadia Mono', "
                   f"monospace; color: {Palette.TEXT};\">"
                   f"{html.escape(m.group(1))}</span>")
        last = m.end()
    out.append(html.escape(detail[last:]))
    return "".join(out)


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
        self._colors = {
            "attention": Palette.RED,
            "updated": Palette.GREEN,
            "skipped": Palette.TEXT_DIM,
            "current": Palette.GREEN,
        }
        self.setStyleSheet(self._qss())

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 18, 20, 16)
        root.setSpacing(12)
        root.addLayout(self._header())

        if self._groups is not None and results:
            body = QWidget()
            body.setObjectName("SyncBody")
            col = QVBoxLayout(body)
            col.setContentsMargins(0, 0, 6, 0)
            col.setSpacing(14)
            for key, heading, glyph in SECTIONS:
                rows = self._groups[key]
                if rows:
                    col.addWidget(self._section(key, heading, glyph, rows))
            col.addStretch(1)
            scroll = QScrollArea()
            scroll.setObjectName("SyncScroll")
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            scroll.setWidget(body)
            self._scroll, self._body = scroll, body
            self._fitted = False
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
        if getattr(self, "_fitted", True):
            return
        # the wrapped reasons only know their height once the window has
        # its width: size the list to that, up to 60% of the screen, so a
        # short report has no empty band and a long one scrolls
        self._fitted = True
        width = self._scroll.viewport().width()
        lay = self._body.layout()
        want = (lay.heightForWidth(width) if lay.hasHeightForWidth()
                else self._body.sizeHint().height())
        screen = self.screen() or QGuiApplication.primaryScreen()
        tall = screen.availableGeometry().height() if screen else 900
        self._scroll.setMinimumHeight(min(want + 2, int(tall * 0.6)))
        self.adjustSize()

    # --- pieces ------------------------------------------------------------
    def _qss(self) -> str:
        p = Palette
        return f"""
QDialog#SyncResults {{ background: {p.BG_PANEL}; }}
QWidget#SyncBody, QScrollArea#SyncScroll {{ background: transparent; }}
QLabel#SyncHeadline {{ color: {p.TEXT}; font-size: 17px; font-weight: 600; }}
QLabel#SyncSectionTitle {{ font-size: 11px; font-weight: 700;
    letter-spacing: 1px; }}
QFrame#SyncRow {{ background: {p.BG_CARD}; border: 1px solid {p.BORDER_SOFT};
    border-radius: 6px; }}
QLabel#SyncRowName {{ color: {p.TEXT}; font-weight: 600; }}
QLabel#SyncRowDetail {{ color: {p.TEXT_DIM}; }}
QLabel#SyncNames {{ color: {p.TEXT}; }}
"""

    def _header(self) -> QHBoxLayout:
        attention = bool(self._groups and self._groups["attention"])
        stopped = self._results is None
        color = Palette.RED if (attention or stopped) else Palette.GREEN
        row = QHBoxLayout()
        row.setSpacing(14)
        badge = self._badge("!" if (attention or stopped) else "✓", color,
                            34)
        row.addWidget(badge, 0, Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(6)
        self.headline_label = QLabel(headline(self._results))
        self.headline_label.setObjectName("SyncHeadline")
        text.addWidget(self.headline_label)
        if self._groups is not None:
            pills = QHBoxLayout()
            pills.setSpacing(6)
            for key, heading, _g in SECTIONS:
                n = len(self._groups[key])
                if n:
                    pills.addWidget(self._pill(_count_text(key, n, heading),
                                               self._colors[key]))
            pills.addStretch(1)
            text.addLayout(pills)
        else:
            why = QLabel("Nothing was changed. Try Sync with GitHub again.")
            why.setObjectName("SyncRowDetail")
            text.addWidget(why)
        row.addLayout(text, 1)
        return row

    def _badge(self, glyph: str, color: str, px: int) -> QLabel:
        b = QLabel(glyph)
        b.setObjectName("SyncBadge")
        b.setFixedSize(px, px)
        b.setAlignment(Qt.AlignmentFlag.AlignCenter)
        b.setStyleSheet(
            f"QLabel#SyncBadge {{ background: {_rgba(color, 0.16)};"
            f" color: {color}; border: 1px solid {_rgba(color, 0.45)};"
            f" border-radius: {px // 2}px; font-weight: 700;"
            f" font-size: {max(13, px * 3 // 5)}px; }}")
        return b

    def _pill(self, text: str, color: str) -> QLabel:
        p = QLabel(text)
        p.setObjectName("SyncPill")
        p.setStyleSheet(
            f"QLabel#SyncPill {{ background: {_rgba(color, 0.14)};"
            f" color: {color}; border: 1px solid {_rgba(color, 0.35)};"
            f" border-radius: 9px; padding: 1px 9px; font-weight: 600; }}")
        return p

    def _section(self, key: str, heading: str, glyph: str, rows) -> QWidget:
        color = self._colors[key]
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        title = QLabel(f"{heading.upper()}  {len(rows)}")
        title.setObjectName("SyncSectionTitle")
        title.setStyleSheet(f"color: {color};")
        col.addWidget(title)
        if key == "current":
            plain = [r for r in rows if r.detail == UP_TO_DATE_DETAIL]
            news = [r for r in rows if r.detail != UP_TO_DATE_DETAIL]
            if plain:
                names = QLabel("   ·   ".join(html.escape(_name(r))
                                              for r in plain))
                names.setObjectName("SyncNames")
                names.setTextFormat(Qt.TextFormat.RichText)
                names.setWordWrap(True)
                names.setTextInteractionFlags(
                    Qt.TextInteractionFlag.TextSelectableByMouse)
                names.setToolTip("\n".join(f"{_name(r)}: {r.folder}"
                                           for r in plain))
                col.addWidget(names)
            rows = news
        for res in rows:
            mark = (_FAILED_GLYPH if res.outcome is Outcome.FAILED
                    else glyph)
            col.addWidget(self._row(res, mark, color))
        return box

    def _row(self, res, glyph: str, color: str) -> QFrame:
        frame = QFrame()
        frame.setObjectName("SyncRow")
        frame.setToolTip(res.folder)
        line = QHBoxLayout(frame)
        line.setContentsMargins(10, 8, 12, 8)
        line.setSpacing(10)
        line.addWidget(self._badge(glyph, color, 22), 0,
                       Qt.AlignmentFlag.AlignTop)
        text = QVBoxLayout()
        text.setSpacing(2)
        name = QLabel(_name(res))
        name.setObjectName("SyncRowName")
        name.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        text.addWidget(name)
        if res.detail:
            detail = QLabel(_detail_html(res.detail))
            detail.setObjectName("SyncRowDetail")
            detail.setTextFormat(Qt.TextFormat.RichText)
            detail.setWordWrap(True)
            detail.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            text.addWidget(detail)
        line.addLayout(text, 1)
        return frame

    # --- actions -----------------------------------------------------------
    def plain_text(self) -> str:
        return plain_text(self._results)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self.plain_text())
        self.copy_btn.setText("Copied")
