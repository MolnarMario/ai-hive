"""Notification chime — a short bell played when an agent needs the user.

AI Hive lights a "?" on a workspace row the moment an agent settles on a
prompt/question awaiting the user (see the waiting-badge invariant in
CLAUDE.md). That visual cue is easy to miss while you are heads-down in
another workspace, so this module gives it an audible partner: a soft
two-note bell that fires on the RISING edge of that state (standby ->
waiting), never while an agent is actively working.

Qt-free and stdlib-only on purpose (like providers.py / mcp_server.py) — it
must be importable from the model layer's tests without a running GUI. Sound
is Windows-first via `winsound`: the chime WAV is synthesised ONCE into the
temp dir (no bundled asset, no external player) and played with SND_ASYNC so
the UI thread never blocks. Everything degrades to a silent no-op if the
platform has no `winsound` or audio is unavailable — a missing chime must
never break the app or a headless test run.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import wave

try:  # Windows-only stdlib module; absent on other platforms
    import winsound
except Exception:  # noqa: BLE001 - any import failure means "no audio here"
    winsound = None  # type: ignore[assignment]

_SAMPLE_RATE = 44100
# bump this when the waveform changes so a stale cached WAV is regenerated
_CHIME_VERSION = 1
_CHIME_PATH = os.path.join(tempfile.gettempdir(),
                           f"aihive-chime-v{_CHIME_VERSION}.wav")

# two ascending notes (a rising perfect fourth) — a gentle "di-ding" that
# reads as a question/attention cue rather than an alarm. Frequencies in Hz.
_NOTE_A = 784.0    # G5
_NOTE_B = 1046.5   # C6


def _bell_samples() -> list[int]:
    """Synthesise the two-note bell as 16-bit signed PCM samples.

    Each note is a fundamental plus a soft second harmonic under an
    exponential decay envelope (fast attack, long-ish ring) so it sounds
    struck, not buzzed. The second note starts before the first fully fades
    so the two blend into one chime.
    """
    note_len = 0.30          # seconds per note
    gap = 0.13               # start-to-start spacing (notes overlap)
    total = gap + note_len
    n_total = int(_SAMPLE_RATE * total)
    buf = [0.0] * n_total

    def add_note(start_s: float, freq: float, amp: float) -> None:
        start = int(_SAMPLE_RATE * start_s)
        n = int(_SAMPLE_RATE * note_len)
        for i in range(n):
            idx = start + i
            if idx >= n_total:
                break
            t = i / _SAMPLE_RATE
            # ~4 ms linear attack, then exponential decay (~6/s)
            attack = min(1.0, t / 0.004)
            env = attack * math.exp(-6.0 * t)
            tone = (math.sin(2 * math.pi * freq * t)
                    + 0.35 * math.sin(2 * math.pi * 2 * freq * t))
            buf[idx] += amp * env * tone

    add_note(0.0, _NOTE_A, 0.55)
    add_note(gap, _NOTE_B, 0.65)

    # normalise to avoid clipping from the overlap/harmonic sum, then quantise
    peak = max((abs(v) for v in buf), default=1.0) or 1.0
    scale = 0.9 * 32767 / peak
    return [int(v * scale) for v in buf]


def _ensure_chime() -> str | None:
    """Write the chime WAV to the temp dir if it isn't there yet; return its
    path (or None if writing failed — the caller then stays silent)."""
    if os.path.exists(_CHIME_PATH) and os.path.getsize(_CHIME_PATH) > 0:
        return _CHIME_PATH
    try:
        samples = _bell_samples()
        with wave.open(_CHIME_PATH, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)          # 16-bit
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        return _CHIME_PATH
    except Exception:  # noqa: BLE001 - a failed write must not raise into the UI
        return None


def available() -> bool:
    """True when a chime can actually be played on this platform."""
    return winsound is not None


def play() -> bool:
    """Play the notification chime asynchronously (never blocks the caller).

    Returns True if playback was dispatched, False if unavailable/failed.
    Safe to call from the GUI thread: SND_ASYNC hands the WAV to the OS mixer
    and returns immediately.
    """
    if winsound is None:
        return False
    path = _ensure_chime()
    try:
        if path is not None:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC
                | winsound.SND_NODEFAULT)
        else:  # synthesis/write failed — fall back to the system asterisk
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return True
    except Exception:  # noqa: BLE001 - audio glitches must stay silent, not crash
        return False
