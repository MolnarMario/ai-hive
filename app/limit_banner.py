"""Recognising a Claude plan-limit cut-off, and when it ends.

Shared by two callers that must agree exactly, which is why the patterns live
here rather than in either of them: `terminal_agent` matches them against the
LIVE screen, and `transcripts` matches them against the conversation on disk
(the durable record used to recover agents at startup). Qt-free and stdlib-only
like `chime.py` / `claude_usage.py`, because `transcripts` is.

TWO signals, in order of reliability:

  * `LIMIT_MENU_RE` — the interactive menu Claude parks the agent on:

        What do you want to do?
        > 1. Stop and wait for limit to reset
          2. Upgrade your plan
        Enter to confirm - Esc to cancel

    This is the PRIMARY live signal, because it is the thing that STAYS on
    screen for as long as the agent is stuck. Its disappearance is equally
    meaningful: it is how we tell a resume actually took.

  * `LIMIT_HIT_RE` — the banner (`You've hit your session limit - resets 3am`).
    Ordinary scrollback, so on the live screen a 4000-char rolling tail evicts
    it within a couple of hours of idling — do not rely on it alone there. It
    IS, however, what lands in the transcript, so it is the only signal
    available when reconstructing a cut-off from disk.

Both deliberately EXCLUDE `Approaching ...` and `You've used N% of your ...`:
those render while the agent is still working, and nudging it would interrupt
real work. Verified against claude.exe 2.1.220, which builds them from
`You've hit your ${label}` with {five_hour:"session limit",
seven_day:"weekly limit", ...}.
"""

import re
import time

# The parked-on menu — primary, because it persists while the agent is stuck.
LIMIT_MENU_RE = re.compile(r"stop\s+and\s+wait\s+for\s+limit\s+to\s+reset",
                           re.I)

# The banner — secondary on screen (it scrolls), but it is what the transcript
# records, so it is the startup-recovery signal.
# The window name is CAPTURED, not just matched: "session" carries a bare clock
# that is always within 24 h, but a "weekly" window can be days out and its
# banner still prints only a wall time — so the two cannot be trusted equally.
# See `banner_window`.
LIMIT_HIT_RE = re.compile(r"you['’]ve hit your\s+"
                          r"(session|weekly|usage|opus|sonnet)\s+limit",
                          re.I)

# The banner states its own reset time ("- resets 8:30pm (Europe/Bucharest)"),
# already in LOCAL time. This is the network-free half of the trigger: it
# survives a usage-endpoint 429, a missed API edge, and an app restart, none of
# which the account-wide reading does. The timezone suffix is ignored on
# purpose — the clock shown is already the user's own.
_LIMIT_RESET_RE = re.compile(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
                             re.I)


# Claude's banner is a SHORT injected message ("You've hit your session limit -
# resets 3am (Europe/Bucharest)" is ~61 chars). Anything long containing those
# words is an agent TALKING ABOUT the limit, not being stopped by it — which is
# not hypothetical: an agent working on this very feature quoted the banner in
# its own output and was armed for a resume it never needed. A real cut-off is
# always its own short line.
_BANNER_MAX_CHARS = 200


def banner_line(text: str) -> str:
    """The banner line itself, normalized, or "" when `text` holds none.

    Anchoring at the start of the line is the real discriminator: Claude's
    banner is injected as its own line, while an agent discussing the limit
    embeds the same words in a sentence. The length cap is a second guard for
    the case where prose happens to begin with the phrase.

    Returning the LINE rather than a bool is what lets a caller tell one
    cut-off from another: the banner names its own reset clock, and successive
    5-hour windows never end at the same wall time, so the text doubles as the
    identity of the cut-off that produced it. `terminal_agent._scrape_limit`
    uses that to ignore the banner still sitting on screen after a resume.

    The LAST match wins: when an old banner and a fresh one are both in view,
    the newest is the one describing the current state.
    """
    found = ""
    if not text:
        return found
    for line in text.splitlines():
        # drop leading whitespace and any box-drawing gutter the TUI draws
        line = line.lstrip(" \t│┃|>❯").strip()
        if len(line) <= _BANNER_MAX_CHARS and LIMIT_HIT_RE.match(line):
            found = line
    return found


def banner_in(text: str) -> bool:
    """True when `text` contains the banner AS a banner — a short line that
    OPENS with it — not prose that merely mentions it mid-sentence."""
    return bool(banner_line(text))


def banner_window(text: str) -> str:
    """Which limit window the banner names — "session" / "weekly" / "opus" /
    "sonnet" / "usage" — or "" when `text` holds no banner.

    Worth carrying because the two kinds of window are NOT equally readable off
    the screen. A session banner's bare "resets 3am" is unambiguous: the next
    3am is at most 24 h away, which is the only thing `parse_reset_clock` can
    resolve. A WEEKLY banner prints the same bare clock for a reset that may be
    days out, so resolving it the same way lands early — by up to a week. A
    caller acting on a weekly cut-off must therefore wait for the account
    reading rather than trusting the clock on screen.
    """
    line = banner_line(text)
    if not line:
        return ""
    m = LIMIT_HIT_RE.match(line)
    return m.group(1).lower() if m else ""


def is_limit_screen(text: str) -> bool:
    """True when `text` shows a plan-limit cut-off (menu, or a real banner)."""
    if not text:
        return False
    return bool(LIMIT_MENU_RE.search(text)) or banner_in(text)


def parse_reset_clock(text: str, now: float | None = None) -> float | None:
    """Epoch seconds of the NEXT occurrence of the wall clock in a limit
    banner, or None when there is no time in it.

    A bare clock time carries no date, so it resolves to today if that moment
    is still ahead and tomorrow otherwise — the rollover that matters, since
    the banner is usually read late at night about a small-hours reset.

    `now` is the ANCHOR, and passing the right one is essential for anything
    read from disk — see `banner_reset_at`.
    """
    m = _LIMIT_RESET_RE.search(text or "")
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
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, minute, 0,
                          0, 0, -1))
    if target <= now:
        target += 86400
    return target


def banner_reset_at(text: str, written_at: float) -> float | None:
    """The reset epoch of a banner, anchored to WHEN IT WAS WRITTEN.

    Startup recovery reads banners out of a transcript, potentially hours after
    the fact, and the clock in them is bare ("resets 3am"). Anchoring to the
    record's own timestamp is what makes that correct: a banner written at
    02:42 saying "resets 3am" means 03:00 THAT DAY — already past, so the agent
    resumes immediately. Resolved against the current clock instead it would
    land on 3am TOMORROW and the agent would sit idle for a full day.
    """
    return parse_reset_clock(text, written_at)
