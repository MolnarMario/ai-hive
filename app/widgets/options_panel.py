"""The Options panel: every top-bar setting, stacked, in one anchored popup.

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

No em dash in any string below: this is all read by the user.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .ornaments import anchored_popup_pos

PANEL_MIN_W = 300


class OptionsPanel(QWidget):
    """Scaffolding for the top bar's settings. See the module docstring."""

    def __init__(self, parent=None):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setObjectName("OptionsPanel")
        self.setMinimumWidth(PANEL_MIN_W)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(10, 10, 10, 10)
        self._lay.setSpacing(4)

    # --- building blocks, called by TopBar.__init__ ----------------------

    def add_section(self, title: str) -> QLabel:
        """A dim uppercase caption introducing the rows below it."""
        if self._lay.count():
            self._lay.addSpacing(6)
        label = QLabel(title.upper(), self)
        label.setObjectName("OptionsSection")
        self._lay.addWidget(label)
        return label

    def add_switch_row(self, label, switch) -> None:
        """A named row ending in a `ToggleSwitch`: label left, switch right.

        Unlike `add_row`, the label is a widget the caller already built (so
        its text can keep changing, like the bell glyph flipping with the
        chime switch) rather than a string this method turns into one. Both
        widgets are parented straight to the panel, like every other setting
        here - a caller that wants to hide the whole row (`set_recovery_
        available` hiding the two limit-recovery switches for a no-Claude-
        login user) hides both widgets itself."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        label.setParent(self)
        row.addWidget(label)
        row.addStretch(1)
        switch.setParent(self)
        row.addWidget(switch)
        self._lay.addLayout(row)

    def add_row(self, label: str, *widgets) -> QHBoxLayout:
        """A named row: caption on the left, controls right-aligned."""
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        if label:
            cap = QLabel(label, self)
            cap.setObjectName("OptionsRowLabel")
            row.addWidget(cap)
        row.addStretch(1)
        for w in widgets:
            w.setParent(self)
            row.addWidget(w)
        self._lay.addLayout(row)
        return row

    def add_widget(self, widget) -> None:
        """A widget that spans the panel on its own (a caption, a pill)."""
        widget.setParent(self)
        self._lay.addWidget(widget)

    def add_separator(self) -> QFrame:
        line = QFrame(self)
        line.setObjectName("OptionsSep")
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        line.setFixedHeight(1)
        self._lay.addSpacing(4)
        self._lay.addWidget(line)
        return line

    # --- opening ---------------------------------------------------------

    def open_under(self, anchor) -> None:
        """Drop under `anchor`, clamped into the window and the screen by the
        same helper the layout palette uses."""
        self.adjustSize()
        self.move(anchored_popup_pos(anchor, self.size()))
        self.show()
        self.raise_()

    def toggle_under(self, anchor) -> None:
        """Click the button again to put the panel away. Qt's popup grab makes
        a second click on the button arrive as an outside-click that closes the
        panel first, so by the time the button fires, `isVisible()` is already
        False and this reads as a plain open. Harmless, and it means the button
        never gets stuck showing an open panel that is not there."""
        if self.isVisible():
            self.hide()
        else:
            self.open_under(anchor)
