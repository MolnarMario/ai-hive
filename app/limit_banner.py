"""Recognising a plan/quota-limit cut-off, and when it ends.

Shared by two callers that must agree exactly, which is why the patterns live
here rather than in either of them: `terminal_agent` matches them against the
LIVE screen, and `transcripts` matches them against the conversation on disk
(the durable record used to recover agents at startup). Qt-free and stdlib-only
like `chime.py` / `claude_usage.py`, because `transcripts` is.

TWO PROVIDERS are recognised, and their cut-offs are not the same shape at all
(see the Gemini section at the bottom for what that costs). `LIMIT_PROVIDERS`
is the set every caller should gate on rather than spelling out either name.

TWO signals, in order of reliability:

  * `LIMIT_MENU_RE` — the interactive menu Claude parks the agent on:

        What do you want to do?
        > 1. Stop and wait for limit to reset
          2. Upgrade your plan
        Enter to confirm - Esc to cancel

    This is the PRIMARY live signal, because it is the thing that STAYS on
    screen for as long as the agent is stuck. Its disappearance is equally
    meaningful: it is how we tell a resume actually took. NOT SEEN as the
    default cut-off UI as of claude.exe 2.1.235+ (see below) — kept for
    whatever install still shows it.

  * `LIMIT_HIT_RE` / `_LIMIT_REACHED_RE` — the banner. Two wordings exist:
    the old `You've hit your session limit - resets 3am`, and the current
    `Usage limit reached \xb7 continuing automatically at 10:10pm \xb7 esc or
    type to cancel` (claude.exe 2.1.235+, read off a real transcript's
    injected system/informational record, not guessed). BOTH ARE STILL
    LIVE on 2.1.251, and they come from different places: the current one
    is the CLI's own quota UI, which auto-continues; the old one is a raw
    429 (`error: "rate_limit"`, `rateLimitType: "five_hour"`) surfaced as
    a synthetic assistant message, which just kills the turn and draws no
    menu. Do not drop either.
    Ordinary scrollback, so on the live screen a 4000-char rolling tail
    evicts it within a couple of hours of idling — do not rely on it alone
    there. It IS, however, what lands in the transcript, so it is the only
    signal available when reconstructing a cut-off from disk. The current
    wording is also Claude Code's OWN auto-continue: verified live, it
    injects a queued "...usage limit has reset. Continue..." message and
    logs "Usage limit reset \xb7 continuing automatically" once the window
    reopens, with no help from AI Hive, PROVIDED the process is still alive
    to see it — a stopped card or a closed app misses that edge exactly like
    it always could, which is what the startup-recovery path is for.

Both deliberately EXCLUDE `Approaching ...` and `You've used N% of your ...`:
those render while the agent is still working, and nudging it would interrupt
real work. The old wording was verified against claude.exe 2.1.220, built
from `You've hit your ${label}` with {five_hour:"session limit",
seven_day:"weekly limit", ...}; the current one against 2.1.251, which still
emits the old one too.
"""

import re
import time

# Providers whose cut-off this module can recognise at all. Everything in the
# recovery path (the live scrape, the resume pass) gates on this rather than on
# a literal "claude", so adding a third CLI is a change here plus its patterns.
LIMIT_PROVIDERS = ("claude", "gemini")

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

# Claude Code replaced this wording (verified: gone from claude.exe 2.1.235+,
# where the only "hit your ... limit" strings left are for spend/Fast-mode
# caps, a different feature). The cut-off notice is now "Usage limit reached
# (c) continuing automatically at 10:10pm (c) esc or type to cancel" -- read
# off a REAL transcript's injected system/informational record, not guessed.
# `LIMIT_HIT_RE` is kept for whatever older installs still print it; this is
# additive, not a replacement. No window name is captured here -- the new
# wording never states one (see `banner_window`).
_LIMIT_REACHED_RE = re.compile(r"usage\s+limit\s+reached\b", re.I)

# Month abbreviations, for the dated form of the new banner (see
# `_LIMIT_RESET_RE`): a reset more than 24 h out (weekly/opus/sonnet) is
# rendered with a date -- "continuing automatically at Aug 25, 3:00pm" --
# rather than a bare clock, verified against claude.exe's own formatter
# (`month:"short"` once the reset is >24h away). English only, matching the
# rest of this module's assumption.
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# The banner states its own reset time, already in LOCAL time: the old wording
# as "- resets 8:30pm (Europe/Bucharest)", the new one as "continuing
# automatically at 10:10pm" (optionally dated, see `_MONTHS`). This is the
# network-free half of the trigger: it survives a usage-endpoint 429, a missed
# API edge, and an app restart, none of which the account-wide reading does.
# The timezone suffix on the old wording is ignored on purpose — the clock
# shown is already the user's own.
_LIMIT_RESET_RE = re.compile(
    r"(?:resets|continuing\s+automatically\s+at|continuing\s+shortly\s+at)"
    r"\s+(?:([A-Za-z]{3})\w*\s+(\d{1,2}),\s*)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.I)


# Claude's banner is a SHORT injected message ("You've hit your session limit -
# resets 3am (Europe/Bucharest)" is ~61 chars). Anything long containing those
# words is an agent TALKING ABOUT the limit, not being stopped by it — which is
# not hypothetical: an agent working on this very feature quoted the banner in
# its own output and was armed for a resume it never needed. A real cut-off is
# always its own short line.
_BANNER_MAX_CHARS = 200

# Everything a TUI can paint to the LEFT of the message itself: indentation,
# the box-drawing gutter Claude draws down its output, the selection caret, and
# the warning glyph agy puts in front of an error (with its variation selector,
# which arrives as a separate code point). Stripped before matching so both
# banners can be anchored at the start of their line, which is the discriminator
# that keeps an agent's own prose about a limit from being read as one.
#
# THE TWO CLAUDE ONES ARE LOAD-BEARING AND WERE MISSING, which cost a live
# 5-hour cut-off (2026-08-31, vinted-country-detector/Agent 1, verified in the
# transcript as a 429 with rateLimitType five_hour). The 429 arrived while a
# Bash tool was mid-flight, so Claude painted the banner as a TOOL-RESULT ROW:
#
#     ● Bash(cd "..." && cat > /tmp/slim.mjs <<'EOF' ...)
#       ⎿ raw 37175 b64 14448 chunks@1400 11
#       ⎿ Allowed by auto mode classifier
#       ⎿ You've hit your session limit · resets 6:40pm (Europe/Bucharest)
#
# `⎿` (U+23BF) is the tool-result gutter and `●` (U+25CF) the assistant
# bullet, and neither was in this set, so `banner_line`'s `.match` anchored on
# the glyph and returned "". Nothing latched, and nothing was AUDITED either:
# `_note_limit_skip` calls this same `banner_line`, so the one log line written
# to explain a miss cannot fire for this kind of miss. A glyph census over 13
# real screen captures found U+23BF and U+25CF the two dominant line prefixes
# (up to 48 and 88 rows in a single frame), so this is the ordinary rendering,
# not a freak frame. U+00A0 rides along because Claude separates that gutter
# from the text with a NO-BREAK space rather than an ordinary one (read off the
# raw pty stream), so a leading run can end on one.
_GUTTER = " \t\xa0│┃⎿●|>❯⚠✗✘×•*️"


def _strip_gutter(line: str) -> str:
    return line.lstrip(_GUTTER).strip()


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
        line = _strip_gutter(line)
        if len(line) > _BANNER_MAX_CHARS:
            continue
        if LIMIT_HIT_RE.match(line) or _LIMIT_REACHED_RE.match(line):
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

    The current wording (`_LIMIT_REACHED_RE`, "Usage limit reached ...") never
    names a window at all, so this returns "" for it — not a regression: that
    wording's own reset clock is dated whenever the reset is >24h out (see
    `_MONTHS`), so it carries the precision the old "weekly" special-case
    existed to make up for, and `parse_reset_clock` resolves it directly.
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
    the banner is usually read late at night about a small-hours reset. The
    current wording's dated form ("... at Aug 25, 3:00pm") skips that guess
    entirely and is resolved directly, except across a Dec->Jan boundary,
    where the named date would otherwise land weeks in the past.

    `now` is the ANCHOR, and passing the right one is essential for anything
    read from disk — see `banner_reset_at`.
    """
    m = _LIMIT_RESET_RE.search(text or "")
    if not m:
        return None
    mon, day, hour, minute, ampm = m.groups()
    hour, minute = int(hour), int(minute or 0)
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
    if mon and day:
        month = _MONTHS.get(mon.lower()[:3])
        if month is None or not (1 <= int(day) <= 31):
            return None
        try:
            target = time.mktime((lt.tm_year, month, int(day), hour, minute,
                                  0, 0, 0, -1))
        except (OverflowError, ValueError):
            return None
        # a weekly/opus/sonnet reset is at most ~8 days out, so a named date
        # that lands more than a few days in the past can only mean the
        # window wraps into next year (a Dec banner naming a January reset).
        if target < now - 3 * 86400:
            try:
                target = time.mktime((lt.tm_year + 1, month, int(day), hour,
                                      minute, 0, 0, 0, -1))
            except (OverflowError, ValueError):
                return None
        return target
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


# --------------------------------------------------------------- Gemini ---
#
# The Antigravity CLI (`agy`) cuts an agent off in a completely different shape,
# and every difference costs something:
#
#     ⚠ Individual quota reached. Please upgrade your subscription to increase
#       your limits. Resets in 1h40m21s.
#       Error ID: 6a22d054-3666-4717-b0ac-7b359153c647-266
#
#  * THERE IS NO MENU. Claude parks the agent on an interactive prompt, which
#    is the primary signal precisely because it persists while the agent is
#    stuck and is torn down when it goes again. agy just prints an error and
#    returns to its prompt, so this whole family has only the weak, scrollback
#    signal — the equivalent of Claude's banner and nothing else.
#  * THE CLOCK IS A DURATION, not a wall time. That is strictly BETTER: it
#    needs no rollover guess, and it is equally usable for a window days out,
#    so a Gemini cut-off never has the "weekly banner can't be trusted" problem
#    `banner_window` exists for. It must be resolved the moment it is READ,
#    though, because the printed text keeps saying "1h40m21s" forever.
#  * THE MESSAGE WRAPS. The duration and the Error ID land on continuation
#    rows, so unlike Claude's one-line banner the identity of the cut-off is
#    NOT on the matched line — which matters enormously, because the identity
#    is what stops a banner still sitting in the scrollback from re-latching an
#    agent that has already been resumed off it. `gemini_banner_line` therefore
#    returns the matched line JOINED with those detail rows. The Error ID is
#    unique per message and the duration differs between any two cut-offs, so
#    the joined string is a far sharper identity than Claude's wall clock.
#  * THE WORDING IS THE SERVER'S. "Individual quota reached" is nowhere in
#    agy.exe (only a "Quota Reached" UI label is), so it arrives over the wire
#    and may vary. Hence one leading qualifier word is allowed, and the match
#    is anchored rather than a bare search.

# One optional qualifier ("Individual"/"Daily"/"Model"/...) then the phrase.
# ANCHORED at the start of the line, like `LIMIT_HIT_RE`: an agent discussing a
# quota embeds the words mid-sentence, and allowing just the one word in front
# is what keeps "note that the quota reached its cap" from matching.
GEMINI_LIMIT_RE = re.compile(r"(?:\w+\s+)?quota\s+(?:reached|exceeded)\b", re.I)

# The rows the message wraps onto that are worth keeping: the countdown and the
# per-message Error ID. Anything else on those rows is ordinary wrapped prose
# and is dropped, so the identity stays short and stable.
_GEMINI_DETAIL_RE = re.compile(r"resets?\s+in\s+\d|error\s+id\s*:", re.I)

# How far past the matched line to look for them. The observed message wraps
# over three rows; four is one row of slack, and small enough that a following
# unrelated line can't be swept in (it would have to match _GEMINI_DETAIL_RE).
_GEMINI_DETAIL_LINES = 4

# "Resets in 1h40m21s" / "resets in 45m" / "resets in 30s".
_GEMINI_RESET_RE = re.compile(r"resets?\s+in\s+((?:\d+\s*[hms]\s*)+)", re.I)
_GEMINI_DUR_RE = re.compile(r"(\d+)\s*([hms])", re.I)
_GEMINI_UNITS = {"h": 3600, "m": 60, "s": 1}


def gemini_banner_line(text: str) -> str:
    """The agy quota message, normalized to one line, or "" when `text` holds
    none.

    The matched line plus its countdown/Error-ID continuation rows, joined with
    single spaces. Returning the whole thing rather than a bool is what makes it
    usable as the IDENTITY of one cut-off — see the section comment above, and
    `terminal_agent._scrape_limit` for what that identity is guarding.

    The LAST match wins, for the same reason as `banner_line`: when a retry has
    printed a second message, the newest one describes the current state.
    """
    if not text:
        return ""
    lines = [_strip_gutter(ln) for ln in text.splitlines()]
    found = ""
    for i, line in enumerate(lines):
        if len(line) > _BANNER_MAX_CHARS or not GEMINI_LIMIT_RE.match(line):
            continue
        parts = [line]
        for cont in lines[i + 1:i + 1 + _GEMINI_DETAIL_LINES]:
            if GEMINI_LIMIT_RE.match(cont):
                break           # the next message begins; this one is done
            if _GEMINI_DETAIL_RE.search(cont):
                parts.append(cont)
        found = " ".join(parts)
    return found


def gemini_banner_in(text: str) -> bool:
    """True when `text` shows an agy quota cut-off."""
    return bool(gemini_banner_line(text))


def gemini_reset_at(text: str, now: float | None = None) -> float | None:
    """Epoch seconds the quota frees up, from agy's RELATIVE countdown, or None
    when `text` states none.

    Resolved against `now` at READ time, which is the whole difference from
    `parse_reset_clock`: the printed text is a snapshot ("Resets in 1h40m21s")
    that never updates, so parsing the same message an hour later would push the
    reset an hour further out. Callers must resolve it ONCE, when the message is
    first seen, and keep the epoch — which is exactly what latching it on the
    agent does.
    """
    m = _GEMINI_RESET_RE.search(text or "")
    if not m:
        return None
    seconds = 0
    for value, unit in _GEMINI_DUR_RE.findall(m.group(1)):
        seconds += int(value) * _GEMINI_UNITS[unit.lower()]
    if seconds <= 0:
        return None
    return (time.time() if now is None else now) + seconds
