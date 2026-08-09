"""Windows taskbar overlay icon: the small badge Windows draws in the corner
of an app's taskbar button.

Why this module exists at all: every "who is working" signal AI Hive has lives
INSIDE the window (the sidebar's pulsing count badge, the WorkspaceSpinner).
That is exactly the wrong place when the user is in another app waiting for the
hive to finish -- they have to raise the window to learn whether anything is
still running. The taskbar button is visible from everywhere, so the working
count goes there.

Qt-free and stdlib-only on purpose, like `chime.py` and `limit_banner.py`: this
is raw Win32/COM plumbing, and keeping Qt out of it means the painting side
(which needs QPainter) can be tested and reasoned about separately. The caller
hands us finished pixels; we own the HICON and the COM pointer.

THE CONSTRAINT THAT SHAPES THE FEATURE: Windows gives an app exactly ONE
overlay, and its position is fixed (bottom-right of the taskbar button). There
is no second slot and no way to choose a corner, so any additional state -- ours
is "an agent is waiting on a question" -- has to be encoded INTO that single
square rather than beside it. Hence the caller's colour swap.

Qt 6 removed QtWinExtras (`QWinTaskbarButton`), which is what would have done
this on Qt 5, so the ITaskbarList3 call is made by hand through ctypes. `main.py`
already reaches into Win32 the same way for the AppUserModelID and the relaunch
properties.

NOTHING HERE RAISES. Every entry point returns a bool and swallows its failures,
the same contract as `claude_usage.fetch`: a taskbar decoration is never worth
taking the app down for, and on a non-Windows dev run the whole module is a
silent no-op.
"""

from __future__ import annotations

import sys

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

# ---------------------------------------------------------------- COM bits ---

# CLSID_TaskbarList {56FDF344-FD6D-11D0-958A-006097C9A090}
# IID_ITaskbarList3 {EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}
_CLSID_TASKBAR_LIST = "{56FDF344-FD6D-11D0-958A-006097C9A090}"
_IID_ITASKBAR_LIST3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"

# ITaskbarList3 vtable layout. The three IUnknown slots come first, then the
# interface is built up by inheritance: ITaskbarList (5 methods), then
# ITaskbarList2 (1), then ITaskbarList3's own. SetOverlayIcon is the 10th of
# ITaskbarList3's, i.e. slot 3+5+1+9 = 18. Getting this index wrong calls a
# DIFFERENT method with our arguments, so it is spelled out rather than guessed.
_VT_HRINIT = 3
_VT_SET_OVERLAY_ICON = 18

_CLSCTX_INPROC_SERVER = 0x1
_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0

_SM_CXSMICON = 49

_BI_RGB = 0
_DIB_RGB_COLORS = 0


if _IS_WIN:

    class _GUID(ctypes.Structure):
        _fields_ = [("Data1", ctypes.c_uint32),
                    ("Data2", ctypes.c_uint16),
                    ("Data3", ctypes.c_uint16),
                    ("Data4", ctypes.c_ubyte * 8)]

    class _BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", ctypes.c_uint32),
                    ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32),
                    ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16),
                    ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32),
                    ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32),
                    ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

    class _BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", _BITMAPINFOHEADER),
                    ("bmiColors", ctypes.c_uint32 * 3)]

    class _ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL),
                    ("xHotspot", wintypes.DWORD),
                    ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP),
                    ("hbmColor", wintypes.HBITMAP)]

    def _bind() -> None:
        """Declare argument/return types for every Win32 call used here.

        Not optional tidiness: on 64-bit, a GDI/icon HANDLE does not fit ctypes'
        default `c_int` argument, so passing an HBITMAP straight back to
        DeleteObject raises "int too long to convert" and the bitmaps leak on
        every single badge push.
        """
        g, u, o = ctypes.windll.gdi32, ctypes.windll.user32, ctypes.windll.ole32
        u.GetDC.argtypes = [wintypes.HWND]
        u.GetDC.restype = wintypes.HDC
        u.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        u.ReleaseDC.restype = ctypes.c_int
        u.GetSystemMetrics.argtypes = [ctypes.c_int]
        u.GetSystemMetrics.restype = ctypes.c_int
        u.CreateIconIndirect.argtypes = [ctypes.POINTER(_ICONINFO)]
        u.CreateIconIndirect.restype = wintypes.HICON
        u.DestroyIcon.argtypes = [wintypes.HICON]
        u.DestroyIcon.restype = wintypes.BOOL
        g.CreateDIBSection.argtypes = [wintypes.HDC,
                                       ctypes.POINTER(_BITMAPINFO),
                                       wintypes.UINT,
                                       ctypes.POINTER(ctypes.c_void_p),
                                       wintypes.HANDLE, wintypes.DWORD]
        g.CreateDIBSection.restype = wintypes.HBITMAP
        g.CreateBitmap.argtypes = [ctypes.c_int, ctypes.c_int, wintypes.UINT,
                                   wintypes.UINT, ctypes.c_void_p]
        g.CreateBitmap.restype = wintypes.HBITMAP
        g.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        g.DeleteObject.restype = wintypes.BOOL
        o.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
        o.CLSIDFromString.restype = ctypes.c_long
        o.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        o.CoInitializeEx.restype = ctypes.c_long
        o.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p,
                                       wintypes.DWORD, ctypes.POINTER(_GUID),
                                       ctypes.POINTER(ctypes.c_void_p)]
        o.CoCreateInstance.restype = ctypes.c_long

    try:
        _bind()
    except Exception:      # a bind failure would only surface as a later no-op
        pass


# ------------------------------------------------------------------ state ---

_taskbar = None          # cached ITaskbarList3*, once HrInit has succeeded
_taskbar_failed = False  # a failed create is permanent; don't retry per push
_current_icon = None     # the HICON currently on the button, ours to destroy


def available() -> bool:
    """True when an overlay can plausibly be set (Windows, COM reachable)."""
    return _IS_WIN and not _taskbar_failed


def overlay_size() -> int:
    """The pixel size Windows wants an overlay icon to be.

    SM_CXSMICON is already DPI-scaled for the process (16 at 100%, 24 at 150%),
    so asking for it means the digit is drawn at native resolution instead of
    being resampled by the shell, which at this size turns a "3" into a smudge.
    """
    if not _IS_WIN:
        return 16
    try:
        n = int(ctypes.windll.user32.GetSystemMetrics(_SM_CXSMICON))
        return n if 8 <= n <= 256 else 16
    except Exception:
        return 16


def _ensure_com() -> None:
    """Join the STA.

    Qt has normally already done this on the GUI thread, so S_FALSE (already
    initialized) and RPC_E_CHANGED_MODE (initialized in another apartment
    model) are both fine. Nothing is checked here on purpose: CoCreateInstance
    is the real test of whether the interface is reachable, and it reports its
    own failure.
    """
    try:
        ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
    except Exception:
        pass


def _guid(text: str):
    g = _GUID()
    hr = ctypes.windll.ole32.CLSIDFromString(ctypes.c_wchar_p(text),
                                             ctypes.byref(g))
    if hr != _S_OK:
        raise OSError("bad guid " + text)
    return g


def _taskbar_list():
    """The process-wide ITaskbarList3, created once."""
    global _taskbar, _taskbar_failed
    if _taskbar is not None or _taskbar_failed:
        return _taskbar
    try:
        _ensure_com()
        ptr = ctypes.c_void_p()
        hr = ctypes.windll.ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_TASKBAR_LIST)), None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(_IID_ITASKBAR_LIST3)), ctypes.byref(ptr))
        if hr != _S_OK or not ptr.value:
            raise OSError(f"CoCreateInstance 0x{hr & 0xFFFFFFFF:08x}")
        # HrInit() must run before any other method on the interface.
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)
        vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_void_p))[0]
        slot = ctypes.cast(vtable,
                           ctypes.POINTER(ctypes.c_void_p))[_VT_HRINIT]
        if proto(slot)(ptr) != _S_OK:
            raise OSError("HrInit failed")
        _taskbar = ptr
    except Exception:
        _taskbar_failed = True
        _taskbar = None
    return _taskbar


# ------------------------------------------------------------------ HICON ---

def _hicon_from_bgra(width: int, height: int, data: bytes):
    """Build an HICON from premultiplied BGRA pixels (top-down rows).

    The colour half is a 32bpp DIB section, which is what carries the alpha --
    the disc has to be round on whatever taskbar colour the user runs, so a
    1-bit mask alone would not do. `CreateIconIndirect` honours that alpha, so
    the mask bitmap is a zero-filled monochrome dummy of the right size.
    """
    if width <= 0 or height <= 0 or len(data) < width * height * 4:
        return None
    gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
    hbm_color = hbm_mask = None
    hdc = user32.GetDC(None)
    try:
        bmi = _BITMAPINFO()
        h = bmi.bmiHeader
        h.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        h.biWidth = width
        h.biHeight = -height      # negative == top-down, matching QImage rows
        h.biPlanes = 1
        h.biBitCount = 32
        h.biCompression = _BI_RGB

        bits = ctypes.c_void_p()
        hbm_color = gdi32.CreateDIBSection(
            hdc, ctypes.byref(bmi), _DIB_RGB_COLORS,
            ctypes.byref(bits), None, 0)
        if not hbm_color or not bits:
            return None
        ctypes.memmove(bits, data, width * height * 4)

        hbm_mask = gdi32.CreateBitmap(width, height, 1, 1, None)
        if not hbm_mask:
            return None

        info = _ICONINFO(True, 0, 0, hbm_mask, hbm_color)
        icon = user32.CreateIconIndirect(ctypes.byref(info))
        return icon or None
    except Exception:
        return None
    finally:
        # the icon owns copies; these are ours to free either way
        if hbm_color:
            gdi32.DeleteObject(hbm_color)
        if hbm_mask:
            gdi32.DeleteObject(hbm_mask)
        if hdc:
            user32.ReleaseDC(None, hdc)


def _destroy_current() -> None:
    global _current_icon
    if _current_icon:
        try:
            ctypes.windll.user32.DestroyIcon(_current_icon)
        except Exception:
            pass
    _current_icon = None


# ------------------------------------------------------------------- push ---

def set_overlay(hwnd: int, image, description: str = "") -> bool:
    """Put `image` on the window's taskbar button, or clear it with None.

    `image` is `(width, height, premultiplied-BGRA bytes)`; `description` is the
    accessibility text Windows reads out for the overlay (screen readers see it;
    it is not a visible tooltip).

    The previous HICON is destroyed only AFTER the new one is on the button:
    the shell reads the handle during the call, so freeing first would hand it
    a dead icon. Returns False on any failure, having changed nothing.
    """
    global _current_icon
    if not _IS_WIN or not hwnd:
        return False
    tb = _taskbar_list()
    if tb is None:
        return False

    icon = None
    if image is not None:
        try:
            width, height, data = image
        except Exception:
            return False
        icon = _hicon_from_bgra(int(width), int(height), data)
        if icon is None:
            return False
    try:
        proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p,
                                   wintypes.HWND, wintypes.HICON,
                                   ctypes.c_wchar_p)
        vtable = ctypes.cast(tb, ctypes.POINTER(ctypes.c_void_p))[0]
        slot = ctypes.cast(
            vtable, ctypes.POINTER(ctypes.c_void_p))[_VT_SET_OVERLAY_ICON]
        hr = proto(slot)(tb, wintypes.HWND(hwnd), wintypes.HICON(icon or 0),
                         ctypes.c_wchar_p(description or None))
    except Exception:
        if icon:
            try:
                ctypes.windll.user32.DestroyIcon(icon)
            except Exception:
                pass
        return False

    if hr != _S_OK:
        if icon:
            try:
                ctypes.windll.user32.DestroyIcon(icon)
            except Exception:
                pass
        return False
    _destroy_current()
    _current_icon = icon
    return True


def clear(hwnd: int) -> bool:
    """Take the overlay off the button (the idle-hive state)."""
    return set_overlay(hwnd, None, "")


def shutdown(hwnd: int = 0) -> None:
    """Drop the overlay and release the interface. Safe to call twice."""
    global _taskbar
    if not _IS_WIN:
        return
    if hwnd:
        try:
            set_overlay(hwnd, None, "")
        except Exception:
            pass
    _destroy_current()
    if _taskbar is not None:
        try:
            proto = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
            vtable = ctypes.cast(_taskbar, ctypes.POINTER(ctypes.c_void_p))[0]
            slot = ctypes.cast(vtable, ctypes.POINTER(ctypes.c_void_p))[2]
            proto(slot)(_taskbar)   # IUnknown::Release
        except Exception:
            pass
        _taskbar = None
