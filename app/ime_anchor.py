"""Tell the Windows input method where a terminal's cursor is.

Win+V (clipboard history) and Win+. (emoji) open next to the focused
window's IME composition point. Qt sets that point only while a CJK
composition is running, so over any Qt widget, a stock QLineEdit too, the
panel fell back to the bottom-right corner of the screen, which on a
stacked pair of monitors can be a whole screen away from the text.

Measured on Windows 11 (build 26300) with Qt 6.11: a system caret
(CreateCaret and SetCaretPos, shown or hidden) does not move the panel,
and the panel makes no UI Automation query. ImmSetCompositionWindow does
move it, and ImmSetCandidateWindow's exclude rectangle makes it open below
the cursor's whole row instead of over it. Tk's Entry sets both on every
caret move, which is why the panel opens at its caret.

Coordinates are client coordinates of the top-level window in physical
pixels. Qt-free and stdlib-only. Nothing here raises: off Windows, or when
the window has no input context, it reports False.
"""

from __future__ import annotations

import sys

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    class _CompositionForm(ctypes.Structure):
        _fields_ = [("dwStyle", wintypes.DWORD),
                    ("ptCurrentPos", wintypes.POINT),
                    ("rcArea", wintypes.RECT)]

    class _CandidateForm(ctypes.Structure):
        _fields_ = [("dwIndex", wintypes.DWORD),
                    ("dwStyle", wintypes.DWORD),
                    ("ptCurrentPos", wintypes.POINT),
                    ("rcArea", wintypes.RECT)]

CFS_POINT = 0x0002
CFS_EXCLUDE = 0x0080

_imm = None


def _lib():
    global _imm
    if _imm is None:
        imm = ctypes.WinDLL("imm32")
        imm.ImmGetContext.restype = ctypes.c_void_p
        imm.ImmGetContext.argtypes = [wintypes.HWND]
        imm.ImmReleaseContext.restype = wintypes.BOOL
        imm.ImmReleaseContext.argtypes = [wintypes.HWND, ctypes.c_void_p]
        imm.ImmSetCompositionWindow.restype = wintypes.BOOL
        imm.ImmSetCompositionWindow.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_CompositionForm)]
        imm.ImmSetCandidateWindow.restype = wintypes.BOOL
        imm.ImmSetCandidateWindow.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(_CandidateForm)]
        _imm = imm
    return _imm


def place(hwnd: int, left: int, top: int, right: int, bottom: int) -> bool:
    """Anchor hwnd's IME panels at the cell (left, top)-(right, bottom).

    The composition point is the cell's top-left corner, and the candidate
    window excludes the cell, so a panel opens just under the cursor row
    (or above it when there is no room below)."""
    if not _IS_WIN or not hwnd:
        return False
    try:
        imm = _lib()
        himc = imm.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            comp = _CompositionForm(CFS_POINT, wintypes.POINT(left, top),
                                    wintypes.RECT(0, 0, 0, 0))
            cand = _CandidateForm(0, CFS_EXCLUDE,
                                  wintypes.POINT(left, bottom),
                                  wintypes.RECT(left, top, right, bottom))
            ok = bool(imm.ImmSetCompositionWindow(himc, ctypes.byref(comp)))
            ok = bool(imm.ImmSetCandidateWindow(himc, ctypes.byref(cand))) and ok
            return ok
        finally:
            imm.ImmReleaseContext(hwnd, himc)
    except OSError:
        return False
