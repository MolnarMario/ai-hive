"""Usage readouts: token and plan usage, the Gemini usage poll, and the
usage pills in the top bar."""

import os
import tempfile
import time
from pathlib import Path

from .harness import SCRATCH_CWD, check


def test_token_usage_badge():
    """The card header shows a compact context-window usage badge (e.g.
    "20% of 1M") read from the transcript's last assistant usage record.
    Transient like the AI title: computed from input+cache+output tokens,
    sized to the model's window, sidechains skipped, never persisted."""
    import json as _json
    from PySide6.QtWidgets import QApplication
    from app import transcripts
    from app.terminal_agent import TerminalAgent
    from app.process_worker import AgentKind, build_spec

    QApplication.instance() or QApplication([])

    # --- context-window sizing per model ---
    check("tokens: opus-4.x sized to the 1M window",
          transcripts.context_window_for("claude-opus-4-8") == 1_000_000)
    check("tokens: unknown model falls back to 200K",
          transcripts.context_window_for("some-old-model") == 200_000)

    # --- transcript parsing: last main-turn usage, sidechains skipped ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-tokens-"))
    tpath = tmp / "conv.jsonl"

    def _asst(inp, cc, cr, out, model="claude-opus-4-8", side=False):
        return {"type": "assistant", "isSidechain": side,
                "message": {"model": model, "role": "assistant",
                            "usage": {"input_tokens": inp,
                                      "cache_creation_input_tokens": cc,
                                      "cache_read_input_tokens": cr,
                                      "output_tokens": out}}}

    recs = [{"type": "user", "text": "hi"},
            _asst(10, 0, 100, 50),                 # early main turn
            _asst(5, 0, 999999, 5, side=True),     # sub-agent: must be ignored
            _asst(100, 900, 199000, 1000)]         # latest main turn -> 201000
    tpath.write_text("\n".join(_json.dumps(r) for r in recs) + "\n",
                     encoding="utf-8")
    used, window = transcripts._read_latest_token_usage(str(tpath))
    check("tokens: sums the LAST main-conversation usage record",
          used == 201000, used)
    check("tokens: sidechain usage never wins", used == 201000)
    check("tokens: window bumped to 1M when a turn exceeds 200K",
          window == 1_000_000, window)
    check("tokens: missing file -> (0, 0)",
          transcripts._read_latest_token_usage(str(tmp / "nope.jsonl")) == (0, 0))

    # re-reads when the transcript grows
    with open(tpath, "a", encoding="utf-8") as fh:
        fh.write(_json.dumps(_asst(1, 0, 299999, 0)) + "\n")  # -> 300000
    used2, _ = transcripts._read_latest_token_usage(str(tpath))
    check("tokens: re-reads when the transcript changes", used2 == 300000, used2)

    # --- agent badge formatting + transient emission ---
    a = TerminalAgent(build_spec(AgentKind.CLAUDE, "Solo", cwd=SCRATCH_CWD))
    seen = []
    a.tokens_changed.connect(seen.append)
    check("tokens: badge empty before any usage", a.token_badge() == "")
    a.set_token_usage(200_000, 1_000_000)
    check("tokens: badge formats as '20% of 1M'",
          a.token_badge() == "20% of 1M" and seen[-1] == "20% of 1M",
          (a.token_badge(), seen))
    a.set_token_usage(50_000, 200_000)
    check("tokens: 200K window renders as 'K'", a.token_badge() == "25% of 200K")
    n = len(seen)
    a.set_token_usage(50_000, 200_000)   # unchanged -> no re-emit
    check("tokens: no signal when usage is unchanged", len(seen) == n)
    a.set_token_usage(0, 0)              # cleared -> badge hidden again
    check("tokens: clearing usage empties the badge", a.token_badge() == "")
    a.deleteLater()


def test_plan_usage():
    """The top-bar Claude plan-usage readout: parsing, the badge line, the
    limit-reached edges other features hook into, and the two rules that keep
    it cheap — a reading NEVER marks the session dirty, and polling is opt-in
    so this suite never touches the network or the user's real account."""
    import json as _json
    import time as _time
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu

    QApplication.instance() or QApplication([])
    now = _time.time()

    def limit(key, pct, resets=None):
        return cu.Limit(key=key, label=cu._LABELS[key], short=cu._SHORT[key],
                        percent=pct, resets_at=resets)

    # --- parsing the real payload shape (captured from /api/oauth/usage) ---
    payload = {
        "five_hour": {"utilization": 21.0,
                      "resets_at": "2026-08-02T12:29:59.983234+00:00",
                      "limit_dollars": None},
        "seven_day": None,               # Pro has no weekly window
        "seven_day_opus": None,
        "extra_usage": {"is_enabled": False},
        "limits": [{"kind": "session", "percent": 21}],
    }
    lims = cu.parse_utilization(payload)
    check("plan-usage: parses five_hour utilization", len(lims) == 1
          and lims[0].key == "five_hour" and lims[0].percent == 21.0)
    check("plan-usage: resets_at parsed to epoch seconds",
          abs(lims[0].resets_at - 1785673799.983234) < 1.0)
    check("plan-usage: null windows skipped, not reported as 0%",
          all(l.key != "seven_day" for l in lims))
    check("plan-usage: garbage payload yields no limits",
          cu.parse_utilization({"five_hour": "nonsense"}) == ()
          and cu.parse_utilization({}) == ())
    check("plan-usage: epoch resets_at also accepted",
          cu.parse_utilization(
              {"five_hour": {"utilization": 5, "resets_at": 1785673799}}
          )[0].resets_at == 1785673799.0)

    # --- headline picks the most-constrained window ---
    multi = cu.Usage(limits=(limit("five_hour", 21.0), limit("seven_day", 64.0)))
    check("plan-usage: headline is the highest-utilization window",
          cu.headline(multi).key == "seven_day")
    check("plan-usage: headline of an empty reading is None",
          cu.headline(cu.Usage()) is None and cu.headline(None) is None)

    # --- five_hour/weekly: the two SEPARATE selectors the two pills use.
    # Unlike headline, neither ever falls back to the other window - that
    # would let one pill silently show the other's number.
    check("plan-usage: five_hour never falls back to the 7d window",
          cu.five_hour(multi).key == "five_hour")
    check("plan-usage: weekly never falls back to the 5h window",
          cu.weekly(multi).key == "seven_day")
    max_plan = cu.Usage(limits=(limit("five_hour", 10.0),
                                limit("seven_day_opus", 30.0),
                                limit("seven_day_sonnet", 55.0)))
    check("plan-usage: weekly picks the most-constrained 7d window on Max",
          cu.weekly(max_plan).key == "seven_day_sonnet")
    check("plan-usage: five_hour/weekly of an empty reading are None",
          cu.five_hour(cu.Usage()) is None and cu.weekly(cu.Usage()) is None
          and cu.five_hour(None) is None and cu.weekly(None) is None)
    check("plan-usage: five_hour is None when the plan has no 5h window",
          cu.five_hour(cu.Usage(limits=(limit("seven_day", 10.0),))) is None)
    check("plan-usage: weekly is None when the plan has no 7d window",
          cu.weekly(cu.Usage(limits=(limit("five_hour", 10.0),))) is None)

    # --- the badge line: countdown FIRST, then wall-clock, in local time ---
    line = cu.format_limit(limit("five_hour", 21.0, now + 4800), now=now)
    check("plan-usage: line reads 'Claude 21% used, resets in 1h20m at HH:MM'",
          line.startswith("Claude 21% used, resets in 1h20m at ")
          and len(line.split(" at ")[1]) == 5, line)
    check("plan-usage: local wall-clock, not UTC",
          line.endswith(_time.strftime("%H:%M", _time.localtime(now + 4800))))
    check("plan-usage: window named only when the plan has several",
          cu.format_limit(limit("seven_day", 64.0, now + 600), now=now,
                          with_label=True).startswith("7d Claude 64% used"))
    check("plan-usage: a spent window spells out 'limit reached'",
          cu.format_limit(limit("five_hour", 100.0, now + 600),
                          now=now).startswith("limit reached, resets in 10m"))
    check("plan-usage: no reset time degrades to the bare percent",
          cu.format_limit(limit("five_hour", 21.0)) == "Claude 21% used")
    check("plan-usage: countdown formats scale",
          (cu.format_countdown(4800), cu.format_countdown(600),
           cu.format_countdown(30)) == ("1h20m", "10m", "30s"))
    # --- format_countdown_dh: days+hours only, for the 7-day pill ---
    check("plan-usage: dh countdown drops minutes at every scale",
          (cu.format_countdown_dh(6 * 86400 + 23 * 3600 + 45 * 60),
           cu.format_countdown_dh(6 * 86400),
           cu.format_countdown_dh(13 * 3600 + 45 * 60),
           cu.format_countdown_dh(1800))
          == ("6d23h", "6d", "13h", "<1h"))
    check("plan-usage: format_limit(days_only=True) uses the dh countdown",
          cu.format_limit(limit("seven_day", 40.0, now + 6 * 86400 + 3600),
                          now=now, days_only=True).startswith(
                              "Claude 40% used, resets in 6d1h at "))
    check("plan-usage: format_limit(days_only=False) keeps minutes",
          cu.format_limit(limit("seven_day", 40.0, now + 4800), now=now,
                          days_only=False).startswith(
                              "Claude 40% used, resets in 1h20m at "))
    check("plan-usage: age formats scale",
          (cu.format_since(2), cu.format_since(42), cu.format_since(180),
           cu.format_since(7200)) == ("just now", "42s ago", "3m ago", "2h ago"))

    # --- blocked/resets_at: the hook other features build on ---
    spent = cu.Usage(limits=(limit("five_hour", 100.0, now + 900),))
    check("plan-usage: blocked reports the spent window",
          spent.blocked is not None and spent.blocked.key == "five_hour")
    check("plan-usage: blocked carries when it frees up",
          spent.resets_at == now + 900)
    check("plan-usage: headroom means not blocked",
          cu.Usage(limits=(limit("five_hour", 99.0, now),)).blocked is None)

    # --- credentials are read-only, and a dead token never hits the wire ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-usage-"))
    creds = tmp / ".credentials.json"
    creds.write_text(_json.dumps({"claudeAiOauth": {
        "accessToken": "not-a-real-token", "subscriptionType": "pro",
        "expiresAt": int((now - 3600) * 1000)}}), encoding="utf-8")
    before = creds.read_bytes()
    old_env = os.environ.get("CLAUDE_CONFIG_DIR")
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    try:
        expired = cu.fetch()
        check("plan-usage: expired token short-circuits (no request)",
              expired.error == "expired" and not expired.limits)
        check("plan-usage: credentials file never rewritten",
              creds.read_bytes() == before)
        creds.unlink()
        check("plan-usage: missing credentials report no-auth, never raise",
              cu.fetch().error == "no-auth")
    finally:
        if old_env is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = old_env

    # --- window wiring: badge, edges, persistence ---
    from main import create_main_window, setup_application
    from app.session_store import SessionStore
    app = QApplication.instance()
    setup_application(app)
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    # wide enough that the top bar's overflow collapse never kicks in - this
    # test is about pill content/visibility logic, not the responsive layout
    win.resize(2400, 900)
    app.processEvents()
    badge = win.top_bar.usage_badge
    weekly_badge = win.top_bar.usage_weekly_badge

    claude_poll = win.usage_poller("claude")
    check("plan-usage: polling is opt-in, so the suite never fetches",
          not claude_poll.is_running() and win.plan_usage() is None)
    check("plan-usage: badge hidden until a reading arrives",
          not badge.isVisible() and not badge.has_reading())

    good = cu.Usage(limits=(limit("five_hour", 21.0, now + 4800),),
                    fetched_at=now, plan="pro")
    win._on_usage_ready(good)
    app.processEvents()
    check("plan-usage: reading shows the badge with the full line, labelled "
          "5h so it's tellable apart from the 7d pill beside it",
          badge.isVisible()
          and badge._text.startswith("5h Claude 21% used, resets in"))
    check("plan-usage: tooltip carries plan, every window, and the age",
          "Pro plan" in badge.toolTip() and "Current session" in badge.toolTip()
          and "Updated" in badge.toolTip())
    check("plan-usage: plan_usage() exposes the reading",
          win.plan_usage() is good)
    check("plan-usage: the 5h reading has no 7d window, so the weekly pill "
          "stays empty",
          not weekly_badge.has_reading())

    # --- the separate 7d pill: its own window, its own days+hours format ---
    weekly_reading = cu.Usage(
        limits=(limit("five_hour", 21.0, now + 4800),
               limit("seven_day", 40.0, now + 6 * 86400 + 3600)),
        fetched_at=now, plan="max")
    win._on_usage_ready(weekly_reading)
    app.processEvents()

    # Not an exact-string match: the widget formats against the REAL clock
    # (unlike the pure format_limit() checks above, which pin `now`), and
    # this whole test function runs long enough that a minute can genuinely
    # tick over between the reading landing and the assertion running. So
    # this checks the SHAPE (5h keeps minutes, 7d drops them), which is the
    # thing days_only actually controls, rather than the exact digits.
    def _countdown(text):
        return text.split("resets in ")[1].split(" at ")[0]

    five_cd = _countdown(badge._text)
    weekly_cd = _countdown(weekly_badge._text)
    check("plan-usage: the 5h pill still shows the 5h window, to the minute",
          badge._text.startswith("5h Claude 21% used, resets in")
          and five_cd.endswith("m"))
    check("plan-usage: the 7d pill shows the 7d window, days+hours only "
          "(no minutes)",
          weekly_badge._text.startswith("7d Claude 40% used, resets in")
          and "m" not in weekly_cd
          and ("d" in weekly_cd or weekly_cd.endswith("h")
               or weekly_cd == "<1h"))
    check("plan-usage: the two pills never share a window",
          badge.window == "five_hour" and weekly_badge.window == "weekly")
    win._on_usage_ready(good)
    app.processEvents()

    # a reading is TRANSIENT: it must never schedule a save (this polls every
    # minute forever; wiring it to dirty would thrash session.json)
    win._save_timer.stop()
    win._on_usage_ready(cu.Usage(limits=(limit("five_hour", 22.0, now + 4700),),
                                 fetched_at=now, plan="pro"))
    check("plan-usage: a reading never marks the session dirty",
          not win._save_timer.isActive())

    # edges: rising once, level-stable, falling once
    seen = {"hit": 0, "clear": 0, "limit": None}
    win.planLimitReached.connect(
        lambda l: seen.update(hit=seen["hit"] + 1, limit=l))
    win.planLimitCleared.connect(lambda: seen.update(clear=seen["clear"] + 1))
    blocked = cu.Usage(limits=(limit("five_hour", 100.0, now + 120),),
                       fetched_at=now, plan="pro")
    win._on_usage_ready(blocked)
    win._on_usage_ready(blocked)          # level, not a new edge
    check("plan-usage: planLimitReached fires once on the rising edge",
          seen["hit"] == 1 and seen["clear"] == 0)
    check("plan-usage: the edge carries the reset time",
          seen["limit"] is not None and seen["limit"].resets_at == now + 120)
    check("plan-usage: blocked badge reads 'limit reached'",
          badge._text.startswith("5h limit reached, resets in"))
    check("plan-usage: an extra poll is armed for just after the reset",
          claude_poll.reset_poll_armed()
          and claude_poll.reset_poll_remaining_ms() > 120000)
    win._on_usage_ready(good)
    check("plan-usage: planLimitCleared fires once on the falling edge",
          seen["clear"] == 1 and seen["hit"] == 1)
    check("plan-usage: a reset beyond the next poll arms nothing extra",
          not claude_poll.reset_poll_armed())
    # a weekly window can reset days out; that is the minute poll's job, not a
    # multi-day QTimer (whose interval is 32-bit anyway)
    win._on_usage_ready(cu.Usage(limits=(limit("seven_day", 100.0,
                                               now + 3 * 86400),),
                                 fetched_at=now, plan="max"))
    check("plan-usage: a far-off reset is left to the ordinary poll",
          not claude_poll.reset_poll_armed())
    win._on_usage_ready(good)

    # --- the window feeds the shared cadence (app.usage_poll) the right
    # agents: only a CLAUDE agent's work speeds Claude's poll. The cadence
    # rules themselves (thresholds, grace, backoff) are test_usage_poll_policy.
    from app import usage_poll as up
    from app.process_worker import AgentKind, build_spec
    hot = cu.Usage(limits=(limit("five_hour", 95.0, now + 600),),
                   fetched_at=now, plan="pro")
    win._on_usage_ready(hot)
    check("plan-usage: nearly spent but nobody working polls at the idle rate",
          claude_poll.interval_ms() == up.IDLE_POLL_MS,
          claude_poll.interval_ms())

    ws_hot = win.manager.create_workspace("usage-hot", str(tmp))
    agent_hot = win.manager.add_terminal(
        ws_hot.id, build_spec(AgentKind.CMD, "Hot", cwd=str(tmp)),
        autostart=False)
    agent_hot.spec.provider = "gemini"
    agent_hot._busy = True            # what _mark_busy sets on an output burst
    check("plan-usage: a Gemini agent working does not speed Claude's poll up",
          claude_poll.interval_ms() == up.IDLE_POLL_MS)
    agent_hot.spec.provider = "claude"
    check("plan-usage: nearly spent + a Claude agent working polls at 30 s",
          claude_poll.interval_ms() == up.URGENT_POLL_MS,
          claude_poll.interval_ms())
    agent_hot._busy = False
    win.manager.remove_workspace(ws_hot.id)
    win._on_usage_ready(good)
    win._save_timer.stop()

    # (failure greying rules: test_usage_poll_policy)
    over = cu.Usage(limits=(limit("five_hour", 21.0, time.time() - 5),),
                    fetched_at=time.time() - 60, plan="pro")
    win._on_usage_ready(over)
    check("plan-usage: a window whose reset passed after the reading greys",
          badge.is_stale())
    win._on_usage_ready(good)
    win._save_timer.stop()

    # visibility preference persists; toggling it IS a save (a UI preference)
    win._on_usage_tracker_toggled("claude_five_hour", False)
    app.processEvents()
    check("plan-usage: closing the pill removes it from the bar",
          not badge.isVisible())
    check("plan-usage: a later reading cannot resurrect a closed pill",
          (win._on_usage_ready(good), app.processEvents(),
           not badge.isVisible())[-1])
    payload_ui = win._session_payload()["ui"]
    check("plan-usage: preference persisted under ui.usage_trackers",
          payload_ui["usage_trackers"]["claude_five_hour"] is False
          and payload_ui["usage_trackers"]["gemini_weekly"] is True)
    check("plan-usage: the legacy usage_visible mirror is derived, not stale",
          payload_ui["usage_visible"] is True)
    win.close()

    win2 = create_main_window(store)
    win2.show()
    app.processEvents()
    check("plan-usage: preference restored on reopen",
          win2.top_bar.usage_trackers()["claude_five_hour"] is False)
    check("plan-usage: default is ON when never saved",
          create_main_window(
              SessionStore(path=tmp / "fresh.json")
          ).top_bar.usage_trackers()["claude_five_hour"])
    win2.close()

    # no Claude login at all: hide for good rather than show an empty pill
    win3 = create_main_window(SessionStore(path=tmp / "noauth.json"))
    win3.show()
    win3.usage_poller("claude")._running = True   # as start() leaves it
    win3._on_usage_ready(cu.Usage(error="no-auth"))
    app.processEvents()
    check("plan-usage: no-auth hides both Claude pills and stops polling",
          not win3.top_bar.usage_badge.isVisible()
          and not win3.top_bar.usage_weekly_badge.isVisible()
          and not win3.usage_poller("claude").is_running())
    win3.close()

    # A poll that fails with NO earlier reading used to leave a hole in the bar
    # — reported live as "did you delete the usage readout?" after a restart hit
    # an http 429 (and CLI 2.1.220 no longer writes the cachedUsageUtilization
    # seed that used to paint a number instantly). It must say so instead.
    win4 = create_main_window(SessionStore(path=tmp / "unreadable.json"))
    win4.show()
    p4 = win4.usage_poller("claude")
    p4._running = True                  # as start() leaves it, minus the fetch
    b4 = win4.top_bar.usage_badge
    win4._on_usage_ready(cu.Usage(error="http 429"))
    app.processEvents()
    check("plan-usage: a failure with no reading shows the can't-read pill",
          b4.isVisible() and "unreadable" in b4._text
          and "click to refresh" in b4._text)
    check("plan-usage: the can't-read pill is not mistaken for a reading",
          not b4.has_reading() and b4.has_content()
          and win4.plan_usage() is None)
    check("plan-usage: its tooltip names the failure and the way out",
          "http 429" in b4.toolTip() and "Click to try again" in b4.toolTip())
    check("plan-usage: polling continues (only no-auth is terminal)",
          p4.is_running() and p4.next_poll_at() > 0)
    # and a real number supersedes the error pill entirely
    win4._on_usage_ready(good)
    app.processEvents()
    check("plan-usage: a later reading replaces the can't-read pill",
          b4.isVisible() and b4.has_reading()
          and b4._text.startswith("5h Claude 21% used") and not b4._unreadable)
    # an error is not a reason to force the readout back onto a bar the user
    # deliberately cleared
    win4._on_usage_tracker_toggled("claude_five_hour", False)
    win4._on_usage_ready(cu.Usage(error="http 429"))
    app.processEvents()
    check("plan-usage: a closed readout stays closed when a poll fails",
          not b4.isVisible())
    win4.close()


def test_usage_trackers_preference():
    """The per-pill usage picker: the X that closes one readout, the + that
    brings it back, and the preference that remembers.

    The bar used to carry one boolean for all readouts, on a right-click item.
    A user who runs only Claude had to look at two Gemini pills that can never
    say anything (and pay a ~3s subprocess a minute for them), or lose the
    Claude ones too. The preference is now per pill (Claude 5h, Claude 7d,
    Gemini 5h, Gemini 7d - four in all), and the + button is the single
    control - a master toggle sitting on top of four checkboxes is two
    controls for one setting, and a pill checked in one but hidden by the other
    is not explainable.
    """
    import json as _json
    import pathlib
    import tempfile
    import time as _time
    from PySide6.QtWidgets import QApplication
    from app import claude_usage as cu
    from app.session_store import SessionStore
    from app.widgets.main_window import (USAGE_TRACKER_KEYS,
                                         USAGE_TRACKER_LABELS)
    from main import create_main_window

    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-trackers-"))
    now = _time.time()
    good = cu.Usage(limits=(cu.Limit(key="five_hour",
                                     label=cu._LABELS["five_hour"],
                                     short=cu._SHORT["five_hour"],
                                     percent=21.0, resets_at=now + 4800),),
                    fetched_at=now, plan="pro")

    store = SessionStore(path=tmp / "s.json")
    win = create_main_window(store)
    win.show()
    # wide enough that the top bar's overflow collapse never kicks in - this
    # test is about the pill picker's content/visibility logic, not layout
    win.resize(2400, 900)
    app = QApplication.instance()
    app.processEvents()
    bar = win.top_bar

    check("usage-trackers: every readout is on by default",
          all(bar.usage_trackers()[k] for k in USAGE_TRACKER_KEYS))
    check("usage-trackers: ...but no pill is on the bar without content",
          not bar.usage_badge.isVisible()
          and not bar.usage_weekly_badge.isVisible()
          and not bar.gemini_badge.isVisible()
          and not bar.gemini_weekly_badge.isVisible())
    check("usage-trackers: the + picker is on the bar",
          bar.usage_add_btn.isVisible())

    # the menu mirrors the preference and is rebuilt per click, so it can never
    # show a stale checkmark
    menu = bar.build_tracker_menu()
    acts = menu.actions()
    check("usage-trackers: one checkable entry per readout",
          len(acts) == len(USAGE_TRACKER_KEYS) and all(a.isCheckable() for a in acts)
          and [a.text() for a in acts]
          == [USAGE_TRACKER_LABELS[k] for k in USAGE_TRACKER_KEYS])
    check("usage-trackers: the entries start checked",
          all(a.isChecked() for a in acts))

    # put content in all five so visibility is decided by the preference alone
    win._on_usage_ready(good)
    bar.mark_usage_loading()
    app.processEvents()
    check("usage-trackers: loading counts as content, so the bar fills at once",
          bar.gemini_badge.isVisible() and bar.gemini_weekly_badge.isVisible()
          and bar.codex_badge.isVisible()
          and bar.usage_badge.isVisible() and bar.usage_weekly_badge.isVisible())

    # the X closes exactly one pill
    seen = []
    bar.usageTrackerToggled.connect(lambda k, on: seen.append((k, on)))
    bar.gemini_weekly_badge.close_btn.click()
    app.processEvents()
    check("usage-trackers: the X emits (its own key, False)",
          seen == [("gemini_weekly", False)], seen)
    check("usage-trackers: it closes that pill and no other",
          not bar.gemini_weekly_badge.isVisible()
          and bar.gemini_badge.isVisible() and bar.usage_badge.isVisible())
    check("usage-trackers: a later reading cannot resurrect a closed pill",
          (bar.mark_usage_loading(), app.processEvents(),
           not bar.gemini_weekly_badge.isVisible())[-1])

    # ...and closing IS a save, while a reading still is NOT
    win._save_timer.stop()
    win._on_usage_tracker_toggled("gemini_five_hour", False)
    check("usage-trackers: closing a pill schedules a save (a UI preference)",
          win._save_timer.isActive())
    win._save_timer.stop()
    win._on_usage_ready(good)
    check("usage-trackers: a reading still never marks the session dirty",
          not win._save_timer.isActive())

    win._on_usage_tracker_toggled("claude_weekly", False)
    check("usage-trackers: the + survives every pill being closed",
          (win._on_usage_tracker_toggled("claude_five_hour", False),
           app.processEvents(),
           bar.usage_add_btn.isVisible()
           and not bar.usage_badge.isVisible()
           and not bar.usage_weekly_badge.isVisible())[-1])
    win._on_usage_tracker_toggled("codex_five_hour", False)
    check("usage-trackers: the menu now shows every readout unchecked",
          not any(a.isChecked() for a in bar.build_tracker_menu().actions()))

    # no Claude login hides the recovery switches, but NEVER the picker: a
    # Gemini-only user would otherwise have no control at all
    bar.set_recovery_available(False)
    # isVisibleTo(options_panel), NOT isVisible(): the two switches live in
    # the Options popup, which is closed here, so isVisible() is False for
    # every one of them and the assertion would pass while testing nothing.
    check("usage-trackers: no-Claude hides the recovery rows, not the picker",
          not bar.recover_btn.isVisibleTo(bar.options_panel)
          and not bar.resume_btn.isVisibleTo(bar.options_panel)
          and bar.usage_add_btn.isVisible())
    bar.set_recovery_available(True)

    # re-checking brings it back, in its loading state rather than as a gap
    win._on_usage_tracker_toggled("claude_five_hour", True)
    app.processEvents()
    check("usage-trackers: re-checking restores the pill, showing loading",
          bar.usage_badge.isVisible() and bar.usage_badge.has_content()
          and "reading" in bar.usage_badge._text)
    check("usage-trackers: its sibling stays closed, closing one leaves the "
          "other alone",
          not bar.usage_weekly_badge.isVisible())

    win._save_session()
    win.close()

    saved = _json.loads((tmp / "s.json").read_text(encoding="utf-8-sig"))
    check("usage-trackers: persisted per key under ui.usage_trackers",
          saved["ui"]["usage_trackers"]["gemini_weekly"] is False
          and saved["ui"]["usage_trackers"]["claude_five_hour"] is True
          and saved["ui"]["usage_trackers"]["claude_weekly"] is False)

    win2 = create_main_window(SessionStore(path=tmp / "s.json"))
    check("usage-trackers: restored per key on reopen",
          win2.top_bar.usage_trackers() == saved["ui"]["usage_trackers"])
    win2.close()

    # MIGRATION off the older single boolean. A user who hid the whole readout
    # must not have it put back; and the newer key wins when both are present.
    saved["ui"].pop("usage_trackers")
    saved["ui"]["usage_visible"] = False
    (tmp / "legacy.json").write_text(_json.dumps(saved), encoding="utf-8")
    win3 = create_main_window(SessionStore(path=tmp / "legacy.json"))
    check("usage-trackers: legacy usage_visible=False closes every pill",
          not any(win3.top_bar.usage_trackers().values()))
    win3.close()

    saved["ui"]["usage_visible"] = True
    (tmp / "legacy_on.json").write_text(_json.dumps(saved), encoding="utf-8")
    win4 = create_main_window(SessionStore(path=tmp / "legacy_on.json"))
    check("usage-trackers: legacy usage_visible=True opens every pill",
          all(win4.top_bar.usage_trackers().values()))
    win4.close()

    saved["ui"]["usage_trackers"] = {"claude_five_hour": False,
                                     "claude_weekly": False,
                                     "gemini_five_hour": True,
                                     "gemini_weekly": True}
    saved["ui"]["usage_visible"] = True          # deliberately contradictory
    (tmp / "both.json").write_text(_json.dumps(saved), encoding="utf-8")
    win5 = create_main_window(SessionStore(path=tmp / "both.json"))
    check("usage-trackers: the per-key preference wins over the legacy mirror",
          win5.top_bar.usage_trackers()["claude_five_hour"] is False)
    win5.close()


def test_gemini_usage_read_disables_agy_auto_update():
    """The terminal window that flashed over the desktop during a Gemini poll
    was agy's OWN auto-updater, not any console AI Hive creates.

    Measured chain: `agy --print /usage` spawns `agy --bg-updater`, which spawns
    `agy --version`, which gets its own console two levels below us; Windows 11
    hands that console to the default terminal app and shows a real 1199x616
    frame for ~280ms. Creation flags never reach a grandchild, so the only lever
    is asking agy not to run the updater at all. The value is compared as a
    STRING by agy: "1" and "TRUE" were both tested live and both still flashed.
    """
    import os
    from app import gemini_usage

    check("gemini env: the exact lowercase literal agy compares against",
          gemini_usage.AUTO_UPDATE_OFF ==
          {"AGY_CLI_DISABLE_AUTO_UPDATE": "true"},
          gemini_usage.AUTO_UPDATE_OFF)

    seen = {}

    class _Res:
        returncode = 0
        stdout = ""

    import subprocess
    real = subprocess.run

    def fake_run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _Res()

    subprocess.run = fake_run
    try:
        gemini_usage._read_usage("agy.exe", 8.0)
    finally:
        subprocess.run = real

    env = seen["kw"].get("env") or {}
    check("gemini env: the usage read passes the disable flag to the child",
          env.get("AGY_CLI_DISABLE_AUTO_UPDATE") == "true", env.get(
              "AGY_CLI_DISABLE_AUTO_UPDATE"))
    check("gemini env: it INHERITS the rest of the environment rather than "
          "replacing it (agy needs PATH/APPDATA to find its own state)",
          all(env.get(k) == v for k, v in os.environ.items()), len(env))
    check("gemini env: os.environ itself is never mutated, so the user's own "
          "agy sessions keep auto-updating",
          "AGY_CLI_DISABLE_AUTO_UPDATE" not in os.environ)
    if os.name == "nt":
        check("gemini env: CREATE_NO_WINDOW stays for the child we DO control",
              seen["kw"].get("creationflags", 0) & 0x08000000)


def test_gemini_usage_polling_is_offthread_and_optin():
    """The Gemini readout must never block the GUI thread, and must never poll
    unless main.py asks for it.

    `gemini_usage.fetch()` shells out to `agy --print /usage` and MEASURES ~3.0
    seconds. It was being called inline, in MainWindow.__init__ and again on
    every timer tick -- and TWICE per tick, because the retune fetched its own
    copy. That froze the whole app for ~6s a minute (worse at the 10s urgent
    rate) with the CPU idle, since it is blocked on a subprocess rather than
    computing. It also made this suite shell out to the user's real CLI and
    added ~3s to every window it builds, which is what started breaking the
    elapsed-time-sensitive checks elsewhere. Same rule as the Claude readout:
    off-thread, and OPT-IN via start_usage_polling."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import gemini_usage
    from app.session_store import SessionStore
    from main import create_main_window

    QApplication.instance() or QApplication([])
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-gemusage-"))

    calls = []
    real = gemini_usage.fetch
    gemini_usage.fetch = lambda *a, **k: (calls.append(1), None)[1]
    try:
        win = create_main_window(SessionStore(path=tmp / "s.json"))
        check("gemini-usage: building a window does NOT shell out to the CLI",
              calls == [], len(calls))
        gpoll = win.usage_poller("gemini")
        check("gemini-usage: ...and does not start the poller either",
              not gpoll.is_running())

        # the countdown tick retunes from the reading it has, never a fetch
        win._tick_usage()
        check("gemini-usage: the tick never fetches", calls == [], len(calls))

        # in-flight guard: an 8s CLI timeout is longer than the urgent
        # interval, so a timer must not launch a thread per tick
        gpoll._inflight = True
        gpoll.poll()
        check("gemini-usage: a poll already in flight is not stacked",
              calls == [], len(calls))

        win._on_gemini_usage_ready(None)
        check("gemini-usage: a finished poll clears the in-flight guard",
              not gpoll._inflight)
        check("gemini-usage: building a window puts no pill on the bar",
              not win.top_bar.gemini_badge.isVisible()
              and not win.top_bar.usage_badge.isVisible())
        # a failed read must SAY so rather than vanish: the badge used to hide
        # itself, and a blanket try/except hid that it had, so with agy absent
        # the readout simply ceased to exist with no way to ask it to retry
        check("gemini-usage: a failed read shows the can't-read pill",
              (win._on_gemini_usage_ready(
                  gemini_usage.GeminiUsage(error="no-data")),
               win.top_bar.gemini_badge.has_content()
               and not win.top_bar.gemini_badge.has_reading())[-1])
        win.close()

        # With both Gemini pills closed nothing consumes the reading, so the
        # ~3s subprocess is skipped entirely - that is the point of letting a
        # Claude-only user close them. The CLAUDE poll is NOT gated this way:
        # planLimitReached/planLimitCleared/plan_usage() hang off it.
        import app.claude_usage as _cu
        real_claude = _cu.fetch
        _cu.fetch = lambda *a, **k: _cu.Usage(error="no-data")
        try:
            win2 = create_main_window(SessionStore(path=tmp / "off.json"))
            win2._on_usage_tracker_toggled("gemini_five_hour", False)
            win2._on_usage_tracker_toggled("gemini_weekly", False)
            calls.clear()
            win2.start_usage_polling()
            check("gemini-usage: both trackers off means the poll never arms",
                  not win2.usage_poller("gemini").is_running() and calls == [],
                  len(calls))
            check("gemini-usage: ...but the Claude poll keeps running",
                  win2.usage_poller("claude").is_running())
            win2._on_usage_tracker_toggled("gemini_weekly", True)
            deadline = time.time() + 5
            while not calls and time.time() < deadline:
                time.sleep(0.01)          # the fetch runs on its own thread
            check("gemini-usage: re-enabling arms the timer and fetches at once",
                  win2.usage_poller("gemini").is_running() and len(calls) == 1,
                  len(calls))
            check("gemini-usage: a re-enabled pill shows loading, not a gap",
                  win2.top_bar.gemini_weekly_badge.has_content())
            win2.close()

            # ...and a window that never opted in must not shell out even when
            # a tracker is switched on at runtime
            win3 = create_main_window(SessionStore(path=tmp / "noopt.json"))
            win3._on_usage_tracker_toggled("gemini_five_hour", False)
            calls.clear()
            win3._on_usage_tracker_toggled("gemini_five_hour", True)
            check("gemini-usage: a window that never opted in never fetches",
                  calls == [] and not win3.usage_poller("gemini").is_running(),
                  len(calls))
            win3.close()
        finally:
            _cu.fetch = real_claude
    finally:
        gemini_usage.fetch = real


def test_usage_poll_policy():
    """Every usage pill polls on ONE policy (app.usage_poll), set by the user:
    90 s while that provider's agents work, 30 s from 90% used, 6 minutes while
    none do (the browser or the official apps can still move the number).

    The pill greys only when its number stopped being trustworthy: polls
    failing AND the reading older than two cadences (never under 3 minutes),
    or a reset passed since it was read. One failed poll used to grey it on the
    spot, which made a minute-old, still-right number look broken ("caught
    with our pants down"). A quick retry, a 429 backoff, a poll after waking
    from sleep and a poll when work starts keep it from getting there.

    Gemini used to poll on its own 5-minute clock and GPT had no backoff at
    all. The last checks make sure every pill on the bar has a source in
    `usage_sources()` and therefore this policy, so a pill added later cannot
    quietly roll its own."""
    import pathlib
    import tempfile

    from PySide6.QtWidgets import QApplication

    from app import codex_usage as xu
    from app import usage_poll as up
    from app.session_store import SessionStore
    from app.widgets.codex_usage_badge import CodexUsageBadge
    from app.widgets.main_window import USAGE_TRACKER_KEYS, usage_sources
    from app.widgets.ornaments import UsagePillBadge
    from main import create_main_window

    QApplication.instance() or QApplication([])

    class L:
        def __init__(self, pct, resets_at=None):
            self.percent, self.resets_at = pct, resets_at

    check("usage-poll: the user's cadence, in ms",
          (up.POLL_MS, up.URGENT_POLL_MS, up.IDLE_POLL_MS, up.URGENT_PCT)
          == (90_000, 30_000, 360_000, 90.0))
    check("usage-poll: no agent working means the idle rate, whatever the %",
          up.cadence_ms([L(99)], active=False) == up.IDLE_POLL_MS)
    check("usage-poll: working below 90% polls every 90 s",
          up.cadence_ms([L(50)], active=True) == up.POLL_MS)
    check("usage-poll: working at 90% or more polls every 30 s",
          up.cadence_ms([L(90)], active=True) == up.URGENT_POLL_MS
          and up.cadence_ms([L(50), L(95)], active=True) == up.URGENT_POLL_MS)
    check("usage-poll: a spent window is the reset poll's job, not 30 s",
          up.cadence_ms([L(100)], active=True) == up.POLL_MS)
    check("usage-poll: stale_after is two cadences, never under 3 minutes",
          up.stale_after_s(up.IDLE_POLL_MS) == 720
          and up.stale_after_s(up.POLL_MS) == 180
          and up.stale_after_s(up.URGENT_POLL_MS) == 180)

    # --- provider_active: only this provider's agents, with a grace ---
    class A:
        def __init__(self, provider, busy=False, worked_ago=None):
            self.spec = type("S", (), {"provider": provider})()
            self._busy = busy
            self._at = 0.0 if worked_ago is None else time.time() - worked_ago

        def is_busy(self):
            return self._busy

        def last_work_at(self):
            return self._at

    check("usage-poll: a busy agent of the provider is activity",
          up.provider_active([A("claude", busy=True)], ("claude",)))
    check("usage-poll: another provider's busy agent is not",
          not up.provider_active([A("gemini", busy=True)], ("claude",)))
    check("usage-poll: work inside the grace still counts",
          up.provider_active([A("openai", worked_ago=60)], ("openai",)))
    check("usage-poll: work older than the grace does not",
          not up.provider_active(
              [A("openai", worked_ago=up.ACTIVE_GRACE_S + 5)], ("openai",)))

    # --- a poller on a fake clock, driven without threads or network ---
    clock = [time.time()]
    active = [False]
    fetched = []

    def make(fetch=lambda: None):
        pill = CodexUsageBadge()
        src = up.UsageSource(name="t", fetch=fetch, agent_providers=("openai",),
                             tracker_keys=("codex_five_hour",))
        poller = up.UsagePoller(src, pills=lambda: [pill],
                                active=lambda: active[0], wanted=lambda: True,
                                clock=lambda: clock[0])
        poller._running = True          # as start() leaves it, minus the fetch
        return poller, pill

    def good(pct=40.0, age=0.0, resets_in=4 * 3600):
        return xu.CodexUsage(limit=xu.CodexLimit(pct, clock[0] + resets_in),
                             fetched_at=clock[0] - age)

    def gap():
        return round(poller.next_poll_at() - clock[0])

    poller, pill = make()
    poller.deliver(good())
    check("usage-poll: a reading schedules the next poll one cadence out",
          gap() == up.IDLE_POLL_MS // 1000, gap())
    check("usage-poll: the pill learns the idle stale_after",
          pill._stale_after == up.stale_after_s(up.IDLE_POLL_MS))

    poller.deliver(xu.CodexUsage(error="urlerror"))
    check("usage-poll: a transient failure retries in 10 s, not 6 minutes",
          gap() == up.RETRY_MS // 1000, gap())
    check("usage-poll: one failure leaves the pill in colour",
          pill.has_reading() and not pill.is_stale())
    check("usage-poll: the tooltip says what failed and when it retries",
          "urlerror" in pill.toolTip() and "Next try in" in pill.toolTip(),
          pill.toolTip())
    poller.deliver(xu.CodexUsage(error="urlerror"))
    check("usage-poll: the retry failing too falls back to the cadence",
          gap() == up.IDLE_POLL_MS // 1000, gap())

    poller.deliver(good())
    poller.deliver(xu.CodexUsage(error="http 429"))
    check("usage-poll: a 429 never quick-retries; it backs off",
          poller.backoff() == 1 and gap() == 2 * up.IDLE_POLL_MS // 1000, gap())
    for _ in range(4):
        poller.deliver(xu.CodexUsage(error="http 429"))
    check("usage-poll: the backoff is capped at 16 minutes",
          poller.interval_ms() == up.BACKOFF_CAP_MS, poller.interval_ms())
    poller._inflight = True             # keep refresh() from spawning a fetch
    poller.refresh()
    poller._inflight = False
    check("usage-poll: a click on the pill clears the backoff",
          poller.backoff() == 0 and gap() == up.IDLE_POLL_MS // 1000, gap())

    # stale by age, not by one failure
    poller.deliver(good(age=up.stale_after_s(up.IDLE_POLL_MS) + 60))
    check("usage-poll: an old reading with healthy polls is not stale",
          not pill.is_stale())
    poller.deliver(xu.CodexUsage(error="timeouterror"))
    check("usage-poll: failing polls on a reading past stale_after grey it",
          pill.is_stale())
    poller.deliver(good())
    check("usage-poll: a good reading clears the failure and the grey",
          not pill.is_stale() and "timeouterror" not in pill.toolTip())

    # work starting after an idle stretch polls at once when overdue
    poller.tick()
    clock[0] += 60
    poller.tick()
    check("usage-poll: idle and a minute old, nothing is due yet",
          gap() == up.IDLE_POLL_MS // 1000 - 60, gap())
    active[0] = True
    clock[0] += 40
    poller.tick()
    check("usage-poll: work starting makes a 100 s old reading due now",
          gap() <= 0, gap())
    poller.deliver(good())
    check("usage-poll: working, the cadence is 90 s",
          gap() == up.POLL_MS // 1000, gap())
    check("usage-poll: ...and the pill's stale_after follows it",
          pill._stale_after == up.stale_after_s(up.POLL_MS))

    # waking from sleep
    poller.tick()
    clock[0] += 2 * 3600
    poller.tick()
    check("usage-poll: a wall-clock jump (sleep) polls 5 s later",
          gap() == up.WAKE_DELAY_MS // 1000, gap())
    poller.tick()                       # the next tick must not undo that
    check("usage-poll: the retune leaves the wake poll alone",
          gap() == up.WAKE_DELAY_MS // 1000, gap())

    # a poll just after the reset when it lands before the next poll
    poller.deliver(good(resets_in=60))
    check("usage-poll: a reset inside the cadence arms a poll just after it",
          poller.reset_poll_armed()
          and 60_000 < poller.reset_poll_remaining_ms() <= 60_000 + up.RESET_GRACE_MS)
    poller.deliver(good(resets_in=3 * 3600))
    check("usage-poll: a reset beyond the next poll arms nothing extra",
          not poller.reset_poll_armed())
    poller.deliver(good(pct=100.0, resets_in=3 * 3600))
    check("usage-poll: a spent window arms its reset poll hours out",
          poller.reset_poll_armed())
    poller.stop()
    active[0] = False

    # no login and nothing ever read: the readout goes away and polling stops
    fresh, fresh_pill = make()
    gone = []
    fresh.absent.connect(lambda: gone.append(1))
    fresh.deliver(xu.CodexUsage(error="no-auth"))
    check("usage-poll: no-auth with no reading clears the pill and stops",
          not fresh.is_running() and not fresh_pill.has_content() and gone == [1])
    other, other_pill = make()
    other.deliver(xu.CodexUsage(error="urlerror"))
    check("usage-poll: a failure with no reading shows the can't-read pill",
          other_pill.has_content() and not other_pill.has_reading()
          and other.is_running())
    other.stop()

    # --- every pill on the bar is on this policy ---
    covered = sorted(k for src in usage_sources() for k in src.tracker_keys)
    check("usage-poll: every tracker key has exactly one source",
          covered == sorted(USAGE_TRACKER_KEYS), covered)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ai-hive-usagepoll-"))
    win = create_main_window(SessionStore(path=tmp / "s.json"))
    for key in USAGE_TRACKER_KEYS:
        poller = win._poller_for(key)
        pills = poller.source.tracker_keys if poller else ()
        check(f"usage-poll: the {key} pill is fed by a shared poller",
              poller is not None and key in pills
              and isinstance(win.top_bar._usage_pills[key], UsagePillBadge))
    check("usage-poll: Gemini and GPT get the same idle cadence as Claude",
          {p.cadence_ms() for p in win._usage_pollers.values()}
          == {up.IDLE_POLL_MS})
    check("usage-poll: GPT pills follow OpenAI agents, Gemini follow Gemini",
          win.usage_poller("codex").source.agent_providers == ("openai",)
          and win.usage_poller("gemini").source.agent_providers == ("gemini",))
    win.close()


def test_gemini_usage_reads_both_cli_output_shapes():
    """The quota table parses whether the CLI printed it to a pipe or a terminal.

    Both samples below are real captured output. Down a pipe agy separates its
    columns with TABS; on a terminal it pretty-prints, padding columns with runs
    of spaces, adding a "Quota:" heading, ending lines with CRLF and wrapping
    the lot in escape sequences.

    The terminal shape is not hypothetical, and the reason it is guarded is a
    MEASURED DEAD END worth not repeating. The usage read briefly ran under a
    pseudo-console, to stop Windows giving the child a console of its own that
    Windows 11 could hand to the default terminal app (the window that flashes
    over the desktop). It fixed nothing: agy starts a NESTED HELPER that asks
    for its own console two levels down, where creation flags cannot reach --
    and pywinpty needs a console to build a pty from, which a pythonw app does
    not have, so every poll had Windows allocate one FOR AI HIVE (44 leaked
    conhost children in one session, and that allocation's own window was caught
    being shown). See `gemini_usage._read_usage`."""
    import shutil

    from app import gemini_usage

    piped = ("Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-25T10:58:17Z\n"
             "Gemini Models\tFive Hour Limit Remaining\t42%\t2026-08-20T16:52:27Z\n"
             "Claude and GPT models\tWeekly Limit Remaining\t100%\t"
             "2026-08-27T11:52:27Z\n")
    terminal = gemini_usage._ESCAPES.sub("", (
        "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001hQuota:\r\n"
        "Gemini Models          Weekly Limit Remaining     100%  "
        "2026-08-25T10:58:17Z\r\n"
        "Gemini Models          Five Hour Limit Remaining   42%  "
        "2026-08-20T16:52:27Z\r\n"
        "Claude and GPT models  Weekly Limit Remaining     100%  "
        "2026-08-27T11:52:27Z\r\n")).replace("\r\n", "\n")

    real_read, real_which = gemini_usage._read_usage, shutil.which
    shutil.which = lambda name: "agy.exe"
    try:
        for label, text in (("piped", piped), ("terminal", terminal)):
            gemini_usage._read_usage = lambda exe, t, _t=text: _t
            usage = gemini_usage.fetch_cli()
            five = next((l for l in (usage.limits if usage else ())
                         if l.key == "five_hour"), None)
            week = next((l for l in (usage.limits if usage else ())
                         if l.key == "seven_day"), None)
            check("gemini-shape: " + label + " output reads 58% of the 5h window used",
                  five is not None and abs(five.percent - 58.0) < 0.01, five)
            check("gemini-shape: " + label + " output reads the weekly window too",
                  week is not None and abs(week.percent - 0.0) < 0.01, week)
    finally:
        gemini_usage._read_usage, shutil.which = real_read, real_which


def test_usage_pill_geometry_and_close():
    """Every usage pill is sized by ONE formula, and carries a hover X.

    The Gemini pills used to be pinned to a hardcoded 315px and elide into it,
    which reserved 630px of the bar for two readouts whose real content is
    ~215px, truncated anything longer, and disagreed with the Claude pill
    sitting immediately beside them. Width is now the text's width, measured
    identically for every pill, which is what these checks pin down.

    The X is a child button rather than a rect hit-tested in mousePressEvent,
    so closing can never be mistaken for the click-to-refresh affordance - and
    its width is reserved even while it is hidden, because growing the pill on
    hover would shove the whole right-hand cluster of the bar sideways.
    """
    import os
    import tempfile
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor, QFont, QFontMetrics
    from app.widgets.gemini_usage_badge import GeminiUsageBadge
    from app.widgets.ornaments import PlanUsageBadge, UsagePillBadge
    from app import gemini_usage

    QApplication.instance() or QApplication([])
    badge = GeminiUsageBadge(window="five_hour")
    weekly_badge = GeminiUsageBadge(window="weekly")
    plan_badge = PlanUsageBadge(window="five_hour")

    check("usage-pill: no content before a reading arrives",
          badge.has_content() is False)
    check("usage-pill: the loading state IS content, so the bar is never blank",
          (badge.mark_loading(), badge.has_content() is True)[-1])
    check("usage-pill: the loading line names its own tracker",
          "5h" in badge._text and "reading" in badge._text
          and "7d" in (weekly_badge.mark_loading(), weekly_badge._text)[-1])
    check("usage-pill: the loading pill paints with no number to draw",
          not badge.grab().isNull())

    lim_five_hour = gemini_usage.GeminiLimit(
        key="five_hour", label="Five Hour Limit (5h)", short="5h",
        percent=13.0, resets_at=time.time() + 3600.0)
    lim_weekly = gemini_usage.GeminiLimit(
        key="seven_day", label="Weekly Limit (all models)", short="7d",
        percent=2.5, resets_at=time.time() + 86400.0)

    reading = gemini_usage.GeminiUsage(limits=(lim_five_hour, lim_weekly))
    badge.set_usage(reading)
    weekly_badge.set_usage(reading)

    check("usage-pill: 5h badge shows the 5h limit text",
          "5h Gemini 13%" in badge._text)
    check("usage-pill: weekly badge shows the 7d limit text",
          "7d Gemini 2%" in weekly_badge._text)
    check("usage-pill: has_content is True once a reading is in",
          badge.has_content() is True)

    # the formula, and that BOTH classes use the same one. Bound to the
    # instance (the paint device `paintEvent` itself uses), same reason
    # `_measure_width` is: an unbound QFontMetrics(font) resolves against the
    # primary screen and can disagree with what actually gets painted.
    def want(instance, text):
        fm = QFontMetrics(instance._text_font(), instance)
        return instance._chrome_width() + fm.horizontalAdvance(text)

    check("usage-pill: width is ring + pads + text, and nothing else",
          badge.width() == want(badge, badge._text))
    check("usage-pill: the X reserves NO width - it floats over the text",
          badge.width()
          == badge._PAD * 2 + badge._RING + badge._GAP + badge._TEXT_SLACK
          + QFontMetrics(badge._text_font(), badge).horizontalAdvance(
              badge._text))
    # ONE formula, not a per-subclass copy. The two pills are given the same
    # font first, deliberately: a measurement follows the pill's OWN font now
    # (the fix for pills that measured one face and painted another), so two
    # widgets in different polish states measuring differently is correct
    # behaviour and would make this a test of nothing.
    same_font = QFont(plan_badge.font())
    plan_badge.setFont(same_font)
    badge.setFont(same_font)
    check("usage-pill: Claude and Gemini measure an identical string alike",
          plan_badge._measure_width("21% used, resets in 1h20m at 14:49")
          == badge._measure_width("21% used, resets in 1h20m at 14:49")
          and GeminiUsageBadge._measure_width is UsagePillBadge._measure_width
          and PlanUsageBadge._measure_width is UsagePillBadge._measure_width)
    check("usage-pill: the full text fits, so nothing is ever truncated",
          badge.width() - (badge._PAD + badge._RING + badge._GAP) - badge._PAD
          >= QFontMetrics(badge._text_font(), badge).horizontalAdvance(
              badge._text))
    check("usage-pill: the X sits inside the text's own run",
          badge.close_btn.x() < badge.width() - badge._PAD
          and badge.close_btn.x() + badge._CLOSE_W
          <= badge.width() - badge._PAD + 1)

    # the hover X (isVisibleTo, not isVisible: these badges are never shown,
    # so a real isVisible() would be False either way and prove nothing)
    check("usage-pill: the X is hidden until the pill is hovered",
          not badge.close_btn.isVisibleTo(badge))
    w_before = badge.width()
    badge.enterEvent(None)
    check("usage-pill: hovering reveals the X",
          badge.close_btn.isVisibleTo(badge))
    check("usage-pill: hovering does NOT change the width",
          badge.width() == w_before)
    check("usage-pill: the hovered pill paints its fade under the X",
          badge._hovering and not badge.grab().isNull())
    badge.leaveEvent(None)
    check("usage-pill: leaving hides the X again",
          not badge.close_btn.isVisibleTo(badge))

    closed, refreshed = [], []
    badge.closeRequested.connect(lambda: closed.append(1))
    badge.refreshRequested.connect(lambda: refreshed.append(1))
    badge.close_btn.click()
    check("usage-pill: the X closes, and does NOT trigger a refresh",
          closed == [1] and refreshed == [])

    col = badge._color()
    check("usage-pill: _color returns a QColor for a live reading",
          isinstance(col, QColor))

    # a can't-read pill is content too, on the Gemini side as well as Claude's
    blank = GeminiUsageBadge(window="five_hour")
    blank.mark_unreadable("no-data")
    check("usage-pill: an unreadable Gemini pill says so and offers a refresh",
          blank.has_content() and not blank.has_reading()
          and "click to refresh" in blank._text
          and not blank.grab().isNull())
    blank.deleteLater()

    check("usage-pill: the shared base owns the geometry",
          issubclass(GeminiUsageBadge, UsagePillBadge)
          and issubclass(PlanUsageBadge, UsagePillBadge))

    real_cli = gemini_usage.fetch_cli
    with tempfile.TemporaryDirectory() as tmpdir:
        old_env = os.environ.get("GEMINI_CONFIG_DIR")
        try:
            os.environ["GEMINI_CONFIG_DIR"] = tmpdir
            gemini_usage.fetch_cli = lambda *a, **k: None
            failed = gemini_usage.fetch()
            check("gemini-usage: a failed CLI read reports no-data, never raises",
                  failed.error == "no-data" and failed.limits == ()
                  and not failed.ok)
            check("gemini-usage: a fetch writes nothing to disk",
                  list(Path(tmpdir).iterdir()) == [])
        finally:
            gemini_usage.fetch_cli = real_cli
            if old_env is None:
                os.environ.pop("GEMINI_CONFIG_DIR", None)
            else:
                os.environ["GEMINI_CONFIG_DIR"] = old_env

    badge.deleteLater()
    weekly_badge.deleteLater()


def test_usage_pill_never_truncates():
    """A pill is as wide as the text it PAINTS, whatever the font turns out
    to be, and it repairs itself if it ever is not.

    Reported live, twice, with screenshots: pills reading "5h 86% used,
    resets n..." and "5h 0% us..." in a bar with hundreds of free pixels. The
    mechanism was a font the measurement never saw. `_text_font` used to
    return a bare `QFont()`, whose family is UNSET, and the two consumers of
    that font resolve an unset family differently: `QFontMetrics` falls back
    to the application font, while `QPainter.setFont` resolves it against the
    widget's own (whatever QSS put there). They agree only while the chrome
    family IS the application default - and `setup_application` deliberately
    prefers "Inter" whenever it is installed, so on such a machine every pill
    measured one face and painted a wider one, and `paintEvent`'s elide (a
    safety net, never meant to fire) truncated the reading.

    Two independent guarantees are checked here: the measurement now follows
    the widget's font, and a pill that finds itself too narrow at paint time
    widens itself using THE PAINTER'S OWN metrics - which is what makes the
    repair work even when the measurement is the thing that is wrong.
    """
    from PySide6.QtCore import QTimer, QEventLoop
    from PySide6.QtGui import QFont, QFontMetrics, QPainter
    from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
    from app import claude_usage
    from app.widgets.ornaments import PlanUsageBadge

    app = QApplication.instance() or QApplication([])

    def spin(ms=60):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def painted_advance(pill):
        """What the pill's own painting metrics say the line needs."""
        return QFontMetrics(pill._text_font(), pill).horizontalAdvance(
            pill._text)

    def text_room(pill):
        return pill.width() - (pill._PAD + pill._RING + pill._GAP) - pill._PAD

    host = QWidget()
    lay = QVBoxLayout(host)
    pill = PlanUsageBadge(host, window="five_hour")
    lay.addWidget(pill)
    host.resize(900, 60)
    host.show()
    pill.set_usage(claude_usage.Usage(
        limits=(claude_usage.Limit(key="five_hour", label="5h", short="5h",
                                   percent=86.0,
                                   resets_at=time.time() + 4800),),
        fetched_at=time.time(), plan="max"))
    spin()

    check("usage-pill: the text font follows the widget's own family",
          pill._text_font().family() == pill.font().family()
          and pill._text_font().pixelSize() == pill._TEXT_PX)
    check("usage-pill: a fresh reading fits with room to spare",
          text_room(pill) >= painted_advance(pill))

    # a font swapped under a line that has NOT changed: `_set_text` would
    # early-return, so only `changeEvent` can keep the width honest
    # Compared against what the pill PAINTS with, not the font asked for: an
    # app stylesheet an earlier test left behind can override setFont, and
    # then the honest outcome is that nothing moves.
    def follows(width_before, adv_before):
        adv = painted_advance(pill)
        moved = pill.width() - width_before
        return (text_room(pill) >= adv
                and (moved > 0) == (adv > adv_before)
                and (moved < 0) == (adv < adv_before))

    before, adv_before = pill.width(), painted_advance(pill)
    wide = QFont("Courier New")
    wide.setPixelSize(13)
    pill.setFont(wide)
    spin()
    widened, adv_wide = pill.width(), painted_advance(pill)
    check("usage-pill: a font change re-measures the pill",
          follows(before, adv_before), (before, widened, adv_before, adv_wide))

    # ...and the same the other way, so a narrower face gives the bar its
    # pixels back rather than leaving a padded pill behind. Measured against
    # the WIDE width, not the original: what a bare QFont() resolves to
    # depends on the widget's polish state, which the suite's earlier tests
    # can have moved - the invariant here is that the pill follows its font
    # in both directions, not what the default face happens to be.
    narrow = QFont("Segoe UI")
    narrow.setPixelSize(11)
    pill.setFont(narrow)
    spin()
    check("usage-pill: a narrower font shrinks the pill back",
          follows(widened, adv_wide), (widened, pill.width(), adv_wide,
                                       painted_advance(pill)))

    # THE MEASUREMENT ITSELF IS WRONG: the exact shape of the reported bug.
    # Re-running it would repeat the error, so the repair has to come from
    # the painter's metrics.
    class Undersizing(PlanUsageBadge):
        def _measure_width(self, text):
            return int(super()._measure_width(text) * 0.62)

    broken = Undersizing(host, window="five_hour")
    lay.addWidget(broken)
    broken.set_usage(claude_usage.Usage(
        limits=(claude_usage.Limit(key="five_hour", label="5h", short="5h",
                                   percent=86.0,
                                   resets_at=time.time() + 4800),),
        fetched_at=time.time(), plan="max"))
    check("usage-pill: a broken measurement starts out too narrow",
          text_room(broken) < painted_advance(broken))
    broken.grab()      # one paint is all the repair needs
    spin()
    healed = broken.width()
    check("usage-pill: painting too narrow widens the pill instead of eliding",
          text_room(broken) >= painted_advance(broken))
    broken.grab()
    spin()
    check("usage-pill: the repair settles - it never oscillates",
          broken.width() == healed)

    # and the repair is bounded: a pill that cannot be helped asks once
    calls = []
    real = broken._reassert_width
    broken._reassert_width = lambda: calls.append(1) or real()
    for _ in range(3):
        broken.grab()
        spin()
    check("usage-pill: a settled pill asks for no further repair",
          calls == [])

    broken.deleteLater()
    pill.deleteLater()
    host.deleteLater()


def test_usage_pill_provider_inks():
    """Each usage pill wears its agent's colour and turns red at 85%.

    Claude terracotta, Gemini blue, GPT in the running-head's title ink, so
    three agents' pills tell apart at a glance and only the one near its
    limit changes. The pills sit on the TOP BAR, which is vellum on the light
    skin, so the inks are checked against every skin's bg_panel: the lifted
    vendor inks and the near-white title ink both vanish on vellum.
    """
    import time as _time
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QColor
    from app import ui_theme, claude_usage as cu, gemini_usage as gu
    from app import codex_usage as xu
    from app.widgets.ornaments import PlanUsageBadge
    from app.widgets.gemini_usage_badge import GeminiUsageBadge
    from app.widgets.codex_usage_badge import CodexUsageBadge
    from app.widgets.terminal_view import contrast_ratio

    QApplication.instance() or QApplication([])
    reset = _time.time() + 4800

    def claude(pct):
        b = PlanUsageBadge(window="five_hour")
        b.set_usage(cu.Usage(limits=(cu.Limit("five_hour", "Session (5h)",
                                              "5h", pct, reset),)))
        return b

    def gemini(pct):
        b = GeminiUsageBadge(window="five_hour")
        b.set_usage(gu.GeminiUsage(limits=(gu.GeminiLimit(
            "five_hour", "5-hour", "5h", pct, reset),)))
        return b

    def gpt(pct):
        b = CodexUsageBadge()
        b.set_usage(xu.CodexUsage(limit=xu.CodexLimit(pct, reset)))
        return b

    check("usage-ink: the Claude pill reads like the Gemini pill",
          claude(21.0)._text.startswith("5h Claude 21% used, resets in")
          and gemini(18.0)._text.startswith("5h Gemini 18% used, resets in"),
          (claude(21.0)._text, gemini(18.0)._text))
    check("usage-ink: the Codex pill is labelled GPT",
          gpt(30.0)._text.startswith("GPT 30% used, resets in")
          and "ChatGPT" not in gpt(30.0)._text, gpt(30.0)._text)

    was = ui_theme.ACTIVE_THEME.id
    fails, reds, inks_ok = [], [], True
    try:
        for tid, t in ui_theme.THEMES.items():
            ui_theme.apply_theme(tid)
            want = {"claude": ui_theme.usage_pill_ink("claude"),
                    "gemini": ui_theme.usage_pill_ink("gemini"),
                    "codex": ui_theme.usage_pill_ink("codex")}
            if not t.light:
                inks_ok &= (want["claude"] == ui_theme.PROVIDER_INK["claude"]
                            and want["gemini"] == ui_theme.PROVIDER_INK["gemini"]
                            and want["codex"] == t.cardhead_fg)
            for key, make in (("claude", claude), ("gemini", gemini),
                              ("codex", gpt)):
                low, edge, high = make(84.0), make(85.0), make(97.0)
                if low._color().name() != QColor(want[key]).name():
                    fails.append(f"{tid}:{key} at 84%={low._color().name()}")
                if (edge._color().name() != QColor(t.red).name()
                        or high._color().name() != QColor(t.red).name()):
                    reds.append(f"{tid}:{key}")
                r = contrast_ratio(QColor(want[key]), QColor(t.bg_panel))
                if r < 4.5:
                    fails.append(f"{tid}:{key} contrast {r:.2f}")
                for b in (low, edge, high):
                    b.deleteLater()
    finally:
        ui_theme.apply_theme(was)
    check("usage-ink: under 85% each pill wears its agent's ink, readable on "
          "every skin's top bar", not fails, fails)
    check("usage-ink: dark skins use the card chip's vendor inks and the "
          "running-head title ink for GPT", inks_ok)
    check("usage-ink: at 85% and above every pill turns the skin's red",
          not reds, reds)
    check("usage-ink: the three agents' resting inks differ on every skin",
          all(len({ui_theme.apply_theme(tid) and None,
                   ui_theme.usage_pill_ink("claude"),
                   ui_theme.usage_pill_ink("gemini"),
                   ui_theme.usage_pill_ink("codex")} - {None}) == 3
              for tid in ui_theme.THEMES))
    ui_theme.apply_theme(was)


def test_codex_summary_reads_first_real_prompt():
    """A Codex card's summary is the conversation's first real user prompt,
    even after the rollout grows past the tail window the live usage is read
    from. Reading only the tail made a later prompt look like the summary,
    and Codex's injected <environment_context> turn must never be the summary."""
    import json as _json
    from app import transcripts

    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-codex-"))
    path = tmp / "rollout-2026-01-01T00-00-00-test-sid.jsonl"

    def _rec(kind, payload):
        return _json.dumps({"timestamp": "2026-01-01T00:00:00Z",
                            "type": kind, "payload": payload})

    lines = [
        _rec("session_meta", {"id": "test-sid", "cwd": SCRATCH_CWD}),
        _rec("event_msg", {"type": "user_message", "message":
                           "<environment_context>cwd</environment_context>"}),
        _rec("event_msg", {"type": "user_message",
                           "message": "Fix the header summary"}),
    ]
    filler = _rec("event_msg", {"type": "agent_message", "message": "x" * 1000})
    lines += [filler] * 400   # pushes the first prompt out of the tail window
    lines += [
        _rec("event_msg", {"type": "user_message", "message": "A later prompt"}),
        _rec("turn_context", {"model": "gpt-5", "effort": "high",
                              "approval_policy": "on-request"}),
        _rec("event_msg", {"type": "token_count", "info": {
            "last_token_usage": {"total_tokens": 1234},
            "model_context_window": 200000}}),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    check("codex summary: test rollout is larger than the head window",
          path.stat().st_size > 262144 + 131072, path.stat().st_size)

    transcripts._CODEX_PATHS["test-sid"] = str(path)
    try:
        sid, model, effort, mode, used, window, summary = \
            transcripts.latest_codex_state(SCRATCH_CWD, "test-sid", 0)
    finally:
        transcripts._CODEX_PATHS.pop("test-sid", None)
        transcripts._CODEX_CACHE.pop(str(path), None)
    check("codex summary: first real prompt, not a later one or env context",
          summary == "Fix the header summary", summary)
    check("codex summary: live usage still comes from the tail",
          used == 1234 and window == 200000 and effort == "high",
          (used, window, effort))
