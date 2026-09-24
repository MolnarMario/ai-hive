"""Notification chimes: short R2-style whistles played when an agent needs the
user, or has finished a reply.

AI Hive lights a "?" on a workspace row the moment an agent settles on a
prompt/question awaiting the user (see the waiting-badge invariant in
CLAUDE.md). That visual cue is easy to miss while you are heads-down in
another workspace, so this module gives it an audible partner. There are two
sounds, told apart by their shape:

  QUESTION  "doo-dee-bweep?"  two blips, then a whistle that is still sliding
            UP when it stops. Unfinished, like a raised eyebrow. Rings on the
            RISING edge of the waiting state (standby -> waiting).
  REPLY     "ta-da!"          two blips stepping up to a HELD high note with a
            slight vibrato. Lands firmly: "hey, check this out". Rings when a
            turn the user asked for has ended (see
            TerminalAgent.reply_finished). Off by default.

Qt-free and stdlib-only on purpose (like providers.py / mcp_server.py): it
must be importable from the model layer's tests without a running GUI. Sound
is Windows-first via `winsound`: each WAV is synthesised ONCE into the temp
dir (no bundled asset, no external player) and played with SND_ASYNC so the
UI thread never blocks. Everything degrades to a silent no-op if the platform
has no `winsound` or audio is unavailable. A missing chime must never break
the app or a headless test run.
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
# bump this when a waveform changes so a stale cached WAV is regenerated
_CHIME_VERSION = 2

QUESTION = "question"
REPLY = "reply"

# A note is (start_s, length_s, pitch_path, amp, vibrato). pitch_path is
# [(t, Hz), ...] relative to the note's start, slid between points on a log
# scale so equal times give equal musical intervals. vibrato is
# (rate_Hz, depth_fraction) or None.
_Note = tuple[float, float, list[tuple[float, float]], float,
              "tuple[float, float] | None"]


def _blip(start: float, hz: float) -> _Note:
    return (start, 0.06, [(0.0, hz)], 0.55, None)


_SOUNDS: dict[str, list[_Note]] = {
    QUESTION: [
        _blip(0.00, 1200.0),
        _blip(0.08, 950.0),
        (0.17, 0.20, [(0.0, 950.0), (0.18, 2000.0)], 0.7, None),
    ],
    REPLY: [
        _blip(0.00, 1046.5),   # C6
        _blip(0.08, 1318.5),   # E6
        (0.17, 0.25, [(0.0, 1568.0)], 0.7, (7.0, 0.015)),   # G6, held
    ],
}


def _pitch(path: list[tuple[float, float]], t: float) -> float:
    if t <= path[0][0]:
        return path[0][1]
    for (t0, f0), (t1, f1) in zip(path, path[1:]):
        if t < t1:
            return f0 * (f1 / f0) ** ((t - t0) / (t1 - t0))
    return path[-1][1]


def _samples(kind: str) -> list[int]:
    """Synthesise one sound as 16-bit signed PCM samples.

    Each note is a whistle: a sine plus a faint second harmonic, with a 5 ms
    attack and a 25 ms release and a flat level between. That is what makes it
    chirp like a droid rather than ring like a bell. Phase is accumulated per
    sample so a sliding pitch stays click-free.
    """
    notes = _SOUNDS[kind]
    end = max(start + length for start, length, *_ in notes) + 0.02
    buf = [0.0] * int(_SAMPLE_RATE * end)
    for start, length, path, amp, vib in notes:
        s0 = int(_SAMPLE_RATE * start)
        n = int(_SAMPLE_RATE * length)
        phase = 0.0
        for i in range(n):
            t = i / _SAMPLE_RATE
            f = _pitch(path, t)
            if vib is not None:
                f *= 1.0 + vib[1] * math.sin(2 * math.pi * vib[0] * t)
            phase += 2 * math.pi * f / _SAMPLE_RATE
            env = min(1.0, t / 0.005, (length - t) / 0.025)
            buf[s0 + i] += amp * env * (math.sin(phase)
                                        + 0.15 * math.sin(2 * phase))
    # normalise to avoid clipping from the harmonic sum, then quantise
    peak = max((abs(v) for v in buf), default=1.0) or 1.0
    scale = 0.9 * 32767 / peak
    return [int(v * scale) for v in buf]


def _chime_path(kind: str) -> str:
    return os.path.join(tempfile.gettempdir(),
                        f"aihive-chime-{kind}-v{_CHIME_VERSION}.wav")


def _ensure_chime(kind: str = QUESTION) -> str | None:
    """Write the sound's WAV to the temp dir if it isn't there yet; return its
    path (or None if writing failed, and the caller then stays silent)."""
    path = _chime_path(kind)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        samples = _samples(kind)
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)          # 16-bit
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(b"".join(struct.pack("<h", s) for s in samples))
        return path
    except Exception:  # noqa: BLE001 - a failed write must not raise into the UI
        return None


def available() -> bool:
    """True when a chime can actually be played on this platform."""
    return winsound is not None


def play(kind: str = QUESTION) -> bool:
    """Play one of the chimes asynchronously (never blocks the caller).

    Returns True if playback was dispatched, False if unavailable/failed.
    Safe to call from the GUI thread: SND_ASYNC hands the WAV to the OS mixer
    and returns immediately. A second call while one is still playing cuts the
    first off, which is the most a burst of agents finishing together can do.
    """
    if winsound is None:
        return False
    path = _ensure_chime(kind)
    try:
        if path is not None:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC
                | winsound.SND_NODEFAULT)
        else:  # synthesis/write failed, fall back to the system asterisk
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        return True
    except Exception:  # noqa: BLE001 - audio glitches must stay silent, not crash
        return False
