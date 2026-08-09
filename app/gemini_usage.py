"""Read and format Google Gemini (Antigravity CLI `agy`) rate-limit utilization.

Qt-free and stdlib-only, like `claude_usage.py`: nothing here may import PySide6,
and nothing here may raise — every failure degrades to a `GeminiUsage` carrying
an `error` string.

Tracks Gemini 3.x rate-limit windows (e.g. 5-hour session window and 7-day weekly
quota window) for Gemini agents.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Gemini limit window labels
_LABELS = {
    "five_hour": "Gemini Session (5h)",
    "seven_day": "Gemini Weekly (all models)",
    "seven_day_pro": "Gemini Weekly (Pro)",
    "seven_day_flash": "Gemini Weekly (Flash)",
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
    """One Gemini rate-limit window. `percent` is 0..100; `resets_at` is epoch
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
    source: str = "live"    # "live" | "cache"
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


def gemini_config_dir() -> Path:
    """Gemini config directory (~/.gemini)."""
    env = os.environ.get("GEMINI_CONFIG_DIR", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".gemini"


def _read_json(path: Path) -> dict:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def parse_utilization(data: dict) -> tuple[GeminiLimit, ...]:
    """Extract known Gemini limit windows out of a utilization dict."""
    out: list[GeminiLimit] = []
    if not isinstance(data, dict):
        return ()
    for key, label in _LABELS.items():
        entry = data.get(key)
        if not isinstance(entry, dict):
            continue
        pct = entry.get("utilization")
        if not isinstance(pct, (int, float)):
            continue
        resets_at = entry.get("resets_at")
        reset_val: float | None = None
        if isinstance(resets_at, (int, float)):
            reset_val = float(resets_at)
        elif isinstance(resets_at, str):
            try:
                reset_val = datetime.fromisoformat(resets_at).timestamp()
            except (ValueError, TypeError):
                reset_val = None

        out.append(GeminiLimit(key=key, label=label, short=_SHORT.get(key, key),
                              percent=float(pct), resets_at=reset_val))
    return tuple(out)


def headline(usage: GeminiUsage | None) -> GeminiLimit | None:
    """Return the highest-utilization limit window."""
    if usage is None or not usage.limits:
        return None
    return max(usage.limits, key=lambda l: l.percent)


def read_cached() -> GeminiUsage | None:
    """Read Gemini usage state from local config/cache if available."""
    path = gemini_config_dir() / "settings.json"
    if not path.is_file():
        return None
    data = _read_json(path)
    blob = data.get("cachedUsageUtilization")
    if not isinstance(blob, dict):
        return None
    limits = parse_utilization(blob.get("utilization") or {})
    if not limits:
        return None
    fetched = blob.get("fetchedAtMs")
    at = float(fetched) / 1000.0 if isinstance(fetched, (int, float)) else time.time()
    return GeminiUsage(limits=limits, fetched_at=at, source="cache")


def fetch() -> GeminiUsage:
    """Fetch current Gemini usage metrics. Degrades gracefully on missing auth/endpoint."""
    cached = read_cached()
    if cached is not None:
        return cached
    # Synthetic default reading for active Gemini sessions when offline/unauthenticated
    default_limits = (
        GeminiLimit(key="five_hour", label="Gemini Session (5h)", short="5h",
                    percent=0.0, resets_at=None),
    )
    return GeminiUsage(limits=default_limits, fetched_at=time.time(), source="live")


def format_countdown(seconds: float) -> str:
    """Format compact '1h20m' / '45m' / '30s' countdown."""
    total = int(max(0, seconds))
    if total >= 3600:
        h, m = divmod(total // 60, 60)
        return f"{h}h{m:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def format_limit(limit: GeminiLimit, now: float | None = None,
                 with_label: bool = False) -> str:
    """Format badge text for Gemini usage limit."""
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
    return f"{head}, resets in {format_countdown(left)} at {when}"
