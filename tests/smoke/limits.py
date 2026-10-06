"""Plan limits: detecting the limit banner (Claude and Gemini), the ledger,
blocked stats, reset clocks and auto-continue."""

import os
import shutil
import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check, skip


def _as_cli_wrote(rec):
    """`rec`, with the fields claude.exe ALWAYS puts on a 429 cut-off record
    when it is an assistant record whose text is a limit banner: `error:
    "rate_limit"`, `isApiErrorMessage: true`, model `<synthetic>` (read off
    every real one on this machine). The transcript reader now requires them,
    because a banner WITHOUT them is an agent writing those words in its own
    reply, so a fixture that leaves them off describes prose, not a cut-off."""
    from app import limit_banner as _lb
    if rec.get("type") != "assistant" or "error" in rec:
        return rec
    content = (rec.get("message") or {}).get("content")
    text = (content if isinstance(content, str) else " ".join(
        b.get("text", "") for b in (content or []) if isinstance(b, dict)))
    if not _lb.banner_line(text):
        return rec
    rec = dict(rec, error="rate_limit", isApiErrorMessage=True)
    rec["message"] = dict(rec.get("message") or {}, model="<synthetic>")
    return rec


def test_limit_blocked_workspace_stats():
    """workspace_stats()'s limit_blocked count -- and the workspaceStatsChanged
    signal it rides on -- must update LIVE on both edges (an agent gets cut off
    AND an agent resumes), not just the rising one: the sidebar's hourglass
    badge is signal-driven, not polled, exactly like the "?" badge. Separately,
    agentLimitBlocked (which feeds the cut-off ledger/audit trail) stays
    rising-edge-only -- it records the cut-off itself, not its resolution."""
    from PySide6.QtWidgets import QApplication
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limitstats-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("LimitStats", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Stuck",
                                               cwd=str(tmp)), autostart=False)
    check("limit stats: nobody blocked yet",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 0)

    stats_events = []
    blocked_events = []
    mgr.workspaceStatsChanged.connect(
        lambda wid, s: stats_events.append(dict(s)) if wid == ws.id else None)
    mgr.agentLimitBlocked.connect(lambda wid, aid: blocked_events.append((wid, aid)))

    agent.mark_limit_blocked(None, from_startup=False)
    check("limit stats: count is 1 once an agent is latched",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 1)
    check("limit stats: workspaceStatsChanged fired live with the new count",
          stats_events and stats_events[-1]["limit_blocked"] == 1, stats_events)
    check("limit stats: agentLimitBlocked announced the rising edge",
          blocked_events == [(ws.id, agent.id)], blocked_events)

    agent.clear_limit_block()
    check("limit stats: count drops back to 0 the instant the agent resumes",
          mgr.workspace_stats(ws.id)["limit_blocked"] == 0)
    check("limit stats: workspaceStatsChanged fired again on the falling "
          "edge too (the sidebar badge must not be stuck on)",
          stats_events[-1]["limit_blocked"] == 0, stats_events)
    check("limit stats: agentLimitBlocked does NOT fire on the falling edge "
          "(it feeds the cut-off ledger, not the resolution)",
          blocked_events == [(ws.id, agent.id)], blocked_events)


def test_limit_blocked_live_ui():
    """The card header's hourglass and the sidebar's inline agent-row hourglass
    both say "stopped by the usage limit" -- and both must clear the instant
    the agent resumes (auto-continue or a manual restart), not sit there
    stale. The card is signal-driven (limit_blocked_changed), the sidebar
    AgentRow is polled (refresh() reads is_limit_blocked() directly) -- this
    exercises both paths end to end through the real clear_limit_block()."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec
    from app.widgets.terminal_card import TerminalCard
    from app.widgets.sidebar import AgentRow

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Stuck", cwd=SCRATCH_CWD))
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(60)
    check("limit UI: hourglass hidden before any cut-off",
          not card.limit_mark.isVisible())

    a.mark_limit_blocked(None, from_startup=False)
    pump(30)
    check("limit UI: card hourglass shows once the agent latches blocked",
          card.limit_mark.isVisible())

    row = AgentRow("w1", a)
    check("limit UI: a freshly built sidebar agent row also shows it",
          not row.limit_mark.isHidden())

    a.clear_limit_block()
    pump(30)
    check("limit UI: card hourglass disappears LIVE on resume (the bug this "
          "fixes: clear_limit_block used to never emit the falling edge)",
          not card.limit_mark.isVisible())
    row.refresh(a)
    check("limit UI: sidebar agent row also clears on its next poll",
          row.limit_mark.isHidden())

    card.detach(); card.close()
    row.deleteLater()
    a.deleteLater()


def test_limit_ledger():
    """The durable record of cut-offs: who was stopped, when, and how it ended.

    Everything else about a cut-off is transient by design, so this file is the
    only thing that can answer "what happened last night" after a restart — and
    it is what decides, on the next launch, whether a cut-off still needs
    acting on."""
    import time as _time
    from app import limit_ledger as L

    tmp = str(Path(tempfile.mkdtemp(prefix="ai-hive-ledger-")))
    now = _time.time()

    check("ledger: an absent file reads as no records, never raises",
          L.read_all(tmp) == [] and L.open_cut_offs(tmp) == [])

    rec = L.record_cut_off(tmp, cwd="C:/proj", session_id="sid-a", at=now,
                           reset_at=now + 3600, ws_id="w1", ws_name="Hive",
                           agent_name="Agent 5", task="fix the parser",
                           window="session", banner="You've hit...",
                           source="live")
    stored = L.read_all(tmp)[0]
    check("ledger: a cut-off round-trips with workspace, agent, task and window",
          stored["ws_name"] == "Hive" and stored["agent_name"] == "Agent 5"
          and stored["task"] == "fix the parser"
          and stored["window"] == "session")
    check("ledger: it carries a readable LOCAL date and time, not just an epoch",
          stored["at_local"][:2] == "20" and ":" in stored["at_local"]
          and stored["reset_local"])

    # THE identity problem: TerminalAgent.id is a fresh uuid on every load, so
    # a cut-off has to be named by things that survive the restart this file
    # exists for -- the folder, the pinned conversation, and which window
    # stopped it.
    same = L.key_of("C:/proj", "sid-a", now + 3600, now + 5)
    check("ledger: the key is stable across runs (agent ids are not)",
          same == rec["key"])
    later = L.key_of("C:/proj", "sid-a", now + 3600 + 5 * 3600, now + 5 * 3600)
    check("ledger: a LATER cut-off of the same conversation is a new record",
          later != rec["key"])
    clockless = L.key_of("C:/proj", "sid-a", 0.0, now)
    check("ledger: a banner with no clock still yields a usable key",
          clockless[2] and clockless != rec["key"])

    check("ledger: a fresh cut-off is open", len(L.open_cut_offs(tmp)) == 1)
    L.record_cut_off(tmp, cwd="C:/proj", session_id="sid-a", at=now + 3,
                     reset_at=now + 3600, source="transcript")
    check("ledger: the same cut-off filed twice (seen live, then read off "
          "disk) is ONE open record", len(L.open_cut_offs(tmp)) == 1)
    L.record_outcome(tmp, rec["key"], L.RESUMED, tries=1)
    check("ledger: an outcome closes it for good",
          L.open_cut_offs(tmp) == []
          and tuple(rec["key"]) in L.closed_keys(L.read_all(tmp)))

    # written at the moment the app may be killed, so a half-line is expected
    with open(L.path_for(tmp), "a", encoding="utf-8") as fh:
        fh.write('{"event":"cut_off","at":  \n')
    check("ledger: a torn last line is skipped, not fatal",
          len(L.read_all(tmp)) == 3)

    # --- grouping: which cut-offs belong to the MOST RECENT window ----------
    def cut(at, reset):
        return {"event": L.CUT_OFF, "at": at, "reset_at": reset}

    old, new1, new2 = cut(now - 6 * 3600, now - 3600), \
        cut(now - 600, now + 1800), cut(now - 900, now + 1800 + 20)
    group = L.latest_window([old, new1, new2])
    check("ledger: agents stopped by one window group by their shared reset",
          group == [new1, new2] or group == [new2, new1])
    check("ledger: an earlier window is not in the group", old not in group)
    check("ledger: no cut-offs means no group", L.latest_window([]) == [])
    # a clockless cut-off can still be the newest one, so the anchor is WHEN it
    # happened -- never the reset it failed to state
    bare_new, bare_old = cut(now, 0.0), cut(now - 20 * 3600, 0.0)
    check("ledger: clockless cut-offs group by how far apart they were",
          L.latest_window([bare_old, bare_new]) == [bare_new])


def test_auto_continue_on_limit_reset():
    """Runs the body below with the app's own delays shrunk. The body pumps
    past each timer, so at the shipped values (2 s stagger, 3 s phantom check,
    400 ms Esc beat) it spent ~36 s asleep. The ORDER of events is what it
    checks, and that survives any scale; the CR's 350 ms beat stays real."""
    from app.widgets import main_window as mw
    saved = (mw.AUTO_CONTINUE_STAGGER_MS, mw.LIMIT_PHANTOM_CHECK_MS,
             mw.AUTO_CONTINUE_ESC_MS)
    mw.AUTO_CONTINUE_STAGGER_MS = 200
    mw.LIMIT_PHANTOM_CHECK_MS = 150
    mw.AUTO_CONTINUE_ESC_MS = 100
    try:
        _auto_continue_on_limit_reset_body()
    finally:
        (mw.AUTO_CONTINUE_STAGGER_MS, mw.LIMIT_PHANTOM_CHECK_MS,
         mw.AUTO_CONTINUE_ESC_MS) = saved


def _auto_continue_on_limit_reset_body():
    """When the plan limit resets, the agents it CUT OFF go back to work by
    themselves: Esc to close the limit's options menu, then "Continue".

    The point is unattended overnight recovery — and, because the first message
    after a window expires is what starts the next one, resuming at 4am also
    rolls the 5-hour clock over before morning. Guards matter as much as the
    action: the account-wide reading that fires the edge cannot say WHICH agents
    were mid-turn, so anything not parked on the limit banner is left alone."""
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu
    from app import limit_ledger
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.widgets.main_window import AUTO_CONTINUE_TEXT
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()

    # long enough to cover the Esc->type beat (shrunk to 100 ms by the
    # wrapper) plus the delayed submit CR (350 ms, never shrunk), with margin
    AUTO_CONTINUE_SETTLE_MS = 900

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    BANNER = ("You've hit your session limit \xb7 resets 4:40am "
              "(Europe/Bucharest)\n")
    # the NEXT window's cut-off. Its clock differs because successive 5-hour
    # windows never end at the same wall time -- which is what makes the banner
    # line the identity of a particular cut-off (see _scrape_limit).
    NEXT_BANNER = ("You've hit your session limit \xb7 resets 9:40am "
                   "(Europe/Bucharest)\n")
    # what Claude actually parks the agent on. Unlike the banner (ordinary
    # scrollback, which the rolling tail evicts) this MENU stays up for as long
    # as the agent is stuck, so it is the primary live signal and the "did it
    # resume?" test.
    MENU = ("What do you want to do?\n"
            "> 1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n"
            "Enter to confirm \xb7 Esc to cancel\n")
    PARKED = BANNER + MENU

    # --- the per-agent "I was cut off" scrape -------------------------------
    def mk(name="Coder", provider="claude", pty=True):
        spec = build_spec(AgentKind.CLAUDE if provider == "claude"
                          else AgentKind.CMD, name, cwd=SCRATCH_CWD, pty=pty)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d), True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    writes: dict = {}

    def settle(agent, screen):
        """Feed a settled screen through the real idle-timer path."""
        agent._screen_tail = screen
        agent._on_idle_timeout()

    a = mk()
    settle(a, "")
    check("auto-continue: a clean screen is not limit-blocked",
          not a.is_limit_blocked())
    settle(a, "Approaching session limit \xb7 resets 4:40am\n")
    check("auto-continue: an 'Approaching' warning is NOT a cut-off",
          not a.is_limit_blocked())
    settle(a, "You've used 62% of your session limit \xb7 resets 4:40am\n")
    check("auto-continue: a 'You've used N%' warning is NOT a cut-off",
          not a.is_limit_blocked())
    settle(a, BANNER)
    check("auto-continue: the exhausted banner IS a cut-off",
          a.is_limit_blocked())
    check("auto-continue: the banner's own reset time is latched with it",
          a.limit_resets_at() is not None)

    # claude.exe 2.1.235+ replaced the "You've hit your session limit" banner
    # with "Usage limit reached (c) continuing automatically at ..." and no
    # longer shows the interactive menu by default -- read off a real
    # transcript's injected system/informational record, not guessed (see
    # limit_banner's module docstring). A latch has to survive on the banner
    # ALONE here, with no menu at all, which is exactly the case that was
    # silently falling through before this wording was recognised.
    NEW_BANNER = ("Usage limit reached \xb7 continuing automatically at "
                  "10:10pm \xb7 esc or type to cancel\n")
    new_wording = mk("NewWording")
    settle(new_wording, NEW_BANNER)
    check("auto-continue: the current 'Usage limit reached' wording IS a "
          "cut-off, with no menu on screen at all",
          new_wording.is_limit_blocked())
    check("auto-continue: its own 'continuing automatically at ...' clock is "
          "latched with it",
          new_wording.limit_resets_at() is not None)
    # the Fast-mode / spend-limit family use similar words but are a DIFFERENT
    # feature (a per-request throttle, not the account-wide cut-off) and must
    # not falsely arm a resume for something that never stopped the agent.
    fast_mode = mk("FastMode")
    settle(fast_mode, "Fast mode disabled \xb7 usage credit limit reached\n")
    check("auto-continue: 'usage credit limit reached' (Fast mode, a "
          "different feature) is NOT the account-wide cut-off",
          not fast_mode.is_limit_blocked())

    # the menu alone is enough — it is what survives on screen when the banner
    # above it has scrolled out of the rolling tail
    menu_only = mk("MenuOnly")
    settle(menu_only, MENU)
    check("auto-continue: the parked-on menu alone IS a cut-off",
          menu_only.is_limit_blocked())

    # --- WHY a banner did not latch has to be on the record -----------------
    # Everything after a latch is audited; the decision NOT to latch was not,
    # and that is the one that strands work. An agent was found parked on a
    # spent limit with no trace anywhere of why the scrape had passed over it,
    # and the cause could only be narrowed by elimination. These lines close
    # that gap -- while staying bounded, since _scrape_limit runs on EVERY
    # output burst.
    skips: list = []
    quiet = mk("Quiet")
    quiet.audit = skips.append
    quiet._prompt_ready = False
    settle(quiet, BANNER)          # a banner during a resume replay
    check("no-latch: a banner ignored as replay says so in the log",
          any("NO-LATCH" in m and "Quiet" in m and "prompt not ready" in m
              for m in skips), skips)
    check("no-latch: ...and it did not latch", not quiet.is_limit_blocked())
    before = len(skips)
    for _ in range(20):            # the same frame repainted many times over
        settle(quiet, BANNER)
    check("no-latch: an unchanged rejection is recorded ONCE, not per burst",
          len(skips) == before, skips[before:])

    # the echo guard is the other silent path: a banner still on screen after
    # a resume, with the menu torn down
    echo = mk("Echo")
    echo.audit = skips.append
    settle(echo, BANNER)                       # first sighting latches
    echo.clear_limit_block()                   # ...resumed off it
    n = len(skips)
    settle(echo, BANNER)                       # same line, no menu -> echo
    check("no-latch: a suppressed banner echo names the reason",
          any("NO-LATCH" in m and "Echo" in m and "already produced a latch"
              in m for m in skips[n:]), skips[n:])
    check("no-latch: ...and the echo still does not re-latch",
          not echo.is_limit_blocked())

    # and it must stay silent when there is nothing to say
    mute: list = []
    ordinary = mk("Ordinary")
    ordinary.audit = mute.append
    settle(ordinary, "building the index...\ndone in 4.1s\n")
    latched = mk("Latched")
    latched.audit = mute.append
    settle(latched, BANNER)
    check("no-latch: ordinary output and a successful latch log nothing",
          mute == [] and latched.is_limit_blocked(), mute)

    # An agent WRITING ABOUT the limit is not stopped by it. Observed live: an
    # agent working on this feature quoted the banner in its own output and was
    # armed for a resume it never needed. A real banner is a short line of its
    # own; prose that mentions it is not.
    talker = mk("Talker")
    settle(talker,
           "The sign an agent shows looks like this: " + BANNER.strip()
           + " -- and that phrase is what we match against the screen "
             "buffer to work out that it stopped.\n")
    check("auto-continue: an agent QUOTING the banner in prose is not a "
          "cut-off", not talker.is_limit_blocked())

    # a cut-off with no clock anywhere must still be resumable -- `unknown`
    # once stranded an agent for five hours
    noclock = mk("NoClock")
    settle(noclock, MENU)                       # the menu carries no time
    check("auto-continue: a menu-only cut-off has no reset time of its own",
          noclock.limit_resets_at() is None)
    noclock.set_limit_reset(now + 1800)         # ...supplied by the account
    check("auto-continue: a reset time can be supplied from the account "
          "reading", noclock.limit_resets_at() == now + 1800)
    noclock.set_limit_reset(now + 9999)
    check("auto-continue: a known reset time is never overwritten",
          noclock.limit_resets_at() == now + 1800)

    # THE REGRESSION that cost a night's work: the banner is latched when it is
    # DRAWN, because _screen_tail is a rolling buffer — by reset time the agent
    # has idled for hours and its own redraws have evicted the banner. A
    # re-scrape at that point sees only the bottom of a frame and resumes
    # nobody.
    settle(a, "\xe2\x94\x82 > \xe2\x94\x82\n  ? for shortcuts\n")
    check("auto-continue: the latch SURVIVES the banner scrolling out of the "
          "rolling screen tail", a.is_limit_blocked())
    # the card header's hourglass and the sidebar's blocked-count badge are
    # both signal-driven, not polled -- clearing the latch must emit the
    # FALLING edge or the marker sits there forever after a real resume
    edge_events = []
    a.limit_blocked_changed.connect(edge_events.append)
    a.clear_limit_block()
    check("auto-continue: clearing the latch forgets the reset time too",
          not a.is_limit_blocked() and a.limit_resets_at() is None)
    check("auto-continue: clearing the latch emits limit_blocked_changed(False)",
          edge_events == [False], edge_events)
    edge_events.clear()
    a.clear_limit_block()   # start()/restart() call this unconditionally
    check("auto-continue: clearing an already-clear latch stays silent "
          "(never blocked -> nothing changed)", edge_events == [], edge_events)

    # a --resume replay redraws the OLD conversation, banner and all; that is
    # history, not a live cut-off, and latching it would schedule a phantom
    # Continue. The input-box footer ends the replay, so pre-prompt output is
    # excluded.
    replay = mk()
    replay._prompt_ready = False
    replay._on_pty_output("pty", BANNER)
    check("auto-continue: a banner replayed before the prompt is ready is "
          "history, not a cut-off", not replay.is_limit_blocked())
    replay._prompt_ready = True
    replay._on_pty_output("pty", BANNER)
    check("auto-continue: the same banner once live DOES latch",
          replay.is_limit_blocked())

    # Prompt readiness must accept the WHOLE rotating footer-hint family, not
    # just "? for shortcuts". Keying on that one member left a restored agent
    # permanently "not ready" -- the watchdog logged WAIT every minute and
    # never nudged it (verified live).
    for hint in ("? for shortcuts",
                 "auto mode on(shift+tab to cycle) \xb7 ctrl+t to show tasks "
                 "\xb7 ← for agents"):
        r = mk("Ready")
        r._prompt_ready = False
        r._on_pty_output("pty", "\x1b[?2004h" + hint + "\n")
        check(f"auto-continue: footer hint {hint.split(chr(183))[0][:24]!r} "
              f"marks the prompt ready", r.prompt_ready())
    notready = mk("NotReady")
    notready._prompt_ready = False
    notready._on_pty_output("pty", "Do you trust the files in this folder?\n")
    check("auto-continue: the trust dialog is NOT mistaken for a live prompt",
          not notready.prompt_ready())

    # the latch must not wait for the idle-timer settle: the banner arrives
    # right after the user hits Enter, which is exactly when _mark_busy treats
    # output as keystroke echo and never arms that timer (a live miss)
    burst = mk()
    burst.write("hi")                 # stamps _last_input_ts -> echo window
    burst._on_pty_output("pty", BANNER)
    check("auto-continue: latched straight off the output burst, with no "
          "idle-timer settle", burst.is_limit_blocked())

    curly = mk()
    settle(curly, "You’ve hit your weekly limit \xb7 resets 3am\n")
    check("auto-continue: curly apostrophe + weekly window also detected",
          curly.is_limit_blocked())
    non_claude = mk(name="Shell", provider="cmd", pty=True)
    settle(non_claude, BANNER)
    check("auto-continue: a non-Claude agent is never limit-blocked",
          not non_claude.is_limit_blocked())

    # --- the banner's clock -> the next occurrence of that wall time ---------
    from app.terminal_agent import parse_reset_clock
    base = _time.mktime((2026, 8, 2, 23, 50, 0, 0, 0, -1))   # 23:50 local
    at = parse_reset_clock("\xb7 resets 4:40am", base)
    lt = _time.localtime(at)
    check("auto-continue: a small-hours reset read late at night rolls over "
          "to tomorrow",
          (lt.tm_hour, lt.tm_min) == (4, 40) and at > base
          and at - base < 6 * 3600)
    noon = _time.mktime((2026, 8, 2, 12, 0, 0, 0, 0, -1))
    lt2 = _time.localtime(parse_reset_clock("resets 8:30pm", noon))
    check("auto-continue: pm is read as afternoon, same day",
          (lt2.tm_hour, lt2.tm_min) == (20, 30))
    lt3 = _time.localtime(parse_reset_clock("resets 12:15am", noon))
    check("auto-continue: 12:15am is after midnight, not noon",
          (lt3.tm_hour, lt3.tm_min) == (0, 15))
    check("auto-continue: a banner with no time yields no reset",
          parse_reset_clock("You've hit your session limit") is None)

    # --- nudge() must not disturb any PERSISTED metadata --------------------
    n = mk()
    n.set_task("write the parser")
    before = (n.current_task, n.assignment, n.spec.role)
    check("auto-continue: nudge() delivers when the prompt is ready",
          n.nudge("Continue") is True)
    check("auto-continue: nudge() leaves task/assignment/role untouched",
          (n.current_task, n.assignment, n.spec.role) == before)
    check("auto-continue: nudge() typed the text",
          "Continue" in "".join(writes.get(id(n.worker), [])))
    check("auto-continue: nudge() does not stamp the user-input clock "
          "(resumed work still pulses the sidebar)",
          n._last_input_ts == 0.0)
    n._prompt_ready = False
    check("auto-continue: nudge() refuses when the TUI isn't prompt-ready",
          n.nudge("Continue") is False)

    # --- the wiring: planLimitCleared -> resume the blocked ones ------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-autocont-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show(); pump(50)
    mgr = win.manager
    ws = mgr.workspaces[0]

    cut_off, busy, fine = mk("CutOff"), mk("Busy"), mk("Fine")
    no_menu, self_cont = mk("NoMenu"), mk("SelfContinue")
    settle(cut_off, PARKED)
    settle(busy, BANNER)
    busy._busy = True                    # already moving again
    settle(fine, "all done\n")           # was never cut off
    settle(no_menu, BANNER)
    settle(self_cont, NEW_BANNER)
    ws.agents.extend([cut_off, busy, fine, no_menu, self_cont])

    from app.widgets.main_window import AUTO_CONTINUE_STAGGER_MS as _STAGGER
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS + 2 * _STAGGER)
    sent = lambda ag: "".join(writes.get(id(ag.worker), []))
    check("auto-continue: the cut-off agent parked on the old menu got Esc "
          "then Continue",
          sent(cut_off).startswith("\x1b") and "Continue" in sent(cut_off))
    # Current CLIs draw no menu, and there an Esc does harm: under "continuing
    # automatically at ... \xb7 esc or type to cancel" it CANCELS Claude's own
    # auto-continue, and on a plain prompt it wipes half-typed input.
    check("auto-continue: with no menu on screen, no Esc is sent, only the "
          "Continue", "\x1b" not in sent(no_menu)
          and "Continue" in sent(no_menu), repr(sent(no_menu)[:40]))
    check("auto-continue: an Esc never cancels Claude's own 'continuing "
          "automatically' timer", "\x1b" not in sent(self_cont),
          repr(sent(self_cont)[:40]))
    check("auto-continue: an agent that is busy again is left alone",
          sent(busy) == "")
    check("auto-continue: an agent that was never cut off is left alone",
          sent(fine) == "")
    check("auto-continue: the card carries an audit notice",
          any("auto-continued" in t for _s, t in cut_off.log))

    # the toggle actually gates it
    writes.clear()
    win._on_auto_continue(False)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: nothing is typed while the toggle is off",
          sent(cut_off) == "")
    payload_ui = win._session_payload()["ui"]
    check("auto-continue: preference persisted under ui.auto_continue",
          payload_ui["auto_continue"] is False)

    # the real trigger is the plan-limit falling edge, not a timer
    writes.clear()
    win._on_auto_continue(True)
    cut_off.clear_limit_block()    # a FRESH cut-off in the next window
    settle(cut_off, NEXT_BANNER)   # ...which states the next window's clock
    win._plan_blocked = True
    win._on_usage_ready(cu.Usage(
        limits=(cu.Limit(key="five_hour", label=cu._LABELS["five_hour"],
                         short=cu._SHORT["five_hour"], percent=12.0,
                         resets_at=now + 4700),),
        fetched_at=now, plan="pro"))
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: the planLimitCleared edge resumes cut-off agents",
          "Continue" in sent(cut_off))
    # The latch is NOT dropped on the nudge itself: a resume can land while the
    # window is still shut, and clearing here burned the only attempt and left
    # the agent parked for good. It clears on VERIFICATION instead.
    check("auto-continue: the latch survives the nudge, pending verification",
          cut_off.is_limit_blocked())
    check("auto-continue: a nudged agent is not re-nudged a minute later",
          not cut_off.limit_retry_ready(300, 4))
    cut_off._screen_tail = PARKED           # menu still up: it didn't take
    check("auto-continue: still parked on the menu -> stays latched for a retry",
          cut_off.recheck_limit() is True)
    # The banner LINGERS in the tail after a successful resume, so verification
    # must key on the menu alone — using the banner would report a false
    # "still blocked" forever and burn every retry.
    cut_off._screen_tail = BANNER + "\nWorking on it...\n  ? for shortcuts\n"
    check("auto-continue: menu gone -> resumed, even with the banner still in "
          "the scrollback",
          cut_off.recheck_limit() is False and not cut_off.is_limit_blocked())
    check("auto-continue: attempts reset with the latch",
          cut_off.limit_attempts() == 0)

    # --- the network-free watchdog: the banner's own reset time -------------
    # The API edge is NOT enough on its own. It only fires if this same process
    # also saw the blocked state first, and the endpoint 429s intermittently --
    # a missed edge silently costs a whole night, which is what happened live.
    writes.clear()
    late = mk("Late")
    settle(late, BANNER)
    ws.agents.append(late)
    late._limit_resets_at = now + 3600          # not due yet
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an agent whose reset is still in the future waits",
          sent(late) == "" and late.is_limit_blocked())
    # a reset read OFF THE SCREEN gets slack before it is acted on: the banner
    # names a minute, not an instant, and a nudge into a window that is still
    # shut spends one of very few retries
    from app.widgets.main_window import LIMIT_RESET_GRACE_S
    late._limit_resets_at = now - 30             # just past, inside the grace
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a reset that has only just passed waits out the grace",
          sent(late) == "" and late.is_limit_blocked())
    late._limit_resets_at = now - LIMIT_RESET_GRACE_S - 60   # grace elapsed
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: the watchdog resumes it with NO usage reading at all",
          AUTO_CONTINUE_TEXT in sent(late))

    # a WEEKLY window is not readable off the screen: its banner prints a bare
    # wall clock for a reset that can be days out, so acting on that clock
    # would nudge days early. It waits for the account reading instead.
    writes.clear()
    weekly = mk("Weekly")
    settle(weekly, "You've hit your weekly limit \xb7 resets 4:40am\n")
    ws.agents.append(weekly)
    check("auto-continue: the weekly window is identified from its banner",
          weekly.limit_window() == "weekly")
    weekly._limit_resets_at = now - 2 * LIMIT_RESET_GRACE_S   # clock says go
    saved_usage, win._usage = win._usage, None                # ...API silent
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a weekly cut-off is NOT resumed on its bare clock",
          sent(weekly) == "" and weekly.is_limit_blocked())
    win._usage = cu.Usage(                                   # account is clear
        limits=(cu.Limit(key="seven_day", label=cu._LABELS["seven_day"],
                         short=cu._SHORT["seven_day"], percent=8.0,
                         resets_at=now + 90000),),
        fetched_at=now, plan="max")
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: ...but IS once the account reading says it cleared",
          AUTO_CONTINUE_TEXT in sent(weekly))
    win._usage = saved_usage

    # a banner with no parseable time has no due date -- it must NOT count as
    # "due now", which would fire a pointless Continue into a still-blocked
    # agent and then drop the latch, missing the real reset
    writes.clear()
    timeless = mk("Timeless")
    settle(timeless, "You've hit your session limit\n")
    ws.agents.append(timeless)
    check("auto-continue: a banner with no time still latches the cut-off",
          timeless.is_limit_blocked() and timeless.limit_resets_at() is None)
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an unknown reset time is NOT treated as due now",
          sent(timeless) == "" and timeless.is_limit_blocked())

    # ...but it must not wait FOREVER either. With no clock from the screen and
    # none from the API, fall back to the longest a window can last, so a latch
    # can never become permanent for want of a timestamp.
    from app.widgets.main_window import LIMIT_UNKNOWN_WAIT_S
    timeless._limit_at = now - LIMIT_UNKNOWN_WAIT_S - 60
    timeless._limit_resets_at = None             # nothing supplied a clock
    saved_usage, win._usage = win._usage, None   # not even the account
    win._check_limit_resets()
    win._usage = saved_usage
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a clockless cut-off is resumed once no window could "
          "still be open", "Continue" in sent(timeless))

    # the banner is not bottom-anchored: its options menu, the input box and
    # the footer all render below it, so the scrape window must be wider than
    # the selection-menu scrape's 18 lines
    deep = mk("Deep")
    settle(deep, BANNER + "\n".join(
        ["  1. Upgrade", "  2. Team plan", "  3. Extra usage", "  4. Cancel"]
        + [f"filler {i}" for i in range(14)]
        + ["│ > │", "  ? for shortcuts"]))
    check("auto-continue: the banner is found above a full frame of menu/input",
          deep.is_limit_blocked())

    # --- the banner still on screen must not re-latch a RESUMED agent -------
    # Observed live on 2026-08-04: an agent was re-BLOCKED 13 s after RESUMED,
    # and another five hours after its resume, both with a reset time exactly
    # 24 h ahead -- the signature of parse_reset_clock re-reading a banner whose
    # clock had already passed. The banner is ordinary output that stays in view
    # (and is redrawn with every frame), so clearing the latch let the next
    # burst latch the very same line again. That muted the agent's "?" chime and
    # later typed a stray Continue into an agent that was working fine.
    relatch = mk("Relatch")
    settle(relatch, PARKED)
    check("auto-continue: the parked agent latches in the first place",
          relatch.is_limit_blocked())
    relatch._screen_tail = BANNER + "\nWorking on it...\n  ? for shortcuts\n"
    check("auto-continue: menu gone -> the resume is verified",
          relatch.recheck_limit() is False)
    relatch._on_pty_output("pty", BANNER + "still working\n")
    check("auto-continue: the SAME banner lingering after a resume does NOT "
          "re-latch the agent", not relatch.is_limit_blocked())
    relatch._on_pty_output("pty", NEXT_BANNER)
    check("auto-continue: the NEXT window's banner (a different clock) does "
          "latch", relatch.is_limit_blocked())

    # the guard is the missing MENU, not the text alone: a genuine cut-off
    # renders its menu directly below the banner, so an identical line WITH the
    # menu is a real new cut-off (the same wall clock can come round again on a
    # weekly window) and must still latch.
    same = mk("SameClock")
    settle(same, PARKED)
    same.clear_limit_block()
    same._on_pty_output("pty", PARKED)
    check("auto-continue: an identical banner accompanied by the parked-on "
          "menu IS a new cut-off", same.is_limit_blocked())
    # ...and a restart starts the screen over, so nothing is an echo any more
    restarted = mk("Restarted")
    settle(restarted, BANNER)
    restarted.clear_limit_block()
    restarted._forget_limit_echo()       # what start()/restart() do
    restarted._on_pty_output("pty", BANNER)
    check("auto-continue: a (re)start forgets the echo guard",
          restarted.is_limit_blocked())

    # --- the two triggers must not double-nudge -----------------------------
    # The attempt counter only advances when the nudge actually goes out, up to
    # a full stagger later, so a second trigger inside that window re-scheduled
    # everyone downstream of the first agent: on 2026-08-04 the API's cleared
    # edge and the minute watchdog both fired at 05:30 and typed Continue TWICE
    # into three of four agents.
    from app.widgets.main_window import AUTO_CONTINUE_STAGGER_MS
    writes.clear()
    dup = mk("Dup")
    settle(dup, BANNER)
    ws.agents.append(dup)
    win._on_auto_continue(True)
    win._resume_blocked_agents()          # the planLimitCleared edge...
    win._resume_blocked_agents()          # ...racing the minute watchdog
    # long enough that a duplicate would land even a full stagger behind
    pump(AUTO_CONTINUE_SETTLE_MS + AUTO_CONTINUE_STAGGER_MS)
    check("auto-continue: two overlapping triggers nudge each agent ONCE",
          sent(dup).count("Continue") == 1 and dup.limit_attempts() == 1)
    check("auto-continue: the claim is released once the nudge has gone out",
          dup.id not in win._resume_pending)

    # --- a latch the TRANSCRIPT refutes is dropped, not nudged --------------
    # The screen said this agent was cut off; the conversation on disk says it
    # carried on. A banner stays in view (and is redrawn) long after the agent
    # moved on, so the screen alone can be describing history -- and a stray
    # "Continue" into an agent that is working is both an interruption and real
    # quota spent.
    import json as _json
    from app import transcripts as _tr

    def write_convo(cwd_, sid, records):
        """A real Claude transcript for `cwd_`/`sid` (records = [(text, at)])."""
        path = _tr.transcript_path(cwd_, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for text, at in records:
                fh.write(_json.dumps(_as_cli_wrote({
                    "type": "assistant",
                    "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S.000Z",
                                                _time.gmtime(at)),
                    "message": {"content": [{"type": "text",
                                             "text": text}]}})) + "\n")

    writes.clear()
    ph_cwd = str(tmp / "phantom")
    phantom = mk("Phantom")
    phantom.spec.cwd, phantom.spec.session_id = ph_cwd, "sid-moved-on"
    write_convo(ph_cwd, "sid-moved-on",
                [(BANNER.strip(), now - 7200), ("carried on", now - 3600)])
    settle(phantom, BANNER)
    ws.agents.append(phantom)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: a latch the transcript refutes is dropped, never "
          "nudged", sent(phantom) == "" and not phantom.is_limit_blocked())
    check("auto-continue: the dismissal is recorded as an outcome",
          any(r.get("event") == limit_ledger.DISMISSED
              for r in limit_ledger.read_all(str(tmp))))

    # ...but a transcript that cannot be READ is no evidence either way. A
    # drifted pin, or a conversation Claude has not flushed, must never strand
    # a genuinely parked agent.
    writes.clear()
    unknown = mk("Unknown")
    unknown.spec.cwd, unknown.spec.session_id = ph_cwd, "sid-no-such-file"
    settle(unknown, BANNER)
    ws.agents.append(unknown)
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("auto-continue: an unreadable transcript does not veto the latch",
          AUTO_CONTINUE_TEXT in sent(unknown))

    # --- a trivial background-task cut-off is dismissed EARLY, before the ---
    # --- watchdog ever gets a chance to nudge it -----------------------------
    # The screen renders the identical "Stop and wait for limit to reset"
    # menu whether the interrupted turn was real work or Claude Code's own
    # background-command-completion notification auto-continuing on its own
    # -- so the live latch (correctly) cannot tell them apart, and the
    # hourglass shows either way. `_dismiss_if_phantom` re-checks the
    # transcript a few seconds after the latch and clears it once the
    # evidence says nothing of the agent's actual work was lost, instead of
    # waiting for reset time to find out via a wasted nudge.
    from app.widgets.main_window import LIMIT_PHANTOM_CHECK_MS

    def write_raw(cwd_, sid, records):
        path = _tr.transcript_path(cwd_, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(_as_cli_wrote(rec)) + "\n")

    def ts(at):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(at))

    writes.clear()
    bg_cwd = str(tmp / "bgnotify")
    bg = mk("BgNotify")
    bg.spec.cwd, bg.spec.session_id = bg_cwd, "sid-bg-notify"
    write_raw(bg_cwd, "sid-bg-notify", [
        {"type": "assistant", "timestamp": ts(now - 3600),
         "message": {"content": [{"type": "text", "text": "Done, merged."}]}},
        {"type": "user", "timestamp": ts(now - 30),
         "message": {"content": "<task-notification>\n<task-id>t1</task-id>\n"
                                "<status>completed</status>\n"
                                "</task-notification>"}},
        {"type": "assistant", "timestamp": ts(now - 30),
         "message": {"content": [{"type": "text", "text": BANNER.strip()}]}},
    ])
    settle(bg, BANNER)
    ws.agents.append(bg)
    check("auto-continue: a trivial background-task cut-off still latches "
          "live (the screen alone can't tell it apart from a real one)",
          bg.is_limit_blocked())
    win._on_agent_limit_blocked(ws.id, bg.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: ...but the early phantom check clears it once the "
          "transcript shows the interrupted turn wasn't real work",
          not bg.is_limit_blocked())
    check("auto-continue: it is never nudged", sent(bg) == "")
    check("auto-continue: the early dismissal is recorded as an outcome too",
          any(r.get("event") == limit_ledger.DISMISSED
              and "background-task" in r.get("detail", "")
              for r in limit_ledger.read_all(str(tmp))))

    # ...and a GENUINE cut-off latched the same way must survive that same
    # early check untouched.
    writes.clear()
    real_cwd = str(tmp / "realask")
    real_ask = mk("RealAsk")
    real_ask.spec.cwd, real_ask.spec.session_id = real_cwd, "sid-real-ask"
    write_raw(real_cwd, "sid-real-ask", [
        {"type": "assistant", "timestamp": ts(now - 3600),
         "message": {"content": [{"type": "text", "text": "Done, merged."}]}},
        {"type": "user", "timestamp": ts(now - 30),
         "message": {"content": "can you add Chess960 castling support?"}},
        {"type": "assistant", "timestamp": ts(now - 30),
         "message": {"content": [{"type": "text", "text": BANNER.strip()}]}},
    ])
    settle(real_ask, BANNER)
    ws.agents.append(real_ask)
    win._on_agent_limit_blocked(ws.id, real_ask.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: a genuine cut-off survives the early phantom check",
          real_ask.is_limit_blocked())

    # THE RACE the early check has to lose safely: it runs seconds after the
    # banner was DRAWN, and Claude may not have written that record yet. A
    # running agent's transcript always exists, so an unflushed banner does
    # NOT read as "no transcript" -- it reads as a conversation that carried
    # on. Dismissing on that would clear a genuine latch outright (nothing
    # re-latches a silently parked agent), so the early check gates on
    # positive `synthetic` evidence instead.
    writes.clear()
    slow_cwd = str(tmp / "unflushed")
    slow = mk("Unflushed")
    slow.spec.cwd, slow.spec.session_id = slow_cwd, "sid-unflushed"
    write_raw(slow_cwd, "sid-unflushed", [
        {"type": "user", "timestamp": ts(now - 60),
         "message": {"content": "please refactor the parser"}},
        {"type": "assistant", "timestamp": ts(now - 50),
         "message": {"content": [{"type": "text", "text": "Working on it."}]}},
    ])
    settle(slow, BANNER)
    ws.agents.append(slow)
    win._on_agent_limit_blocked(ws.id, slow.id)
    pump(LIMIT_PHANTOM_CHECK_MS + 500)
    check("auto-continue: a latch whose banner has not been written to the "
          "transcript yet is NOT dismissed early", slow.is_limit_blocked())

    # (a USER-typed slash command is classified in test_startup_limit_recovery;
    # the window only acts on that classification, as the two cases above show)

    # --- the cut-off is visible on the card ---------------------------------
    marked = mk("Marked")
    settle(marked, BANNER)
    check("auto-continue: an interrupted agent describes its cut-off",
          "usage limit" in marked.limit_summary()
          and "resets" in marked.limit_summary())
    check("auto-continue: ...and says a resume is pending",
          "waiting to auto-continue" in marked.limit_summary())
    marked.note_limit_attempt()
    check("auto-continue: ...then how many resumes have been tried",
          "tried 1x" in marked.limit_summary())
    marked.clear_limit_block()
    check("auto-continue: an agent that is not cut off describes nothing",
          marked.limit_summary() == "")

    win._on_auto_continue(False)   # what the reopen below must find
    win.close()

    win2 = create_main_window(store)
    win2.show(); pump(50)
    check("auto-continue: preference restored on reopen",
          win2.top_bar.auto_continue() is False)
    check("auto-continue: default is ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")).top_bar.auto_continue())
    win2.close()

    # the limit banner must NOT ring the chime — nothing the sleeper can answer
    rung = {"n": 0}
    import app.chime as _chime
    real_play, _chime.play = _chime.play, lambda *_a: rung.__setitem__("n", rung["n"] + 1)
    try:
        win3 = create_main_window(SessionStore(path=tmp / "chime.json"))
        win3.show(); pump(50)
        w3 = win3.manager.workspaces[0]
        quiet, loud = mk("Quiet"), mk("Loud")
        settle(quiet, BANNER)
        settle(loud, "1. Yes\n❯ 2. No\n")
        w3.agents.extend([quiet, loud])
        win3._on_agent_waiting(w3.id, quiet.id)
        check("auto-continue: a limit-blocked agent does not ring the chime",
              rung["n"] == 0)
        win3._on_agent_waiting(w3.id, loud.id)
        check("auto-continue: an ordinary prompt still rings the chime",
              rung["n"] == 1)
        win3.close()
    finally:
        _chime.play = real_play


def test_gemini_limit_detection():
    """A Gemini agent cut off by its quota is seen, shown and resumed too.

    The whole recovery path was gated on `provider == "claude"`, so an agy agent
    that ran out of quota sat there silently: no hourglass, no ledger entry, no
    auto-continue (reported live, 2026-08-09, on Agent 10 of the AI Hive
    workspace). Detecting it is not a matter of one more pattern -- agy's
    cut-off is a different SHAPE, and each difference is checked here:

      * no interactive menu, so the identity guard carries the whole weight;
      * a RELATIVE countdown ("Resets in 1h40m21s") rather than a wall clock;
      * the message WRAPS, so its identity is not on the matched line;
      * no conversation on disk, so there is no second source to agree with.
    """
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import limit_banner as lb
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.widgets.main_window import AUTO_CONTINUE_TEXT
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()
    AUTO_CONTINUE_SETTLE_MS = 1200

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    # exactly as agy renders it, wrapped across three rows, warning glyph and
    # all. The Error ID's trailing counter differs per message.
    def quota(countdown="1h40m21s", tag="266"):
        return ("⚠Individual quota reached. Please upgrade your "
                "subscription to increase your\n"
                f"limits. Resets in {countdown}.\n"
                f"Error ID: 6a22d054-3666-4717-b0ac-7b359153c647-{tag}\n")

    FOOTER = "\n> \n? for shortcuts                    Gemini 3.6 Flash \xb7 high\n"

    # --- the patterns, in isolation -----------------------------------------
    line = lb.gemini_banner_line(quota())
    check("gemini-limit: the wrapped message is read as one banner",
          line.startswith("Individual quota reached") and "1h40m21s" in line
          and line.endswith("-266"), line)
    check("gemini-limit: the LAST message wins (a retry describes the state "
          "now)", "-273" in lb.gemini_banner_line(quota() + quota("1h39m56s",
                                                                  "273")))
    check("gemini-limit: two cut-offs are told apart by their own text",
          lb.gemini_banner_line(quota()) != lb.gemini_banner_line(
              quota("1h39m56s", "273")))
    check("gemini-limit: an agent DISCUSSING a quota is not cut off by one",
          lb.gemini_banner_line("note that the quota reached its cap "
                                "yesterday, resets in 3h") == "")
    check("gemini-limit: Claude's banner is not read as a Gemini one, or the "
          "reverse",
          lb.gemini_banner_line("You've hit your session limit - resets 3am")
          == "" and lb.banner_line(quota()) == "")
    check("gemini-limit: the relative countdown resolves against NOW",
          lb.gemini_reset_at(quota(), 1000.0) == 1000.0 + 3600 + 40 * 60 + 21)
    for text, secs in (("resets in 45m", 2700), ("Resets in 30s", 30),
                       ("resets in 2h", 7200)):
        check(f"gemini-limit: {text!r} -> {secs}s",
              lb.gemini_reset_at(text, 0.0) == secs)
    check("gemini-limit: a message with no countdown yields no reset time",
          lb.gemini_reset_at("Individual quota reached.") is None)
    # the printed text is frozen; only the moment it is READ can date it, which
    # is why the epoch is resolved once at latch time and then kept
    check("gemini-limit: the same text read later resolves later (read once)",
          lb.gemini_reset_at(quota(), 2000.0)
          - lb.gemini_reset_at(quota(), 1000.0) == 1000.0)

    # --- the live latch ------------------------------------------------------
    writes: dict = {}

    def mk(name="Gem", kind=AgentKind.GEMINI):
        spec = build_spec(kind, name, cwd=SCRATCH_CWD, pty=True)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d), True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    sent = lambda ag: "".join(writes.get(id(ag.worker), []))

    g = mk()
    g._on_pty_output("pty", "running the suite...\n" + FOOTER)
    check("gemini-limit: ordinary output is not a cut-off",
          not g.is_limit_blocked())
    g._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: the quota message IS a cut-off", g.is_limit_blocked())
    check("gemini-limit: its countdown is latched as a real reset time",
          g.limit_resets_at() is not None
          and 6000 < g.limit_resets_at() - _time.time() < 6100)
    # "quota" and not "weekly": agy states a DURATION, which is equally correct
    # for a window days out, so it never has the bare-wall-clock ambiguity that
    # makes a Claude weekly banner unusable
    check("gemini-limit: the window is ordinary and due on its own clock",
          g.limit_window() == "quota")

    # No menu exists, so the message alone must not re-latch an agent that has
    # already been resumed off it -- the guard Claude gets from the torn-down
    # menu, Gemini gets entirely from the message's identity.
    g.clear_limit_block()
    g._on_pty_output("pty", quota() + "\ncarrying on\n" + FOOTER)
    check("gemini-limit: the same message lingering after a resume does NOT "
          "re-latch", not g.is_limit_blocked())
    g._on_pty_output("pty", quota("58m12s", "301") + FOOTER)
    check("gemini-limit: a genuinely NEW refusal (its own Error ID) does latch",
          g.is_limit_blocked())

    notready = mk("NotReady")
    notready._prompt_ready = False
    notready._on_pty_output("pty", quota())
    check("gemini-limit: a message drawn before the prompt is ready is history",
          not notready.is_limit_blocked())

    # --- recovery after a restart, off the REPLAY ---------------------------
    # This IS Gemini's startup recovery: the live latch is never persisted and
    # there is no conversation on disk, but `agy --continue` redraws the
    # conversation it ended on, so the message is right there. Verified live
    # (2026-08-09, Agent 10): it latched 5 s after launch off the replay.
    #
    # THE COUNTDOWN IN IT IS STALE, and that is the defect this pins down. The
    # text says "1h39m56s" forever, so resolving it against the clock at launch
    # dated the reset 1h49m too late -- a quota that came back at 19:05 would
    # not have been touched until 21:54. It is discarded and the agent probed
    # at once; a quota that is still spent says so with an exact countdown.
    replay = mk("Replay")
    replay._session_started = _time.time()          # ...just launched
    replay._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a cut-off replayed at launch is recovered",
          replay.is_limit_blocked())
    check("gemini-limit: ...probed NOW, not on the replay's frozen countdown",
          replay.limit_resets_at() is not None
          and abs(replay.limit_resets_at() - _time.time()) < 5)
    check("gemini-limit: ...and it answers to the startup-recovery toggle",
          replay.limit_from_startup() is True)

    # a message with real work after it belongs to a cut-off the conversation
    # already recovered from -- continuing that would interrupt finished work
    carried = mk("CarriedOn")
    carried._session_started = _time.time()
    carried._on_pty_output("pty", quota()
                           + "".join(f"  Edit(file_{i}.py)\n" for i in range(14))
                           + "All 1324 tests passed cleanly!\n" + FOOTER)
    check("gemini-limit: a replayed message with work after it is NOT a "
          "live cut-off", not carried.is_limit_blocked())
    # ...and that judgement has to OUTLAST the launch window: the message sits
    # in the rolling tail long after, so without arming the echo guard the next
    # repaint would read it as live and latch its stale countdown
    carried._session_started = _time.time() - 3600
    carried._on_pty_output("pty", "  ? for shortcuts\n")
    check("gemini-limit: ...and a later repaint does not latch it either",
          not carried.is_limit_blocked())

    # once the launch window has passed, the printed countdown is current again
    live = mk("Live")
    live._session_started = _time.time() - 3600      # long since settled
    live._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a refusal outside the launch window keeps its own "
          "countdown", live.is_limit_blocked()
          and live.limit_resets_at() - _time.time() > 3000)
    check("gemini-limit: ...and is owned by the live auto-continue toggle",
          live.limit_from_startup() is False)

    # a provider with no patterns of its own is untouched by either CLI's
    shell = mk("Shell", kind=AgentKind.CMD)
    shell._on_pty_output("pty", quota() + FOOTER)
    check("gemini-limit: a plain shell agent is never limit-blocked",
          not shell.is_limit_blocked())

    # --- verification: agy answers a still-spent quota with a NEW message ----
    v = mk("Verify")
    v._on_pty_output("pty", quota() + FOOTER)
    first_reset = v.limit_resets_at()
    v._screen_tail = quota() + "\nContinue\n" + quota("59m1s", "500") + FOOTER
    check("gemini-limit: a fresh refusal after the nudge means STILL blocked",
          v.recheck_limit() is True and v.is_limit_blocked())
    check("gemini-limit: ...and the retry waits out the NEW countdown, not the "
          "spent one", v.limit_resets_at() != first_reset
          and v.limit_resets_at() - _time.time() > 3000)
    # ...and now nothing newer appears: the messages are still in the
    # scrollback (they never go away, there being no menu to tear down), but the
    # newest is the one already accounted for, so the agent is going again
    v._screen_tail = (quota() + "\nContinue\n" + quota("59m1s", "500")
                      + "\nworking on it...\n" + FOOTER)
    check("gemini-limit: the message merely lingering means it RESUMED",
          v.recheck_limit() is False and not v.is_limit_blocked())

    # --- the wiring ----------------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-gemlimit-"))
    win = create_main_window(SessionStore(path=tmp / "s.json"))
    win.show(); pump(50)
    ws = win.manager.workspaces[0]

    gem = mk("GemCut")
    gem._on_pty_output("pty", quota() + FOOTER)
    ws.agents.append(gem)
    check("gemini-limit: the workspace's blocked count sees it (hourglass)",
          win.manager.workspace_stats(ws.id)["limit_blocked"] == 1)

    # The account reading behind the no-`due` trigger is CLAUDE's plan, which
    # says nothing at all about a Gemini quota -- so that shortcut must not
    # reach a Gemini agent.
    win._resume_blocked_agents()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: the Claude account's cleared edge does NOT resume a "
          "Gemini agent", sent(gem) == "" and gem.is_limit_blocked())

    # its own countdown does, through the network-free watchdog
    gem._limit_resets_at = now + 3600
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: an agent whose countdown has not run out waits",
          sent(gem) == "")
    gem._limit_resets_at = now - 600
    # ...and with NO usage reading of any kind in the app. The Gemini pill and
    # this path share nothing: the resume is driven entirely by the countdown
    # the agent's own message stated, so a usage readout that is missing, stale
    # or wrong can never be the reason a Continue fails to go out.
    check("gemini-limit: the resume path holds no usage reading at all",
          win.plan_usage() is None)
    win._check_limit_resets()
    pump(AUTO_CONTINUE_SETTLE_MS)
    check("gemini-limit: the watchdog resumes it once the countdown has passed",
          AUTO_CONTINUE_TEXT in sent(gem))
    # Esc closes CLAUDE's options menu. agy has none, and an Esc there would
    # clear whatever the user had half-typed instead.
    check("gemini-limit: no stray Esc is sent (there is no menu to dismiss)",
          "\x1b" not in sent(gem))
    win.close()


def test_startup_limit_recovery():
    """Agents the plan limit stopped BEFORE the app opened are found and armed.

    The live latch cannot see these: after a restart each agent's screen shows
    a REPLAYED conversation, which _scrape_limit deliberately ignores as
    history. The transcript is the durable record — it still ends exactly where
    the limit stopped it. The gate the user asked for is strict: no stoppage
    message, no action."""
    import json as _json
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import limit_banner, limit_ledger, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-startup-rec-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def iso(epoch):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(epoch))

    def write_transcript(cwd, sid, records):
        d = Path(transcripts.transcript_path(cwd, sid)).parent
        d.mkdir(parents=True, exist_ok=True)
        with open(transcripts.transcript_path(cwd, sid), "w",
                  encoding="utf-8") as fh:
            for r in records:
                fh.write(_json.dumps(_as_cli_wrote(r)) + "\n")

    def assistant(text, at):
        return {"type": "assistant", "timestamp": iso(at),
                "message": {"content": [{"type": "text", "text": text}]}}

    def limit_banner_key(cwd_, sid, at):
        """The ledger identity of a cut-off written at `at` -- keyed on the
        window's RESET, the one number the live screen and the transcript both
        resolve identically."""
        reset = limit_banner.banner_reset_at(BANNER, at) or 0.0
        return limit_ledger.key_of(cwd_, sid, reset, at)

    now = _time.time()
    cwd = str(tmp)
    # no "(Area/City)": the anchor below is built in LOCAL time, and a named
    # zone would read "3am" in that zone instead (test_reset_clock_zones)
    BANNER = "You've hit your session limit \xb7 resets 3am"

    # --- reading the durable record -----------------------------------------
    # The MOST RECENT 02:42 local: the wall time matters (it is what makes
    # "resets 3am" land 18 minutes later, the anchoring this test is about) but
    # the DATE must not. Pinned to a fixed calendar day this test passed for a
    # while and then began failing on its own, once that day fell outside
    # STARTUP_RECOVERY_MAX_AGE_S and recovery correctly skipped it as stale.
    _lt = _time.localtime(now)
    cut_at = _time.mktime((_lt.tm_year, _lt.tm_mon, _lt.tm_mday,
                           2, 42, 0, 0, 0, -1))
    if cut_at > now:
        cut_at -= 86400
    write_transcript(cwd, "sid-cut", [assistant("working", cut_at - 600),
                                      assistant(BANNER, cut_at)])
    hit, when, resets = transcripts.ended_on_limit(cwd, "sid-cut")
    check("startup-recovery: a transcript ending on the banner is a cut-off",
          hit and abs(when - cut_at) < 2)
    lt = _time.localtime(resets)
    check("startup-recovery: the reset is anchored to WHEN THE BANNER WAS "
          "WRITTEN, not to now (02:42 + 'resets 3am' -> 03:00 THAT day)",
          (lt.tm_hour, lt.tm_min) == (3, 0) and 0 < resets - cut_at < 3600)

    # the same banner resolved against a later clock lands a full day out --
    # the bug this anchoring exists to prevent
    naive = limit_banner.parse_reset_clock(BANNER, cut_at + 6 * 3600)
    check("startup-recovery: un-anchored parsing would stall a day",
          naive - resets > 80000)

    write_transcript(cwd, "sid-went-on", [assistant(BANNER, cut_at),
                                          assistant("carrying on", cut_at + 60)])
    hit2, _, _ = transcripts.ended_on_limit(cwd, "sid-went-on")
    check("startup-recovery: a banner followed by real output is history, "
          "not a cut-off", not hit2)
    check("startup-recovery: no transcript at all is not a cut-off",
          transcripts.ended_on_limit(cwd, "sid-missing")[0] is False)

    # claude.exe 2.1.235+ injects the cut-off notice as a standalone
    # system/informational record rather than an assistant turn -- read off a
    # real transcript, not guessed (see limit_banner's module docstring). A
    # transcript-based recovery pass that only ever looked at assistant
    # records would silently pass over every one of these.
    def system_msg(content, at, subtype="informational"):
        return {"type": "system", "subtype": subtype, "isSidechain": False,
                "content": content, "timestamp": iso(at)}

    NEW_BANNER = ("Usage limit reached \xb7 continuing automatically at "
                  "10:10pm \xb7 esc or type to cancel")
    write_transcript(cwd, "sid-cut-system", [
        assistant("working", cut_at - 600),
        system_msg(NEW_BANNER, cut_at),
        # a same-record-shape sibling with no plain-string content (the real
        # turn_duration record right after it in a live transcript) must not
        # crash the read or be mistaken for a banner
        {"type": "system", "subtype": "turn_duration", "isSidechain": False,
         "durationMs": 971179, "timestamp": iso(cut_at + 1)}])
    hit_sys, when_sys, resets_sys = transcripts.ended_on_limit(
        cwd, "sid-cut-system")
    check("startup-recovery: the CURRENT 'Usage limit reached' wording, "
          "injected as a system/informational record, is recognised as a "
          "cut-off", hit_sys and abs(when_sys - cut_at) < 2)
    check("startup-recovery: its own 'continuing automatically at ...' clock "
          "is resolved, not left unknown", resets_sys > 0)

    # the CLI's own auto-continue logs "Usage limit reset (c) continuing
    # automatically" once the window reopens (verified live) -- that is
    # equally real output and must clear the cut-off exactly like an
    # assistant reply carrying on would.
    write_transcript(cwd, "sid-cli-self-resumed", [
        system_msg(NEW_BANNER, cut_at),
        system_msg("Usage limit reset \xb7 continuing automatically",
                   cut_at + 9000)])
    hit_resumed, _, _ = transcripts.ended_on_limit(cwd, "sid-cli-self-resumed")
    check("startup-recovery: the CLI's own 'Usage limit reset' notice reads "
          "as history, not a cut-off (it already resumed on its own)",
          not hit_resumed)

    # A cut-off is only real when the turn it stopped was something the user
    # (or a delivered task) actually asked for. Claude Code can turn a
    # background command's own completion into a brand-new turn with NO input
    # from anyone -- if the account is exhausted right then, that turn hits
    # the identical banner a real interruption would, but nothing of the
    # agent's actual work was lost. Observed live: an agent long done with its
    # assigned task got auto-nudged over a stray background Playwright lookup
    # finishing hours later.
    def user_msg(content, at):
        return {"type": "user", "timestamp": iso(at),
                "message": {"content": content}}

    write_transcript(cwd, "sid-bg-notify", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("<task-notification>\n<task-id>abc</task-id>\n"
                 "<status>completed</status>\n</task-notification>", cut_at),
        assistant(BANNER, cut_at)])
    hit3, _, _ = transcripts.ended_on_limit(cwd, "sid-bg-notify")
    check("startup-recovery: a banner behind a <task-notification> (no real "
          "input from the user or AI Hive) is not a cut-off worth resuming",
          not hit3)

    write_transcript(cwd, "sid-real-ask", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("can you also add Chess960 castling support?", cut_at),
        assistant(BANNER, cut_at)])
    hit4, _, _ = transcripts.ended_on_limit(cwd, "sid-real-ask")
    check("startup-recovery: a banner behind a REAL user prompt is still a "
          "genuine cut-off", hit4)

    write_transcript(cwd, "sid-tool-result", [
        assistant("Done, merged and clean.", cut_at - 3600),
        {"type": "user", "timestamp": iso(cut_at),
         "message": {"content": [{"type": "tool_result",
                                  "content": [{"type": "text",
                                               "text": "exit 0"}]}]}},
        assistant(BANNER, cut_at)])
    hit5, _, _ = transcripts.ended_on_limit(cwd, "sid-tool-result")
    check("startup-recovery: a banner behind an ordinary tool-result reply "
          "(mid-turn, not synthetic) is still a genuine cut-off", hit5)

    # A slash command READS like plumbing -- a bare `<command-name>` string,
    # same shape as a <task-notification> -- but the user typed it, so the
    # work behind it is theirs and a cut-off there is as real as any other.
    # Keying "synthetic" off the leading "<" alone swept these in and would
    # have silently dropped the cut-off (real transcripts do carry a
    # `<command-name>` record followed straight by the assistant turn).
    write_transcript(cwd, "sid-slash-cmd", [
        assistant("Done, merged and clean.", cut_at - 3600),
        user_msg("<command-name>/security-review</command-name>\n"
                 "<command-message>security-review</command-message>", cut_at),
        assistant(BANNER, cut_at)])
    hit6, _, _ = transcripts.ended_on_limit(cwd, "sid-slash-cmd")
    check("startup-recovery: a banner behind a USER-typed slash command is a "
          "genuine cut-off, not plumbing", hit6)

    # `synthetic` is positive evidence and must never stand in for "no banner
    # found": an early caller gates on it precisely because a banner Claude
    # has drawn but not yet WRITTEN reads as cut_off False on an existing
    # transcript, which is not evidence of anything.
    write_transcript(cwd, "sid-unflushed", [
        user_msg("please refactor the parser", cut_at - 60),
        assistant("Working on it now.", cut_at - 50)])
    unflushed = transcripts.limit_cut_off(cwd, "sid-unflushed")
    check("startup-recovery: an unwritten banner is NOT flagged synthetic "
          "(absence of evidence is not evidence)",
          unflushed is not None and not unflushed["cut_off"]
          and not unflushed["synthetic"], unflushed)
    check("startup-recovery: a real plumbing cut-off IS flagged synthetic",
          transcripts.limit_cut_off(cwd, "sid-bg-notify")["synthetic"])
    check("startup-recovery: a genuine cut-off is not flagged synthetic",
          not transcripts.limit_cut_off(cwd, "sid-real-ask")["synthetic"])

    # --- arming from it ------------------------------------------------------
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show(); pump(50)
    ws = win.manager.workspaces[0]

    def agent_for(name, sid):
        spec = build_spec(AgentKind.CLAUDE, name, cwd=cwd, pty=True)
        spec.session_id = sid
        a = TerminalAgent(spec)
        a.worker = type("W", (), {"is_running": lambda s: True,
                                  "write": lambda s, d: True,
                                  "start": lambda s: None,
                                  "dispose": lambda s: None})()
        a._prompt_ready = True
        ws.agents.append(a)
        return a

    cut = agent_for("Cut", "sid-cut")
    went_on = agent_for("WentOn", "sid-went-on")
    check("startup-recovery: armed exactly the cut-off agent",
          win.recover_blocked_at_startup() == 1)
    check("startup-recovery: the cut-off agent is latched, with its reset time",
          cut.is_limit_blocked() and cut.limit_resets_at() is not None)
    check("startup-recovery: the latch is marked as coming from startup",
          cut.limit_from_startup() is True)
    check("startup-recovery: an agent without the stoppage message is untouched",
          not went_on.is_limit_blocked())

    # A card left stopped is STARTED here, unlike an ordinary restore: it was
    # the limit that stopped this agent, not the user, so leaving it alone
    # would drop exactly the work this feature exists to rescue. It must come
    # back as a RESUME -- a fresh start would mint a new session id and abandon
    # the transcript that proved the cut-off.
    stopped = agent_for("Stopped", "sid-cut")
    started = []
    stopped.worker = type("W", (), {"is_running": lambda s: False,
                                    "write": lambda s, d: True,
                                    "start": lambda s: started.append(1),
                                    "dispose": lambda s: None})()
    win.recover_blocked_at_startup()
    check("startup-recovery: an agent the LIMIT stopped is started, not left "
          "stopped", stopped.is_limit_blocked() and started == [1])
    check("startup-recovery: it is started as a resume, keeping its pinned "
          "conversation", stopped.spec.session_id == "sid-cut")

    # --- only the most recent window is acted on ----------------------------
    # This replaced a fixed age bound, which asked the wrong question (how OLD
    # is this) instead of the one that decides (was it the LAST thing that
    # happened) -- and had begun silently skipping real cut-offs once its
    # 36-hour window elapsed. Agents stopped by one window all state the same
    # reset clock, so they group; an earlier window is recorded, not revived.
    older_at, newer_at = now - 30 * 3600, now - 2 * 3600
    write_transcript(cwd, "sid-win-old", [assistant(BANNER, older_at)])
    write_transcript(cwd, "sid-win-new", [assistant(BANNER, newer_at)])
    w_old = agent_for("WinOld", "sid-win-old")
    w_new = agent_for("WinNew", "sid-win-new")
    win.recover_blocked_at_startup()
    check("startup-recovery: the most recent window is armed",
          w_new.is_limit_blocked())
    check("startup-recovery: an earlier window is left alone",
          not w_old.is_limit_blocked())

    # ...but it IS recorded: the ledger is complete even where the action is
    # selective, because the record is what the next feature reads
    entries = limit_ledger.read_all(str(tmp))
    filed = {r.get("session_id") for r in entries
             if r.get("event") == limit_ledger.CUT_OFF}
    check("startup-recovery: every cut-off found is filed in the ledger, "
          "including the ones not acted on",
          {"sid-cut", "sid-win-old", "sid-win-new"} <= filed)
    newest = next(r for r in entries if r.get("session_id") == "sid-win-new")
    check("startup-recovery: the record names workspace, agent and local time",
          newest["ws_name"] and newest["agent_name"] == "WinNew"
          and newest["at_local"][:4].isdigit()
          and abs(newest["at"] - newer_at) < 2)

    # a cut-off already resolved in an EARLIER run is never revived: without
    # this an abandoned conversation whose transcript still ends on the banner
    # would be resumed afresh on every launch
    limit_ledger.record_outcome(
        str(tmp), limit_banner_key(cwd, "sid-win-new", newer_at),
        limit_ledger.RESUMED, tries=1)
    again = agent_for("Again", "sid-win-new")
    win.recover_blocked_at_startup()
    check("startup-recovery: a cut-off already resolved is not revived",
          not again.is_limit_blocked())

    # The `is_pty` gate above is unreachable for a claude agent, and that is
    # load-bearing rather than incidental: CLAUDE is in PTY_ONLY_KINDS, so
    # `build_spec` forces pty=True -- including inside `AgentSpec.from_dict`,
    # which is what makes a session record that has LOST its pty field (see
    # the degraded-save fallback in test_v3_features) still restore as a real
    # terminal instead of a line-mode card that this feature would skip.
    from app.process_worker import AgentSpec as _Spec
    check("startup-recovery: a claude agent is pty even when asked not to be",
          build_spec(AgentKind.CLAUDE, "X", cwd=cwd, pty=False).pty is True)
    check("startup-recovery: ...and a record with no pty field restores as one",
          _Spec.from_dict({"kind": "claude", "name": "X", "cwd": cwd,
                           "provider": "claude", "session_id": "s"}).pty is True)

    # --- the toggle owns its own latches ------------------------------------
    win._startup_recovery = False
    fresh = agent_for("Fresh", "sid-cut")
    check("startup-recovery: scanning is skipped entirely while the toggle is "
          "off", win.recover_blocked_at_startup() == 0
          and not fresh.is_limit_blocked())

    # a startup latch must NOT be resumed by the OTHER toggle
    writes = []
    cut.worker = type("W", (), {
        "is_running": lambda s: True,
        "write": lambda s, d: (writes.append(d), True)[1],
        "start": lambda s: None, "dispose": lambda s: None})()
    cut._limit_resets_at = now - 3600  # its reset is well past the grace
    win._auto_continue = True          # the other switch is on...
    win._check_limit_resets()          # ...and must not act on a startup latch
    pump(1200)
    check("startup-recovery: resume-on-reset does NOT resume a startup latch",
          writes == [])
    win._startup_recovery = True
    win._check_limit_resets()
    pump(1200)
    check("startup-recovery: its own toggle DOES resume it",
          any("Continue" in d for d in writes))

    # --- both preferences persist -------------------------------------------
    win._on_startup_recovery(False)
    win._on_auto_continue(True)
    ui = win._session_payload()["ui"]
    check("startup-recovery: preference persisted under ui.startup_recovery",
          ui["startup_recovery"] is False and ui["auto_continue"] is True)
    win.close()

    win2 = create_main_window(store)
    win2.show(); pump(50)
    check("startup-recovery: both toggles restore independently",
          win2.top_bar.startup_recovery() is False
          and win2.top_bar.auto_continue() is True)
    check("startup-recovery: defaults are ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")).top_bar.startup_recovery())
    win2.close()


def test_limit_recovery_reliability():
    """Every way a real cut-off has gone unrecovered, and the screen that did it.

    Sources so far: 2026-08-07 (CVsummer2026) and 2026-08-31
    (vinted-country-detector). A running count in this docstring only goes
    stale on the next find, so it names the class instead.

    Every one of them is silent by construction — the agent simply sits there —
    so each gets a check that reproduces the exact screen or timing shape that
    defeated it. See app/terminal_agent.py `_tail_lines` and `mark_limit_blocked`
    for the reasoning behind the fixes."""
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import (REPAINT_RESTORE_MS, AgentStatus,
                                    TerminalAgent)
    from app.widgets.main_window import LIMIT_REPAINT_AFTER_WAITS
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limit-rel-"))

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    BANNER = ("You've hit your session limit \xb7 resets 9:30pm "
              "(Europe/Bucharest)")
    # a LATER window's cut-off: successive 5-hour windows never end at the same
    # wall time, which is what makes the banner line a cut-off's identity
    NEXT = ("You've hit your session limit \xb7 resets 2:30am "
            "(Europe/Bucharest)")
    MENU = ("What do you want to do?\n"
            "> 1. Stop and wait for limit to reset\n"
            "  2. Upgrade your plan\n")
    # Claude's TUI pads its frame with blank rows, so the banner ends up far
    # above the bottom in LINES while being close to it in CONTENT. Measured on
    # real screen snapshots: a raw [-40:] slice spanned 432 chars / 5 non-blank
    # lines. This is the frame that lost a genuine cut-off.
    PADDED = BANNER + "\n" + "\n" * 60 + "> try \"fix the tests\"\n  ? for shortcuts\n"

    def mk(name="Coder"):
        a = TerminalAgent(build_spec(AgentKind.CLAUDE, name, cwd=SCRATCH_CWD))
        a._prompt_ready = True
        return a

    # --- 1. the scrape window counts CONTENT, not padding -------------------
    a = mk()
    a._screen_tail = PADDED
    a._scrape_limit()
    check("limit-scrape: a banner above the TUI's blank padding still latches",
          a.is_limit_blocked())
    check("limit-scrape: ...and it carries the reset clock the banner stated",
          a.limit_resets_at() is not None)

    # the two callers that deliberately keep RAW-line windows must not have
    # been widened along with it: recheck_limit reaching further back would
    # find a torn-down menu forever and never report a resume
    b = mk()
    b.mark_limit_blocked(_time.time() + 60, from_startup=False, banner=BANNER)
    b._screen_tail = MENU + "\n" * 60 + "  ? for shortcuts\n"
    check("limit-scrape: recheck_limit still uses raw lines (a menu pushed "
          "past 40 raw lines reads as resumed)",
          b.recheck_limit() is False)

    # --- 2. a disk-recovered latch arms the banner-echo guard ---------------
    # The live re-latch this prevents was observed one SECOND after a verified
    # resume, and dated its phantom cut-off a full day out.
    c = mk()
    c.mark_limit_blocked(1786127400.0, from_startup=True,
                         cut_off_at=1786127061.0, window="session",
                         banner=BANNER)
    c.clear_limit_block()          # what a successful resume does
    c._screen_tail = PADDED        # the same banner, still on screen
    c._scrape_limit()
    check("limit-echo: a startup-armed latch is not re-raised by its own "
          "banner after the resume",
          not c.is_limit_blocked())
    c._screen_tail = NEXT + "\n" + "\n" * 60 + "  ? for shortcuts\n"
    c._scrape_limit()
    check("limit-echo: ...but a genuinely NEW cut-off still latches",
          c.is_limit_blocked())

    # --- 3. readiness is re-checked once the screen settles -----------------
    # _on_pty_output decides readiness against a 600-char tail, once per burst.
    # A footer followed by more than that in the same burst is never seen, and
    # a child that then falls quiet is never looked at again.
    d = mk()
    d._prompt_ready = False
    d.status = AgentStatus.RUNNING
    d._on_pty_output("pty", "welcome\n  ? for shortcuts\n"
                     + ("x" * 400 + "\n") * 3)
    check("prompt-ready: a footer buried in its own burst is missed per-burst",
          not d.prompt_ready())
    d._on_idle_timeout()
    check("prompt-ready: ...and recovered when the screen settles",
          d.prompt_ready())

    e = mk()
    e._prompt_ready = False
    e.status = AgentStatus.RUNNING
    e._on_pty_output("pty", "booting" + "y" * 2000)
    e._on_idle_timeout()
    check("prompt-ready: a settle with no footer at all stays not-ready",
          not e.prompt_ready())

    # --- 4. request_repaint, for a card that never gets a resizeEvent -------
    f = mk()
    check("repaint: refused when the child isn't running", not f.request_repaint())
    resizes: list = []
    f.worker.rows, f.worker.cols = 30, 100
    f.worker.is_running = lambda: True
    f.worker.resize = lambda r, c: resizes.append((r, c))
    check("repaint: accepted for a live pty agent", f.request_repaint())
    check("repaint: narrows first", resizes == [(30, 99)])
    pump(REPAINT_RESTORE_MS + 120)
    check("repaint: and gives the width straight back",
          resizes == [(30, 99), (30, 100)])

    # --- 5. the banner under Claude's own tool-result gutter ----------------
    # A 429 that lands while a tool is still running is painted as a TOOL-RESULT
    # ROW, not as a bare line: "\u23bf" in front of it, and a NO-BREAK space
    # between the two. _GUTTER had neither, so banner_line's .match anchored on
    # the glyph and returned "", which hid the cut-off from the live scrape AND
    # from _note_limit_skip, which calls the same function, so not even a
    # NO-LATCH line was written. Observed live 2026-08-31: an agent sat spent
    # for 36 minutes with no trace of it anywhere. This fixture has to keep the
    # gutter glyphs, or it tests a screen the TUI never draws.
    GUTTERED = (
        "\u25cf Bash(cd \"C:/Users/molna/...\" && cat > /tmp/slim.mjs)\n"
        "  \u23bf\xa0raw 37175 b64 14448 chunks@1400 11\n"
        "  \u23bf\xa0Allowed by auto mode classifier\n"
        "  \u23bf\xa0" + BANNER + "\n"
        "    /upgrade or /usage-credits to finish what you're working on.\n"
        + "\n" * 60 + "> try \"fix the tests\"\n  ? for shortcuts\n")

    g = mk()
    g._screen_tail = GUTTERED
    g._scrape_limit()
    check("limit-gutter: a banner painted as a tool-result row (U+23BF plus a "
          "no-break space) still latches", g.is_limit_blocked())
    check("limit-gutter: ...and still carries the reset clock the banner "
          "stated", g.limit_resets_at() is not None)

    from app import limit_banner as _lb
    check("limit-gutter: the assistant bullet (U+25CF) is stripped too",
          _lb.banner_line("\u25cf " + BANNER) == BANNER)
    check("limit-gutter: a leading no-break space doesn't strand the glyph "
          "behind it", _lb.banner_line("\xa0\u23bf\xa0" + BANNER) == BANNER)
    # the start-of-line anchor is still the discriminator: widening _GUTTER
    # must not let an agent's own sentence about the limit read as a cut-off
    check("limit-gutter: prose that merely mentions the banner still doesn't "
          "match",
          _lb.banner_line("\u25cf I think you've hit your session limit here")
          == "")

    # --- 6. the watchdog stops waiting forever ------------------------------
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    ws = win.manager.create_workspace("W", str(tmp))
    # autostart=False: the default launches a real claude before the stubs
    # below are in place
    agent = win.manager.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "A",
                                                       cwd=str(tmp)),
                                     autostart=False)
    agent._prompt_ready = False    # a card the user has never opened
    asked: list = []
    agent.request_repaint = lambda: (asked.append(1), True)[1]
    agent.worker.is_running = lambda: True
    agent.mark_limit_blocked(_time.time() - 60, from_startup=True,
                             banner=BANNER)
    for _ in range(LIMIT_REPAINT_AFTER_WAITS - 1):
        win._auto_continue_agent(agent)
    check("limit-wait: a booting TUI is left alone at first", not asked)
    win._auto_continue_agent(agent)
    check("limit-wait: after a few quiet ticks the TUI is asked to redraw",
          len(asked) == 1)
    win._auto_continue_agent(agent)
    check("limit-wait: and it is asked exactly once, not every tick",
          len(asked) == 1)
    check("limit-wait: a refused resume still consumes no attempt",
          agent.limit_attempts() == 0)
    agent._prompt_ready = True
    win._auto_continue_agent(agent)
    check("limit-wait: the counter resets once the prompt is live",
          win._limit_wait_ticks.get(agent.id) is None)
    win.close()


def test_reset_clock_zones():
    """A banner's clock is read in the zone it names, and "tomorrow" keeps the
    wall time on the night the clocks change.

    claude.exe prints "resets 3am (Europe/Bucharest)" in ITS zone, which only
    matches Python's local zone when nothing sets an IANA `TZ` (Node honours
    one, the Windows C runtime does not). And the old rollover added 86400 s,
    which lands an hour off on a 23- or 25-hour night. Every check here pins
    its zone explicitly, so it holds on a machine in any zone.
    """
    import datetime as _dt
    import time as _time
    from zoneinfo import ZoneInfo
    from app import limit_banner as lb
    from app import scheduled_send as ss

    tokyo, buc = ZoneInfo("Asia/Tokyo"), ZoneInfo("Europe/Bucharest")

    def wall(epoch, tz):
        d = _dt.datetime.fromtimestamp(epoch, tz)
        return (d.month, d.day, d.hour, d.minute)

    # --- the named zone wins over the local one -----------------------------
    now = _dt.datetime(2026, 9, 24, 22, 0, tzinfo=tokyo).timestamp()
    at = lb.parse_reset_clock("You've hit your session limit \xb7 resets 3am "
                              "(Asia/Tokyo)", now)
    check("reset-zone: a bare clock is read in the zone the banner names",
          at is not None and wall(at, tokyo) == (9, 25, 3, 0)
          and 0 < at - now <= 5 * 3600, at and wall(at, tokyo))
    at2 = lb.parse_reset_clock("You'vehityoursessionlimit\xb7resets3am"
                               "(Asia/Tokyo)", now)
    check("reset-zone: ...also when the renderer dropped the spaces",
          at2 == at)
    wrapped = lb.banner_clock_text(
        "You've hit your session limit \xb7 resets 3am\n(Asia/Tokyo)\n")
    check("reset-zone: a zone wrapped onto the next row is still read",
          lb.parse_reset_clock(wrapped, now) == at, wrapped)
    dated = lb.parse_reset_clock("You've hit your weekly limit \xb7 resets "
                                 "Sep 30, 9am (Asia/Tokyo)", now)
    check("reset-zone: a dated clock is read in the named zone too",
          dated is not None and wall(dated, tokyo) == (9, 30, 9, 0))
    local = lb.parse_reset_clock("resets 3am", now)
    check("reset-zone: an unknown zone falls back to the local one",
          lb.parse_reset_clock("resets 3am (Mars/Olympus_Mons)", now) == local
          and lb.parse_reset_clock("resets 3am (esc to cancel)", now) == local)

    # --- DST: tomorrow is the same WALL time, not now + 24 h ----------------
    # Bucharest leaves summer time at 04:00 on 2026-10-25, so that day is 25 h.
    eve = _dt.datetime(2026, 10, 24, 23, 0, tzinfo=buc).timestamp()
    fall = lb.parse_reset_clock("resets 10pm (Europe/Bucharest)", eve)
    check("reset-zone: a rollover across the autumn change keeps 10pm",
          fall is not None and wall(fall, buc) == (10, 25, 22, 0),
          fall and wall(fall, buc))
    # ...and enters it at 03:00 on 2026-03-29, a 23 h day
    spring_eve = _dt.datetime(2026, 3, 28, 23, 0, tzinfo=buc).timestamp()
    spring = lb.parse_reset_clock("resets 9pm (Europe/Bucharest)", spring_eve)
    check("reset-zone: a rollover across the spring change keeps 9pm",
          spring is not None and wall(spring, buc) == (3, 29, 21, 0),
          spring and wall(spring, buc))

    # the local zone's own change, when it has one this year: the same rule
    # through the local path (no zone printed) and the scheduler's clock field
    t = _time.mktime((2026, 1, 1, 12, 0, 0, 0, 0, -1))
    change = None
    for _ in range(366):
        if _time.localtime(t).tm_isdst != _time.localtime(t + 86400).tm_isdst:
            change = t
            break
        t += 86400
    if change is None:
        skip('reset-zone', 'local zone has no DST change, nothing to roll over')
    else:
        lt = _time.localtime(change)
        eve_local = _time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday,
                                  23, 0, 0, 0, 0, -1))
        for name, got in (
                ("limit banner", lb.parse_reset_clock("resets 10pm", eve_local)),
                ("scheduled send", ss.parse_clock("22:00", eve_local))):
            got_lt = _time.localtime(got)
            check(f"reset-zone: {name} rollover across the LOCAL clock change "
                  "keeps the wall time",
                  (got_lt.tm_hour, got_lt.tm_min) == (22, 0)
                  and 0 < got - eve_local < 26 * 3600,
                  (got_lt.tm_hour, got_lt.tm_min))


def test_limit_detection_hardening():
    """The 2026-09-24 audit of the usage-limit flag, one check per finding.

    (1) The live screen check matched regexes needing whitespace against a
    stream whose spaces are mostly cursor jumps, so a painted banner was a
    coin flip. (2) claude.exe 2.1.281 prints cut-off wordings the patterns
    never knew. (3) Prose starting "Usage limit reached" latched a working
    agent, and the reset clock was read from anywhere in 40 lines. (4) Any
    later system record with text (an away_summary recap, "Remote Control
    disconnected") erased a genuine cut-off on disk, which then got it
    dismissed as a PHANTOM. (5) Nothing caught a cut-off the screen missed.
    (6) The Esc sent before every Continue cancels Claude's own auto-continue.
    """
    import json as _json
    import time as _time
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import limit_banner as lb
    from app import limit_ledger, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import _CSI_RE, AgentStatus, TerminalAgent
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limit-hard-"))
    cwd = str(tmp)
    now = _time.time()

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def jumps(text, col=3):
        """`text` the way Claude's classic renderer paints it: every word put
        in place by a `CSI n G` column jump, and no literal space anywhere."""
        out = []
        for word in text.split(" "):
            out.append(f"\x1b[{col}G{word}")
            col += len(word) + 1
        return "".join(out)

    import datetime as _dt
    from zoneinfo import ZoneInfo
    bucharest = ZoneInfo("Europe/Bucharest")

    def hm(epoch):
        """The wall clock of `epoch` in the zone SESSION names, so these
        checks hold on a machine in any zone."""
        if not epoch:
            return None
        d = _dt.datetime.fromtimestamp(epoch, bucharest)
        return (d.hour, d.minute)

    SESSION = ("You've hit your session limit \xb7 resets 6:40pm "
               "(Europe/Bucharest)")
    AUTO = ("Usage limit reached \xb7 continuing automatically at 10:10pm "
            "\xb7 esc or type to cancel")

    # --- 1. a banner painted with cursor jumps ------------------------------
    painted = _CSI_RE.sub("", jumps(SESSION))
    check("limit-hardening: a jump-painted banner has no spaces left once "
          "escapes are stripped (the shape the old regex never matched)",
          " " not in painted, painted)
    check("limit-hardening: ...and is recognised anyway",
          lb.banner_line(painted) != "")
    check("limit-hardening: ...with its window",
          lb.banner_window(painted) == "session")
    check("limit-hardening: ...and its clock",
          hm(lb.parse_reset_clock(lb.banner_clock_text(painted))) == (18, 40))
    check("limit-hardening: the spaced and the jump-painted rendering are the "
          "SAME cut-off", lb.same_banner(painted, SESSION))
    check("limit-hardening: ...while two windows' banners are not",
          not lb.same_banner(SESSION, SESSION.replace("6:40pm", "11:40pm")))
    check("limit-hardening: a wrapped first row that already has the clock "
          "matches the transcript's whole line",
          lb.same_banner("You've hit your session limit \xb7 resets 6:40pm",
                         SESSION))
    check("limit-hardening: ...but one cut before the clock does not",
          not lb.same_banner("You've hit your session limit \xb7", SESSION))
    check("limit-hardening: the auto-continue line survives jump-painting",
          lb.banner_line(_CSI_RE.sub("", jumps(AUTO))) != "")

    # --- 2. every cut-off wording in claude.exe 2.1.281 ---------------------
    for line, window in [
            ("You've hit your limit \xb7 resets 3am \xb7 progress saved",
             "usage"),
            ("You've hit your Fable limit \xb7 resets Sep 30, 9am "
             "(Europe/Bucharest)", "fable"),
            ("You've hit your Opus limit \xb7 resets Oct 1, 2:15pm", "opus"),
            ("You've hit your usage credit limit", "credit"),
            ("You've hit your monthly spend limit.", "usage"),
            ("You're out of usage credits \xb7 resets 3am", "credit"),
            (AUTO, ""),
            ("Usage limit reached \xb7 wrapping up", ""),
            ("Usage limit reached", ""),
            ("Your usage limit has reset \xb7 press enter to continue", "")]:
        check(f"limit-hardening: recognised {line[:44]!r}",
              lb.banner_line("  ⎿\xa0" + line) != ""
              and lb.banner_window(line) == window, lb.banner_window(line))
    for line in ['Usage limit reached" cut-off',
                 "Usage limit reached. I'll pause here.",
                 "You've hit your limit on retries, so I stopped",
                 "You've hit your fast limit \xb7 using the standard model",
                 "Approaching session limit \xb7 resets 3am",
                 "You've used 90% of your weekly limit \xb7 resets Sep 30, 9am",
                 "● I think you've hit your session limit here",
                 "Usage limit reset \xb7 continuing automatically"]:
        check(f"limit-hardening: NOT a cut-off {line[:44]!r}",
              lb.banner_line(line) == "")
    check("limit-hardening: the CLI's own reset notice is recognised as the "
          "END of a cut-off, spaced or not",
          lb.is_reset_notice("Usage limit reset \xb7 continuing automatically")
          and lb.is_reset_notice("Usagelimitreset\xb7continuingautomatically"))

    yr = _time.localtime(lb.parse_reset_clock(
        "You've hit your weekly limit \xb7 resets Jan 2, 2031, 3:15pm"))
    check("limit-hardening: a dated clock with a year resolves exactly",
          (yr.tm_year, yr.tm_mon, yr.tm_mday, yr.tm_hour, yr.tm_min)
          == (2031, 1, 2, 15, 15))
    check("limit-hardening: a dated clock is exact, a bare one is not",
          lb.reset_is_dated("You've hit your Fable limit \xb7 resets Sep 30, "
                            "9am") and not lb.reset_is_dated(SESSION))
    check("limit-hardening: 'press enter to continue' and 'continuing "
          "shortly' are due now",
          lb.banner_due_now("Your usage limit has reset \xb7 press enter to "
                            "continue")
          and lb.banner_due_now("Usage limit reached \xb7 continuing shortly "
                                "\xb7 esc to cancel")
          and not lb.banner_due_now(AUTO))
    region = ("the cache resets 11pm nightly, so rerun after\n"
              "You've hit your session limit\n  ? for shortcuts\n")
    check("limit-hardening: the clock comes from the banner, never from "
          "another 'resets' elsewhere on screen",
          lb.parse_reset_clock(lb.banner_clock_text(region)) is None)
    check("limit-hardening: a banner wrapped before its clock still yields "
          "the clock from the next row",
          hm(lb.parse_reset_clock(lb.banner_clock_text(
              "You've hit your session limit \xb7 resets\n"
              "6:40pm (Europe/Bucharest)"))) == (18, 40))

    # --- 3. the live path, end to end through _on_pty_output ----------------
    def mk(name):
        a = TerminalAgent(build_spec(AgentKind.CLAUDE, name, cwd=cwd))
        a._prompt_ready = True
        a.status = AgentStatus.RUNNING
        return a

    live = mk("Live")
    live._on_pty_output("pty", "\r\n  ⎿" + jumps(SESSION, 5) + "\r\n")
    check("limit-hardening: a jump-painted banner arriving on the pty "
          "latches", live.is_limit_blocked())
    check("limit-hardening: ...with the clock it stated",
          hm(live.limit_resets_at()) == (18, 40))
    live.clear_limit_block()             # what a verified resume does
    live._screen_tail = SESSION + "\n  ? for shortcuts\n"   # repainted spaced
    live._scrape_limit()
    check("limit-hardening: the same banner repainted WITH spaces is not a "
          "new cut-off (no re-latch after a resume)",
          not live.is_limit_blocked())

    prose = mk("Prose")
    prose._screen_tail = 'Usage limit reached" cut-off\nmore prose\n'
    prose._scrape_limit()
    check("limit-hardening: an agent quoting 'Usage limit reached' is not "
          "latched (happened twice on 2026-08-31)",
          not prose.is_limit_blocked())

    unrelated = mk("Unrelated")
    unrelated._screen_tail = ("the token resets Sep 4, 3:59am\n"
                              "You've hit your session limit\n")
    unrelated._scrape_limit()
    check("limit-hardening: a latch never borrows another line's clock",
          unrelated.is_limit_blocked()
          and unrelated.limit_resets_at() is None)

    weekly = mk("WeeklyBare")
    weekly._screen_tail = "You've hit your weekly limit \xb7 resets 8pm\n"
    weekly._scrape_limit()
    check("limit-hardening: a weekly banner with a bare clock is NOT exact",
          weekly.is_limit_blocked() and weekly.limit_window() == "weekly"
          and not weekly.limit_reset_exact())
    fable = mk("FableDated")
    fable._screen_tail = ("You've hit your Fable limit \xb7 resets Sep 30, "
                          "9am\n")
    fable._scrape_limit()
    check("limit-hardening: a dated 7-day banner IS exact",
          fable.limit_window() == "fable" and fable.limit_reset_exact())
    stale = mk("Stale")
    stale._screen_tail = ("Your usage limit has reset \xb7 press enter to "
                          "continue\n")
    stale._scrape_limit()
    check("limit-hardening: 'press enter to continue' latches as due now",
          stale.is_limit_blocked()
          and (stale.limit_resets_at() or 1e18) <= _time.time() + 1)

    # --- 4. the conversation on disk ----------------------------------------
    def iso(at):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(at))

    def write(sid, records):
        path = transcripts.transcript_path(cwd, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for r in records:
                fh.write(_json.dumps(r) + "\n")

    def user(text, at, **extra):
        return dict({"type": "user", "timestamp": iso(at),
                     "message": {"role": "user", "content": text}}, **extra)

    def reply(text, at):
        return {"type": "assistant", "timestamp": iso(at),
                "message": {"content": [{"type": "text", "text": text}]}}

    def system(text, at, sub="informational"):
        return {"type": "system", "subtype": sub, "content": text,
                "timestamp": iso(at)}

    def real_429(text, at, reset, kind="five_hour"):
        """A cut-off exactly as claude.exe writes it (fields read off a real
        transcript from 2026-08-31)."""
        return {"type": "assistant", "timestamp": iso(at),
                "error": "rate_limit", "isApiErrorMessage": True,
                "apiErrorStatus": 429,
                "quotaLimits": {"status": "rejected", "resetsAt": int(reset),
                                "rateLimitType": kind},
                "message": {"model": "<synthetic>",
                            "content": [{"type": "text", "text": text}]}}

    at = now - 600
    ask = user("build the parser", at - 5)
    opus = real_429("You've hit your Opus limit \xb7 resets 8pm", at,
                    now + 3 * 86400, "seven_day_opus")
    write("sid-quota", [ask, opus])
    info = transcripts.limit_cut_off(cwd, "sid-quota")
    check("limit-hardening: a real 429 record's quotaLimits epoch wins over "
          "the bare printed clock", info["cut_off"]
          and info["resets_at"] == int(now + 3 * 86400) and info["exact"]
          and info["window"] == "opus", info)
    for name, rec in [
            ("an away_summary recap",
             system("Was building the parser; the limit stopped it.",
                    at + 1800, "away_summary")),
            ("'Remote Control disconnected'",
             system("Remote Control disconnected — /login", at + 3600)),
            ("a local slash command's output",
             system("<local-command-stdout></local-command-stdout>",
                    at + 60, "local_command"))]:
        sid = "sid-after-" + str(abs(hash(name)))
        write(sid, [ask, opus, rec])
        check(f"limit-hardening: a cut-off followed by {name} is STILL a "
              f"cut-off", transcripts.limit_cut_off(cwd, sid)["cut_off"])
    write("sid-prose", [ask, reply(SESSION, at)])
    check("limit-hardening: an ordinary reply that merely starts with the "
          "banner's words is not a cut-off on disk",
          not transcripts.limit_cut_off(cwd, "sid-prose")["cut_off"])
    write("sid-self", [ask, system(AUTO, at),
                       system("Usage limit reset \xb7 continuing "
                              "automatically", at + 3600)])
    selfinfo = transcripts.limit_cut_off(cwd, "sid-self")
    check("limit-hardening: the CLI continuing by itself reads as over, and "
          "says so", not selfinfo["cut_off"] and selfinfo["self_resumed"])
    wrap = user("[Usage limit reached — grace window active. Wrap up.]",
                at, isMeta=True, usageLimitNote="wrap_up")
    write("sid-grace", [ask, wrap, reply("Checkpoint: parser half done.",
                                         at + 30)])
    check("limit-hardening: a grace-window wrap-up is a cut-off, and its own "
          "wrap-up reply does not end it",
          transcripts.limit_cut_off(cwd, "sid-grace")["cut_off"])
    write("sid-grace-on", [ask, wrap, reply("Checkpoint.", at + 30),
                           user("go on", at + 7200), reply("On it.",
                                                           at + 7210)])
    check("limit-hardening: ...until a new prompt gets a real reply",
          not transcripts.limit_cut_off(cwd, "sid-grace-on")["cut_off"])

    # --- 5. the window: the sweep, the 7-day rule, the Esc, self-resume -----
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show(); pump(50)
    ws = win.manager.workspaces[0]
    sent: dict = {}

    def agent_for(name, sid):
        spec = build_spec(AgentKind.CLAUDE, name, cwd=cwd, pty=True)
        spec.session_id = sid
        a = TerminalAgent(spec)
        buf = sent.setdefault(name, [])
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: buf.append(d) or True,
            "job_process_count": lambda s: 0,
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        a.status = AgentStatus.RUNNING
        ws.agents.append(a)
        return a

    typed = lambda name: "".join(sent.get(name, []))

    write("sid-missed", [ask, real_429(SESSION, now - 5, now - 300)])
    missed = agent_for("Missed", "sid-missed")
    write("sid-before", [ask, real_429(SESSION, win._launched_at - 7200,
                                       now - 3600)])
    before = agent_for("Before", "sid-before")
    busy_sid = "sid-busy"
    write(busy_sid, [ask, real_429(SESSION, now - 5, now - 300)])
    busy = agent_for("Busy", busy_sid)
    busy._busy = True
    check("limit-hardening: the transcript sweep adopts exactly the missed "
          "cut-off", win._sweep_transcript_cut_offs() == 1)
    check("limit-hardening: ...latched as a LIVE cut-off with its exact "
          "reset", missed.is_limit_blocked()
          and not missed.limit_from_startup() and missed.limit_reset_exact()
          and missed.limit_resets_at() == int(now - 300))
    check("limit-hardening: ...never one from before this run (startup "
          "recovery's call)", not before.is_limit_blocked())
    check("limit-hardening: ...never a busy agent", not busy.is_limit_blocked())
    check("limit-hardening: ...and files it in the ledger as found by the "
          "sweep", any(r.get("source") == "sweep" and
                       r.get("session_id") == "sid-missed"
                       for r in limit_ledger.read_all(str(tmp))))
    key = win._ledger_key(missed)
    missed.clear_limit_block()
    limit_ledger.record_outcome(str(tmp), key, limit_ledger.FAILED, tries=4)
    check("limit-hardening: a cut-off the ledger already closed is never "
          "re-armed by the sweep",
          win._sweep_transcript_cut_offs() == 0
          and not missed.is_limit_blocked())

    # 7-day windows: a bare clock waits for the account, an exact one does not
    bare = agent_for("WeeklyBare", "sid-none-1")
    bare.mark_limit_blocked(now - 3600, from_startup=False, window="weekly",
                            banner="You've hit your weekly limit \xb7 resets "
                                   "8pm", exact=False)
    dated = agent_for("WeeklyDated", "sid-none-2")
    dated.mark_limit_blocked(now - 3600, from_startup=False, window="weekly",
                             banner="You've hit your weekly limit \xb7 "
                                    "resets Sep 30, 9am", exact=True)
    win._check_limit_resets()
    pump(2600)
    check("limit-hardening: a weekly cut-off on a BARE clock is not nudged "
          "on that clock", typed("WeeklyBare") == "")
    check("limit-hardening: a weekly cut-off with an EXACT reset is nudged "
          "once it passes", "Continue" in typed("WeeklyDated"))
    check("limit-hardening: ...and gets no Esc, since no menu is up",
          "\x1b" not in typed("WeeklyDated"))

    write("sid-self", [ask, system(AUTO, now - 7200),
                       system("Usage limit reset \xb7 continuing "
                              "automatically", now - 60)])
    selfr = agent_for("SelfResumed", "sid-self")
    selfr.mark_limit_blocked(now - 120, from_startup=False, banner=AUTO,
                             exact=True)
    win._resume_blocked_agents()
    pump(2600)
    check("limit-hardening: an agent the CLI already continued is not "
          "nudged again", typed("SelfResumed") == ""
          and not selfr.is_limit_blocked())
    check("limit-hardening: ...and the ledger records it RESUMED by the CLI",
          any(r.get("event") == limit_ledger.RESUMED
              and "on its own" in r.get("detail", "")
              for r in limit_ledger.read_all(str(tmp))))

    win._closing = True
    win.close()
    pump(50)


def test_limit_echo_after_self_continue():
    """2026-09-25: the hourglass stayed on the Lifting App and TBE Site agents
    after Claude Code continued on its own at the 5:50am reset.

    Claude draws a cut-off twice: "You've hit your session limit \xb7 resets
    5:50am" in the conversation, and the status row "Usage limit reached \xb7
    continuing automatically at 5:50am" below it. The screen latched on the
    status row. At the reset the CLI removed that row and got back to work,
    so the older line became the last banner in view. Its wording differs, so
    `same_banner` called it a NEW cut-off, and with its clock just passed it
    was dated 24 h out. Nothing re-checked the latch before then.
    """
    import json as _json
    import time as _time
    from types import SimpleNamespace
    from PySide6.QtWidgets import QApplication
    from app import limit_banner as lb
    from app import limit_ledger, transcripts
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent
    from app.widgets.main_window import MainWindow

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-limit-echo-"))
    cwd = str(tmp)
    now = int(_time.time())

    def clock(at):
        return _time.strftime("%I:%M%p", _time.localtime(at)).lstrip("0").lower()

    def hit(at):
        return f"You've hit your session limit \xb7 resets {clock(at)}"

    def status(at):
        return (f"Usage limit reached \xb7 continuing automatically at "
                f"{clock(at)} \xb7 esc to cancel")

    def mk(name):
        a = TerminalAgent(build_spec(AgentKind.CLAUDE, name, cwd=cwd))
        a._prompt_ready = True
        a.status = AgentStatus.RUNNING
        a.spec.session_id = "sid-" + name.lower()
        return a

    def show(agent, text):
        agent._screen_tail = text + "\n  ? for shortcuts\n"
        agent._scrape_limit()

    reset = now + 60
    PARKED = hit(reset) + "\n" + status(reset)
    WORKING = hit(reset) + "\nMoved the date row into the Today tab."

    # --- the clock identity -------------------------------------------------
    check("limit-echo: the status row and the conversation's line name the "
          "same clock", lb.banner_clock_key(hit(reset))
          == lb.banner_clock_key(status(reset)) != "")
    check("limit-echo: ...which same_banner alone cannot see",
          not lb.same_banner(hit(reset), status(reset)))
    check("limit-echo: '5am' and '5:00am' are one clock",
          lb.banner_clock_key("resets 5am") == lb.banner_clock_key(
              "resets 5:00am") == "5:00am")
    check("limit-echo: a jump-painted row keys like a spaced one",
          lb.banner_clock_key("You'vehityoursessionlimit\xb7resets5:50am")
          == lb.banner_clock_key("You've hit your session limit \xb7 "
                                 "resets 5:50am"))
    check("limit-echo: a dated clock keeps its date",
          lb.banner_clock_key("resets Sep 30, 9am") == "sep30,9:00am")
    check("limit-echo: a clockless banner has no key",
          lb.banner_clock_key("You've hit your session limit") == "")

    # --- the live screen ----------------------------------------------------
    a = mk("Echo")
    show(a, PARKED)
    check("limit-echo: the parked agent latches on the status row",
          a.is_limit_blocked() and lb.banner_key(
              a.limit_banner_text()).startswith("usagelimitreached"),
          a.limit_banner_text())
    a.clear_limit_block()                  # the PHANTOM dismissal at 5:50
    show(a, WORKING)                       # the CLI continued, row gone
    check("limit-echo: the conversation's own line, left as the last banner "
          "after the CLI continues, does NOT re-latch", not a.is_limit_blocked())
    show(a, hit(now + 3 * 3600))
    check("limit-echo: a banner with a different clock still latches",
          a.is_limit_blocked())

    b = mk("NextDay")
    show(b, PARKED)
    b.clear_limit_block()
    b._limit_last_reset -= 20 * 3600       # that reset was 20 h ago
    show(b, WORKING)
    check("limit-echo: the same clock a day later is a genuine new cut-off",
          b.is_limit_blocked())

    r = mk("Restart")
    show(r, PARKED)
    r.clear_limit_block()
    r._forget_limit_echo()                 # what start()/restart() do
    show(r, WORKING)
    check("limit-echo: a (re)start forgets the clock too", r.is_limit_blocked())

    d = mk("Disk")
    d.mark_limit_blocked(float(reset), banner=hit(reset) + " (Europe/Bucharest)")
    d.clear_limit_block()
    show(d, status(reset))
    check("limit-echo: a latch recovered from disk arms the clock guard too",
          not d.is_limit_blocked())

    # --- the conversation on disk -------------------------------------------
    def iso(at):
        return _time.strftime("%Y-%m-%dT%H:%M:%S.000Z", _time.gmtime(at))

    def write(sid, records):
        path = transcripts.transcript_path(cwd, sid)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(_json.dumps(_as_cli_wrote(rec)) + "\n")

    def user(text, at):
        return {"type": "user", "timestamp": iso(at),
                "message": {"role": "user", "content": text}}

    def reply(text, at):
        return {"type": "assistant", "timestamp": iso(at),
                "message": {"content": [{"type": "text", "text": text}]}}

    def notice(at):
        return {"type": "system", "subtype": "informational", "timestamp":
                iso(at), "content": "Usage limit reset \xb7 continuing "
                                    "automatically"}

    ask, cut = user("fix the date row", now - 300), reply(hit(reset), now - 290)
    write("sid-tx-on", [ask, cut, notice(now + 50), reply("Moved it.", now + 70)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-on")
    check("limit-echo: work after a cut-off dates when it carried on",
          not info["cut_off"] and info["carried_on_at"] == now + 70, info)
    write("sid-tx-stuck", [ask, cut])
    check("limit-echo: a conversation still on its cut-off never carried on",
          transcripts.limit_cut_off(cwd, "sid-tx-stuck")["carried_on_at"]
          == 0.0)
    write("sid-tx-self", [ask, cut, notice(now + 50)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-self")
    check("limit-echo: the CLI's reset notice counts as carrying on",
          info["self_resumed"] and info["carried_on_at"] == now + 50, info)

    # --- a background task hitting the same spent limit later ---------------
    # The same morning: the real cut-off at 03:16 stopped work, a background
    # command's notification hit the limit again at 05:16, and the reader
    # let that plumbing turn replace the real cut-off. AI Hive then dismissed
    # all three agents as PHANTOM and would never have resumed them.
    def notif(at):
        return user("<task-notification> <task-id>b1</task-id> "
                    "</task-notification>", at)

    def quota(at, reset):
        return dict(reply(hit(reset), at), quotaLimits={
            "status": "rejected", "resetsAt": int(reset),
            "rateLimitType": "five_hour"})

    write("sid-tx-bg", [ask, cut, notif(now - 100), reply(hit(reset), now - 99)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-bg")
    check("limit-echo: a background task hitting the limit again does not "
          "cancel the real cut-off before it", info["cut_off"]
          and not info["synthetic"] and info["at"] == now - 290, info)
    write("sid-tx-bg-later", [ask, quota(now - 290, now + 60), notif(now - 100),
                              quota(now - 99, now + 7200)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-bg-later")
    check("limit-echo: ...and takes the later reset the refusal stated",
          info["cut_off"] and info["resets_at"] == now + 7200
          and info["at"] == now - 290, info)
    write("sid-tx-bg-only", [ask, reply("Done.", now - 290), notif(now - 100),
                             reply(hit(reset), now - 99)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-bg-only")
    check("limit-echo: with nothing open, a background task's cut-off is "
          "still synthetic", not info["cut_off"] and info["synthetic"], info)
    write("sid-tx-bg-work", [ask, cut, reply("Picked it back up.", now - 200),
                             notif(now - 100), reply(hit(reset), now - 99)])
    info = transcripts.limit_cut_off(cwd, "sid-tx-bg-work")
    check("limit-echo: real work between the two means the first cut-off "
          "was over", not info["cut_off"] and info["synthetic"], info)

    # --- the minute watchdog clears a latch the conversation moved past -----
    agents, audits, outcomes = [], [], []
    stub = SimpleNamespace(
        manager=SimpleNamespace(all_agents=lambda: agents),
        _resume_pending=set(), _limit_audit=audits.append,
        _ledger_outcome=lambda ag, o, detail="":
            outcomes.append((ag.spec.name, o)))

    def latched(name, records):
        ag = mk(name)
        write(ag.spec.session_id, records)
        show(ag, PARKED)
        agents.append(ag)
        return ag

    on = latched("On", [ask, cut, reply("Moved it.", now + 60)])
    selfr = latched("Self", [ask, cut, notice(now + 60)])
    unflushed = latched("Unflushed", [ask, reply("working", now - 100)])
    stuck = latched("Stuck", [ask, cut])
    nudged = latched("Nudged", [ask, cut, reply("Moved it.", now + 60)])
    nudged.note_limit_attempt()
    pending = latched("Pending", [ask, cut, reply("Moved it.", now + 60)])
    stub._resume_pending.add(pending.id)
    cleared = MainWindow._clear_carried_on_latches(stub)
    check("limit-echo: a latch with work written after it is cleared",
          not on.is_limit_blocked()
          and ("On", limit_ledger.DISMISSED) in outcomes, outcomes)
    check("limit-echo: ...and one the CLI resumed itself is filed as resumed",
          not selfr.is_limit_blocked()
          and ("Self", limit_ledger.RESUMED) in outcomes, outcomes)
    check("limit-echo: work from BEFORE the latch (banner not flushed yet) "
          "keeps it", unflushed.is_limit_blocked())
    check("limit-echo: a conversation still ending on the cut-off keeps it",
          stuck.is_limit_blocked())
    check("limit-echo: a nudge already sent is left to its verify",
          nudged.is_limit_blocked() and pending.is_limit_blocked())
    check("limit-echo: exactly those two, each with an audit line",
          cleared == 2 and len(audits) == 2
          and any("CARRIED-ON" in x for x in audits), audits)
    shutil.rmtree(tmp, ignore_errors=True)
