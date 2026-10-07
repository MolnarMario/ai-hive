"""LaneService: what every agent lane holds, and where lanes overlap.

Phase 2 of agent lanes ("Awareness without the token cost"; the lane specs
are in git history under .scratch/agent-lanes/, removed in 0.35.0). Lanes keep agents from sharing one index and one working
tree; this service keeps them aware of each other without every agent
reading the whole board:

* every POLL_MS (and soon after any agent's turn ends) it reads every lane
  of every repository with `lanes.snapshot_repo`: head, ahead/behind,
  committed and uncommitted files, and the overlaps (two lanes changed one
  file; a lane changed a file its base also changed; a real in-memory merge
  says which of those CONFLICT);
* it writes `lanes.json` into each workspace's own `.aihive` folder, which
  the overlap hook (app/session_hook.py, PostToolUse on the edit tools)
  reads to warn an agent the moment it edits a file someone else changed;
* it appends lane notices to `.aihive/notices/<agent uid>.jsonl` for
  overlaps that appeared since, which the UserPromptSubmit hook injects on
  that agent's next prompt (once: both hooks share the seen file);
* it emits `lanesChanged` for the card chips, the Activity panel and the
  board roster, and `overlapFound` (first sightings only) for the event log;
* every FETCH_MS it fetches each repo's base through LaneOps (a fetch writes
  refs), so "the base moved under you" is seen without anyone pulling.

It runs while any agent anywhere has a lane, whatever the lanes toggles say:
MainWindow._sync_lane_service
starts it with the first lane and stops it when the last one goes, so with
no lane AI Hive starts no lane git process at all. Stopping writes
`"enabled": false` into every lanes.json it wrote, which silences
already-running agents' hooks at once. A workspace that loses its last lane
while another workspace keeps one gets no such write: the service keeps
running, stops rewriting that workspace's lanes.json, and its hooks treat
the file as off once it is LANES_STALE_S old (no agent there has a lane to
arm a hook with anyway). On Windows
that write fails while a hook has the file open, so a failed one is retried
every STOP_RETRY_MS. As a backstop, every poll rewrites lanes.json with a
fresh `ts` even when nothing changed, and the hooks treat a lanes.json older
than session_hook.LANES_STALE_S as switched off. Lane state is transient:
nothing here marks the session dirty.

Polls are read-only and lock-free (see the awareness section of
app/lanes.py), so they run on their own daemon thread per repository, one
at a time per repo (an in-flight guard; a poll asked for meanwhile runs
right after), and never through the LaneOps queue, which would make every
lane create wait behind a status read.
"""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QObject, Qt, QTimer, Signal

from . import lanes
from . import session_hook

POLL_MS = 15000
FETCH_MS = 5 * 60 * 1000
FETCH_FIRST_MS = 20000      # the first fetch after the service starts
POKE_MS = 1500              # coalesce a burst of turn-ends into one poll
STOP_RETRY_MS = 250         # retrying a "switched off" write that failed
STOP_RETRIES = 20
INDEX_NAME = "lanes.json"
NOTICES_DIR = "notices"
SEEN_DIR = "seen"


def index_path(ws) -> str:
    return os.path.join(ws.board.dir, INDEX_NAME)


def notices_path(ws, uid: str) -> str:
    return os.path.join(ws.board.dir, NOTICES_DIR, f"{uid}.jsonl")


def seen_path(ws, uid: str) -> str:
    return os.path.join(ws.board.dir, SEEN_DIR, f"{uid}.json")


def _repo_key(repo: str) -> str:
    return os.path.normcase(os.path.normpath(repo or ""))


class LaneService(QObject):
    lanesChanged = Signal(str, object)          # ws_id, {uid: LaneView}
    # first sighting of an overlap level between two lanes (or a lane and
    # its base), for the event log: ws_id, agent uid, {peer, peer_uid ("" =
    # the base branch), level, paths}. One per unordered pair, not per file.
    overlapFound = Signal(str, str, object)
    _polled = Signal(object)                    # worker thread -> GUI thread

    def __init__(self, manager, lane_ops, skip=None, audit=None, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.lane_ops = lane_ops
        # skip(uid) -> True while that agent's lane is being created or
        # repaired: its folder is not there yet, and that is not news
        self._skip = skip or (lambda _uid: False)
        self._audit = audit
        self._running = False
        self._snaps: dict[str, lanes.RepoSnapshot] = {}   # repo key -> last
        self._caches: dict[str, dict] = {}       # repo key -> merge cache
        self._inflight: set[str] = set()
        self._again: set[str] = set()            # asked for while in flight
        self._repos: dict[str, tuple] = {}       # repo key -> (repo, base)
        self._ws_views: dict[str, dict] = {}     # ws_id -> {uid: LaneView}
        self._written: dict[str, str] = {}       # index path -> last content
        self._index_failing: set[str] = set()    # audited once until it works
        # "enabled": false writes that failed at stop(), retried on a timer
        self._disable_pending: set[str] = set()
        self._disable_tries = 0
        self._noticed: set[tuple] = set()        # (uid, key) appended
        self._events: set[tuple] = set()         # (a, b, level) logged
        self._seeded: set[str] = set()           # repo keys past 1st poll
        self._failed: set[str] = set()           # repo keys whose poll failed
        self._polled.connect(self._on_polled, Qt.ConnectionType.QueuedConnection)

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(POLL_MS)
        self._poll_timer.timeout.connect(self.poll)
        self._poke_timer = QTimer(self)
        self._poke_timer.setSingleShot(True)
        self._poke_timer.setInterval(POKE_MS)
        self._poke_timer.timeout.connect(self.poll)
        self._fetch_timer = QTimer(self)
        self._fetch_timer.setInterval(FETCH_MS)
        self._fetch_timer.timeout.connect(self.fetch)
        self._first_fetch = QTimer(self)
        self._first_fetch.setSingleShot(True)
        self._first_fetch.setInterval(FETCH_FIRST_MS)
        self._first_fetch.timeout.connect(self.fetch)
        self._disable_timer = QTimer(self)
        self._disable_timer.setInterval(STOP_RETRY_MS)
        self._disable_timer.timeout.connect(self._retry_disable)

        for ws in manager.workspaces:
            for agent in ws.agents:
                self._wire(agent)
        # a bound slot, not a lambda: Qt drops it when this service goes, so
        # a manager that outlives it never calls into a deleted object
        manager.terminalAdded.connect(self._on_terminal_added)

    # ------------------------------------------------------------ public ---

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        # a "switched off" write still being retried must not land after this
        self._disable_timer.stop()
        self._disable_pending.clear()
        self._seeded.clear()
        self._poll_timer.start()
        self._fetch_timer.start()
        self._first_fetch.start()
        self.poll()

    def stop(self) -> None:
        """The last lane went: no more polls or fetches, every lanes.json
        this run wrote says disabled (running agents' hooks go quiet at
        once), and the chips go back to plain."""
        if not self._running:
            return
        self._running = False
        for t in (self._poll_timer, self._poke_timer, self._fetch_timer,
                  self._first_fetch):
            t.stop()
        self._disable_pending = set(self._written)
        self._disable_tries = 0
        self._written.clear()
        self._retry_disable()
        self._snaps.clear()
        self._again.clear()
        for ws_id in list(self._ws_views):
            self._ws_views[ws_id] = {}
            self.lanesChanged.emit(ws_id, {})

    def _retry_disable(self) -> None:
        """Write "enabled": false into every lanes.json still pending. On
        Windows the replace fails while a hook process has the file open,
        so a failure is retried every STOP_RETRY_MS, STOP_RETRIES times."""
        for path in sorted(self._disable_pending):
            try:
                session_hook.write_lanes_index(
                    path, {"version": session_hook.LANES_INDEX_VERSION,
                           "enabled": False, "ts": time.time()})
                self._disable_pending.discard(path)
            except OSError:
                pass
        if not self._disable_pending:
            self._disable_timer.stop()
            return
        self._disable_tries += 1
        if self._disable_tries > STOP_RETRIES:
            self._disable_timer.stop()
            if self._audit is not None:
                for path in sorted(self._disable_pending):
                    self._audit(f"LANE-FAIL disable {path}: still locked "
                                f"after {STOP_RETRIES} tries; its hooks go "
                                f"quiet once it is "
                                f"{int(session_hook.LANES_STALE_S)} s old")
            self._disable_pending.clear()
            return
        if not self._disable_timer.isActive():
            self._disable_timer.start()

    def shutdown(self) -> None:
        """The app is closing: stop polling, write nothing (the agents die
        with the app, so their hooks need no silencing)."""
        self._running = False
        for t in (self._poll_timer, self._poke_timer, self._fetch_timer,
                  self._first_fetch, self._disable_timer):
            t.stop()

    def poke(self) -> None:
        """Something probably changed (an agent finished a turn): poll soon."""
        if self._running:
            self._poke_timer.start()

    def views(self, ws_id: str) -> dict:
        return dict(self._ws_views.get(ws_id, {}))

    def view(self, uid: str):
        for views in self._ws_views.values():
            if uid in views:
                return views[uid]
        return None

    def snapshot(self, repo: str):
        return self._snaps.get(_repo_key(repo))

    def busy(self) -> bool:
        return bool(self._inflight)

    def drain(self, timeout: float = 30.0) -> bool:
        """Tests: wait until every poll in flight has been delivered."""
        from PySide6.QtCore import QCoreApplication
        deadline = time.monotonic() + timeout
        while self._inflight and time.monotonic() < deadline:
            QCoreApplication.processEvents()
            time.sleep(0.005)
        QCoreApplication.processEvents()
        return not self._inflight

    # -------------------------------------------------------------- poll ---

    def _wire(self, agent) -> None:
        agent.reply_finished.connect(self.poke)

    def _on_terminal_added(self, _ws_id: str, agent) -> None:
        self._wire(agent)

    def _collect(self) -> dict:
        """repo key -> (repo, [entry]) for every laned agent, on the GUI
        thread (the model is not thread-safe; git runs on the worker)."""
        groups: dict[str, tuple] = {}
        for ws in self.manager.workspaces:
            for agent in ws.agents:
                lane = agent.spec.lane
                if not lane or self._skip(agent.spec.uid):
                    continue
                key = _repo_key(lane["repo"])
                role = (lanes.INTEGRATOR_ROLE
                        if self.manager.is_integrator(agent) else "")
                groups.setdefault(key, (lane["repo"], []))[1].append(
                    {"uid": agent.spec.uid, "agent": agent.spec.name,
                     "ws_id": ws.id, "lane": dict(lane), "role": role})
        return groups

    def poll(self) -> None:
        if not self._running:
            return
        groups = self._collect()
        for key in [k for k in self._snaps if k not in groups]:
            # the repo's last laned agent is gone
            self._snaps.pop(key, None)
            self._caches.pop(key, None)
            self._repos.pop(key, None)
        self._publish_views()
        for key, (repo, entries) in groups.items():
            self._repos[key] = (repo, entries[0]["lane"].get("base", ""))
            if key in self._inflight:
                self._again.add(key)
                continue
            self._inflight.add(key)
            cache = self._caches.setdefault(key, {})
            lock = self.lane_ops.lock_for(repo)
            threading.Thread(target=self._run,
                             args=(key, repo, entries, cache, lock),
                             daemon=True, name="aihive-lane-poll").start()

    def _run(self, key, repo, entries, cache, lock) -> None:
        try:
            result = lanes.snapshot_repo(repo, entries, cache, lock=lock)
        except Exception as exc:        # report, never kill the app
            result = exc
        try:
            self._polled.emit((key, result))
        except RuntimeError:
            pass                        # the service is gone (app quit)

    def _on_polled(self, item) -> None:
        key, result = item
        self._inflight.discard(key)
        if not self._running:
            return
        if isinstance(result, BaseException):
            if key not in self._failed and self._audit is not None:
                self._audit(f"LANE-FAIL poll repo={key}: "
                            f"{type(result).__name__}: {result}")
            self._failed.add(key)
        else:
            self._failed.discard(key)
            # the agents may have gone while git ran
            live = self._collect().get(key)
            if live is not None:
                uids = {e["uid"] for e in live[1]}
                result.lanes = [s for s in result.lanes if s.uid in uids]
                result.overlaps = {
                    u: [o for o in ovs if not o.peer_uid or o.peer_uid in uids]
                    for u, ovs in result.overlaps.items() if u in uids}
                self._snaps[key] = result
                try:
                    self._after_snapshot(key, result)
                except Exception as exc:    # never stop the next poll
                    if self._audit is not None:
                        self._audit(f"LANE-FAIL publish repo={key}: "
                                    f"{type(exc).__name__}: {exc}")
        if key in self._again:
            self._again.discard(key)
            self.poll()

    # ------------------------------------------------------------ output ---

    def _publish_views(self) -> None:
        """Rebuild every workspace's {uid: LaneView} from the snapshots and
        emit lanesChanged where it changed."""
        by_ws: dict[str, dict] = {ws.id: {} for ws in self.manager.workspaces}
        for snap in self._snaps.values():
            for s in snap.lanes:
                if s.ws_id in by_ws:
                    by_ws[s.ws_id][s.uid] = snap.view(s.uid)
        for ws_id, views in by_ws.items():
            if views != self._ws_views.get(ws_id):
                self._ws_views[ws_id] = views
                self.lanesChanged.emit(ws_id, views)
        for gone in [w for w in self._ws_views if w not in by_ws]:
            self._ws_views.pop(gone, None)

    def _after_snapshot(self, key: str, snap) -> None:
        first = key not in self._seeded
        self._seeded.add(key)
        self._write_indexes(snap)
        self._write_notices(snap, first)
        self._log_events(snap, first)
        self._publish_views()

    def _workspaces_of(self, snap) -> list:
        ids = {s.ws_id for s in snap.lanes}
        return [ws for ws in self.manager.workspaces
                if ws.id in ids and ws.board is not None]

    def _write_indexes(self, snap) -> None:
        """lanes.json in each workspace on this repo: the whole repo's lanes
        (two workspaces on one repo do see each other's lanes)."""
        import json
        lanes_part = {}
        for s in snap.lanes:
            lanes_part[s.uid] = {
                "agent": s.agent, "branch": s.branch, "root": s.root,
                "base_ref": s.base_ref, "ahead": s.ahead, "behind": s.behind,
                "dirty": len(s.dirty), "exists": s.exists,
                "fork": s.fork, "base_changed": s.base_changed[:500],
                # the Stop hook's nudge to commit skips the integrator
                # (session_hook.lane_stop_decision)
                "role": s.role}
        data = {"version": session_hook.LANES_INDEX_VERSION, "enabled": True,
                "repo": snap.repo, "merge_tree": snap.merge_tree,
                "lanes": lanes_part, "files": snap.index()}
        body = json.dumps(data, sort_keys=True)
        for ws in self._workspaces_of(snap):
            path = index_path(ws)
            # written on every poll, changed or not: the fresh `ts` is what
            # keeps the hooks listening (session_hook.lanes_index_live)
            try:
                ws.board.ensure()
                session_hook.write_lanes_index(path, {**data,
                                                      "ts": time.time()})
                self._written[path] = body
                self._index_failing.discard(path)
            except OSError as exc:
                # a hook reading the file at that moment: the next poll
                # writes it again, so only a lasting failure is worth a line
                if path not in self._index_failing and self._audit is not None:
                    self._audit(f"LANE-FAIL index {path}: {exc}")
                self._index_failing.add(path)
                self._written.setdefault(path, "")

    def _write_notices(self, snap, first: bool) -> None:
        """One notice per new (agent, file, peer, level) overlap. On the first
        poll after a start, what the agent was already told (its seen file)
        is skipped, so a restart does not repeat old news."""
        ws_by_id = {ws.id: ws for ws in self._workspaces_of(snap)}
        for s in snap.lanes:
            ws = ws_by_id.get(s.ws_id)
            if ws is None:
                continue
            if first:
                known = session_hook.read_seen(seen_path(ws, s.uid))["keys"]
                self._noticed.update((s.uid, k) for k in known)
            for o in snap.overlaps.get(s.uid, []):
                if (s.uid, o.key) in self._noticed:
                    continue
                self._noticed.add((s.uid, o.key))
                try:
                    session_hook.append_notice(notices_path(ws, s.uid), o.key,
                                               lanes.describe_overlap(o))
                except OSError:
                    pass

    def _log_events(self, snap, first: bool) -> None:
        """overlapFound once per new (pair, level), with every file of that
        pair in this poll. The first poll after a start only seeds: those
        overlaps were news in the run that first saw them."""
        groups: dict[tuple, list] = {}
        names = {s.uid: s for s in snap.lanes}
        for s in snap.lanes:
            for o in snap.overlaps.get(s.uid, []):
                if o.peer_uid:
                    if s.uid > o.peer_uid:
                        continue        # the pair's other side reports it
                    pair = (s.uid, o.peer_uid, o.level)
                else:
                    pair = (s.uid, "", o.level)
                groups.setdefault(pair, []).append(o)
        for pair, ovs in groups.items():
            if pair in self._events:
                continue
            self._events.add(pair)
            if first:
                continue
            uid, peer_uid, level = pair
            me = names.get(uid)
            if me is None:
                continue
            self.overlapFound.emit(me.ws_id, uid, {
                "peer": ovs[0].peer, "peer_uid": peer_uid, "level": level,
                "paths": sorted({o.path for o in ovs})})

    # ------------------------------------------------------------- fetch ---

    def fetch(self) -> None:
        """Fetch each laned repo's base (through LaneOps: it writes refs),
        then poll, so a base that moved shows up as notices."""
        if not self._running:
            return
        for repo, base in list(self._repos.values()):
            # queued (a fetch writes refs) but not exclusive: it never
            # touches a lane folder, and a slow network must not stall polls
            self.lane_ops.submit(repo, lanes.fetch_base, repo, base,
                                 label="fetch", exclusive=False,
                                 callback=lambda _ok, _err: self.poke())
