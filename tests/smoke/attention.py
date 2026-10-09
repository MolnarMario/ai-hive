"""Telling the user an agent needs them: waiting and busy state, prompt
hook events, chimes, the taskbar badge and the event log."""

import os
import tempfile
from pathlib import Path

from .harness import SCRATCH_CWD, check


def test_agent_waiting():
    """A settled Claude prompt/question flags the agent as waiting-for-input;
    fresh output clears it, idle-at-prompt does not flag, exit clears it, and
    bypassPermissions suppresses it."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Ask", cwd=SCRATCH_CWD))
    a.status = AgentStatus.RUNNING
    events = []
    a.waiting_changed.connect(events.append)

    prompt = ("\x1b[1mDo you want to make this edit to app.py?\x1b[0m\r\n"
              " \x1b[2m1.\x1b[0m Yes\r\n"
              " 2. Yes, and don't ask again this session\r\n"
              " 3. No, and tell Claude what to do differently\r\n"
              "\x1b[7m> 1. Yes\x1b[0m\r\n? for shortcuts")
    a._on_pty_output("pty", prompt)
    check("waiting: not flagged while output is still fresh (busy)",
          not a.is_waiting())
    a._on_idle_timeout()                 # simulate the 2 s settle
    check("waiting: settled permission prompt -> waiting + signal",
          a.is_waiting() and events and events[-1] is True,
          (a.is_waiting(), events))

    a._on_pty_output("pty", "Bash(ls) running...\r\n")   # movement resumes
    check("waiting: fresh output clears the waiting state", not a.is_waiting())

    a._screen_tail = "esc to interrupt\n? for shortcuts"  # idle at prompt
    a._on_idle_timeout()
    check("waiting: idle-at-prompt (no question) is NOT waiting",
          not a.is_waiting())

    a._screen_tail = ("which approach?\r\n 1. rewrite it\r\n 2. patch it\r\n"
                      " 3. leave as-is\r\n> 1. rewrite it")
    a._on_idle_timeout()
    check("waiting: an option menu (2+ numbered options) flags waiting",
          a.is_waiting())

    # regression: an agent's OWN prose with a numbered list (and even a
    # question / "proceed?"-style phrasing) but NO selection caret must NOT
    # flag waiting — this false-fired the "?" + chime on ordinary output
    a._on_pty_output("pty", "working...\r\n")   # clear, then settle on prose
    a._screen_tail = ("Do you want the summary? here's what I did:\r\n"
                      " 1. committed the change\r\n 2. pushed to main\r\n"
                      " 3. merged and closed the PRs\r\nall done.")
    a._on_idle_timeout()
    check("waiting: a plain numbered list (no selection caret) is NOT waiting",
          not a.is_waiting())

    a._set_status(AgentStatus.EXITED_OK)
    check("waiting: exit clears the waiting state", not a.is_waiting())

    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bypass", cwd=SCRATCH_CWD))
    b.spec.permission_mode = "bypassPermissions"
    b.status = AgentStatus.RUNNING
    b._screen_tail = "Do you want to proceed?\r\n 1. Yes\r\n 2. No"
    b._on_idle_timeout()
    check("waiting: bypassPermissions suppresses the prompt '?'",
          not b.is_waiting())


def test_notification_chime():
    """The notification chime: the synthesiser writes a valid WAV, and the
    manager announces the RISING edge of an agent's waiting state via
    agentWaiting (so the UI can ring) without ever marking the session dirty.
    Playback itself is a non-blocking, degrade-to-silent no-op — not exercised
    here so the headless suite stays quiet."""
    import wave as _wave
    from PySide6.QtWidgets import QApplication
    from app import chime
    from app.terminal_agent import AgentStatus
    from app.workspace_manager import WorkspaceManager
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- synthesiser: a real, playable 16-bit mono WAV lands in temp ---
    path = chime._ensure_chime()
    check("chime: WAV synthesised to a temp file", path and os.path.exists(path))
    with _wave.open(path, "rb") as w:
        params_ok = (w.getnchannels() == 1 and w.getsampwidth() == 2
                     and w.getframerate() == chime._SAMPLE_RATE
                     and w.getnframes() > 0)
    check("chime: WAV is 16-bit mono at the expected rate with frames",
          params_ok)

    # --- manager announces the waiting rising edge, transient (no dirty) ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-chime-"))
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Chime", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Ask",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    rings = []
    dirtied = []
    mgr.agentWaiting.connect(lambda wid, aid: rings.append((wid, aid)))
    mgr.dirty.connect(lambda: dirtied.append(True))

    agent._screen_tail = ("which approach?\r\n 1. rewrite it\r\n 2. patch it\r\n"
                          " 3. leave as-is\r\n> 1. rewrite it")
    agent._on_idle_timeout()   # settle -> waiting rising edge
    check("chime: agentWaiting emitted with (ws_id, agent_id) on rising edge",
          rings == [(ws.id, agent.id)], rings)
    check("chime: waiting rising edge never marks the session dirty",
          not dirtied, dirtied)

    # fresh output clears waiting; re-settling on the SAME prompt fires again
    agent._on_pty_output("pty", "Bash(ls) running...\r\n")
    check("chime: fresh output clears waiting", not agent.is_waiting())
    agent._on_idle_timeout()
    check("chime: re-entering waiting rings again (edge, not level)",
          len(rings) == 2, rings)


def test_chime_toggles():
    """The chime switches flip their glyph and emit soundToggled /
    replySoundToggled; the restore setters update without emitting. (The
    preference's round-trip through the session is test_custom_chime_sounds.)

    The switch lives in the Options panel as a real track-and-thumb
    `ToggleSwitch` (green/slid-right when armed, grey/slid-left when off),
    label at `sound_label` and switch at `sound_btn`, `isChecked()` mirroring
    the state the thumb is drawn in. The bell/muted-bell glyph is still in
    the label, so the state is readable two ways."""
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import TopBar

    QApplication.instance() or QApplication([])
    bar = TopBar()
    check("chime toggle: defaults to ON (bell glyph, lit)",
          bar._sound_on and bar.sound_btn.isChecked()
          and "\U0001F514" in bar.sound_label.text()
          and "Question chime" in bar.sound_label.text(),
          bar.sound_label.text())
    emitted = []
    bar.soundToggled.connect(emitted.append)
    bar.sound_btn.click()
    check("chime toggle: click mutes + emits False + shows muted glyph",
          emitted == [False] and not bar._sound_on
          and "\U0001F515" in bar.sound_label.text()
          and not bar.sound_btn.isChecked(), (emitted, bar._sound_on))
    bar.sound_btn.click()
    check("chime toggle: click again re-enables + emits True",
          emitted == [False, True] and bar._sound_on, emitted)
    # set_sound_enabled reflects state WITHOUT re-emitting (restore path)
    bar.set_sound_enabled(False)
    check("chime toggle: set_sound_enabled updates glyph, no emit",
          not bar._sound_on and emitted == [False, True]
          and not bar.sound_btn.isChecked())

    # the reply-finished chime: its own switch, OFF by default
    check("reply chime toggle: defaults to OFF (muted glyph, unlit)",
          not bar._reply_sound_on and not bar.reply_sound_btn.isChecked()
          and "🔕" in bar.reply_sound_label.text()
          and "Reply finished chime" in bar.reply_sound_label.text(),
          bar.reply_sound_label.text())
    replied = []
    bar.replySoundToggled.connect(replied.append)
    bar.reply_sound_btn.click()
    check("reply chime toggle: click arms it + emits True, question one untouched",
          replied == [True] and bar._reply_sound_on
          and bar.reply_sound_btn.isChecked()
          and emitted == [False, True], (replied, emitted))
    bar.set_reply_sound_enabled(False)
    check("reply chime toggle: set_reply_sound_enabled updates, no emit",
          not bar._reply_sound_on and replied == [True]
          and not bar.reply_sound_btn.isChecked())
    bar.deleteLater()


def test_custom_chime_sounds():
    """A user's own WAV/MP3 per chime: chime.validate gates the file, play()
    hands a custom file to the right player and falls back to the built-in
    sound when it is missing or will not play, and MainWindow copies the file
    into app data and round-trips it through the session.

    No real audio: winsound and MCI are swapped for recorders, so the suite
    stays silent and this runs the same off Windows."""
    import types
    import wave as _wave
    from PySide6.QtWidgets import QApplication, QMessageBox
    from app import chime
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-customchime-"))

    def write_wav(name, seconds):
        path = tmp / name
        with _wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(8000)
            w.writeframes(b"\0\0" * int(8000 * seconds))
        return str(path)

    short = write_wav("ding.wav", 0.5)
    long_ = write_wav("long.wav", 6.0)
    fake_wav = tmp / "notes.wav"
    fake_wav.write_text("not a riff header at all", encoding="utf-8")
    ogg = tmp / "ding.ogg"
    ogg.write_bytes(b"OggS" + b"\0" * 100)
    huge = tmp / "huge.wav"
    huge.write_bytes(b"\0" * (3 * 1024 * 1024))

    # --- validate ---------------------------------------------------------
    check("custom chime: a 0.5 s PCM WAV is accepted",
          chime.validate(short) is None, chime.validate(short))
    check("custom chime: a 6 s WAV is rejected as too long",
          "at most" in (chime.validate(long_) or ""), chime.validate(long_))
    check("custom chime: a text file named .wav is rejected",
          chime.validate(str(fake_wav)) is not None)
    check("custom chime: an .ogg is rejected by extension",
          "WAV and MP3" in (chime.validate(str(ogg)) or ""))
    check("custom chime: a 3 MB file is rejected by size",
          "MB" in (chime.validate(str(huge)) or ""),
          chime.validate(str(huge)))
    check("custom chime: a missing file is rejected",
          chime.validate(str(tmp / "gone.wav")) is not None)

    # --- play: recorders instead of real audio ----------------------------
    played, mci_cmds = [], []
    fake_ws = types.SimpleNamespace(
        SND_FILENAME=1, SND_ASYNC=2, SND_NODEFAULT=4, MB_ICONASTERISK=0,
        PlaySound=lambda p, flags: played.append(p),
        MessageBeep=lambda *_a: played.append("beep"))
    mci_ok = [True]

    def fake_mci(cmd):
        mci_cmds.append(cmd)
        return (0 if mci_ok[0] else 263), ""

    real_ws, real_mci = chime.winsound, chime._mci
    chime.winsound, chime._mci = fake_ws, fake_mci
    chime._mci_open_path = None
    try:
        builtin_q = chime._ensure_chime(chime.QUESTION)
        chime.play(chime.QUESTION, short)
        check("custom chime: a custom WAV goes to PlaySound as-is",
              played == [short], played)
        played.clear()
        chime.play(chime.QUESTION, str(tmp / "gone.wav"))
        check("custom chime: a missing custom file falls back to built-in",
              played == [builtin_q], played)

        mp3 = tmp / "ding.mp3"
        mp3.write_bytes(b"ID3" + b"\0" * 100)
        played.clear()
        chime.play(chime.REPLY, str(mp3))
        chime._flush()
        check("custom chime: an MP3 opens and plays on the MCI device",
              any(c.startswith("open ") and str(mp3) in c for c in mci_cmds)
              and any(c.startswith("play ") for c in mci_cmds)
              and played == [None], (mci_cmds, played))
        mci_cmds.clear()
        chime.play(chime.REPLY, str(mp3))
        chime._flush()
        check("custom chime: a repeat MP3 replays without reopening",
              not any(c.startswith("open ") for c in mci_cmds), mci_cmds)

        chime.stop()
        mci_ok[0] = False
        played.clear()
        chime.play(chime.REPLY, str(mp3))
        chime._flush()
        check("custom chime: an MP3 MCI cannot open falls back to built-in",
              played[-1:] == [chime._ensure_chime(chime.REPLY)], played)
        mci_ok[0] = True

        # --- MainWindow: copy, persist, restore, reset -------------------
        store = SessionStore(path=tmp / "session.json")
        win = create_main_window(store)
        warned = []
        real_warning = QMessageBox.warning
        QMessageBox.warning = staticmethod(
            lambda *a, **k: warned.append(a[2] if len(a) > 2 else ""))
        try:
            check("custom chime: an invalid pick is refused with a message",
                  win.set_custom_chime(chime.QUESTION, long_) is not None
                  and warned and chime.QUESTION not in win._custom_sounds,
                  warned)
        finally:
            QMessageBox.warning = real_warning
        played.clear()
        check("custom chime: a valid pick is accepted",
              win.set_custom_chime(chime.QUESTION, short) is None)
        copied = win._custom_sounds.get(chime.QUESTION, "")
        check("custom chime: the file is COPIED into the sounds dir",
              copied and os.path.isfile(copied)
              and os.path.dirname(copied) == win.sounds_dir()
              and copied != short, copied)
        check("custom chime: accepting it plays a preview of the copy",
              played == [copied], played)
        check("custom chime: the Options tooltip names the original file",
              "ding.wav" in win.top_bar.chime_sound_btns[
                  chime.QUESTION].toolTip()
              and "ding.wav" in win.top_bar.sound_btn.toolTip())
        os.remove(short)
        played.clear()
        win._play_chime(chime.QUESTION)
        check("custom chime: deleting the ORIGINAL changes nothing",
              played == [copied], played)

        # replacing drops the old copy
        again_src = write_wav("dong.wav", 0.3)
        win.set_custom_chime(chime.QUESTION, again_src)
        second = win._custom_sounds[chime.QUESTION]
        check("custom chime: a replacement deletes the old copy",
              second != copied and not os.path.exists(copied)
              and os.path.isfile(second), (copied, second))

        # a replacement in the same millisecond once copied over the
        # current copy and then deleted it
        import app.widgets.main_window as mw
        real_time = mw.time
        frozen = real_time.time()
        mw.time = types.SimpleNamespace(time=lambda: frozen)
        try:
            first_src = write_wav("tick.wav", 0.3)
            win.set_custom_chime(chime.QUESTION, first_src)
            first_ms = win._custom_sounds[chime.QUESTION]
            second_src = write_wav("dong.wav", 0.3)
            win.set_custom_chime(chime.QUESTION, second_src)
            second = win._custom_sounds[chime.QUESTION]
        finally:
            mw.time = real_time
        check("custom chime: a replacement in the same millisecond keeps "
              "the new copy", second != first_ms
              and not os.path.exists(first_ms) and os.path.isfile(second),
              (first_ms, second))

        win._save_session()
        win._save_timer.stop()
        win.close()
        app.processEvents()
        win2 = create_main_window(SessionStore(path=tmp / "session.json"))
        check("custom chime: the custom sound survives a reopen",
              win2._custom_sounds.get(chime.QUESTION) == second
              and win2.top_bar.custom_sound_name(chime.QUESTION)
              == "dong.wav", win2._custom_sounds)
        check("custom chime: the other chime stays built-in",
              chime.REPLY not in win2._custom_sounds
              and not win2.top_bar.custom_sound_name(chime.REPLY))

        # the menu: reset is only offered when there is something to reset
        menu = win2.top_bar.build_chime_sound_menu(chime.REPLY)
        acts = {a.text(): a for a in menu.actions()}
        check("custom chime: built-in chime's menu greys out the reset",
              not acts["Use built-in sound"].isEnabled())
        asked = []
        win2.top_bar.chimeSoundChooseRequested.disconnect()
        win2.top_bar.chimeSoundChooseRequested.connect(asked.append)
        acts["Choose sound file..."].trigger()
        check("custom chime: Choose emits the chime's kind",
              asked == [chime.REPLY], asked)
        menu = win2.top_bar.build_chime_sound_menu(chime.QUESTION)
        acts = {a.text(): a for a in menu.actions()}
        acts["Use built-in sound"].trigger()
        check("custom chime: Use built-in sound drops the file and the entry",
              chime.QUESTION not in win2._custom_sounds
              and not os.path.exists(second)
              and not win2.top_bar.custom_sound_name(chime.QUESTION))

        # a session pointing at a vanished copy restores as built-in
        win2._custom_sounds[chime.REPLY] = str(tmp / "sounds" / "nope.wav")
        win2._save_session()
        win2._save_timer.stop()
        win2.close()
        app.processEvents()
        win3 = create_main_window(SessionStore(path=tmp / "session.json"))
        check("custom chime: a missing copy restores as the built-in sound",
              chime.REPLY not in win3._custom_sounds, win3._custom_sounds)
        win3._save_timer.stop()
        win3.close()
        app.processEvents()
    finally:
        chime._flush()
        chime.winsound, chime._mci = real_ws, real_mci
        chime._mci_open_path = None


def test_reply_chime():
    """The reply-finished chime's cue (TerminalAgent.reply_finished ->
    WorkspaceManager.agentReplied). Claude's comes from the Stop hook's
    turn_clear edge; the hookless providers wait REPLY_QUIET_MS past a settle.
    Either way: only for a turn somebody submitted, once per turn, never when
    the agent is asking something back, never for a plain shell."""
    import wave as _wave
    from PySide6.QtWidgets import QApplication
    from app import chime
    from app import session_hook as sh
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])

    # --- both sounds synthesise, and they are different sounds ---
    q, r = chime._ensure_chime(chime.QUESTION), chime._ensure_chime(chime.REPLY)
    check("reply chime: question + reply WAVs are separate files",
          q and r and q != r and os.path.exists(q) and os.path.exists(r),
          (q, r))
    with _wave.open(r, "rb") as w:
        check("reply chime: reply WAV is 16-bit mono with frames",
              w.getnchannels() == 1 and w.getsampwidth() == 2
              and w.getnframes() > 0)
    check("reply chime: the question sound ends mid-slide UP, the reply on a "
          "held note",
          chime._SOUNDS[chime.QUESTION][-1][2][-1][1]
          > chime._SOUNDS[chime.QUESTION][-1][2][0][1]
          and len(chime._SOUNDS[chime.REPLY][-1][2]) == 1)

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-reply-"))
    events = str(tmp / "events.jsonl")
    sh.reset_events(events)
    mgr = WorkspaceManager()
    mgr.prompt_events_path = events
    ws = mgr.create_workspace("Reply", str(tmp))
    replies = []
    mgr.agentReplied.connect(lambda wid, aid: replies.append(aid))

    # --- Claude: the Stop hook's turn_clear is the edge ---
    cl = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Cl",
                                            cwd=str(tmp)), autostart=False)
    cl.status = AgentStatus.RUNNING

    def emit(agent, kind):
        os.environ[sh.AGENT_ID_ENV] = agent.id
        try:
            sh._append_event(events, kind)
        finally:
            os.environ.pop(sh.AGENT_ID_ENV, None)

    emit(cl, sh.EV_TURN_CLEAR)
    mgr.sync_prompt_events()
    check("reply chime: a Stop with no submitted turn (a --resume) is silent",
          replies == [], replies)
    cl._note_submit()
    cl._busy = True     # the hook lands inside the 2 s idle window
    emit(cl, sh.EV_TURN_CLEAR)
    mgr.sync_prompt_events()
    check("reply chime: Claude Stop (turn_clear) after a submit rings, even "
          "while still inside the busy window", replies == [cl.id], replies)
    emit(cl, sh.EV_TURN_CLEAR)
    mgr.sync_prompt_events()
    check("reply chime: once per turn", replies == [cl.id], replies)
    cl._busy = False
    cl._note_submit()
    cl._on_idle_timeout()
    check("reply chime: Claude never arms the quiet timer (the hook is exact)",
          not cl._reply_timer.isActive())
    cl._tool_waiting = True
    cl._emit_waiting()
    cl.note_turn_ended()
    check("reply chime: silent while the agent is asking the user something",
          replies == [cl.id], replies)
    cl._tool_waiting = False
    cl._emit_waiting()

    # --- hookless provider: settle arms the quiet timer, output cancels it ---
    gm = mgr.add_terminal(ws.id, build_spec(AgentKind.GEMINI, "Gm",
                                            cwd=str(tmp)), autostart=False)
    gm.status = AgentStatus.RUNNING
    gm._on_idle_timeout()
    check("reply chime: Gemini settle with no submitted turn arms nothing",
          not gm._reply_timer.isActive())
    gm._note_submit()
    gm._last_input_ts = 0.0
    gm._mark_busy()
    gm._on_idle_timeout()
    check("reply chime: Gemini settle inside a turn arms the quiet timer",
          gm._reply_timer.isActive())
    gm._mark_busy()     # a tool ran long, then more output
    check("reply chime: fresh output cancels the pending chime (mid-reply)",
          not gm._reply_timer.isActive())
    gm._on_idle_timeout()
    gm._bg_shell = True
    gm._on_reply_quiet()
    check("reply chime: silent while a background command still runs",
          gm.id not in replies, replies)
    gm._bg_shell = False
    gm._on_reply_quiet()
    check("reply chime: quiet timer expiry rings for Gemini",
          replies.count(gm.id) == 1, replies)
    gm._idle_timer.stop()
    gm._reply_timer.stop()

    # --- a plain shell never announces a reply ---
    sh_agent = mgr.add_terminal(ws.id, build_spec(AgentKind.POWERSHELL, "Sh",
                                                  cwd=str(tmp)),
                                autostart=False)
    sh_agent.status = AgentStatus.RUNNING
    sh_agent._note_submit()
    sh_agent._on_reply_quiet()
    check("reply chime: a plain shell never rings", sh_agent.id not in replies,
          replies)
    for a in (cl, gm, sh_agent):
        a._idle_timer.stop()
        a._reply_timer.stop()


def test_taskbar_badge():
    """The Windows taskbar overlay is the ONLY 'agents are working' signal that
    reaches the user in another application, so its state table is the feature.

    Windows allows exactly one overlay icon, fixed to the corner of the taskbar
    button, so the working COUNT and the "someone is asking" flag have to share
    one ~16px square: the digit is the count, the fill colour is the question.
    An idle hive must show NO overlay - that absence is the readout.

    Also checks the two rules a transient indicator in this app always has to
    obey: the count NEVER marks the session dirty (this recomputes every time an
    agent's output starts or stops, so a save here would rewrite session.json
    all day), and the push is edge-guarded on a rendered key (each push builds
    an HICON and crosses a COM boundary, and workspaceStatsChanged fires every
    couple of seconds per busy agent).
    """
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets import ornaments
    from app.widgets.main_window import TopBar
    from app import taskbar_overlay
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    # --- the painter: real pixels, whatever the shell asks for -------------
    size = taskbar_overlay.overlay_size()
    check("taskbar: overlay_size is a sane icon size",
          8 <= size <= 256, size)
    w, h, raw = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_WORKING, 16)
    check("taskbar: badge is a 16x16 BGRA buffer of the right length",
          (w, h) == (16, 16) and len(raw) == 16 * 16 * 4, (w, h, len(raw)))
    # a disc, not a square: the corners stay transparent so it reads as a badge
    # on whatever colour the user's taskbar happens to be
    corner = raw[0:4]
    centre = raw[((8 * 16) + 8) * 4:((8 * 16) + 8) * 4 + 4]
    check("taskbar: the badge is a disc (corner transparent, centre opaque)",
          corner[3] < 40 and centre[3] > 200, (corner[3], centre[3]))
    amber = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_WORKING, 16)[2]
    blue = ornaments.taskbar_badge_bgra("3", ornaments.TASKBAR_ASKING, 16)[2]
    check("taskbar: the working and asking fills are visibly different",
          amber != blue)
    check("taskbar: a two-character count still renders",
          len(ornaments.taskbar_badge_bgra(
              "9+", ornaments.TASKBAR_WORKING, 16)[2]) == 16 * 16 * 4)

    # --- the state table --------------------------------------------------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-taskbar-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    app.processEvents()
    mgr = win.manager
    ws = mgr.create_workspace("Taskbar", str(tmp))
    agents = [mgr.add_terminal(ws.id,
                               build_spec(AgentKind.CLAUDE, f"A{i}",
                                          cwd=str(tmp)), autostart=False)
              for i in range(3)]

    def spec():
        return win._taskbar_badge_spec()

    key, text, fill, note = spec()
    check("taskbar: an idle hive gets NO overlay at all",
          text is None and key == "0|0|0", (key, text))

    agents[0]._busy = True        # what _mark_busy sets on an output burst
    key, text, fill, note = spec()
    check("taskbar: one agent working shows an amber 1",
          (key, text, fill) == ("1|0|0", "1", ornaments.TASKBAR_WORKING),
          (key, text, fill))
    check("taskbar: the description names the count", note == "1 working", note)

    agents[1]._busy = True
    agents[2]._busy = True
    check("taskbar: the count is every working agent across ALL workspaces",
          spec()[1] == "3", spec())

    # the "?" rides the SAME square as a colour swap, since there is no second
    # overlay slot to put it in
    agents[2]._waiting = True
    key, text, fill, note = spec()
    check("taskbar: an agent with a question turns the disc blue, count intact",
          (key, text, fill) == ("3|1|0", "3", ornaments.TASKBAR_ASKING),
          (key, text, fill))
    check("taskbar: the description says someone is waiting",
          "waiting for you" in note, note)

    for a in agents:
        a._busy = False
    key, text, fill, note = spec()
    check("taskbar: nothing working but a question pending shows a blue '?'",
          (key, text, fill) == ("0|1|0", "?", ornaments.TASKBAR_ASKING),
          (key, text, fill))

    agents[2]._waiting = False
    check("taskbar: everything quiet again clears the overlay",
          spec()[1] is None, spec())

    # --- the edge guard: identical states collapse to ONE key -------------
    for a in agents:
        a._busy = True
    first = spec()[0]
    check("taskbar: an unchanged state renders an unchanged key",
          spec()[0] == first, (first, spec()[0]))
    many = [mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, f"B{i}",
                                               cwd=str(tmp)), autostart=False)
            for i in range(9)]
    for a in many:
        a._busy = True
    key, text, _f, _n = spec()
    check("taskbar: past 9 the disc says 9+ (a bigger number is unreadable "
          "at this size) and every such state collapses to one key",
          (key, text) == ("10|0|0", "9+"), (key, text))

    # --- transient: the count must NEVER reach the session ----------------
    win._save_timer.stop()
    win._taskbar_key = None
    win._push_taskbar_badge()
    check("taskbar: pushing the badge never marks the session dirty",
          not win._save_timer.isActive())
    check("taskbar: the push is a no-op off a real taskbar (offscreen suite)",
          win._taskbar_key is None, win._taskbar_key)

    # --- the toggle: a preference, so it DOES save, and it round-trips -----
    bar = TopBar()
    check("taskbar toggle: defaults to ON",
          bar._taskbar_badge and bar.taskbar_btn.isChecked())
    emitted = []
    bar.taskbarBadgeToggled.connect(emitted.append)
    bar.taskbar_btn.click()
    check("taskbar toggle: a click turns it off and emits False",
          emitted == [False] and not bar._taskbar_badge, emitted)
    bar.set_taskbar_badge(True)   # restore path: reflect without re-emitting
    check("taskbar toggle: set_taskbar_badge does not re-emit",
          bar._taskbar_badge and emitted == [False])
    bar.deleteLater()

    win._save_timer.stop()
    win._on_taskbar_badge_toggled(False)
    check("taskbar: flipping the PREFERENCE does mark the session dirty",
          win._save_timer.isActive())
    check("taskbar: switched off, a fully working hive still shows nothing",
          spec()[1] is None, spec())
    payload = win._session_payload()
    check("taskbar: the preference is persisted under ui",
          payload["ui"]["taskbar_badge"] is False, payload["ui"])
    win._restore_ui_state({"ui": {"taskbar_badge": True}})
    check("taskbar: the preference is restored onto the window and the button",
          win._taskbar_badge and win.top_bar._taskbar_badge)
    win._restore_ui_state({"ui": {}})
    check("taskbar: a session that predates the feature defaults it ON",
          win._taskbar_badge)

    # ...and the same thing through a REAL close/reopen, which is the only way
    # to catch the default being assigned after _restore_ui_state has run (it
    # was, and it silently switched the badge back on at every launch).
    win._taskbar_badge = False
    win.top_bar.set_taskbar_badge(False)
    win._save_session()
    win.close()
    app.processEvents()
    again = create_main_window(SessionStore(path=tmp / "session.json"))
    check("taskbar: OFF survives a close and reopen",
          not again._taskbar_badge and not again.top_bar._taskbar_badge,
          again._taskbar_badge)
    check("taskbar: reopening starts with no badge pushed yet",
          again._taskbar_key is None, again._taskbar_key)
    again._save_timer.stop()
    again.close()
    app.processEvents()


def test_bg_shell_taskbar_state():
    """The taskbar's third state: no agent is busy or asking, but one or more
    are idle with a background command still running -- previously invisible
    everywhere, including the taskbar. It must surface ONLY when neither busy
    nor asking is true (both outrank it), and the edge-guard key must still
    coalesce an unchanged state."""
    from PySide6.QtWidgets import QApplication
    from app.session_store import SessionStore
    from app.process_worker import AgentKind, build_spec
    from app.widgets import ornaments
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-taskbar-bg-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    app.processEvents()
    mgr = win.manager
    ws = mgr.create_workspace("TaskbarBg", str(tmp))
    agents = [mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, f"A{i}",
                                                 cwd=str(tmp)), autostart=False)
              for i in range(2)]

    def spec():
        return win._taskbar_badge_spec()

    key, text, fill, note = spec()
    check("taskbar bg: idle hive with nothing flagged shows no overlay",
          text is None, (key, text))

    agents[0]._bg_shell = True
    key, text, fill, note = spec()
    check("taskbar bg: one agent idle-on-a-shell shows a violet 1",
          (text, fill) == ("1", ornaments.TASKBAR_BG_SHELL), (text, fill))
    check("taskbar bg: the description names the count",
          "background command" in note, note)

    agents[1]._bg_shell = True
    check("taskbar bg: the count is every bg-shell agent",
          spec()[1] == "2", spec())

    # busy anywhere outranks bg-shell, even elsewhere in the hive
    agents[1]._busy = True
    key, text, fill, note = spec()
    check("taskbar bg: a busy agent elsewhere wins over bg-shell (amber, "
          "busy count only)",
          (text, fill) == ("1", ornaments.TASKBAR_WORKING), (text, fill))
    agents[1]._busy = False

    # asking outranks both
    agents[1]._waiting = True
    key, text, fill, note = spec()
    check("taskbar bg: asking wins over bg-shell too",
          fill == ornaments.TASKBAR_ASKING, (text, fill))
    agents[1]._waiting = False

    # unchanged bg-shell state -> unchanged key (the push's edge guard)
    first_key = spec()[0]
    check("taskbar bg: an unchanged bg-shell state renders an unchanged key",
          spec()[0] == first_key, (first_key, spec()[0]))

    agents[0]._bg_shell = False
    agents[1]._bg_shell = False
    check("taskbar bg: clearing every flag clears the overlay",
          spec()[1] is None, spec())

    win._save_timer.stop()
    win.close()
    app.processEvents()


def test_hook_prompt_events():
    """The Claude-hook script is the AUTHORITATIVE 'needs the user' signal for
    the prompts the screen scrape cannot see. Verify: the shared settings file
    carries PreToolUse/PostToolUse/Stop hooks with the right matchers; the
    dispatch turns each event into the right edge record (AskUserQuestion/
    ExitPlanMode -> tool set/clear; Stop -> tool clear + turn set/clear on the
    'ends with ?' test; a non-waiting tool is ignored); and the incremental
    reader consumes only complete lines and advances its offset."""
    import json as _json
    from app import session_hook as sh

    tmpd = Path(tempfile.mkdtemp(prefix="ai-hive-hook-"))
    settings = str(tmpd / "settings.json")
    mapping = str(tmpd / "map.jsonl")
    events = str(tmpd / "events.jsonl")

    # --- settings file: the new hooks are present + correctly scoped ---
    sh.write_settings_file(settings, mapping, events, python_exe="py")
    with open(settings, encoding="utf-8") as fh:
        hooks = _json.load(fh)["hooks"]
    check("hook: PreToolUse matches AskUserQuestion|ExitPlanMode",
          hooks["PreToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode")
    check("hook: PostToolUse matches the same waiting tools",
          hooks["PostToolUse"][0]["matcher"] == "AskUserQuestion|ExitPlanMode")
    check("hook: Stop hook present (turn-end / free-text question)",
          "Stop" in hooks and hooks["Stop"][0]["hooks"])
    check("hook: the command carries BOTH the map and events paths",
          events in hooks["PreToolUse"][0]["hooks"][0]["command"])
    # SessionStart is unchanged and still excludes `startup` (launch-timing)
    check("hook: SessionStart still matches only resume|clear|compact",
          hooks["SessionStart"][0]["matcher"] == "resume|clear|compact")
    # omitting events_path degrades to SessionStart-only (back-compat)
    sh.write_settings_file(settings, mapping, python_exe="py")
    with open(settings, encoding="utf-8") as fh:
        only = _json.load(fh)["hooks"]
    check("hook: no events path -> SessionStart-only (no prompt hooks)",
          set(only) == {"SessionStart"})

    # --- dispatch: one payload -> the right edge record(s) ---
    os.environ[sh.AGENT_ID_ENV] = "agent-xyz"
    try:
        def dispatch(payload):
            sh._dispatch(mapping, events, payload)

        sh.reset_events(events)
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion"})
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "Bash"})  # ignored
        dispatch({"hook_event_name": "PostToolUse", "tool_name": "AskUserQuestion"})
        dispatch({"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode"})
        dispatch({"hook_event_name": "Stop",
                  "last_assistant_message": "Which of these do you prefer?"})
        dispatch({"hook_event_name": "Stop",
                  "last_assistant_message": "All done and pushed."})

        recs, off = sh.read_prompt_events(events, 0)
        kinds = [r["kind"] for r in recs]
        check("hook: AskUserQuestion PreToolUse -> tool_set (Bash ignored)",
              kinds[0] == sh.EV_TOOL_SET and sh.EV_TOOL_SET not in kinds[1:2],
              kinds)
        check("hook: every record is attributed to the env agent id",
              all(r["agent_id"] == "agent-xyz" for r in recs))
        check("hook: PostToolUse -> tool_clear",
              sh.EV_TOOL_CLEAR in kinds)
        check("hook: ExitPlanMode PreToolUse -> tool_set",
              kinds.count(sh.EV_TOOL_SET) == 2)
        # Stop on a question: tool_clear THEN turn_set; on a statement: turn_clear
        check("hook: Stop ending in '?' -> tool_clear + turn_set",
              sh.EV_TURN_SET in kinds
              and kinds.index(sh.EV_TOOL_CLEAR) < kinds.index(sh.EV_TURN_SET))
        check("hook: Stop ending in a statement -> turn_clear (not set)",
              sh.EV_TURN_CLEAR in kinds)

        # --- incremental read: offset advances, no line re-delivered ---
        recs2, off2 = sh.read_prompt_events(events, off)
        check("hook: reading again from the offset yields nothing new",
              recs2 == [] and off2 == off, (recs2, off2, off))
        # a half-written trailing line is NOT consumed until it completes
        with open(events, "a", encoding="utf-8") as fh:
            fh.write('{"agent_id":"agent-xyz","kind":"tool_set"')  # no newline
        recs3, off3 = sh.read_prompt_events(events, off)
        check("hook: a partial trailing line is held back until newline arrives",
              recs3 == [] and off3 == off)
        with open(events, "a", encoding="utf-8") as fh:
            fh.write(',"ts":1}\n')  # complete it
        recs4, off4 = sh.read_prompt_events(events, off)
        check("hook: the completed line is delivered exactly once",
              len(recs4) == 1 and recs4[0]["kind"] == sh.EV_TOOL_SET
              and off4 > off)
    finally:
        os.environ.pop(sh.AGENT_ID_ENV, None)

    # _ends_with_question tolerates trailing wrapping punctuation
    check("hook: question detector strips trailing quotes/parens",
          sh._ends_with_question('Do you want that?"')
          and sh._ends_with_question("Shall I proceed?)")
          and not sh._ends_with_question("Done.")
          and not sh._ends_with_question("I asked: why? Then I fixed it."))


def test_agent_hook_waiting():
    """TerminalAgent combines three waiting sources. The hook-driven ones catch
    what the screen scrape cannot: a tool prompt (AskUserQuestion/ExitPlanMode)
    stays flagged THROUGH its own output (sticky), while a free-text turn
    question clears the moment fresh output arrives (the user engaged)."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Ask", cwd=SCRATCH_CWD))
    a.status = AgentStatus.RUNNING
    events = []
    a.waiting_changed.connect(events.append)

    # tool prompt: set by the hook, and NOT cleared by the tool's own UI output
    a.set_tool_waiting(True)
    check("agent-hook: tool prompt flags waiting + emits True",
          a.is_waiting() and events and events[-1] is True)
    a._on_pty_output("pty", "rendering the question box...\r\n")
    check("agent-hook: a tool prompt survives its own output (sticky)",
          a.is_waiting())
    a.set_tool_waiting(False)
    check("agent-hook: PostToolUse clear drops waiting", not a.is_waiting())

    # free-text turn question: cleared by the next output burst
    a.set_turn_waiting(True)
    check("agent-hook: a free-text turn question flags waiting", a.is_waiting())
    a._on_pty_output("pty", "sure, doing it now...\r\n")
    check("agent-hook: fresh output clears a turn question (user engaged)",
          not a.is_waiting())

    # a tool prompt OR-ed with the scrape: clearing one leaves the other
    a.set_tool_waiting(True)
    a._screen_tail = ("which?\r\n 1. a\r\n 2. b\r\n> 1. a")
    a._on_idle_timeout()  # scrape also True now
    a.set_tool_waiting(False)
    check("agent-hook: clearing the tool source leaves scrape-waiting intact",
          a.is_waiting())

    a._set_status(AgentStatus.EXITED_OK)
    check("agent-hook: exit resets every waiting source", not a.is_waiting())


def test_manager_prompt_events_sync():
    """End-to-end: the manager reads the hook events file incrementally and
    drives each agent's waiting state, ringing the chime on the rising edge.
    This is the regression for the reported miss: an AskUserQuestion prompt
    (which renders no numbered menu) now lights the '?' and rings. A stale
    turn-question edge is ignored while the agent is already producing output."""
    from PySide6.QtWidgets import QApplication
    from app import session_hook as sh
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus
    from app.workspace_manager import WorkspaceManager

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-psync-"))
    events = str(tmp / "events.jsonl")
    sh.reset_events(events)
    mgr = WorkspaceManager()
    mgr.prompt_events_path = events
    ws = mgr.create_workspace("Sync", str(tmp))
    agent = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Ask",
                                               cwd=str(tmp)), autostart=False)
    agent.status = AgentStatus.RUNNING
    rings = []
    dirtied = []
    mgr.agentWaiting.connect(lambda wid, aid: rings.append((wid, aid)))
    mgr.dirty.connect(lambda: dirtied.append(True))

    def emit(kind):
        os.environ[sh.AGENT_ID_ENV] = agent.id
        try:
            sh._append_event(events, kind)
        finally:
            os.environ.pop(sh.AGENT_ID_ENV, None)

    # AskUserQuestion opens -> tool_set line -> agent waits + chime rings
    emit(sh.EV_TOOL_SET)
    mgr.sync_prompt_events()
    check("psync: AskUserQuestion (tool_set) lights '?' + rings the chime",
          agent.is_waiting() and rings == [(ws.id, agent.id)], (rings,))
    check("psync: the hook waiting edge never marks the session dirty",
          not dirtied, dirtied)

    # the tool's own render must NOT drop it (sticky), then PostToolUse clears
    agent._on_pty_output("pty", "question box...\r\n")
    check("psync: tool prompt survives its own output through the manager",
          agent.is_waiting())
    emit(sh.EV_TOOL_CLEAR)
    mgr.sync_prompt_events()
    check("psync: PostToolUse (tool_clear) drops waiting", not agent.is_waiting())

    # a turn_set that arrives while the agent is BUSY is stale -> ignored
    agent._busy = True
    emit(sh.EV_TURN_SET)
    mgr.sync_prompt_events()
    check("psync: a turn question is ignored while the agent is busy (stale)",
          not agent.is_waiting())
    # once quiet, a fresh turn question is honored
    agent._busy = False
    emit(sh.EV_TURN_SET)
    mgr.sync_prompt_events()
    check("psync: a turn question while idle flags waiting + rings again",
          agent.is_waiting() and len(rings) == 2, (rings,))

    # nothing new on the next poll (edges applied exactly once)
    before = len(rings)
    mgr.sync_prompt_events()
    check("psync: re-polling with no new lines is a no-op",
          agent.is_waiting() and len(rings) == before)


def test_input_echo_not_busy():
    """The pty echoes the user's OWN keystrokes back as output; that echo must
    NOT light the sidebar 'working' pulse (typing at an idle prompt made the row
    animate as if the agent were thinking). Genuine agent output still does."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, TerminalAgent

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Echo", cwd=SCRATCH_CWD))
    a.status = AgentStatus.RUNNING
    acts = []
    a.activity_changed.connect(acts.append)

    # write() records the user-input time even though the unstarted worker no-ops
    before = a._last_input_ts
    a.write("h")
    check("echo: write() records the user-input timestamp",
          a._last_input_ts > before)

    # keystroke echo (output right after the user typed) does NOT flag busy
    a.write("i")
    a._on_pty_output("pty", "i")   # the pty echoing the typed char back
    check("echo: typing at the prompt does NOT flag the agent busy",
          not a.is_busy() and acts == [], acts)

    # genuine agent output (no recent keystroke) DOES flag busy + emits
    a._last_input_ts = 0.0   # as if the user hasn't typed in a long while
    a._on_pty_output("pty", "Thinking... running the task\r\n")
    check("echo: real agent output (no recent input) flags busy + emits",
          a.is_busy() and acts == [True], acts)

    # a keystroke arriving mid-work does not DROP an already-lit pulse here...
    a.write("x")
    a._on_pty_output("pty", "x")
    check("echo: an echo burst leaves an existing pulse untouched",
          a.is_busy() and acts == [True], acts)
    # ...but the idle timer (armed by the last REAL output, not the echo) still
    # drops it once output truly falls quiet
    a._on_idle_timeout()
    check("echo: the pulse drops when output settles", not a.is_busy())


def test_resize_redraw_not_busy():
    """Opening or closing the sidebar resizes every card, and the full-screen
    TUI redraws its frame in answer. That redraw is not work: it must not light
    the working pulse on an idle agent (it turned every idle workspace amber).
    Real output after the window, and a pulse already running, still count."""
    from PySide6.QtWidgets import QApplication
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import AgentStatus, RESIZE_REDRAW_S, TerminalAgent

    QApplication.instance() or QApplication([])
    from app.process_worker import WorkerState

    class _Child:               # a running child that takes every size
        def setwinsize(self, rows, cols):
            pass

    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Redraw", cwd=SCRATCH_CWD))
    a.status = AgentStatus.RUNNING
    a.worker._proc = _Child()
    a.worker.state = WorkerState.RUNNING
    acts = []
    a.activity_changed.connect(acts.append)

    rows, cols = a.worker.rows, a.worker.cols
    a.resize(rows, cols)    # same size: the child draws nothing
    a._on_pty_output("pty", "Thinking... running the task\r\n")
    check("redraw: a same-size resize does not suppress real output",
          a.is_busy() and acts == [True], acts)
    a._on_idle_timeout()
    acts.clear()

    a.resize(rows, cols + 20)    # the sidebar closed: the card got wider
    a._on_pty_output("pty", "\x1b[2J\x1b[Hframe redrawn\r\n")
    check("redraw: the redraw after a real resize does NOT flag busy",
          not a.is_busy() and acts == [], acts)

    a._last_resize_ts -= RESIZE_REDRAW_S + 0.1
    a._on_pty_output("pty", "Thinking... running the task\r\n")
    check("redraw: output after the window flags busy as usual",
          a.is_busy() and acts == [True], acts)

    a.resize(rows, cols)
    a._on_pty_output("pty", "more output\r\n")
    check("redraw: a resize leaves a running pulse alone",
          a.is_busy() and acts == [True], acts)
    a._on_idle_timeout()
    acts.clear()

    # code review: the redraw keeps a waiting agent waiting and still arms
    # the settle scrape, the one that re-reads menus, limits and readiness
    a.set_turn_waiting(True)
    a.resize(rows, cols + 20)
    a._on_pty_output("pty", "\x1b[2J\x1b[Hframe redrawn\r\n")
    check("redraw: a question the agent asked stays pending",
          a.is_waiting() and not a.is_busy(), (a.is_waiting(), a.is_busy()))
    check("redraw: ...and the settle scrape is still armed",
          a._idle_timer.isActive())
    a._idle_timer.stop()

    # a short reply to a submitted line, landing inside the window, is work
    a._note_submit()
    a.resize(rows, cols)
    a._on_pty_output("pty", "Done.\r\n")
    check("redraw: output while a submit awaits its reply flags busy",
          a.is_busy() and acts == [True], acts)
    a._on_idle_timeout()

    # a child that is not running draws nothing, so nothing is suppressed
    a.worker.state = WorkerState.STARTING
    stamp = a._last_resize_ts
    a.resize(rows, cols + 20)
    check("redraw: a resize no running child took stamps nothing",
          a._last_resize_ts == stamp)

    # Luna: a line no child received awaits no reply, so it must not latch
    a.write("hello\r")
    check("redraw: a submit that failed to write awaits no reply",
          not a._awaiting_reply)

    # Luna: a submit whose only output was its echo never settles, and must
    # not keep the redraw check off for good
    from app.terminal_agent import REPLY_START_S
    a.worker.state = WorkerState.RUNNING
    a._note_submit()
    a._last_submit_ts -= REPLY_START_S + 0.1
    acts.clear()
    a.resize(rows, cols)
    a._on_pty_output("pty", "\x1b[2J\x1b[Hframe redrawn\r\n")
    check("redraw: an old unsettled submit no longer counts the redraw",
          not a.is_busy() and acts == [], acts)
    a._idle_timer.stop()
    a.worker._proc = None
    a.dispose()


def test_agent_busy_activity():
    """is_busy() tracks OUTPUT ACTIVITY, not process-alive: an interactive
    agent idling at its prompt is running but NOT busy, so the sidebar badge
    can't falsely pulse green (the reported bug). Output marks it busy; a quiet
    spell — or exit — drops it back to standby."""
    from PySide6.QtWidgets import QApplication
    from app.terminal_agent import TerminalAgent, AgentStatus
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Busy", cwd=SCRATCH_CWD))
    a.status = AgentStatus.RUNNING  # pretend the process is up (no real child)
    check("busy: running but no output yet -> standby", not a.is_busy())
    a._on_pty_output("", "generating tokens...")
    check("busy: streaming output -> busy (working)", a.is_busy())
    a._on_idle_timeout()  # simulate the quiet window elapsing
    check("busy: output goes quiet -> back to standby", not a.is_busy())
    a._on_pty_output("", "more output")
    check("busy: output resumes -> busy again", a.is_busy())
    a._set_status(AgentStatus.EXITED_OK)  # exit clears busy at once
    check("busy: process exit clears busy immediately", not a.is_busy())
    a.dispose()


def test_event_log():
    """The app-wide event log: one timeline of every agent in every workspace
    (app/event_log.py storage, app/event_hub.py collector, the window in
    app/widgets/event_log_window.py). Records survive a restart, point back
    at agents through AgentSpec.uid, and the window's links reveal the card."""
    import json as _json
    import time as _time
    from PySide6.QtCore import QEventLoop, QPoint, QRect, QTimer
    from PySide6.QtWidgets import QApplication
    from app import event_log as el
    from app.event_hub import EventHub
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.terminal_agent import AgentStatus
    from app.widgets.event_log_window import EventLogWindow, LogFilter
    from app.workspace_manager import WorkspaceManager

    app = QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    # --- a stable agent identity ---
    a = build_spec(AgentKind.CLAUDE, "Agent 1")
    b = build_spec(AgentKind.CLAUDE, "Agent 1")
    check("event log: every new spec gets its own uid",
          a.uid and b.uid and a.uid != b.uid)
    check("event log: the uid survives to_dict/from_dict",
          AgentSpec.from_dict(a.to_dict()).uid == a.uid)
    legacy = a.to_dict()
    legacy.pop("uid")
    check("event log: a pre-uid session record still gets one",
          len(AgentSpec.from_dict(legacy).uid) == 32)

    # --- storage ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-eventlog-"))
    d = str(tmp / "event_log")
    now = _time.time()
    check("event log: an absent directory reads as nothing, never raises",
          el.read_since(d, 0) == [])
    old = el.make_record(el.REPLY, agent_name="A", at=now - 2 * 86400)
    new = el.make_record(el.PROMPT, agent_name="A", text="hi", at=now)
    check("event log: append writes one line to the day's file",
          el.append(d, old) and el.append(d, new)
          and len(os.listdir(d)) == 2)
    with open(os.path.join(d, el._day_name(now)), "a", encoding="utf-8") as f:
        f.write('{"torn": \n')
    got = el.read_since(d, now - 3 * 86400)
    check("event log: records read back oldest first across day files, a "
          "torn line skipped", [r["id"] for r in got] == [old["id"], new["id"]],
          got)
    check("event log: read_since drops what is older than asked",
          [r["id"] for r in el.read_since(d, now - 3600)] == [new["id"]])
    for days in (29, 31):
        el.append(d, el.make_record(el.REPLY, at=now - days * 86400))
    gone = el.prune(d, keep_days=30, now=now)
    kept = {n[:-6] for n in os.listdir(d)}
    check("event log: prune deletes the 31-day-old file and keeps the "
          "29-day-old one",
          gone == 1 and _time.strftime("%Y-%m-%d", _time.localtime(
              now - 29 * 86400)) in kept, (gone, kept))
    check("event log: durations read like a person wrote them",
          [el.format_duration(s) for s in (45, 252, 4320, 183600)]
          == ["45s", "4m 12s", "1h 12m", "2d 3h"])
    check("event log: a board line parses into agent, stamp and message",
          el.parse_board_line("- [Agent 5] 2026-09-25 01:46 did a thing")
          == ("Agent 5", "2026-09-25 01:46", "did a thing"))

    # --- the collector ---
    wsdir = tmp / "proj"
    wsdir.mkdir()
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Hive", str(wsdir))
    old_agent = mgr.add_terminal(ws.id, build_spec(
        AgentKind.CLAUDE, "Agent 1", cwd=str(wsdir)), autostart=False)
    logdir = str(tmp / "hub_log")
    hub = EventHub(mgr, logdir)
    check("event log: agents that already existed are adopted silently",
          hub.records == [], hub.records)
    ag = mgr.add_terminal(ws.id, build_spec(
        AgentKind.CLAUDE, "Agent 2", cwd=str(wsdir)), autostart=False)
    check("event log: a new agent is a lifecycle 'added' row",
          hub.records[-1]["kind"] == el.LIFECYCLE
          and hub.records[-1]["text"] == "added"
          and hub.records[-1]["agent_uid"] == ag.spec.uid)

    ag.note_prompt_submitted("fix the parser\nand the tests")
    rec = hub.records[-1]
    check("event log: the user's own Enter is a prompt row with its text",
          rec["kind"] == el.PROMPT and rec["text"].startswith("fix the parser")
          and rec["ws_name"] == "Hive" and rec["agent_name"] == "Agent 2")
    ag.reply_finished.emit()
    rec = hub.records[-1]
    check("event log: a finished reply says how long it took",
          rec["kind"] == el.REPLY and "took" in rec["data"]
          and el.describe(rec).startswith("finished (took "), rec)
    check("event log: a task row from an older log still reads as its text",
          el.describe({"kind": el.TASK, "text": "write the docs"})
          == 'AI Hive sent a task: "write the docs"',
          el.describe({"kind": el.TASK, "text": "write the docs"}))

    ag.status = AgentStatus.RUNNING
    ag._tool_waiting = True
    ag._emit_waiting()
    check("event log: a question waits to settle before it is recorded",
          hub.records[-1]["kind"] != el.QUESTION)
    hub.flush_pending()
    q = hub.records[-1]
    check("event log: a settled question is recorded and counted",
          q["kind"] == el.QUESTION and hub.open_questions() == 1)
    check("event log: an open question reads 'waiting'",
          el.status(q, hub.closer_of(q), _time.time(),
                    hub.session_start).startswith("waiting "))
    ag._tool_waiting = False
    ag._emit_waiting()
    closer = hub.closer_of(q)
    check("event log: answering closes the question row in place",
          closer is not None and closer["kind"] == el.ANSWERED
          and closer["ref"] == q["id"] and hub.open_questions() == 0
          and el.status(q, closer, _time.time(), hub.session_start)
          .startswith("answered after "))

    # the plan-limit menu raises "?" too; it is not a question
    ag._tool_waiting = True
    ag._emit_waiting()
    hub.flush_pending()
    menu_q = hub.records[-1]
    ag._limit_blocked = True
    ag._limit_resets_at = _time.time() + 3600
    ag.limit_blocked_changed.emit(True)
    hub.flush_pending()
    lim = hub.records[-1]
    check("event log: a cut-off is a limit row carrying its reset time",
          lim["kind"] == el.LIMIT
          and abs(lim["data"]["reset_at"] - ag._limit_resets_at) < 1
          and el.status(lim, None, _time.time(), hub.session_start)
          .startswith("in "), lim)
    check("event log: the limit menu's '?' is closed as not-a-question",
          hub.closer_of(menu_q) is not None
          and hub.closer_of(menu_q)["data"].get("limit_menu")
          and hub.open_questions() == 0)
    n = len(hub.records)
    ag.limit_blocked_changed.emit(True)   # a failed verify re-latches
    hub.flush_pending()
    check("event log: re-latching the same cut-off adds no second row",
          len(hub.records) == n)
    ag._tool_waiting = False
    ag._emit_waiting()
    ag._limit_blocked = False
    hub.limit_outcome(ag, "resumed", tries=1)
    res = hub.records[-1]
    check("event log: a resume is its own row AND closes the cut-off",
          res["kind"] == el.LIMIT_RESUMED and res["ref"] == lim["id"]
          and hub.closer_of(lim) is res
          and el.status(lim, res, _time.time(), hub.session_start)
          .startswith("resumed "))

    ag.status = AgentStatus.RUNNING
    ag._set_status(AgentStatus.CRASHED)
    check("event log: a crash is a problem row",
          hub.records[-1]["kind"] == el.CRASHED
          and hub.records[-1]["text"] == "crashed")
    ag.status = AgentStatus.STOPPING
    hub._prev_status[ag.id] = AgentStatus.STOPPING
    ag._set_status(AgentStatus.EXITED_ERR)
    check("event log: an error exit after the user's own stop is just "
          "'stopped'", hub.records[-1]["kind"] == el.LIFECYCLE
          and hub.records[-1]["text"] == "stopped")
    hub.scheduled(ag, False, "run the suite", "the agent is stopped")
    check("event log: a missed scheduled send says why",
          hub.records[-1]["kind"] == el.SCHED_MISSED
          and "the agent is stopped" in el.describe(hub.records[-1]))

    ws.board.append_activity("Agent 1", "refactoring the tiler")
    hub.poll_boards()
    rec = hub.records[-1]
    check("event log: a new board line becomes a board row for its agent",
          rec["kind"] == el.BOARD and rec["text"] == "refactoring the tiler"
          and rec["agent_uid"] == old_agent.spec.uid, rec)

    on_disk = el.read_since(logdir, 0)
    check("event log: every record is on disk as it happens",
          [r["id"] for r in on_disk] == [r["id"] for r in hub.records])
    hub2 = EventHub(mgr, logdir)
    check("event log: a restart reads the history and its closers back",
          len(hub2.records) == len(hub.records)
          and hub2.closer_of(lim)["id"] == res["id"])
    check("event log: a question left open by an earlier run is not "
          "'waiting' forever",
          el.status(dict(q, at=hub2.session_start - 60), None, _time.time(),
                    hub2.session_start) == "no answer recorded")
    hub2.shutdown()

    # --- the window ---
    win = EventLogWindow(hub)
    win.show()
    pump(30)
    kinds = [p["kind"] for k, p in win.model.rows if k == "event"]
    newest = next(r for r in reversed(hub.records)
                  if win.flt.passes(r, hub.closer_of(r), hub.session_start))
    check("event log window: newest first under one 'Today' header",
          win.model.rows[0][0] == "header"
          and win.model.data(win.model.index(0)) == "Today"
          and win.model.rows[1][1]["id"] == newest["id"])
    check("event log window: lifecycle and board rows are off by default, "
          "answers are merged into their question",
          el.LIFECYCLE not in kinds and el.BOARD not in kinds
          and el.ANSWERED not in kinds and el.QUESTION in kinds
          and el.LIMIT_RESUMED in kinds, kinds)
    win.chips["board"].setChecked(True)
    kinds = [p["kind"] for k, p in win.model.rows if k == "event"]
    check("event log window: switching a group on shows its history",
          el.BOARD in kinds)
    win.needs_btn.setChecked(True)
    kinds = {p["kind"] for k, p in win.model.rows if k == "event"}
    check("event log window: 'Needs you' keeps only problems and open "
          "questions", kinds <= {el.CRASHED, el.SCHED_MISSED, el.LIMIT_FAILED,
                                 el.QUESTION} and el.CRASHED in kinds, kinds)
    win.clear_filters()
    check("event log window: Clear filters restores the defaults",
          win.flt.groups == set(el.DEFAULT_GROUPS) and not win.flt.needs_you
          and not win.clear_btn.isVisibleTo(win))
    win._only_agent(ws.id, old_agent.spec.uid)
    uids = {p.get("agent_uid") for k, p in win.model.rows if k == "event"}
    check("event log window: 'Show only this agent' narrows to it",
          uids == {old_agent.spec.uid} or not uids, uids)
    win.clear_filters()

    before = sum(1 for k, _p in win.model.rows if k == "event")
    ag.note_prompt_submitted("one more thing")
    check("event log window: a new record appears live at the top",
          sum(1 for k, _p in win.model.rows if k == "event") == before + 1
          and win.model.rows[1][1]["text"] == "one more thing")

    jumped = []
    win.agentActivated.connect(lambda w, aid: jumped.append((w, aid)))
    prompt_rec = win.model.rows[1][1]
    win._on_link("agent", prompt_rec)
    check("event log window: an agent link resolves the uid to the LIVE "
          "agent id", jumped == [(ws.id, ag.id)], jumped)
    fm = win.view.fontMetrics()
    rect = QRect(0, 0, 900, 24)
    lay = win.delegate._layout(rect, prompt_rec, fm)
    check("event log window: the painted agent name is a hit target",
          win.delegate.hit(rect, prompt_rec, lay["agent"].center(), fm)
          == "agent"
          and win.delegate.hit(rect, prompt_rec, lay["ws"].center(), fm)
          == "ws")
    mgr.remove_terminal(ws.id, ag.id)
    pump(10)
    check("event log window: a removed agent's name is no longer a link",
          hub.resolve(ws.id, prompt_rec["agent_uid"]) is None
          and win.delegate.hit(rect, prompt_rec, lay["agent"].center(), fm)
          is None)
    state = win.state()
    flt = LogFilter()
    flt.load(_json.loads(_json.dumps(state["filter"])))
    check("event log window: filters survive a JSON round trip",
          flt.to_dict() == win.flt.to_dict())
    win.close()
    hub.shutdown()
    n = len(hub.records)
    old_agent.note_prompt_submitted("after shutdown")
    check("event log: nothing is recorded once the app is closing",
          len(hub.records) == n)

    # --- MainWindow wiring ---
    from app.session_store import SessionStore
    from main import create_main_window, setup_application
    setup_application(app)
    store = SessionStore(path=tmp / "mw" / "session.json")
    mw = create_main_window(store)
    mw.show()
    pump(100)
    check("event log: MainWindow owns a hub writing beside session.json",
          mw.event_hub is not None and mw.event_hub.directory
          == str(tmp / "mw" / "event_log"))
    check("event log: the top bar has a Log button",
          mw.top_bar.log_btn.text().endswith("Log"))
    mw.top_bar.set_log_attention(2)
    check("event log: the Log button counts unanswered questions",
          "? 2" in mw.top_bar.log_btn.text()
          and mw.top_bar.log_btn.property("attention") is True)
    mw.top_bar.set_log_attention(0)
    mw.open_event_log()
    pump(30)
    check("event log: Ctrl+Shift+L / the button opens the window",
          mw._event_log_window is not None
          and mw._event_log_window.isVisible())
    mw._event_log_window.chips["lifecycle"].setChecked(True)
    ui = mw._session_payload()["ui"]
    check("event log: the window's filters are saved with the session",
          "lifecycle" in ui["event_log"]["filter"]["groups"])
    mw._event_log_window.close()
    mw.close()


def test_chime_sound_menu_second_click_closes():
    """A second click on a chime row's note button closes its menu. The
    popup closes as for any outside press, and Qt then replayed that press
    to the button, whose click opened the menu again (the bug
    `ornaments.close_on_anchor_press` fixes for the other dropdowns). The
    menu runs a real exec(); a timer inside its loop does the checking and
    then closes it."""
    from PySide6.QtCore import QEvent, QPointF, Qt, QTimer
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication
    from app import chime
    from app.widgets.main_window import TopBar

    QApplication.instance() or QApplication([])
    bar = TopBar()
    btn = bar.chime_sound_btns[chime.QUESTION]
    # the button lives in the Options panel; the filter only counts a
    # press on an anchor that is on screen
    btn.window().show(); QApplication.processEvents()
    seen = {}

    def inside_exec():
        menu = QApplication.activePopupWidget()
        seen["menu"] = menu is not None
        if menu is None:
            return
        g = btn.mapToGlobal(btn.rect().center())
        QApplication.sendEvent(menu, QMouseEvent(
            QEvent.Type.MouseButtonPress, QPointF(menu.mapFromGlobal(g)),
            QPointF(g), Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        seen["no_replay"] = menu.testAttribute(
            Qt.WidgetAttribute.WA_NoMouseReplay)
        menu.close()

    check("chime menu: test button is on screen", btn.isVisible())
    QTimer.singleShot(150, inside_exec)
    btn.click()                   # returns once inside_exec closed the menu
    check("chime menu: the note button opens a menu", seen.get("menu"), seen)
    check("chime menu: a press on its button is not replayed to reopen it",
          seen.get("no_replay"), seen)
    btn.window().hide()
    bar.deleteLater()
