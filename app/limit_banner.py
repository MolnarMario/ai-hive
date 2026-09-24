"""Recognising a plan/quota-limit cut-off, and when it ends.

Shared by two callers that must agree exactly, which is why the patterns live
here rather than in either of them: `terminal_agent` matches them against the
LIVE screen, and `transcripts` matches them against the conversation on disk
(the durable record used to recover agents at startup, and swept once a
minute as a second detector). Qt-free and stdlib-only like `chime.py` /
`claude_usage.py`, because `transcripts` is.

TWO PROVIDERS are recognised, and their cut-offs are not the same shape at all
(see the Gemini section at the bottom for what that costs). `LIMIT_PROVIDERS`
is the set every caller should gate on rather than spelling out either name.

EVERY CLAUDE PATTERN IS MATCHED WITH THE WHITESPACE REMOVED. This is the rule
that matters most in this file, and breaking it silently disables the live
detector. Claude's classic renderer (`tui: "default"`, the one AI Hive runs)
places words by jumping the cursor (`CSI n G` / `CSI n C`) instead of printing
the spaces between them, so once `terminal_agent` strips escapes a row reads
"You'vehityoursessionlimit\xb7resets6:40pm". Measured 2026-09-24 on the 16
saved screen streams: roughly two rows in three arrive like that, assistant
prose included. Every pattern here used to require `\\s+` between words, so a
banner painted that way never matched, and whether a cut-off was caught was a
coin flip per frame. `_has_ready_hint` had already learned this for the input
box footer; the limit detector had not. So: `_despace` both sides, compare,
and keep `banner_key` (the despaced form) as the identity of a cut-off, since
the same banner can come back spaced on one repaint and despaced on the next.

The cut-off lines, all read off claude.exe 2.1.281's own formatters:

  * the 429 banner, `You've hit your <label>` + optional " \xb7 resets <when>"
    + optional " \xb7 progress saved". <label> is "session limit" / "weekly
    limit" / "Opus limit" / "Sonnet limit" / "Fable limit" / "usage limit" /
    "usage credit limit" / plain "limit" / "monthly spend limit" / "team's
    shared budget" / ... ("fast limit" is Fast mode falling back, NOT a
    cut-off). Written to the transcript as a synthetic assistant record with
    `error: "rate_limit"` and a `quotaLimits` block carrying the exact reset
    epoch, which `transcripts` prefers over the printed clock.
  * `You're out of usage credits \xb7 resets <when>`.
  * the CLI's own auto-continue: `Usage limit reached \xb7 continuing
    automatically at 10:10pm \xb7 esc or type to cancel` (also "... continuing
    shortly", "... when it resets", "... again after you continued"), and the
    grace-window status `Usage limit reached \xb7 wrapping up` / bare `Usage
    limit reached`. Claude continues by itself when it can, so AI Hive only
    steps in if the conversation shows it did not.
  * `Your usage limit has reset \xb7 press enter to continue`: the auto-continue
    went stale (the reset passed while nothing could fire it) and Claude is
    now WAITING FOR A KEYPRESS. Due immediately.

`LIMIT_MENU_RE` is the older interactive "Stop and wait for limit to reset"
menu. Not drawn by current versions, kept for whatever install still shows it.

All of them deliberately EXCLUDE `Approaching ...` and `You've used N% of your
...`: those render while the agent is still working, and nudging it would
interrupt real work. And every one must be the WHOLE start of its own short
line, which is what keeps an agent's own prose about a limit from latching.
"""

import re
import time

# Providers whose cut-off this module can recognise at all. Everything in the
# recovery path (the live scrape, the resume pass) gates on this rather than on
# a literal "claude", so adding a third CLI is a change here plus its patterns.
LIMIT_PROVIDERS = ("claude", "gemini")

# The old parked-on menu. `\s*` rather than `\s+` for the despacing reason in
# the module docstring: this one is searched on the raw escape-stripped region.
LIMIT_MENU_RE = re.compile(
    r"stop\s*and\s*wait\s*for\s*limit\s*to\s*reset", re.I)

# --- the despaced patterns. Each runs on `_despace(line)`: gutter stripped,
# every whitespace character removed, lower-cased. ---

# `You've hit your <label>`. The label is captured so the window can be named.
# It must END the phrase: followed by the " \xb7 " separator, a full stop, the
# "(Europe/...)" of a bare reset, or the end of the line. That is what keeps
# "You've hit your limit on retries, so..." (prose) out. "fast" is excluded:
# Fast mode's cap switches the model back, it does not stop the agent.
_HIT_RE = re.compile(
    r"you['’]vehityour(?!fast)([a-z0-9.'’]{0,40}?)(?:limit|budget)(?=[·•.(]|$)")

_OUT_OF_CREDITS_RE = re.compile(r"you['’]reoutofusagecredits(?=[·•.(]|$)")

# The CLI's own auto-continue and grace-window status lines. Again the phrase
# must end where Claude ends it: `Usage limit reached" cut-off` (an agent
# quoting the words, which latched a live agent twice on 2026-08-31) does not.
_REACHED_RE = re.compile(r"usagelimitreached(?=[·•]|$)")

# The auto-continue went stale and is waiting for Enter.
_STALE_RE = re.compile(r"yourusagelimithasreset(?=[·•]|$)")

# The CLI logged that it continued by itself. Not a cut-off: the END of one.
_RESET_NOTICE_RE = re.compile(r"usagelimitreset(?=[·•]|$)")

# The captured 429 label -> the window name the rest of the app uses, first
# substring match wins. "weekly"/"opus"/"sonnet"/"fable" are all 7-day windows
# (see `SEVEN_DAY_WINDOWS`); anything unrecognised ("monthly spend", "team's
# shared", a bare "limit") is "usage", which is treated as an ordinary window.
_LABEL_WINDOWS = (("session", "session"), ("weekly", "weekly"),
                  ("opus", "opus"), ("sonnet", "sonnet"), ("fable", "fable"),
                  ("credit", "credit"))

# Windows that can be days away. A bare clock on one of these cannot be
# resolved (it names a time, not a day), so only a DATED clock or an exact
# epoch may be trusted for them; see `reset_is_dated`.
SEVEN_DAY_WINDOWS = ("weekly", "opus", "sonnet", "fable")

# `quotaLimits.rateLimitType` on a transcript's 429 record, in window names.
RATE_LIMIT_WINDOWS = {"five_hour": "session", "seven_day": "weekly",
                      "seven_day_opus": "opus", "seven_day_sonnet": "sonnet",
                      "seven_day_overage_included": "fable",
                      "overage": "credit"}

# Month abbreviations, for the dated form of a reset: more than 24 h out,
# claude.exe renders `toLocaleString("en-US", {month:"short", day:"numeric",
# hour, minute})` with " AM" folded to "am", so "Sep 30, 9am" or "Sep 30,
# 3:15pm", plus ", 2027" when the year differs. English only, like the rest of
# this module.
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}

# The banner's own reset time, already in LOCAL time. `\s*` throughout so it
# reads the despaced form too ("resets6:40pm", "continuingautomaticallyat
# sep30,9am"). The "(Europe/Bucharest)" suffix is ignored on purpose: the
# clock shown is already the user's own.
_LIMIT_RESET_RE = re.compile(
    r"(?:resets|continuing\s*automatically\s*at|continuing\s*shortly\s*at)"
    r"\s*(?:([a-z]{3})[a-z]*\.?\s*(\d{1,2}),\s*(?:(\d{4}),\s*)?)?"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.I)

# Claude's banner is a SHORT injected message ("You've hit your session limit
# \xb7 resets 3am (Europe/Bucharest)" is ~61 chars). Anything long containing
# those words is an agent TALKING ABOUT the limit, not being stopped by it.
_BANNER_MAX_CHARS = 200

# Everything a TUI can paint to the LEFT of the message itself: indentation,
# the box-drawing gutter Claude draws down its output, the selection caret, and
# the warning glyph agy puts in front of an error (with its variation selector,
# which arrives as a separate code point). Stripped before matching so the
# banners can be anchored at the start of their line.
#
# `⎿` (U+23BF, the tool-result gutter) and `●` (U+25CF, the assistant bullet)
# are load-bearing: a 429 that lands while a tool is running is painted as a
# TOOL-RESULT ROW, "  ⎿ You've hit your session limit \xb7 resets 6:40pm", and
# missing them cost a live 5-hour cut-off on 2026-08-31. U+00A0 rides along
# because Claude separates that gutter from the text with a no-break space.
_GUTTER = " \t\xa0│┃⎿●|>❯⚠✗✘×•*️"

_WS_RE = re.compile(r"\s+")


def _strip_gutter(line: str) -> str:
    return line.lstrip(_GUTTER).strip()


def _despace(text: str) -> str:
    """`text` with every whitespace character removed and lower-cased: the one
    form in which a spaced and a cursor-positioned rendering of the same row
    compare equal. `\\s` covers U+00A0 and U+202F for `str` patterns."""
    return _WS_RE.sub("", text or "").lower()


def _classify(line: str) -> str:
    """What kind of cut-off line `line` is (already gutter-stripped), or ""."""
    if not line or len(line) > _BANNER_MAX_CHARS:
        return ""
    d = _despace(line)
    if not d:
        return ""
    if _HIT_RE.match(d):
        return "hit"
    if _OUT_OF_CREDITS_RE.match(d):
        return "credits"
    if _REACHED_RE.match(d):
        return "reached"
    if _STALE_RE.match(d):
        return "stale"
    return ""


def _find_banner(text: str) -> tuple[str, str]:
    """`(line, next_line)` for the LAST cut-off line in `text`, or ("", "").

    `next_line` is the following non-blank row, gutter-stripped. A narrow card
    wraps "... \xb7 resets" / "6:40pm (Europe/Bucharest)" across two rows, and
    the clock is only readable with both halves."""
    if not text:
        return "", ""
    lines = [_strip_gutter(ln) for ln in text.splitlines()]
    found = ("", "")
    for i, line in enumerate(lines):
        if not _classify(line):
            continue
        nxt = next((ln for ln in lines[i + 1:i + 3] if ln), "")
        found = (line, nxt)
    return found


def banner_line(text: str) -> str:
    """The cut-off line itself (gutter-stripped, otherwise as drawn), or ""
    when `text` holds none.

    Anchoring at the start of the line is the real discriminator: Claude's
    banner is injected as its own line, while an agent discussing the limit
    embeds the same words in a sentence. The length cap is a second guard for
    the case where prose happens to begin with the phrase.

    Returns the LINE rather than a bool so it can double as the identity of
    the cut-off (it names its own reset clock, and successive 5-hour windows
    never end at the same wall time). Compare identities with `banner_key`,
    never with `==` on this: the same row can be spaced on one repaint and
    despaced on the next.

    The LAST match wins: when an old banner and a fresh one are both in view,
    the newest is the one describing the current state.
    """
    return _find_banner(text)[0]


def banner_key(text: str) -> str:
    """The comparable identity of a banner line: despaced and lower-cased, so
    a spaced and a cursor-positioned rendering of one cut-off are equal."""
    return _despace(_strip_gutter(text or ""))


def same_banner(a: str, b: str) -> bool:
    """Whether two banner lines describe the SAME cut-off.

    Equal `banner_key`s, or one a prefix of the other when the shorter one
    already states its clock: a narrow card wraps "(Europe/Bucharest)" or
    "\xb7 progress saved" onto the next row, so the screen's first row is a
    prefix of the transcript's whole line. A prefix that stops BEFORE the
    clock is not enough, because it cannot tell two windows apart."""
    ka, kb = banner_key(a), banner_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    short, long_ = (ka, kb) if len(ka) < len(kb) else (kb, ka)
    return long_.startswith(short) and bool(_LIMIT_RESET_RE.search(short))


def banner_in(text: str) -> bool:
    """True when `text` contains a cut-off line AS one, not prose that merely
    mentions it mid-sentence."""
    return bool(banner_line(text))


def banner_window(text: str) -> str:
    """Which limit window the banner names ("session" / "weekly" / "opus" /
    "sonnet" / "fable" / "usage" / "credit"), or "" when it names none (the
    auto-continue and grace-window lines never do) or `text` holds no banner.

    Worth carrying because a 7-day window's bare clock is not readable off the
    screen: "resets 8pm" on a weekly banner may be days away. See
    `SEVEN_DAY_WINDOWS` and `reset_is_dated`.
    """
    line = banner_line(text)
    kind = _classify(line)
    if kind == "credits":
        return "credit"
    if kind != "hit":
        return ""
    label = _HIT_RE.match(_despace(line)).group(1)
    for key, window in _LABEL_WINDOWS:
        if key in label:
            return window
    return "usage"


def banner_clock_text(text: str) -> str:
    """The text a banner's reset clock should be read from: the banner line,
    plus its wrapped continuation row when the line itself states no clock.
    "" when `text` holds no banner. Reading the whole screen region instead is
    how an unrelated "resets ..." elsewhere on screen (an agent's prose, a
    usage warning) once dated a cut-off four days out."""
    line, nxt = _find_banner(text)
    if not line:
        return ""
    if _LIMIT_RESET_RE.search(line) or not nxt:
        return line
    # Borrow the next row only when this one visibly stops mid-phrase (Ink
    # wraps at word boundaries, so a clock pushed to the next row leaves
    # "resets" / "at" / the separator dangling). A banner that simply HAS no
    # clock ("... usage limit \xb7 contact your admin") must not pick one up
    # from whatever prose follows it.
    if not _despace(line).endswith(("resets", "at", "\xb7", "•")):
        return line
    return line + " " + nxt


def banner_due_now(text: str) -> bool:
    """True when the banner says the window has ALREADY reopened: the stale
    "Your usage limit has reset \xb7 press enter to continue" (Claude is waiting
    for a keypress), or "... continuing shortly" (its own timer is about to
    fire). Either way there is no clock to wait for."""
    line = banner_line(text)
    if not line:
        return False
    return (_classify(line) == "stale"
            or "continuingshortly" in _despace(line))


def is_reset_notice(text: str) -> bool:
    """True when `text` is the CLI's own "Usage limit reset \xb7 continuing
    automatically" line: the cut-off before it ENDED without AI Hive."""
    line = _strip_gutter(text or "")
    return bool(line) and len(line) <= _BANNER_MAX_CHARS \
        and bool(_RESET_NOTICE_RE.match(_despace(line)))


def reset_is_dated(text: str) -> bool:
    """True when the banner's reset names a DATE ("Sep 30, 9am"), which the CLI
    does whenever the reset is more than 24 h out. A dated clock resolves
    exactly; a bare one is only safe for a window that is at most a day long."""
    m = _LIMIT_RESET_RE.search(banner_clock_text(text) or "")
    return bool(m and m.group(1) and m.group(2))


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
    dated form ("Sep 30, 9am", optionally with a year) skips that guess
    entirely and is resolved directly, except across a Dec->Jan boundary,
    where the named date would otherwise land weeks in the past.

    Pass it a banner LINE (`banner_clock_text`), not a whole screen region:
    this reads the first clock it finds.

    `now` is the ANCHOR, and passing the right one is essential for anything
    read from disk — see `banner_reset_at`.
    """
    m = _LIMIT_RESET_RE.search(text or "")
    if not m:
        return None
    mon, day, year, hour, minute, ampm = m.groups()
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
        yr = int(year) if year else lt.tm_year
        try:
            target = time.mktime((yr, month, int(day), hour, minute,
                                  0, 0, 0, -1))
        except (OverflowError, ValueError):
            return None
        # a 7-day reset is at most ~8 days out, so a named date (with no year)
        # that lands more than a few days in the past can only mean the window
        # wraps into next year (a Dec banner naming a January reset).
        if not year and target < now - 3 * 86400:
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

    Reads the banner line only (`banner_clock_text`), so any other "resets"
    in the same message cannot supply the clock. A `text` with no banner in it
    at all is parsed as given, for callers that already hold the line.
    """
    clock = banner_clock_text(text) or text
    return parse_reset_clock(clock, written_at)


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
