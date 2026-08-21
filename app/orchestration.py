"""Qt-free orchestration intelligence: task → model/effort.

Kept dependency-free and headless-testable (like providers.py / coordination.py)
so both the UI and the MCP orchestrator call the exact same logic. Manual/
explicit choices always override these heuristics (see resolve_model_effort).

There used to be a task → ROLE heuristic here as well (keyword-matched
"Testing Agent", "Security Analyst", ...). It was removed. Its only live entry
point was the card's "Assign / reassign a task..." dialog, and on the way
through it also RENAMED the agent to the role it had guessed, so the header
printed that guessed string twice (once as the title, once as the sublabel) and
the user lost the name they had chosen. spec.role now only ever holds the kind
descriptor build_spec gives it ("PowerShell", "cmd", "python foo.py"); nothing
infers or mutates it afterwards.
"""

import re

# Task → (model alias, effort). Ordered by descending stakes: a mixed task
# ("refactor and fix a typo") escalates rather than downgrades. The heuristic
# deliberately never returns opus+max/xhigh or fable — top cost/latency is
# reserved for an explicit human/orchestrator override.
_TIER_ARCH = ("architect", "design", "migrat", "security", "auth", "performance",
              "optimize", "redesign", "complex", "distributed", "concurren")
_TIER_MID = ("refactor", "implement", "feature", "endpoint", "integrate",
             "debug", "fix bug", "test", "add ", "build")
_TIER_TRIVIAL = ("typo", "rename", "comment", "format", "whitespace", "spelling",
                 "trivial", "tweak", "small", "quick", "one-liner", "lint")

_FILENAME_RE = re.compile(r"\b[\w./\\-]+\.[a-z]{1,5}\b")


def select_model_effort(task: str) -> tuple[str, str]:
    """Return (model_alias, effort_token) from CLAUDE aliases/tokens."""
    t = f" {(task or '').lower()} "
    # count distinct filenames mentioned — multi-file work escalates
    files = len(set(m.group(0) for m in _FILENAME_RE.finditer(task or "")))

    if any(k in t for k in _TIER_ARCH) or files >= 3:
        return ("opus", "high")
    if any(k in t for k in _TIER_TRIVIAL) and files <= 1 and len(task or "") < 80:
        return ("haiku", "low")
    if any(k in t for k in _TIER_MID) or files >= 1:
        return ("sonnet", "medium")
    return ("sonnet", "medium")   # safe default


def resolve_model_effort(task: str, model: str = "",
                         effort: str = "") -> tuple[str, str]:
    """Precedence: explicit > heuristic. Empty args fall back to the heuristic."""
    auto_m, auto_e = select_model_effort(task)
    return (model or auto_m, effort or auto_e)
