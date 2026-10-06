"""The suite runner itself: the parallel workers report the same checks as
a serial run, and bad arguments are refused."""

import os
import re
import subprocess
import sys

from .harness import ROOT, check

# three tests that take well under a second, from three modules
_FAST = ["test_ansi", "test_fsopen_helpers", "test_filetypes_icons"]


def _run(*args):
    env = dict(os.environ)
    env.pop("AIHIVE_SMOKE_WORKER", None)
    argv = [sys.executable, str(ROOT / "tests" / "smoke_test.py")]
    for name in _FAST:
        argv += ["-k", name]
    proc = subprocess.run(argv + list(args), capture_output=True,
                          encoding="utf-8", errors="replace", env=env,
                          cwd=str(ROOT), timeout=180)
    result = re.search(r"RESULT: (\d+) passed, (\d+) failed, (\d+) skipped "
                       r"\((\d+) of", proc.stdout)
    return proc, (tuple(int(g) for g in result.groups()) if result else None)


def test_parallel_runner():
    serial, serial_counts = _run("-j", "1")
    parallel, parallel_counts = _run("-j", "3")
    check("runner: a serial run of three tests passes",
          serial.returncode == 0 and serial_counts
          and serial_counts[3] == 3, serial.stdout[-400:])
    check("runner: three workers report the same counts as one process",
          parallel.returncode == 0 and parallel_counts == serial_counts,
          (serial_counts, parallel_counts, parallel.stdout[-400:]))
    # each test's output arrives as one block, never interleaved with
    # another worker's: every check line sits next to its test's others
    prefixes = [ln.split(":")[0] for ln in parallel.stdout.splitlines()
                if ln.startswith("[PASS] ")]
    runs = [p for i, p in enumerate(prefixes) if i == 0
            or p != prefixes[i - 1]]
    check("runner: worker output is not interleaved between tests",
          len(runs) == len(set(runs)), runs)
    bad, _ = _run("-j", "0")
    check("runner: -j 0 is refused", bad.returncode != 0
          and "-j needs a positive number" in bad.stdout + bad.stderr,
          bad.stdout[-200:] + bad.stderr[-200:])
