"""AI Hive end-to-end smoke test.

Run with the project venv, no display needed:
    .venv\\Scripts\\python.exe tests\\smoke_test.py            (full)
    .venv\\Scripts\\python.exe tests\\smoke_test.py --quick    (no real claude)
    .venv\\Scripts\\python.exe tests\\smoke_test.py -k NAME    (matching tests)
    .venv\\Scripts\\python.exe tests\\smoke_test.py -j 1       (one process)

Drives the REAL application (create_main_window) on the offscreen Qt
platform with real child processes; prints [PASS]/[FAIL]/[SKIP] per check,
saves screenshots to tests/screenshots/, exits nonzero on any failure. A
test that raises is one FAIL and the run goes on. Every wait has a timeout,
so a hang reads as a deterministic FAIL. Runs under a throwaway profile, see
smoke/harness.py.

The tests live in tests/smoke/<topic>.py. Every `test_*` function in a module
listed in MODULES runs, so a new test needs no registration.

By default the tests run in parallel worker processes (-j, default up to 6).
Most of the suite's time is spent waiting on child processes and timers, so
6 workers cut the quick run from about 6 minutes to about 1.5. Each worker
imports smoke.harness itself and so gets its own throwaway profile; the
parent hands out one test at a time, slowest first by the durations the last
run saved in tests/.smoke-times.json, and prints each test's output as one
block when it ends. Output order is completion order. A failure that only
shows in parallel is an order or load dependence: rerun the test alone with
-k NAME -j 1.
"""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from smoke import harness  # first: sets up the sandbox profile
from smoke import (attention, board, e2e, lanes, limits, processes,
                   reply_marks, runner, sessions, sidebar, terminal, updates,
                   usage, window)
from smoke.harness import check

# Run order with -j 1. e2e last: it launches a real claude.
MODULES = [sidebar, attention, processes, terminal, reply_marks, sessions,
           limits, usage, board, window, updates, lanes, runner, e2e]

# Skipped by --quick: real, billed, minutes-long. Run the full suite before a
# merge; --quick is for the edit-run loop.
SLOW_TESTS = {"test_lifecycle_e2e"}

# More than 6 workers on an 8-core machine bought nothing measurable and
# pushes the timing-sensitive tests (task delivery waits for Claude's echo)
# closer to their limits.
DEFAULT_JOBS = max(1, min(6, os.cpu_count() or 1))
TIMES_FILE = Path(__file__).resolve().parent / ".smoke-times.json"
# A worker prints this, then JSON, after each test. Nothing else in the suite
# prints it.
SENTINEL = "@@smoke-worker@@ "
# The parent sets this on every worker it starts. A worker leaves the sweep
# of the real ~/.claude/projects to the parent: in a worker it could delete
# the folder of an e2e test still running in another worker.
WORKER_ENV = "AIHIVE_SMOKE_WORKER"


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
    whose name contains one of the patterns. -j N runs N worker processes,
    -j 1 runs everything in this process."""
    quick, patterns, jobs, worker, it = False, [], DEFAULT_JOBS, False, \
        iter(argv)
    for arg in it:
        if arg == "--quick":
            quick = True
        elif arg == "--worker":
            worker = True
        elif arg in ("-k", "-j"):
            value = next(it, "")
            if arg == "-k":
                patterns.append(value)
            else:
                jobs = _jobs(value)
        elif arg.startswith("-k"):
            patterns.append(arg[2:])
        elif arg.startswith("-j"):
            jobs = _jobs(arg[2:])
        else:
            raise SystemExit(f"unknown argument: {arg} "
                             f"(use --quick, -k NAME, -j N)")
    return quick, patterns, jobs, worker


def _jobs(value):
    try:
        jobs = int(value)
    except ValueError:
        jobs = 0
    if jobs < 1:
        raise SystemExit(f"-j needs a positive number, got {value!r}")
    return jobs


def _load_times():
    try:
        times = json.loads(TIMES_FILE.read_text(encoding="utf-8"))
        return {k: float(v) for k, v in times.items()}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def _save_times(measured):
    """Merge this run's durations into the file. A -k run updates only the
    tests it ran."""
    if not measured:
        return
    times = _load_times()
    times.update({k: round(v, 2) for k, v in measured.items()})
    try:
        # own temp name: two runs in one checkout may finish together
        tmp = TIMES_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(times, indent=1, sort_keys=True),
                       encoding="utf-8")
        os.replace(tmp, TIMES_FILE)
    except OSError:
        pass  # a missing timing file only costs scheduling order


def _run_one(test):
    """Run one test in this process. Returns its duration."""
    started = time.perf_counter()
    # one crashing test is one FAIL, never the end of the run: before
    # this, an exception in an early test hid every check after it
    try:
        test()
    except Exception:
        traceback.print_exc()
        check(f"{test.__name__}: crashed", False)
    return time.perf_counter() - started


def _finish(selected, total, wall):
    check("suite: no test launched a real AI CLI outside the e2e test",
          not harness.REAL_AI_LAUNCHES, harness.REAL_AI_LAUNCHES)
    print(f"\nRESULT: {harness.PASS} passed, {harness.FAIL} failed, "
          f"{harness.SKIP} skipped ({selected} of {total} tests, "
          f"{wall:.0f}s)", flush=True)
    return 1 if harness.FAIL else 0


def _run_serial(selected, total):
    started = time.perf_counter()
    measured = {}
    for test in selected:
        measured[test.__name__] = _run_one(test)
    _save_times(measured)
    return _finish(len(selected), total, time.perf_counter() - started)


def _worker_loop(by_name):
    """--worker: run the test named on each stdin line, then print the
    sentinel with this test's counts. Exits at EOF."""
    for line in sys.stdin:
        name = line.strip()
        if not name:
            continue
        before = (harness.PASS, harness.FAIL, harness.SKIP,
                  len(harness.REAL_AI_LAUNCHES))
        seconds = _run_one(by_name[name])
        sys.stderr.flush()
        print(SENTINEL + json.dumps({
            "name": name, "seconds": seconds,
            "pass": harness.PASS - before[0],
            "fail": harness.FAIL - before[1],
            "skip": harness.SKIP - before[2],
            "launches": harness.REAL_AI_LAUNCHES[before[3]:],
        }), flush=True)
    return 0


class _Worker:
    """One `smoke_test.py --worker` child. A reader thread turns its output
    into (worker, test name, lines, result) items on the shared queue;
    result is None when the child died mid-test."""

    def __init__(self, argv, results):
        env = dict(os.environ, **{WORKER_ENV: "1",
                                  "PYTHONIOENCODING": "utf-8",
                                  "PYTHONUNBUFFERED": "1"})
        # the harness pointed TEMP and friends at the parent's sandbox; the
        # worker must build its own from the real profile, or every worker
        # nests its sandbox inside the parent's and the sweep and the
        # sandbox-removal guard both miss it
        env.update({k: v for k, v in harness._REAL_PROFILE.items()
                    if v is not None})
        for k, v in harness._REAL_PROFILE.items():
            if v is None:
                env.pop(k, None)
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(Path(__file__).resolve()),
             "--worker"] + argv,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=env, encoding="utf-8",
            errors="replace", bufsize=1,
            cwd=str(Path(__file__).resolve().parents[1]))
        self.current = None
        self._results = results
        threading.Thread(target=self._read, daemon=True).start()

    def send(self, name):
        self.current = name
        self.proc.stdin.write(name + "\n")
        self.proc.stdin.flush()

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def _read(self):
        lines = []
        for line in self.proc.stdout:
            if line.startswith(SENTINEL):
                result = json.loads(line[len(SENTINEL):])
                self._results.put((self, result["name"], lines, result))
                lines = []
            else:
                lines.append(line)
        self.proc.wait()
        self._results.put((self, self.current, lines, None))


def _run_parallel(selected, total, jobs, argv):
    started = time.perf_counter()
    times = _load_times()
    # slowest first, so the long tests overlap instead of trailing at the
    # end; unknown tests count as slow, since a new test is often a big one
    pending = sorted((t.__name__ for t in selected),
                     key=lambda n: -times.get(n, 60.0))
    results = queue.Queue()
    # pass --quick and -k through so the worker's collect() agrees on names
    workers = [_Worker(argv, results) for _ in range(min(jobs, len(pending)))]
    for w in workers:
        w.send(pending.pop(0))
    measured, busy = {}, len(workers)
    while busy:
        worker, name, lines, result = results.get()
        sys.stdout.write("".join(lines))
        if result is None:
            # the child died (a crash in Qt or a native module). Its test is
            # one FAIL; a fresh worker takes the rest of the queue.
            if worker.current is not None:
                check(f"{worker.current}: worker process died "
                      f"(exit {worker.proc.returncode})", False)
            busy -= 1
            if pending:
                worker = _Worker(argv, results)
                worker.send(pending.pop(0))
                busy += 1
            continue
        harness.PASS += result["pass"]
        harness.FAIL += result["fail"]
        harness.SKIP += result["skip"]
        harness.REAL_AI_LAUNCHES.extend(result["launches"])
        measured[name] = result["seconds"]
        if pending:
            worker.send(pending.pop(0))
        else:
            worker.current = None
            worker.close()   # EOF: the worker exits, its reader posts None
    sys.stdout.flush()
    _save_times(measured)
    return _finish(len(selected), total, time.perf_counter() - started)


def main(argv=()):
    argv = list(argv)
    quick, patterns, jobs, worker = _parse_args(argv)
    harness._guard_real_ai_launches()
    tests = collect()
    selected = [t for t in tests
                if not (quick and t.__name__ in SLOW_TESTS)
                and (not patterns
                     or any(p in t.__name__ for p in patterns))]
    if worker:
        return _worker_loop({t.__name__: t for t in selected})
    if jobs > 1 and len(selected) > 1:
        passthrough = [a for a in argv if a != "--worker"]
        return _run_parallel(selected, len(tests), jobs, passthrough)
    return _run_serial(selected, len(tests))


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception:
        traceback.print_exc()
        print(f"\nRESULT: {harness.PASS} passed, {harness.FAIL + 1} failed "
              f"(crash)", flush=True)
        sys.exit(1)
    finally:
        if not os.environ.get(WORKER_ENV):
            harness._remove_fixture_transcripts()
        harness._remove_sandbox_home()
