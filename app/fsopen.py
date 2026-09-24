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


def explorer_select_cmdline(path: str) -> str:
    """The raw command line that makes Explorer open `path`'s folder with it
    selected. Built as a STRING, never an argv list: `subprocess` quotes any
    argv entry containing a space, so ["explorer", "/select,C:\\AI Projects\\x"]
    reaches Explorer as `"/select,C:\\AI Projects\\x"`. Explorer does not
    recognise a quoted switch, ignores it and opens its default location
    (Documents), which is exactly the reported bug. Only the PATH is quoted."""
    return f'explorer /select,"{os.path.normpath(os.path.abspath(path))}"'


def _shell_select(path: str) -> bool:
    """Select `path` in its folder through the shell API
    (SHOpenFolderAndSelectItems). Preferred over the command line because it
    takes a parsed item, so a comma in a file name (which Explorer's own
    `/select,` parser splits on) cannot misroute it. Returns False on any
    failure so the caller can fall back."""
    try:
        import ctypes
        from ctypes import wintypes
        ole32 = ctypes.WinDLL("ole32")  # WinDLL: CoTaskMemFree returns void
        shell32 = ctypes.WinDLL("shell32")
        # Qt's GUI thread is already an STA; this returns S_FALSE there, or
        # RPC_E_CHANGED_MODE on an MTA thread, both fine for this call.
        ole32.CoInitializeEx(None, 0x2)  # COINIT_APARTMENTTHREADED
        pidl = ctypes.c_void_p()
        shell32.SHParseDisplayName.argtypes = [
            wintypes.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
            wintypes.ULONG, ctypes.POINTER(wintypes.ULONG)]
        shell32.SHParseDisplayName.restype = ctypes.c_long
        hr = shell32.SHParseDisplayName(
            os.path.normpath(os.path.abspath(path)), None,
            ctypes.byref(pidl), 0, None)
        if hr != 0 or not pidl:
            return False
        try:
            shell32.SHOpenFolderAndSelectItems.argtypes = [
                ctypes.c_void_p, wintypes.UINT, ctypes.c_void_p, wintypes.DWORD]
            shell32.SHOpenFolderAndSelectItems.restype = ctypes.c_long
            # cidl=0 with the item's own absolute pidl: open its parent and
            # select it
            return shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree.restype = None
            ole32.CoTaskMemFree(pidl)
    except (OSError, AttributeError, ImportError):
        return False


def reveal_in_folder(path: str) -> None:
    """Show the file selected in its containing folder (the shell API on
    Windows, with an Explorer `/select,` command line as the fallback; opens
    the parent directory elsewhere)."""
    if not path or not os.path.exists(path):
        return
    try:
        if os.name == "nt":
            if not _shell_select(path):
                subprocess.Popen(explorer_select_cmdline(path))
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path)))
    except OSError:
        pass
