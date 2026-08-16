"""Read and format Google Gemini (Antigravity CLI `agy`) rate-limit utilization.

Qt-free and stdlib-only: nothing here may import PySide6, and nothing here may raise.

Tracks Gemini 3.x rate-limit windows (e.g. 5-hour session window and 7-day weekly
quota window) for Gemini agents.

There is deliberately NO disk cache: see `fetch`. A stale usage number is worse
than none, because nothing on screen tells the two apart.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_LABELS = {
    "five_hour": "Five Hour Limit (5h)",
    "seven_day": "Weekly Limit (all models)",
    "seven_day_pro": "Weekly Limit (Pro)",
    "seven_day_flash": "Weekly Limit (Flash)",
}

_SHORT = {
    "five_hour": "5h",
    "seven_day": "7d",
    "seven_day_pro": "7d Pro",
    "seven_day_flash": "7d Flash",
}

EXHAUSTED_PCT = 100.0


@dataclass(frozen=True)
class GeminiLimit:
    """One Gemini rate-limit window. `percent` is 0..100 used; `resets_at` is epoch
    seconds (UTC), or None when unreadable."""

    key: str
    label: str
    short: str
    percent: float
    resets_at: float | None


@dataclass(frozen=True)
class GeminiUsage:
    """A single Gemini usage reading. Constructible without raising."""

    limits: tuple[GeminiLimit, ...] = ()
    fetched_at: float = 0.0
    source: str = "live"
    plan: str = "gemini-3.x"
    error: str = ""         # "" when valid

    @property
    def ok(self) -> bool:
        return bool(self.limits) and not self.error

    @property
    def blocked(self) -> GeminiLimit | None:
        """The limit window currently spent (>= 100%), or None when headroom remains."""
        return next((l for l in self.limits if l.percent >= EXHAUSTED_PCT), None)

    @property
    def resets_at(self) -> float | None:
        """When the currently binding limit frees up (epoch seconds)."""
        limit = self.blocked or headline(self)
        return limit.resets_at if limit else None


def parse_utilization(data: dict) -> tuple[GeminiLimit, ...]:
    """Extract known Gemini limit windows out of a utilization dict.

    Accepts both direct 'utilization' (% used) and 'remaining_pct' (% remaining,
    where used % = 100 - remaining %).
    """
    out: list[GeminiLimit] = []
    if not isinstance(data, dict):
        return ()
    now = time.time()
    for key, label in _LABELS.items():
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue

        pct: float | None = None
        if "utilization" in entry and isinstance(entry["utilization"], (int, float)):
            pct = float(entry["utilization"])
        elif "remaining_pct" in entry and isinstance(entry["remaining_pct"], (int, float)):
            pct = max(0.0, 100.0 - float(entry["remaining_pct"]))
        elif "percent_remaining" in entry and isinstance(entry["percent_remaining"], (int, float)):
            pct = max(0.0, 100.0 - float(entry["percent_remaining"]))

        if pct is None:
            continue

        reset_val: float | None = None
        if "resets_at" in entry:
            resets_at = entry["resets_at"]
            if isinstance(resets_at, (int, float)):
                reset_val = float(resets_at)
            elif isinstance(resets_at, str):
                try:
                    reset_val = datetime.fromisoformat(resets_at).timestamp()
                except (ValueError, TypeError):
                    reset_val = None
        elif "resets_in_seconds" in entry and isinstance(entry["resets_in_seconds"], (int, float)):
            reset_val = now + float(entry["resets_in_seconds"])

        if reset_val is not None and reset_val <= now:
            pct = 0.0

        out.append(GeminiLimit(key=key, label=label, short=_SHORT.get(key, key),
                              percent=float(pct), resets_at=reset_val))
    return tuple(out)


def headline(usage: GeminiUsage | None) -> GeminiLimit | None:
    """Return the primary binding limit window (e.g. 5-hour limit or highest used %)."""
    if usage is None or not usage.limits:
        return None
    five_hour = next((l for l in usage.limits if l.key == "five_hour"), None)
    if five_hour is not None:
        return five_hour
    return max(usage.limits, key=lambda l: l.percent)


def weekly(usage: GeminiUsage | None) -> GeminiLimit | None:
    """Return the weekly limit window (e.g. seven_day, seven_day_pro, seven_day_flash)."""
    if usage is None or not usage.limits:
        return None
    return next((l for l in usage.limits if l.key.startswith("seven_day")), None)


def fetch_cli(timeout: float = 6.0) -> GeminiUsage | None:
    """Shell out to `agy --print /usage` to fetch live Gemini rate-limit utilization."""
    import re
    import shutil
    import subprocess

    exe = shutil.which("agy") or shutil.which("agy.exe")
    if not exe:
        alt = Path.home() / "AppData" / "Local" / "agy" / "bin" / "agy.exe"
        if alt.exists():
            exe = str(alt)
    if not exe:
        return None

    try:
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        res = subprocess.run([exe, "--print", "/usage"], capture_output=True, text=True, timeout=timeout, **kwargs)
        if res.returncode != 0 or not res.stdout:
            return None
    except (subprocess.SubprocessError, OSError):
        return None

    now = time.time()
    limits_map: dict[str, GeminiLimit] = {}
    for line in res.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            group, limit_name, rem_str, reset_str = parts[0], parts[1], parts[2], parts[3]
            if "gemini" not in group.lower():
                continue
            rem_match = re.search(r"(\d+(?:\.\d+)?)%", rem_str)
            if not rem_match:
                continue
            rem_pct = float(rem_match.group(1))
            used_pct = max(0.0, 100.0 - rem_pct)

            reset_val = None
            if reset_str:
                try:
                    reset_val = datetime.fromisoformat(reset_str.replace("Z", "+00:00")).timestamp()
                except (ValueError, TypeError):
                    reset_val = None

            key = None
            if "five hour" in limit_name.lower():
                key = "five_hour"
            elif "weekly" in limit_name.lower():
                key = "seven_day"

            if key and key in _LABELS:
                limits_map[key] = GeminiLimit(
                    key=key,
                    label=_LABELS[key],
                    short=_SHORT.get(key, key),
                    percent=used_pct,
                    resets_at=reset_val,
                )

    if not limits_map:
        return None

    limits = tuple(limits_map.values())
    return GeminiUsage(limits=limits, fetched_at=now, source="live")


def fetch() -> GeminiUsage:
    """Fetch current Gemini usage metrics on demand. Never raises.

    There is deliberately NO disk cache here, and the one this module used to
    keep has been removed. A usage number is a LIVE READOUT, not history: a
    five-hour window is routinely spent and reopened by the next launch, so a
    stored figure is not merely old, it is WRONG in the direction that matters
    (it says there is headroom when there may be none) — and nothing on screen
    distinguishes a restored number from a fetched one. A failed read reports
    itself instead, and the caller paints the can't-read pill, which keeps the
    click-to-refresh affordance.
    """
    live = fetch_cli()
    if live is not None and live.ok:
        return live
    return GeminiUsage(fetched_at=time.time(), source="live", error="no-data")


def format_countdown(seconds: float) -> str:
    """Format compact '6d23h' / '1h20m' / '45m' / '30s' countdown."""
    total = int(max(0, seconds))
    if total >= 86400:
        d = total // 86400
        h = (total % 86400) // 3600
        return f"{d}d{h}h" if h > 0 else f"{d}d"
    if total >= 3600:
        h, m = divmod(total // 60, 60)
        return f"{h}h{m:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def format_countdown_dh(seconds: float) -> str:
    """Days+hours only, no minutes: "6d23h" / "6d" / "13h" / "<1h".

    For the 7-day window: a week-long countdown doesn't need to-the-minute
    precision, and dropping minutes keeps it readable at a glance whether the
    window has days or just hours left. Mirrors `claude_usage.format_countdown_dh`.
    """
    total = int(max(0, seconds))
    if total < 3600:
        return "<1h"
    d, rem = divmod(total, 86400)
    h = rem // 3600
    if d > 0:
        return f"{d}d{h}h" if h > 0 else f"{d}d"
    return f"{h}h"


def format_limit(limit: GeminiLimit, now: float | None = None,
                 with_label: bool = False, days_only: bool = False) -> str:
    """Format badge text for Gemini usage limit.

    `days_only` drops the countdown to day+hour granularity (no minutes) —
    set it for the 7-day window pill.
    """
    now = time.time() if now is None else now
    head = ("limit reached" if limit.percent >= EXHAUSTED_PCT
            else f"Gemini {limit.percent:.0f}% used")
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

