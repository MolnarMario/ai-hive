"""Qt-free orchestration intelligence: task → role, and task → model/effort.

Kept dependency-free and headless-testable (like providers.py / coordination.py)
so both the UI and the MCP orchestrator call the exact same logic. Manual/
explicit choices always override these heuristics (see resolve_model_effort).
"""

import re

# Canonical roles, ordered most-specific-first so the first keyword hit wins.
ROLE_KEYWORDS = [
    ("Authentication Specialist",
     ("auth", "login", "oauth", "jwt", "session", "password", "sso",
      "permission", "rbac", "credential")),
    ("Database Engineer",
     ("database", " db ", "sql", "schema", "migration", "query", "index",
      "postgres", "sqlite", "orm", "table")),
    ("Testing Agent",
     ("test", "pytest", "coverage", "e2e", "fixture", " mock", "assert",
      "regression")),
    ("Refactoring Agent",
     ("refactor", "rename", "cleanup", "clean up", "extract", "dedupe",
      "tidy", "restructure", "simplif")),
    ("Documentation Agent",
     ("document", " doc", "docs", "readme", "docstring", "changelog",
      "guide", "comment")),
    ("Build Engineer",
     ("build", " ci", "pipeline", "package", "bundle", "deploy", "docker",
      "release", "makefile", "install")),
    ("Performance Analyst",
     ("perf", "performance", "optimize", "optimise", "profil", "latency",
      "benchmark", "memory", "slow", "throughput")),
    ("Security Analyst",
     ("security", "vulnerab", "exploit", "sanitiz", "injection", "xss", "csrf")),
    ("UI Designer",
     (" ui", " ux", "widget", "css", "layout", "style", "frontend", "button",
      "dialog", "qss", "view", "component")),
    ("Backend Architect",
     ("architect", "design", " api", "endpoint", "service", "backend",
      "integrate", "system", "server")),
]

DEFAULT_ROLE = "Worker"

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


def infer_role(task: str) -> str:
    t = f" {(task or '').lower()} "
    for role, keywords in ROLE_KEYWORDS:
        if any(k in t for k in keywords):
            return role
    return DEFAULT_ROLE


def select_model_effort(task: str, role: str = "") -> tuple[str, str]:
    """Return (model_alias, effort_token) from CLAUDE aliases/tokens."""
    t = f" {(task or '').lower()} "
    hi_role = role in ("Backend Architect", "Security Analyst",
                       "Performance Analyst", "Authentication Specialist")
    # count distinct filenames mentioned — multi-file work escalates
    files = len(set(m.group(0) for m in _FILENAME_RE.finditer(task or "")))

    if hi_role or any(k in t for k in _TIER_ARCH) or files >= 3:
        return ("opus", "high")
    if any(k in t for k in _TIER_TRIVIAL) and files <= 1 and len(task or "") < 80:
        return ("haiku", "low")
    if any(k in t for k in _TIER_MID) or files >= 1:
        return ("sonnet", "medium")
    return ("sonnet", "medium")   # safe default


def resolve_model_effort(task: str, role: str = "", model: str = "",
                         effort: str = "") -> tuple[str, str]:
    """Precedence: explicit > heuristic. Empty args fall back to the heuristic."""
    auto_m, auto_e = select_model_effort(task, role)
    return (model or auto_m, effort or auto_e)
