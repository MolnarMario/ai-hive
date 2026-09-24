"""One polling policy for every top-bar usage pill.

Claude, Gemini and GPT each used to carry their own timer, in-flight guard,
429 backoff and retune logic in `MainWindow`, three hand copies that had
already drifted apart (Gemini polled every 5 minutes, Claude every minute, GPT
had no backoff and no urgent rate). A pill added later would have been a
fourth copy. Now there is one `UsagePoller`, and a readout gets its cadence,
its retries and its grey state by registering a `UsageSource` with it. There
is no other way to put a polled number on the bar.

THE CADENCE (the user's rules, identical for every provider):

* `IDLE_POLL_MS` (6 min) while no agent of that provider has worked in the
  last `ACTIVE_GRACE_S`. AI Hive's own agents are then not moving the number,
  but the browser, the official apps or another machine still can, so it
  keeps polling at a crawl rather than stopping.
* `POLL_MS` (90 s) while an agent of that provider is working.
* `URGENT_POLL_MS` (30 s) while one is working AND a window is at or past
  `URGENT_PCT` but not yet spent. A spent window drops back to `POLL_MS`: the
  reset poll below already fires the moment it reopens.

WHAT "STALE" MEANS. A pill greys when its number has stopped being trustworthy,
not when one request failed. Before this, a single timeout or 429 greyed the
pill on the spot, even though the number on it was a minute old and still
right. Now the pill greys when the polls have been failing AND the reading is
older than `stale_after_s` (twice the current cadence, never under
`STALE_FLOOR_S`), or when a window's reset has passed since the reading was
taken (the percent then describes a window that no longer exists).

WHAT KEEPS IT FROM GETTING THERE:

* one quick retry (`RETRY_MS`) after a transient failure, instead of waiting a
  full interval that can be 6 minutes long;
* a 429 backs off exponentially instead (hammering a rate limiter is how it
  stays limited), capped at `BACKOFF_CAP_MS`, and a click on a pill clears it;
* waking from sleep (the wall clock jumps past `WAKE_GAP_S` between ticks)
  polls `WAKE_DELAY_MS` later instead of waiting out whatever was pending;
* agent activity starting after an idle stretch polls at once when the reading
  is already older than the new, shorter cadence;
* a poll is armed for just after the earliest reset, so a reopened window
  shows its new number within seconds.

The fetch runs on a daemon thread and hands its reading back through a queued
signal. Nothing here touches the network on construction: `start()` is opt-in,
called from `MainWindow.start_usage_polling`, which main.py alone calls (the
smoke suite builds windows and must never hit a real account).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

from PySide6.QtCore import QObject, Qt, QTimer, Signal

POLL_MS = 90_000
URGENT_POLL_MS = 30_000
IDLE_POLL_MS = 360_000
URGENT_PCT = 90.0
EXHAUSTED_PCT = 100.0

# how long after an agent's last output burst its provider still counts as
# active. Two ordinary polls, so the number that the last turn moved is read at
# the active rate before the poll relaxes to idle.
ACTIVE_GRACE_S = 180.0

RETRY_MS = 10_000
MAX_BACKOFF = 4                  # 2**4 = 16x the cadence at most...
BACKOFF_CAP_MS = 16 * 60_000     # ...and never longer than 16 minutes

# the pill greys once failing polls have left its reading this old: two missed
# cadences, never less than three minutes (30 s x 2 would grey on one blip)
STALE_FLOOR_S = 180.0

# the countdown tick fires every 20 s; a gap past this means the machine slept
WAKE_GAP_S = 90.0
# the network is usually not back the instant Windows resumes
WAKE_DELAY_MS = 5_000

# a poll armed just after a window's stated reset, plus this cushion for skew
RESET_GRACE_MS = 8_000
# QTimer takes a 32-bit interval; farther resets are the ordinary poll's job
RESET_HORIZON_MS = 6 * 3600 * 1000

# errors a quick retry cannot fix. A 429 has its own backoff; a missing or
# expired login waits for the CLI to fix it, which a retry 10 s later won't see.
NO_RETRY_ERRORS = frozenset({"http 429", "no-auth", "expired"})
# no login at all and nothing ever read: this readout has nothing to show this
# run, so its pills are cleared and the poll stops
ABSENT_ERRORS = frozenset({"no-auth"})


def limits_of(reading) -> tuple:
    """Every limit a reading carries, for either reading shape: `.limits` (a
    tuple, Claude and Gemini) or `.limit` (one, GPT). Each has `.percent` and
    `.resets_at`. An empty tuple means the reading has no number in it."""
    if reading is None:
        return ()
    many = getattr(reading, "limits", None)
    if many is not None:
        return tuple(many)
    one = getattr(reading, "limit", None)
    return (one,) if one is not None else ()


def cadence_ms(limits: Iterable, active: bool) -> int:
    """The poll gap a situation asks for, before any 429 backoff."""
    if not active:
        return IDLE_POLL_MS
    percents = [l.percent for l in limits]
    if percents and max(percents) < EXHAUSTED_PCT and max(percents) >= URGENT_PCT:
        return URGENT_POLL_MS
    return POLL_MS


def stale_after_s(cadence: int) -> float:
    """How old a reading may get, while polls fail, before its pill greys."""
    return max(2 * cadence / 1000.0, STALE_FLOOR_S)


@dataclass
class UsageSource:
    """What a provider hands the poller. Everything else is shared.

    `fetch` runs on a worker thread and should never raise (a raise is caught
    and treated as an unreadable poll). `agent_providers` are the agent
    `spec.provider` keys whose work moves this number. `tracker_keys` are the
    pill keys it feeds, in `TopBar._usage_pills`. `always` polls even with
    every pill closed, which only Claude needs: the plan-limit edges and
    auto-continue hang off its reading, not off the pill.
    """

    name: str
    fetch: Callable[[], object]
    agent_providers: tuple[str, ...]
    tracker_keys: tuple[str, ...]
    always: bool = False


@dataclass
class _FailState:
    error: str = ""
    at: float = 0.0
    retried: bool = False        # the one quick retry of this streak is spent


class UsagePoller(QObject):
    """Polls one `UsageSource` on the shared cadence and keeps its pills'
    stale state honest.

    The poller feeds its pills itself: a reading with a number in it goes
    into every pill through `set_usage` (each picks its own window), and a
    failure goes in through `note_poll_failed` or `mark_unreadable`. The
    owner only has to re-decide visibility, and handle whatever else hangs off
    the reading (Claude's plan-limit edges).

    `succeeded(reading)` fires on the GUI thread after the pills have the
    reading. `failed(reading_or_None)` fires for anything else, after the
    pills have been told. `absent()` fires once when the source turns out to
    have no login at all.
    """

    succeeded = Signal(object)
    failed = Signal(object)
    absent = Signal()
    _ready = Signal(object)          # worker thread -> GUI thread

    def __init__(self, source: UsageSource,
                 pills: Callable[[], Sequence],
                 active: Callable[[], bool],
                 wanted: Callable[[], bool],
                 parent=None, clock: Callable[[], float] = time.time):
        super().__init__(parent)
        self.source = source
        self._pills = pills
        self._active = active
        self._wanted = wanted
        self._clock = clock
        self._running = False
        self._inflight = False
        self._reading = None             # last reading with a number in it
        self._last_attempt = 0.0         # wall time the last poll came back
        self._backoff = 0
        self._fail = _FailState()
        self._next_at = 0.0
        # the pending poll is a quick retry or a wake poll, which the cadence
        # retune must leave alone (it would push it back out to the cadence)
        self._pending_special = False
        self._last_tick = 0.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.poll)
        self._reset_timer = QTimer(self)
        self._reset_timer.setSingleShot(True)
        self._reset_timer.timeout.connect(self.poll)
        self._ready.connect(self.deliver, Qt.ConnectionType.QueuedConnection)

    # -- state -------------------------------------------------------------
    def is_running(self) -> bool:
        return self._running

    def reading(self):
        return self._reading

    def backoff(self) -> int:
        return self._backoff

    def cadence_ms(self) -> int:
        """The cadence the situation asks for right now, no backoff."""
        return cadence_ms(limits_of(self._reading), self._active())

    def interval_ms(self) -> int:
        """The gap to the next ordinary poll: the cadence times any backoff."""
        return min(self.cadence_ms() * (2 ** self._backoff),
                   max(BACKOFF_CAP_MS, self.cadence_ms()))

    def next_poll_at(self) -> float:
        """Wall time of the next scheduled poll, or 0 when none is."""
        return self._next_at if self._timer.isActive() else 0.0

    def reset_poll_armed(self) -> bool:
        return self._reset_timer.isActive()

    def reset_poll_remaining_ms(self) -> int:
        return self._reset_timer.remainingTime()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        """Begin polling (and poll now), if anything wants this reading."""
        if self._running or not self.wanted():
            return
        self._running = True
        self._last_tick = self._clock()
        self.poll()

    def stop(self) -> None:
        self._running = False
        self._timer.stop()
        self._reset_timer.stop()
        self._next_at = 0.0

    def wanted(self) -> bool:
        return self.source.always or bool(self._wanted())

    # -- polling -----------------------------------------------------------
    def poll(self) -> None:
        """Kick one fetch on a daemon thread. `_inflight` is the guard that
        matters: a slow fetch must never let a timer stack threads behind it."""
        if self._inflight or not self.wanted():
            return
        self._inflight = True
        fetch = self.source.fetch
        ready = self._ready

        def worker():
            try:
                reading = fetch()
            except Exception:
                reading = None
            try:
                ready.emit(reading)
            except RuntimeError:
                pass        # the window closed while this fetch was out

        threading.Thread(target=worker, daemon=True,
                         name=f"aihive-usage-{self.source.name}").start()

    def refresh(self) -> None:
        """The user clicked a pill: they are asking NOW, so clear any backoff
        and poll, rather than leave the next automatic poll parked 16 minutes
        out behind a run of 429s."""
        self._backoff = 0
        self._fail.retried = False
        if self._running:
            self._schedule(self.interval_ms())
        self.poll()

    def deliver(self, reading) -> None:
        """Adopt a finished poll on the GUI thread. Public so the smoke suite
        can feed synthetic readings without a thread or a network."""
        self._inflight = False
        now = self._clock()
        self._last_attempt = now
        if limits_of(reading):
            self._reading = reading
            self._backoff = 0
            self._fail = _FailState()
            for pill in self._pills():
                pill.set_usage(reading)
            self.succeeded.emit(reading)
            self._arm_reset_poll(now)
            self._schedule(self.interval_ms())
            self._push_stale_after()
            return
        error = (getattr(reading, "error", "") or "unknown") if reading is not None else "unknown"
        if error in ABSENT_ERRORS and self._reading is None:
            for pill in self._pills():
                pill.mark_absent()
            self.stop()
            self.absent.emit()
            self.failed.emit(reading)
            return
        if error == "http 429":
            self._backoff = min(self._backoff + 1, MAX_BACKOFF)
        if not self._fail.error:
            self._fail = _FailState(error=error, at=now)
        self._fail.error = error
        if error not in NO_RETRY_ERRORS and not self._fail.retried:
            self._fail.retried = True
            delay = RETRY_MS
            self._schedule(delay, special=True)
        else:
            delay = self.interval_ms()
            self._schedule(delay)
        retry_at = now + delay / 1000.0 if self._running else 0.0
        for pill in self._pills():
            if pill.has_reading():
                pill.note_poll_failed(error, now, retry_at,
                                      stale_after_s(self.cadence_ms()))
            else:
                pill.mark_unreadable(error)
        self.failed.emit(reading)

    def tick(self) -> None:
        """The 20 s countdown tick. No network, unless the situation changed
        enough to warrant a poll: waking from sleep, or work starting after an
        idle stretch (the shorter cadence then says the reading is overdue)."""
        now = self._clock()
        slept = self._last_tick and now - self._last_tick > WAKE_GAP_S
        self._last_tick = now
        if not self._running:
            return
        if slept:
            # a retry the sleep swallowed is not this streak's fault
            self._fail.retried = False
            self._schedule(WAKE_DELAY_MS, special=True)
            return
        self._retune(now)
        self._push_stale_after()

    def _retune(self, now: float) -> None:
        """Follow the cadence as activity and utilization change. Measured
        from the last poll, so a shorter cadence can make the next poll due at
        once. Leaves a pending quick retry alone."""
        if self._inflight or not self._timer.isActive() or self._pending_special:
            return
        due = self._last_attempt + self.interval_ms() / 1000.0
        if abs(due - self._next_at) < 1.0:
            return
        self._schedule(max(0, int((due - now) * 1000)))

    def _schedule(self, delay_ms: int, special: bool = False) -> None:
        if not self._running:
            return
        delay_ms = max(0, int(delay_ms))
        self._pending_special = special
        self._next_at = self._clock() + delay_ms / 1000.0
        self._timer.start(delay_ms)

    def _arm_reset_poll(self, now: float) -> None:
        """Poll just after the earliest upcoming reset, when that comes before
        the next ordinary poll (or the window is spent and the reset is within
        the horizon, which is what fires Claude's planLimitCleared promptly
        even behind a 429 backoff)."""
        resets = [l.resets_at for l in limits_of(self._reading)
                  if l.resets_at is not None and l.resets_at > now]
        spent = any(l.percent >= EXHAUSTED_PCT and l.resets_at is not None
                    and l.resets_at > now for l in limits_of(self._reading))
        if not resets:
            self._reset_timer.stop()
            return
        if spent:
            soonest = min(l.resets_at for l in limits_of(self._reading)
                          if l.percent >= EXHAUSTED_PCT and l.resets_at is not None
                          and l.resets_at > now)
        else:
            soonest = min(resets)
        delay = int((soonest - now) * 1000) + RESET_GRACE_MS
        if spent and delay <= RESET_HORIZON_MS:
            self._reset_timer.start(delay)
        elif not spent and delay <= self.interval_ms():
            self._reset_timer.start(delay)
        else:
            self._reset_timer.stop()

    def _push_stale_after(self) -> None:
        s = stale_after_s(self.cadence_ms())
        for pill in self._pills():
            pill.set_stale_after(s)


def provider_active(agents: Iterable, providers: Sequence[str],
                    now: float | None = None) -> bool:
    """True if any agent of these providers is working, or finished working
    within `ACTIVE_GRACE_S`. The grace keeps the cadence from flapping on the
    2 s idle debounce between tool calls, and reads the number the last turn
    moved at the active rate."""
    now = time.time() if now is None else now
    for agent in agents:
        if agent.spec.provider not in providers:
            continue
        if agent.is_busy():
            return True
        if now - agent.last_work_at() < ACTIVE_GRACE_S:
            return True
    return False
