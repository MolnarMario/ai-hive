"""The Options panel: every top-bar setting, in one anchored popup.

WHY THIS EXISTS. The top bar is a 42px strip and it lost the argument with its
own contents. Five successive attempts rearranged the same fourteen widgets
inside it - a responsive layout, an overflow menu that crashed, a horizontally
scrolling row, a wheel handler for that row, a priority re-ordering, a dropped
caption - and the strip was still too small. Worse, the scrolling row's answer
to running out of room is to slide a control out of view with no affordance
saying it did, so a setting could simply be missing. Settings do not need to be
on the bar at all: they are read rarely and changed rarely. Only the things that
must be GLANCEABLE (the usage pills) or are a primary action (the sidebar
toggle, Add Terminal) earn permanent space.

THIS CLASS IS A DUMB CONTAINER, AND THAT IS THE POINT. It owns the window
flags, the row scaffolding and the placement, and NOTHING else. Every control
inside it is still constructed and driven by `TopBar`, which keeps each
`_refresh_*` / `set_*` / getter exactly as it was. That is what makes the move
invisible to `MainWindow`, whose entire interface to the bar is a set of signals
and non-emitting `set_*` reflectors, and what holds the change in the smoke
suite down to a handful of lines.

`Qt.Popup` rather than a `QMenu`, deliberately, and this repo has paid for both
answers: a `QWidgetAction` DELETES its reparented widget when the menu releases
it, which crashed the earlier overflow design outright, and a widget parked in
an unopened menu genuinely is not `isVisible()`, which silently broke every
visibility assertion in the suite. A plain `Qt.Popup` container has neither
problem, dismisses on click-outside and Escape for free, and is the shape
`GridSelectorPopup` already uses successfully. Unlike that one this panel is
built ONCE and never recreated: its children carry checked state that
`TopBar.set_*` writes into on restore, so a per-click rebuild would throw away
the very thing the panel is showing.

Because a `Qt.Popup` is a top-level window it has no ancestor to inherit a
background from, and an unstyled one renders as an OS-native white rectangle.
`#OptionsPanel` in `build_qss` is therefore load-bearing, not decoration.

TWO COLUMNS, AND A HEIGHT CAP. Stacked in one column the panel grew to about
820 logical px, the whole usable height of a 1080p laptop at 125%, and the
next setting would have run off the bottom. `add_column` starts the right
column, which halves the height and keeps every setting in view. The rows
still sit in a frameless `QScrollArea`: `sizeHint` caps the height at the
room inside the window and screen bounds (`ornaments.popup_bounds`) that
`anchored_popup_pos` places in, so a
short window scrolls the panel instead of clipping its last rows. A window
narrower than the two columns stacks them again (`_set_stacked`), and the
height cap scrolls the result, so no row lands past the right edge. The panel
computes its own size hint from the body's because `QScrollArea.sizeHint` is
bounded to 24 lines of text. The rows are children of their column widget,
so tests ask `panel.isAncestorOf(w)`, not `w.parent() is panel`.

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QSize, Qt
from PySide6.QtWidgets import (QApplication, QBoxLayout, QFrame, QHBoxLayout,
                               QLabel, QScrollArea, QVBoxLayout, QWidget)

from .ornaments import anchored_popup_pos, popup_bounds

COLUMN_MIN_W = 280
COLUMN_GAP = 12
STACKED_GAP = 6
MIN_ROOM = 120  # never cap the panel below this, however short the window


class OptionsPanel(QWidget):
    """Scaffolding for the top bar's settings. See the module docstring."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("OptionsPanel")
        outer = QVBoxLayout(self)
        # inside the panel's 1px QSS border, or a scroll bar draws over it
        outer.setContentsMargins(1, 1, 1, 1)
        self._scroll = QScrollArea(self)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self._scroll)
        self._body = QWidget()
        self._body.setObjectName("OptionsBody")
        self._cols_lay = QHBoxLayout(self._body)
        self._cols_lay.setContentsMargins(10, 10, 10, 10)
        self._cols_lay.setSpacing(COLUMN_GAP)
        self._scroll.setWidget(self._body)
        self._body.installEventFilter(self)
        self._fitted = QSize()  # the body's hint when `_fit` last ran
        self._col = None
        self._lay = None
        self._stacked = False
        self._col_seps: list[tuple[QFrame, QFrame]] = []
        self._anchor = None
        self.add_column()

    # --- building blocks, called by TopBar.__init__ ----------------------

    def add_column(self) -> QWidget:
        """Start a new column to the right. The rows added after this land in
        it. Columns align to the top, so a shorter one leaves its gap at the
        bottom instead of spreading its rows apart."""
        if self._col is not None:
            # a vertical rule between columns, and the horizontal one that
            # replaces it when a narrow window stacks them (`_set_stacked`)
            vline = QFrame(self._body)
            vline.setObjectName("OptionsColSep")
            vline.setFrameShape(QFrame.Shape.VLine)
            vline.setFrameShadow(QFrame.Shadow.Plain)
            vline.setFixedWidth(1)
            hline = QFrame(self._body)
            hline.setObjectName("OptionsSep")
            hline.setFrameShape(QFrame.Shape.HLine)
            hline.setFrameShadow(QFrame.Shadow.Plain)
            hline.setFixedHeight(1)
            hline.setVisible(self._stacked)
            vline.setVisible(not self._stacked)
            self._cols_lay.addWidget(vline)
            self._cols_lay.addWidget(hline)
            self._col_seps.append((vline, hline))
        self._col = QWidget(self._body)
        self._col.setMinimumWidth(COLUMN_MIN_W)
        self._lay = QVBoxLayout(self._col)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)
        self._cols_lay.addWidget(self._col, 1, Qt.AlignmentFlag.AlignTop)
        return self._col

    def add_section(self, title: str) -> QLabel:
        """A dim uppercase caption introducing the rows below it."""
        if self._lay.count():
            self._lay.addSpacing(6)
        label = QLabel(title.upper(), self._col)
        label.setObjectName("OptionsSection")
        self._lay.addWidget(label)
        return label

    def add_switch_row(self, label, switch, *extra) -> QHBoxLayout:
        """A named row ending in a `ToggleSwitch`: label left, switch right.

        Unlike `add_row`, the label is a widget the caller already built (so
        its text can keep changing, like the bell glyph flipping with the
        chime switch) rather than a string this method turns into one. Both
        widgets are parented straight to the column, like every other setting
        here - a caller that wants to hide the whole row (`set_recovery_
        available` hiding the two limit-recovery switches for a no-Claude-
        login user) hides both widgets itself. `extra` widgets (the chimes'
        sound button) sit just left of the switch."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        label.setParent(self._col)
        row.addWidget(label)
        row.addStretch(1)
        for w in extra:
            w.setParent(self._col)
            row.addWidget(w)
        switch.setParent(self._col)
        row.addWidget(switch)
        self._lay.addLayout(row)
        return row

    def add_row(self, label: str, *widgets) -> QHBoxLayout:
        """A named row: caption on the left, controls right-aligned."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        if label:
            cap = QLabel(label, self._col)
            cap.setObjectName("OptionsRowLabel")
            row.addWidget(cap)
        row.addStretch(1)
        for w in widgets:
            w.setParent(self._col)
            row.addWidget(w)
        self._lay.addLayout(row)
        return row

    def set_row_visible(self, row: QHBoxLayout, visible: bool) -> None:
        """Show or hide every widget of a row `add_row` returned. A row with
        nothing visible takes no height, spacing included. A popup never
        resizes itself when its layout changes, so an open panel is sized
        and placed again, or a hidden row leaves a gap and a shown one
        grows past the window's bottom edge."""
        for i in range(row.count()):
            w = row.itemAt(i).widget()
            if w is not None:
                w.setVisible(visible)
        if self.isVisible() and self._anchor is not None:
            self._fit()

    def add_widget(self, widget) -> None:
        """A widget that spans the column on its own (a caption, a pill)."""
        widget.setParent(self._col)
        self._lay.addWidget(widget)

    def add_separator(self) -> QFrame:
        line = QFrame(self._col)
        line.setObjectName("OptionsSep")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        self._lay.addSpacing(4)
        self._lay.addWidget(line)
        return line

    # --- sizing ----------------------------------------------------------

    def _room(self) -> QSize:
        """The most room the panel may take: from under the anchor to the
        bottom of the window intersected with the screen's available
        geometry, the bound `anchored_popup_pos` uses, and that bound's full
        width. Measured from the anchor, not the window's top, so a capped
        panel still drops under the Options button instead of flipping up
        over the bar."""
        anchor = self._anchor
        if anchor is None:
            avail = (self.screen() or QApplication.primaryScreen()
                     ).availableGeometry()
            return QSize(avail.width(), max(MIN_ROOM, avail.height()))
        left, top, right, bottom = popup_bounds(anchor)
        # the row `anchored_popup_pos` puts the panel's top on
        top = max(top, anchor.mapToGlobal(anchor.rect().bottomLeft()).y())
        return QSize(right - left, max(MIN_ROOM, bottom - top))

    def _bar_w(self) -> int:
        return self._scroll.verticalScrollBar().sizeHint().width()

    def _set_stacked(self, stacked: bool) -> None:
        """Columns side by side, or one under the other with a horizontal
        rule between them. A window narrower than the two columns stacks
        them, and the height cap then scrolls the taller panel, so no
        setting ends up past the window's right edge."""
        self._stacked = stacked
        self._cols_lay.setDirection(
            QBoxLayout.Direction.TopToBottom if stacked
            else QBoxLayout.Direction.LeftToRight)
        self._cols_lay.setSpacing(STACKED_GAP if stacked else COLUMN_GAP)
        for vline, hline in self._col_seps:
            vline.setVisible(not stacked)
            hline.setVisible(stacked)
        self._cols_lay.activate()

    def sizeHint(self) -> QSize:
        """Every row at its natural size, or, when that is taller than the
        room there is, that room plus a scroll bar's width, so the bar does
        not squeeze the rows sideways."""
        self._cols_lay.activate()
        m = self.layout().contentsMargins()
        body = self._body.sizeHint().grownBy(m)
        room = self._room()
        if body.height() <= room.height():
            return body
        return QSize(body.width() + self._bar_w(), room.height())

    def _fit(self) -> None:
        # side by side when the columns (and a scroll bar, should the
        # height cap need one) fit the width; stacked otherwise
        self._set_stacked(False)
        room = self._room()
        if self._col_seps and self.sizeHint().width() > room.width():
            self._set_stacked(True)
        self.resize(self.sizeHint())
        self.move(anchored_popup_pos(self._anchor, self.size()))
        self._fitted = self._body.sizeHint()

    def eventFilter(self, obj, event) -> bool:
        """Fit an open panel again when its rows change size outside
        `set_row_visible`: the update pill appearing, a label's text
        growing. A popup never resizes itself, and the scroll area would
        clip the switches at the right edge. Only on a changed hint,
        because `_fit` posts layout requests of its own."""
        if (obj is self._body and event.type() == QEvent.Type.LayoutRequest
                and self.isVisible() and self._anchor is not None
                and self._body.sizeHint() != self._fitted):
            self._fit()
        return super().eventFilter(obj, event)

    # --- opening ---------------------------------------------------------

    def open_under(self, anchor) -> None:
        """Drop under `anchor`, clamped into the window and the screen by the
        same helper the layout palette uses."""
        self._anchor = anchor
        # a close by an anchor click (below) left this set; any other outside
        # click must still reach whatever it landed on
        self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, False)
        self._fit()
        self.show()
        self.raise_()

    def toggle_under(self, anchor) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.open_under(anchor)

    def mousePressEvent(self, event) -> None:
        """A click on the Options button while the panel is open closes it.

        Qt's popup grab routes that press here first, the base handler closes
        the panel as an outside click, and then Qt REPLAYS the press to the
        button. The button fired `clicked`, found the panel hidden and opened
        it again, so the second click looked like it did nothing.
        `WA_NoMouseReplay` drops the replay for presses on the anchor only,
        the same trick QComboBox plays for its own arrow."""
        anchor = self._anchor
        if anchor is not None and anchor.isVisible():
            hit = QRect(anchor.mapToGlobal(anchor.rect().topLeft()),
                        anchor.size())
            if hit.contains(event.globalPosition().toPoint()):
                self.setAttribute(Qt.WidgetAttribute.WA_NoMouseReplay, True)
        super().mousePressEvent(event)
