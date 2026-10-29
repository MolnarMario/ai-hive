"""Child processes: the Windows Job Object, background shells and their
stats, the pty worker and its launch width."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .harness import SCRATCH_CWD, check, skip


def _fake_job_ids(n):
    """`n` made-up pids for a stubbed job; 0 reads as a failed query."""
    return [100 + 4 * i for i in range(n)]


def test_winjob_process_ids():
    """WinJob.process_ids() lists the live processes in a job -- the
    detection primitive behind poll_bg_shell(). Windows-only; skipped
    everywhere else since job objects don't exist there."""
    if sys.platform != "win32":
        skip('winjob', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import WinJob

    job = WinJob()
    check("winjob: a fresh job object gets a real handle on Windows",
          job._handle is not None)
    check("winjob: process_ids on an empty (unassigned) job is []",
          job.process_ids() == [])

    procs = [subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(5)"])
             for _ in range(2)]
    try:
        for p in procs:
            check(f"winjob: assign succeeds for a live child (pid {p.pid})",
                  job.assign(p.pid))
        check("winjob: process_ids lists both assigned processes",
              sorted(job.process_ids()) == sorted(p.pid for p in procs),
              job.process_ids())
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()
    check("winjob: process_ids after close is [] (no handle)",
          job.process_ids() == [])


def test_winjob_process_ids_and_kill():
    """process_ids() is the identity-carrying sibling of process_count() --
    what kill_bg_shell_extras() needs to know WHICH processes are extra, not
    just how many there are. kill_extra_processes() (identical on
    ProcessWorker and PtyWorker) must kill everything in the job except the
    ids it's told to keep, and leave the kept one alone. Windows-only."""
    if sys.platform != "win32":
        skip('winjob kill', 'non-Windows: job objects are Windows-only')
        return
    import types
    from app.process_worker import (CREATE_NO_WINDOW, WinJob, ProcessWorker,
                                    describe_pid)

    job = WinJob()
    # CREATE_NO_WINDOW avoids spawning a console host (conhost.exe) of our
    # own; membership checks below are still subset (<=), not equality --
    # a dev shell already nested inside its own job (this suite can run
    # inside a live AI Hive agent's own terminal) can add incidental extra
    # members that have nothing to do with the primitives under test.
    procs = [subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(20)"],
                              creationflags=CREATE_NO_WINDOW)
             for _ in range(3)]
    try:
        for p in procs:
            check(f"winjob kill: assign succeeds for pid {p.pid}",
                  job.assign(p.pid))
        ids = job.process_ids()
        check("winjob kill: process_ids reports every assigned pid",
              {p.pid for p in procs} <= set(ids), (ids, [p.pid for p in procs]))

        label = describe_pid(procs[0].pid)
        check("winjob kill: describe_pid names a real live process "
              "(python's own executable), not the bare-pid fallback",
              label.lower() != f"pid {procs[0].pid}"
              and label.lower().startswith("python"), label)
        check("winjob kill: describe_pid falls back to a bare label for an "
              "unreachable/invalid pid",
              describe_pid(0) == "pid 0")

        worker = types.SimpleNamespace(_job=job, job_process_ids=job.process_ids)
        keep_pid = procs[0].pid
        killed = ProcessWorker.kill_extra_processes(worker, {keep_pid})
        # a subset check, not exact equality: this test's own nested-job dev
        # environment (a live shell spawning python which spawns python, all
        # already inside another job) can add incidental extra members
        # (conhost.exe, stray interpreter helpers) that have nothing to do
        # with the primitive under test -- what matters is that the two
        # deliberately spawned targets ARE killed and the kept one NEVER is
        check("winjob kill: kills every pid except the one told to keep",
              keep_pid not in killed
              and {procs[1].pid, procs[2].pid} <= set(killed), killed)

        for p in procs[1:]:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        check("winjob kill: the kept process is still alive",
              procs[0].poll() is None)
        check("winjob kill: the killed processes are gone",
              procs[1].poll() is not None and procs[2].poll() is not None)
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()


def test_bg_shell_workspace_stats():
    """poll_bg_shell()/workspace_stats()'s bg_shell count must: debounce a
    transient process blip (git/rg-style, never flags), never flag a BUSY
    agent (the amber "working" indicator already covers that case), flag
    once a job's process count sits above its learned baseline for
    BG_SHELL_DEBOUNCE_S while quiet, clear the instant the extra process is
    gone, and reset on exit -- transient like busy/waiting, never dirty."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import AgentStatus, BG_SHELL_DEBOUNCE_S
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShell", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    check("bg shell stats: nobody flagged yet",
          mgr.workspace_stats(ws.id)["bg_shell"] == 0)

    stats_events = []
    dirtied = []
    mgr.workspaceStatsChanged.connect(
        lambda wid, s: stats_events.append(dict(s)) if wid == ws.id else None)
    mgr.dirty.connect(lambda: dirtied.append(True))

    fake_now = [1000.0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: _fake_job_ids(count[0])
    process_worker_mod.process_image_path = lambda pid: ""
    try:
        agent.poll_bg_shell()   # learns the baseline (1)
        check("bg shell: the baseline alone never flags",
              not agent.is_bg_shell_busy())

        count[0] = 0   # a failed/unsupported query must never flap state
        agent.poll_bg_shell()
        check("bg shell: an empty job list (query failed) leaves the "
              "state alone", not agent.is_bg_shell_busy())
        count[0] = 1

        count[0] = 2   # an extra process appears
        agent.poll_bg_shell()
        check("bg shell: an extra process alone (before the debounce "
              "elapses) does not flag yet", not agent.is_bg_shell_busy())

        fake_now[0] += 1.0   # a blip: gone before the debounce elapses
        count[0] = 1
        agent.poll_bg_shell()
        check("bg shell: a process that goes away before the debounce "
              "elapses never flags (no flicker on git/rg-style blips)",
              not agent.is_bg_shell_busy() and agent._bg_extra_since is None)

        count[0] = 2
        agent.poll_bg_shell()             # extra_since starts again
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell: an extra process sustained past the debounce flags "
              "it", agent.is_bg_shell_busy())
        check("bg shell stats: count is 1 once flagged",
              mgr.workspace_stats(ws.id)["bg_shell"] == 1)
        check("bg shell stats: workspaceStatsChanged fired live",
              stats_events and stats_events[-1]["bg_shell"] == 1, stats_events)
        check("bg shell: never marks the session dirty (transient)",
              not dirtied, dirtied)

        # a genuinely busy agent must never be flagged, even with an extra
        # process present -- the amber "working" indicator already covers it
        agent._busy = True
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: a busy agent is never flagged",
              not agent.is_bg_shell_busy())
        check("bg shell stats: count drops back to 0 while busy",
              mgr.workspace_stats(ws.id)["bg_shell"] == 0)
        agent._busy = False

        # re-flag once busy clears and it's sustained again (the extra-since
        # clock restarted while busy, so this needs its own poll to start,
        # then a later one past the debounce), then clear the instant the
        # extra process itself is gone
        agent.poll_bg_shell()             # extra_since starts now
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: re-flags once busy clears and it's sustained again",
              agent.is_bg_shell_busy())
        count[0] = 1
        agent.poll_bg_shell()
        check("bg shell: clears the instant the extra process is gone",
              not agent.is_bg_shell_busy())
        check("bg shell stats: workspaceStatsChanged fired on the falling "
              "edge too", stats_events[-1]["bg_shell"] == 0, stats_events)

        # exit resets the latch AND the learned baseline
        count[0] = 2
        agent.poll_bg_shell()             # extra_since starts now
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        check("bg shell: sanity flag before exit", agent.is_bg_shell_busy())
        agent._set_status(AgentStatus.EXITED_OK)
        check("bg shell: exit clears the flag and its learned baseline",
              not agent.is_bg_shell_busy() and agent._bg_baseline is None)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image


def test_bg_shell_settle_relearn():
    """Live-reported: right after a cold boot, the log_activity MCP bridge's
    own double-fork can still be missing when the FIRST post-warmup sample is
    taken, so a job whose real steady state is 3 processes got a baseline of
    1 -- and since baseline only ratchets down, the gear badge stayed on for
    that agent's entire remaining life (session.log showed count=3
    baseline=1 repeatedly, never clearing). BG_SHELL_SETTLE_S fixes this: for
    a further window after warmup, baseline tracks the latest sample outright
    (up or down) instead of only ratcheting down, so a late-arriving steady
    process is absorbed as normal. Once that settle window elapses too, the
    original ratchet-down-only behavior must still catch a genuine background
    job started later -- the settle window must not weaken that guarantee."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-settle-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellSettle", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING

    fake_now = [1000.0]
    agent._session_started = fake_now[0]   # simulate a just-launched agent
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: _fake_job_ids(count[0])
    process_worker_mod.process_image_path = lambda pid: ""
    try:
        agent.poll_bg_shell()   # still within warmup: ignored entirely
        check("bg shell settle: a sample during warmup is ignored",
              agent._bg_baseline is None)

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1   # warmup just elapsed
        count[0] = 1   # the too-early low sample (bridge not forked yet)
        agent.poll_bg_shell()
        check("bg shell settle: first post-warmup sample seeds baseline",
              agent._bg_baseline == 1, agent._bg_baseline)
        check("bg shell settle: never flags while still settling",
              not agent.is_bg_shell_busy())

        fake_now[0] += 2.0   # still within the settle window
        count[0] = 3   # the bridge's child finally forked -- real steady state
        agent.poll_bg_shell()
        check("bg shell settle: baseline RISES to the real steady state "
              "instead of staying locked on the too-early low sample",
              agent._bg_baseline == 3, agent._bg_baseline)
        check("bg shell settle: still doesn't flag mid-settle even though "
              "count rose above the old baseline",
              not agent.is_bg_shell_busy())

        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1   # sustained, but still settling
        agent.poll_bg_shell()
        check("bg shell settle: a sustained-but-unchanged count never flags "
              "while the settle window is still open",
              not agent.is_bg_shell_busy())

        # settle window fully elapsed: ratchet-down-only resumes, and the
        # now-correct baseline must not spuriously flag a steady count
        fake_now[0] = 1000.0 + BG_SHELL_WARMUP_S + BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell settle: once settled, an unchanged steady count "
              "never flags (this is the bug: it used to flag forever)",
              not agent.is_bg_shell_busy())
        check("bg shell settle: baseline holds at the learned steady state",
              agent._bg_baseline == 3, agent._bg_baseline)

        # a GENUINE background job starting after settle must still be
        # caught -- the settle window must not weaken the real detection
        count[0] = 5
        agent.poll_bg_shell()             # extra_since starts
        check("bg shell settle: a fresh extra process doesn't flag before "
              "the debounce elapses", not agent.is_bg_shell_busy())
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell settle: a genuine background job started after "
              "settle still flags once sustained", agent.is_bg_shell_busy())
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image


def test_bg_shell_submit_ends_settle():
    """A background command started inside the first 16 s of a launch was
    adopted by the settle window as resting overhead, so the gear never lit
    and Free up RAM's sleep ended it an hour later. The launch's first
    submit now takes the baseline and ends the settle window: whatever
    shows up after it counts as extra."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-submit-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellSubmit", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Quick",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: _fake_job_ids(count[0])
    process_worker_mod.process_image_path = lambda pid: ""
    try:
        fake_now[0] += 3.0   # a task lands inside the warmup
        agent._note_submit()
        check("bg shell submit: the first submit seeds the baseline",
              agent._bg_baseline == 1 and agent._bg_baseline_pids
              == set(_fake_job_ids(1)), agent._bg_baseline)

        count[0] = 2   # the agent starts a background command at 5 s
        fake_now[0] = 1000.0 + BG_SHELL_WARMUP_S + 0.1
        agent.poll_bg_shell()
        check("bg shell submit: the settle window no longer adopts a "
              "process started after the submit", agent._bg_baseline == 1,
              agent._bg_baseline)
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell submit: that command lights the gear while still "
              "inside the old settle window",
              agent.is_bg_shell_busy()
              and fake_now[0] < 1000.0 + BG_SHELL_WARMUP_S
              + BG_SHELL_SETTLE_S)
        check("bg shell submit: the kill menu lists it",
              agent.bg_shell_extra_pids() == [_fake_job_ids(2)[1]],
              agent.bg_shell_extra_pids())
        agent._note_submit()   # a later submit must not re-seed
        check("bg shell submit: a later submit keeps the baseline",
              agent._bg_baseline == 1, agent._bg_baseline)

        agent._reset_bg_shell()   # a restart learns from scratch again
        check("bg shell submit: a restart reopens the settle window",
              not agent._bg_settled and agent._bg_baseline is None)

        # a launch with no submit inside the windows: a submit after them
        # leaves the learned baseline alone
        count[0] = 3
        fake_now[0] = 1000.0 + BG_SHELL_WARMUP_S + 1.0
        agent.poll_bg_shell()
        fake_now[0] = 1000.0 + BG_SHELL_WARMUP_S + BG_SHELL_SETTLE_S + 1.0
        count[0] = 4
        agent._note_submit()
        check("bg shell submit: a submit after the settle window keeps the "
              "learned baseline", agent._bg_baseline == 3,
              agent._bg_baseline)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image


def test_bg_shell_kill_extras():
    """kill_bg_shell_extras() is a no-op unless the gear is actually lit (a
    stray click on a just-cleared marker must not kill anything), and once
    lit it must ask the worker to kill everything EXCEPT the agent's own
    root process and whatever was seen while the baseline was learned (the
    mcp bridge, ConPTY's own conhost/OpenConsole helper) -- never the whole
    job, which would also take down the interactive session. A successful
    kill clears the latch immediately rather than waiting for the next poll,
    and is recorded to the audit trail."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-kill-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellKill", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    audit_lines = []
    agent.audit = audit_lines.append

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    orig_pid = agent.worker.pid
    orig_kill = agent.worker.kill_extra_processes
    pids = [100, 101]           # root(100) + the mcp bridge(101), the baseline
    kill_calls = []
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: list(pids)
    process_worker_mod.process_image_path = lambda pid: ""
    agent.worker.pid = lambda: 100
    agent.worker.kill_extra_processes = (
        lambda keep: kill_calls.append(keep) or [p for p in pids
                                                  if p not in keep])
    try:
        killed = agent.kill_bg_shell_extras()
        check("bg shell kill: a no-op when nothing is flagged",
              killed == [] and not kill_calls, killed)

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1   # seed the baseline: {100,101}
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1    # settle elapses, still {100,101}
        agent.poll_bg_shell()
        check("bg shell kill: baseline pids learned during settle",
              agent._bg_baseline_pids == {100, 101}, agent._bg_baseline_pids)

        pids.append(103)   # a genuine extra shows up (e.g. a Gradle daemon)
        agent.poll_bg_shell()             # extra_since starts
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell kill: flags once the extra is sustained",
              agent.is_bg_shell_busy())

        killed = agent.kill_bg_shell_extras()
        check("bg shell kill: asks the worker to keep the baseline pids "
              "(root + mcp bridge), never the whole job",
              kill_calls and kill_calls[-1] == {100, 101}, kill_calls)
        check("bg shell kill: returns exactly the pid(s) the worker killed",
              killed == [103], killed)
        check("bg shell kill: clears the latch immediately, not on the "
              "next poll", not agent.is_bg_shell_busy())
        check("bg shell kill: the kill is recorded to the audit trail",
              any("BG-SHELL-KILL" in line and "103" in line
                  for line in audit_lines), audit_lines)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image
        agent.worker.pid = orig_pid
        agent.worker.kill_extra_processes = orig_kill


def test_bg_shell_live_ui():
    """The card header's gear mark and the sidebar's inline agent-row gear
    mark both say "idle, but a background command it started is still
    running" -- and both must clear the instant it isn't true anymore. The
    card is signal-driven (bg_shell_changed), the sidebar AgentRow is polled
    (refresh() reads is_bg_shell_busy() directly) -- this exercises both
    paths end to end."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.sidebar import AgentRow

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Shelled", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    check("bg shell UI: gear hidden before anything is flagged",
          not card.bg_mark.isVisible())

    a._bg_shell = True
    a.bg_shell_changed.emit(True)
    pump(30)
    check("bg shell UI: card gear shows once the agent is flagged",
          card.bg_mark.isVisible())

    row = AgentRow("w1", a)
    check("bg shell UI: a freshly built sidebar agent row also shows it",
          not row.bg_mark.isHidden())

    a._bg_shell = False
    a.bg_shell_changed.emit(False)
    pump(30)
    check("bg shell UI: card gear disappears LIVE once cleared",
          not card.bg_mark.isVisible())
    row.refresh(a)
    check("bg shell UI: sidebar agent row also clears on its next poll",
          row.bg_mark.isHidden())

    # clicking either opens a menu of individual processes to kill, rather
    # than an all-or-nothing kill on the click itself -- an accidental click
    # near the badge must not risk killing something an agent is actually
    # waiting on. bg_shell_extra_pids() is empty here (no baseline was ever
    # learned in this test), so both handlers must take the early-return
    # "nothing to show" path rather than opening a real (blocking) QMenu --
    # this is what proves a click can never fall back to killing everything.
    a._bg_shell = True
    a.bg_shell_changed.emit(True)
    pump(30)
    check("bg shell UI: nothing to kill yet (no baseline learned)",
          a.bg_shell_extra_pids() == [])
    card.bg_mark.click()          # must NOT hang on a QMenu.exec() or crash
    check("bg shell UI: clicking the card gear with nothing resolvable is a "
          "safe no-op, not a kill-everything fallback", a.is_bg_shell_busy())

    row.refresh(a)
    row.bg_mark.click()           # same early-return path, same guarantee
    check("bg shell UI: clicking the sidebar gear is the same safe no-op",
          a.is_bg_shell_busy())

    card.detach(); card.close()
    row.deleteLater()
    a.deleteLater()


def test_card_bg_mark_compact():
    """The header gear is one glyph wide. It used to inherit QToolButton's
    8px side padding plus the two spaces QToolButton adds around text, a
    47px pill that pushed the task summary away."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard
    from main import setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Geared", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show()
    a._bg_shell = True
    a.bg_shell_changed.emit(True)
    loop = QEventLoop(); QTimer.singleShot(60, loop.quit); loop.exec()
    w = card.bg_mark.width()
    check("bg shell UI: the card gear is at most 24px wide", 0 < w <= 24, w)
    card.detach(); card.close()
    a.deleteLater()


def test_bg_shell_extra_pids_and_kill_pid():
    """bg_shell_extra_pids() is what the kill menu lists, and
    kill_bg_shell_pid() is its per-item action -- letting the user kill one
    process at a time instead of the all-or-nothing kill_bg_shell_extras().
    kill_bg_shell_pid() must refuse anything that isn't CURRENTLY a genuine
    extra (re-checked, not trusted from a menu built a moment ago), and must
    only clear the latch once EVERY extra is gone, not on the first kill."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-pids-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellPids", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Shelled",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    kill_calls = []

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    orig_pid = agent.worker.pid
    orig_kill_pid = agent.worker.kill_pid
    pids = [100, 101]   # root(100) + mcp bridge(101): the baseline
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: list(pids)
    process_worker_mod.process_image_path = lambda pid: ""
    agent.worker.pid = lambda: 100
    agent.worker.kill_pid = lambda p: (kill_calls.append(p),
                                       pids.remove(p) if p in pids else None)
    try:
        check("bg shell pids: nothing before a baseline is learned",
              agent.bg_shell_extra_pids() == [])

        fake_now[0] += BG_SHELL_WARMUP_S + 0.1
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell pids: still nothing once settled with no extras",
              agent.bg_shell_extra_pids() == [])

        pids.extend([102, 103])   # e.g. Gradle daemon + Kotlin daemon
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell pids: lists exactly the extras, not root/baseline",
              agent.bg_shell_extra_pids() == [102, 103],
              agent.bg_shell_extra_pids())

        check("bg shell pids: refuses to kill a baseline pid (not extra)",
              agent.kill_bg_shell_pid(101) is False and not kill_calls)
        check("bg shell pids: refuses to kill the agent's own root process",
              agent.kill_bg_shell_pid(100) is False and not kill_calls)

        ok = agent.kill_bg_shell_pid(102)
        check("bg shell pids: kills a genuine extra", ok is True)
        check("bg shell pids: the worker was asked to kill exactly that pid",
              kill_calls == [102], kill_calls)
        check("bg shell pids: one extra remains, so the badge stays lit",
              agent.is_bg_shell_busy() and agent.bg_shell_extra_pids() == [103])

        ok = agent.kill_bg_shell_pid(103)
        check("bg shell pids: kills the last remaining extra", ok is True)
        check("bg shell pids: the badge clears once EVERY extra is gone",
              not agent.is_bg_shell_busy())
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image
        agent.worker.pid = orig_pid
        agent.worker.kill_pid = orig_kill_pid


def test_cli_tooling_classifier():
    """is_cli_tooling() names the programs an AI CLI runs for itself, which
    the gear badge leaves out. The positives are the image paths of a live
    Codex 0.162 agent whose gear stayed lit while idle; the negatives are
    what a real background command runs as, and must keep counting."""
    from app.process_worker import is_cli_tooling

    saved = os.environ.pop("CODEX_HOME", None)
    home = os.path.expanduser("~")
    local = os.environ["LOCALAPPDATA"]
    daemon = os.path.join(home, ".codex", "packages", "app-server-daemon",
                          "releases", "0.162.1-x86_64-pc-windows-msvc", "bin",
                          "codex.exe")
    cua = os.path.join(local, "OpenAI", "Codex", "runtimes", "cua_node",
                       "71e3f41277f96d73", "bin")
    try:
        tooling = {
            "the app-server daemon (and its updater)": daemon,
            "the daemon launched by a \\\\?\\ path": "\\\\?\\" + daemon,
            "the same path in other case": daemon.upper(),
            "the computer-use node runtime": os.path.join(cua, "node.exe"),
            "the node_repl MCP server": os.path.join(cua, "node_repl.exe"),
            "the desktop app's own rg": os.path.join(
                local, "OpenAI", "Codex", "bin", "26f5cb65bac96647", "rg.exe"),
            "a bundled plugin python": os.path.join(
                home, ".cache", "codex-runtimes", "codex-primary-runtime",
                "dependencies", "python", "python.exe"),
            "the npm Codex TUI": (r"C:\Users\u\AppData\Roaming\npm"
                                  r"\node_modules\@openai\codex"
                                  r"\node_modules\@openai\codex-win32-x64"
                                  r"\vendor\x86_64-pc-windows-msvc\bin"
                                  r"\codex.exe"),
            "npm Codex's rg under another npm prefix": (
                r"C:\nvm\v24\node_modules\@openai\codex-win32-x64\vendor"
                r"\x86_64-pc-windows-msvc\codex-path\rg.exe"),
            "npm's staging copy during a Codex update": (
                r"C:\npm\node_modules\@openai\.codex-NrqtOZ5d"
                r"\node_modules\@openai\codex-win32-x64\bin\codex.exe"),
            "the sandbox command runner": os.path.join(
                home, ".codex", ".sandbox-bin",
                "codex-command-runner-0.160.1.exe"),
            "a console host": r"C:\Windows\System32\conhost.exe",
            "a ConPTY console host": (r"C:\Program Files\WindowsApps\Term"
                                      r"\OpenConsole.exe"),
        }
        for what, path in tooling.items():
            check(f"cli tooling: {what} is tooling", is_cli_tooling(path),
                  path)
        work = {
            "the user's node": r"C:\Users\u\AppData\Local\nvm\v24\node.exe",
            "PowerShell": (r"C:\WINDOWS\System32\WindowsPowerShell\v1.0"
                           r"\powershell.exe"),
            "cmd": r"C:\WINDOWS\system32\cmd.exe",
            "Git Bash": r"C:\Program Files\Git\usr\bin\bash.exe",
            "a project venv python": r"C:\proj\.venv\Scripts\python.exe",
            "a folder that only starts like .codex": os.path.join(
                home, ".codexbackup", "node.exe"),
            "a program only named like codex": r"C:\proj\codexify.exe",
            "a codex.exe built from source": (r"C:\src\codex\codex-rs"
                                              r"\target\debug\codex.exe"),
            "another @openai npm package": (r"C:\npm\node_modules\@openai"
                                            r"\agents\bin\tool.exe"),
            "an unreadable path": "",
        }
        for what, path in work.items():
            check(f"cli tooling: {what} is not tooling",
                  not is_cli_tooling(path), path)
        os.environ["CODEX_HOME"] = r"D:\codexhome"
        check("cli tooling: CODEX_HOME moves Codex's own folder",
              is_cli_tooling(r"D:\codexhome\packages\x\node.exe"))
    finally:
        if saved is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = saved


def test_bg_shell_ignores_cli_tooling():
    """Live report: a Codex agent showed the gear right after a cold boot
    and kept it while idle (session.log: count=10 baseline=4). Codex 0.162
    starts a shared app-server daemon, its updater, two node_repl MCP
    servers and their console hosts 10 to 17 s after launch, and the last
    of them landed after the baseline had settled. Those must never count,
    whenever they arrive, and the gear's kill-all must keep them (a kill
    took down the daemon Codex runs its turns on). A real background
    command still lights the gear and is the only thing the menu lists."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-tooling-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellTooling", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.OPENAI, "Codexed",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING

    home = os.path.expanduser("~")
    cua = os.path.join(os.environ["LOCALAPPDATA"], "OpenAI", "Codex",
                       "runtimes", "cua_node", "71e3f41277f96d73", "bin")
    daemon = os.path.join(home, ".codex", "packages", "app-server-daemon",
                          "releases", "0.162.1", "bin", "codex.exe")
    conhost = r"C:\Windows\System32\conhost.exe"
    images = {
        200: r"C:\WINDOWS\system32\cmd.exe",       # the codex.CMD shim, root
        204: r"C:\Users\u\AppData\Local\nvm\v24\node.exe",
        208: (r"C:\Users\u\AppData\Roaming\npm\node_modules\@openai\codex"
              r"\node_modules\@openai\codex-win32-x64\vendor\bin"
              r"\codex.exe"),                     # the Codex TUI
        212: daemon, 216: daemon,                  # daemon + its updater
        220: os.path.join(cua, "node.exe"), 224: conhost,
        228: os.path.join(cua, "node_repl.exe"),
        232: os.path.join(cua, "node_repl.exe"), 236: conhost,
        240: (r"C:\WINDOWS\System32\WindowsPowerShell\v1.0"
              r"\powershell.exe"),
        244: conhost,                              # its console host
    }
    pids = [200, 204, 208]
    keeps = []

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    orig_pid = agent.worker.pid
    orig_kill = agent.worker.kill_extra_processes
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: list(pids)
    process_worker_mod.process_image_path = lambda pid: images.get(pid, "")
    agent.worker.pid = lambda: 200
    agent.worker.kill_extra_processes = (
        lambda keep: keeps.append(set(keep)) or [p for p in pids
                                                 if p not in keep])
    try:
        fake_now[0] += BG_SHELL_WARMUP_S + 0.1
        agent.poll_bg_shell()
        pids.append(212)             # the daemon makes it into the settle
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell tooling: the baseline counts the shim and node, "
              "not the Codex binaries", agent._bg_baseline == 2,
              agent._bg_baseline)

        pids.extend([216, 220, 224, 228, 232, 236])   # after the settle
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        fake_now[0] += 600
        agent.poll_bg_shell()
        check("bg shell tooling: Codex's daemon, updater and MCP servers "
              "arriving after the settle never light the gear",
              not agent.is_bg_shell_busy())
        check("bg shell tooling: and the kill menu lists none of them",
              agent.bg_shell_extra_pids() == [], agent.bg_shell_extra_pids())
        check("bg shell tooling: a click on the unlit gear kills nothing",
              agent.kill_bg_shell_extras() == [] and not keeps, keeps)

        pids.extend([240, 244])      # a real background command
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell tooling: a background command still lights the "
              "gear", agent.is_bg_shell_busy())
        check("bg shell tooling: the menu lists the command, not its "
              "console host or Codex's tooling",
              agent.bg_shell_extra_pids() == [240],
              agent.bg_shell_extra_pids())
        check("bg shell tooling: a kill of one tooling pid is refused",
              agent.kill_bg_shell_pid(212) is False)
        killed = agent.kill_bg_shell_extras()
        check("bg shell tooling: kill-all keeps the daemon, its MCP servers "
              "and every console host",
              keeps and {212, 216, 220, 224, 228, 232, 236, 244} <= keeps[-1]
              and 240 not in keeps[-1], keeps)
        check("bg shell tooling: kill-all kills only the command",
              killed == [240], killed)

        # a nested `codex` the agent ran in the background, from its own
        # npm install: codex.exe is tooling by location, but npm's node
        # shim waits for it and still counts
        pids[:] = [p for p in pids if p not in (240, 244)]
        images[248] = images[204]                  # the shim's node.exe
        images[252] = images[208]                  # the same npm codex.exe
        pids.extend([248, 252])
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell tooling: a nested codex run from the agent's own "
              "npm install lights the gear through its node shim",
              agent.is_bg_shell_busy())
        check("bg shell tooling: the menu offers the shim, whose tree kill "
              "takes the nested codex with it",
              agent.bg_shell_extra_pids() == [248],
              agent.bg_shell_extra_pids())
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image
        agent.worker.pid = orig_pid
        agent.worker.kill_extra_processes = orig_kill


def test_cli_tooling_real_process():
    """process_image_path() reads a live process's real executable path,
    and _job_tooling_pids() tells a program under Codex's runtime folder
    apart from an ordinary child in the same job. Measured on real
    processes, since every other gear test stubs the image lookup."""
    if sys.platform != "win32":
        skip('cli tooling real', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import (CREATE_NO_WINDOW, WinJob, is_cli_tooling,
                                    process_image_path)
    from app.terminal_agent import TerminalAgent

    runtime = Path(os.environ["LOCALAPPDATA"], "OpenAI", "Codex", "runtimes",
                   "smoke")
    runtime.mkdir(parents=True, exist_ok=True)
    fake = runtime / "ping.exe"
    shutil.copyfile(Path(os.environ.get("SystemRoot", r"C:\Windows"),
                         "System32", "PING.EXE"), fake)
    job = WinJob()
    procs = [subprocess.Popen([str(fake), "-n", "30", "127.0.0.1"],
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,
                              creationflags=CREATE_NO_WINDOW),
             subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(30)"],
                              creationflags=CREATE_NO_WINDOW)]
    try:
        for p in procs:
            job.assign(p.pid)
        tool, plain = procs
        path = process_image_path(tool.pid)
        check("cli tooling real: the image path is the copy that runs",
              os.path.normcase(path) == os.path.normcase(str(fake)),
              (path, str(fake)))
        check("cli tooling real: a program in Codex's runtime folder is "
              "tooling", is_cli_tooling(path), path)
        check("cli tooling real: a plain python child is not",
              not is_cli_tooling(process_image_path(plain.pid)),
              process_image_path(plain.pid))
        check("cli tooling real: an exited or unknown pid reads as no path",
              process_image_path(0) == "")
        found = TerminalAgent._job_tooling_pids(job.process_ids())
        check("cli tooling real: the job's tooling is exactly the copy",
              found == {tool.pid}, (found, tool.pid, plain.pid))
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()


def test_hive_bridge_classifier():
    """is_hive_bridge() names AI Hive's own log_activity bridge from its
    image and command line, as the live app showed them: the venv launcher
    and the base interpreter it starts, both `-m app.mcp_server`. Anything
    else a python runs is work and keeps counting."""
    from app.process_worker import HIVE_BRIDGE_ARGS, is_hive_bridge

    venv = r"C:\proj\ai-hive\.venv\Scripts\pythonw.exe"
    base = r"C:\Program Files\Python311\pythonw.exe"
    check("hive bridge: the config starts it with these args",
          HIVE_BRIDGE_ARGS == ("-m", "app.mcp_server"), HIVE_BRIDGE_ARGS)
    bridges = {
        "the venv launcher": (venv, f'"{venv}" -m app.mcp_server'),
        "the base interpreter": (base, f'"{base}" -m app.mcp_server'),
        "a console python": (r"C:\Py\python.exe",
                             r"C:\Py\python.exe -m app.mcp_server"),
        "an upper-case image": (base.upper(), f'"{base}" -m app.mcp_server'),
    }
    for what, (image, line) in bridges.items():
        check(f"hive bridge: {what} is the bridge",
              is_hive_bridge(image, line), (image, line))
    work = {
        "a python script": (base, f'"{base}" build.py'),
        "the bridge module with more args": (
            base, f'"{base}" -m app.mcp_server --selftest'),
        "another module": (base, f'"{base}" -m pytest'),
        "a non-python image": (r"C:\tools\node.exe",
                               r"node.exe -m app.mcp_server"),
        "an unreadable command line": (base, ""),
        "an unreadable image": ("", f'"{base}" -m app.mcp_server'),
    }
    for what, (image, line) in work.items():
        check(f"hive bridge: {what} is not the bridge",
              not is_hive_bridge(image, line), (image, line))


def test_bg_shell_ignores_late_hive_bridge():
    """Live report: right after launch, nearly every Claude agent showed the
    gear while idle and kept it (session.log: count=3 baseline=1). Claude
    started AI Hive's log_activity bridge, a venv launcher plus the base
    python it starts, 25 to 39 s after launch on a start of 15 agents, after
    the baseline had settled on claude.exe alone. The bridge must never
    count, whenever it arrives, and kill-all must keep it."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import (AgentStatus, BG_SHELL_DEBOUNCE_S,
                                     BG_SHELL_SETTLE_S, BG_SHELL_WARMUP_S)
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec
    import app.terminal_agent as terminal_agent_mod
    import app.process_worker as process_worker_mod

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-bgshell-bridge-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("BgShellBridge", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Bridged",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING

    venv = r"C:\proj\ai-hive\.venv\Scripts\pythonw.exe"
    base = r"C:\Program Files\Python311\pythonw.exe"
    images = {300: r"C:\Users\u\.local\bin\claude.exe",
              304: venv, 308: base, 312: base}
    lines = {300: r"C:\Users\u\.local\bin\claude.exe --resume x",
             304: f'"{venv}" -m app.mcp_server',
             308: f'"{base}" -m app.mcp_server',
             312: f'"{base}" serve.py'}
    pids = [300]
    keeps = []

    fake_now = [1000.0]
    agent._session_started = fake_now[0]
    orig_time = terminal_agent_mod.time.time
    orig_ids = agent.worker.job_process_ids
    orig_image = process_worker_mod.process_image_path
    orig_line = process_worker_mod.process_command_line
    orig_pid = agent.worker.pid
    orig_kill = agent.worker.kill_extra_processes
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_ids = lambda: list(pids)
    process_worker_mod.process_image_path = lambda pid: images.get(pid, "")
    process_worker_mod.process_command_line = lambda pid: lines.get(pid, "")
    agent.worker.pid = lambda: 300
    agent.worker.kill_extra_processes = (
        lambda keep: keeps.append(set(keep)) or [p for p in pids
                                                 if p not in keep])
    try:
        fake_now[0] += BG_SHELL_WARMUP_S + 0.1
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_SETTLE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell bridge: the baseline is claude alone",
              agent._bg_baseline == 1, agent._bg_baseline)

        fake_now[0] = agent._session_started + 30
        pids.extend([304, 308])      # the bridge pair, long after the settle
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 1
        agent.poll_bg_shell()
        fake_now[0] += 600
        agent.poll_bg_shell()
        check("bg shell bridge: a bridge arriving after the settle never "
              "lights the gear", not agent.is_bg_shell_busy())
        check("bg shell bridge: and the kill menu does not list it",
              agent.bg_shell_extra_pids() == [], agent.bg_shell_extra_pids())

        pids.append(312)             # a real python background command
        agent.poll_bg_shell()
        fake_now[0] += BG_SHELL_DEBOUNCE_S + 0.1
        agent.poll_bg_shell()
        check("bg shell bridge: a python background command still lights "
              "the gear", agent.is_bg_shell_busy())
        check("bg shell bridge: the menu lists only that command",
              agent.bg_shell_extra_pids() == [312],
              agent.bg_shell_extra_pids())
        check("bg shell bridge: a kill of a bridge pid is refused",
              agent.kill_bg_shell_pid(304) is False)
        killed = agent.kill_bg_shell_extras()
        check("bg shell bridge: kill-all keeps both bridge processes",
              keeps and {304, 308} <= keeps[-1] and 312 not in keeps[-1],
              keeps)
        check("bg shell bridge: kill-all kills only the command",
              killed == [312], killed)
    finally:
        terminal_agent_mod.time.time = orig_time
        agent.worker.job_process_ids = orig_ids
        process_worker_mod.process_image_path = orig_image
        process_worker_mod.process_command_line = orig_line
        agent.worker.pid = orig_pid
        agent.worker.kill_extra_processes = orig_kill


def test_hive_bridge_real_process():
    """process_command_line() reads a live process's real command line, and
    _job_tooling_pids() finds a real `-m app.mcp_server` (every process of
    it, a venv launcher included) while a plain python child in the same
    job still counts."""
    if sys.platform != "win32":
        skip('hive bridge real', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import (CREATE_NO_WINDOW, WinJob, is_cli_tooling,
                                    process_command_line, process_image_path)
    from app.terminal_agent import TerminalAgent

    base = getattr(sys, "_base_executable", sys.executable)
    launcher = os.path.normcase(sys.executable) != os.path.normcase(base)
    repo = str(Path(__file__).resolve().parents[2])
    env = dict(os.environ, PYTHONPATH=repo, AIHIVE_PIPE="aihive-smoke-none")
    job = WinJob()
    # A parent that is one process (the base interpreter, no launcher) joins
    # the job before it starts anything, so both children and the base
    # interpreter each venv launcher starts are born inside it. Assigning a
    # venv child after its Popen could lose that second process to the race.
    parent = subprocess.Popen([base, "-c", _BRIDGE_PARENT, sys.executable,
                               SCRATCH_CWD],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, env=env,
                              creationflags=CREATE_NO_WINDOW)
    try:
        job.assign(parent.pid)
        parent.stdin.write("go\n")
        parent.stdin.flush()
        pids = [int(p) for p in parent.stdout.readline().split()]
        check("hive bridge real: the parent started both children",
              len(pids) == 2, pids)
        if len(pids) != 2:
            return
        bridge, plain = pids
        line = process_command_line(bridge)
        check("hive bridge real: the command line ends in the bridge args",
              line.split()[-2:] == ["-m", "app.mcp_server"], line)
        check("hive bridge real: an unknown pid reads as no command line",
              process_command_line(0) == "")
        # a venv python is a launcher: each child above is two processes,
        # and the second shows up a moment after its parent's Popen.
        # CREATE_NO_WINDOW also gives each a hidden conhost, tooling already.
        per = 2 if launcher else 1

        def pythons(ids):
            return [p for p in ids if p != parent.pid
                    and not is_cli_tooling(process_image_path(p))]

        deadline = time.time() + 5
        ids = job.process_ids()
        while time.time() < deadline and len(pythons(ids)) < 2 * per:
            time.sleep(0.1)
            ids = job.process_ids()
        found = TerminalAgent._job_tooling_pids(ids)
        check("hive bridge real: the bridge is tooling, the plain child is "
              "not", bridge in found and plain not in found,
              (found, bridge, plain))
        bridges = {p for p in pythons(ids)
                   if process_command_line(p).split()[-2:]
                   == ["-m", "app.mcp_server"]}
        check("hive bridge real: every process of the bridge is tooling and "
              "nothing else is",
              len(bridges) == per and found & set(pythons(ids)) == bridges,
              (ids, found, bridges, per))
    finally:
        job.terminate_tree()
        try:
            parent.wait(timeout=5)
        except Exception:
            parent.kill()
        job.close()


# Run as the base interpreter inside a WinJob (test_hive_bridge_real_process):
# waits until it has joined, then starts the bridge and a plain sleeper with
# argv[1] (the venv python) and prints their pids.
_BRIDGE_PARENT = r'''
import subprocess, sys
sys.stdin.readline()
flags = 0x08000000  # CREATE_NO_WINDOW
bridge = subprocess.Popen([sys.argv[1], "-m", "app.mcp_server"],
                          stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, cwd=sys.argv[2],
                          creationflags=flags)
plain = subprocess.Popen([sys.argv[1], "-c", "import time; time.sleep(30)"],
                         creationflags=flags)
print(bridge.pid, plain.pid, flush=True)
sys.stdin.readline()
'''


# Run inside a WinJob. Spawns one plain child and one that asks to break away,
# then joins a job of its own the way node's libuv puts every child it spawns
# in one (breakaway allowed, silently for children that don't ask) and spawns
# the same pair again: that is where the Codex TUI starts its daemon from.
_BREAKAWAY_CHILD = r'''
import ctypes, subprocess, sys
from ctypes import wintypes
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
k32.CreateJobObjectW.restype = wintypes.HANDLE
k32.GetCurrentProcess.restype = wintypes.HANDLE
k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                        ctypes.c_void_p, wintypes.DWORD]
k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]

class Limits(ctypes.Structure):
    _fields_ = [("times", wintypes.LARGE_INTEGER * 2),
                ("LimitFlags", wintypes.DWORD),
                ("working_set", ctypes.c_size_t * 2),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("classes", wintypes.DWORD * 2),
                ("io", ctypes.c_ulonglong * 6),
                ("memory", ctypes.c_size_t * 4)]

def spawn(flags):
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            creationflags=0x08000000 | flags).pid

BREAKAWAY = 0x01000000
sys.stdin.readline()
pids = [spawn(0), spawn(BREAKAWAY)]
libuv = k32.CreateJobObjectW(None, None)
info = Limits(LimitFlags=0x2000 | 0x0800 | 0x1000)
assert k32.SetInformationJobObject(libuv, 9, ctypes.byref(info),
                                   ctypes.sizeof(info))
assert k32.AssignProcessToJobObject(libuv, k32.GetCurrentProcess())
pids += [spawn(0), spawn(BREAKAWAY)]
print(*pids, flush=True)
sys.stdin.readline()
'''


def _breakaway_scenario(job):
    """Run _BREAKAWAY_CHILD inside `job`. Returns its four children (plain,
    asked, plain under a node-style nested job, asked under it), the job's
    members once they started, and the parent to reap; pids is [] when the
    parent didn't report four."""
    from app.process_worker import CREATE_NO_WINDOW

    script = Path(tempfile.mkdtemp()) / "breakaway_child.py"
    script.write_text(_BREAKAWAY_CHILD, encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(script)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              text=True, creationflags=CREATE_NO_WINDOW)
    joined = job.assign(parent.pid)
    parent.stdin.write("go\n")
    parent.stdin.flush()
    pids = [int(p) for p in parent.stdout.readline().split()]
    return joined, (pids if len(pids) == 4 else []), \
        set(job.process_ids()), parent


def _breakaway_procs():
    """alive(pid) and kill(pid) on real processes, for the breakaway tests."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]

    def alive(pid):
        h = k32.OpenProcess(0x00100000 | 0x0001, False, pid)  # SYNCHRONIZE|TERMINATE
        if not h:
            return False
        try:
            return k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
        finally:
            k32.CloseHandle(h)

    def kill(pid):
        h = k32.OpenProcess(0x0001, False, pid)
        if h:
            k32.TerminateProcess(h, 1)
            k32.CloseHandle(h)

    return alive, kill


def _reap_breakaway(job, pids, parent, kill):
    job.terminate_tree()
    job.close()
    for pid in pids:
        kill(pid)
    try:
        parent.wait(timeout=5)
    except Exception:
        parent.kill()


def test_winjob_breakaway_only_on_request():
    """In a Codex agent's job, a child that asks to break away leaves it,
    and every other child stays and dies with it. Codex starts its shared
    app-server daemon that way from a TUI that node put in a nested job.
    Before BREAKAWAY_OK, Windows kept the daemon in the agent's job without
    failing the launch, and closing that one agent killed the daemon every
    Codex client was using. Real processes, since the flags only matter to
    the kernel."""
    if sys.platform != "win32":
        skip('winjob breakaway', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import WinJob

    alive, kill = _breakaway_procs()
    job = WinJob(breakaway=True)
    joined, pids, ids, parent = _breakaway_scenario(job)
    try:
        check("winjob breakaway: the parent joins the job", joined)
        check("winjob breakaway: the parent started its four children",
              len(pids) == 4, pids)
        if len(pids) != 4:
            return
        plain, asked, plain_nested, asked_nested = pids
        check("winjob breakaway: a plain child stays in the job",
              plain in ids, (plain, sorted(ids)))
        check("winjob breakaway: a child that asks to break away leaves it",
              asked not in ids, (asked, sorted(ids)))
        check("winjob breakaway: under a node-style nested job a plain "
              "child still stays", plain_nested in ids,
              (plain_nested, sorted(ids)))
        check("winjob breakaway: and one that asks leaves, like Codex's "
              "daemon", asked_nested not in ids, (asked_nested, sorted(ids)))
        job.terminate_tree()
        deadline = time.monotonic() + 5
        while (alive(plain) or alive(plain_nested)) \
                and time.monotonic() < deadline:
            time.sleep(0.05)
        check("winjob breakaway: the tree kill takes the plain children",
              not alive(plain) and not alive(plain_nested))
        check("winjob breakaway: and leaves the ones that broke away running",
              alive(asked) and alive(asked_nested))
    finally:
        _reap_breakaway(job, pids, parent, kill)


def test_winjob_keeps_children_that_ask():
    """Every agent but Codex gets a job without BREAKAWAY_OK. Git Bash asks
    to break away for every program it starts whenever its job allows it,
    so under the old flag a Claude agent's hooks and Bash tool commands
    left the job: a dev server outlived Stop, and a tree kill landing while
    Bash had a program created but not yet resumed left it suspended
    outside any job (two session_hook.py launchers sat frozen for days).
    Here a child that asks stays, nested or not, and dies with the job."""
    if sys.platform != "win32":
        skip('winjob keeps', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import (AgentKind, WinJob, build_spec,
                                    job_allows_breakaway)

    check("winjob keeps: only a Codex agent's job allows breakaway",
          job_allows_breakaway(build_spec(AgentKind.OPENAI, "C"))
          and not job_allows_breakaway(build_spec(AgentKind.CLAUDE, "A"))
          and not job_allows_breakaway(build_spec(AgentKind.GEMINI, "G")))
    alive, kill = _breakaway_procs()
    job = WinJob()
    joined, pids, ids, parent = _breakaway_scenario(job)
    try:
        check("winjob keeps: the parent joins the job", joined)
        check("winjob keeps: the parent started its four children",
              len(pids) == 4, pids)
        if len(pids) != 4:
            return
        check("winjob keeps: every child stays, the ones that asked too",
              set(pids) <= ids, (pids, sorted(ids)))
        job.terminate_tree()
        deadline = time.monotonic() + 5
        while any(alive(p) for p in pids) and time.monotonic() < deadline:
            time.sleep(0.05)
        check("winjob keeps: the tree kill takes all four",
              not any(alive(p) for p in pids))
    finally:
        _reap_breakaway(job, pids, parent, kill)


def test_winjob_keeps_git_bash_children():
    """The real case behind test_winjob_keeps_children_that_ask: Git Bash
    (Cygwin's spawn) asks to break away for every program it starts when
    its job allows that. In an agent's default job the program stays and
    dies with it."""
    if sys.platform != "win32":
        skip('winjob git bash', 'non-Windows: job objects are Windows-only')
        return
    bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files"),
                "Git", "bin", "bash.exe")
    if not bash.is_file():
        skip('winjob git bash', f'no Git Bash at {bash}')
        return
    import ctypes
    from ctypes import wintypes

    from app.process_worker import CREATE_NO_WINDOW, WinJob, describe_pid

    ntdll = ctypes.WinDLL("ntdll")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    alive, kill = _breakaway_procs()
    python = sys.executable.replace("\\", "/")
    job = WinJob()
    # suspended until it is in the job, so bash can't start python first
    proc = subprocess.Popen(
        [str(bash), "-c", f"'{python}' -c 'import time; time.sleep(30)'"],
        creationflags=CREATE_NO_WINDOW | 0x00000004)  # CREATE_SUSPENDED
    pythons = []
    try:
        check("winjob git bash: bash joins the job", job.assign(proc.pid))
        h = k32.OpenProcess(0x0800, False, proc.pid)  # PROCESS_SUSPEND_RESUME
        ntdll.NtResumeProcess(h)
        k32.CloseHandle(h)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            pythons = [p for p in job.process_ids()
                       if describe_pid(p).lower().startswith("python")]
            if pythons:
                break
            time.sleep(0.1)
        check("winjob git bash: the program bash started is in the job",
              bool(pythons), sorted(describe_pid(p)
                                    for p in job.process_ids()))
        job.terminate_tree()
        deadline = time.monotonic() + 5
        while any(alive(p) for p in pythons) and time.monotonic() < deadline:
            time.sleep(0.05)
        check("winjob git bash: and dies with it",
              not any(alive(p) for p in pythons))
    finally:
        job.terminate_tree()
        job.close()
        for p in pythons:
            kill(p)
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


_NPM_CMD_SHIM = r'''@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@openai\codex\bin\codex.js" %*
'''


def _codex_package(root, version, name="@openai/codex"):
    pkg = Path(root, "node_modules", "@openai", "codex")
    (pkg / "bin").mkdir(parents=True, exist_ok=True)
    (pkg / "bin" / "codex.js").write_text("", encoding="utf-8")
    (pkg / "package.json").write_text(
        '{"name": "%s", "version": "%s"}' % (name, version), encoding="utf-8")
    return pkg


def test_codex_version_from_shims():
    """codex_version() reads the version from the npm package a shim runs,
    without starting the CLI, through every shim layout seen in the wild.
    An unknown version must read as (), so AI Hive never passes
    --no-daemon to a Codex that would refuse to start on it."""
    from app import providers

    base = Path(tempfile.mkdtemp(), "with space")
    npm = base / "npm"
    _codex_package(npm, "0.162.1")
    shim = npm / "codex.cmd"
    shim.write_text(_NPM_CMD_SHIM, encoding="utf-8")
    check("codex version: npm's own cmd shim",
          providers.codex_version(str(shim)) == (0, 162, 1),
          providers.codex_version(str(shim)))
    check("codex version: 0.162 takes --no-daemon",
          providers.codex_has_no_daemon(str(shim)))

    local_bin = base / ".local" / "bin"
    local_bin.mkdir(parents=True)
    hand = local_bin / "codex.CMD"
    hand.write_text('@ECHO off\nnode "%s" %%*\n'
                    % (npm / "node_modules/@openai/codex/bin/codex.js"),
                    encoding="utf-8")
    check("codex version: a hand-written shim naming the package by "
          "absolute path", providers.codex_version(str(hand)) == (0, 162, 1),
          providers.codex_version(str(hand)))

    pnpm = base / "pnpm"
    _codex_package(pnpm / "global" / "5", "0.158.0")
    ps1 = pnpm / "codex.ps1"
    ps1.write_text('#!/usr/bin/env pwsh\n$basedir=Split-Path '
                   '$MyInvocation.MyCommand.Definition -Parent\n'
                   '& "$basedir/node$exe"  "$basedir/global/5/node_modules/'
                   '@openai/codex/bin/codex.js" $args\n', encoding="utf-8")
    check("codex version: a ps1 shim relative to $basedir",
          providers.codex_version(str(ps1)) == (0, 158, 0),
          providers.codex_version(str(ps1)))

    appdata_pkg = _codex_package(Path(os.environ["APPDATA"], "npm"), "0.160.1")
    env_shim = base / "envshim" / "codex.cmd"
    env_shim.parent.mkdir()
    env_shim.write_text('@node "%APPDATA%\\npm\\node_modules\\@openai\\codex'
                        '\\bin\\codex.js" %*\n', encoding="utf-8")
    check("codex version: a shim that names the package through %APPDATA%",
          providers.codex_version(str(env_shim)) == (0, 160, 1),
          providers.codex_version(str(env_shim)))
    shutil.rmtree(appdata_pkg.parent.parent)

    vendor = (npm / "node_modules" / "@openai" / "codex" / "node_modules"
              / "@openai" / "codex-win32-x64" / "vendor" / "bin" / "codex.exe")
    vendor.parent.mkdir(parents=True)
    vendor.write_bytes(b"")
    check("codex version: the vendor codex.exe inside the package",
          providers.codex_version(str(vendor)) == (0, 162, 1),
          providers.codex_version(str(vendor)))

    old = base / "old"
    _codex_package(old, "0.155.2")
    (old / "codex.cmd").write_text(_NPM_CMD_SHIM, encoding="utf-8")
    check("codex version: 0.155 has no --no-daemon",
          providers.codex_version(str(old / "codex.cmd")) == (0, 155, 2)
          and not providers.codex_has_no_daemon(str(old / "codex.cmd")))

    alone = base / "standalone" / "codex.exe"
    alone.parent.mkdir()
    alone.write_bytes(b"")
    other = base / "other"
    _codex_package(other, "9.9.9", name="codex-lookalike")
    (other / "codex.cmd").write_text(_NPM_CMD_SHIM, encoding="utf-8")
    empty = base / "empty"
    empty.mkdir()
    saved_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = str(npm)
        check("codex version: a bare name resolves through PATH, as the "
              "launch does", providers.codex_version("codex") == (0, 162, 1),
              providers.codex_version("codex"))
        os.environ["PATH"] = str(empty)
        for label, program in (
                ("a standalone codex.exe", str(alone)),
                ("a package that isn't @openai/codex",
                 str(other / "codex.cmd")),
                ("a bare name not on PATH", "codex"),
                ("a shim that no longer exists", str(base / "gone.cmd")),
                ("nothing at all", "")):
            check(f"codex version: {label} reads as unknown",
                  providers.codex_version(program) == ()
                  and not providers.codex_has_no_daemon(program),
                  providers.codex_version(program))
    finally:
        os.environ["PATH"] = saved_path


def test_codex_launches_without_daemon():
    """A Codex agent launches with --no-daemon when its Codex knows the flag,
    so the session's server, and every command it runs, stays inside the
    agent's Job Object instead of in the daemon all Codex clients share."""
    from app.process_worker import AgentKind, build_spec

    base = Path(tempfile.mkdtemp())
    new, old = base / "new", base / "old"
    _codex_package(new, "0.162.1")
    _codex_package(old, "0.155.2")
    for d in (new, old):
        (d / "codex.cmd").write_text(_NPM_CMD_SHIM, encoding="utf-8")

    def spec_for(root, **kw):
        spec = build_spec(AgentKind.OPENAI, "Codex", cwd=SCRATCH_CWD, **kw)
        if not kw.get("custom_command"):
            spec.program = str(root / "codex.cmd")
        return spec

    spec = spec_for(new, model="gpt-5.6")
    check("codex launch: --no-daemon leads the arguments",
          spec.effective_args() == ["--no-daemon", "--model", "gpt-5.6"],
          spec.effective_args())
    spec.resume, spec.session_id = True, "019a-thread"
    check("codex launch: a resume keeps it before the subcommand",
          spec.effective_args() == ["--no-daemon", "--model", "gpt-5.6",
                                    "resume", "019a-thread"],
          spec.effective_args())
    check("codex launch: an older Codex gets no flag it can't parse",
          "--no-daemon" not in spec_for(old).effective_args(),
          spec_for(old).effective_args())
    custom = spec_for(new, custom_command='"%s" --search' % (new / "codex.cmd"))
    check("codex launch: a custom command stays exactly as typed",
          custom.effective_args() == ["--search"], custom.effective_args())
    remote = spec_for(new, args=["--remote", "ws://127.0.0.1:4500"])
    check("codex launch: --remote, which Codex refuses with --no-daemon, "
          "gets no flag", "--no-daemon" not in remote.effective_args(),
          remote.effective_args())
    twice = spec_for(new, args=["--no-daemon"])
    check("codex launch: a user's own --no-daemon isn't doubled",
          twice.effective_args().count("--no-daemon") == 1,
          twice.effective_args())

    # the real default path, with no program swapped in: build_spec resolves
    # `codex` on PATH and the launch carries the flag
    saved_path = os.environ.get("PATH", "")
    try:
        os.environ["PATH"] = str(new)
        plain = build_spec(AgentKind.OPENAI, "Codex", cwd=SCRATCH_CWD)
        check("codex launch: the built-in provider resolves codex on PATH",
              os.path.normcase(plain.program)
              == os.path.normcase(str(new / "codex.cmd")), plain.program)
        check("codex launch: and launches it with --no-daemon",
              plain.effective_args() == ["--no-daemon"],
              plain.effective_args())
    finally:
        os.environ["PATH"] = saved_path


def test_pty():
    """ConPTY backend through the real card + TerminalView widget."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    import ctypes
    from ctypes import wintypes

    from app import pty_worker
    from app.process_worker import AgentKind, build_spec, describe_pid
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import TerminalCard

    if not HAS_CONPTY:
        check("pty: pywinpty available", False, "pywinpty not installed")
        return

    app = QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_until(pred, timeout_ms=15000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    home = os.path.expanduser("~")
    spec = build_spec(AgentKind.POWERSHELL, "Pty", cwd=home, pty=True)
    check("pty: interactive PowerShell spec is pty", spec.pty)
    agent = TerminalAgent(spec)
    check("pty: agent selects pty backend", agent.is_pty)
    card = TerminalCard(agent)
    card.resize(820, 420)
    card.show()
    check("pty: card hosts a TerminalView (no line console)",
          card.terminal is not None and card.console is None)

    # Hand the spawn the WORST case rather than whatever this suite happens to
    # have been launched with: set the ignore-Ctrl+C ConsoleFlag ON, exactly as
    # a harness that spawns with CREATE_NEW_PROCESS_GROUP does. Children capture
    # it at spawn, so unless PtyWorker.start() clears it, the interrupt check
    # below fails -- which is what makes that check a test of OUR fix and not of
    # the launching terminal.
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.SetConsoleCtrlHandler.argtypes = [ctypes.c_void_p, wintypes.BOOL]
    k32.SetConsoleCtrlHandler.restype = wintypes.BOOL
    k32.SetConsoleCtrlHandler(None, True)
    enable_calls = []
    real_enable = pty_worker.enable_ctrl_c_for_children

    def counting_enable():
        enable_calls.append(1)
        return real_enable()

    pty_worker.enable_ctrl_c_for_children = counting_enable
    try:
        agent.start()
    finally:
        pty_worker.enable_ctrl_c_for_children = real_enable
    check("pty: the spawn clears the inherited ignore-Ctrl+C flag",
          enable_calls == [1], enable_calls)
    check("pty: agent reaches running", wait_until(agent.is_running, 15000))
    check("pty: interactive prompt renders in the grid",
          wait_until(lambda: "PS" in card.terminal.screen_text()
                     and ">" in card.terminal.screen_text(), 20000),
          card.terminal.screen_text()[-160:])

    agent.write("echo ('grid'+'ok')\r")
    check("pty: typed command output appears in the grid",
          wait_until(lambda: "gridok" in
                     card.terminal.screen_text().replace(" ", ""), 12000))

    # Ctrl+C interrupts a running child (a real console control event, not
    # available in line mode). Measured on the PROCESS, never on screen text:
    # the old check compared a screen snapshot taken before the pending output
    # had even flushed against one 2.5s later, counted the English-only string
    # "Reply", and fell back to "PS" in the last 80 chars -- which pyte pads to
    # full width, so that arm could never fire. It reported FAIL on a working
    # interrupt and PASS on a broken one, and was written off as flaky for
    # months while Ctrl+C was genuinely dead (see enable_ctrl_c_for_children).
    def ping_pids():
        return [p for p in agent.worker.job_process_ids()
                if describe_pid(p).lower().startswith("ping")]

    agent.write("ping -t 127.0.0.1\r")
    check("pty: the child command is running",
          wait_until(lambda: bool(ping_pids()), 15000),
          agent.worker.job_process_ids())
    # ...and past its startup. A ping that is in the job but still loading
    # can drop the control event: 8 of 24 runs failed with 8 copies of this
    # test running at once. Three 127.0.0.1s on screen are the typed command,
    # the header and a first reply, in any locale.
    check("pty: the child command printed its first reply",
          wait_until(lambda: card.terminal.screen_text().count("127.0.0.1")
                     >= 3, 15000),
          card.terminal.screen_text()[-160:])
    agent.write("\x03")
    check("pty: Ctrl+C interrupted the running child",
          wait_until(lambda: not ping_pids(), 10000), ping_pids())
    # an INTERRUPT, not a kill: the shell survives its child being stopped
    check("pty: Ctrl+C left the shell alive", agent.is_running())

    def at_prompt():
        lines = [ln.rstrip() for ln in
                 card.terminal.screen_text().splitlines() if ln.strip()]
        return bool(lines) and lines[-1].startswith("PS") \
            and lines[-1].endswith(">")

    # type the next command at a prompt, not into a shell still unwinding
    # the interrupt
    check("pty: the prompt came back after Ctrl+C",
          wait_until(at_prompt, 10000), card.terminal.screen_text()[-160:])

    # background retention while hidden
    # a 4 s stream and a deadline, not an 800 ms stream and one fixed 1.3 s
    # window: with the machine busy, the shell could start the stream late
    # and the window saw no change (2 of 24 runs, 8 copies at once)
    agent.write("1..40 | %{ $_; Start-Sleep -Milliseconds 100 }\r")
    pump(150)
    card.hide()
    before = card.terminal.screen_text()
    check("pty: hidden terminal kept updating",
          wait_until(lambda: card.terminal.screen_text() != before, 6000)
          and not card.isVisible(), before[-240:])

    pid = agent.worker.pid()
    card.detach()
    agent.dispose()
    check("pty: process terminated on dispose",
          wait_until(lambda: not agent.is_running(), 6000))
    pump(300)
    tl = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                        capture_output=True, text=True)
    check("pty: no orphan process after dispose", str(pid) not in tl.stdout)


def test_pty_width_at_launch():
    """Every pty child is SPAWNED at its card's real width, in every workspace.

    The regression this guards, reported as "reopen AI Hive, scroll up in a
    restored conversation and the text is half width": a terminal's column
    count is not a cosmetic detail that can be corrected afterwards. The child
    WRAPS ITS OWN TEXT to whatever the pseudo-console reports, and a `--resume`
    launch dumps the whole past conversation the instant it boots, so lines
    broken for the wrong width stay broken for it forever -- pyte cannot reflow
    them, and neither can `TerminalCard._reproject_on_size`, which only
    re-projects the raw stream the child already hard-wrapped.

    Three separate holes let that happen at launch, all measured on this
    suite's own repro before the fix:

      * `PtyWorker` spawned at DEFAULT_COLS (100) and heard the real width
        ~250ms later, because `TerminalView` debounces its resize by 120ms;
      * `main()` calls `autostart_active_workspace()` the instant `show()`
        returns, where a window restoring MAXIMIZED still reports its
        restore-down geometry (1249x662 measured there, 1536x793 one
        `processEvents` later);
      * a workspace the user has not opened is NEVER laid out by
        `QStackedLayout`, so six of eight agents in a four-workspace hive ran
        their entire resumed conversation at 100 columns and only found out
        the truth when the user first clicked that workspace.

    `MainWindow.settle_layout` closes all three, and the width a child is given
    must then never change again."""
    import json
    import shutil
    import tempfile

    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import pty_worker
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    if not HAS_CONPTY:
        skip('pty width', 'no pywinpty')
        return

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-ptywidth-"))
    store = SessionStore(path=tmp / "session.json")

    # every width the pseudo-console is ever told, and the width each child is
    # SPAWNED at -- the two numbers the bug lived between
    spawned: list[tuple] = []
    widths: dict[str, list] = {}
    orig_resize = pty_worker.PtyWorker.resize
    orig_start = pty_worker.PtyWorker.start

    def traced_resize(self, rows, cols):
        before = (self.rows, self.cols)
        orig_resize(self, rows, cols)
        if (self.rows, self.cols) != before:
            widths.setdefault(id(self), []).append(self.cols)

    def traced_start(self):
        spawned.append((self.spec.name, self.cols))
        widths.setdefault(id(self), []).append(self.cols)
        return orig_start(self)

    pty_worker.PtyWorker.resize = traced_resize
    pty_worker.PtyWorker.start = traced_start
    try:
        # -- session 1: four workspaces, two pty agents each, all running ----
        win = create_main_window(store)
        win.show()
        pump(400)
        mgr = win.manager
        wss = [mgr.workspaces[0]]
        for nm in ("Two", "Three", "Four"):
            wss.append(mgr.create_workspace(nm, str(tmp)))
        for ws in wss:
            while len(ws.agents) < 2:
                mgr.add_terminal(ws.id, build_spec(
                    AgentKind.POWERSHELL, "A%d" % (len(ws.agents) + 1),
                    cwd=str(tmp), pty=True), autostart=False)
            for a in ws.agents:      # all eight RUNNING, so all eight restore
                if not a.is_running():
                    a.start()
        mgr.set_active(wss[0].id)
        pump(1200)
        win.close()
        pump(400)

        # a maximized window whose restore-down geometry is much smaller: the
        # shape that makes show() report a width the card will not keep
        data = json.loads((tmp / "session.json").read_text(encoding="utf-8"))
        data.setdefault("ui", {})["window"] = {"w": 900, "h": 600,
                                               "maximized": True}
        (tmp / "session.json").write_text(json.dumps(data), encoding="utf-8")

        spawned.clear()
        widths.clear()

        # -- session 2: main()'s exact order, no event loop in between -------
        win2 = create_main_window(SessionStore(path=tmp / "session.json"))
        win2.show()
        win2.autostart_active_workspace()
        # the other workspaces' agents follow from the launch queue, one
        # per tick
        deadline = time.monotonic() + 10
        while len(spawned) < 8 and time.monotonic() < deadline:
            pump(100)
        pump(1000)

        default_cols = pty_worker.DEFAULT_COLS
        check("pty width: every restored child was actually started",
              len(spawned) == 8, spawned)
        check("pty width: none was spawned at the placeholder default",
              all(cols != default_cols for _n, cols in spawned), spawned)

        active = win2.manager.active_id
        check("pty width: the on-screen workspace is honest",
              all(c.terminal.screen.columns == c.agent.worker.cols
                  for c in win2._pages[active].cards),
              [(c.terminal.screen.columns, c.agent.worker.cols)
               for c in win2._pages[active].cards])
        # the half of the bug that never corrected itself: Qt lays out the
        # CURRENT page only, so these six used to sit at DEFAULT_COLS forever
        hidden = [c for ws in win2.manager.workspaces if ws.id != active
                  for c in win2._pages[ws.id].cards]
        check("pty width: ...and so is every workspace still off screen",
              hidden and all(c.terminal.screen.columns == c.agent.worker.cols
                             and c.terminal.screen.columns != default_cols
                             for c in hidden),
              [(c.agent.spec.name, c.terminal.screen.columns,
                c.agent.worker.cols) for c in hidden])
        check("pty width: no child was ever told two different widths",
              all(len(set(seen)) == 1 for seen in widths.values()),
              {k: v for k, v in widths.items() if len(set(v)) > 1})

        # -- and opening a workspace must not move its width ----------------
        others = [ws for ws in win2.manager.workspaces if ws.id != active]
        before = [(c.agent.id, c.agent.worker.cols)
                  for c in win2._pages[others[0].id].cards]
        win2.manager.set_active(others[0].id)
        pump(600)
        after = [(c.agent.id, c.agent.worker.cols)
                 for c in win2._pages[others[0].id].cards]
        check("pty width: opening a workspace does not re-wrap its terminals",
              before == after, (before, after))

        win2.close()
        pump(400)
    finally:
        pty_worker.PtyWorker.resize = orig_resize
        pty_worker.PtyWorker.start = orig_start
        shutil.rmtree(tmp, ignore_errors=True)


_CONSOLE_PROBE = r'''
import ctypes, sys
from ctypes import wintypes
sys.path.insert(0, sys.argv[1])
from app.pty_worker import ensure_windowless_console
k32 = ctypes.windll.kernel32
k32.GetConsoleWindow.restype = wintypes.HWND
had = k32.GetConsoleWindow()
done = ensure_windowless_console()
hwnd = k32.GetConsoleWindow()
shown = bool(hwnd) and bool(ctypes.windll.user32.IsWindowVisible(hwnd))
from winpty import PtyProcess
p = PtyProcess.spawn("cmd.exe /c exit 0")
after = k32.GetConsoleWindow()
again = ensure_windowless_console()
with open(sys.argv[2], "w") as fh:
    print(had is None, done, shown, after == hwnd, again, sep="|", file=fh)
'''


def test_windowless_console():
    """A pythonw AI Hive gets a console with no window (or a hidden one on
    older Windows) before its window shows, so pywinpty's first spawn never
    allocates one. That console used to take the foreground from AI Hive,
    and F11 then went to conhost and opened a black window over the app."""
    if sys.platform != "win32":
        skip('windowless console', 'non-Windows: no consoles to allocate')
        return
    root = str(Path(__file__).resolve().parents[2])
    # pythonw, the production launch: a GUI-subsystem process with no
    # console. A venv python.exe is a redirector that starts the real one
    # as a child, which gets a console of its own whatever flags we pass.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        skip('windowless console', f'no pythonw.exe beside {sys.executable}')
        return
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-console-"))
    try:
        result = tmp / "out.txt"
        res = subprocess.run(
            [str(pythonw), "-c", _CONSOLE_PROBE, root, str(result)],
            capture_output=True, text=True, timeout=60, cwd=SCRATCH_CWD)
        out = (result.read_text().strip().split("|") if result.is_file()
               else [])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    check("console: the probe ran", res.returncode == 0 and len(out) == 5,
          (res.returncode, out, res.stderr[-800:]))
    if len(out) != 5:
        return
    had_none, done, shown, same, again = out
    check("console: a pythonw launch starts with no console",
          had_none == "True", out)
    check("console: it gets a windowless or hidden one",
          done in ("windowless", "hidden"), out)
    check("console: nothing on screen", shown == "False", out)
    check("console: the first pty spawn allocates no console of its own",
          same == "True", out)
    check("console: a second call leaves the console alone",
          again == "existing", out)
