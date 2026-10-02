"""Agent lanes: a private git worktree per agent
(.scratch/agent-lanes/spec-v2.md).

Lane tests build real temp git repos in the sandbox profile and never touch
the real repo. Every agent is a stubbed worker (`_stub_starts`), per the
real-AI-launch guard: a laned Claude agent is started by the lane callback,
and that start must record, not launch.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from .harness import check

_NO_WINDOW = 0x08000000


def _git(cwd, *args) -> str:
    r = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t",
                        *args], cwd=str(cwd), capture_output=True, text=True,
                       creationflags=_NO_WINDOW)
    if r.returncode != 0:
        raise RuntimeError(f"git {args}: {r.stderr.strip()}")
    return r.stdout.strip()


def _make_repo(tmp: Path, name: str = "proj", remote: bool = True) -> Path:
    """A repo with an ignored `.venv` (holding a marker file), an ignored
    `build/`, a `sub/` folder and, with `remote`, an origin whose HEAD is
    main, the way a real clone looks."""
    seed = tmp / f"{name}-seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / ".gitignore").write_text(".venv/\nnode_modules/\nbuild/\n")
    (seed / "a.txt").write_text("a\n")
    (seed / "sub").mkdir()
    (seed / "sub" / "b.txt").write_text("b\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    if remote:
        origin = tmp / f"{name}-origin.git"
        _git(tmp, "clone", "-q", "--bare", str(seed), str(origin))
        work = tmp / name
        _git(tmp, "clone", "-q", str(origin), str(work))
    else:
        work = tmp / name
        seed.rename(work)
    (work / ".venv").mkdir()
    (work / ".venv" / "marker.txt").write_text("real venv")
    return work


def _drop_lane_folder(root) -> None:
    """Delete a lane folder the way a user might, but junction-safe."""
    from app import lanes
    lanes.unlink_junctions(str(root))
    shutil.rmtree(root)


class _stub_starts:
    """Replace PtyWorker.start with a recorder of (name, cwd, args)."""

    def __enter__(self):
        from app import pty_worker
        self.calls = []
        self._orig = pty_worker.PtyWorker.start
        calls = self.calls

        def start(worker):
            calls.append((worker.spec.name, worker.spec.cwd,
                          list(worker.spec.effective_args())))
        pty_worker.PtyWorker.start = start
        return self

    def __exit__(self, *exc):
        from app import pty_worker
        pty_worker.PtyWorker.start = self._orig
        return False

    def cwds(self, name):
        return [c[1] for c in self.calls if c[0] == name]


class _record_git:
    """Wrap lanes.RUNNER so a test can see every git command lane code ran."""

    def __enter__(self):
        from app import lanes
        self.calls = []
        self._orig = lanes.RUNNER
        orig, calls = self._orig, self.calls

        def runner(args, cwd, timeout):
            calls.append(list(args))
            return orig(args, cwd, timeout)
        lanes.RUNNER = runner
        return self

    def __exit__(self, *exc):
        from app import lanes
        lanes.RUNNER = self._orig
        return False


def _write_transcript(cwd: str, sid: str, branch: str, text: str) -> None:
    """A minimal Claude transcript that started in `cwd`, in the sandbox
    profile's ~/.claude/projects."""
    from app import transcripts
    path = Path(transcripts.transcript_path(cwd, sid))
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"type": "user", "cwd": cwd, "gitBranch": branch, "sessionId": sid,
           "message": {"role": "user", "content": text}}
    path.write_text(json.dumps(rec) + "\n", encoding="utf-8")


def _log(store) -> str:
    try:
        return store.path.with_suffix(".log").read_text(encoding="utf-8")
    except OSError:
        return ""


def test_agent_lanes_switch():
    """The Options master switch for agent lanes.

    OFF by default, because armed it runs `git worktree add` in the user's
    repo for every new agent. It is an ordinary UI preference (an additive
    key under "ui", no SESSION_VERSION bump), and `MainWindow.lanes_enabled()`
    is the only gate lane code may ask, so the switch means one thing
    everywhere. The real close/reopen at the end is the check that matters:
    a default assigned after `_restore_ui_state` would silently switch it back
    off at every launch (the taskbar badge once had exactly that bug).
    """
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from main import create_main_window

    app = QApplication.instance() or QApplication([])

    # --- the switch on the bar -------------------------------------------
    bar = TopBar()
    check("lanes switch: defaults to OFF",
          not bar._agent_lanes and not bar.agent_lanes_btn.isChecked())
    check("lanes switch: it lives in the Options panel, not on the bar",
          bar.agent_lanes_btn.parent() is bar.options_panel
          and bar.agent_lanes_label.parent() is bar.options_panel)
    check("lanes switch: the label names the feature",
          "Agent lanes" in bar.agent_lanes_label.text(),
          bar.agent_lanes_label.text())
    check("lanes switch: OFF tooltip promises existing lanes are kept",
          "OFF" in bar.agent_lanes_btn.toolTip()
          and "keep it" in bar.agent_lanes_btn.toolTip(),
          bar.agent_lanes_btn.toolTip())
    emitted = []
    bar.agentLanesToggled.connect(emitted.append)
    bar.agent_lanes_btn.click()
    check("lanes switch: a click turns it on and emits True",
          emitted == [True] and bar._agent_lanes
          and bar.agent_lanes_btn.isChecked(), emitted)
    check("lanes switch: the tooltip follows the state",
          "ON" in bar.agent_lanes_btn.toolTip(), bar.agent_lanes_btn.toolTip())
    bar.agent_lanes_btn.click()
    check("lanes switch: a second click turns it off and emits False",
          emitted == [True, False] and not bar._agent_lanes, emitted)
    bar.set_agent_lanes(True)   # restore path: reflect without re-emitting
    check("lanes switch: set_agent_lanes reflects without emitting",
          bar._agent_lanes and bar.agent_lanes_btn.isChecked()
          and emitted == [True, False], emitted)
    bar.deleteLater()

    # --- the window: gate, save, restore ----------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-lanes-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    app.processEvents()
    check("lanes switch: a fresh install has lanes disabled",
          win.lanes_enabled() is False and not win.top_bar._agent_lanes)
    check("lanes switch: the saved payload records OFF",
          win._session_payload()["ui"]["agent_lanes"] is False)

    win._save_timer.stop()
    win.top_bar.agent_lanes_btn.click()   # the real signal path, end to end
    check("lanes switch: clicking it enables the gate at once (no restart)",
          win.lanes_enabled() is True)
    check("lanes switch: flipping it marks the session dirty",
          win._save_timer.isActive())
    check("lanes switch: the preference is persisted under ui",
          win._session_payload()["ui"]["agent_lanes"] is True,
          win._session_payload()["ui"])

    win._restore_ui_state({"ui": {"agent_lanes": False}})
    check("lanes switch: restore reflects OFF onto the window and the switch",
          not win.lanes_enabled() and not win.top_bar.agent_lanes_btn.isChecked())
    win._restore_ui_state({"ui": {}})
    check("lanes switch: a session that predates the switch loads it OFF",
          not win.lanes_enabled())

    # ON must survive a real close and reopen
    win._agent_lanes = True
    win.top_bar.set_agent_lanes(True)
    win._save_session()
    win._save_timer.stop()
    win.close()
    app.processEvents()
    again = create_main_window(SessionStore(path=tmp / "session.json"))
    app.processEvents()
    check("lanes switch: ON survives a close and reopen",
          again.lanes_enabled() and again.top_bar.agent_lanes_btn.isChecked())
    again._save_timer.stop()
    again.close()
    app.processEvents()


def test_lanes_core():
    """app/lanes.py on real temp repos: identity, creation, collisions,
    junctions, status, safe removal, repair at the same path."""
    from app import lanes

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-lanes-core-"))
    work = _make_repo(tmp)
    local = _make_repo(tmp, "solo", remote=False)
    origin_sha = _git(work, "rev-parse", "origin/main")

    # --- discovery ---------------------------------------------------------
    check("lanes: the repo root is found from a subfolder, without git",
          lanes.same_path(lanes.find_repo_root(str(work / "sub")), str(work)))
    plain = tmp / "plain"
    plain.mkdir()
    check("lanes: a folder outside any repo has no root",
          lanes.find_repo_root(str(plain)) == "")
    check("lanes: the base is what origin/HEAD names",
          lanes.default_base(str(work)) == "main")
    check("lanes: a lane starts from origin/<base> when the remote has it",
          lanes.start_ref(str(work), "main") == "origin/main")
    check("lanes: ...and from the local base in a repo with no remote",
          lanes.default_base(str(local)) == "main"
          and lanes.start_ref(str(local), "main") == "main")

    # --- identity ----------------------------------------------------------
    plan = lanes.plan_lane(str(work), str(work), "Agent 5", "3fa9c1" + "0" * 26)
    check("lanes: the folder is <parent>\\<repo>.lanes\\<slug>-<uid6>",
          lanes.same_path(plan.root,
                          str(tmp / "proj.lanes" / "agent-5-3fa9c1")),
          plan.root)
    check("lanes: the branch is hive/<slug>-<uid6>",
          plan.branch == "hive/agent-5-3fa9c1", plan.branch)
    check("lanes: a workspace at the repo root starts the agent at the lane "
          "root", plan.cwd == plan.root)
    sub_plan = lanes.plan_lane(str(work), str(work / "sub"), "Agent 5",
                               "3fa9c1" + "0" * 26)
    check("lanes: a subfolder workspace starts the agent in the same "
          "subfolder of its lane",
          lanes.same_path(sub_plan.cwd, os.path.join(plan.root, "sub")),
          sub_plan.cwd)
    twin = lanes.plan_lane(str(work), str(work), "Agent 5", "77aa00" + "0" * 26)
    check("lanes: two agents with one name get different folders and branches",
          twin.root != plan.root and twin.branch != plan.branch)
    check("lanes: every planned path is absolute",
          all(os.path.isabs(p) for p in (plan.root, plan.cwd, plan.repo)))

    # --- create ------------------------------------------------------------
    lane = lanes.create_lane(plan.lane_dict())
    root = Path(lane["root"])
    check("lanes: create makes the worktree on its own branch",
          root.is_dir() and _git(root, "rev-parse", "--abbrev-ref", "HEAD")
          == plan.branch)
    check("lanes: the new branch starts at origin/main",
          _git(root, "rev-parse", "HEAD") == origin_sha)
    check("lanes: create records the base", lane["base"] == "main", lane)
    check("lanes: the ignored .venv is linked in as a junction",
          lanes.is_junction(str(root / ".venv"))
          and (root / ".venv" / "marker.txt").read_text() == "real venv")
    check("lanes: a folder the repo doesn't have is not linked",
          not os.path.lexists(root / "node_modules"))
    exclude = Path(_git(work, "rev-parse", "--path-format=absolute",
                        "--git-common-dir")) / "info" / "exclude"
    check("lanes: .aihive/ is added to the shared info/exclude",
          ".aihive/" in exclude.read_text().splitlines())
    lanes.ensure_exclude(str(work))
    check("lanes: ...once",
          exclude.read_text().splitlines().count(".aihive/") == 1)
    check("lanes: the lane's status is clean",
          _git(root, "status", "--porcelain") == "")

    # --- collisions fail and never reuse -----------------------------------
    _git(work, "branch", "hive/taken-111111", "main")
    branches_before = _git(work, "branch", "--list", "hive/*")
    try:
        lanes.create_lane(plan.lane_dict())
        clash = None
    except lanes.LaneError as exc:
        clash = exc
    check("lanes: an existing folder fails with code 'exists'",
          clash is not None and clash.code == "exists", clash)
    taken = lanes.LanePlan(root=str(tmp / "proj.lanes" / "taken-111111"),
                           branch="hive/taken-111111", repo=str(work),
                           cwd=str(tmp / "proj.lanes" / "taken-111111"))
    try:
        lanes.create_lane(taken.lane_dict())
        clash = None
    except lanes.LaneError as exc:
        clash = exc
    check("lanes: an existing branch fails with code 'exists'",
          clash is not None and clash.code == "exists", clash)
    check("lanes: ...and creates no folder",
          not os.path.lexists(taken.root))
    check("lanes: failed creates leave the branches as they were",
          _git(work, "branch", "--list", "hive/*") == branches_before)

    # --- status ------------------------------------------------------------
    st = lanes.lane_status(lane)
    check("lanes: a fresh lane is clean, 0 ahead and already in the base",
          st.exists and not st.dirty and st.ahead == 0 and st.merged, st)
    (root / "a.txt").write_text("changed\n")
    st = lanes.lane_status(lane)
    check("lanes: an edit shows as dirty", len(st.dirty) == 1, st.dirty)
    try:
        lanes.remove_lane(lane)
        refused = None
    except lanes.LaneError as exc:
        refused = exc
    check("lanes: remove refuses a dirty lane",
          refused is not None and refused.code == "dirty" and root.is_dir(),
          refused)
    _git(root, "commit", "-q", "-am", "work")
    st = lanes.lane_status(lane)
    check("lanes: a commit shows as 1 ahead and not merged",
          st.ahead == 1 and not st.merged and not st.dirty, st)
    check("lanes: the close prompt wording",
          st.describe() == "1 unmerged commit", st.describe())
    try:
        lanes.remove_lane(lane)
        refused = None
    except lanes.LaneError as exc:
        refused = exc
    check("lanes: remove refuses an unmerged lane",
          refused is not None and refused.code == "unmerged" and root.is_dir(),
          refused)
    res = lanes.retire_lane(lane)
    check("lanes: retiring an unmerged lane keeps it",
          not res.removed and res.status.ahead == 1 and root.is_dir(), res)

    # --- removal never follows a link ---------------------------------------
    clean = lanes.create_lane(twin.lane_dict())
    croot = Path(clean["root"])
    precious = tmp / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    (croot / "build").mkdir()
    lanes._make_junction(str(precious), str(croot / "build" / "cache"))
    try:
        lanes.remove_lane(clean)
        refused = None
    except lanes.LaneError as exc:
        refused = exc
    check("lanes: remove refuses while an unknown directory link is left",
          refused is not None and refused.code == "links", refused)
    check("lanes: ...and that link's target is intact",
          (precious / "keep.txt").read_text() == "keep")
    os.rmdir(croot / "build" / "cache")
    result = lanes.remove_lane(clean)
    check("lanes: a clean, merged lane is removed (worktree and branch)",
          not croot.exists() and result.branch_deleted
          and twin.branch not in _git(work, "branch", "--list", "hive/*"),
          result)
    check("lanes: removing a lane leaves the real .venv intact (git follows "
          "junctions)",
          (work / ".venv" / "marker.txt").read_text() == "real venv")

    # --- repair at the same path -------------------------------------------
    head = _git(root, "rev-parse", "HEAD")
    _drop_lane_folder(root)
    fixed, recreated = lanes.repair_lane(lane)
    check("lanes: repair re-adds a deleted lane folder at the same path",
          root.is_dir() and lanes.same_path(fixed["root"], str(root))
          and not recreated)
    check("lanes: ...from its recorded branch, commits intact",
          _git(root, "rev-parse", "HEAD") == head)
    check("lanes: ...with its junction back",
          lanes.is_junction(str(root / ".venv")))
    again, recreated = lanes.repair_lane(lane)
    check("lanes: repairing an intact lane changes nothing",
          not recreated and _git(root, "rev-parse", "HEAD") == head)

    gone = lanes.create_lane(lanes.plan_lane(str(work), str(work), "Gone",
                                             "9b9b9b" + "0" * 26).lane_dict())
    lanes.remove_lane(gone)
    back, recreated = lanes.repair_lane(gone)
    check("lanes: repair recreates a deleted (merged) branch from the base",
          recreated and os.path.isdir(gone["root"])
          and _git(gone["root"], "rev-parse", "HEAD") == origin_sha)
    os.makedirs(os.path.join(tmp, "proj.lanes", "squatter-000000"))
    Path(tmp, "proj.lanes", "squatter-000000", "f.txt").write_text("x")
    try:
        lanes.repair_lane({"root": str(tmp / "proj.lanes" / "squatter-000000"),
                           "branch": "hive/squatter-000000", "base": "",
                           "repo": str(work)})
        refused = None
    except lanes.LaneError as exc:
        refused = exc
    check("lanes: repair never takes over a folder that is not its worktree",
          refused is not None and refused.code == "exists"
          and Path(tmp, "proj.lanes", "squatter-000000", "f.txt").exists(),
          refused)

    # --- records -----------------------------------------------------------
    check("lanes: a malformed lane record is no lane",
          lanes.clean_lane({"root": "x"}) == {} and lanes.clean_lane("x") == {}
          and lanes.clean_lane(None) == {})
    check("lanes: a good record survives clean_lane",
          lanes.clean_lane(lane) == lane)
    shutil.rmtree(tmp, ignore_errors=True)


def test_lane_ops_serial():
    """LaneOps runs one operation at a time per repository (keyed by the git
    common dir, so a subfolder workspace shares its repo's queue) and repos
    in parallel. Callbacks arrive on the GUI thread, in submit order."""
    from PySide6.QtWidgets import QApplication
    from app.lane_ops import LaneOps

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-laneops-"))
    repo_a = _make_repo(tmp, "a", remote=False)
    repo_b = _make_repo(tmp, "b", remote=False)
    ops = LaneOps()
    check("laneops: a subfolder of a repo shares the repo's queue",
          ops.key_for(str(repo_a / "sub")) == ops.key_for(str(repo_a)))
    check("laneops: two repos get two queues",
          ops.key_for(str(repo_a)) != ops.key_for(str(repo_b)))

    spans = []
    lock = threading.Lock()

    def op(tag):
        t0 = time.monotonic()
        time.sleep(0.06)
        with lock:
            spans.append((tag, t0, time.monotonic()))
        return tag

    delivered, threads = [], []

    def done(res, err):
        delivered.append(res)
        threads.append(threading.current_thread() is threading.main_thread())

    # "workspace 1" at the repo root, "workspace 2" in its subfolder
    for i in range(3):
        ops.submit(str(repo_a), op, f"A{i}", callback=done)
        ops.submit(str(repo_a / "sub"), op, f"A{i}s", callback=done)
    for i in range(2):
        ops.submit(str(repo_b), op, f"B{i}", callback=done)
    check("laneops: everything submitted is delivered", ops.drain(30))
    a = sorted((s for s in spans if s[0].startswith("A")), key=lambda s: s[1])
    b = [s for s in spans if s[0].startswith("B")]
    check("laneops: operations on one repo never overlap",
          all(a[i][2] <= a[i + 1][1] for i in range(len(a) - 1)),
          [(s[0], round(s[1], 3), round(s[2], 3)) for s in a])
    check("laneops: two repos run in parallel",
          any(x[1] < y[2] and y[1] < x[2] for x in a for y in b))
    check("laneops: one repo's results come back in submit order",
          [d for d in delivered if d.startswith("A")]
          == ["A0", "A0s", "A1", "A1s", "A2", "A2s"], delivered)
    check("laneops: callbacks run on the GUI thread", all(threads))

    def boom():
        raise RuntimeError("nope")
    errors = []
    ops.submit(str(repo_a), boom, callback=lambda r, e: errors.append(e))
    ops.submit(str(repo_a), op, "after", callback=done)
    ops.drain(10)
    check("laneops: a failing operation reports its error and the queue "
          "goes on", isinstance(errors[0], RuntimeError)
          and delivered[-1] == "after")
    ops.deleteLater()
    app.processEvents()
    shutil.rmtree(tmp, ignore_errors=True)


def test_lanes_persistence():
    """The three-part persisted-field rule for AgentSpec.lane: it saves,
    load_session_dict restores it (repairing a missing folder in place, never
    falling back to the workspace folder), SESSION_VERSION is 5 and a v4 file
    loads with no lanes. The degraded save record keeps it."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.workspace_manager import SESSION_VERSION, WorkspaceManager

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-lanes-persist-"))
    proj = tmp / "proj"
    lane_root = tmp / "proj.lanes" / "agent-1-abcdef"
    (proj).mkdir()
    lane_root.mkdir(parents=True)
    lane = {"root": str(lane_root), "branch": "hive/agent-1-abcdef",
            "base": "main", "repo": str(proj)}

    spec = build_spec(AgentKind.CLAUDE, "Agent 1", cwd=str(lane_root))
    spec.lane = dict(lane)
    d = spec.to_dict()
    check("persist: AgentSpec.to_dict carries the lane", d["lane"] == lane)
    check("persist: from_dict restores it", AgentSpec.from_dict(d).lane == lane)
    check("persist: an agent without a lane saves an empty record",
          build_spec(AgentKind.CLAUDE, "x").to_dict()["lane"] == {})
    check("persist: a malformed record loads as no lane",
          AgentSpec.from_dict({**d, "lane": {"root": 5}}).lane == {})

    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Repo", str(proj))
    agent = mgr.add_terminal(ws.id, spec, autostart=False)
    data = mgr.to_session_dict()
    check("persist: SESSION_VERSION is 5 and the file says so",
          SESSION_VERSION == 5 and data["version"] == 5)
    saved = data["workspaces"][0]["terminals"][-1]
    check("persist: the session file carries the lane", saved["lane"] == lane)

    # degraded record: a kind that is a plain str makes to_dict raise
    agent.spec.kind = "claude"
    audits = []
    mgr.audit = audits.append
    degraded = mgr.to_session_dict()["workspaces"][0]["terminals"][-1]
    check("persist: the degraded save record keeps the lane",
          degraded.get("lane") == lane and any("SAVE-DEGRADE" in a
                                               for a in audits), degraded)
    agent.spec.kind = AgentKind.CLAUDE

    again = WorkspaceManager()
    again.load_session_dict(data)
    a2 = again.workspaces[0].agents[-1]
    check("persist: the lane round-trips a load", a2.spec.lane == lane
          and lanes_same(a2.spec.cwd, str(lane_root)))
    check("persist: an intact lane needs no repair",
          again.take_lane_repairs() == [] and not a2.start_hold())

    # the lane folder is gone: repair, never the project_path fallback
    shutil.rmtree(lane_root)
    third = WorkspaceManager()
    third.load_session_dict(data)
    a3 = third.workspaces[0].agents[-1]
    check("persist: a missing lane folder keeps the lane path as cwd",
          lanes_same(a3.spec.cwd, str(lane_root)), a3.spec.cwd)
    repairs = third.take_lane_repairs()
    check("persist: ...and queues a repair for that agent",
          repairs == [(third.workspaces[0].id, a3.id)], repairs)
    check("persist: ...taken once", third.take_lane_repairs() == [])
    check("persist: ...and the agent holds its start until then",
          bool(a3.start_hold()))
    started = []
    a3.worker.start = lambda: started.append(a3.spec.cwd)
    a3.start()
    a3.restart()
    check("persist: a start (or restart) while held launches nothing",
          started == [])
    a3.release_start()
    check("persist: releasing runs the start that was asked for",
          started == [a3.spec.cwd])

    old = json.loads(json.dumps(data))
    old["version"] = 4
    fourth = WorkspaceManager()
    fourth.load_session_dict(old)
    a4 = fourth.workspaces[0].agents[-1]
    check("persist: a v4 session loads with no lanes",
          a4.spec.lane == {} and fourth.take_lane_repairs() == []
          and lanes_same(a4.spec.cwd, str(proj)), (a4.spec.lane, a4.spec.cwd))
    for m in (mgr, again, third, fourth):
        for w in list(m.workspaces):
            m.remove_workspace(w.id)
    app.processEvents()
    shutil.rmtree(tmp, ignore_errors=True)


def lanes_same(a, b) -> bool:
    from app import lanes
    return lanes.same_path(a, b)


def test_lanes_window():
    """Lanes through the real window: the switch gates everything new (OFF
    runs no git at all and shows no checkbox), Count 3 makes 3 lanes, a lane
    failure still starts the agent in the workspace folder, closing removes
    an empty lane and keeps one with work, the picker revives a retired
    lane's conversation at its identical path, a failed revive never runs in
    the workspace folder, and with the switch off an existing lane still
    repairs and retires."""
    from PySide6.QtWidgets import QApplication, QDialog
    from app import coordination, lanes
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.widgets.main_window import AddTerminalDialog
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-lanes-win-"))
    work = _make_repo(tmp)
    lanes_dir = tmp / "proj.lanes"
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win._save_timer.stop()
    ws = win.manager.create_workspace("Repo", str(work))
    seen = []

    def run_dialog(setup=None):
        """Drive _on_add_terminal_clicked with the dialog's exec stubbed."""
        orig = AddTerminalDialog.exec

        def fake_exec(dlg):
            seen.append(dlg)
            if setup is not None:
                setup(dlg)
            return QDialog.DialogCode.Accepted
        AddTerminalDialog.exec = fake_exec
        try:
            before = list(ws.agents)
            win._on_add_terminal_clicked(ws.id)
        finally:
            AddTerminalDialog.exec = orig
        return [a for a in ws.agents if a not in before]

    with _stub_starts() as starts:
        # --- switch OFF: exactly today's behavior, no git ------------------
        with _record_git() as gitlog:
            new = run_dialog()
        agent = new[0]
        check("lanes window: OFF shows no lane checkbox",
              seen[-1].lane_check is None)
        check("lanes window: OFF runs no git command at all",
              gitlog.calls == [], gitlog.calls)
        check("lanes window: OFF starts a Claude agent in the workspace folder",
              agent.spec.lane == {}
              and starts.cwds(agent.spec.name) == [str(work)],
              starts.calls)
        check("lanes window: OFF leaves no lanes folder", not lanes_dir.exists())

        # --- the dialog with the switch ON ---------------------------------
        win.top_bar.agent_lanes_btn.click()
        check("lanes window: the switch turns lanes on without a restart",
              win.lanes_enabled())
        dlg = AddTerminalDialog("Agent 9", cwd=str(work), lanes_on=True,
                                repo_root=str(work))
        cb = dlg.lane_check
        check("lanes dialog: ON shows the checkbox, ticked for Claude in a git "
              "workspace", cb is not None and cb.isChecked() and cb.isEnabled()
              and dlg.wants_lane())
        ps = dlg.kind_combo.findData(AgentKind.POWERSHELL)
        dlg.kind_combo.setCurrentIndex(ps)
        check("lanes dialog: a shell gets no lane",
              not cb.isChecked() and not cb.isEnabled() and not dlg.wants_lane())
        others = [k for k in (AgentKind.OPENAI, AgentKind.GEMINI)
                  if dlg.kind_combo.findData(k) >= 0]
        dlg.kind_combo.setCurrentIndex(dlg.kind_combo.findData(others[0]))
        check("lanes dialog: other providers get no lane (Claude only, Phase 1)",
              not cb.isChecked() and not cb.isEnabled())
        dlg.kind_combo.setCurrentIndex(dlg.kind_combo.findData(AgentKind.CLAUDE))
        check("lanes dialog: back on Claude it is ticked again",
              cb.isChecked() and cb.isEnabled())
        cb.click()
        dlg.kind_combo.setCurrentIndex(ps)
        dlg.kind_combo.setCurrentIndex(dlg.kind_combo.findData(AgentKind.CLAUDE))
        check("lanes dialog: the user's untick survives a Type change",
              not cb.isChecked() and cb.isEnabled() and not dlg.wants_lane())
        dlg.deleteLater()
        nogit = AddTerminalDialog("Agent 9", cwd=str(tmp), lanes_on=True,
                                  repo_root="")
        check("lanes dialog: a folder outside git gets no lane, with a reason",
              not nogit.lane_check.isEnabled()
              and "git" in nogit.lane_check.toolTip())
        nogit.deleteLater()
        sid_ws = "aaaaaaaa-0000-4000-8000-000000000001"
        _write_transcript(str(work), sid_ws, "main", "an old main chat")
        dlg = AddTerminalDialog("Agent 9", cwd=str(work), lanes_on=True,
                                repo_root=str(work))
        dlg.resume_combo.setCurrentIndex(dlg.resume_combo.findData(sid_ws))
        check("lanes dialog: resuming a workspace-folder conversation unticks "
              "and disables the lane", not dlg.lane_check.isChecked()
              and not dlg.lane_check.isEnabled() and not dlg.wants_lane())
        dlg.deleteLater()

        # --- Count 3: three lanes, created serially, started after ---------
        starts.calls.clear()

        def three(d):
            d.count_plus.click()
            d.count_plus.click()
        trio = run_dialog(three)
        check("lanes window: Count 3 opens 3 agents", len(trio) == 3, len(trio))
        roots = [a.spec.lane.get("root") for a in trio]
        check("lanes window: each gets its own lane folder and branch",
              len(set(roots)) == 3 and all(roots)
              and len({a.spec.lane["branch"] for a in trio}) == 3, roots)
        check("lanes window: the lane is recorded before the save the add makes",
              all(t.get("lane", {}).get("root") in roots for t in
                  win.manager.to_session_dict()["workspaces"][-1]["terminals"]
                  if t.get("lane")))
        check("lanes window: nobody starts before their lane exists",
              starts.calls == [] and all(a.start_hold() for a in trio))
        win.lane_ops.drain(60)
        check("lanes window: all three lanes exist as worktrees",
              all(_git(r, "rev-parse", "--abbrev-ref", "HEAD").startswith(
                  "hive/") for r in roots))
        check("lanes window: each agent then starts once, in its own lane",
              [starts.cwds(a.spec.name) for a in trio]
              == [[a.spec.lane["root"]] for a in trio], starts.calls)
        check("lanes window: the base is recorded once the lane exists",
              all(a.spec.lane["base"] == "main" for a in trio))
        check("lanes window: LANE-CREATE is audited for each",
              _log(store).count("LANE-CREATE") == 3)
        laned = trio[0]
        prompt = laned.spec.system_prompt
        check("lanes window: the system prompt names the lane and branch",
              laned.spec.lane["root"] in prompt
              and laned.spec.lane["branch"] in prompt
              and "integrated" in prompt)
        check("lanes window: the board stays the workspace's own, by absolute "
              "path", ws.board.path in prompt and os.path.isabs(ws.board.path)
              and laned.spec.extra_dirs == [ws.board.dir])
        check("lanes window: no relative .aihive path is handed to an agent",
              all(os.path.isabs(d) for d in laned.spec.extra_dirs)
              and ".aihive" not in prompt.replace(ws.board.path, "")
              and not os.path.exists(os.path.join(laned.spec.lane["root"],
                                                  ".aihive")))
        check("lanes window: an agent with no lane keeps the plain prompt",
              "worktree" not in agent.spec.system_prompt)

        # --- a lane failure still starts the agent, in the workspace folder -
        clash_uid = "c1a5c1" + "0" * 26
        squat = lanes_dir / "clash-c1a5c1"
        squat.mkdir(parents=True)
        (squat / "mine.txt").write_text("not yours")

        def clash_spec(dlg, cwd=""):
            spec = build_spec(AgentKind.CLAUDE, "Clash", cwd=cwd)
            spec.uid = clash_uid
            return spec
        orig_rs = AddTerminalDialog.result_spec
        AddTerminalDialog.result_spec = clash_spec
        try:
            clash = run_dialog()[0]
        finally:
            AddTerminalDialog.result_spec = orig_rs
        win.lane_ops.drain(30)
        check("lanes window: a failed lane leaves the agent with no lane",
              clash.spec.lane == {} and lanes_same(clash.spec.cwd, str(work)))
        check("lanes window: ...and still starts it, in the workspace folder",
              starts.cwds("Clash") == [str(work)], starts.calls)
        check("lanes window: ...audited as LANE-FAIL",
              "LANE-FAIL exists create agent='Clash'" in _log(store))
        check("lanes window: ...without touching the folder in the way",
              (squat / "mine.txt").read_text() == "not yours"
              and "hive/clash-c1a5c1" not in _git(work, "branch", "--list"))

        # --- close: an empty lane goes, a lane with work stays -------------
        empty, busy, live = trio
        (Path(busy.spec.lane["root"]) / "a.txt").write_text("work\n")
        _git(busy.spec.lane["root"], "commit", "-q", "-am", "busy work")
        busy_lane = dict(busy.spec.lane)
        win._close_agent(ws.id, empty.id)
        win._close_agent(ws.id, busy.id)
        win.lane_ops.drain(30)
        check("lanes window: closing a clean, merged lane removes it",
              not os.path.exists(empty.spec.lane["root"])
              and "LANE-REMOVE" in _log(store))
        check("lanes window: closing a lane with commits keeps it",
              os.path.isdir(busy_lane["root"]) and "LANE-KEEP" in _log(store))
        box = win._lane_boxes[-1] if win._lane_boxes else None
        check("lanes window: ...and says so, with no delete button",
              box is not None and "1 unmerged commit" in box.text()
              and "kept" in box.text()
              and [b.text() for b in box.buttons()] == ["Keep", "Open folder"],
              box.text() if box else None)
        if box is not None:
            box.close()
        check("lanes window: closing the cards removed the agents",
              empty not in ws.agents and busy not in ws.agents)

        # --- revive a retired lane's conversation --------------------------
        sid_kept = "bbbbbbbb-0000-4000-8000-000000000002"
        sid_live = "cccccccc-0000-4000-8000-000000000003"
        _write_transcript(busy_lane["root"], sid_kept, busy_lane["branch"],
                          "finish the parser")
        _write_transcript(live.spec.lane["root"], sid_live,
                          live.spec.lane["branch"], "a live agent's chat")
        pick = {}

        def choose(dlg):
            i = dlg.resume_combo.findData(sid_kept)
            pick["label"] = dlg.resume_combo.itemText(i) if i >= 0 else ""
            pick["live"] = dlg.resume_combo.findData(sid_live)
            dlg.resume_combo.setCurrentIndex(i)
            pick["text"] = dlg.lane_check.text()
            pick["count"] = dlg.count()
        _drop_lane_folder(busy_lane["root"])     # make the revive repair it
        starts.calls.clear()
        revived = run_dialog(choose)[0]
        check("lanes picker: a retired lane's conversation is listed",
              "lane " + os.path.basename(busy_lane["root"]) in pick["label"],
              pick)
        check("lanes picker: a live agent's lane conversation is hidden",
              pick["live"] == -1, pick)
        check("lanes picker: picking it says it resumes in its own lane, "
              "Count 1", pick["text"] == "Resumes in its own lane"
              and pick["count"] == 1, pick)
        check("lanes picker: the revived agent records the same lane",
              revived.spec.lane["root"] == busy_lane["root"]
              and revived.spec.lane["branch"] == busy_lane["branch"])
        check("lanes picker: nothing starts before the lane is back",
              starts.calls == [])
        win.lane_ops.drain(30)
        check("lanes picker: the lane is repaired at the identical path, "
              "commits intact", os.path.isdir(busy_lane["root"])
              and _git(busy_lane["root"], "log", "-1", "--format=%s")
              == "busy work")
        args = starts.calls[0][2] if starts.calls else []
        check("lanes picker: then it resumes that conversation, in the lane",
              starts.cwds(revived.spec.name) == [busy_lane["root"]]
              and "--resume" in args and sid_kept in args, starts.calls)
        check("lanes picker: LANE-REVIVE is audited",
              "LANE-REVIVE" in _log(store))

        # --- a revive that fails never runs in the workspace folder --------
        ghost = lanes_dir / "ghost-123456"
        ghost.mkdir()
        (ghost / "stray.txt").write_text("someone else's")
        sid_ghost = "dddddddd-0000-4000-8000-000000000004"
        _write_transcript(str(ghost), sid_ghost, "hive/ghost-123456",
                          "an orphan")
        starts.calls.clear()
        boxes_before = len(win._lane_boxes)
        gone = run_dialog(lambda d: d.resume_combo.setCurrentIndex(
            d.resume_combo.findData(sid_ghost)))
        win.lane_ops.drain(30)
        check("lanes picker: a failed revive starts nothing",
              starts.calls == [], starts.calls)
        check("lanes picker: ...and drops the card instead of using the "
              "workspace folder", all(a not in ws.agents for a in gone))
        check("lanes picker: ...and tells the user",
              len(win._lane_boxes) > boxes_before
              and "not resumed" in win._lane_boxes[-1].text())
        check("lanes picker: ...leaving the folder in the way untouched",
              (ghost / "stray.txt").exists())
        for b in list(win._lane_boxes):
            b.close()

        # --- switch OFF: an existing lane still loads, repairs, retires ----
        win.top_bar.agent_lanes_btn.click()
        check("lanes window: the switch is off again", not win.lanes_enabled())
        live_lane = dict(live.spec.lane)
        win._save_session()
        win._save_timer.stop()
        win.close()
        app.processEvents()
        _drop_lane_folder(live_lane["root"])
        again = create_main_window(SessionStore(path=tmp / "session.json"))
        again._save_timer.stop()
        ws2 = next(w for w in again.manager.workspaces if w.name == "Repo")
        back = next((a for a in ws2.agents
                     if a.spec.lane.get("root") == live_lane["root"]), None)
        check("lanes window: with the switch off a laned agent still loads with "
              "its lane", back is not None and not again.lanes_enabled())
        check("lanes window: ...in its lane folder, not the workspace folder",
              back is not None and lanes_same(back.spec.cwd, live_lane["root"]))
        again.lane_ops.drain(30)
        check("lanes window: ...its missing folder is repaired in place",
              os.path.isdir(live_lane["root"]) and "LANE-REPAIR" in _log(store))
        check("lanes window: ...and it may start again",
              back is not None and not back.start_hold())
        again._close_agent(ws2.id, back.id)
        again.lane_ops.drain(30)
        check("lanes window: ...and closing it retires the lane",
              not os.path.exists(live_lane["root"]))
        again._save_timer.stop()
        again.close()
        app.processEvents()
    check("lanes window: the real .venv survived every removal",
          (work / ".venv" / "marker.txt").read_text() == "real venv")
    shutil.rmtree(tmp, ignore_errors=True)
