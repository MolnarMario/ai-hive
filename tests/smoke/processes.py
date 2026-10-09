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


def test_winjob_process_count():
    """WinJob.process_count() reports the live process count for a job -- the
    detection primitive behind poll_bg_shell(). Windows-only; skipped
    everywhere else since job objects don't exist there."""
    if sys.platform != "win32":
        skip('winjob', 'non-Windows: job objects are Windows-only')
        return
    from app.process_worker import WinJob

    job = WinJob()
    check("winjob: a fresh job object gets a real handle on Windows",
          job._handle is not None)
    check("winjob: process_count on an empty (unassigned) job is 0",
          job.process_count() == 0)

    procs = [subprocess.Popen([sys.executable, "-c",
                               "import time; time.sleep(5)"])
             for _ in range(2)]
    try:
        for p in procs:
            check(f"winjob: assign succeeds for a live child (pid {p.pid})",
                  job.assign(p.pid))
        check("winjob: process_count reflects both assigned processes",
              job.process_count() == 2, job.process_count())
    finally:
        job.terminate_tree()
        for p in procs:
            try:
                p.wait(timeout=5)
            except Exception:
                p.kill()
        job.close()
    check("winjob: process_count after close is 0 (no handle)",
          job.process_count() == 0)


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
    orig_count = agent.worker.job_process_count
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: count[0]
    try:
        agent.poll_bg_shell()   # learns the baseline (1)
        check("bg shell: the baseline alone never flags",
              not agent.is_bg_shell_busy())

        count[0] = 0   # a failed/unsupported query must never flap state
        agent.poll_bg_shell()
        check("bg shell: a count<=0 (query failed) leaves the state alone",
              not agent.is_bg_shell_busy())
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
        agent.worker.job_process_count = orig_count


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
    orig_count = agent.worker.job_process_count
    count = [1]
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: count[0]
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
        agent.worker.job_process_count = orig_count


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
    orig_count = agent.worker.job_process_count
    orig_ids = agent.worker.job_process_ids
    orig_pid = agent.worker.pid
    orig_kill = agent.worker.kill_extra_processes
    pids = [100, 101]           # root(100) + the mcp bridge(101), the baseline
    kill_calls = []
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: len(pids)
    agent.worker.job_process_ids = lambda: list(pids)
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
        agent.worker.job_process_count = orig_count
        agent.worker.job_process_ids = orig_ids
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
    orig_count = agent.worker.job_process_count
    orig_ids = agent.worker.job_process_ids
    orig_pid = agent.worker.pid
    orig_kill_pid = agent.worker.kill_pid
    pids = [100, 101]   # root(100) + mcp bridge(101): the baseline
    terminal_agent_mod.time.time = lambda: fake_now[0]
    agent.worker.job_process_count = lambda: len(pids)
    agent.worker.job_process_ids = lambda: list(pids)
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
        agent.worker.job_process_count = orig_count
        agent.worker.job_process_ids = orig_ids
        agent.worker.pid = orig_pid
        agent.worker.kill_pid = orig_kill_pid


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
        pump(2500)

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
