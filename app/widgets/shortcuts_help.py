"""The keyboard shortcut list, opened from Options > Help > Keyboard shortcuts.

The window shortcuts are registered in `MainWindow` and the terminal keys in
`TerminalView.keyPressEvent`; this table only describes them. A smoke check
fails when a window `QShortcut` is missing from `WINDOW_ROWS`, so a new one
can't ship undocumented.

No em dash in any string below: this is all read by the user.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

TITLE = "Keyboard shortcuts"

# (keys, what they do). The keys of every window QShortcut appear verbatim
# (as QKeySequence.toString() spells them) in the first column.
WINDOW_ROWS = (
    ("Ctrl+T or Ctrl+Shift+T", "New terminal in the active workspace"),
    ("Ctrl+Shift+N", "New workspace"),
    ("Ctrl+Tab",
     "Next agent: left to right along a row, then the next row down, "
     "back to the first after the last"),
    ("Ctrl+Shift+Tab", "Previous agent, the same order backwards"),
    ("Ctrl+Up", "Maximize the selected agent"),
    ("Ctrl+Down", "Show every agent again"),
    ("Ctrl+Shift+B", "Show or hide the sidebar"),
    ("Ctrl+Shift+L", "Open the event log"),
    ("F11", "Fullscreen: only the agent grid"),
)

TERMINAL_ROWS = (
    ("Ctrl+C",
     "Copy the selection, or interrupt the agent when nothing is selected"),
    ("Ctrl+Shift+C", "Copy"),
    ("Ctrl+V or Ctrl+Shift+V", "Paste"),
    ("Ctrl+X", "Cut the selection"),
    ("Ctrl+A", "Highlight what you typed. Backspace then clears it"),
    ("Ctrl+Shift+A", "Select all text, scrollback included"),
    ("Ctrl+Z, Ctrl+Y", "Undo and redo your typing"),
    ("Shift+arrows", "Select text. Add Ctrl to select whole words"),
    ("Shift+PageUp, Shift+PageDown", "Scroll through the history"),
    ("Ctrl+F", "Find in the conversation. Enter or F3 goes to the next "
     "match, Shift+Enter or Shift+F3 to the previous, Esc closes"),
    ("Ctrl+Enter", "New line in the prompt"),
    ("Ctrl+Shift+Enter", "Send what you typed later, on a timer"),
    ("Shift+Tab", "Cycle Claude Code's modes"),
    ("Ctrl+Plus, Ctrl+Minus", "This agent's font size"),
    ("Ctrl+Click a path", "Open the file"),
)


def _table(rows) -> str:
    return "<table>" + "".join(
        f"<tr><td style='padding:2px 14px 2px 0'><b>{keys}</b></td>"
        f"<td style='padding:2px 0'>{what}</td></tr>"
        for keys, what in rows) + "</table>"


def shortcuts_html() -> str:
    return (
        "<p><b>Anywhere in the window</b></p>"
        f"{_table(WINDOW_ROWS)}"
        "<p><b>In a focused terminal</b></p>"
        f"{_table(TERMINAL_ROWS)}"
        "<p>Every other Ctrl and Alt key goes straight to the agent, as in "
        "a real terminal.</p>")


def show_shortcuts_help(parent=None) -> QMessageBox:
    """Open the list without blocking and return it. It deletes itself
    when closed."""
    box = QMessageBox(QMessageBox.Icon.NoIcon, TITLE, "", parent=parent)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(shortcuts_html())
    box.setStyleSheet("QLabel#qt_msgbox_label { min-width: 520px; }")
    box.setModal(False)
    box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    box.setObjectName("ShortcutsHelp")
    box.show()
    return box
