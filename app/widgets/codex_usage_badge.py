"""Top-bar GPT (ChatGPT/Codex) five-hour usage readout."""

from __future__ import annotations

import time

from app import codex_usage
from app.widgets.ornaments import UsagePillBadge


class CodexUsageBadge(UsagePillBadge):
    """The signed-in ChatGPT account's Codex primary usage window, labelled
    "GPT" on the bar."""

    _PROVIDER = "codex"

    def _pick_limit(self, usage):
        return usage.limit

    def _loading_text(self) -> str:
        return "GPT 5h usage, reading..."

    def _format_limit(self) -> str:
        return codex_usage.format_limit(self._limit)

    def _unreadable_text(self) -> str:
        return "GPT usage unreadable, click to refresh"

    def _build_tooltip(self) -> str:
        if self._loading:
            return "Reading GPT 5-hour usage..."
        if self._limit is None and self._unreadable:
            return ("GPT usage could not be read.\n"
                    f"Last attempt failed: {self._unreadable}\nClick to refresh.")
        if self._usage is None or self._limit is None:
            return "GPT 5-hour usage"
        lines = ["GPT" + (f" ({self._usage.plan})" if self._usage.plan else ""),
                 codex_usage.format_limit(self._limit)]
        if self._usage.fetched_at:
            lines.append(f"Updated {codex_usage.format_countdown(time.time() - self._usage.fetched_at)} ago")
        lines.extend(self._failure_lines())
        lines.append("Click to refresh")
        return "\n".join(lines)
