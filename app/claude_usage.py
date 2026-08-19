"""Read the logged-in Claude account's PLAN USAGE (rate-limit utilization).

Qt-free and stdlib-only, like `chime.py` / `session_hook.py`: nothing here may
import PySide6, and nothing here may raise — every failure degrades to a
`Usage` carrying an `error` string, so a missing login or a dead network can
never take the GUI down.

WHERE THE NUMBER COMES FROM. There is no CLI path: `claude --help` (2.1.220)
exposes agents/auth/doctor/mcp/plugin/project/setup-token/ultrareview/update and
nothing else, so shelling out is impossible. What the TUI's `/usage` actually
does is `GET /api/oauth/usage` against the API host with the account's OAuth
bearer token (the binary logs it verbatim: "fetchUtilization: GET
/api/oauth/usage"), and that is what `fetch()` reproduces. The response is a map
of limit-window name -> {utilization, resets_at} (plus `limits[]`, `extra_usage`
and `spend`, which we ignore).

The same payload is ALSO cached on disk by the CLI at `~/.claude.json` ->
`cachedUsageUtilization`, and `read_cached()` parses it with the very same
`parse_utilization`. That copy is only a COLD-START SEED and an offline
fallback, never the primary source: the CLI rewrites it opportunistically, so it
goes stale for days (observed 1.5 days / 10 points out of date while the live
endpoint was correct). Always prefer `fetch()`; fall back to the cache.

TOKEN HANDLING IS STRICTLY READ-ONLY. The bearer token is re-read from
`.credentials.json` on every call — AI Hive keeps Claude agents running, and
those agents rotate the file for us, so a fresh token is simply there. We never
refresh it (that would race the CLI's own refresh and could invalidate the
user's session), never write the file, and never log or persist the token: an
expired `expiresAt` short-circuits to `error="expired"` instead of putting a
dead credential on the wire.

Nothing here is persisted into AI Hive's session either. Usage is a live
readout, not history — Anthropic keeps the record on their side.

THIS MODULE IS ALSO A HOOK POINT, not just a pixel. `Usage.blocked` reports the
limit window that is currently spent (>= EXHAUSTED_PCT) and `Usage.resets_at`
says when it frees up, so a feature that wants to act on being cut off — e.g.
relaunching agents that died on a limit, unattended, the moment it resets — can
read those two instead of scraping a terminal for "You've hit your session
limit". `MainWindow` turns the same information into edge-triggered
`planLimitReached` / `planLimitCleared` signals; see the plan-usage invariant in
CLAUDE.md.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# The endpoint + beta header the CLI itself uses; the User-Agent mirrors the
# CLI's shape so the request is unremarkable to the server.
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA = "oauth-2025-04-20"
_USER_AGENT = "ai-hive (plan-usage readout)"

# utilization at which a window is spent and the account is cut off
EXHAUSTED_PCT = 100.0

# Limit windows we surface, in the order the TUI lists them. Anything else in
# the payload (promotional/experimental buckets, extra_usage, spend) is ignored:
# an unknown key is not worth guessing a label for.
_LABELS = {
    "five_hour": "Current session (5h)",
    "seven_day": "Current week (all models)",
    "seven_day_opus": "Current week (Opus)",
    "seven_day_sonnet": "Current week (Sonnet)",
}
# compact forms for the badge, used only when more than one limit is live
_SHORT = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_opus": "7d Opus",
    "seven_day_sonnet": "7d Sonnet",
}


@dataclass(frozen=True)
class Limit:
    """One rate-limit window. `percent` is 0..100; `resets_at` is epoch
    seconds (UTC), or None when the server didn't say."""

    key: str
    label: str
    short: str
    percent: float
    resets_at: float | None


@dataclass(frozen=True)
class Usage:
    """A single reading. Always constructible — `error` carries the reason when
    `limits` is empty, so callers branch on data, not on exceptions."""

    limits: tuple[Limit, ...] = ()
    fetched_at: float = 0.0
    source: str = "live"   # "live" (endpoint) | "cache" (~/.claude.json)
    plan: str = ""         # subscriptionType, e.g. "pro" / "max"
    error: str = ""        # "" when the reading is good

    @property
    def ok(self) -> bool:
        return bool(self.limits) and not self.error

    @property
    def blocked(self) -> Limit | None:
        """The limit that is currently OUT, or None while there's headroom.

        This is the machine-readable half of the feature and the hook other
        parts of the app are meant to build on (e.g. relaunching agents that
        died on a limit, once it resets): `usage.blocked` answers "am I cut
        off", and `usage.blocked.resets_at` answers "until when".

        Exhaustion is derived from utilization rather than from the payload's
        `severity` string: `severity` is server-side vocabulary we don't
        control (only ever observed as "normal"), whereas ">= 100% used" means
        the same thing on every plan and can't drift under us.
        """
        return next((l for l in self.limits if l.percent >= EXHAUSTED_PCT), None)

    @property
    def resets_at(self) -> float | None:
        """When the CURRENTLY BINDING limit frees up (epoch seconds) — the
        blocked one if we're cut off, else whichever is closest to biting."""
        limit = self.blocked or headline(self)
        return limit.resets_at if limit else None


# ------------------------------------------------------------------ paths ---

def config_dir() -> Path:
    """Claude's config directory — CLAUDE_CONFIG_DIR wins, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".claude"


def credentials_path() -> Path:
    return config_dir() / ".credentials.json"


def cache_path() -> Path:
    """The CLI's global config blob, which holds `cachedUsageUtilization`.

    Note this is `~/.claude.json` (a FILE beside the config dir), not a file
    inside it — and it follows CLAUDE_CONFIG_DIR when that is set.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env) / ".claude.json"
    return Path.home() / ".claude.json"


def _read_json(path: Path) -> dict:
    # utf-8-sig for the same reason the session store uses it: a BOM written by
    # some other tool must not make the file unreadable.
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


# ------------------------------------------------------------- parsing ------

def _parse_reset(value) -> float | None:
    """`resets_at` is ISO-8601 with an offset ("...+00:00") from the endpoint,
    but the CLI has historically also written a bare epoch. Accept both."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (ValueError, TypeError):
        return None


def parse_utilization(data: dict) -> tuple[Limit, ...]:
    """Pull the known limit windows out of a utilization payload.

    Shared by the live endpoint and the on-disk cache — the two carry byte-identical
    shapes, which is exactly why there is one parser. Null windows (a Pro account
    has no weekly limit, so `seven_day` is null) are skipped rather than reported
    as 0%, which would read as "plenty left" when the truth is "not applicable".
    """
    out: list[Limit] = []
    if not isinstance(data, dict):
        return ()
    for key, label in _LABELS.items():
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("utilization")
        if not isinstance(pct, (int, float)):
            continue
        out.append(Limit(key=key, label=label, short=_SHORT.get(key, key),
                         percent=float(pct),
                         resets_at=_parse_reset(entry.get("resets_at"))))
    return tuple(out)


def headline(usage: Usage | None) -> Limit | None:
    """The limit to show at a glance: whichever is closest to biting.

    On Pro only `five_hour` is ever non-null so this is simply the session
    limit; on Max it surfaces the weekly/Opus window automatically when that is
    the one about to stop you, with no code change.

    This stays the ACCOUNT-WIDE reading used by the machine hooks (`blocked`,
    `resets_at`, `planLimitReached`/`Cleared`) — it deliberately does NOT
    distinguish 5h from 7d. `five_hour`/`weekly` below are for the two SEPARATE
    top-bar pills, which must never show the same window by coincidence.
    """
    if usage is None or not usage.limits:
        return None
    return max(usage.limits, key=lambda l: l.percent)


def five_hour(usage: Usage | None) -> Limit | None:
    """The 5-hour session window, specifically.

    Unlike `headline`, this never falls back to a different window: the 5h
    and 7d pills are separate readouts now, and a fallback would let one
    silently show the other's number.
    """
    if usage is None or not usage.limits:
        return None
    return next((l for l in usage.limits if l.key == "five_hour"), None)


def weekly(usage: Usage | None) -> Limit | None:
    """The most binding 7-day window: `seven_day` (all models) on most plans,
    or whichever per-model 7-day window (`seven_day_opus`/`seven_day_sonnet`,
    the Max-plan breakdown) is closest to biting when there's no all-models
    figure."""
    if usage is None or not usage.limits:
        return None
    weeklies = [l for l in usage.limits if l.key.startswith("seven_day")]
    if not weeklies:
        return None
    return max(weeklies, key=lambda l: l.percent)


# ------------------------------------------------------------- reading ------

def _token() -> tuple[str, str]:
    """(access_token, error). Read fresh every call — never cached, never
    written back, never logged."""
    try:
        creds = _read_json(credentials_path())
    except (OSError, ValueError):
        return "", "no-auth"
    oauth = creds.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return "", "no-auth"          # API-key user, or not logged in
    token = oauth.get("accessToken") or ""
    if not token:
        return "", "no-auth"
    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 <= time.time():
        # Don't put a dead credential on the wire. The CLI refreshes it as soon
        # as any agent talks to the API; the next poll will simply succeed.
        return "", "expired"
    return str(token), ""


def _plan() -> str:
    try:
        oauth = _read_json(credentials_path()).get("claudeAiOauth")
    except (OSError, ValueError):
        return ""
    return str(oauth.get("subscriptionType") or "") if isinstance(oauth, dict) else ""


def fetch(timeout: float = 6.0) -> Usage:
    """Live reading from `/api/oauth/usage`. Never raises."""
    token, err = _token()
    if err:
        return Usage(fetched_at=time.time(), source="live", error=err)
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "anthropic-beta": OAUTH_BETA,
        "User-Agent": _USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as e:
        # 401 means the token went stale between our read and the request; the
        # CLI will refresh it, so this is transient, not fatal.
        return Usage(fetched_at=time.time(), source="live",
                     error=f"http {e.code}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        return Usage(fetched_at=time.time(), source="live",
                     error=type(e).__name__.lower())
    limits = parse_utilization(payload if isinstance(payload, dict) else {})
    return Usage(limits=limits, fetched_at=time.time(), source="live",
                 plan=_plan(), error="" if limits else "no-limits")


def read_cached() -> Usage | None:
    """The CLI's own last reading, for an instant first paint at startup.

    Returns None when there is no usable cache. `fetched_at` is the CLI's
    timestamp, NOT now — so a caller can tell the user how old this is and
    replace it the moment a live fetch lands.
    """
    try:
        blob = _read_json(cache_path()).get("cachedUsageUtilization")
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    limits = parse_utilization(blob.get("utilization") or {})
    if not limits:
        return None
    fetched = blob.get("fetchedAtMs")
    at = float(fetched) / 1000.0 if isinstance(fetched, (int, float)) else 0.0
    return Usage(limits=limits, fetched_at=at, source="cache", plan=_plan())


# ----------------------------------------------------------- formatting -----

def format_countdown(seconds: float) -> str:
    """A compact "1h20m" / "45m" / "30s" countdown."""
    total = int(max(0, seconds))
    if total >= 3600:
        h, m = divmod(total // 60, 60)
        return f"{h}h{m:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def format_countdown_dh(seconds: float) -> str:
    """Days+hours only, no minutes: "6d23h" / "6d" / "13h" / "<1h".

    For the 7-day window: a week-long countdown doesn't need to-the-minute
    precision, and "167h23m" (what `format_countdown` gives a multi-day
    duration, since it never rolls hours into days) is unreadable at a glance.
    """
    total = int(max(0, seconds))
    if total < 3600:
        return "<1h"
    d, rem = divmod(total, 86400)
    h = rem // 3600
    if d > 0:
        return f"{d}d{h}h" if h > 0 else f"{d}d"
    return f"{h}h"


def format_since(seconds: float) -> str:
    """Age of a reading: "just now" / "42s ago" / "3m ago" / "2h ago"."""
    total = int(max(0, seconds))
    if total < 5:
        return "just now"
    if total < 60:
        return f"{total}s ago"
    if total < 3600:
        return f"{total // 60}m ago"
    if total < 86400:
        return f"{total // 3600}h ago"
    return f"{total // 86400}d ago"


def format_limit(limit: Limit, now: float | None = None,
                 with_label: bool = False, days_only: bool = False) -> str:
    """The badge line: "21% used, resets in 1h20m at 14:49".

    Countdown FIRST, wall-clock second (the user's chosen order): "how long have
    I got" is the question being asked; the clock time is the follow-up. The
    time is rendered in LOCAL time via `datetime.fromtimestamp` — the payload's
    `resets_at` is UTC, and the whole point is to read it in your own timezone.

    `days_only` drops the countdown to day+hour granularity (no minutes) — set
    it for a 7-day window, where "3d14h" reads better than "3d14h27m".
    """
    now = time.time() if now is None else now
    # spell the blocked state out rather than showing a bare "100% used" —
    # this is the state the user most needs to read at a glance
    head = ("limit reached" if limit.percent >= EXHAUSTED_PCT
            else f"{limit.percent:.0f}% used")
    if with_label:
        head = f"{limit.short} {head}"
    if limit.resets_at is None:
        return head
    left = limit.resets_at - now
    when = datetime.fromtimestamp(limit.resets_at).strftime("%H:%M")
    if left <= 0:
        return f"{head}, resets now"
    countdown = format_countdown_dh(left) if days_only else format_countdown(left)
    return f"{head}, resets in {countdown} at {when}"
