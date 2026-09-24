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

CUSTOM SOUNDS. Each chime can be swapped for the user's own WAV or MP3 (the
Options panel's note button). The caller owns WHERE that file lives (it copies
it into app data) and passes its path to `play`; this module only checks a
file with `validate` and plays it. WAV goes through winsound like the built-in
sounds. MP3 goes through Windows' MCI (winmm.dll via ctypes), still stdlib,
run on its own thread because MCI calls block. A custom file that is missing or will not play falls back to the
built-in sound for that kind, so swapping a sound can never lose a
notification.
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import struct
import tempfile
import threading
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
KINDS = (QUESTION, REPLY)

# What a custom sound may be. 5 s keeps a chime a chime (a new one cuts the
# last off anyway), and 2 MB is far more than 5 s of any sane WAV or MP3.
CUSTOM_EXTS = (".wav", ".mp3")
MAX_CUSTOM_BYTES = 2 * 1024 * 1024
MAX_CUSTOM_SECONDS = 5.0

# the one MCI device custom MP3s play on, and a second one for probing a file
# in validate() without disturbing a chime that is playing
_MCI_ALIAS = "aihive_chime"
_MCI_PROBE = "aihive_probe"

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


def _mci(command: str) -> tuple[int, str]:
    """Send one MCI command string; return (error code, reply). 0 is success.
    Anything short of a working winmm (another platform, a broken audio
    stack) comes back as a non-zero code so every caller takes its fallback."""
    try:
        buf = ctypes.create_unicode_buffer(256)
        rc = ctypes.windll.winmm.mciSendStringW(command, buf, 255, None)
        return int(rc), buf.value
    except Exception:  # noqa: BLE001 - no winmm means "cannot play this"
        return -1, ""


# ---- the MCI player thread ----------------------------------------------
# MCI calls block: measured on this machine, the first open of the mpegvideo
# device took 414 ms and a replay from 0 took 81 ms. That is a visible freeze
# on the GUI thread, so every command on _MCI_ALIAS runs on ONE daemon thread
# fed by a queue. One thread, because MCI binds a device to the thread that
# opened it. validate() probes on the caller's thread with its own alias,
# opened and closed there.

_mci_jobs: "queue.Queue | None" = None
_mci_lock = threading.Lock()

# path currently open on _MCI_ALIAS (touched only on the player thread), so a
# repeat chime replays it instead of paying the open again
_mci_open_path: str | None = None


def _mci_worker(jobs: "queue.Queue") -> None:
    while True:
        job, done = jobs.get()
        try:
            job()
        except Exception:  # noqa: BLE001 - one bad job must not kill the player
            pass
        finally:
            if done is not None:
                done.set()


def _mci_submit(job, wait: float = 0.0) -> None:
    """Run `job` on the player thread. With `wait`, block up to that many
    seconds for it to finish (stop() needs the device closed)."""
    global _mci_jobs
    with _mci_lock:
        if _mci_jobs is None:
            _mci_jobs = queue.Queue()
            threading.Thread(target=_mci_worker, args=(_mci_jobs,),
                             name="aihive-chime-mci", daemon=True).start()
    done = threading.Event() if wait else None
    _mci_jobs.put((job, done))
    if done is not None:
        done.wait(wait)


def _flush(timeout: float = 2.0) -> None:
    """Wait for every queued MCI job to finish. For tests."""
    _mci_submit(lambda: None, wait=timeout)


def _mci_close() -> None:
    global _mci_open_path
    if _mci_open_path is not None:
        _mci(f"close {_MCI_ALIAS}")
        _mci_open_path = None


def _play_mp3(path: str) -> bool:
    """Player thread only."""
    global _mci_open_path
    if _mci_open_path != path:
        _mci_close()
        if _mci(f'open "{path}" type mpegvideo alias {_MCI_ALIAS}')[0] != 0:
            return False
        _mci_open_path = path
    return _mci(f"play {_MCI_ALIAS} from 0")[0] == 0


def _stop_winsound() -> None:
    if winsound is not None:
        try:
            winsound.PlaySound(None, 0)
        except Exception:  # noqa: BLE001
            pass


def stop() -> None:
    """Silence whatever is playing and let go of any custom file. Waits (up
    to 2 s) for the player thread to close its device, so a caller may delete
    the file straight after."""
    if _mci_jobs is not None:
        _mci_submit(_mci_close, wait=2.0)
    _stop_winsound()


def validate(path: str) -> str | None:
    """Check a file the user picked as a custom chime. Returns None if it is
    usable, else a short reason to show them (no em dash, the user reads it).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in CUSTOM_EXTS:
        return "Only WAV and MP3 files can be used as a chime."
    try:
        size = os.path.getsize(path)
    except OSError:
        return "The file could not be read."
    if size == 0:
        return "The file is empty."
    if size > MAX_CUSTOM_BYTES:
        return (f"The file is {size / 1024 / 1024:.1f} MB. A chime can be at "
                f"most {MAX_CUSTOM_BYTES // (1024 * 1024)} MB.")
    if ext == ".wav":
        try:
            with wave.open(path, "rb") as w:
                frames, rate = w.getnframes(), w.getframerate()
        except Exception:  # noqa: BLE001 - wave.Error, EOFError, OSError...
            return ("This WAV could not be opened. Only uncompressed (PCM) "
                    "WAV files play; try saving it as 16-bit PCM or as MP3.")
        if frames <= 0 or rate <= 0:
            return "This WAV has no sound in it."
        seconds = frames / rate
    else:
        if _mci(f'open "{path}" type mpegvideo alias {_MCI_PROBE}')[0] != 0:
            return "Windows could not open this MP3."
        try:
            _mci(f"set {_MCI_PROBE} time format milliseconds")
            rc, length = _mci(f"status {_MCI_PROBE} length")
        finally:
            _mci(f"close {_MCI_PROBE}")
        if rc != 0 or not length.strip().isdigit():
            return "Windows could not read this MP3's length."
        seconds = int(length) / 1000.0
        if seconds <= 0:
            return "This MP3 has no sound in it."
    if seconds > MAX_CUSTOM_SECONDS:
        return (f"The sound is {seconds:.1f} s long. A chime can be at most "
                f"{MAX_CUSTOM_SECONDS:g} s.")
    return None


def _play_builtin(kind: str) -> bool:
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


def play(kind: str = QUESTION, path: str | None = None) -> bool:
    """Play one of the chimes asynchronously (never blocks the caller).

    `path` is the user's custom sound for this kind, if they chose one. It
    falls back to the built-in sound when the file is gone or will not play.
    Returns True if playback was dispatched, False if unavailable/failed.
    WAVs go to winsound with SND_ASYNC, which hands the file to the OS mixer
    and returns at once. MP3s go to the MCI player thread, which rings the
    built-in sound itself if the MP3 will not open. A second call while one is
    still playing cuts the first off, which is the most a burst of agents
    finishing together can do.
    """
    if winsound is None:
        return False
    ext = os.path.splitext(path)[1].lower() if path else ""
    if path and ext == ".mp3" and os.path.isfile(path):
        _stop_winsound()   # the two players never overlap
        _mci_submit(lambda: _play_mp3(path) or _play_builtin(kind))
        return True
    if _mci_jobs is not None:
        _mci_submit(_mci_close)
    if path and ext == ".wav" and os.path.isfile(path):
        try:
            winsound.PlaySound(
                path,
                winsound.SND_FILENAME | winsound.SND_ASYNC
                | winsound.SND_NODEFAULT)
            return True
        except Exception:  # noqa: BLE001 - e.g. a WAV winsound rejects
            pass
    return _play_builtin(kind)
