"""LaneOps: every mutating lane operation, one at a time per repository.

`git worktree add`, `worktree remove`, `worktree prune` and `branch -d` take
locks in the repo's shared git dir, so two of them at once fail on
`index.lock` or a ref lock. Two dialog submits, a Count of 3, or two
workspaces on one repo would do exactly that. So there is ONE LaneOps, owned
by MainWindow for the app's lifetime, and it keeps one FIFO queue and one
worker thread per repository, keyed by the repo's git common dir (the same
for the main checkout and every worktree of it). Different repos run in
parallel.

Callers `submit(repo, fn, *args, callback=...)`. `fn` runs on the repo's
worker thread and must not touch Qt; the result comes back to the GUI thread
through a queued signal (the `app/usage_poll.py` pattern) and `callback(result,
error)` runs there. `drain()` lets tests wait for every submitted operation
to finish and be delivered.

Read-only lane queries do not queue here (app/lane_service.py polls on its
own threads), but they take the repo's `lock_for` around each lane they read,
and every queued job holds it while it runs. Windows refuses to delete a
folder that is any process's working directory, so a poll's `git status`
running inside a lane at the moment `git worktree remove` deletes it would
leave a half-deleted worktree. A job that never touches a lane folder (the
base fetch) passes `exclusive=False` so a slow network does not stall polls.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QCoreApplication, QObject, Qt, Signal

from . import lanes


@dataclass
class _Job:
    fn: Callable
    args: tuple
    callback: Callable | None
    label: str = ""
    exclusive: bool = True
    result: object = None
    error: BaseException | None = None
    key: str = ""
    started: float = 0.0
    ended: float = 0.0


class LaneOps(QObject):
    """One FIFO + one worker thread per repository."""

    _done = Signal(object)              # worker thread -> GUI thread

    def __init__(self, parent: QObject | None = None,
                 audit: Callable[[str], None] | None = None):
        super().__init__(parent)
        self._audit = audit
        self._queues: dict[str, queue.Queue] = {}
        self._keys: dict[str, str] = {}     # normcased repo path -> queue key
        self._repo_locks: dict[str, threading.Lock] = {}   # queue key -> lock
        self._lock = threading.Lock()
        self._pending = 0                   # submitted, not yet delivered
        self._done.connect(self._deliver, Qt.ConnectionType.QueuedConnection)

    def key_for(self, repo: str) -> str:
        """The queue a repo's operations share: its git common dir, so every
        checkout of one repository lands in the same queue. Read from the
        `.git` entries, never by running git: this runs on the GUI thread.
        Cached."""
        norm = os.path.normcase(os.path.normpath(repo or ""))
        key = self._keys.get(norm)
        if key is None:
            key = (lanes.read_common_dir(repo) if repo else "") or norm
            self._keys[norm] = key
        return key

    def lock_for(self, repo: str) -> threading.Lock:
        """The repo's folder lock: held by every exclusive job while it
        runs, and by the lane poller around each lane it reads."""
        key = self.key_for(repo)
        with self._lock:
            return self._repo_locks.setdefault(key, threading.Lock())

    def submit(self, repo: str, fn: Callable, *args,
               callback: Callable | None = None, label: str = "",
               exclusive: bool = True) -> None:
        job = _Job(fn=fn, args=args, callback=callback, label=label,
                   exclusive=exclusive, key=self.key_for(repo))
        lock = self.lock_for(repo)
        with self._lock:
            self._pending += 1
            q = self._queues.get(job.key)
            if q is None:
                q = self._queues[job.key] = queue.Queue()
                threading.Thread(target=self._run, args=(q, lock), daemon=True,
                                 name="aihive-lanes").start()
        q.put(job)

    def _run(self, q: queue.Queue, lock: threading.Lock) -> None:
        while True:
            job = q.get()
            job.started = time.monotonic()
            try:
                if job.exclusive:
                    with lock:
                        job.result = job.fn(*job.args)
                else:
                    job.result = job.fn(*job.args)
            except BaseException as exc:    # report, never kill the worker
                job.error = exc
            job.ended = time.monotonic()
            try:
                self._done.emit(job)
            except RuntimeError:
                return                      # the owner is gone (app quit)

    def _deliver(self, job: _Job) -> None:
        self._pending -= 1
        if job.callback is None:
            return
        try:
            job.callback(job.result, job.error)
        except Exception as exc:
            if self._audit is not None:
                try:
                    self._audit(f"LANE-FAIL callback {job.label} "
                                f"{type(exc).__name__}: {exc}")
                except Exception:
                    pass

    def busy(self) -> bool:
        return self._pending > 0

    def drain(self, timeout: float = 60.0) -> bool:
        """Pump events until every submitted operation has run and its
        callback has been delivered (callbacks may submit more). For tests;
        the app never blocks on lanes."""
        deadline = time.monotonic() + timeout
        while self._pending and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return not self._pending
