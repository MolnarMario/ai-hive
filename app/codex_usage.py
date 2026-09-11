"""Read the signed-in ChatGPT/Codex account's short-window usage.

This is deliberately read-only: Codex owns ``~/.codex/auth.json`` and refreshes
its own tokens.  AI Hive only reads the current access token and asks the same
account endpoint the Codex clients use.  A failed read is represented as data
rather than an exception so a usage pill can say what happened without ever
affecting an agent.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
_USER_AGENT = "ai-hive (codex-usage readout)"


@dataclass(frozen=True)
class CodexLimit:
    """The ChatGPT/Codex primary (normally five-hour) usage window."""

    percent: float
    resets_at: float | None
    window_seconds: float | None = None


@dataclass(frozen=True)
class CodexUsage:
    limit: CodexLimit | None = None
    fetched_at: float = 0.0
    plan: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.limit is not None and not self.error


def auth_path() -> Path:
    """Codex CLI's local login file (never written by AI Hive)."""
    import os
    configured = os.environ.get("CODEX_HOME", "").strip()
    return (Path(configured).expanduser() / "auth.json" if configured
            else Path.home() / ".codex" / "auth.json")


def _read_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8-sig") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def _credentials() -> tuple[str, str, str]:
    """Return access token, account id and a safe error code; never raise."""
    try:
        auth = _read_json(auth_path())
    except (OSError, ValueError):
        return "", "", "no-auth"
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict):
        return "", "", "no-auth"
    token = tokens.get("access_token") or tokens.get("accessToken") or ""
    if not isinstance(token, str) or not token:
        return "", "", "no-auth"
    account_id = (tokens.get("account_id") or tokens.get("accountId")
                  or auth.get("account_id") or "")
    return token, str(account_id), ""


def _timestamp(value, now: float) -> float | None:
    if isinstance(value, (int, float)):
        value = float(value)
        return value / 1000.0 if value > 10_000_000_000 else value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def parse_usage(payload: dict, now: float | None = None) -> CodexLimit | None:
    """Parse Codex's primary rate-limit window, accepting known field variants."""
    if not isinstance(payload, dict):
        return None
    now = time.time() if now is None else now
    rate = payload.get("rate_limit") or payload.get("rateLimit") or payload
    if not isinstance(rate, dict):
        return None
    window = rate.get("primary_window") or rate.get("primaryWindow")
    if not isinstance(window, dict):
        return None
    used = window.get("used_percent")
    if not isinstance(used, (int, float)):
        remaining = window.get("remaining_percent")
        if isinstance(remaining, (int, float)):
            used = 100.0 - float(remaining)
    if not isinstance(used, (int, float)):
        return None
    reset = _timestamp(window.get("reset_at") or window.get("resets_at"), now)
    if reset is None and isinstance(window.get("reset_after_seconds"), (int, float)):
        reset = now + float(window["reset_after_seconds"])
    seconds = window.get("limit_window_seconds") or window.get("window_seconds")
    return CodexLimit(percent=max(0.0, min(100.0, float(used))), resets_at=reset,
                      window_seconds=float(seconds) if isinstance(seconds, (int, float)) else None)


def fetch(timeout: float = 6.0) -> CodexUsage:
    """Fetch the current ChatGPT/Codex primary usage window. Never raises."""
    token, account_id, error = _credentials()
    now = time.time()
    if error:
        return CodexUsage(fetched_at=now, error=error)
    headers = {"Authorization": f"Bearer {token}", "User-Agent": _USER_AGENT}
    if account_id:
        headers["chatgpt-account-id"] = account_id
    try:
        with urllib.request.urlopen(urllib.request.Request(USAGE_URL, headers=headers),
                                    timeout=timeout) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        return CodexUsage(fetched_at=now, error=f"http {exc.code}")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        return CodexUsage(fetched_at=now, error=type(exc).__name__.lower())
    limit = parse_usage(payload, now)
    plan = payload.get("plan_type") or payload.get("planType") or ""
    return CodexUsage(limit=limit, fetched_at=now, plan=str(plan),
                      error="" if limit else "no-limits")


def format_countdown(seconds: float) -> str:
    total = int(max(0, seconds))
    if total >= 3600:
        hours, minutes = divmod(total // 60, 60)
        return f"{hours}h{minutes:02d}m"
    if total >= 60:
        return f"{total // 60}m"
    return f"{total}s"


def format_limit(limit: CodexLimit, now: float | None = None) -> str:
    now = time.time() if now is None else now
    head = "limit reached" if limit.percent >= 100.0 else f"ChatGPT/Codex {limit.percent:.0f}% used"
    if limit.resets_at is None:
        return head
    left = limit.resets_at - now
    if left <= 0:
        return f"{head}, resets now"
    when = datetime.fromtimestamp(limit.resets_at).strftime("%H:%M")
    return f"{head}, resets in {format_countdown(left)} at {when}"
