"""Options > Keep the PC awake: stop Windows from sleeping while AI Hive runs.

Agents left working overnight die with the machine when it idles into sleep.
A playing video keeps a PC awake by holding a power request, and this does
the same with the cheapest call Windows has for it: one
`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`. No thread, no
timer, no fake input. The flag lives on the calling thread until it is
cleared or the process exits, so a crash or a kill releases it too.

Every call MUST come from the GUI thread. The state belongs to the thread
that set it, and a worker thread's state goes away when the worker ends.

Only the system is held, not the screen: the display still turns off and
locks on its normal timer, which costs nothing and saves the panel. The one
exception is a Modern Standby (S0 low power idle) machine on battery.
Microsoft's PowerSetRequest docs: "On Modern Standby systems on DC power,
system and execution required power requests are terminated 5 minutes after
the system sleep timeout has expired." A video survives that because it also
holds the display, and so does this, but only in that one case (`wanted`).
The caller watches the power source while it holds the system there.

User-initiated sleep (lid, power button, Start > Sleep) still sleeps. That
is Windows' rule for every power request, and the right one.

Qt-free and stdlib-only, like `taskbar_overlay.py`. Nothing here raises: off
Windows, or when a call fails, it reports False and the app carries on.
"""

from __future__ import annotations

import functools
import sys

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# byte offset of the AoAc ("always on, always connected", i.e. Modern
# Standby) BOOLEAN in SYSTEM_POWER_CAPABILITIES: 15 BOOLEANs, then the two
# throttle BYTEs, FastSystemS4, Hiberboot, WakeAlarmPresent, then AoAc
_AOAC_OFFSET = 20
# the struct is 76 bytes on Windows 10 and 11; the buffer has headroom in
# case a later SDK grows it, since GetPwrCapabilities takes no length
_CAPS_BUFFER = 256


def wanted(on: bool, on_battery: bool,
           modern_standby: bool) -> tuple[bool, bool]:
    """(hold the system, hold the display) for the switch and the machine.

    The display is held only where a system request alone would lapse: a
    Modern Standby machine running on battery (module docstring)."""
    if not on:
        return False, False
    return True, bool(on_battery and modern_standby)


def hold(system: bool, display: bool = False) -> bool:
    """Set this thread's execution state. Idempotent: it replaces the state
    rather than counting, so calling it twice holds nothing twice.
    hold(False, False) releases. True on success."""
    if not _IS_WIN:
        return False
    flags = ES_CONTINUOUS
    if system or display:
        # Windows needs the system request with the display one, or the
        # screen stays on while the machine sleeps under it
        flags |= ES_SYSTEM_REQUIRED
    if display:
        flags |= ES_DISPLAY_REQUIRED
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.SetThreadExecutionState
        fn.argtypes = [wintypes.DWORD]
        fn.restype = wintypes.DWORD
        return fn(flags) != 0      # the previous state, or NULL on failure
    except Exception:
        return False


def release() -> bool:
    return hold(False, False)


def on_battery() -> bool:
    """True when the machine is running on battery. Unknown counts as not."""
    if not _IS_WIN:
        return False

    class _PowerStatus(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                    ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte),
                    ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", wintypes.DWORD),
                    ("BatteryFullLifeTime", wintypes.DWORD)]

    try:
        status = _PowerStatus()
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        fn = k32.GetSystemPowerStatus
        fn.argtypes = [ctypes.POINTER(_PowerStatus)]
        fn.restype = wintypes.BOOL
        if not fn(ctypes.byref(status)):
            return False
        return status.ACLineStatus == 0     # 0 offline, 1 online, 255 unknown
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def modern_standby() -> bool:
    """True on a Modern Standby (S0 low power idle) machine. Read once: it is
    a firmware fact that cannot change while the app runs."""
    if not _IS_WIN:
        return False
    try:
        buf = (ctypes.c_ubyte * _CAPS_BUFFER)()
        fn = ctypes.WinDLL("powrprof").GetPwrCapabilities
        fn.argtypes = [ctypes.c_void_p]
        fn.restype = ctypes.c_ubyte       # BOOLEAN
        if not fn(ctypes.byref(buf)):
            return False
        return buf[_AOAC_OFFSET] != 0
    except Exception:
        return False
