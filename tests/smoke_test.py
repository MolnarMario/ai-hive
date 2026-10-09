"""AI Hive end-to-end smoke test.

Run with the project venv, no display needed:
    .venv\\Scripts\\python.exe tests\\smoke_test.py            (full)
    .venv\\Scripts\\python.exe tests\\smoke_test.py --quick    (no real claude)
    .venv\\Scripts\\python.exe tests\\smoke_test.py -k NAME    (matching tests)
    .venv\\Scripts\\python.exe tests\\smoke_test.py -m lanes   (one module)
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
6 workers cut the quick run from about 6 minutes to about 1. Each worker
imports smoke.harness itself and so gets its own throwaway profile; the
parent hands out one test at a time, slowest first by the durations the last
run saved in tests/.smoke-times.json, and prints each test's output as one
block when it ends. Output order is completion order. A failure that only
shows in parallel is an order or load dependence: rerun the test alone with
-k NAME -j 1.

The parent owns cleanup. It removes each worker's sandbox after the worker
exits (a worker can't: its agents' children keep the sandbox as their cwd
until the worker's Job Objects close at exit, and Windows won't delete a
cwd), kills a worker whose test runs past TEST_TIMEOUT, and shuts every
worker down on Ctrl+C. At start it sweeps test folders that killed runs
left in %TEMP% (see harness.sweep_stale_temp).
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
                   reply_marks, runner, sessions, sidebar, terminal, tooltips,
                   updates, usage, window)
from smoke.harness import check

# Run order with -j 1. e2e last: it launches a real claude.
MODULES = [sidebar, attention, processes, terminal, reply_marks, sessions,
           limits, usage, board, window, updates, lanes, runner, tooltips,
           e2e]

# Skipped by --quick: real, billed, minutes-long. Run the full suite before a
# merge; --quick is for the edit-run loop.
SLOW_TESTS = {"test_lifecycle_e2e"}

# 8 workers on an 8-core machine saved about 7 s of 62 over 6 and push the
# timing-sensitive tests (task delivery waits for Claude's echo) closer to
# their limits.
DEFAULT_JOBS = max(1, min(6, os.cpu_count() or 1))
TIMES_FILE = Path(__file__).resolve().parent / ".smoke-times.json"
# A worker prints this, then JSON, after each test. Nothing else in the suite
# prints it.
SENTINEL = "@@smoke-worker@@ "
# The parent sets this on every worker it starts. A worker leaves the sweep
# of the real ~/.claude/projects to the parent: in a worker it could delete
# the folder of an e2e test still running in another worker.
WORKER_ENV = "AIHIVE_SMOKE_WORKER"
# A parallel test running longer than this is killed with its worker and
# counts as a FAIL. Every wait in a test has its own timeout, so this only
# catches a real hang; the slowest test, the e2e, needs about 2 minutes.
TEST_TIMEOUT = int(os.environ.get("AIHIVE_SMOKE_TEST_TIMEOUT", "600"))
# For test_parallel_runner only: "crash:NAME" or "hang:NAME" makes a worker
# print one FAIL and then die or hang when it reaches test NAME.
FAULT_ENV = "AIHIVE_SMOKE_FAULT"


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
    whose name contains one of the patterns. -m MODULE (repeatable) runs only
    the tests in tests/smoke/MODULE.py; with -k too, a test must match both.
    -j N runs N worker processes, -j 1 runs everything in this process."""
    quick, worker, jobs = False, False, DEFAULT_JOBS
    patterns, modules, it = [], [], iter(argv)
    for arg in it:
        flag, value = arg[:2], arg[2:]
        if arg == "--quick":
            quick = True
        elif arg == "--worker":
            worker = True
        elif flag in ("-k", "-m", "-j"):
            if not value:
                value = next(it, "")
            if flag == "-k":
                patterns.append(value)
            elif flag == "-m":
                modules.append(value)
            else:
                jobs = _jobs(value)
        else:
            raise SystemExit(f"unknown argument: {arg} "
                             f"(use --quick, -k NAME, -m MODULE, -j N)")
    return quick, patterns, modules, jobs, worker


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
    harness.drop_windows()
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
    """--worker: announce this worker's sandbox, then run the test named on
    each stdin line and print the sentinel with its counts. Exits at EOF."""
    fault, _, fault_test = os.environ.get(FAULT_ENV, "").partition(":")
    if fault == "boot":
        os._exit(4)
    print(SENTINEL + json.dumps({"sandbox": str(harness.SANDBOX_HOME)}),
          flush=True)
    for line in sys.stdin:
        name = line.strip()
        if not name:
            continue
        if name == fault_test and fault in ("crash", "hang"):
            check(f"{name}: injected fault ({fault})", False)
            if fault == "crash":
                os._exit(3)
            time.sleep(3600)
        before = (harness.PASS, harness.FAIL, harness.SKIP,
                  len(harness.REAL_AI_LAUNCHES))
        seconds = _run_one(by_name[name])
        if name == fault_test and fault == "glue":
            sys.stdout.write("a partial line with no newline")
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
    into (worker, test name, lines, result) items on the shared queue.
    result is None once the child has exited, after the reader removed its
    sandbox; `current` then names the test it died in, if any."""

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
        self.started = 0.0
        self.timed_out = False
        self.sandbox = None
        self.ran = 0        # results this worker posted
        self._results = results
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def send(self, name):
        """Hand the worker its next test. False when its stdin is gone: the
        worker died after its last result, and its reader reports that."""
        try:
            self.proc.stdin.write(name + "\n")
            self.proc.stdin.flush()
        except OSError:
            return False
        self.current, self.started = name, time.monotonic()
        return True

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def kill(self):
        # the worker's agents sit in Job Objects with KILL_ON_JOB_CLOSE, so
        # killing the worker takes its whole process tree down with it
        try:
            self.proc.kill()
        except OSError:
            pass

    def _read(self):
        # The exit item is posted whatever happens in here: without it the
        # parent waits on this worker forever.
        lines = []
        try:
            for line in self.proc.stdout:
                # stderr shares the pipe, so a partial line written just
                # before the sentinel puts it mid-line
                at = line.find(SENTINEL)
                try:
                    msg = (json.loads(line[at + len(SENTINEL):])
                           if at >= 0 else None)
                except ValueError:
                    msg = None
                if not isinstance(msg, dict):
                    lines.append(line)
                    continue
                if at > 0:
                    lines.append(line[:at] + "\n")
                if "sandbox" in msg:
                    self.sandbox = msg["sandbox"]
                    continue
                self.ran += 1
                self._results.put((self, msg.get("name"), lines, msg))
                lines = []
            self.proc.wait()
        finally:
            self._results.put((self, self.current, lines, None))
            # after the post: a replacement worker need not wait for this
            # deletion, and the parent joins this thread before it exits
            if self.sandbox:
                harness.remove_sandbox(self.sandbox, wait_s=10)


def _count_checks(lines):
    """PASS/FAIL/SKIP lines a dead worker printed before it died, so a crash
    mid-test never hides the failures that came before it."""
    return [sum(ln.startswith(tag) for ln in lines)
            for tag in ("[PASS] ", "[FAIL] ", "[SKIP] ")]


def _run_parallel(selected, total, jobs, argv):
    started = time.perf_counter()
    times = _load_times()
    # slowest first, so the long tests overlap instead of trailing at the
    # end; unknown tests count as slow, since a new test is often a big one
    pending = sorted((t.__name__ for t in selected),
                     key=lambda n: -times.get(n, 60.0))
    results = queue.Queue()
    measured, everyone, live = {}, [], set()
    # workers that died in a row without finishing a test: a broken import
    # or a sandbox that can't be built would otherwise respawn forever
    stillborn = [0]

    def spawn():
        # pass --quick, -k and -m through so the worker selects the same
        # tests and agrees on names
        w = _Worker(argv, results)
        everyone.append(w)
        live.add(w)
        dispatch(w)

    def dispatch(w):
        w.current = None
        if not pending:
            w.close()   # EOF: the worker exits, its reader posts None
            return
        if w.send(pending[0]):
            pending.pop(0)
        # else the pipe is dead: the reader's None spawns a replacement,
        # which takes this test

    try:
        for _ in range(min(jobs, len(pending))):
            spawn()
        while live:
            # every pass, not only on a quiet second: other workers' results
            # can keep the queue busy while one test hangs
            now = time.monotonic()
            for w in live:
                if (w.current and not w.timed_out
                        and now - w.started > TEST_TIMEOUT):
                    w.timed_out = True
                    w.kill()
            try:
                worker, name, lines, result = results.get(timeout=1.0)
            except queue.Empty:
                continue
            sys.stdout.write("".join(lines))
            if result is not None:
                harness.PASS += result["pass"]
                harness.FAIL += result["fail"]
                harness.SKIP += result["skip"]
                harness.REAL_AI_LAUNCHES.extend(result["launches"])
                measured[name] = result["seconds"]
                stillborn[0] = 0
                dispatch(worker)
                continue
            # the child exited: normally at EOF, otherwise mid-test (a hang
            # past TEST_TIMEOUT, or a crash in Qt or a native module). That
            # test is one FAIL; a fresh worker takes the rest of the queue.
            live.discard(worker)
            if worker.current is not None:
                passed, failed, skipped = _count_checks(lines)
                harness.PASS += passed
                harness.FAIL += failed
                harness.SKIP += skipped
                why = (f"ran past {TEST_TIMEOUT}s, worker killed"
                       if worker.timed_out else
                       f"worker process died (exit {worker.proc.returncode})")
                check(f"{worker.current}: {why}", False)
            elif worker.ran == 0 and pending:
                stillborn[0] += 1
                if stillborn[0] >= 3:
                    check(f"suite: 3 workers in a row died before running a "
                          f"test (last exit {worker.proc.returncode}); "
                          f"{len(pending)} tests not run", False)
                    pending.clear()
            if pending:
                spawn()
    finally:
        # Ctrl+C or a crash here: no worker may outlive the run, or its
        # sandbox and its agents' processes stay behind
        for w in everyone:
            w.close()
        deadline = time.monotonic() + 5
        for w in everyone:
            try:
                w.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                w.kill()
        for w in everyone:
            w.reader.join(timeout=15)
    sys.stdout.flush()
    _save_times(measured)
    return _finish(len(selected), total, time.perf_counter() - started)


def main(argv=()):
    argv = list(argv)
    quick, patterns, modules, jobs, worker = _parse_args(argv)
    harness._guard_real_ai_launches()
    tests = collect()
    known = [m.__name__.rsplit(".", 1)[-1] for m in MODULES]
    unknown = [m for m in modules if m not in known]
    if unknown:
        raise SystemExit(f"no test module {', '.join(unknown)} "
                         f"(have: {', '.join(known)})")
    selected = [t for t in tests
                if not (quick and t.__name__ in SLOW_TESTS)
                and (not patterns
                     or any(p in t.__name__ for p in patterns))
                and (not modules
                     or t.__module__.rsplit(".", 1)[-1] in modules)]
    if worker:
        return _worker_loop({t.__name__: t for t in selected})
    if not selected:
        # a typo in -k used to run nothing and report a clean pass
        raise SystemExit("no test matches the -k/-m filters")
    # in the background: the first sweep after a long gap has thousands of
    # folders to delete, and the tests need not wait for it
    threading.Thread(target=harness.sweep_stale_temp, daemon=True).start()
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
