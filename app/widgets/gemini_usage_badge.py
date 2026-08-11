"""Top-bar visual readout for Gemini (Antigravity CLI `agy`) rate-limit
utilization.

Everything structural lives in `ornaments.UsagePillBadge`: the ring, the text,
the hover X, and above all the WIDTH FORMULA. This file used to carry its own
`_FIXED_WIDTH = 315` and elide into it, which reserved 630px of the bar for two
pills whose real content is ~215px each, truncated anything longer, and drifted
from the Claude pill sitting immediately beside it. The pill's width is now the
text's width, measured exactly as the Claude pill measures its own.

What is genuinely Gemini's own, and all that is left here: which window of the
reading this pill shows (`five_hour` vs `weekly`), and the wording.
"""

from __future__ import annotations

import time

from app import gemini_usage
from app.widgets.ornaments import UsagePillBadge


class GeminiUsageBadge(UsagePillBadge):
    """The top-bar readout of one Gemini rate-limit window:

        (o) 5h Gemini 18% used, resets in 2h45m at 17:30
    """

    def __init__(self, parent=None, window: str = "five_hour"):
        super().__init__(parent)
        self.window = window
        # always name the window: the two Gemini pills are otherwise identical
        # on the bar, and "which one is the weekly?" is the whole question
        self._label = True

    def set_usage(self, usage) -> None:
        """Adopt a reading. Never calls `setVisible` — a pill is on the bar when
        it has content AND the user wants it, and only `TopBar` knows both. This
        used to hide ITSELF whenever a reading carried no matching window, which
        is the same silent-disappearance the Claude pill's can't-read state
        exists to prevent; failures now route through `mark_unreadable`."""
        self._loading = False
        self._usage = usage
        self._limit = (gemini_usage.weekly(usage) if self.window == "weekly"
                       else gemini_usage.headline(usage))
        self._unreadable = ""
        self._stale = bool(usage.error) if usage else False
        self._refresh_text()

    # -- wording ---------------------------------------------------------
    def _loading_text(self) -> str:
        return ("Gemini 7d usage, reading..." if self.window == "weekly"
                else "Gemini 5h usage, reading...")

    def _format_limit(self) -> str:
        return gemini_usage.format_limit(self._limit, with_label=self._label)

    def _unreadable_text(self) -> str:
        return "Gemini limit unreadable, click to refresh"

    def _build_tooltip(self) -> str:
        if self._loading:
            return "Reading the Gemini rate-limit usage..."
        if self._limit is None and self._unreadable:
            return ("Gemini rate-limit usage could not be read.\n"
                    f"Last attempt failed: {self._unreadable}\n"
                    "Click to refresh.")
        if self._usage is None or self._limit is None:
            return "Gemini rate-limit usage"
        lines = [f"Gemini ({self._usage.plan})"]
        for lim in self._usage.limits:
            lines.append(f"{lim.label}: " + gemini_usage.format_limit(lim))
        if self._usage.fetched_at:
            age = gemini_usage.format_countdown(
                time.time() - self._usage.fetched_at)
            lines.append(f"Updated {age} ago")
        if self._usage.error:
            lines.append(f"Last refresh failed: {self._usage.error}")
        lines.append("Click to refresh")
        return "\n".join(lines)
