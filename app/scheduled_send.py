"""A message the user wrote now and wants typed into an agent LATER.

Qt-free and stdlib-only, like `chime.py` / `limit_banner.py` / `limit_ledger.py`
- everything here is pure data and parsing, so it is directly testable without a
QApplication, and `TerminalAgent` only has to hold a list of these.

WHY THIS EXISTS. Pressing Enter submits immediately, which means chaining agents
while away from the machine is impossible: you cannot say "start the second
agent twenty minutes from now, once the first is likely done". A scheduled
message is that Enter, deferred.

WHAT IT IS NOT. It is not an assignment. When one of these fires it goes through
`TerminalAgent.nudge`, never `deliver_task` - the same distinction the plan-limit
auto-continue makes. It is a message inside work the agent already has, so it
must not overwrite the persisted `current_task`, flip the assignment to WORKING,
or re-infer the role.

STATES. A message is PENDING until it is either typed (SENT) or given up on
(MISSED). MISSED is the important one and exists for a single case: the app was
closed (or the agent unreachable) when the message came due. A message set for
3am must NOT fire at 10am into a conversation that has moved on - the user is
shown it instead and decides. `TerminalAgent.restore_scheduled` is where that
rule is applied on load; the tick in `MainWindow` applies it to an agent that
stayed unreachable too long.

Only PENDING messages are persisted (see `WorkspaceManager._agent_dict`): a sent
message is history the transcript already records, and this is not a ledger.
"""

import time
import re
import uuid
from dataclasses import dataclass, field

from .limit_banner import next_wall_clock

PENDING = "pending"
SENT = "sent"
MISSED = "missed"

# "45m", "1h30", "90s", "1h 30m 10s" - one number with an optional unit.
# Scanned as repeated tokens rather than matched by a single all-optional
# regex, which is what makes "1h30" and "90s" both work: the first needs a
# unitless number to inherit a unit, the second must not be read as one.
_TOKEN_RE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*"
                       r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)?\s*",
                       re.I)
_HHMM_RE = re.compile(r"^\s*(\d{1,2})\s*:\s*(\d{2})\s*$")
# "3:30pm", "15:30", "3pm" - the absolute-time field
_CLOCK_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)

_UNIT_S = {"h": 3600, "m": 60, "s": 1}
# What a number with NO unit means: one step finer than the token before it, so
# "1h30" is an hour and a half and "1h30m10" is that plus ten seconds. A lone
# number is MINUTES - that is what anyone typing into a "send in" box means,
# and seconds would be a useless unit here.
_IMPLIED_UNIT = {"": "m", "h": "m", "m": "s"}

MAX_DELAY_S = 7 * 24 * 3600  # a week; past this it is a typo, not an intent
# A sanity cap per agent. These are persisted, so an accidental loop adding
# them must not grow session.json without bound.
MAX_PER_AGENT = 20


@dataclass
class ScheduledMessage:
    """One deferred submit. `due_ts`/`created_ts` are epoch seconds (local
    clock, like the rest of the limit machinery)."""
    text: str
    due_ts: float
    created_ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: str = PENDING
    attempts: int = 0

    def is_pending(self) -> bool:
        return self.state == PENDING

    def seconds_left(self, now: float | None = None) -> float:
        return self.due_ts - (time.time() if now is None else now)

    def is_due(self, now: float | None = None) -> bool:
        return self.is_pending() and self.seconds_left(now) <= 0

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "due_ts": self.due_ts,
                "created_ts": self.created_ts, "state": self.state,
                "attempts": self.attempts}

    @classmethod
    def from_dict(cls, data: dict) -> "ScheduledMessage | None":
        """Rebuild from a session record, or None when the record is unusable.

        Tolerant on purpose: this is restore-path code, and one malformed entry
        must never cost the agent its other messages (the same posture
        `_agent_dict_safe` takes on the way out)."""
        try:
            text = str(data.get("text") or "").strip()
            due = float(data.get("due_ts"))
        except (TypeError, ValueError):
            return None
        if not text:
            return None
        state = str(data.get("state") or PENDING)
        if state not in (PENDING, SENT, MISSED):
            state = PENDING
        try:
            created = float(data.get("created_ts") or due)
        except (TypeError, ValueError):
            created = due
        try:
            attempts = int(data.get("attempts") or 0)
        except (TypeError, ValueError):
            attempts = 0
        return cls(text=text, due_ts=due, created_ts=created,
                   id=str(data.get("id") or uuid.uuid4().hex),
                   state=state, attempts=attempts)


def parse_delay(text: str) -> float | None:
    """Seconds from a relative delay, or None when it doesn't parse.

    Accepts "45", "45m", "1h", "1h30", "1h30m", "90s", "1.5h", "2:15". A BARE
    number is minutes, and "H:MM" is hours and minutes - both are what a person
    typing into a "send in" box means. Zero and negatives are rejected: a
    countdown of nothing is a plain Enter, which the user already has.
    """
    text = (text or "").strip()
    if not text:
        return None
    m = _HHMM_RE.match(text)
    if m:  # "2:15" is hours and minutes, never minutes and seconds
        total = int(m.group(1)) * 3600 + int(m.group(2)) * 60
        return total if 0 < total <= MAX_DELAY_S else None
    total, pos, prev = 0.0, 0, ""
    while pos < len(text):
        tok = _TOKEN_RE.match(text, pos)
        if tok is None or tok.end() == pos:
            return None  # trailing junk: reject rather than guess
        unit = (tok.group(2) or "")[:1].lower() or _IMPLIED_UNIT.get(prev, "")
        if not unit:
            return None  # a unitless number after seconds means nothing
        total += float(tok.group(1)) * _UNIT_S[unit]
        prev, pos = unit, tok.end()
    return total if 0 < total <= MAX_DELAY_S else None


def parse_clock(text: str, now: float | None = None) -> float | None:
    """Epoch seconds of the NEXT occurrence of a wall clock ("03:30", "3:30pm",
    "15:30"), or None when it doesn't parse.

    Deliberately the same rollover rule as `limit_banner.parse_reset_clock`: a
    bare clock carries no date, so a time already past today means tomorrow.
    That is the behaviour someone setting "at 03:00" late in the evening
    expects. The parsing is kept separate from that function, because this one
    parses a whole field the user typed while that one searches inside a
    banner, but both roll over through `next_wall_clock`, which keeps the
    wall time right on the night the clocks change.
    """
    m = _CLOCK_RE.match(text or "")
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        ampm = ampm.lower()
        if hour == 12:
            hour = 0
        if ampm == "pm":
            hour += 12
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = time.time() if now is None else now
    return next_wall_clock(hour, minute, now)


def format_countdown(seconds: float) -> str:
    """"1:04:22" / "12:04" / "0:09" - what the card's chip shows.

    Clamped at zero: a message that is due but hasn't gone out yet (the agent
    is still booting its TUI) should read 0:00, never a negative."""
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_clock(due_ts: float) -> str:
    """The absolute fire time, for tooltips and the dialog's confirmation line
    ("today at 03:41" / "tomorrow at 03:41")."""
    due = time.localtime(due_ts)
    today = time.localtime()
    stamp = time.strftime("%H:%M", due)
    if (due.tm_year, due.tm_yday) == (today.tm_year, today.tm_yday):
        return f"today at {stamp}"
    if due_ts - time.time() < 36 * 3600:
        return f"tomorrow at {stamp}"
    return time.strftime("%d %b at %H:%M", due)
