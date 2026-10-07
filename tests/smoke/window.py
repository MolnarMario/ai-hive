"""The main window as a whole: the full app run, providers, themes, the top
bar and Options panel, scheduled sends, visible-text rules and review-fix
regressions."""

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from .harness import ROOT, SCRATCH_CWD, SHOTS, check


def test_ai_title_summary():
    """The per-agent summary follows Claude's /resume title when available,
    using an assigned task only until that title is reported. Latest-title
    parsing takes the LAST ai-title record and re-reads when the file changes."""
    import json as _json
    from PySide6.QtWidgets import QApplication
    from app import transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- transcript parsing: latest ai-title wins, refreshes on change ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-title-"))
    tpath = tmp / "conv.jsonl"
    recs = [{"type": "user", "text": "hi"},
            {"type": "ai-title", "aiTitle": "First guess", "sessionId": "s"},
            {"type": "assistant", "text": "working"},
            {"type": "ai-title", "aiTitle": "Recolor the badge", "sessionId": "s"}]
    tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                     encoding="utf-8")
    check("ai-title: returns the LAST title record",
          transcripts._read_latest_ai_title(str(tpath)) == "Recolor the badge",
          transcripts._read_latest_ai_title(str(tpath)))
    with open(tpath, "a", encoding="utf-8") as fh:   # conversation evolves
        fh.write(_json.dumps({"type": "ai-title", "aiTitle": "Add the spinner",
                              "sessionId": "s"}) + "\n")
    check("ai-title: re-reads when the transcript changes",
          transcripts._read_latest_ai_title(str(tpath)) == "Add the spinner")
    check("ai-title: missing file -> empty string",
          transcripts._read_latest_ai_title(str(tmp / "nope.jsonl")) == "")

    # --- summary precedence on the agent ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd=SCRATCH_CWD))
    seen = []
    a.summary_changed.connect(seen.append)
    check("summary: empty with no task and no title", a.summary() == "")
    a.set_ai_title("Recolor the badge")
    check("summary: falls back to the AI title", a.summary() == "Recolor the badge"
          and seen[-1] == "Recolor the badge", (a.summary(), seen))
    a.set_task("Fix the parser")
    check("summary: AI Hive task does not replace the /resume title",
          a.summary() == "Recolor the badge" and seen[-1] == "Recolor the badge")
    n = len(seen)
    a.set_ai_title("A newer title")
    check("summary: follows the latest /resume title",
          a.summary() == "A newer title" and len(seen) == n + 1
          and seen[-1] == "A newer title", (a.summary(), seen))
    a.deleteLater()


def test_live_model_effort():
    """The card header says which model, effort and permission mode the agent
    is ACTUALLY on. All three change mid-session (/model, /effort, Shift+Tab in
    the terminal), so the reading comes from the transcript: assistant records
    carry message.model + a top-level effort, a /model or /effort pick writes a
    local-command-stdout record the instant the user chooses, and the mode rides
    on every prompt plus a dedicated permission-mode record. Last in file order
    wins. Model and effort are transient (they never touch spec and never save);
    the MODE is written back, because it is the flag that reopens the agent in
    the same mode."""
    import json as _json
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import providers, transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.widgets.terminal_card import TerminalCard

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    # --- model id normalization ---
    cases = [("claude-opus-5", "Opus 5"),
             ("claude-opus-4-8", "Opus 4.8"),
             ("claude-haiku-4-5-20251001", "Haiku 4.5"),
             ("claude-sonnet-5[1m]", "Sonnet 5 (1M)"),
             ("opus", "Opus"),
             ("Opus 4.8 (1M context)", "Opus 4.8 (1M)"),
             ("fable[1m]", "Fable (1M)"),
             ("opusplan", "Opus Plan"),
             ("", "")]
    for raw, want in cases:
        check(f"model: {raw or 'empty'} displays as {want or 'empty'}",
              transcripts.model_display(raw) == want,
              transcripts.model_display(raw))

    # --- the New Terminal model picker offers the CLI's current aliases ---
    claude_prov = providers.get("claude")
    picker_values = [v for _, v in claude_prov.models]
    check("model picker: Default first, then every current CLI alias",
          picker_values[0] == "" and set(picker_values) >= {
              "fable", "fable[1m]", "opus", "opus[1m]", "sonnet",
              "sonnet[1m]", "haiku", "opusplan"}, picker_values)
    check("model picker: labels carry the model version",
          all(any(ch.isdigit() for ch in label)
              for label, v in claude_prov.models if v in (
                  "fable", "opus", "sonnet", "haiku")),
          [label for label, _ in claude_prov.models])
    check("model picker: a 1M alias launches as --model <alias>[1m]",
          providers.build_invocation("claude", model="opus[1m]")[1][:2]
          == ["--model", "opus[1m]"])
    check("model picker: efforts match claude --effort exactly",
          claude_prov.efforts == ("", "low", "medium", "high", "xhigh", "max"))

    # --- transcript parsing ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-model-"))
    tpath = tmp / "conv.jsonl"

    def _turn(model="claude-opus-5", effort="high", side=False):
        return {"type": "assistant", "isSidechain": side, "effort": effort,
                "message": {"model": model, "role": "assistant"}}

    def _pick(text):
        return {"type": "user",
                "message": {"role": "user",
                            "content": f"<local-command-stdout>{text}"
                                       f"</local-command-stdout>"}}

    def write(recs):
        tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                         encoding="utf-8")

    write([{"type": "user", "text": "hi"}, _turn()])
    check("model: a turn reports its model and effort",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # a /model pick AFTER the last turn is the newer truth (the whole point:
    # an idle agent's switch must show without waiting for another turn)
    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions")])
    check("model: a /model pick after the last turn wins",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _pick("Set effort level to max (this session only): Maximum "
                 "capability with deepest reasoning.")])
    check("model: a /effort pick changes only the effort",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5", "max", ""),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to \x1b[1mOpus 4.8 (1M context)\x1b[22m and saved "
                 "as your default for new sessions with \x1b[1mhigh\x1b[22m "
                 "effort")])
    check("model: a pick that names an effort applies both",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8 (1M)", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # newer builds quote the name in backticks; they must not reach the badge
    write([_turn(),
           _pick("Set model to `Sonnet 5.5` and saved as your default for new "
                 "sessions with `high` effort")])
    check("model: a backtick-quoted pick shows no backticks",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5.5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    write([_turn(),
           _pick("Set model to `Opus 5.5 (default)` and saved as your default "
                 "for new sessions")])
    check("model: a pick's (default) note is dropped",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5.5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # a session-only pick has no "and saved" tail to stop the name at
    write([_turn(), _pick("Set model to `Sonnet 5.5` with `low` effort")])
    check("model: a backtick pick with no tail stops at the closing tick",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5.5", "low", ""),
          transcripts._read_model_effort(str(tpath)))
    write([_turn(), _pick("Set model to Sonnet 5.5 with high effort")])
    check("model: a plain pick's name stops before 'with ... effort'",
          transcripts._read_model_effort(str(tpath)) == ("Sonnet 5.5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    # a turn AFTER a pick wins again (ordering is file order, not kind)
    write([_pick("Set model to \x1b[1mSonnet 5\x1b[22m and saved as your "
                 "default for new sessions"),
           _turn(model="claude-fable-5", effort="max")])
    check("model: a turn after a pick wins again",
          transcripts._read_model_effort(str(tpath)) == ("Fable 5", "max", ""),
          transcripts._read_model_effort(str(tpath)))

    # a sub-agent's model is a different context; <synthetic> is not a model
    write([_turn(), _turn(model="claude-haiku-4-5", effort="low", side=True),
           {"type": "assistant", "message": {"model": "<synthetic>"}}])
    check("model: sidechain and synthetic records never win",
          transcripts._read_model_effort(str(tpath)) == ("Opus 5", "high", ""),
          transcripts._read_model_effort(str(tpath)))

    check("model: missing file reads as unknown",
          transcripts.latest_model_effort(str(tmp), "nope") == ("", "", ""))

    # the reader only touches the tail, so it must still find evidence that sits
    # behind a long stretch of unrelated records (full-scan fallback)
    filler = [{"type": "user", "text": "x" * 400} for _ in range(400)]
    write([_turn(model="claude-opus-4-8", effort="xhigh")] + filler)
    check("model: falls back to a full scan when the tail has no evidence",
          transcripts._read_model_effort(str(tpath)) == ("Opus 4.8", "xhigh", ""),
          transcripts._read_model_effort(str(tpath)))

    # --- the permission mode (Shift+Tab), read off the same transcript ---
    def _mode(mode):    # the record Claude appends the instant the mode changes
        return {"type": "permission-mode", "permissionMode": mode}

    write([_turn(), _mode("plan")])
    check("mode: a permission-mode record is the live mode",
          transcripts._read_model_effort(str(tpath))[2] == "plan",
          transcripts._read_model_effort(str(tpath)))
    write([_mode("plan"), _mode("auto")])
    check("mode: the LAST mode record wins",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))
    # an ordinary prompt carries the mode it was submitted under, which is what
    # covers a CLI build that writes no dedicated record
    write([_mode("plan"),
           {"type": "user", "permissionMode": "auto",
            "message": {"role": "user", "content": "go"}}])
    check("mode: a user prompt's own permissionMode counts too",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))
    write([_mode("auto"),
           {"type": "user", "isSidechain": True, "permissionMode": "plan",
            "message": {"role": "user", "content": "sub"}}])
    check("mode: a sidechain's mode is a sub-agent's, never this one's",
          transcripts._read_model_effort(str(tpath))[2] == "auto",
          transcripts._read_model_effort(str(tpath)))

    # translation: the transcript's names are not all launch flags
    check("mode: 'default' is spelled by omitting the flag",
          providers.normalize_permission_mode("default") == ""
          and providers.normalize_permission_mode("manual") == "")
    check("mode: a real CLI token passes through",
          providers.normalize_permission_mode("auto") == "auto"
          and providers.normalize_permission_mode("plan") == "plan")
    check("mode: an unknown token never reaches the command line",
          providers.normalize_permission_mode("wat") == "")
    check("mode: 'default' reads as manual on the card",
          providers.permission_mode_display("default") == "manual"
          and providers.permission_mode_display("") == "manual")
    # ...and a mode adopted from a conversation must be launchable, even though
    # the New Agent dropdown never offers it
    _, auto_args = providers.build_invocation("claude", permission_mode="auto")
    check("mode: 'auto' survives into the launch flags",
          auto_args == ["--permission-mode", "auto"], auto_args)
    _, bogus_args = providers.build_invocation("claude", permission_mode="wat")
    check("mode: a bogus mode is dropped rather than launched", bogus_args == [])

    # the spec REBUILDS its args, or the new mode would persist while every
    # launch in this process kept using the old flag
    mspec = build_spec(AgentKind.CLAUDE, "Mode", cwd=str(tmp))
    check("mode: a fresh spec carries no --permission-mode",
          "--permission-mode" not in mspec.args, mspec.args)
    check("mode: adopting a mode reports the change",
          mspec.set_permission_mode("plan")
          and not mspec.set_permission_mode("plan"))
    check("mode: adopting a mode rebuilds the launch args",
          mspec.args[-2:] == ["--permission-mode", "plan"], mspec.args)
    check("mode: the adopted mode round-trips through the session file",
          AgentSpec.from_dict(mspec.to_dict()).permission_mode == "plan"
          and "--permission-mode" in AgentSpec.from_dict(mspec.to_dict()).args)
    mspec.set_permission_mode("")
    check("mode: going back to the default drops the flag again",
          "--permission-mode" not in mspec.args, mspec.args)

    # --- agent-side badge: transient, emits only on a real change ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd=SCRATCH_CWD,
                                 model="opus", effort="high"))
    seen = []
    a.model_changed.connect(seen.append)
    check("model: badge seeded from the launch flags",
          a.model_badge() == "Opus · high · manual", a.model_badge())
    a.set_live_model("Sonnet 5", "max")
    check("model: badge follows the live reading",
          a.model_badge() == "Sonnet 5 · max · manual"
          and seen[-1] == "Sonnet 5 · max · manual",
          (a.model_badge(), seen))
    n = len(seen)
    a.set_live_model("Sonnet 5", "max")
    check("model: no signal when the reading is unchanged", len(seen) == n)
    a.set_live_model("", "")
    check("model: an empty reading never blanks a good label",
          a.model_badge() == "Sonnet 5 · max · manual" and len(seen) == n)
    a.set_live_model("", "", "plan")
    check("model: badge follows the live permission mode",
          a.model_badge() == "Sonnet 5 · max · plan"
          and seen[-1] == "Sonnet 5 · max · plan", (a.model_badge(), seen))
    a.set_live_model("Sonnet 5", "max", "auto")
    check("model: the mode label is the CLI's own name",
          a.model_badge() == "Sonnet 5 · max · auto", a.model_badge())
    b = TerminalAgent(build_spec(AgentKind.CLAUDE, "Bare", cwd="", model="",
                                 effort=""))
    b._live_model = ""      # no launch flag and no saved user default
    check("model: badge hidden when nothing is known", b.model_badge() == "")
    b.set_live_model("Opus 5", "")
    check("model: model alone renders without an effort",
          b.model_badge() == "Opus 5 · manual", b.model_badge())
    sh = TerminalAgent(build_spec(AgentKind.POWERSHELL, "Shell", cwd=""))
    check("model: a shell has no permission mode to show",
          sh.permission_mode_label() == "" and sh.model_badge() == "",
          (sh.permission_mode_label(), sh.model_badge()))

    # --- header: the chip shows/hides with the badge ---
    card = TerminalCard(a)
    card.resize(900, 300); card.show(); pump(80)
    check("model: card chip shows the live model, effort and mode",
          card.model_label.isVisible()
          and card.model_label.text() == "Sonnet 5 · max · auto",
          card.model_label.text())
    # the kind sublabel (#CardRole) is redundant beside that chip -- on an AI
    # agent spec.role only ever held the provider display name ("Claude Code"),
    # and the ~70px it cost came out of the summary. A shell keeps it: there is
    # no model chip to read instead.
    check("model: the AI card hides its redundant provider sublabel",
          not card.role.isVisible() and card.role.text() == "",
          card.role.text())
    card_sh = TerminalCard(sh)
    card_sh.resize(900, 300); card_sh.show(); pump(60)
    check("model: a shell card keeps its kind sublabel",
          card_sh.role.isVisible() and card_sh.role.text() == "PowerShell",
          card_sh.role.text())
    card_sh.deleteLater()
    card2 = TerminalCard(b)
    b._live_model = ""
    card2._on_model(b.model_badge())
    card2.resize(900, 300); card2.show(); pump(60)
    check("model: card chip hidden when the model is unknown",
          not card2.model_label.isVisible())

    # --- the chip is inked by PROVIDER, and the skin never overrides it ---
    # Colour is the only thing on a split screen that says whose agent a card
    # is without reading the model name, so it belongs to the vendor and not
    # to the theme. Read back the RESOLVED palette rather than the QSS text:
    # a property selector that never matched would still leave the rule in the
    # stylesheet, and the chip would quietly keep the gold-dim default.
    from PySide6.QtGui import QPalette as _QPalette
    from app import ui_theme as _ui_theme

    def _chip_ink(agent_kind):
        ag = TerminalAgent(build_spec(agent_kind, "Ink", cwd=""))
        ag.set_live_model("Opus 5", "high", "auto")
        c = TerminalCard(ag)
        c.resize(900, 300); c.show(); pump(40)
        ink = c.model_label.palette().color(
            _QPalette.ColorRole.WindowText).name()
        c.deleteLater(); ag.deleteLater()
        return ink

    app_inst = QApplication.instance()
    was_theme = _ui_theme.ACTIVE_THEME.id
    ink_seen = {}
    for _tid in _ui_theme.THEMES:
        _ui_theme.apply_theme(_tid)
        app_inst.setStyleSheet(_ui_theme.build_qss())
        ink_seen[_tid] = (_chip_ink(AgentKind.CLAUDE),
                          _chip_ink(AgentKind.GEMINI))
    _ui_theme.apply_theme(was_theme)
    app_inst.setStyleSheet(_ui_theme.build_qss())
    want = (_ui_theme.PROVIDER_INK["claude"], _ui_theme.PROVIDER_INK["gemini"])
    check("model: the chip is Claude terracotta / Gemini blue under EVERY skin",
          all(v == want for v in ink_seen.values()), ink_seen)
    check("model: Claude and Gemini are never the same ink",
          want[0] != want[1], want)

    # --- the manager's poll adopts it, and never saves for it ---
    from app.workspace_manager import WorkspaceManager
    mgr = WorkspaceManager()
    ws = mgr.create_workspace("Models", str(tmp))
    live = mgr.add_terminal(ws.id, build_spec(AgentKind.CLAUDE, "Live",
                                              cwd=str(tmp)), autostart=False)
    live.spec.session_id = "11111111-2222-3333-4444-555555555555"
    live.is_running = lambda: True    # stand in for a launched process
    conv = Path(transcripts.transcript_path(str(tmp), live.spec.session_id))
    conv.parent.mkdir(parents=True, exist_ok=True)
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n", encoding="utf-8")
    dirtied = []
    mgr.dirty.connect(lambda: dirtied.append(True))
    mgr.refresh_model_effort()
    check("model: the manager poll adopts the transcript's model/effort",
          live.model_badge() == "Sonnet 5 · low · manual", live.model_badge())
    check("model: a reading never marks the session dirty", not dirtied, dirtied)

    # ...except the permission mode, which IS the launch flag for next time:
    # the CLI does not carry a mode across --resume, so an agent the user put
    # in plan mode came back ask-each-time on every reopen
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n" + _json.dumps(_mode("plan")) + "\n",
                    encoding="utf-8")
    mgr.refresh_model_effort()
    check("mode: the manager poll shows the live mode",
          live.model_badge() == "Sonnet 5 · low · plan", live.model_badge())
    check("mode: the live mode is written back as the next launch flag",
          live.spec.permission_mode == "plan"
          and live.spec.args[-2:] == ["--permission-mode", "plan"],
          (live.spec.permission_mode, live.spec.args))
    check("mode: adopting a mode marks the session dirty", dirtied)
    check("mode: the adopted mode reaches the persisted session record",
          mgr.to_session_dict()["workspaces"][0]["terminals"][0].get(
              "permission_mode") == "plan")
    dirtied.clear()
    mgr.refresh_model_effort()
    check("mode: an unchanged mode never re-saves", not dirtied, dirtied)
    # the ask-each-time mode is written "default" in the transcript and has no
    # flag spelling, so it must clear the flag rather than launch a bogus one
    conv.write_text(_json.dumps(_turn(model="claude-sonnet-5", effort="low"))
                    + "\n" + _json.dumps(_mode("default")) + "\n",
                    encoding="utf-8")
    mgr.refresh_model_effort()
    check("mode: going back to ask-each-time clears the flag",
          live.spec.permission_mode == ""
          and "--permission-mode" not in live.spec.args, live.spec.args)
    check("mode: clearing it reaches the persisted session record too",
          mgr.to_session_dict()["workspaces"][0]["terminals"][0].get(
              "permission_mode", "-") == "")
    shutil.rmtree(conv.parent, ignore_errors=True)

    # --- the summary uses the width it was actually given ---
    long_task = ("Rework the pty worker so grandchildren die with the job "
                 "object and the graceful stop stays an stdin EOF, then check "
                 "the restart path keeps its pinned conversation")
    a.set_task(long_task)
    pump(80)
    shown = card.task_summary.text()
    check("summary: fits the real header width, not a fixed character count",
          len(shown) > 60, (len(shown), shown))
    check("summary: keeps the untruncated text on hover",
          card.task_summary.toolTip() == long_task)
    card.resize(420, 300); pump(80)
    narrow = card.task_summary.text()
    check("summary: re-fits when the card narrows",
          len(narrow) < len(shown), (len(narrow), len(shown)))
    a.set_task("")
    pump(40)
    check("summary: empty task clears the label",
          card.task_summary.text() == "")

    card.detach(); card2.detach()
    card.close(); card2.close()
    a.deleteLater(); b.deleteLater()
    shutil.rmtree(tmp, ignore_errors=True)


def test_repo_activity_cache():
    """The Open repo dropdown shows a cached list at once. The fetch runs
    in the background (prefetch, or a click on a stale copy), a failed
    refresh keeps the last good list, and a folder change drops it."""
    import inspect
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    import main as main_mod
    from app import repo_activity
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_idle(win):
        for _ in range(100):
            if not win._repo_activity_busy:
                return
            pump(20)

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-repoact-"))
    pa, pb = tmp / "a", tmp / "b"
    pa.mkdir(); pb.mkdir()
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    calls = []
    answer = {"error": ""}

    def fake_fetch(path):
        calls.append(path)
        if answer["error"]:
            return {"pull_requests": [], "commits": [], "repo_url": "",
                    "error": answer["error"]}
        return {"pull_requests": [{"number": 7, "title": "Seven",
                                   "state": "open", "merged": False,
                                   "url": "https://github.com/o/r/pull/7"}],
                "commits": [{"sha": "abcdef1234", "message": "first line",
                             "url": "https://github.com/o/r/commit/abc"}],
                "error": "", "repo_url": "https://github.com/o/r"}

    win._repo_activity_fetch = fake_fetch
    mgr = win.manager
    wa = mgr.workspaces[0]
    mgr.set_workspace_path(wa.id, str(pa))
    wb = mgr.create_workspace("B", str(pb))
    mgr.set_active(wb.id)
    page = win._pages[wa.id]

    def labels():
        return [a.text() for a in page.repo_activity_menu.actions()
                if a.text()]

    check("repo activity: the menu opens on a loading line before any fetch",
          labels() == ["Loading recent GitHub activity…"], labels())

    win._prefetch_repo_activity()
    wait_idle(win)
    check("repo activity: prefetch fetches every workspace, the visible "
          "one first", calls == [str(pb), str(pa)], calls)
    check("repo activity: the prefetched list is in the menu",
          "#7  Seven" in labels(), labels())

    menu_actions = page.repo_activity_menu.actions()
    page.repoActivityRequested.emit(wa.id)
    pump(30)
    check("repo activity: a click on a fresh copy fetches nothing",
          len(calls) == 2, calls)

    win._repo_activity[wa.id]["at"] -= repo_activity.STALE_AFTER_S + 1
    page.repoActivityRequested.emit(wa.id)
    wait_idle(win)
    check("repo activity: a click on a stale copy refreshes it",
          calls[2:] == [str(pa)], calls)
    check("repo activity: an unchanged refresh leaves the menu untouched",
          page.repo_activity_menu.actions() == menu_actions)

    answer["error"] = "Could not load GitHub activity: HTTP Error 403"
    win._repo_activity[wa.id]["at"] -= repo_activity.STALE_AFTER_S + 1
    page.repoActivityRequested.emit(wa.id)
    wait_idle(win)
    check("repo activity: a failed refresh keeps the last good list",
          "#7  Seven" in labels(), labels())
    page.repoActivityRequested.emit(wa.id)
    wait_idle(win)
    check("repo activity: after a failed refresh the next click retries",
          len(calls) == 5, calls)

    mgr.set_workspace_path(wa.id, str(pb))
    check("repo activity: a new folder drops the old list",
          wa.id not in win._repo_activity
          and labels() == ["Loading recent GitHub activity…"], labels())
    page.repoActivityRequested.emit(wa.id)
    wait_idle(win)
    check("repo activity: an error with nothing cached shows the error",
          labels() == [answer["error"]], labels())
    page.repoActivityRequested.emit(wa.id)
    wait_idle(win)
    check("repo activity: an error is never treated as fresh",
          len(calls) == 7, calls)

    check("repo activity: create_main_window never starts the prefetch",
          "start_repo_activity_prefetch"
          not in inspect.getsource(main_mod.create_main_window))
    src = inspect.getsource(main_mod.main)
    check("repo activity: main() starts it last, after the startup recovery",
          src.find("start_repo_activity_prefetch")
          > src.find("recover_blocked_at_startup") > 0)

    win.close()
    pump(100)
    shutil.rmtree(tmp, ignore_errors=True)


def test_repo_activity_browser_url():
    """The origin remote forms git prints map to the github.com page."""
    from app import repo_activity

    cases = {
        "git@github.com:o/r.git": "https://github.com/o/r",
        "https://github.com/o/r.git/": "https://github.com/o/r",
        "ssh://git@github.com/o/r.git": "https://github.com/o/r",
        "https://gitlab.com/o/r.git": "",
        "": "",
    }
    got = {k: repo_activity.browser_url(k) for k in cases}
    check("repo url: origin forms map to the github.com browser URL",
          got == cases, got)


def test_no_em_dashes_in_visible_text():
    """No em dash reaches the reader. The app's visible strings (labels,
    tooltips, dialog copy, terminal notices, the board markdown) are checked by
    parsing every module and looking at string literals that are NOT
    docstrings; comments and docstrings are prose for us, not for the user, and
    are deliberately out of scope."""
    import ast

    targets = [ROOT / "main.py"] + sorted((ROOT / "app").rglob("*.py"))
    offenders = []
    for path in targets:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        docs = set()
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) and body \
                    and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docs and "—" in node.value:
                offenders.append(f"{path.name}:{node.lineno}")
    check("text: no em dash in any user-visible string",
          not offenders, ", ".join(offenders[:8]))


# ------------------------------------------------------------ application ---

def test_app():
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtCore import QProcess
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.tiling import compute_grid
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)
    check("app: stylesheet applied", "TerminalCard" in app.styleSheet())

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_until(pred, timeout_ms=10000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    def snap(widget, name):
        widget.grab().save(str(SHOTS / f"{name}.png"))

    # isolated session dir + two project dirs for the cwd checks
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-smoke-"))
    proj_alpha = tmp / "proj-alpha"
    proj_bravo = tmp / "proj-bravo"
    proj_alpha.mkdir()
    proj_bravo.mkdir()
    store = SessionStore(path=tmp / "session.json")

    # -- 1. boot ----------------------------------------------------------
    win = create_main_window(store)
    win.resize(1600, 900)
    win.show()
    pump(200)
    mgr = win.manager
    check("boot: window shown", win.isVisible())
    check("boot: first-run default workspace exists", len(mgr.workspaces) == 1)
    check("boot: first-run default agent is idle",
          len(mgr.workspaces[0].agents) == 1
          and not mgr.workspaces[0].agents[0].is_running())
    snap(win, "01_boot")

    # -- 3. workspaces (model API + real widget wiring) --------------------
    alpha = mgr.workspaces[0]
    mgr.rename_workspace(alpha.id, "Alpha")
    alpha.project_path = str(proj_alpha)
    bravo = mgr.create_workspace("Beta", str(proj_bravo))
    pump(100)
    check("ws: sidebar has 2 rows", len(win.sidebar._rows) == 2)
    check("ws: rename reflected in sidebar",
          win.sidebar._rows[alpha.id][1].name_label.text() == "Alpha")
    mgr.rename_workspace(bravo.id, "Bravo")
    pump(50)
    check("ws: second rename reflected",
          win.sidebar._rows[bravo.id][1].name_label.text() == "Bravo")
    check("ws: Alpha row is active",
          win.sidebar._rows[alpha.id][1].property("active") is True
          and win.sidebar._rows[bravo.id][1].property("active") is False)

    # remove the default idle agent so the tiling count starts at 0
    mgr.remove_terminal(alpha.id, alpha.agents[0].id)
    pump(100)
    page = win._pages[alpha.id]
    check("ws: empty state visible with 0 terminals", page.empty.isVisible())
    snap(win, "02_two_workspaces")

    # -- 4. incremental tiling 1..6 against the applied layout -------------
    def assert_layout(page, n):
        plan = compute_grid(n)
        ok = len(page.cards) == n
        detail = ""
        for i, card in enumerate(page.cards):
            got = page.grid.getItemPosition(page.grid.indexOf(card))
            want = tuple(plan.cells[i])
            if got != want:
                ok = False
                detail = f"card {i}: got {got}, want {want}"
                break
        for c in range(plan.vcols, page.grid.columnCount()):
            if page.grid.columnStretch(c) != 0:
                ok = False
                detail = f"stale column stretch at col {c}"
        for r in range(plan.rows, page.grid.rowCount()):
            if page.grid.rowStretch(r) != 0:
                ok = False
                detail = f"stale row stretch at row {r}"
        check(f"tiling applied: n={n}", ok, detail)

    idle_ids = []
    for n in range(1, 7):
        agent = mgr.add_terminal(
            alpha.id, build_spec(AgentKind.CMD, f"Tile {n}",
                                 cwd=str(proj_alpha)), autostart=False)
        idle_ids.append(agent.id)
        pump(30)
        assert_layout(page, n)
        if n in (1, 3, 5, 6):
            snap(win, f"03_tiles_{n}")
    check("tiling: sidebar stats show 6 total",
          mgr.workspace_stats(alpha.id)["total"] == 6)

    # shrink 6 -> 3 exercises the stale-stretch reset (vcols 3 -> 2)
    for agent_id in idle_ids[3:]:
        mgr.remove_terminal(alpha.id, agent_id)
    pump(50)
    assert_layout(page, 3)
    snap(win, "04_shrink_3")

    # -- 4b. maximize / restore a single card (solo view) ------------------
    # solo is a transient VIEW toggle: it must never persist or touch siblings'
    # processes, and restoring must reproduce the prior tiling verbatim. Every
    # branch below leaves the original 3 idle cards intact for section 5.
    solo_dirty = {"n": 0}
    _solo_conn = mgr.dirty.connect(
        lambda: solo_dirty.__setitem__("n", solo_dirty["n"] + 1))
    page.toggle_solo(page.cards[0])
    pump(30)
    max_pos = page.grid.getItemPosition(page.grid.indexOf(page.cards[0]))
    check("maximize: soloed card spans full grid at (0,0)",
          page.cards[0].isVisible() and max_pos == (0, 0, 1, 1), max_pos)
    check("maximize: sibling cards hidden (processes untouched)",
          not page.cards[1].isVisible() and not page.cards[2].isVisible())
    from PySide6.QtGui import QRawFont
    from app.widgets.terminal_card import MAXIMIZE_GLYPH, RESTORE_GLYPH
    check("maximize: button shows Restore glyph",
          page.cards[0].btn_max.text() == RESTORE_GLYPH)
    # the two squares are private-use codepoints, so the QSS rule must give
    # the button a font that holds them itself. QRawFont looks at that one
    # font only; QFontMetrics.inFontUcs4 counts fallback and says yes for any.
    _max_raw = QRawFont.fromFont(page.cards[0].btn_max.font())
    check("maximize: button font holds the maximize and restore glyphs",
          _max_raw.supportsCharacter(ord(MAXIMIZE_GLYPH))
          and _max_raw.supportsCharacter(ord(RESTORE_GLYPH)),
          _max_raw.familyName())
    check("maximize: no stale row/col stretch while soloed",
          page.grid.columnStretch(1) == 0 and page.grid.rowStretch(1) == 0)
    snap(win, "04b_maximized")

    page.toggle_solo(page.cards[0])   # restore
    pump(30)
    assert_layout(page, 3)            # exact prior arrangement reproduced
    check("restore: all cards visible again",
          all(c.isVisible() for c in page.cards))
    check("restore: button shows Maximize glyph",
          page.cards[0].btn_max.text() == MAXIMIZE_GLYPH)
    check("maximize: solo toggling never marks the session dirty",
          solo_dirty["n"] == 0, solo_dirty["n"])
    mgr.dirty.disconnect(_solo_conn)

    # adding an agent while maximized exits solo so the new card is visible
    page.toggle_solo(page.cards[0])
    pump(20)
    solo_tmp = mgr.add_terminal(
        alpha.id, build_spec(AgentKind.CMD, "Solo Tmp",
                             cwd=str(proj_alpha)), autostart=False)
    pump(30)
    tmp_card = page.card_for(solo_tmp.id)
    check("maximize: adding an agent exits solo",
          page._solo_card is None and tmp_card.isVisible())

    # closing the maximized card auto-restores (never strands a blank area);
    # close the throwaway card so section 5 still sees the original 3 idle cards
    page.toggle_solo(tmp_card)
    pump(20)
    mgr.remove_terminal(alpha.id, solo_tmp.id)
    pump(30)
    check("maximize: closing the soloed card exits solo",
          page._solo_card is None)
    assert_layout(page, 3)

    # -- 4c. context-usage badge renders on the card header ----------------
    tok_card = page.cards[0]
    check("tokens: card badge hidden before any usage",
          not tok_card.token_label.isVisible())
    tok_card.agent.set_token_usage(200_000, 1_000_000)
    pump(20)
    check("tokens: card badge shows the percentage",
          tok_card.token_label.isVisible()
          and tok_card.token_label.text() == "20% of 1M",
          tok_card.token_label.text())
    tok_card.agent.set_token_usage(0, 0)   # reset so nothing leaks downstream
    pump(20)
    check("tokens: card badge hides again when usage clears",
          not tok_card.token_label.isVisible())

    # -- 4d. header tools collapse until hovered ---------------------------
    # A-/A+/maximize cost ~100px of every header for actions with keyboard
    # equivalents, and that width comes out of the task summary. They collapse
    # to a hint strip and expand on hover; the usage chip and the close button
    # sit to their RIGHT, and the summary carries the stretch, so nothing to
    # the right of the strip moves as the pointer crosses it.
    from PySide6.QtGui import QEnterEvent
    from PySide6.QtCore import QEvent, QPointF
    tools = tok_card.header_tools
    hlay = tok_card.header.layout()
    check("header tools: buttons hidden while the pointer is elsewhere",
          not tok_card.btn_font_dec.isVisible()
          and not tok_card.btn_font_inc.isVisible()
          and not tok_card.btn_max.isVisible()
          and tools.hint.isVisibleTo(tools))
    check("header tools: usage chip and close sit right of the strip",
          hlay.indexOf(tools) < hlay.indexOf(tok_card.token_label)
          < hlay.indexOf(tok_card.btn_close))
    collapsed_w = tools.width()
    pos = QPointF(2, 2)
    tools.enterEvent(QEnterEvent(pos, pos, tools.mapToGlobal(pos)))
    pump(10)
    check("header tools: hover reveals all three buttons",
          tok_card.btn_font_dec.isVisibleTo(tools.tray)
          and tok_card.btn_font_inc.isVisibleTo(tools.tray)
          and tok_card.btn_max.isVisibleTo(tools.tray)
          and tools.tray.isVisible()
          and not tools.hint.isVisibleTo(tools))
    check("header tools: expanding costs the layout nothing",
          tools.width() == collapsed_w,
          (collapsed_w, tools.width()))
    check("header tools: the tray floats left of the strip, inside the header",
          tools.tray.parentWidget() is tok_card.header
          and tools.tray.geometry().right() <= tools.geometry().right() + 1
          and tools.tray.width() > collapsed_w + 40,
          (tools.tray.geometry(), tools.geometry()))
    # REGRESSION: past three cards across a 1080p screen the header is already
    # over-subscribed, and Qt answers a layout it cannot satisfy by shrinking
    # every item BELOW its minimum -- which used to hand a 65px button 39px
    # and leave QToolButton eliding "A-" and "A+" to "...", i.e. three
    # ellipses where two font steppers should be. The tray is not in the
    # layout, so a narrow card cannot squeeze it.
    tok_card.header.setFixedWidth(470)   # 4 cards across a 1080p screen
    pump(10)
    check("header tools: a narrow card cannot squeeze the buttons",
          all(b.width() >= b.sizeHint().width()
              for b in (tok_card.btn_font_dec, tok_card.btn_font_inc,
                        tok_card.btn_max)),
          [(b.width(), b.sizeHint().width())
           for b in (tok_card.btn_font_dec, tok_card.btn_font_inc,
                     tok_card.btn_max)])
    check("header tools: the tray stays inside a narrow header",
          0 <= tools.tray.x()
          and tools.tray.geometry().right() < tok_card.header.width(),
          (tools.tray.geometry(), tok_card.header.width()))
    tok_card.header.setMinimumWidth(0)
    tok_card.header.setMaximumWidth(16777215)
    pump(10)
    # the real cursor is nowhere near an offscreen widget, so the deferred
    # re-check (which is what keeps the buttons up while the pointer is over
    # one of them) collapses again
    tools.leaveEvent(QEvent(QEvent.Type.Leave))
    pump(20)
    check("header tools: collapse again once the pointer leaves",
          not tok_card.btn_max.isVisibleTo(tools.tray)
          and not tools.tray.isVisible()
          and tools.hint.isVisibleTo(tools))

    # -- 5. live streaming --------------------------------------------------
    ticker = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Ticker", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c",
              "import time\nfor i in range(2000): print(f'tick {i}', flush=True); time.sleep(0.05)"]))
    ticker_card = page.card_for(ticker.id)
    check("stream: live output reaches console",
          wait_until(lambda: "tick 5" in ticker_card.console.toPlainText(), 15000),
          ascii(ticker_card.console.toPlainText()[-200:]))
    snap(win, "05_streaming")

    # -- 6. stdin round-trip via the real input line ------------------------
    echo = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Echo", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c",
              "import sys\nfor line in sys.stdin: print('echo:'+line.strip(), flush=True)"]))
    echo_card = page.card_for(echo.id)
    wait_until(lambda: echo.is_running(), 8000)
    echo_card.input.setText("hello hive")
    QTest.keyClick(echo_card.input, Qt.Key.Key_Return)
    check("stdin: round-trip through input line",
          wait_until(lambda: "echo:hello hive" in echo_card.console.toPlainText(),
                     10000),
          ascii(echo_card.console.toPlainText()[-200:]))
    check("stdin: input line cleared after submit", echo_card.input.text() == "")
    check("stdin: local echo rendered",
          "> hello hive" in echo_card.console.toPlainText())

    # -- 7. terminal cwd == workspace project folder ------------------------
    cwd_agent = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CUSTOM, "Cwd", cwd=str(proj_alpha),
        program=sys.executable,
        args=["-u", "-c", "import os; print('CWD=' + os.getcwd(), flush=True)"]))
    cwd_card = page.card_for(cwd_agent.id)
    check("cwd: terminal opens in the workspace project folder",
          wait_until(lambda: f"CWD={proj_alpha}" in cwd_card.console.toPlainText(),
                     10000),
          ascii(cwd_card.console.toPlainText()[-200:]))
    mgr.remove_terminal(alpha.id, cwd_agent.id)
    mgr.remove_terminal(alpha.id, echo.id)
    pump(100)
    assert_layout(page, 4)  # 3 idle + ticker

    # -- 8. background retention across workspace switch --------------------
    bravo_row = win.sidebar._rows[bravo.id][1]
    QTest.mouseClick(bravo_row, Qt.MouseButton.LeftButton)  # real click
    pump(100)
    check("retention: Bravo is active after row click",
          mgr.active_id == bravo.id)
    check("retention: ticker card hidden", not ticker_card.isVisible())
    before = len(ticker_card.console.toPlainText())
    pump(1500)
    after = len(ticker_card.console.toPlainText())
    check("retention: hidden terminal kept streaming",
          after > before and not ticker_card.isVisible(),
          f"before={before} after={after}")
    check("retention: process still running while hidden", ticker.is_running())
    QTest.mouseClick(win.sidebar._rows[alpha.id][1], Qt.MouseButton.LeftButton)
    pump(100)
    check("retention: back to Alpha", mgr.active_id == alpha.id)
    snap(win, "06_after_switch")

    # -- 8b. review-fix regressions -----------------------------------------
    # rename: Escape must cancel, not commit
    alpha_row = win.sidebar._rows[alpha.id][1]
    alpha_row.start_rename()
    alpha_row.rename_edit.setText("ShouldNotStick")
    QTest.keyClick(alpha_row.rename_edit, Qt.Key.Key_Escape)
    pump(50)
    check("rename: Escape cancels instead of committing",
          mgr.workspace(alpha.id).name == "Alpha"
          and alpha_row.name_label.text() == "Alpha",
          mgr.workspace(alpha.id).name)
    # rename: Enter commits exactly once
    alpha_row.start_rename()
    alpha_row.rename_edit.setText("AlphaPrime")
    QTest.keyClick(alpha_row.rename_edit, Qt.Key.Key_Return)
    pump(50)
    check("rename: Enter commits", mgr.workspace(alpha.id).name == "AlphaPrime")
    mgr.rename_workspace(alpha.id, "Alpha")
    pump(50)

    # card header: inline agent-name rename (mirrors the workspace-row UX)
    rc = page.cards[0]
    orig_name = rc.agent.spec.name
    orig_role = rc.agent.spec.role
    QTest.mouseDClick(rc.title, Qt.MouseButton.LeftButton)
    check("card rename: double-click opens the editor",
          rc._renaming and rc.title_edit.isVisible())
    # Escape cancels without changing the name
    rc.title_edit.setText("NopeName")
    QTest.keyClick(rc.title_edit, Qt.Key.Key_Escape)
    pump(50)
    check("card rename: Escape cancels",
          not rc._renaming and rc.agent.spec.name == orig_name
          and rc.title.text() == orig_name, rc.agent.spec.name)
    # Enter commits: set_name updates the name+title, role stays, name is custom
    rc.start_rename()
    rc.title_edit.setText("MyCoder")
    QTest.keyClick(rc.title_edit, Qt.Key.Key_Return)
    pump(50)
    check("card rename: Enter commits the new name",
          rc.agent.spec.name == "MyCoder" and rc.title.text() == "MyCoder"
          and rc.agent.spec.role == orig_role and rc.agent.spec.custom_name,
          f"name={rc.agent.spec.name} role={rc.agent.spec.role}")

    # task summary: the header shows what the agent is working on (its task) to
    # the right of the name, so several agents are tellable apart at a glance
    long_task = "Refactor the authentication module and add integration tests"
    rc.agent.set_task(long_task)
    pump(20)
    check("task summary: header shows the current task",
          rc.task_summary.isVisible()
          and rc.task_summary.toolTip() == long_task
          and rc.task_summary.text().startswith("Refactor"),
          rc.task_summary.text())
    rc.agent.set_task("")
    pump(20)
    # the label stays in the layout (it carries the header's stretch, keeping
    # the name left-aligned) but shows NO text when there is no task
    check("task summary: blank when there is no task",
          rc.task_summary.text() == "", rc.task_summary.text())

    # history: draft survives an accidental Up
    any_card = page.cards[0]
    any_card._history = ["old command"]
    any_card._hist_idx = 1
    any_card.input.setText("my draft")
    QTest.keyClick(any_card.input, Qt.Key.Key_Up)
    check("history: Up recalls previous", any_card.input.text() == "old command")
    QTest.keyClick(any_card.input, Qt.Key.Key_Down)
    check("history: Down restores draft", any_card.input.text() == "my draft")
    any_card.input.clear()

    # TTY-only commands get a local hint; cls/clear clears the console
    any_card.agent.send_command("claude")
    check("hint: bare claude explains the line-mode limitation",
          "can't run here" in any_card.console.toPlainText()
          and 'Claude Code (interactive)' in any_card.console.toPlainText())
    any_card.agent.send_command('claude -p "hello"')
    check("hint: claude -p passes without the hint",
          any_card.console.toPlainText().count("can't run here") == 1)
    any_card.agent.send_command("vim notes.txt")
    check("hint: vim flagged as full-screen app",
          "full-screen terminal app" in any_card.console.toPlainText())
    any_card.agent.send_command("cls")
    check("clear: cls empties the console locally",
          any_card.console.toPlainText() == ""
          and len(any_card.agent.log) == 0)

    # session dict carries per-terminal run state (lazy-restore correctness)
    payload = mgr.to_session_dict()
    alpha_terms = next(w for w in payload["workspaces"]
                       if w["id"] == alpha.id)["terminals"]
    check("session: per-terminal run state persisted",
          any(t["running"] for t in alpha_terms)
          and any(not t["running"] for t in alpha_terms), alpha_terms)

    # per-workspace numbering: seeded from THIS workspace's agents, and each
    # workspace counts independently (Feature 1)
    mgr.add_terminal(alpha.id, build_spec(AgentKind.CMD, "Agent 7",
                                          cwd=str(proj_alpha)), autostart=False)
    check("naming: proposal seeded from existing agents",
          mgr.next_agent_name(alpha.id) == "Agent 8", mgr.next_agent_name(alpha.id))
    check("naming: other workspace resets to Agent 1",
          mgr.next_agent_name(bravo.id) == "Agent 1", mgr.next_agent_name(bravo.id))
    mgr.remove_terminal(alpha.id, alpha.agents[-1].id)
    pump(50)

    # -- 9. close a card via its real ✕ button ------------------------------
    proc = ticker.worker.process()
    QTest.mouseClick(page.card_for(ticker.id).btn_close,
                     Qt.MouseButton.LeftButton)
    check("close: process terminated",
          wait_until(lambda: proc.state() == QProcess.ProcessState.NotRunning,
                     8000))
    pump(100)
    check("close: card removed from page",
          page.card_for(ticker.id) is None and len(page.cards) == 3)
    assert_layout(page, 3)
    snap(win, "07_after_close")

    # -- 10. clean shutdown: no zombies, session saved -----------------------
    zombie = mgr.add_terminal(alpha.id, build_spec(
        AgentKind.CMD, "Zombie", cwd=str(proj_alpha)))
    wait_until(lambda: zombie.is_running(), 10000)
    zombie.send_command("ping -t 127.0.0.1")
    wait_until(lambda: "Reply" in page.card_for(zombie.id).console.toPlainText()
               or "127.0.0.1" in page.card_for(zombie.id).console.toPlainText(),
               10000)
    all_agents = mgr.all_agents()
    # the grandchildren THIS test started (ping under the zombie's cmd), by
    # pid: a machine-wide "is any PING.EXE running" failed whenever anything
    # else on the box happened to be pinging
    root_pid = zombie.worker.process().processId()

    def grandkid_pids():
        return [pid for pid in zombie.worker.job_process_ids()
                if pid != root_pid]
    # the console echoes the typed command at once, so wait for the job
    # itself to hold the ping before trusting the list
    wait_until(lambda: bool(grandkid_pids()), 10000)
    grandkids = grandkid_pids()
    win.close()
    procs = [a.worker.process() for a in all_agents if a.worker.process()]
    check("shutdown: every child process terminated",
          wait_until(lambda: all(
              p.state() == QProcess.ProcessState.NotRunning for p in procs), 6000))
    pump(300)

    def pid_alive(pid):
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    check("shutdown: the zombie's grandchildren died with it (job tree kill)",
          grandkids and not any(pid_alive(pid) for pid in grandkids),
          grandkids)
    check("shutdown: session persisted",
          (tmp / "session.json").exists()
          and "Bravo" in (tmp / "session.json").read_text(encoding="utf-8"))

    # -- 11. restore round-trip: lazy start honors saved run state ----------
    win2 = create_main_window(store)
    win2.resize(1600, 900)
    win2.show()
    pump(150)
    mgr2 = win2.manager
    alpha2 = next(w for w in mgr2.workspaces if w.name == "Alpha")
    running_flags = [a.autostart_on_restore for a in alpha2.agents]
    check("restore: run state loaded per agent",
          any(running_flags) and not all(running_flags), running_flags)
    win2.autostart_active_workspace()
    to_start = [a for a in alpha2.agents if a.autostart_on_restore]
    to_stay = [a for a in alpha2.agents if not a.autostart_on_restore]
    check("restore: previously-running agents autostart",
          wait_until(lambda: all(a.is_running() for a in to_start), 15000))
    pump(500)
    check("restore: stopped agents stay idle (no side-effect re-runs)",
          all(not a.is_running() for a in to_stay))
    win2.close()
    procs2 = [a.worker.process() for a in mgr2.all_agents()
              if a.worker.process()]
    check("restore: second shutdown clean",
          wait_until(lambda: all(
              p.state() == QProcess.ProcessState.NotRunning for p in procs2),
              6000))
    pump(200)

    shutil.rmtree(tmp, ignore_errors=True)


def test_providers_grid_and_workspace():
    """Provider argv building, explicit grids, per-workspace numbering, stats,
    folder changes and shared-board awareness, the last through the real
    widgets. (Was test_v2_features; check names keep their "v2" prefix.)"""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import providers, ui_theme
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.tiling import Cell, explicit_grid, parse_layout
    from app.workspace_manager import WorkspaceManager
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v2-"))
    proj_a, proj_b = tmp / "a", tmp / "b"
    proj_a.mkdir(); proj_b.mkdir()

    # ---- providers / model / effort (#4) --------------------------------
    prog, args = providers.build_invocation("claude", model="opus", effort="high")
    check("v2 providers: claude --model/--effort flags",
          args == ["--model", "opus", "--effort", "high"], args)
    spec = build_spec(AgentKind.CLAUDE, "C", cwd=str(proj_a), model="sonnet", effort="max")
    check("v2 providers: build_spec routes claude + pty",
          spec.provider == "claude" and spec.pty
          and "--model" in spec.effective_args() and "max" in spec.effective_args())
    # template expansion must not depend on whether the CLI is installed on
    # this machine (Codex may legitimately exist here)
    check("v2 providers: current openai model template expands",
          "gpt-5.6" in providers.build_invocation("openai", model="gpt-5.6")[1])
    prog, args = providers.build_invocation(
        "openai", custom_command='"C:\\Program Files\\OpenAI\\codex.exe" --full-auto')
    check("v2 providers: quoted Windows path unwrapped for launch",
          prog == r"C:\Program Files\OpenAI\codex.exe" and args == ["--full-auto"],
          (prog, args))
    # Gemini rides the Antigravity CLI (agy): its model values are multiword
    # display strings and MUST stay one argv entry, and its spec shares
    # Claude's --continue resume + --add-dir board access (agy 1.0.16)
    _, gargs = providers.build_invocation("gemini", model="Gemini 3.6 Flash (High)", effort="high")
    check("v2 providers: gemini model flag builds correctly",
          gargs == ["--model", "Gemini 3.6 Flash (High)"], gargs)
    gspec = build_spec(AgentKind.GEMINI, "G", cwd=str(proj_a),
                       model="Gemini 3.6 Flash (High)", effort="high")
    gspec.resume = True
    gspec.extra_dirs = [str(proj_a)]
    # no pinned conversation id, so this is the --continue fallback
    check("v2 providers: gemini resumes with --continue + board --add-dir",
          gspec.provider == "gemini" and gspec.pty
          and "--continue" in gspec.effective_args()
          and "--add-dir" in gspec.effective_args())

    # Grok rides the xAI CLI (grok 0.2.93): single-token model ids expand
    # through -m, it's a template provider (no MCP/system-prompt wiring), and
    # its spec resumes with --continue like Claude/Gemini.
    _, xargs = providers.build_invocation("grok", model="grok-build")
    check("v2 providers: grok template expands -m {model}",
          xargs == ["-m", "grok-build"], xargs)
    _, xdef = providers.build_invocation("grok", model="")
    check("v2 providers: grok default omits -m flag", xdef == [], xdef)
    xspec = build_spec(AgentKind.GROK, "X", cwd=str(proj_a), model="grok-build")
    check("v2 providers: build_spec routes grok + pty",
          xspec.provider == "grok" and xspec.pty
          and "-m" in xspec.effective_args())
    xspec.resume = True
    check("v2 providers: grok resumes with --continue",
          "--continue" in xspec.effective_args())
    # grok has no --add-dir (it uses --cwd), so extra_dirs must never leak in
    xspec2 = build_spec(AgentKind.GROK, "X2", cwd=str(proj_a))
    xspec2.resume = True
    xspec2.extra_dirs = [str(proj_a)]
    check("v2 providers: grok never emits --add-dir",
          "--add-dir" not in xspec2.effective_args())

    # ---- explicit grid (#7) ---------------------------------------------
    ep = explicit_grid(2, 2, 2)
    check("v2 grid: 2x2/2 agents fills TL,TR + 2 empty",
          ep.agent_cells == [Cell(0, 0, 1, 1), Cell(0, 1, 1, 1)]
          and len(ep.empty_cells) == 2)
    check("v2 grid: over-capacity rebalances squarish (2x2/5 -> 3 cols x 2 rows)",
          explicit_grid(2, 2, 5).rows == 2 and explicit_grid(2, 2, 5).cols == 3)
    # overflow of a horizontal strip must widen/wrap naturally, not stack a
    # sparse extra row (the reported bug): 2x1 + a 3rd agent -> 3x1 (not 2x2),
    # 3x1 + a 4th -> 2x2 (not 3x2).
    e21 = explicit_grid(1, 2, 3)     # was 2x1, now 3 agents
    check("v2 grid: 2x1 + 3rd agent -> 3x1 (not 2x2)",
          (e21.rows, e21.cols) == (1, 3) and len(e21.empty_cells) == 0)
    e31 = explicit_grid(1, 3, 4)     # was 3x1, now 4 agents
    check("v2 grid: 3x1 + 4th agent -> 2x2 (not 3x2)",
          (e31.rows, e31.cols) == (2, 2) and len(e31.empty_cells) == 0)
    # a deliberate vertical stack stays vertical on overflow (orientation bias)
    e12 = explicit_grid(2, 1, 3)     # was 1x2, now 3 agents
    check("v2 grid: 1x2 + 3rd agent stays vertical -> 1x3",
          (e12.rows, e12.cols) == (3, 1))
    # layout strings read WIDTH x HEIGHT (like screen resolutions): "3x1" is
    # three cards side by side, "1x3" three stacked. Parsing them rows-first
    # made every non-square layout apply TRANSPOSED vs its palette diagram.
    check("v2 grid: parse_layout is width x height",
          parse_layout("auto") is None
          and parse_layout("3x2") == (2, 3)      # 3 across, 2 down
          and parse_layout("3x1") == (1, 3)      # horizontal strip
          and parse_layout("1x3") == (3, 1))     # vertical stack
    from app.widgets.grid_selector import LAYOUTS
    mismatched = [(s, r, c, parse_layout(s))
                  for (_lbl, s, r, c) in LAYOUTS
                  if s != "auto" and parse_layout(s) != (r, c)]
    check("v2 grid: every palette swatch matches its applied layout (WYSIWYG)",
          not mismatched, mismatched)
    offered = {s for _lbl, s, _r, _c in LAYOUTS}
    check("v2 grid: 3x1, 1x3, 4x1, 1x4 offered",
          {"3x1", "1x3", "4x1", "1x4"} <= offered, offered)

    # ---- through the real app: numbering, stats, folder, board ----------
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.resize(1500, 900)
    win.show()
    pump(150)
    mgr = win.manager
    wa = mgr.workspaces[0]
    mgr.rename_workspace(wa.id, "Alpha")
    mgr.set_workspace_path(wa.id, str(proj_a))
    wb = mgr.create_workspace("Beta", str(proj_b))

    # per-workspace numbering (#1)
    mgr.remove_terminal(wa.id, wa.agents[0].id)
    mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, mgr.next_agent_name(wa.id),
                                       cwd=str(proj_a)), autostart=False)
    check("v2 numbering: Alpha next is Agent 2", mgr.next_agent_name(wa.id) == "Agent 2")
    check("v2 numbering: Beta resets to Agent 1", mgr.next_agent_name(wb.id) == "Agent 1")

    # dashboard stats (#2) reach the sidebar row's count badge
    row = win.sidebar._rows[wa.id][1]
    check("v2 dashboard: count badge shows the agent tally",
          row.count_badge._count == 1, row.count_badge._count)
    check("v2 dashboard: one idle agent -> idle (amber) state",
          row.count_badge._state == "idle", row.count_badge._state)

    # folder change (#3) updates model + new agents' cwd
    win.manager.set_workspace_path(wa.id, str(proj_b))
    na = mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, "X", cwd=""), autostart=False)
    check("v2 folder: new agent inherits changed path", na.spec.cwd == str(proj_b))
    win.manager.set_workspace_path(wa.id, str(proj_a))

    # grid layout persists + applies (#7) through the page
    page = win._pages[wa.id]
    page.set_layout("3x2")
    check("v2 grid: page applies fixed layout with empty slots",
          len(page._empty_slots) >= 1)
    win.manager.set_layout(wa.id, "3x2")
    saved = {w["id"]: w for w in mgr.to_session_dict()["workspaces"]}
    check("v2 grid: layout persisted", saved[wa.id].get("layout") == "3x2",
          saved[wa.id].get("layout"))

    # shared board (#8): a Claude agent creates the board with a roster
    cl = mgr.add_terminal(wa.id, build_spec(AgentKind.CLAUDE, "Claude", cwd=str(proj_a)),
                          autostart=False)
    board = proj_a / ".aihive" / "board.md"
    check("v2 board: Claude agent creates shared board", board.is_file())
    check("v2 board: launch injects --add-dir + system prompt",
          "--add-dir" in cl.spec.effective_args()
          and "--append-system-prompt" in cl.spec.effective_args())
    cl.set_task("wiring auth")
    pump(30)
    check("v2 board: current task written to roster",
          "wiring auth" in board.read_text(encoding="utf-8"))
    check("v2 board: Beta workspace stays isolated",
          not (proj_b / ".aihive" / "board.md").is_file()
          or "Claude" not in (proj_b / ".aihive" / "board.md").read_text(encoding="utf-8"))

    # activity panel (#8) opens and lists the roster
    win._toggle_activity(wa.id)
    check("v2 activity: panel opens", win.activity_panel.is_open())
    check("v2 activity: roster lists agents", len(win.activity_panel._items) >= 1)
    win._toggle_activity(wa.id)
    pump(250)  # let the conceal animation finish
    check("v2 activity: panel closes", not win.activity_panel.is_open())

    # a narrow window: the panel floats over the terminals instead of raising
    # the window's minimum width (which pushed the window off a 1080p screen)
    win.resize(1250, 900)
    pump(100)
    min_before = win.minimumSizeHint().width()
    win._toggle_activity(wa.id)
    pump(300)
    check("v2 activity: narrow window floats the panel",
          win._activity_floating and win.activity_panel.width() == 320,
          (win._activity_floating, win.activity_panel.width()))
    check("v2 activity: open panel leaves the window's minimum width alone",
          win.minimumSizeHint().width() <= min_before,
          (min_before, win.minimumSizeHint().width()))
    win._toggle_activity(wa.id)
    pump(250)

    win.close()
    pump(200)
    ui_theme.CONSOLE_FONT_PX = ui_theme.DEFAULT_CONSOLE_PX
    shutil.rmtree(tmp, ignore_errors=True)


def test_v2_review_fixes():
    """Regressions for the confirmed v2 review findings."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication

    from app import ui_theme
    from app.coordination import git_changed_files
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v2fix-"))
    pa, pb = tmp / "a", tmp / "b"
    pa.mkdir(); pb.mkdir()

    # git on a non-repo returns [] fast (islice-capped, short timeout)
    check("fix git: non-repo returns empty", git_changed_files(str(pa)) == [])

    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.resize(1500, 900)
    win.show()
    pump(120)
    mgr = win.manager
    wa = mgr.workspaces[0]
    mgr.set_workspace_path(wa.id, str(pa))
    mgr.remove_terminal(wa.id, wa.agents[0].id)

    # --- set_workspace_path repoints existing Claude agent + scaffolds board ---
    cl = mgr.add_terminal(wa.id, build_spec(AgentKind.CLAUDE, "Claude", cwd=str(pa)),
                          autostart=False)
    mgr.set_workspace_path(wa.id, str(pb))
    check("fix path: existing agent cwd follows new folder", cl.spec.cwd == str(pb))
    check("fix path: coordination re-injected to new board",
          any(str(pb) in d for d in cl.spec.extra_dirs), cl.spec.extra_dirs)
    check("fix path: new board scaffolded immediately",
          (pb / ".aihive" / "board.md").is_file())

    # --- line-mode per-agent font uses a per-widget stylesheet (QSS-proof) ---
    line = mgr.add_terminal(wa.id, build_spec(AgentKind.CMD, "Line", cwd=str(pb), pty=False),
                            autostart=False)
    lcard = win._pages[wa.id].card_for(line.id)
    lcard._font_delta(+3)
    want = ui_theme.CONSOLE_FONT_PX + 3
    check("fix font: line card applies per-widget stylesheet",
          f"{want}px" in lcard.console.styleSheet(), lcard.console.styleSheet())
    check("fix font: current_font_px reflects override", lcard.current_font_px() == want)
    # a global font change must NOT wipe the per-agent override
    win._change_global_font(-1)
    check("fix font: global change preserves line override",
          f"{want}px" in lcard.console.styleSheet(), lcard.console.styleSheet())

    # --- activity button sync across workspace switch ---
    wb = mgr.create_workspace("B", str(pb))
    win._toggle_activity(wa.id)
    check("fix activity: A button lit when open on A",
          win._pages[wa.id].activity_btn.isChecked())
    mgr.set_active(wb.id)
    pump(30)
    check("fix activity: A button cleared after switching away",
          not win._pages[wa.id].activity_btn.isChecked()
          and win._pages[wb.id].activity_btn.isChecked())
    win._toggle_activity(wb.id)
    pump(30)
    check("fix activity: all buttons cleared when closed",
          not win._pages[wa.id].activity_btn.isChecked()
          and not win._pages[wb.id].activity_btn.isChecked())

    # --- layoutChanged from the model drives the page ---
    mgr.set_layout(wa.id, "2x2")
    check("fix layout: model set_layout updates the page",
          win._pages[wa.id]._layout == "2x2")

    win.close()
    pump(200)
    ui_theme.CONSOLE_FONT_PX = ui_theme.DEFAULT_CONSOLE_PX
    shutil.rmtree(tmp, ignore_errors=True)


def test_render_perm_mode_bridge():
    """Private-CSI/underline fix, clipboard, the permission-mode flag and
    the named-pipe board bridge. (Was test_v3_features;
    check names keep their "v3" prefix.)"""
    import threading

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QEventLoop, QTimer

    from app import providers
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.widgets.terminal_view import TerminalView
    from app.workspace_manager import WorkspaceManager
    from main import setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def wait_until(pred, timeout_ms=10000, step=50):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(step)
        return pred()

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-v3-"))

    # --- #7 the underline root-cause fix (private CSI stripped) ---
    tv = TerminalView(rows=4, cols=30)
    tv.feed("\x1b[>4;2m\x1b[38;2;255;193;7mGOLD")  # modifyOtherKeys + truecolor
    cell = next(tv.screen.buffer[0][c] for c in range(30)
                if tv.screen.buffer[0][c].data.strip())
    check("v3 render: modifyOtherKeys no longer forces underline",
          not cell.underscore and cell.fg == "ffc107")

    # --- #1 clipboard: selection + paste on the terminal ---
    tv2 = TerminalView(rows=4, cols=30)
    tv2.feed("hello world")
    tv2._sel_anchor, tv2._sel_end = (0, 0), (0, 4)
    check("v3 clipboard: selection text", tv2.selected_text() == "hello", tv2.selected_text())

    _, a = providers.build_invocation("claude", effort="ultracode")
    check("v3 ultracode: never a launch flag", "--effort" not in a)

    # --- permission mode (Shift+Tab modes) startup flag (Claude) ------------
    _, pm_default = providers.build_invocation("claude")
    check("perm-mode: default omits the flag (today's behavior)",
          "--permission-mode" not in pm_default, pm_default)
    _, pm_plan = providers.build_invocation("claude", permission_mode="plan")
    check("perm-mode: chosen mode becomes --permission-mode <mode>",
          pm_plan == ["--permission-mode", "plan"], pm_plan)
    _, pm_bad = providers.build_invocation("claude", permission_mode="bogus")
    check("perm-mode: an unknown mode never reaches the CLI",
          "--permission-mode" not in pm_bad, pm_bad)
    pm_spec = build_spec(AgentKind.CLAUDE, "PM", cwd=str(tmp),
                         permission_mode="acceptEdits")
    check("perm-mode: build_spec bakes it into args + persists round-trip",
          "--permission-mode" in pm_spec.effective_args()
          and "acceptEdits" in pm_spec.effective_args()
          and AgentSpec.from_dict(pm_spec.to_dict()).permission_mode
          == "acceptEdits", pm_spec.to_dict())

    # --- #3/#8 board bridge over the real named pipe: log_activity round-trips
    from app.orchestrator_bridge import OrchestratorBridge, HAS_QTNETWORK
    if HAS_QTNETWORK:
        from app import mcp_server
        mgr = WorkspaceManager()
        ws = mgr.create_workspace("W", str(tmp))
        bridge = OrchestratorBridge(mgr, active_ws=lambda: ws.id)
        check("v3 bridge: named pipe listening", bridge.start())
        os.environ["AIHIVE_PIPE"] = bridge.pipe_name
        os.environ["AIHIVE_WS"] = ws.id
        res = {}
        ev = threading.Event()

        def rpc():
            try:
                res["log"] = mcp_server._rpc(
                    "log_activity",
                    {"agent": "Echo", "message": "wired the parser"})
            except Exception as e:
                res["err"] = repr(e)
            finally:
                ev.set()

        threading.Thread(target=rpc, daemon=True).start()
        wait_until(ev.is_set, 15000)
        check("v3 bridge: MCP client + pipe RPC round-trip",
              "err" not in res and res.get("log", {}).get("logged") is True,
              res.get("err"))
        check("v3 bridge: log_activity note lands on the board",
              "wired the parser" in "\n".join(ws.board.read_log_tail()))
        bridge.stop()
        os.environ.pop("AIHIVE_WS", None)
        os.environ.pop("AIHIVE_PIPE", None)

    pump(200)
    shutil.rmtree(tmp, ignore_errors=True)


def test_themes():
    """Winamp-style skin registry: apply_theme rewrites Palette/ANSI/fonts in
    place (so all Palette.X reads follow the skin), Scriptorium Dark stays the
    shipped palette, build_qss reflects the active theme, and the choice
    persists across a save/restore."""
    from PySide6.QtWidgets import QApplication
    from app import ui_theme
    from app.session_store import SessionStore
    from main import create_main_window, setup_application

    check("themes: ids match their keys and names are unique",
          all(k == t.id for k, t in ui_theme.THEMES.items())
          and len({t.name for t in ui_theme.THEMES.values()})
          == len(ui_theme.THEMES))

    # apply_theme mutates the SAME Palette/ANSI_16 objects in place
    ansi_obj = ui_theme.ANSI_16
    ui_theme.apply_theme("scriptorium-dark")
    dark_qss = ui_theme.build_qss()
    ui_theme.apply_theme("illuminated-manuscript")
    check("themes: apply_theme mutates Palette in place (same refs update)",
          ui_theme.Palette.BG_ROOT == "#e6d7b1"
          and ui_theme.Palette.CARDHEAD_FG == "#f0d777")
    check("themes: ANSI_16 is refreshed in the SAME list object",
          ui_theme.ANSI_16 is ansi_obj and ui_theme.ANSI_16[0] == "#5a4636")
    ms_qss = ui_theme.build_qss()
    check("themes: build_qss reflects the active skin",
          "#e6d7b1" in ms_qss and "#e6d7b1" not in dark_qss
          and "#12100c" in dark_qss)
    check("themes: unknown id falls back to default",
          ui_theme.apply_theme("nope").id == ui_theme.DEFAULT_THEME_ID)

    # persistence: a saved theme restores and drives the dropdown
    app = QApplication.instance() or QApplication([])
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-theme-"))
    store = SessionStore(path=tmp / "s.json")
    store.save({"version": 3, "active": "w", "workspaces": [
        {"id": "w", "name": "W", "project_path": str(tmp), "layout": "auto",
         "terminals": []}], "ui": {"theme": "illuminated-manuscript"}})
    win = create_main_window(store)
    check("themes: saved skin restored on launch",
          win._theme_id == "illuminated-manuscript"
          and ui_theme.Palette.BG_ROOT == "#e6d7b1")
    check("themes: dropdown reflects the restored skin",
          win.top_bar.theme_select.currentData() == "illuminated-manuscript")
    # Phase B ornament: the illuminated page border shows + opens root margins
    # only for the manuscript skin; hidden and flush for the others
    # isHidden(), not isVisible(): the window isn't shown in this headless test,
    # so isVisible() is always False; isHidden() reflects the explicit setVisible
    check("themes: manuscript shows the illuminated border",
          not win._page_border.isHidden()
          and win._root_layout.contentsMargins().top() > 0)
    win._change_theme("obsidian")
    check("themes: live switch updates palette + dropdown",
          ui_theme.Palette.BG_ROOT == "#0f1117"
          and win.top_bar.theme_select.currentData() == "obsidian")
    check("themes: non-manuscript skin hides the border (flush)",
          win._page_border.isHidden()
          and win._root_layout.contentsMargins().top() == 0)
    check("themes: switch is written to the session payload",
          win._session_payload()["ui"]["theme"] == "obsidian")
    win.close()

    # the ornament widgets paint without error under both illuminated and
    # plain themes (exercises the QSvgRenderer + QPainter paths)
    from app.widgets.ornaments import DropCap, LogoRoundel, PageBorder
    for tid in ("illuminated-manuscript", "adeptus-mechanicus", "scriptorium-dark"):
        ui_theme.apply_theme(tid)
        for W in (DropCap, LogoRoundel, PageBorder):
            w = W("A") if W is DropCap else W()
            w.resize(200, 160)
            w.grab()  # runs paintEvent; raises if the paint path is broken
            w.deleteLater()

    # SHAPED-ICON GUARANTEE. The artwork is exported flat on a near-black
    # ground; shipping that unkeyed put a hard black square in the title bar,
    # the taskbar, Alt+Tab and the Start tile (all of which can be light), and
    # the same bitmap backs LogoRoundel, where it read as a cold black tile on
    # the warm panel. generate_app_icon.py keys the ground out and trims to the
    # mark, so both assets must arrive with see-through corners; a future flat
    # re-export that skipped that step would fail here rather than quietly
    # reinstating the square.
    from PySide6.QtGui import QImage
    icons = str(ROOT / "app" / "assets" / "icons")
    shaped, opaque_corner = [], []
    for asset in ("app_logo.png", "app_icon.ico"):
        img = QImage(os.path.join(icons, asset))
        if img.isNull():
            opaque_corner.append(f"{asset}: missing")
            continue
        w, h = img.width(), img.height()
        alphas = [img.pixelColor(x, y).alpha() for x, y in
                  ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
        (shaped if not any(alphas) else opaque_corner).append(
            f"{asset}: {alphas}")
        # and the mark itself has to still be there — a key that ate the
        # artwork would also leave the corners clear. A RATIO, because the
        # .ico hands QImage whichever single frame it likes (a 16px one here),
        # so an absolute sample count would only measure that choice.
        step_x, step_y = max(1, w // 40), max(1, h // 40)
        pts = [(x, y) for y in range(0, h, step_y) for x in range(0, w, step_x)]
        ink = sum(1 for x, y in pts if img.pixelColor(x, y).alpha() > 200)
        if ink < len(pts) * 0.25:
            opaque_corner.append(
                f"{asset}: only {ink}/{len(pts)} samples are opaque")
    check("themes: shipped icon assets are shaped, not opaque squares",
          len(shaped) == 2 and not opaque_corner, opaque_corner)

    # CONTRAST GUARANTEE (chrome): every skin's text tokens must clearly read
    # on their own backgrounds — the fix behind the white-on-vellum report.
    from PySide6.QtGui import QColor
    from app.widgets.terminal_view import (contrast_ratio, legible_color,
                                           LEGIBLE_TARGET)
    fails = []
    for t in ui_theme.THEMES.values():
        pairs = [  # (fg, bg, min-ratio, label)
            (t.text, t.bg_panel, 4.5, "text/panel"),
            (t.text, t.bg_card, 4.5, "text/card"),
            (t.text, t.bg_root, 4.5, "text/root"),
            (t.console_fg, t.bg_console, 4.5, "console"),
            (t.cardhead_fg, t.bg_cardhead, 4.5, "cardhead"),
            (t.appname_fg, t.bg_panel, 4.0, "appname"),
            (t.input_echo, t.bg_input, 4.0, "input-echo"),
            (t.text_dim, t.bg_panel, 3.0, "text_dim"),
            (t.cardhead_sub, t.bg_cardhead, 3.0, "cardhead_sub"),
            (t.selection_fg, t.selection, 3.0, "selection"),
        ]
        for fg, bg, mn, label in pairs:
            r = contrast_ratio(QColor(fg), QColor(bg))
            if r < mn:
                fails.append(f"{t.id}:{label}={r:.2f}<{mn}")
    check("themes: all chrome text meets contrast in every skin", not fails, fails)

    # VENDOR INK. The model chip is coloured by provider and NOT by the skin,
    # so it is the one chrome token a new theme cannot tune -- every skin has
    # to stay readable under it instead. Every running-head is dark today,
    # including the manuscript's ultramarine, which is why the inks are lifted
    # from the vendors' own #d97757 / #4285f4 (those land at 3.8:1 and 3.3:1
    # there). A future light running-head fails here rather than shipping a
    # washed-out chip.
    ink_fails = []
    for t in ui_theme.THEMES.values():
        for key, ink in ui_theme.PROVIDER_INK.items():
            r = contrast_ratio(QColor(ink), QColor(t.bg_cardhead))
            if r < 4.5:
                ink_fails.append(f"{t.id}:{key}={r:.2f}")
    check("themes: vendor model-chip inks read on every running-head",
          not ink_fails, ink_fails)

    # the checked-checkbox tick must be clearly visible on the accent fill in
    # every skin (the near-invisible-default-tick report). _check_icon_path
    # picks black/white for max contrast; assert the chosen tick clears the 3:1
    # graphical-contrast target on each accent.
    ck_fails = []
    for t in ui_theme.THEMES.values():
        path = ui_theme._check_icon_path(t.accent_gold)
        if not path or not os.path.isfile(path):
            ck_fails.append(f"{t.id}:no-icon")
            continue
        with open(path, encoding="utf-8") as f:
            stroke = f.read().split("stroke='")[1].split("'")[0]
        r = contrast_ratio(QColor(stroke), QColor(t.accent_gold))
        if r < 3.0:
            ck_fails.append(f"{t.id}:tick={r:.2f}")
    check("themes: checkbox tick contrasts with its accent fill", not ck_fails,
          ck_fails)

    # CONTRAST GUARANTEE (terminal): whatever color a child emits, the glyph is
    # forced readable on its actual background (the invisible-Claude-on-vellum
    # bug); text that is already readable is left untouched.
    vellum, ink = QColor("#fbf4df"), QColor("#0d0d0d")
    check("legible: white-on-vellum rescued to readable",
          contrast_ratio(legible_color(QColor("#ffffff"), vellum), vellum)
          >= LEGIBLE_TARGET - 0.05)
    check("legible: light-grey-on-vellum rescued to readable",
          contrast_ratio(legible_color(QColor("#c9d1d9"), vellum), vellum)
          >= LEGIBLE_TARGET - 0.05)
    check("legible: dark-on-dark rescued to readable",
          contrast_ratio(legible_color(QColor("#222222"), ink), ink)
          >= LEGIBLE_TARGET - 0.05)
    already = QColor("#c9d1d9")
    check("legible: already-readable text is left unchanged",
          legible_color(already, ink) == already)
    check("legible: hue is preserved when rescuing (blue stays blueish)",
          legible_color(QColor("#88bbff"), vellum).hue()
          in range(QColor("#88bbff").hue() - 25, QColor("#88bbff").hue() + 25))

    ui_theme.apply_theme("scriptorium-dark")  # leave the default active
    shutil.rmtree(tmp, ignore_errors=True)


def test_review_fixes():
    """Regressions for the Codex-review findings: single-instance mutex guard
    (fail closed), task-assignment state autosave, workspace-bound board
    log_activity, quoted-path command parsing, and the session save-audit
    log."""
    import json as _json
    from PySide6.QtWidgets import QApplication

    from app.orchestrator_bridge import OrchestratorBridge, _RpcError
    from app.process_worker import AgentKind, AgentSpec, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import AssignmentState
    from app.workspace_manager import WorkspaceManager
    from main import _single_instance_guard

    QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-review-"))

    # --- single-instance guard: kernel mutex, second acquisition refused ---
    # (own mutex name: the REAL app may legitimately be running during a
    # test run, and colliding with its lock is not this test's business)
    mtx = f"Local\\ai-hive-test-{os.getpid()}"
    first = _single_instance_guard(mtx)
    check("guard: first instance acquires the mutex", bool(first))
    second = _single_instance_guard(mtx)
    check("guard: second instance is refused (fail closed)", second is None)
    if sys.platform == "win32" and first:
        import ctypes
        ctypes.windll.kernel32.CloseHandle(first)  # release for the real app

    # --- task-assignment state changes mark the session dirty ---
    mgr = WorkspaceManager()
    ws_a = mgr.create_workspace("ScopeA", project_path=str(tmp))
    ws_b = mgr.create_workspace("ScopeB", project_path=str(tmp))
    a1 = mgr.add_terminal(ws_a.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    b1 = mgr.add_terminal(ws_b.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    dirty_count = {"n": 0}
    mgr.dirty.connect(lambda: dirty_count.__setitem__("n", dirty_count["n"] + 1))
    a1.set_task("write the docs")
    check("autosave: task change marks dirty", dirty_count["n"] >= 1,
          dirty_count["n"])
    before = dirty_count["n"]
    a1.set_assignment(AssignmentState.COMPLETED)
    check("autosave: assignment change marks dirty", dirty_count["n"] > before)
    before = dirty_count["n"]
    a1.set_name("Docs Writer")
    check("autosave: name change marks dirty", dirty_count["n"] > before)

    # --- manual rename (set_name) is the ONLY thing that renames an agent ---
    # isolated in its own workspace so it never disturbs the scope tests below,
    # which resolve a1/b1 by their names.
    ws_c = mgr.create_workspace("ScopeC", project_path=str(tmp))
    r1 = mgr.add_terminal(ws_c.id, build_spec(AgentKind.CMD, "Agent 1",
                                              cwd=str(tmp)), autostart=False)
    names_seen = []
    r1.name_changed.connect(lambda n: names_seen.append(n))
    before = dirty_count["n"]
    r1.set_name("Scribe")
    check("rename: set_name changes only the display name",
          r1.spec.name == "Scribe" and r1.spec.role == "cmd",
          f"name={r1.spec.name} role={r1.spec.role}")
    check("rename: set_name flags the name custom", r1.spec.custom_name)
    check("rename: set_name emits name_changed", names_seen == ["Scribe"],
          names_seen)
    check("rename: set_name marks dirty", dirty_count["n"] > before)
    # next_agent_name still numbers monotonically off the highest "Agent N"
    r2 = mgr.add_terminal(ws_c.id, build_spec(AgentKind.CMD, "Agent 2",
                                              cwd=str(tmp)), autostart=False)
    check("rename: next_agent_name is max+1, ignoring custom names",
          mgr.next_agent_name(ws_c.id) == "Agent 3",
          mgr.next_agent_name(ws_c.id))
    # persistence round-trip: custom_name survives to_dict/from_dict
    restored = AgentSpec.from_dict(r1.spec.to_dict())
    check("rename: custom_name round-trips through to_dict/from_dict",
          restored.name == "Scribe" and restored.custom_name is True,
          f"name={restored.name} custom={restored.custom_name}")
    # degraded save path keeps the flag too (a malformed agent must not lose it)
    safe = mgr._agent_dict_safe(r1)
    check("rename: custom_name present for serialize", "custom_name" in safe)
    mgr.remove_workspace(ws_c.id)

    # --- workspace-bound board log_activity via the bridge dispatch ---
    bridge = OrchestratorBridge(mgr, active_ws=lambda: ws_a.id)
    logged = bridge._dispatch(
        "log_activity", {"agent": "Docs Writer", "message": "wrote the docs"},
        ws=ws_a.id)
    check("board: agent may log_activity", logged.get("logged") is True)
    check("board: log_activity binds to the caller's workspace",
          logged.get("workspace_id") == ws_a.id)
    tail = ws_a.board.read_log_tail()
    check("board: log_activity entry lands on the board",
          any("wrote the docs" in t for t in tail), tail)
    # a note for one workspace never crosses onto another's board (distinct
    # project folders => distinct board.md files)
    ws_d = mgr.create_workspace("ScopeD", project_path=str(tmp / "d"))
    bridge._dispatch("log_activity", {"agent": "X", "message": "note for D"},
                     ws=ws_d.id)
    check("board: a note lands only on its own workspace board",
          not any("note for D" in t for t in ws_a.board.read_log_tail())
          and any("note for D" in t for t in ws_d.board.read_log_tail()))
    # the removed orchestration ops are simply unknown now
    try:
        bridge._dispatch("spawn_agent", {"task": "x"}, ws=ws_a.id)
        unknown_ok = False
    except _RpcError as e:
        unknown_ok = e.code == "bad_args"
    check("board: removed orchestration ops are unknown", unknown_ok)
    # append_activity must NOT disturb the roster block
    ws_a.board.update_roster([{"name": "A1", "role": "", "provider":
                "claude", "model": "", "status": "idle", "task": "keep me"}])
    ws_a.board.append_activity("A1", "another line")
    body = Path(ws_a.board.path).read_text(encoding="utf-8")
    check("board: roster survives an activity append",
          "keep me" in body and body.count("AIHIVE:ROSTER:BEGIN") == 1
          and "another line" in body)

    # --- per-workspace mcp config carries the binding (WS only, no role) ---
    bridge.pipe_name = "test-pipe"
    bridge._mcp_dir = str(tmp / "mcp")
    cfg_path = bridge.mcp_config_path_for(ws_a.id)
    cfg = _json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    env = cfg["mcpServers"]["aihive"]["env"]
    check("board: mcp config binds AIHIVE_WS to the workspace",
          env.get("AIHIVE_WS") == ws_a.id and env.get("AIHIVE_PIPE") == "test-pipe")
    check("board: mcp config no longer carries a role",
          "AIHIVE_ROLE" not in env)

    # --- session save-audit log (forensics for any future clobber) ---
    sp = tmp / "audit" / "s.json"
    store = SessionStore(path=sp)
    store.save({"version": 3, "workspaces": [{"terminals": [1, 2]}]})
    log = sp.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: save writes a forensic line with pid",
          "SAVE ws=1 terminals=[2]" in log and f"pid={os.getpid()}" in log, log)
    sp.write_text("{corrupt", encoding="utf-8")
    store.load()
    log = sp.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: load failure is recorded before .bak rename",
          "LOAD-FAIL" in log, log)

    # a save that fails must be VISIBLE in the log (an instance once went
    # silent — no saves, no errors — while the user kept working), and every
    # successful save keeps a rolling previous generation for recovery
    sp2 = tmp / "audit" / "roll.json"
    store2 = SessionStore(path=sp2)
    store2.save({"version": 3, "gen": 1, "workspaces": []})
    store2.save({"version": 3, "gen": 2, "workspaces": []})
    prev = _json.loads(sp2.with_suffix(".json.1").read_text(encoding="utf-8"))
    cur = _json.loads(sp2.read_text(encoding="utf-8"))
    check("audit: rolling previous-generation session backup",
          prev.get("gen") == 1 and cur.get("gen") == 2, (prev, cur))
    ok = store2.save({"bad": object()})  # non-serializable -> save fails
    log2 = sp2.with_suffix(".log").read_text(encoding="utf-8")
    check("audit: failed save is recorded, not silent",
          ok is False and "SAVE-FAIL" in log2, log2[-200:])
    check("audit: failed save never corrupts the session file",
          _json.loads(sp2.read_text(encoding="utf-8")).get("gen") == 2)

    # --- mojibake armor: JSON can carry lone surrogates (MCP tool calls,
    # session files); strict-UTF-8 sinks refuse them. One such task string
    # crashed the app at startup while rewriting the board roster.
    from app import coordination
    bad = "reconcile the docs \udc81 with reality"
    a1.set_task(bad)
    check("mojibake: set_task strips lone surrogates",
          "\udc81" not in a1.current_task and a1.current_task.startswith("reconcile"),
          ascii(a1.current_task))
    board = coordination.WorkspaceBoard(str(tmp / "boardws"))
    ok = board.update_roster([{"name": "X", "role": "", "provider": "claude",
                               "model": "", "status": "idle", "task": bad}])
    check("mojibake: board roster write survives surrogates", ok)
    body = Path(board.path).read_text(encoding="utf-8")  # strict decode
    check("mojibake: board file remains valid strict UTF-8", "| X |" in body)

    for a in mgr.all_agents():
        a.dispose()
    shutil.rmtree(tmp, ignore_errors=True)


def test_review_hardening_fixes():
    """Regressions for the six defects a whole-app code review surfaced:
      #1 the SessionStart hook is re-armed on restore even when the orchestrator
         bridge is disabled (the hook is bridge-independent);
      #2 terminal_view.feed caps _esc_carry so an unterminated OSC can't swallow
         the stream / grow memory without bound;
      #3 the orchestrator pipe drops a client that streams bytes with no newline;
      #4 the 350 ms task-submit Enter is generation-guarded so a restart in the
         window can't fire a stray CR into a fresh TUI;
      #5 a scrolled-back terminal view stays anchored after history saturates.
    (#6 tested spawn_worker, which is gone with the orchestrator.)"""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import session_hook
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import TerminalAgent
    from app.workspace_manager import WorkspaceManager
    from app.widgets.terminal_view import (TerminalView, HISTORY_LINES,
                                           _CountingDeque, _MAX_ESC_CARRY)
    from main import create_main_window, setup_application

    app = QApplication.instance() or QApplication([])
    setup_application(app)

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def stub_worker():
        writes = []
        w = type("W", (), {
            "write": lambda s, d: (writes.append(d), True)[1],
            "is_running": lambda s: True, "start": lambda s: None,
            "restart": lambda s: None, "dispose": lambda s: None,
            "send_line": lambda s, t: True})()
        return w, writes

    # -- #1: hook re-armed on restore even with the bridge disabled -----------
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-harden-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    try:
        ws = win.manager.workspaces[0]
        spec = build_spec(AgentKind.CLAUDE, "Restored", cwd=str(tmp), pty=True)
        agent = win.manager.add_terminal(ws.id, spec, autostart=False)
        win.bridge.enabled = False          # simulate no Qt network / listen fail
        agent.spec.settings_path = ""        # simulate an un-armed restored agent
        agent.spec.env.pop(session_hook.AGENT_ID_ENV, None)
        win._rearm_agent_configs()
        check("harden: hook re-armed on restore even when the bridge is disabled",
              agent.spec.settings_path == win._hook_settings_path
              and agent.spec.env.get(session_hook.AGENT_ID_ENV) == agent.id)
    finally:
        win.close()
        shutil.rmtree(tmp, ignore_errors=True)

    # -- #2: _esc_carry is bounded -------------------------------------------
    v = TerminalView()
    v.feed("\x1b[")  # a short, genuine partial escape is carried
    check("harden: a short partial escape is still carried",
          v._esc_carry == "\x1b[")
    v.feed("m")      # completes it
    v._esc_carry = ""
    v.feed("\x1b]0;" + "A" * (_MAX_ESC_CARRY + 100))  # unterminated OSC
    check("harden: an over-long unterminated OSC is not carried unbounded",
          len(v._esc_carry) <= _MAX_ESC_CARRY)
    v.feed("B" * 8000)
    check("harden: esc_carry stays bounded across chunks",
          len(v._esc_carry) <= _MAX_ESC_CARRY)

    # -- #3: oversized no-newline pipe request is dropped ---------------------
    from app.orchestrator_bridge import OrchestratorBridge, _MAX_REQUEST_BYTES
    br_mgr = WorkspaceManager()
    bridge = OrchestratorBridge(br_mgr, active_ws=lambda: "", parent=None)

    class _FakeBA:
        def __init__(self, d): self._d = d
        def data(self): return self._d

    class _FakeSock:
        def __init__(self, chunk): self._chunk = chunk; self.aborted = False
        def readAll(self):
            d, self._chunk = self._chunk, b""
            return _FakeBA(d)
        def abort(self): self.aborted = True

    sock = _FakeSock(b"x" * (_MAX_REQUEST_BYTES + 16))  # no newline, runaway
    bridge._buffers[sock] = bytearray()
    bridge._on_ready(sock)
    check("harden: an oversized newline-less pipe request is dropped",
          sock.aborted and sock not in bridge._buffers)

    # -- #4: task-submit Enter is generation-guarded --------------------------
    spec4 = build_spec(AgentKind.CLAUDE, "Submit", cwd=str(Path(tempfile.gettempdir())),
                       pty=True)
    a4 = TerminalAgent(spec4)
    w, writes = stub_worker()
    a4.worker = w
    a4._prompt_ready = True
    a4._write_task_to_pty("hello")   # schedules the delayed Enter
    a4._on_pty_output("", "hello")   # Claude draws it in the box
    pump(500)
    check("harden: the task-submit Enter fires in the normal case",
          "\r" in writes)
    writes.clear()
    a4._prompt_ready = True
    a4._write_task_to_pty("world")   # schedule again...
    a4._on_pty_output("", "world")
    a4.restart()                      # ...then restart inside the 350 ms window
    pump(500)
    check("harden: a restart in the submit window suppresses the stray Enter",
          "\r" not in writes)

    # -- #5: scrolled-back view stays anchored past the history cap -----------
    v5 = TerminalView()
    for i in range(HISTORY_LINES + 200):
        v5.feed(f"line{i}\r\n")
    top = v5.screen.history.top
    check("harden: terminal history uses the counting deque",
          isinstance(top, _CountingDeque))
    check("harden: history saturated at the cap (the drift-prone case)",
          len(top) == HISTORY_LINES)
    v5._scroll_offset = 40           # user scrolled back
    pushed_before = top.pushed
    v5.feed("A\r\nB\r\nC\r\n")       # more lines scroll into a full history
    grown = v5.screen.history.top.pushed - pushed_before
    check("harden: scrolled-back offset tracks pushes even after saturation",
          grown >= 1 and v5._scroll_offset == 40 + grown,
          (grown, v5._scroll_offset))


def test_task_submit_waits_for_echo():
    """A task delivered at launch in a fresh git folder was typed but never
    submitted (agent lanes, Phase 2 finding). Claude stalled ~570 ms after its
    first frame, so the text and the Enter sent 350 ms later reached it as one
    chunk, and a CR inside a chunk is part of a paste. The Enter now waits
    until Claude has drawn the text, never sooner than TASK_SUBMIT_MS, with
    TASK_ECHO_TIMEOUT_MS as the fallback. Reproduced live 6/6 before the fix,
    0/6 after."""
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from app import terminal_agent as ta
    from app.process_worker import AgentKind, build_spec
    from app.terminal_agent import TerminalAgent

    QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    def agent(kind=AgentKind.CLAUDE):
        writes = []
        a = TerminalAgent(build_spec(kind, "Echo", cwd=SCRATCH_CWD, pty=True))
        a.worker = type("W", (), {
            "write": lambda s, d: (writes.append(d), True)[1],
            "is_running": lambda s: True, "start": lambda s: None,
            "restart": lambda s: None, "dispose": lambda s: None,
            "send_line": lambda s, t: True})()
        a._prompt_ready = True
        return a, writes

    task = "Use the Write tool to create a file named out.txt"
    # what the classic renderer actually sent in the live run: cursor moves
    # stand in for the spaces, so only a despaced match finds it
    drawn = ("\x1b[?25l\x1b[2D\x1b[3B\r\x1b[2C\x1b[3AUse the Write tool to "
             "create\x1b[32Ga\x1b[34Gfile\x1b[39Gnamed\x1b[45Gout.txt")

    floor = ta.TASK_SUBMIT_MS + 150   # past the floor, well short of the fallback

    a, writes = agent()
    a.nudge(task)
    check("submit-echo: the text is typed at once", writes == [task], writes)
    pump(floor)
    check("submit-echo: no Enter while Claude has not drawn the text (the "
          "stalled-event-loop case)", "\r" not in writes, writes)
    a._on_pty_output("", drawn)
    check("submit-echo: the Enter follows the echo at once past the floor",
          writes[-1:] == ["\r"], writes)
    check("submit-echo: ...and opens a turn like any submit", a._turn_open)
    pump(ta.TASK_SUBMIT_MS)
    check("submit-echo: exactly one Enter", writes.count("\r") == 1, writes)

    a, writes = agent()
    a.nudge(task)
    a._on_pty_output("", drawn)
    pump(100)
    check("submit-echo: an early echo still waits for the floor",
          "\r" not in writes, writes)
    pump(floor)
    check("submit-echo: ...then the Enter goes", writes.count("\r") == 1,
          writes)

    a, writes = agent()
    a._on_pty_output("", "Use the Write tool to create a file named out.txt")
    a.nudge(task)
    pump(floor)
    check("submit-echo: the same words on screen BEFORE the typing don't "
          "count", "\r" not in writes, writes)
    a._on_pty_output("", "\x1b[2K")
    check("submit-echo: ...nor does unrelated output after it",
          "\r" not in writes, writes)

    a, writes = agent()
    a.nudge("line one of a plan\nline two\nline three")
    check("submit-echo: a multi-line task is one bracketed paste",
          writes and writes[0].startswith(ta.PASTE_ON), writes)
    pump(floor)
    a._on_pty_output("", "\x1b[3A\x1b[2C[Pasted\x1b[11Gtext\x1b[16G#1 +2 lines]")
    check("submit-echo: Claude's collapsed-paste placeholder counts as the "
          "echo", writes[-1:] == ["\r"], writes)

    saved = ta.TASK_ECHO_TIMEOUT_MS
    ta.TASK_ECHO_TIMEOUT_MS = 600
    try:
        a, writes = agent()
        a.nudge(task)
        pump(800)
        check("submit-echo: with no echo at all the Enter still goes after "
              "the fallback (the old behavior, never worse)",
              writes.count("\r") == 1, writes)
        a._on_pty_output("", drawn)
        pump(floor)
        check("submit-echo: ...and a late echo adds no second Enter",
              writes.count("\r") == 1, writes)

        a, writes = agent()
        a.nudge(task)
        a.restart()
        a._on_pty_output("", drawn)
        pump(800)
        check("submit-echo: a restart drops the pending Enter, echo or "
              "fallback", "\r" not in writes, writes)
    finally:
        ta.TASK_ECHO_TIMEOUT_MS = saved

    a, writes = agent()
    a.nudge("Continue")
    a.nudge("Continue")
    a._on_pty_output("", "\x1b[3A\x1b[2CContinueContinue")
    pump(floor)
    check("submit-echo: two deliveries in one window each get their Enter",
          writes.count("\r") == 2, writes)

    a, writes = agent(AgentKind.POWERSHELL)
    a.nudge("Get-ChildItem")
    pump(floor)
    check("submit-echo: other TUIs keep the plain fixed beat",
          writes[-1:] == ["\r"], writes)


def test_scheduled_send():
    """A message the user writes now and has typed in LATER.

    Ctrl+Shift+Enter in a terminal is "Enter, but on a countdown" - the gesture
    that makes chaining agents possible while away from the machine. The rules
    that matter are the ones about NOT sending: a scheduled message is a nudge
    and never an assignment, and one that came due while the app was closed is
    surfaced as missed rather than fired hours late into a conversation that has
    moved on."""
    import time as _time
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtWidgets import QApplication
    from app import scheduled_send as ss
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.terminal_agent import AssignmentState, TerminalAgent
    from app.widgets.main_window import SCHEDULE_GIVE_UP_S
    from app.workspace_manager import WorkspaceManager
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    now = _time.time()

    # --- parsing: what a person types into a "send in" box ------------------
    delays = {"45": 2700, "45m": 2700, "30 min": 1800, "1h": 3600,
              "1h30": 5400, "1h30m": 5400, "1.5h": 5400, "90s": 90,
              "2:15": 8100, "1h 30m 10s": 5410}
    bad = ["", "   ", "abc", "0", "45x", "-5", "200h"]  # 200h > the week cap
    check("schedule: delays parse (bare number = minutes, 1h30, 90s, 2:15)",
          all(ss.parse_delay(k) == v for k, v in delays.items()),
          {k: ss.parse_delay(k) for k, v in delays.items()
           if ss.parse_delay(k) != v})
    check("schedule: junk and non-positive delays are rejected",
          all(ss.parse_delay(b) is None for b in bad),
          [b for b in bad if ss.parse_delay(b) is not None])
    # a bare clock has no date, so a time already past today means tomorrow --
    # the rollover that matters when scheduling late at night for the morning
    anchor = _time.mktime((2026, 8, 7, 14, 0, 0, 0, 0, -1))
    later = ss.parse_clock("15:30", anchor)
    tomorrow = ss.parse_clock("03:30", anchor)
    check("schedule: a clock still ahead today resolves to today",
          later is not None and 0 < later - anchor < 86400
          and _time.localtime(later).tm_mday == 7)
    check("schedule: a clock already past resolves to TOMORROW",
          tomorrow is not None and _time.localtime(tomorrow).tm_mday == 8)
    check("schedule: pm/am clocks parse",
          ss.parse_clock("3pm", anchor) == ss.parse_clock("15:00", anchor))
    check("schedule: an impossible clock is rejected",
          ss.parse_clock("25:00", anchor) is None)
    check("schedule: countdowns format h:mm:ss / m:ss and clamp at zero",
          (ss.format_countdown(3862), ss.format_countdown(724),
           ss.format_countdown(9), ss.format_countdown(-5))
          == ("1:04:22", "12:04", "0:09", "0:00"))

    # a malformed persisted row must never cost an agent its other messages
    check("schedule: an unusable persisted row decodes to None, not a crash",
          ss.ScheduledMessage.from_dict({"text": "", "due_ts": 1}) is None
          and ss.ScheduledMessage.from_dict({"text": "x"}) is None
          and ss.ScheduledMessage.from_dict({"text": "x", "due_ts": "no"})
          is None)

    # --- the queue on the agent --------------------------------------------
    writes: dict = {}

    def mk(name="Coder", pty=True):
        spec = build_spec(AgentKind.CLAUDE, name, cwd=SCRATCH_CWD, pty=pty)
        a = TerminalAgent(spec)
        a.worker = type("W", (), {
            "is_running": lambda s: True,
            "write": lambda s, d: (writes.setdefault(id(s), []).append(d),
                                   True)[1],
            "start": lambda s: None, "dispose": lambda s: None})()
        a._prompt_ready = True
        return a

    def sent(agent):
        return "".join(writes.get(id(agent.worker), []))

    a = mk()
    edges = []
    a.scheduled_changed.connect(lambda: edges.append(1))
    msg = a.schedule_message("run the smoke suite", now + 600)
    check("schedule: a queued message is held and announced once",
          msg is not None and len(a.pending_scheduled()) == 1 and edges == [1])
    check("schedule: an empty message is refused",
          a.schedule_message("   ", now + 60) is None)
    check("schedule: the soonest message is the one shown",
          a.schedule_message("later", now + 9000) is not None
          and a.next_scheduled().text == "run the smoke suite")
    check("schedule: nothing is due before its time", a.due_scheduled(now) == [])
    check("schedule: it is due at its time",
          [m.text for m in a.due_scheduled(now + 601)]
          == ["run the smoke suite"])
    for i in range(ss.MAX_PER_AGENT):
        a.schedule_message(f"filler {i}", now + 4000 + i)
    check("schedule: an agent caps how many it will hold",
          len(a.pending_scheduled()) == ss.MAX_PER_AGENT)
    a._scheduled = [m for m in a._scheduled if not m.text.startswith("filler")]

    # --- editing an already-queued message in place, instead of cancel+ ----
    # recreate (which would silently lose its spot in the queue)
    edges.clear()
    ok = a.reschedule(msg.id, "run the smoke suite twice", now + 1200)
    check("schedule: reschedule edits text and due_ts in place, same id",
          ok and msg.text == "run the smoke suite twice"
          and msg.due_ts == now + 1200 and edges == [1])
    check("schedule: an unknown id is refused",
          not a.reschedule("no-such-id", "x", now + 60))
    check("schedule: empty text is refused, existing message unchanged",
          not a.reschedule(msg.id, "   ", now + 1)
          and msg.text == "run the smoke suite twice")
    stale = a.schedule_message("stale", now - 10)
    a.mark_scheduled_missed(stale.id)
    check("schedule: a missed message given a future time revives to PENDING",
          a.reschedule(stale.id, "stale", now + 300)
          and stale.state == ss.PENDING)
    a._scheduled = [m for m in a._scheduled if m.id != stale.id]

    # --- delivery is a NUDGE, never an assignment --------------------------
    # The user pressed a deferred Enter; they did not assign anything, so the
    # persisted current_task, the assignment and the role may not move.
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-sched-"))
    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show()
    mgr = win.manager
    ws = mgr.workspaces[0]

    d = mk("Deliver")
    d.set_task("the original task")
    d.set_assignment(AssignmentState.COMPLETED)
    d.spec.role = "Reviewer"
    before = (d.current_task, d.assignment, d.spec.role)
    due = d.schedule_message("please continue", now - 1)
    ws.agents.append(d)
    win._tick_schedules()
    check("schedule: a due message is typed into the agent",
          "please continue" in sent(d))
    check("schedule: delivery leaves task/assignment/role untouched "
          "(it is a nudge)",
          (d.current_task, d.assignment, d.spec.role) == before,
          (d.current_task, d.assignment, d.spec.role))
    check("schedule: a sent message is dropped from the queue",
          d.scheduled_messages() == [])
    check("schedule: delivery does not stamp the user-input clock "
          "(the work it starts still pulses the sidebar)",
          d._last_input_ts == 0.0)

    # --- a refusal is retried, then given up on as MISSED -------------------
    r = mk("NotReady")
    r._prompt_ready = False
    late = r.schedule_message("go", now - 5)
    ws.agents.append(r)
    win._tick_schedules()
    check("schedule: an agent whose prompt is not ready is not typed into",
          sent(r) == "")
    check("schedule: ...and the message stays queued for the next tick",
          late.is_pending() and late.attempts == 1)
    late.due_ts = now - SCHEDULE_GIVE_UP_S - 1
    win._tick_schedules()
    check("schedule: past the give-up window it becomes MISSED, not sent",
          late.state == ss.MISSED and sent(r) == "")
    check("schedule: a missed message is KEPT so the user can see it",
          [m.state for m in r.scheduled_messages()] == [ss.MISSED])

    # an agent parked on the plan limit must not be typed into: the text would
    # land in the limit's options menu, not the prompt underneath it
    b = mk("Blocked")
    b.mark_limit_blocked(now + 3600)
    b.schedule_message("go", now - 1)
    ws.agents.append(b)
    win._tick_schedules()
    check("schedule: an agent parked on the plan limit is not typed into",
          sent(b) == "" and b.pending_scheduled())

    # --- the countdown tick must never touch the session file ---------------
    saves = []
    mgr.dirty.connect(lambda: saves.append(1))
    t = mk("Ticker")
    ws.agents.append(t)
    mgr._wire_agent(ws, t)
    t.schedule_message("soon", now + 3600)
    queued_saves = len(saves)
    check("schedule: queueing a message DOES mark the session dirty "
          "(the queue is persisted)", queued_saves >= 1)
    for _ in range(5):
        win._tick_schedules()
    check("schedule: the per-second tick marks the session dirty ZERO times",
          len(saves) == queued_saves, len(saves) - queued_saves)
    t.cancel_scheduled(t.next_scheduled().id)
    check("schedule: cancelling drops it and marks dirty",
          not t.scheduled_messages() and len(saves) > queued_saves)

    # the tick only RUNS while something is queued, so a hive with nothing
    # scheduled pays nothing for the feature
    for agent in (d, r, b, t):
        agent._scheduled.clear()
    win._sync_schedule_timer()
    check("schedule: the tick timer stops when nothing is queued",
          not win._schedule_timer.isActive())
    t.schedule_message("wake up", now + 60)
    win._sync_schedule_timer()
    check("schedule: the tick timer runs while something is queued",
          win._schedule_timer.isActive())
    # QTimer.start() RESTARTS a running timer, and this is called from
    # workspaceStatsChanged (which fires every couple of seconds per busy
    # agent) -- an unconditional start would reset the countdown forever
    # let the countdown run down a little first: a restart would put it back
    # at the full 50 s, which a check straight after setInterval can't see
    win._schedule_timer.setInterval(50000)
    win._schedule_timer.start()
    from PySide6.QtCore import QEventLoop as _Loop, QTimer as _T
    _l = _Loop(); _T.singleShot(300, _l.quit); _l.exec()
    win._sync_schedule_timer()
    left = win._schedule_timer.remainingTime()
    check("schedule: re-syncing an already-running tick does not restart it",
          0 <= left <= 49800, left)
    win._schedule_timer.setInterval(1000)

    check("schedule: workspace stats count agents holding a message",
          mgr.workspace_stats(ws.id)["scheduled"] == 1)

    # ...and the whole thing runs on its OWN timer. Every check above drives
    # _tick_schedules by hand, which proves the logic but not the feature: this
    # one queues a message, touches nothing, and waits for it to arrive.
    from PySide6.QtCore import QEventLoop, QTimer

    def pump(ms):
        loop = QEventLoop(); QTimer.singleShot(ms, loop.quit); loop.exec()

    live = mk("Live")
    ws.agents.append(live)
    live.schedule_message("wake up and work", _time.time() + 0.2)
    win._sync_schedule_timer()
    pump(1600)
    check("schedule: the countdown fires on its own timer and delivers",
          "wake up and work" in sent(live) and not live.scheduled_messages(),
          sent(live))

    # --- persistence, and the rule about coming back late -------------------
    m2 = WorkspaceManager()
    ahead, behind = now + 7200, now - 7200
    keeper = mk("Keeper")
    keeper.schedule_message("still ahead", ahead)
    keeper.schedule_message("long overdue", behind)
    ws.agents.append(keeper)
    data = mgr.to_session_dict()
    rows = [t_ for w in data["workspaces"] for t_ in w["terminals"]
            if t_.get("name") == "Keeper"]
    check("schedule: pending messages are persisted with the agent",
          len(rows) == 1 and len(rows[0].get("scheduled", [])) == 2,
          rows)
    m2.load_session_dict(data)
    back = next((x for x in m2.all_agents() if x.spec.name == "Keeper"), None)
    states = {m.text: m.state for m in (back.scheduled_messages() if back else [])}
    check("schedule: a message still ahead comes back PENDING",
          states.get("still ahead") == ss.PENDING, states)
    # THE RULE: a 3am message the app was closed for must NOT fire at 10am into
    # a conversation that has moved on. It comes back visible, not delivered.
    check("schedule: a message that came due while the app was closed comes "
          "back MISSED, never sent", states.get("long overdue") == ss.MISSED,
          states)
    check("schedule: a session with no queue restores cleanly",
          m2.load_session_dict({"workspaces": [{"id": "w", "name": "W",
                                                "project_path": SCRATCH_CWD,
                                                "terminals": []}]}) is None)
    # the degraded save path keeps the queue too: it exists so a malformed
    # agent loses as little as possible, and a dropped hand-off is a real loss
    broken = mk("Broken")
    broken.schedule_message("survive the degrade", ahead)
    broken.spec.kind = "not-an-enum"       # AgentSpec.to_dict raises on .value
    degraded = mgr._agent_dict_safe(broken)
    check("schedule: the degraded save record still carries the queue",
          len(degraded.get("scheduled", [])) == 1, degraded)

    # --- the gesture --------------------------------------------------------
    from app.widgets.terminal_view import TerminalView
    view = TerminalView(rows=24, cols=80)
    seen, keys = [], []
    view.scheduleRequested.connect(seen.append)
    view.keyInput.connect(keys.append)

    def press(key, ctrl=False, shift=False):
        mods = Qt.KeyboardModifier.NoModifier
        if ctrl:
            mods |= Qt.KeyboardModifier.ControlModifier
        if shift:
            mods |= Qt.KeyboardModifier.ShiftModifier
        view.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key, mods, "\r"))

    view.feed("> run the tests")
    press(Qt.Key.Key_Return, ctrl=True, shift=True)
    check("schedule: Ctrl+Shift+Enter asks for a countdown, carrying what is "
          "typed", seen == ["run the tests"], seen)
    check("schedule: ...and sends NOTHING to the child (no submit, no clear)",
          keys == [], keys)
    # Ctrl+Enter is NOT available for this: it inserts a newline, which is how
    # multi-line input works in Claude Code
    press(Qt.Key.Key_Return, ctrl=True)
    check("schedule: plain Ctrl+Enter still inserts a newline",
          keys == ["\n"] and len(seen) == 1, (keys, seen))
    view.deleteLater()

    # a genuine wrapped/multi-line message (no prompt glyph on continuation
    # rows) must still capture in full
    view2 = TerminalView(rows=24, cols=80)
    seen2 = []
    view2.scheduleRequested.connect(seen2.append)
    view2.feed("> line one\r\nline two")
    view2.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: a real wrapped continuation line is captured in full",
          seen2 == ["line one\nline two"], seen2)
    view2.deleteLater()

    # Claude Code paints its footer hint directly under the box with NO
    # blank line in between, then repositions the caret back onto the input
    # row (a full-screen TUI redraw, not a plain linefeed) -- that hint row
    # (and anything under it) must never be swept into the captured message
    view3 = TerminalView(rows=24, cols=80)
    seen3 = []
    view3.scheduleRequested.connect(seen3.append)
    view3.feed("> send this only" "\x1b[2;1H? for shortcuts" "\x1b[1;17H")
    view3.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: the footer hint under the box is not swept into the "
          "captured message", seen3 == ["send this only"], seen3)
    view3.deleteLater()

    # Claude Code also paints a plain divider/box-border row between the
    # input and its footer hint, again with no blank line -- that must not
    # be swept in either (it showed up literally as a line of dashes in a
    # scheduled message's prefill)
    view4 = TerminalView(rows=24, cols=80)
    seen4 = []
    view4.scheduleRequested.connect(seen4.append)
    view4.feed("> send this only" + "\x1b[2;1H" + ("─" * 40)
               + "\x1b[3;1H? for shortcuts" + "\x1b[1;17H")
    view4.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return,
                                  Qt.KeyboardModifier.ControlModifier
                                  | Qt.KeyboardModifier.ShiftModifier, "\r"))
    check("schedule: a divider row under the box is not swept into the "
          "captured message", seen4 == ["send this only"], seen4)
    view4.deleteLater()

    # --- the composer -------------------------------------------------------
    from PySide6.QtWidgets import QDialog, QDialogButtonBox
    from app.widgets.main_window import ScheduleMessageDialog

    comp = mk("Composer")
    dlg = ScheduleMessageDialog(comp, parent=win, prefill="deploy the thing")
    ok_btn = dlg.buttons.button(QDialogButtonBox.StandardButton.Ok)
    check("schedule: the composer opens prefilled with what was typed",
          dlg.text_edit.toPlainText() == "deploy the thing")
    check("schedule: it opens on a usable default delay",
          ok_btn.isEnabled() and dlg.result_message() is not None)
    # a preset must be written in the DELAY vocabulary, never the countdown
    # one: "5:00" reads back as five HOURS, not five minutes
    preset_bad = []
    for label, seconds in ScheduleMessageDialog.PRESETS:
        dlg._set_preset(seconds)
        got = ss.parse_delay(dlg.delay_edit.text())
        if got != seconds:
            preset_bad.append(f"{label}: {dlg.delay_edit.text()!r}={got}")
    check("schedule: every preset button means what its label says",
          not preset_bad, preset_bad)
    dlg.delay_edit.setText("90m")
    dlg._revalidate()
    text, when = dlg.result_message()
    check("schedule: a custom delay resolves to a fire time",
          text == "deploy the thing" and 5300 < when - _time.time() < 5500)
    # The caption has to name BOTH halves: the wall clock the message goes out
    # at, and how long that is from now. Assert the MEANING, never one exact
    # string - _revalidate reads the clock TWICE (once inside _resolve_due to
    # build the due time, once to subtract for the countdown), so whether a
    # tick lands between those two reads decides "1:30:00" versus "1:29:59".
    # Both are correct captions; which one appears is decided by the machine's
    # timer granularity (15.6ms on Windows, where two adjacent time.time()
    # calls routinely return the identical float), so pinning either one is a
    # coin flip. format_countdown's own formatting is pinned deterministically
    # further up; what is left to test here is that _revalidate wires the label
    # to the right VALUES.
    import re as _re
    when_text = dlg.when_label.text()
    left = _re.search(r", in (\d+):(\d{2}):(\d{2})\.", when_text)
    left_s = (int(left[1]) * 3600 + int(left[2]) * 60 + int(left[3])
              if left else -1)
    check("schedule: the composer says exactly when it will fire",
          ss.format_clock(when) in when_text and 5390 < left_s <= 5400,
          when_text)
    # the two time fields are alternatives: there must never be a hidden second
    # answer deciding the fire time
    dlg.clock_edit.setText("03:30")
    dlg._on_time_edited(False)
    check("schedule: typing a clock clears the delay field (one answer only)",
          dlg.delay_edit.text() == ""
          and dlg.result_message()[1] == ss.parse_clock("03:30"))
    dlg.text_edit.setPlainText("   ")
    check("schedule: an empty message cannot be scheduled",
          not ok_btn.isEnabled() and dlg.result_message() is None)
    dlg.text_edit.setPlainText("ok")
    dlg.clock_edit.setText("nonsense")
    dlg._on_time_edited(False)
    check("schedule: an unparseable time cannot be scheduled",
          not ok_btn.isEnabled() and dlg.result_message() is None)
    comp.schedule_message("already queued", now + 60)
    dlg.refresh_pending()
    check("schedule: the composer lists what is already queued",
          dlg.pending_box.count() > 0)

    # --- editing an existing entry in place, via its row's ✏ button --------
    target = comp.next_scheduled()
    dlg._start_edit(target)
    check("schedule: edit loads the message's text into the form",
          dlg.text_edit.toPlainText() == "already queued"
          and dlg.editing_id == target.id)
    check("schedule: the OK button reads Save while editing",
          ok_btn.text() == "Save")
    dlg._cancel(target)
    check("schedule: cancelling the row you're editing exits edit mode",
          dlg.editing_id is None and ok_btn.text() == "Schedule")
    dlg.deleteLater()

    # editing end-to-end through the popup updates the SAME entry in place --
    # not a second one alongside it
    editable = mk("Editable")
    ws.agents.append(editable)
    original = editable.schedule_message("first draft", now + 500)
    real_exec2 = ScheduleMessageDialog.exec
    try:
        def fake_edit_exec(self):
            self._start_edit(self.agent.next_scheduled())
            self.text_edit.setPlainText("revised draft")
            self.delay_edit.setText("15m")
            self._revalidate()
            return QDialog.DialogCode.Accepted
        ScheduleMessageDialog.exec = fake_edit_exec
        win._on_schedule_message(editable.id)
    finally:
        ScheduleMessageDialog.exec = real_exec2
    check("schedule: editing through the popup rewrites the entry in place",
          [m.text for m in editable.scheduled_messages()] == ["revised draft"]
          and editable.next_scheduled().id == original.id,
          [m.text for m in editable.scheduled_messages()])

    # confirming from the TERMINAL gesture also clears the child's input box:
    # the text now lives in AI Hive, so a copy left in the prompt would be
    # submitted a second time the moment the user pressed Enter
    gest = mk("Gesture")
    ws.agents.append(gest)
    real_exec = ScheduleMessageDialog.exec
    try:
        ScheduleMessageDialog.exec = lambda self: (
            self.text_edit.setPlainText("scheduled from the terminal"),
            self.delay_edit.setText("10m"), self._revalidate(),
            QDialog.DialogCode.Accepted)[-1]
        win._on_schedule_message(gest.id, "scheduled from the terminal")
    finally:
        ScheduleMessageDialog.exec = real_exec
    check("schedule: confirming queues the message",
          [m.text for m in gest.pending_scheduled()]
          == ["scheduled from the terminal"])
    check("schedule: ...and clears the child's input box (double-Escape), so "
          "the text is never submitted twice", sent(gest) == "\x1b\x1b")

    # --- the card chip ------------------------------------------------------
    page = win._pages[ws.id]
    chip_agent = mgr.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "Chip", cwd=SCRATCH_CWD),
        autostart=False)
    card = page.card_for(chip_agent.id)
    check("schedule: a card with nothing queued shows no countdown chip",
          card is not None and not card.sched_mark.isVisible())
    # `now` was captured far above: under load, seconds pass before this line
    # and the countdown reads 11:5x. Take the time here.
    chip_agent.schedule_message("later", time.time() + 724)
    check("schedule: the chip appears with the countdown to the soonest one",
          card.sched_mark.isVisible() and "12:0" in card.sched_mark.text(),
          card.sched_mark.text())
    chip_agent.mark_scheduled_missed(chip_agent.next_scheduled().id)
    check("schedule: a missed message flips the chip to its warning state",
          card.sched_mark.property("missed") is True
          and "missed" in card.sched_mark.text())
    chip_agent.cancel_scheduled(chip_agent.scheduled_messages()[0].id)
    check("schedule: cancelling the last message hides the chip again",
          not card.sched_mark.isVisible())

    # --- the sidebar's own clock, next to the agent in its inline row -------
    from app.widgets.sidebar import AgentRow

    sb_agent = mgr.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "SidebarSched", cwd=SCRATCH_CWD),
        autostart=False)
    row = AgentRow(ws.id, sb_agent)
    check("schedule: the sidebar row's clock is hidden with nothing queued",
          row.sched_mark.isHidden())
    sb_agent.schedule_message("ping later", now + 300)
    row.refresh(sb_agent)
    check("schedule: it shows once something is queued",
          not row.sched_mark.isHidden())
    sched_hits, act_hits = [], []
    row.schedRequested.connect(lambda w, a: sched_hits.append((w, a)))
    row.activated.connect(lambda w, a: act_hits.append((w, a)))
    row.sched_mark.click()
    check("schedule: clicking it emits schedRequested(ws_id, agent_id)",
          sched_hits == [(ws.id, sb_agent.id)], sched_hits)
    check("schedule: ...and the click is CONSUMED, not also a row-wide "
          "'reveal the card' activation", act_hits == [], act_hits)
    row.deleteLater()

    win.close()


def test_topbar_extras_autosize():
    """The top bar's non-essential controls live in a QScrollArea (so the
    window's minimum width is a small constant, not the sum of everything the
    bar could show - see the responsive-layout fix). Built with
    `setWidgetResizable(False)`, which means Qt does NOT automatically resize
    the content widget when ITS OWN layout's sizeHint changes later.

    Reported live: a usage pill starts hidden and only gains its real (much
    wider) size once a reading arrives. Without `_AutoSizingScrollContent`,
    `_extras` stayed frozen at the narrower size it had when it was last laid
    out, and its own QHBoxLayout crammed the newly-widened pill into that
    stale rect - two pills drawing on top of each other, garbled and
    unreadable. `_AutoSizingScrollContent` catches the `QEvent.LayoutRequest`
    Qt already sends `_extras` whenever its layout invalidates and resizes it
    to match, so this covers ANY future cause of a size change, not just
    usage pills.
    """
    import time as _time
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import QApplication
    from app.widgets.main_window import TopBar
    from app import claude_usage as cu

    QApplication.instance() or QApplication([])
    bar = TopBar()
    bar.show()
    bar.resize(1920, 42)
    QApplication.instance().processEvents()

    def limit(key, pct, resets):
        return cu.Limit(key=key, label=cu._LABELS[key], short=cu._SHORT[key],
                        percent=pct, resets_at=resets)

    now = _time.time()
    good = cu.Usage(limits=(limit("five_hour", 21.0, now + 4800),
                            limit("seven_day", 64.0, now + 400000)),
                    fetched_at=now, plan="pro")
    bar.set_usage(good)
    QApplication.instance().processEvents()

    check("topbar-autosize: both Claude pills became visible",
          bar.usage_badge.isVisible() and bar.usage_weekly_badge.isVisible())
    check("topbar-autosize: _extras grew to fit its now-wider content",
          bar._extras.width() >= bar._extras.layout().sizeHint().width())
    check("topbar-autosize: the two pills do not overlap",
          not bar.usage_badge.geometry().intersects(
              bar.usage_weekly_badge.geometry()),
          (bar.usage_badge.geometry(), bar.usage_weekly_badge.geometry()))

    # The MINIMUM is what makes a squeeze impossible rather than unlikely: a
    # QHBoxLayout with less room than its children's minimums shrinks them
    # PAST those minimums, and any moment where this widget is narrower than
    # its row would truncate a pill that has the pixels to spare. `resize()`
    # is clamped to `minimumWidth`, so nothing can put it in that state.
    check("topbar-autosize: the row's minimum is its real content width",
          bar._extras.minimumWidth()
          == bar._extras.layout().sizeHint().width())
    bar._extras.resize(120, bar._extras.height())
    check("topbar-autosize: the row refuses to be squeezed below its content",
          bar._extras.width() >= bar._extras.layout().sizeHint().width())
    for pill in (bar.usage_badge, bar.usage_weekly_badge):
        room = (pill.width() - (pill._PAD + pill._RING + pill._GAP)
                - pill._PAD)
        check("topbar-autosize: a squeezed row still fits each pill's text",
              room >= QFontMetrics(pill._text_font(),
                                   pill).horizontalAdvance(pill._text))
    bar.deleteLater()


def test_options_panel():
    """Every top-bar SETTING lives in one anchored Options popup.

    The bar is a 42px strip and it lost the argument with its own contents.
    Five successive attempts rearranged the same fourteen widgets inside it and
    it was still too small; the scrolling row's answer to running out of room
    is to slide a control out of view with no affordance saying it did. So the
    settings moved out, and only the glanceable readouts and the two primary
    actions keep permanent space.

    What is checked here is the whole contract of that move: the bar keeps
    exactly the right widgets, the panel owns the rest, every switch still
    emits its own signal and every `set_*` reflector still reflects WITHOUT
    emitting (the restore path depends on that - eight saves would fire at
    launch otherwise), and the placement is clamped so the panel cannot open
    off the edge of the window or the screen.
    """
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QToolButton
    from app import ui_theme
    from app.widgets.main_window import TopBar
    from app.widgets.ornaments import anchored_popup_pos

    app = QApplication.instance() or QApplication([])
    app.setStyleSheet(ui_theme.build_qss())
    bar = TopBar()
    bar.resize(1400, 42)
    bar.show()
    app.processEvents()
    panel = bar.options_panel

    # --- 1. what stayed on the bar, and what left ------------------------
    row = bar._extras.layout()
    on_row = [row.itemAt(i).widget() for i in range(row.count())]
    check("options: the bar's scrolling row is now the five pills and the +",
          on_row == [bar.usage_badge, bar.usage_weekly_badge, bar.gemini_badge,
                     bar.gemini_weekly_badge, bar.codex_badge,
                     bar.usage_add_btn],
          [w.objectName() or type(w).__name__ for w in on_row])
    moved = [bar.recover_btn, bar.resume_btn, bar.sound_btn, bar.taskbar_btn,
             bar.auto_update_btn, bar.updates_manage_btn, bar.install_label,
             bar.update_pill, bar.theme_select, bar.font_dec_btn,
             bar.font_inc_btn]
    check("options: every setting is parented to the panel, not the bar",
          all(w.parent() is panel for w in moved),
          [type(w).__name__ for w in moved if w.parent() is not panel])
    check("options: the two essential actions stay pinned on the bar",
          bar.toggle_btn.parent() is bar
          and bar.add_terminal_btn.parent() is bar
          and bar.options_btn.parent() is bar)

    # --- 2. the panel is a real popup, built once ------------------------
    # NOT a QMenu: a QWidgetAction DELETES its reparented widget on release,
    # which crashed the earlier overflow design, and a widget parked in an
    # unopened menu genuinely is not isVisible(), which broke every visibility
    # assertion in this suite. A plain Qt.Popup has neither problem.
    check("options: the panel is a Qt.Popup window",
          bool(panel.windowFlags() & Qt.WindowType.Popup))
    check("options: it is not delete-on-close (its children carry state)",
          not panel.testAttribute(Qt.WidgetAttribute.WA_DeleteOnClose))
    bar.options_btn.click()
    app.processEvents()
    check("options: clicking the button opens it", panel.isVisible())
    same = bar.options_panel
    panel.hide()
    app.processEvents()
    bar.options_btn.click()
    app.processEvents()
    check("options: reopening reuses the SAME panel, never a rebuild",
          bar.options_panel is same and panel.isVisible())

    # A second click on the button must CLOSE the panel. The popup grab hands
    # that press to the panel, which closes as for any outside click, and Qt
    # then replays the press to the button, whose click reopened the panel.
    # QTest clicks bypass the grab, so feed the panel the press it would get.
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    def press_at(global_pt):
        g = QPointF(global_pt)
        ev = QMouseEvent(QEvent.Type.MouseButtonPress,
                         QPointF(panel.mapFromGlobal(global_pt)), g,
                         Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                         Qt.KeyboardModifier.NoModifier)
        QApplication.sendEvent(panel, ev)
        app.processEvents()

    no_replay = Qt.WidgetAttribute.WA_NoMouseReplay
    press_at(bar.options_btn.mapToGlobal(bar.options_btn.rect().center()))
    check("options: a press on the button closes the open panel",
          not panel.isVisible())
    check("options: ...and is not replayed to the button to reopen it",
          panel.testAttribute(no_replay))
    bar.options_btn.click()
    app.processEvents()
    check("options: opening again re-arms the replay for other clicks",
          panel.isVisible() and not panel.testAttribute(no_replay))
    press_at(bar.toggle_btn.mapToGlobal(bar.toggle_btn.rect().center()))
    check("options: a press elsewhere closes it and still replays there",
          not panel.isVisible() and not panel.testAttribute(no_replay))
    panel.hide()

    # --- 3. every switch: click emits, set_* reflects and stays silent ----
    # Each row is a real track-and-thumb ToggleSwitch (green/slid-right when
    # armed, grey/slid-left when off) plus a separate plain-words QLabel -
    # this replaced a whole-row button whose own text carried an LED glyph,
    # which read as a clickable link rather than a switch.
    cases = [
        ("startup recovery", bar.recover_btn, bar.recover_label,
         bar.startupRecoveryToggled, bar.set_startup_recovery,
         bar.startup_recovery, True),
        ("auto continue", bar.resume_btn, bar.resume_label,
         bar.autoContinueToggled, bar.set_auto_continue,
         bar.auto_continue, True),
        ("chime", bar.sound_btn, bar.sound_label,
         bar.soundToggled, bar.set_sound_enabled,
         lambda: bar._sound_on, True),
        ("reply chime", bar.reply_sound_btn, bar.reply_sound_label,
         bar.replySoundToggled, bar.set_reply_sound_enabled,
         lambda: bar._reply_sound_on, False),
        ("taskbar count", bar.taskbar_btn, bar.taskbar_label,
         bar.taskbarBadgeToggled, bar.set_taskbar_badge,
         lambda: bar._taskbar_badge, True),
        ("cli updates", bar.auto_update_btn, bar.auto_update_label,
         bar.autoUpdateToggled, bar.set_auto_update,
         bar.auto_update, False),
    ]
    for name, btn, _label, signal, setter, getter, default in cases:
        seen = []
        signal.connect(seen.append)
        check(f"options: {name} starts at its documented default",
              getter() is default and btn.isChecked() is default,
              (getter(), btn.isChecked()))
        btn.click()
        check(f"options: clicking {name} flips it and emits the new value",
              seen == [not default] and getter() is (not default)
              and btn.isChecked() is (not default), (name, seen, getter()))
        setter(default)
        check(f"options: set_* for {name} reflects without re-emitting",
              seen == [not default] and getter() is default
              and btn.isChecked() is default, (name, seen))
        signal.disconnect(seen.append)

    # every row says what it does in words, not in a glyph the tooltip
    # explains - that was the whole reason for leaving the 42px strip
    labels = ["Recover at start-up", "Resume on usage reset",
              "Question chime", "Reply finished chime", "Taskbar count",
              "Check for CLI updates at start-up"]
    check("options: every switch is labelled in plain words",
          all(lab in label.text() for lab, (_n, _b, label, *_r)
              in zip(labels, cases)),
          [label.text() for _n, _b, label, *_r in cases])

    # --- 4. the appearance rows still drive the same signals -------------
    themed = []
    bar.themeChanged.connect(themed.append)
    ids = [bar.theme_select.itemData(i)
           for i in range(bar.theme_select.count())]
    other = [i for i in ids if i != bar.theme_select.currentData()][0]
    bar.theme_select.setCurrentIndex(bar.theme_select.findData(other))
    check("options: the theme row still emits themeChanged", themed == [other],
          themed)
    bar.set_theme(ids[0])
    check("options: set_theme reflects without re-emitting",
          themed == [other] and bar.theme_select.currentData() == ids[0])
    deltas = []
    bar.globalFontDelta.connect(deltas.append)
    bar.font_dec_btn.click()
    bar.font_inc_btn.click()
    check("options: the font steppers still emit -1 / +1", deltas == [-1, 1],
          deltas)

    # --- 5. no Claude login hides the two recovery rows, nothing else -----
    bar.set_recovery_available(False)
    check("options: no-Claude hides the recovery rows inside the panel",
          not bar.recover_btn.isVisibleTo(panel)
          and not bar.resume_btn.isVisibleTo(panel)
          and bar.sound_btn.isVisibleTo(panel)
          and bar.usage_add_btn.isVisible())
    bar.set_recovery_available(True)

    # --- 6. the placement is clamped, same helper the layout palette uses -
    # The Options button sits at the RIGHT end of a possibly-ultrawide bar, so
    # left-anchoring a panel under it spills off the window on a narrow window
    # and off the display on a wide one.
    panel.adjustSize()
    pos = anchored_popup_pos(bar.options_btn, panel.size())
    screen = bar.screen() or QApplication.primaryScreen()
    avail = screen.availableGeometry()
    win = bar.window().geometry()
    check("options: the panel opens inside the app window",
          pos.x() >= min(win.x(), avail.x())
          and pos.x() + panel.width()
          <= max(win.x() + win.width(), avail.x() + avail.width()),
          (pos.x(), panel.width(), win))
    check("options: ...and on the screen",
          pos.y() >= avail.y()
          and pos.y() + panel.height() <= avail.y() + avail.height(),
          (pos.y(), panel.height(), avail))

    # --- 7. the bar can now shrink far further than it could -------------
    check("options: the bar's minimum width is a small constant",
          bar.minimumSizeHint().width() < 1000,
          bar.minimumSizeHint().width())

    # --- 8. one control per setting: no duplicate lives on the bar -------
    bar_buttons = [w for w in bar.findChildren(QToolButton)
                   if w.parent() is bar]
    # the Log button is no setting: like Add Terminal it opens something (the
    # event log window), so it belongs on the bar. So is the AI Hive update
    # button beside the version badge: an action, not a preference.
    check("options: the bar itself carries exactly five buttons",
          set(bar_buttons) == {bar.toggle_btn, bar.add_terminal_btn,
                               bar.options_btn, bar.log_btn,
                               bar.app_update_btn},
          [w.objectName() for w in bar_buttons])
    bar.deleteLater()


def test_topbar_extras_grow_with_window():
    """The extras row must take every pixel a wider window can give it.

    `QAbstractScrollArea.sizeHint()` is a small constant unrelated to what is
    inside it, so with the breadcrumb holding the layout's stretch the row
    stayed frozen at that constant no matter how wide the window got -
    identically on a 1280px laptop and a 3440px ultrawide, with most of the
    bar's controls parked off-screen behind a permanent scrollbar (reported
    live on a 1440p monitor). `_HWheelScrollArea.sizeHint()` reports the
    content's real width instead, so the QHBoxLayout satisfies it before
    handing the leftover to the breadcrumb.

    Two properties are checked together because either one alone is
    satisfiable by the wrong fix: the row GROWS with the window (a fixed-width
    row fails), and the WINDOW's minimum stays a small constant (a row that
    simply demands its full width fails - that is the >2000px minimum the
    scroll area was introduced to remove).
    """
    from PySide6.QtWidgets import QApplication, QWidget, QVBoxLayout
    app = QApplication.instance() or QApplication([])
    from app.widgets.main_window import TopBar

    host = QWidget()
    v = QVBoxLayout(host)
    v.setContentsMargins(0, 0, 0, 0)
    bar = TopBar(host)
    v.addWidget(bar)
    host.show()

    # The row starts EMPTY - every usage pill is hidden until it has something
    # to say - and an empty row is narrower than the scroll area's own floor,
    # so nothing about growth is observable on it. Put all four pills into
    # their loading state first, which is the state the bar is genuinely in a
    # second after launch.
    bar.mark_usage_loading()
    app.processEvents()

    # Sized RELATIVE to the row's own natural width rather than at fixed pixel
    # widths: font metrics differ between the offscreen platform and a real
    # screen, and a fixed 1280px is already roomy enough here that the row
    # would sit at its full width for every sample and "growth" could not be
    # observed at all.
    # ...and OFFSET by the bar's own minimum, because the pinned chrome (the
    # sidebar toggle, the identity block, Options, Add Terminal) is served
    # before the row is: sampling below that floor measures three windows that
    # all leave the row at its minimum, which says nothing about growth.
    natural = bar._extras.sizeHint().width()
    base = bar.minimumSizeHint().width()
    narrow, mid, wide = (base + natural // 3, base + (natural * 2) // 3,
                         base + natural * 2)

    widths = {}
    for w in (narrow, mid, wide):
        host.resize(w, 120)
        app.processEvents()
        widths[w] = bar._extras_scroll.width()

    check("topbar-grow: the extras row widens as the window does",
          widths[narrow] < widths[mid] < widths[wide], widths)
    check("topbar-grow: a wide window fits the row's whole natural width",
          widths[wide] >= natural, (widths[wide], natural))
    check("topbar-grow: ...and then no scrollbar is needed",
          not bar._extras_scroll.horizontalScrollBar().isVisible())

    # the scroll area's own minimum is what decides how narrow the WINDOW may
    # be; it must stay a small constant rather than tracking the content
    check("topbar-grow: the row's minimum stays a small constant",
          bar._extras_scroll.minimumSizeHint().width() <= 120,
          bar._extras_scroll.minimumSizeHint().width())
    check("topbar-grow: so the whole bar still fits a laptop width",
          bar.minimumSizeHint().width() < 1000,
          bar.minimumSizeHint().width())
    host.deleteLater()
