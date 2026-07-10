"""Open a file/folder through the OS shell — the single implementation shared by
the terminal view (Ctrl+Click a path), the Agent/File Map (double-click a file),
the sidebar file explorer (click a file), and the workspace folder button.

Previously each call site carried its own copy of the "Qt openUrl, then fall
back to os.startfile" dance; they are consolidated here so the fallback logic
(and its hard-won reasons) live in one place.

Depends on PySide6 (UI-side helper) but no app modules, so any widget can import
it without cycles.
"""

import os
import subprocess

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices


def open_path(path: str) -> bool:
    """Open a file (or folder) with the OS default program. Falls back to the
    Windows shell (os.startfile) when Qt's handler reports failure — Qt returns
    False for some file associations, which would otherwise make a click look
    dead. Returns True if an opener was invoked, False if the path is missing.
    Safe to call on a non-existent path (no-ops)."""
    if not path or not os.path.exists(path):
        return False
    if QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
        return True
    try:  # Windows-only shell open; harmless AttributeError elsewhere
        os.startfile(path)  # noqa: S606 - path is an existing local file
        return True
    except (OSError, AttributeError):
        return False


def open_url(url: str) -> bool:
    """Open a URL in the default browser (used by the terminal link handler)."""
    if not url:
        return False
    return bool(QDesktopServices.openUrl(QUrl(url)))


def open_with(path: str) -> None:
    """Windows shell 'Open with...' picker so the user can choose the program.
    Falls back to a plain open elsewhere."""
    if not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL",
                              os.path.normpath(path)])
        else:
            open_path(path)
    except OSError:
        pass


def reveal_in_folder(path: str) -> None:
    """Show the file selected in its containing folder (Explorer /select on
    Windows; opens the parent directory elsewhere)."""
    if not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", f"/select,{os.path.normpath(path)}"])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    except OSError:
        pass
