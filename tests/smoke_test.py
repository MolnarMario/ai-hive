"""AI Hive end-to-end smoke test.

Run with the project venv, no display needed:
    .venv\\Scripts\\python.exe tests\\smoke_test.py            (full)
    .venv\\Scripts\\python.exe tests\\smoke_test.py --quick    (no real claude)
    .venv\\Scripts\\python.exe tests\\smoke_test.py -k NAME    (matching tests)

Drives the REAL application (create_main_window) on the offscreen Qt
platform with real child processes; prints [PASS]/[FAIL]/[SKIP] per check,
saves screenshots to tests/screenshots/, exits nonzero on any failure. A
test that raises is one FAIL and the run goes on. Every wait has a timeout,
so a hang reads as a deterministic FAIL. Runs under a throwaway profile, see
smoke/harness.py.

The tests live in tests/smoke/<topic>.py. Every `test_*` function in a module
listed in MODULES runs, in file order, so a new test needs no registration.
"""

import sys
import traceback

from smoke import harness  # first: sets up the sandbox profile
from smoke import (attention, board, e2e, limits, processes, reply_marks,
                   sessions, sidebar, terminal, updates, usage, window)
from smoke.harness import check

# Run order. e2e last: it launches a real claude.
MODULES = [sidebar, attention, processes, terminal, reply_marks, sessions,
           limits, usage, board, window, updates, e2e]

# Skipped by --quick: real, billed, minutes-long. Run the full suite before a
# merge; --quick is for the edit-run loop.
SLOW_TESTS = {"test_lifecycle_e2e"}


def collect():
    """Every test_* function defined in MODULES, in file order. Found, not
    listed: under the old hand-kept list, test_workspace_header sat
    unregistered for six weeks and never ran."""
    tests, seen = [], {}
    for mod in MODULES:
        for name, fn in vars(mod).items():
            if (name.startswith("test_") and callable(fn)
                    and getattr(fn, "__module__", None) == mod.__name__):
                # -k selects by bare name, so a name must mean one test
                if name in seen:
                    raise SystemExit(f"{name} is defined in both "
                                     f"{seen[name]} and {mod.__name__}")
                seen[name] = mod.__name__
                tests.append(fn)
    return tests


def _parse_args(argv):
    """--quick skips SLOW_TESTS. -k PATTERN (repeatable) runs only the tests
    whose name contains one of the patterns."""
    quick, patterns, it = False, [], iter(argv)
    for arg in it:
        if arg == "--quick":
            quick = True
        elif arg == "-k":
            patterns.append(next(it, ""))
        elif arg.startswith("-k"):
            patterns.append(arg[2:])
        else:
            raise SystemExit(f"unknown argument: {arg} (use --quick, -k NAME)")
    return quick, patterns


def main(argv=()):
    quick, patterns = _parse_args(list(argv))
    harness._guard_real_ai_launches()
    tests = collect()
    selected = [t for t in tests
                if not (quick and t.__name__ in SLOW_TESTS)
                and (not patterns
                     or any(p in t.__name__ for p in patterns))]
    for test in selected:
        # one crashing test is one FAIL, never the end of the run: before
        # this, an exception in an early test hid every check after it
        try:
            test()
        except Exception:
            traceback.print_exc()
            check(f"{test.__name__}: crashed", False)
    check("suite: no test launched a real AI CLI outside the e2e test",
          not harness.REAL_AI_LAUNCHES, harness.REAL_AI_LAUNCHES)
    print(f"\nRESULT: {harness.PASS} passed, {harness.FAIL} failed, "
          f"{harness.SKIP} skipped ({len(selected)} of {len(tests)} tests)",
          flush=True)
    return 1 if harness.FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        traceback.print_exc()
        print(f"\nRESULT: {harness.PASS} passed, {harness.FAIL + 1} failed "
              f"(crash)", flush=True)
        sys.exit(1)
    finally:
        harness._remove_fixture_transcripts()
        harness._remove_sandbox_home()
