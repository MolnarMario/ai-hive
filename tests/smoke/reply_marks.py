"""Prompt and reply marks: when a turn earns a mark, reply timestamps, and
recovering marks from transcripts after a reopen."""

import os
import shutil
import tempfile
import time
from pathlib import Path

from .harness import SCRATCH_CWD, check


def test_reply_marks_need_a_submitted_turn():
    """A reply stamp is only minted for a turn somebody actually ASKED for.

    A busy -> idle settle is 2 s of quiet and nothing more, and a launch
    produces several that are not replies: a --resume launch reprints the whole
    past conversation as real output, pausing while Claude loads the transcript
    and again once the reprint ends, and settle_layout then hands every card
    its real width, which makes the child redraw its entire frame. The old
    guard suppressed only the FIRST settle of a resumed launch, so reopening
    the app stamped the CURRENT time over the last reply in every terminal --
    live-reported, and the check below that settles TWICE is the one that
    reproduces it.

    Second half: ONE stamp per turn. Claude falls quiet mid-reply whenever a
    tool runs longer than the idle window, and each of those lulls used to mint
    its own mark, so a single long reply wore a stamp at every pause it took
    instead of one where it ended."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus, submits_a_line
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # ---- what counts as a submit -----------------------------------------
    check("reply-turn: a bare CR submits", submits_a_line("hi\r"))
    check("reply-turn: Shift/Alt+Enter (ESC-CR) inserts a newline, no submit",
          not submits_a_line("\x1b\r"))
    check("reply-turn: Ctrl+Enter (LF) inserts a newline, no submit",
          not submits_a_line("\n"))
    check("reply-turn: a CR inside a bracketed paste is pasted text, no submit",
          not submits_a_line("\x1b[200~one\rtwo\x1b[201~"))
    check("reply-turn: ...but the Enter that follows the paste does submit",
          submits_a_line("\x1b[200~one\rtwo\x1b[201~\r"))

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "ResumeSettle", cwd=SCRATCH_CWD,
                                 pty=True))
    a.status = AgentStatus.RUNNING
    a._resume_attempt = True          # simulate a --resume launch
    a._turn_open = False              # nothing has been asked of it yet

    # the resume replay arrives as real output and falls quiet, TWICE: Claude
    # pauses while it loads the conversation, then again once it has reprinted
    # it. The old one-shot guard let the second one through.
    for burst in ("Claude Code v2.1 ...", "...replayed conversation..."):
        a._on_pty_output("pty", burst)
        a._on_idle_timeout()
    check("reply-turn: a resumed launch's replay settles mint no milestone",
          a.reply_marks() == [], a.reply_marks())

    # a card being sized into its real width makes the child repaint, which
    # settles again -- still nobody asked it anything
    a._on_pty_output("pty", "...full-frame repaint after the resize...")
    a._on_idle_timeout()
    check("reply-turn: ...nor does a repaint after the launch resize",
          a.reply_marks() == [], a.reply_marks())

    # ---- a real turn: submit, output, settle ------------------------------
    a.write("do the thing\r")
    check("reply-turn: a submitted line opens a turn", a._turn_open)
    # the real reply lands well after the pty echoed the keystrokes back, so
    # _mark_busy reads it as work rather than echo (see INPUT_ECHO_S)
    a._last_input_ts = 0.0
    a._on_pty_output("pty", "a real new reply")
    before = time.time()
    a._on_idle_timeout()
    check("reply-turn: the settle after a submit records one milestone",
          len(a.reply_marks()) == 1, a.reply_marks())
    first = a.reply_marks()[0]
    check("reply-turn: ...stamped with a recent walltime",
          before - 1 <= first.ts <= time.time() + 1)
    first_uid, first_pos, first_ts = first.uid, first.pos, first.ts

    # ---- a lull INSIDE that turn moves the mark, it does not add one ------
    a._on_pty_output("pty", "...still the same reply, after a slow tool...")
    a._on_idle_timeout()
    check("reply-turn: a second settle in the same turn keeps ONE mark",
          len(a.reply_marks()) == 1, a.reply_marks())
    moved = a.reply_marks()[0]
    check("reply-turn: ...and moves it to where the reply really ended",
          moved.uid == first_uid and moved.pos > first_pos
          and moved.ts >= first_ts, (first_pos, first_ts, moved))

    # ---- the next submit starts a new turn, and a new mark ---------------
    a.write("and now this\r")
    a._last_input_ts = 0.0
    a._on_pty_output("pty", "the second reply")
    a._on_idle_timeout()
    check("reply-turn: a fresh submit starts a fresh mark",
          len(a.reply_marks()) == 2, a.reply_marks())

    # ---- a restart closes the turn --------------------------------------
    # a stub: from IDLE, PtyWorker.restart() STARTS a real claude
    a.worker.restart = lambda: None
    a.restart()
    check("reply-turn: a restart closes the open turn",
          not a._turn_open and a._turn_mark_uid is None)
    a.status = AgentStatus.RUNNING
    a._on_pty_output("pty", "output from the fresh session")
    a._on_idle_timeout()
    check("reply-turn: ...so its output settles without a stamp",
          a.reply_marks() == [], a.reply_marks())

    a.dispose()


def test_reply_marks_recovered_after_reprint():
    """A conversation REPRINTED after its card was built still gets its marks.

    Milestone recovery rides a projection, and the launch autostart leaves a
    restored RUNNING card with none: `drop_restored_screen` cancels the
    settled-size projection (that snapshot is the previous run's screen, wrapped
    for a width nothing can reflow) and `_reproject_on_size` bails while the
    history is empty, which it is, because `settle_layout` sizes the card before
    the child is spawned. The conversation then arrives seconds later from
    `--resume`, with nothing left to scan it -- so a reopened hive showed no
    stamp and no prompt dot anywhere. It went unnoticed only because a phantom
    live mark used to land on the last reply instead, stamped with the launch
    time. `_rescan_recovery` looks again once the reprint settles."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent
    from app.widgets.terminal_card import (_RECOVER_RESCAN_TRIES, TerminalCard,
                                           _format_reply_stamp)

    QApplication.instance() or QApplication([])
    spec = build_spec(AgentKind.CLAUDE, "Reopen", cwd=SCRATCH_CWD, pty=True)
    spec.session_id = "11111111-2222-3333-4444-555555555555"
    agent = TerminalAgent(spec)
    card = TerminalCard(agent)
    card.resize(900, 500)
    card._proj_cols = card.terminal.screen.columns
    # the transcript says this conversation's one reply finished two hours ago
    when = time.time() - 7200
    card._recover_key = (spec.provider, spec.cwd, spec.session_id)
    card._recover_prompts = []
    card._recover_replies = [(when, "Done. The fix is in and the suite passes.")]

    card.drop_restored_screen()      # what autostart does before start()
    check("reply-reprint: the launch autostart leaves the card with no marks",
          card.terminal.reply_marks() == [], card.terminal.reply_marks())

    # ...and only THEN does the child reprint the resumed conversation
    agent.status = AgentStatus.RUNNING
    agent._on_pty_output("pty", "> do the thing\r\n\r\n"
                         "● Done. The fix is in and the suite passes."
                         "\r\n\r\n✳ Crunched for 41s\r\n\r\n"
                         + "─" * 40 + "\r\n> ")
    agent._on_idle_timeout()         # the settle after the reprint
    check("reply-reprint: the settle after the reprint recovers the stamp",
          card.terminal.reply_marks() == [(5, _format_reply_stamp(when))],
          card.terminal.reply_marks())
    check("reply-reprint: ...with the TRANSCRIPT's time, not the launch clock",
          _format_reply_stamp(when) != _format_reply_stamp(time.time()))
    check("reply-reprint: a scan that found something never runs again",
          card._recover_tries < _RECOVER_RESCAN_TRIES)
    agent._on_idle_timeout()
    check("reply-reprint: ...and the recovered stamp survives later settles",
          card.terminal.reply_marks() == [(5, _format_reply_stamp(when))],
          card.terminal.reply_marks())
    card._rescan_recovery()          # a settle with marks already anchored
    check("reply-reprint: ...with the budget closed for good",
          card._recover_tries == 0)

    # a conversation whose replies are all out of reach stops asking rather
    # than re-scanning on every settle for the life of the process
    other = TerminalCard(agent)
    other.resize(900, 500)
    other._recover_key = card._recover_key
    other._recover_prompts = []
    other._recover_replies = [(when, "a reply that scrolled away long ago")]
    for _ in range(_RECOVER_RESCAN_TRIES + 3):
        other._rescan_recovery()
    check("reply-reprint: an unfindable reply spends a bounded budget",
          other._recover_tries == 0 and other.terminal.reply_marks() == [],
          (other._recover_tries, other.terminal.reply_marks()))

    other.deleteLater()
    card.deleteLater()
    agent.dispose()


def test_reply_stamp_repaint_and_merge():
    """Two ways a stamp that was recorded correctly still reads wrong.

    First: setting marks has to REPAINT. `_notify_view`'s signature is
    (pushed, history, offset, lines) and marks are not in it, so on a quiet
    screen it returns at its own guard, and even when it does emit,
    `viewChanged` goes to the scrollbar rather than to this widget's paint
    queue. A reply stamp is minted 2 s after the last output, by which time the
    repaint that last feed() scheduled has already run, so without an explicit
    update() the stamp sat unpainted until something unrelated repainted the
    card -- flaky-looking, on exactly the agent that has just gone quiet.

    Second: a live mark and a recovered one are the SAME reply when they are
    within _STAMP_MERGE_SLACK, not only when the rows match exactly. The two
    anchors fall back differently when the row under Claude's footer is not
    blank (recovery goes above the footer, the live path onto it), and
    requiring equality kept both -- one reply wearing two stamps with two
    different times, the wrong one rendering."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import (_STAMP_MERGE_SLACK, TerminalCard,
                                           _format_reply_stamp)

    QApplication.instance() or QApplication([])
    # a scratch cwd, never the repo: a card reads the transcripts of whatever
    # folder its spec names, and this suite runs INSIDE a real conversation
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-stamps-"))
    spec = build_spec(AgentKind.CLAUDE, "Stamps", cwd=str(tmp), pty=True)
    spec.session_id = "99999999-8888-7777-6666-555555555555"
    agent = TerminalAgent(spec)
    card = TerminalCard(agent)
    card.resize(900, 500)
    view = card.terminal

    # counting paints means shadowing update() on the instance. Restore it by
    # DELETING the attribute, never by assigning the bound method back: that
    # leaves view.__dict__["update"] holding a method bound to view, i.e. a
    # reference cycle that outlives deleteLater() and keeps a dead card's
    # widget tree alive for the rest of the process.
    painted = []
    real_update = view.update
    view.update = lambda *a, **k: (painted.append(1), real_update(*a, **k))[1]
    view.set_reply_marks([(3, "Aug 19, 19:14")])
    check("reply-paint: setting a reply stamp asks for a repaint", painted)
    # the SAME view state twice: _notify_view returns at its signature guard,
    # so only the explicit update() can carry the second one to the screen
    before = len(painted)
    view.set_reply_marks([(4, "Aug 19, 19:15")])
    check("reply-paint: ...even when the view signature has not moved",
          len(painted) > before, (before, len(painted)))
    before = len(painted)
    view.set_marks([(3, "do the thing")])
    check("reply-paint: a prompt milestone repaints too",
          len(painted) > before, (before, len(painted)))
    del view.update
    del real_update

    # ---- the merge -------------------------------------------------------
    when = time.time() - 7200        # what the transcript recorded
    agent._note_submit()             # a turn somebody asked for
    mark = agent.note_reply_settled()

    def stamps(live_row, recovered_rows):
        card._reply_mark_lines = {mark.uid: live_row}
        card._recovered_replies = [(r, when) for r in recovered_rows]
        card._refresh_reply_marks()
        return view.reply_marks()

    check("reply-merge: an exact row match takes the recorded time",
          stamps(12, [12]) == [(12, _format_reply_stamp(when))],
          view.reply_marks())
    check("reply-merge: ...and so does one a footer away",
          stamps(12, [12 - _STAMP_MERGE_SLACK]) ==
          [(12, _format_reply_stamp(when))], view.reply_marks())
    check("reply-merge: ...which is the LIVE row with the RECORDED time",
          _format_reply_stamp(when) != _format_reply_stamp(mark.ts))
    out = stamps(12, [12 - _STAMP_MERGE_SLACK - 1])
    check("reply-merge: a reply further off is a separate turn, not this one",
          out == sorted([(12 - _STAMP_MERGE_SLACK - 1,
                          _format_reply_stamp(when)),
                         (12, _format_reply_stamp(mark.ts))]), out)

    # one recovered reply can only ever be claimed once, so a second live mark
    # beside it keeps its own clock instead of stamping the same time twice
    agent._note_submit()
    second = agent.note_reply_settled()
    card._reply_mark_lines = {mark.uid: 12, second.uid: 13}
    card._recovered_replies = [(12, when)]
    card._refresh_reply_marks()
    out = view.reply_marks()
    check("reply-merge: a recovered reply is claimed by ONE live mark",
          out == sorted([(12, _format_reply_stamp(when)),
                         (13, _format_reply_stamp(second.ts))]), out)

    # detach() does NOT unhook the two mark signals _wire() connects, so drop
    # them by hand as well; a card left listening to an agent it no longer
    # paints is how a torn-down widget keeps taking work in later checks
    for sig, slot in ((agent.prompt_marks_changed, card._refresh_marks),
                      (agent.reply_marks_changed, card._on_reply_mark_added)):
        try:
            sig.disconnect(slot)
        except (RuntimeError, TypeError):
            pass
    card.detach()
    card.deleteLater()
    agent.dispose()
    shutil.rmtree(tmp, ignore_errors=True)


def test_transcript_reply_times():
    """transcripts.reply_times reads when each reply ACTUALLY finished out of
    the conversation on disk -- the only source that survives a restart, since
    the live stamp is minted from the clock at a settle this process watched.

    The shape that matters: a turn narrates BETWEEN its tool calls, so only the
    LAST assistant text before the next real user turn is a finished reply, and
    a tool RESULT (a user record) sits inside a turn rather than ending one."""
    import datetime as _dt
    import json
    import tempfile

    from app import transcripts

    def rec(**kw):
        return json.dumps(kw)

    stamp = "2026-08-19T12:%02d:00.000Z"

    def at(minute):
        return _dt.datetime.fromisoformat(
            (stamp % minute).replace("Z", "+00:00")).timestamp()

    lines = [
        # turn 1: a prompt, a mid-turn narration, a tool call/result, the reply
        rec(type="user", timestamp=stamp % 0, promptSource="typed",
            message={"role": "user", "content": "do the thing"}),
        rec(type="assistant", timestamp=stamp % 1,
            message={"content": [{"type": "text", "text": "Looking now."}]}),
        rec(type="user", timestamp=stamp % 2,
            message={"content": [{"type": "tool_result", "content": "ok"}]}),
        rec(type="assistant", timestamp=stamp % 3,
            message={"content": [{"type": "text", "text": "Done, all green."}]}),
        # a sub-agent's own turn must never count as this conversation's reply
        rec(type="assistant", timestamp=stamp % 4, isSidechain=True,
            message={"content": [{"type": "text", "text": "sidechain noise"}]}),
        # turn 2
        rec(type="user", timestamp=stamp % 5, promptSource="typed",
            message={"role": "user", "content": "and again"}),
        rec(type="assistant", timestamp=stamp % 6,
            message={"content": [{"type": "text", "text": "Second reply."}]}),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "conv.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        got = transcripts._read_reply_times(path)

    check("reply-times: one entry per FINISHED reply, not per assistant text",
          len(got) == 2, got)
    check("reply-times: a mid-turn narration is not a reply ending",
          [t for _, t in got] == ["Done, all green.", "Second reply."], got)
    check("reply-times: each carries the record's OWN timestamp",
          [w for w, _ in got] == [at(3), at(6)], got)
    check("reply-times: a tool result does not end a turn",
          got[0][0] == at(3), got[0])
    check("reply-times: a sidechain turn is skipped",
          all("sidechain" not in t for _, t in got), got)

    # a missing / unreadable transcript is "" rather than an exception
    check("reply-times: no transcript -> empty, never raises",
          transcripts.reply_times("C:/nope", "no-such-id") == [])


def test_recovered_prompts_are_cached():
    """_recover_marks must not re-read the transcript on every projection.

    It runs on every projection (card build, and every width change), the read
    is O(whole transcript), and transcripts' own (mtime,size) cache misses for
    exactly the agents that matter -- a LIVE agent rewrites its transcript
    continuously. Measured at 90-165ms on the user's real 50MB transcripts, on
    the GUI thread, per card. Re-reading within one conversation cannot find
    anything the card doesn't already know: it watched those prompts being
    typed, so they carry live marks that outrank a recovered one."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import transcripts
    from app.process_worker import AgentKind, build_spec
    from app.pty_worker import HAS_CONPTY
    from app.terminal_agent import TerminalAgent
    from app.widgets import terminal_card as tc

    if not HAS_CONPTY:
        return
    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aihive-recover-"))

    calls = []
    real = transcripts.typed_prompts

    def counting(cwd, sid):
        calls.append(sid)
        return ["count the reads"]

    transcripts.typed_prompts = counting
    try:
        agent = TerminalAgent(build_spec(
            AgentKind.CLAUDE, "Cached", cwd=str(tmp), pty=True))
        agent.spec.session_id = "conv-1"
        agent._pty_buffer.append("count the reads\r\n" + "x\r\n" * 200)
        agent._pty_bytes = agent._pty_total = 1
        card = tc.TerminalCard(agent)
        check("recover: the constructor's seed projection does NOT read the "
              "transcript at all", calls == [], calls)

        card._rerender_restored()
        first = len(calls)
        check("recover: the settled projection reads it once", first == 1, calls)

        for _ in range(5):
            card._recover_marks()
        check("recover: ...and further projections of the same conversation "
              "reuse it", len(calls) == first, calls)

        agent.spec.session_id = "conv-2"
        card._recover_marks()
        check("recover: a NEW conversation re-reads (a /clear or a pin change "
              "means different prompts)", len(calls) == first + 1, calls)
        card.detach(); agent.dispose()
    finally:
        transcripts.typed_prompts = real


def test_reply_marks_recovered_from_transcript():
    """A reply mark is minted from a busy -> idle settle, so a conversation
    restored from disk comes back with NONE -- and since a resume's replay
    settle is deliberately suppressed, a reopened hive showed no reply time
    anywhere. These are recovered by matching each transcript reply's CLOSING
    line against the scrollback.

    MEASURED, and it is why the anchor is what it is: Claude's own "for Ns"
    footer (what a LIVE mark anchors to) does not survive per turn into the
    scrollback -- across seven real captured screens at five widths, at most
    ONE was still present in 2000 lines. And matching a reply's HEAD found a
    stale narrower re-render first (pyte does not reflow, so a card resized
    mid-session keeps both) and stamped the middle of a paragraph."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent
    from app.widgets.terminal_card import (TerminalCard, _format_reply_stamp,
                                           _last_content_line, _norm_reply_line,
                                           _reply_end_row)

    QApplication.instance() or QApplication([])

    # ---- the normalizer drops what the renderer paints rather than prints --
    check("reply-recover: markdown syntax is dropped from both sides",
          _norm_reply_line("**Rebuilt `dist/x.zip`** from the *good* files")
          == "rebuilt dist/x.zip from the good files",
          _norm_reply_line("**Rebuilt `dist/x.zip`** from the *good* files"))
    check("reply-recover: the closing line is the last one with content on it",
          _last_content_line("first\n\nlast line\n\n  \n") == "last line")

    # ---- the anchor: tail on the row, blank row underneath ----------------
    rows = ["a stale truncated copy of the same rep",   # 0: no tail, no blank
            "more of the stale copy",                   # 1
            "the reply, wrapping over",                 # 2: real copy starts
            "two rows and ending here.",                # 3: the tail lives here
            "",                                         # 4: <- the anchor
            "> next prompt"]                            # 5
    check("reply-recover: anchors the blank row under the reply's last row",
          _reply_end_row(rows, "ending here.", 0) == 4,
          _reply_end_row(rows, "ending here.", 0))
    check("reply-recover: a row with no blank beneath is not a reply ending",
          _reply_end_row(["tail here", "still going"], "tail here", 0) is None)
    check("reply-recover: nothing matching -> no stamp rather than a guess",
          _reply_end_row(rows, "never written", 0) is None)

    # ---- end to end through a real card ----------------------------------
    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "ReplyRecover", cwd=SCRATCH_CWD,
                                     pty=True))
    card = TerminalCard(agent)
    card.resize(640, 420)

    when = time.time() - 3600
    # pretend the transcript said this, bypassing the per-conversation cache
    # read (the reader itself is covered by test_transcript_reply_times)
    card._recover_key = (agent.spec.cwd, agent.spec.session_id)
    card._recover_replies = [(when, "All four suites pass now.")]
    agent._on_pty_output("pty", "All four suites pass now.\r\n\r\n> ")
    card._recover_reply_marks()

    check("reply-recover: the past reply is located in the scrollback",
          len(card._recovered_replies) == 1, card._recovered_replies)
    line, stamped = card._recovered_replies[0]
    check("reply-recover: ...stamped with the TRANSCRIPT's time, not now",
          stamped == when, (stamped, when))
    check("reply-recover: ...on the blank row under the reply",
          line == agent_row_after(card, "All four suites pass now."), line)

    card._refresh_reply_marks()
    check("reply-recover: the view carries it as an inline stamp",
          (line, _format_reply_stamp(when)) in card.terminal.reply_marks(),
          card.terminal.reply_marks())

    # ---- the same placement when the footer IS in the scrollback ---------
    from app.widgets.terminal_view import is_reply_footer
    check("reply-recover: Claude's turn footer is recognised",
          is_reply_footer("✻ Worked for 16m 36s")
          and is_reply_footer("✻ Cooked for 8m 2s · 1 shell still running"))
    check("reply-recover: ...and the input box's own hints are not",
          not is_reply_footer("? for shortcuts")
          and not is_reply_footer("← for agents"))
    footered = ["the reply ends here.", "", "✻ Worked for 3m 13s", "", "> "]
    check("reply-recover: a footer moves the anchor down BELOW it",
          _reply_end_row(footered, "reply ends here.", 0) == 3,
          _reply_end_row(footered, "reply ends here.", 0))

    # a reply that is NOT on screen is skipped, never invented
    card._recovered_replies = []
    card._recover_replies = [(when, "a reply that scrolled away long ago")]
    card._recover_reply_marks()
    check("reply-recover: a reply not in the scrollback yields no stamp",
          card._recovered_replies == [], card._recovered_replies)

    # where a live mark and a recovered one land on the same row, the live
    # mark keeps the ROW (it anchored the screen it was looking at) and the
    # transcript supplies the TIME. Claude stamps every record it writes; a
    # live mark reads the wall clock at the settle, seconds late at best and
    # wrong outright for a settle that was never a reply. That is what makes a
    # stray stamp self-correcting on the next projection.
    card._recovered_replies = [(line, when)]
    card._reply_mark_lines = {}
    mark = agent.note_reply_settled()
    card._reply_mark_lines[mark.uid] = line
    card._refresh_reply_marks()
    check("reply-recover: the transcript's time wins the row over the clock",
          card.terminal.reply_marks() == [(line, _format_reply_stamp(when))]
          and abs(mark.ts - when) > 60,
          (card.terminal.reply_marks(), _format_reply_stamp(when)))

    card.deleteLater()
    agent.dispose()


def agent_row_after(card, text):
    """Absolute line of the blank row directly under `text` in a card's
    terminal -- the row test_reply_marks_recovered_from_transcript expects a
    recovered stamp to land on."""
    from app.widgets.terminal_card import _norm_reply_line

    oldest, raw = card._scrollback_rows()
    lines = [_norm_reply_line(t) for t in raw]
    needle = _norm_reply_line(text)
    for i, line in enumerate(lines):
        if needle and needle in line and i + 1 < len(lines) and not lines[i + 1]:
            return oldest + i + 1
    return -1


def test_reply_marks_inline():
    """Reply-finished milestones drawn INLINE in the terminal content -- a dim
    date-and-time stamp on the blank row directly UNDER Claude's own "for Ns"
    footer. This is the only reply-time surface there is: the card header's
    #CardReplyTime badge was removed at the user's request, along with the
    agent-side reply clock that fed it. Covers the anchor scan
    (TerminalView.reply_anchor_line), the shared stamp formatter, a card
    rebuild re-deriving the same anchor, and every reset path that must wipe
    a reply mark alongside a prompt mark."""
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import REPLY_MARK_CAP, TerminalAgent
    from app.widgets.terminal_card import TerminalCard, _format_reply_stamp
    from app.widgets.terminal_view import TerminalView

    QApplication.instance() or QApplication([])

    agent = TerminalAgent(build_spec(AgentKind.CLAUDE, "ReplyMarks", cwd=SCRATCH_CWD,
                                     pty=True))
    card = TerminalCard(agent)
    card.resize(640, 420)
    t = card.terminal

    # A settled turn in the shape Claude Code REALLY leaves on screen: the
    # reply, a blank, the "for Ns" footer, a blank, then the input box --
    # whose TOP BORDER (a rule) sits directly above the prompt row. That
    # border is the point of this fixture. An earlier version fed only
    # "footer, blank, > " with no border, so the anchor scan never met the
    # rule it stopped dead on in real use, and the test passed while the
    # live stamp silently never appeared. Do NOT simplify this back.
    # Fed through _on_pty_output (not terminal.feed directly) so it lands in
    # BOTH the live view (via the connected pty_output signal) and the
    # agent's own replay buffer -- real usage does the same, and the
    # rebuilt-card check below needs the replay half.
    agent._on_pty_output("pty", "● Hi!\r\n\r\n✳ Crunched for 58s"
                         "\r\n\r\n" + "─" * 40 + "\r\n> ")
    mark = agent.note_reply_settled()
    check("reply-mark: note_reply_settled records a mark",
          mark is not None and agent.reply_marks() == [mark])
    check("reply-mark: the fixture really has the box border in the way",
          t._row_is_rule(4) and t._input_block_span() == (5, 5),
          (t._input_block_span(),))
    # UNDER the footer, on the blank separator below it -- not beside the
    # footer, not above it wedged between the reply and its own footer
    # (where the user reported finding it), and not skipped because the
    # box border got in the way of the scan
    under_footer = t.abs_line_at_row(3)
    check("reply-mark: the card anchors it UNDER the footer row",
          card._reply_mark_lines.get(mark.uid) == under_footer,
          (card._reply_mark_lines, under_footer))
    check("reply-mark: the view carries exactly one inline stamp",
          t.reply_marks() == [(under_footer, _format_reply_stamp(mark.ts))],
          t.reply_marks())
    import datetime as _dt
    check("reply-mark: the stamp carries the DATE as well as the time",
          _format_reply_stamp(mark.ts) == _dt.datetime.fromtimestamp(
              mark.ts).strftime("%b %d, %H:%M"), _format_reply_stamp(mark.ts))

    # ---- a rule directly above the box (no footer line) anchors nothing --
    ruled = TerminalView(rows=10, cols=40)
    ruled.feed("─" * 20 + "\r\n> ")
    check("reply-mark: a rule row above the box is never mistaken for a footer",
          ruled.reply_anchor_line() is None)

    # ---- no live input box at all (e.g. settled on a menu) -> no anchor --
    menu = TerminalView(rows=10, cols=40)
    menu.feed("some output\r\n")
    check("reply-mark: no input box in view -> nothing to anchor to",
          menu.reply_anchor_line() is None)

    # ---- FIFO cap -----------------------------------------------------
    for _ in range(REPLY_MARK_CAP + 5):
        agent._note_submit()      # a new turn each time: one mark apiece
        agent.note_reply_settled()
    check("reply-mark: the mark list is FIFO-capped",
          len(agent.reply_marks()) == REPLY_MARK_CAP, len(agent.reply_marks()))

    # ---- a rebuilt card re-derives the SAME anchor from the pty replay ---
    card2 = TerminalCard(agent)
    card2.resize(640, 420)
    latest = agent.reply_marks()[-1]
    check("reply-mark: a rebuilt card recovers the latest mark's anchor",
          latest.uid in card2._reply_mark_lines, card2._reply_mark_lines)
    card2.deleteLater()

    # ---- resets: restart and history-clear wipe reply marks too ---------
    card.terminal.clear_history()
    check("reply-mark: clearing history wipes the card's reply-mark lines",
          card._reply_mark_lines == {} and card.terminal.reply_marks() == [])
    check("reply-mark: ...and the agent's own mark list",
          agent.reply_marks() == [])

    agent.note_reply_settled()
    # a stub: from IDLE, PtyWorker.restart() STARTS a real claude
    agent.worker.restart = lambda: None
    agent.restart()
    check("reply-mark: a restart clears reply marks too",
          agent.reply_marks() == [])

    card.deleteLater()


def test_reply_stamp_time_comes_from_stop_hook():
    """A Claude reply's stamp reads the moment its Stop hook fired, and later
    settles cannot move it.

    Live-reported: a reply that finished at 18:21 (the Stop hook's own line
    right above it said "18:21:47", Claude's footer said "done 6:21 PM") wore
    "Sep 29, 22:27", the moment its workspace was next opened. A settle is only
    2 s of quiet after ANY output, the turn stayed open until the next submit,
    and a workspace switch resizes the card so the full-screen TUI redraws its
    frame; every one of those settles moved the mark to the clock.

    Second half, same report, other workspace: NO stamps at all. A user-level
    Stop hook prints a "⎿  Stop says: ..." row directly under every reply, and
    transcript recovery required the very next row to be blank, so it never
    located a single reply. The rows below are the real shape, taken from the
    user's saved screen."""
    import json

    from PySide6.QtWidgets import QApplication

    from app import session_hook as sh
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent
    from app.widgets.terminal_card import _norm_reply_line, _reply_end_row
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])

    # ---- recovery: a hook row between the reply and its blank row ---------
    raw = ["  in docs/RELEASE-CHECKLIST.md.",
           "  ⎿ \xa0Stop says: · 2026-09-29 18:21:47",
           "",
           "  Worked for 21m 39s · done 6:21 PM",
           "",
           "  recap: You asked me to finish the split.",
           "> "]
    rows = [_norm_reply_line(t) for t in raw]
    check("stop-stamp: a Stop hook row under the reply no longer hides it",
          _reply_end_row(rows, "release-checklist.md.", 0) == 4,
          _reply_end_row(rows, "release-checklist.md.", 0))
    no_footer = rows[:3] + ["> "]
    check("stop-stamp: ...and without a footer the blank row under the hook",
          _reply_end_row(no_footer, "release-checklist.md.", 0) == 2,
          _reply_end_row(no_footer, "release-checklist.md.", 0))
    check("stop-stamp: a reply that carries on below is still not an ending",
          _reply_end_row(["tail here", "more prose", ""], "tail here", 0)
          is None)

    # ---- live: the hook's time, and nothing moves it afterwards -----------
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "StopStamp", cwd=SCRATCH_CWD,
                                 pty=True))
    a.status = AgentStatus.RUNNING
    ended = time.time() - 4 * 3600      # 18:21, seen from 22:27

    # the usual order: the hook is polled in while the reply is still drawing
    a._note_submit()
    a._busy = True
    a.note_reply_stopped(ended)
    check("stop-stamp: a Stop before the settle mints nothing yet",
          a.reply_marks() == [], a.reply_marks())
    a._busy = False
    mark = a.note_reply_settled()
    check("stop-stamp: the settle after the hook carries the HOOK's time",
          mark is not None and mark.ts == ended, mark and mark.ts)
    pos = mark.pos
    a._on_pty_output("pty", "redrawn frame after a workspace switch")
    a._busy = True
    a._on_idle_timeout()
    check("stop-stamp: a later settle (a redraw, a recap) keeps the time",
          a.reply_marks() == [mark] and mark.ts == ended, mark.ts)
    check("stop-stamp: ...and the position", mark.pos == pos, (mark.pos, pos))

    # the other order: the agent had already settled when the hook arrived
    a._note_submit()
    first = a.note_reply_settled()
    a.note_reply_stopped(ended + 60)
    check("stop-stamp: a Stop after the settle rewrites that mark's time",
          first.ts == ended + 60, first.ts)
    a._on_pty_output("pty", "recap: ...")
    a._busy = True
    a._on_idle_timeout()
    check("stop-stamp: ...and freezes it", first.ts == ended + 60, first.ts)

    # Claude replying on its own (a background task finished): a new ending
    a.note_reply_stopped(ended + 120)
    check("stop-stamp: a later Stop in the same turn moves it to that ending",
          first.ts == ended + 120 and len(a.reply_marks()) == 2,
          (first.ts, len(a.reply_marks())))

    # nobody asked: a Stop with no submitted turn stamps nothing
    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "StopNoTurn",
                                 cwd=SCRATCH_CWD, pty=True))
    b.note_reply_stopped(ended)
    b._busy = True
    b._on_idle_timeout()
    check("stop-stamp: a Stop with no submitted turn stamps nothing",
          b.reply_marks() == [], b.reply_marks())

    # ---- the manager hands the hook's own time over, not the poll's ------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-stopstamp-"))
    events = str(tmp / "events.jsonl")
    sh.reset_events(events)
    mgr = WorkspaceManager()
    mgr.prompt_events_path = events
    ws = mgr.create_workspace("StopStamp", str(tmp))
    cl = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Cl",
                                            cwd=SCRATCH_CWD, pty=True),
                          autostart=False)
    cl.status = AgentStatus.RUNNING
    for kind in (sh.EV_TURN_CLEAR, sh.EV_TURN_SET):
        cl._note_submit()
        cl.note_reply_settled()
        # the record _append_event writes, backdated to when the hook fired
        with open(events, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"agent_id": cl.id, "kind": kind,
                                 "ts": ended}) + "\n")
        mgr.sync_prompt_events()
        check(f"stop-stamp: the manager passes the hook's time ({kind})",
              cl.reply_marks()[-1].ts == ended, cl.reply_marks()[-1].ts)
    for ag in (a, b, cl):
        ag._idle_timer.stop()
        ag._reply_timer.stop()
    a.dispose()
    b.dispose()
    shutil.rmtree(tmp, ignore_errors=True)
