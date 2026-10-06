"""Updating the AI CLIs (startup gate, native install migration) and AI
Hive itself."""

import os
import shutil
import tempfile
import time
from pathlib import Path

from .harness import ROOT, check


def test_self_update():
    """Updating AI Hive from GitHub main (app/self_update.py + the version
    button). Every git/pip call goes through a scripted fake runner against a
    temp folder: the suite runs FROM this clone and must never fetch or merge
    into it."""
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QApplication
    from app import __version__, self_update
    from app.self_update import Status

    app = QApplication.instance() or QApplication([])

    def pump(ms):
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def wait_until(pred, timeout_ms=5000):
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if pred():
                return True
            pump(20)
        return pred()

    # --- the repo's own CHANGELOG.md -----------------------------------------
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = self_update.parse_changelog(changelog)
    check("self-update: CHANGELOG.md's newest section is the current "
          "__version__ (bump both together)",
          bool(sections) and sections[0][0] == __version__,
          sections[0][0] if sections else "no sections")
    check("self-update: CHANGELOG.md sections run newest first",
          [s[0] for s in sections] == sorted(
              (s[0] for s in sections), key=self_update.version_tuple,
              reverse=True), [s[0] for s in sections])
    check("self-update: CHANGELOG.md has no em dash (the app shows it)",
          "—" not in changelog)
    check("self-update: the __version__ line parses from source",
          self_update.version_from_source(
              (ROOT / "app" / "__init__.py").read_text(encoding="utf-8"))
          == __version__)

    # --- notes_between -------------------------------------------------------
    log = ("# Changelog\n\npreamble\n\n## 0.4.0\n- four\n\n## v0.3.1 (fix)\n"
           "- three one\n\n## 0.3.0\n- three\n\n## 0.2.0\n- two\n")
    notes = self_update.notes_between(log, "0.3.0", "0.4.0")
    check("self-update: notes cover every version in (installed, remote], "
          "newest first",
          "four" in notes and "three one" in notes
          and "### v0.3.0" not in notes and "two" not in notes
          and notes.index("v0.4.0") < notes.index("v0.3.1"), notes)
    check("self-update: notes skip the preamble and accept a v prefix",
          "preamble" not in notes and "### v0.3.1" in notes)
    check("self-update: no matching section gives no notes",
          self_update.notes_between(log, "0.4.0", "0.5.0") == ""
          and self_update.notes_between("", "0.1.0", "0.2.0") == ""
          and self_update.notes_between(log, "junk", "0.4.0") == "")

    # --- a scripted git ------------------------------------------------------
    def make_repo(installed, changelog_text=log):
        d = Path(tempfile.mkdtemp(prefix="ai-hive-selfupd-"))
        (d / "app").mkdir()
        (d / "app" / "__init__.py").write_text(
            f'__version__ = "{installed}"\n', encoding="utf-8")
        (d / "CHANGELOG.md").write_text(changelog_text, encoding="utf-8")
        return d

    class FakeGit:
        def __init__(self, repo, remote="0.4.0", branch="main", dirty="",
                     ancestor=0, fetch=(0, ""), remote_log=log,
                     merge_rc=0, deps_changed=False, pip_rc=0, delay=0.0):
            self.repo, self.remote, self.branch = repo, remote, branch
            self.dirty, self.ancestor, self.fetch = dirty, ancestor, fetch
            self.remote_log, self.merge_rc = remote_log, merge_rc
            self.deps_changed, self.pip_rc = deps_changed, pip_rc
            self.delay = delay
            self.calls = []

        def __call__(self, argv, timeout=0):
            self.calls.append(list(argv))
            if self.delay:
                time.sleep(self.delay)
            if argv[0] != "git":
                return (self.pip_rc, "pip output")
            cmd = argv[3:]
            if cmd[0] == "fetch":
                return self.fetch
            if cmd[0] == "show" and cmd[1].endswith(":app/__init__.py"):
                return (0, f'__version__ = "{self.remote}"\n')
            if cmd[0] == "show" and cmd[1].endswith(":CHANGELOG.md"):
                return ((0, self.remote_log) if self.remote_log is not None
                        else (128, "fatal: path does not exist"))
            if cmd[0] == "log":
                return (0, "feat: a\nfix: b\n")
            if cmd[:2] == ["rev-parse", "--abbrev-ref"]:
                return (0, self.branch + "\n")
            if cmd[0] == "rev-parse":
                return (0, "abc123\n")
            if cmd[0] == "status":
                return (0, self.dirty)
            if cmd[0] == "merge-base":
                return (self.ancestor, "")
            if cmd[0] == "merge":
                if self.merge_rc == 0:
                    (Path(self.repo) / "app" / "__init__.py").write_text(
                        f'__version__ = "{self.remote}"\n', encoding="utf-8")
                    return (0, "")
                return (self.merge_rc, "error: Your local changes would be "
                                       "overwritten by merge")
            if cmd[0] == "diff":
                return (1 if self.deps_changed else 0, "")
            return (0, "")

        def writes(self):
            return [c for c in self.calls if c[0] != "git" or c[3] in (
                "merge", "reset", "stash", "checkout", "pull", "rebase",
                "clean", "switch")]

    repo = make_repo("0.3.0")
    git = FakeGit(str(repo))
    c = self_update.check(git, str(repo), running="0.3.0")
    check("self-update: a higher version on main is offered, with its notes",
          c.status is Status.UPDATE_AVAILABLE and c.remote == "0.4.0"
          and c.installed == "0.3.0" and "four" in c.notes and not c.blocked,
          c)
    check("self-update: checking never writes to the folder",
          git.writes() == [], git.writes())
    check("self-update: every git call is pinned to the repo folder",
          all(cl[:3] == ["git", "-C", str(repo)] for cl in git.calls))

    same = self_update.check(FakeGit(str(repo), remote="0.3.0"), str(repo),
                             running="0.3.0")
    ahead = self_update.check(FakeGit(str(repo), remote="0.2.0"), str(repo),
                              running="0.3.0")
    check("self-update: the same version, or a local clone ahead of main, is "
          "up to date (never an update backwards)",
          same.status is Status.UP_TO_DATE
          and ahead.status is Status.UP_TO_DATE, (same.status, ahead.status))

    def fetch_error(rc, out):
        return self_update.check(FakeGit(str(repo), fetch=(rc, out)),
                                 str(repo), running="0.3.0")

    failed = fetch_error(128, "fatal: unable to access 'https://github.com/'")
    slow = fetch_error(self_update.RC_TIMEOUT, "")
    nogit = fetch_error(127, "")
    check("self-update: a failed fetch is an error that says why",
          failed.status is Status.ERROR and "unable to access" in failed.detail
          and "too long" in slow.detail and "Git was not found" in nogit.detail,
          (failed.detail, slow.detail, nogit.detail))

    pulled = make_repo("0.4.0")
    offline = self_update.check(FakeGit(str(pulled), fetch=(128, "offline")),
                                str(pulled), running="0.3.0")
    online = self_update.check(FakeGit(str(pulled)), str(pulled),
                               running="0.3.0")
    check("self-update: files newer than the running app read as restart "
          "pending, even offline, with the notes from disk",
          offline.status is Status.RESTART_PENDING
          and online.status is Status.RESTART_PENDING
          and "four" in offline.notes and "two" not in offline.notes,
          (offline, online.status))

    fallback = self_update.check(FakeGit(str(repo), remote_log=None),
                                 str(repo), running="0.3.0")
    check("self-update: without a changelog section the commit subjects "
          "stand in, so the notes are never blank",
          "- feat: a" in fallback.notes, fallback.notes)

    def blocked(**kw):
        return self_update.check(FakeGit(str(repo), **kw), str(repo),
                                 running="0.3.0").blocked

    on_branch = blocked(branch="feature/x")
    dirty = blocked(dirty=" M app/widgets/main_window.py\n")
    diverged = blocked(ancestor=1)
    check("self-update: another branch, local changes and local commits "
          "each block the update, with a reason",
          "feature/x" in on_branch and "local changes" in dirty
          and "not on GitHub" in diverged, (on_branch, dirty, diverged))

    # --- apply ---------------------------------------------------------------
    all_calls = []
    for what, kw in (("on another branch", dict(branch="feature/x")),
                     ("with local changes", dict(dirty=" M main.py\n")),
                     ("with local commits", dict(ancestor=1))):
        g = FakeGit(str(make_repo("0.3.0")), **kw)
        r = self_update.apply(g, g.repo)
        all_calls += g.calls
        check(f"self-update: apply refuses and writes nothing {what}",
              not r.ok and r.detail and g.writes() == [], (r, g.writes()))

    g = FakeGit(str(make_repo("0.3.0")))
    r = self_update.apply(g, g.repo, python="py.exe")
    all_calls += g.calls
    merges = [c for c in g.calls if c[0] == "git" and c[3] == "merge"]
    check("self-update: apply is one fast-forward-only merge of origin/main",
          r.ok and merges == [["git", "-C", g.repo, "merge", "--ff-only",
                               "--quiet", "origin/main"]], (r, merges))
    check("self-update: apply reports the new version and asks for a restart",
          r.version == "0.4.0" and "Restart AI Hive" in r.detail
          and not r.deps_changed
          and not any(c[0] == "py.exe" for c in g.calls), r)

    g = FakeGit(str(make_repo("0.3.0")), deps_changed=True)
    r = self_update.apply(g, g.repo, python="py.exe")
    all_calls += g.calls
    pip = [c for c in g.calls if c[0] == "py.exe"]
    check("self-update: changed requirements are installed with pip",
          r.ok and r.deps_changed and r.deps_ok and len(pip) == 1
          and pip[0][1:4] == ["-m", "pip", "install"], pip)

    g = FakeGit(str(make_repo("0.3.0")), deps_changed=True, pip_rc=1)
    r = self_update.apply(g, g.repo, python="py.exe")
    all_calls += g.calls
    check("self-update: a failed pip keeps the update but says what to run",
          r.ok and not r.deps_ok
          and "pip install -r requirements.txt" in r.detail, r.detail)

    g = FakeGit(str(make_repo("0.3.0")), merge_rc=1)
    r = self_update.apply(g, g.repo)
    all_calls += g.calls
    check("self-update: a refused merge is reported, not retried another way",
          not r.ok and "overwritten" in r.detail, r.detail)
    risky = [c for c in all_calls if c[0] == "git" and c[3] in (
        "reset", "stash", "checkout", "clean", "switch", "rebase", "pull")]
    check("self-update: nothing ever resets, stashes or checks out",
          risky == [], risky)
    check("self-update: audit lines name the outcome",
          self_update.audit_line(r).startswith("SELF-UPDATE apply ok=False")
          and "status=update_available" in self_update.audit_line(c))

    # --- the button and the dialog -------------------------------------------
    from app.session_store import SessionStore
    from main import create_main_window, setup_application
    setup_application(app)
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-selfupd-mw-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win.show()
    pump(50)
    btn = win.top_bar.app_update_btn
    check("self-update: a check button sits beside the version badge",
          btn.isVisible() and btn.text() == ""
          and "GitHub" in btn.toolTip(), btn.text())
    bar_h = win.top_bar.height()
    check("self-update: the idle glyph is header-sized with clearance",
          bar_h - 12 <= btn.height() <= bar_h - 6
          and btn.width() >= btn.height(), (btn.size(), bar_h))
    glyph = btn.grab().toImage()
    inked = sum(1 for x in range(glyph.width()) for y in range(glyph.height())
                if glyph.pixelColor(x, y).alpha() > 0
                and glyph.pixelColor(x, y).lightness() > 80)
    check("self-update: the button paints the refresh glyph, not a font char",
          inked > 60, inked)
    ink_rows = [y for y in range(glyph.height())
                if any(glyph.pixelColor(x, y).alpha() > 0
                       and glyph.pixelColor(x, y).lightness() > 80
                       for x in range(glyph.width()))]
    ink_h = (ink_rows[-1] - ink_rows[0] + 1) if ink_rows else 0
    check("self-update: the refresh glyph is ~70% of the button, not full size",
          0 < ink_h <= 0.75 * btn.height(), (ink_h, btn.height()))
    btn.click()
    pump(50)
    check("self-update: unarmed (the suite, any test window) the button "
          "cannot reach git",
          getattr(win, "_app_update_job", None) is None
          and getattr(win, "_self_update_dialog", None) is None)

    audits = []
    win._audit_install = audits.append
    wrepo = make_repo(__version__)
    major, minor, patch = self_update.version_tuple(__version__)[:3]
    newer = f"{major}.{minor}.{patch + 1}"
    wlog = f"# Changelog\n\n## {newer}\n- the shiny thing\n"
    git = FakeGit(str(wrepo), remote=newer, remote_log=wlog, delay=0.05)
    win.arm_self_update(git, repo=str(wrepo))
    btn.click()
    check("self-update: the check runs off the GUI thread and says so",
          btn.text() == "Checking..." and not btn.isEnabled(), btn.text())
    wait_until(lambda: getattr(win, "_self_update_dialog", None) is not None)
    dlg = win._self_update_dialog
    check("self-update: an update lights the button and opens the notes",
          dlg is not None and btn.property("attention") is True
          and newer in btn.text() and btn.isEnabled()
          and "the shiny thing" in dlg.notes.toPlainText()
          and dlg.update_btn.isEnabled(), btn.text())
    check("self-update: the check was audited",
          any(a.startswith("SELF-UPDATE check") for a in audits), audits)
    dlg.update_btn.click()
    check("self-update: the dialog cannot be closed mid-update",
          dlg.update_btn.text() == "Updating..."
          and not dlg.close_btn.isEnabled())
    dlg.reject()
    check("self-update: ...even by Escape", dlg.isVisible())
    wait_until(lambda: dlg._result is not None)
    check("self-update: after updating, the user is told to restart and the "
          "window stays open",
          dlg._result is not None and dlg._result.ok
          and "Restart AI Hive" in dlg.body_label.text()
          and not dlg.update_btn.isVisible() and win.isVisible()
          and btn.text() == "Restart to update"
          and btn.property("attention") is True, dlg.body_label.text())
    dlg.reject()
    pump(30)
    check("self-update: closing the dialog forgets it",
          win._self_update_dialog is None)

    # the running process is still the old version, the files are new
    win.arm_self_update(FakeGit(str(wrepo), remote=newer, remote_log=wlog),
                        repo=str(wrepo))
    btn.click()
    wait_until(lambda: getattr(win, "_self_update_dialog", None) is not None)
    dlg = win._self_update_dialog
    check("self-update: checking again before a restart says restart, and "
          "offers no second update",
          dlg is not None and dlg._check.status is Status.RESTART_PENDING
          and not dlg.update_btn.isVisible()
          and btn.text() == "Restart to update", btn.text())
    if dlg is not None:
        dlg.reject()
    pump(30)

    blocked_repo = make_repo(__version__)
    win.arm_self_update(FakeGit(str(blocked_repo), remote=newer,
                                remote_log=wlog, branch="feature/wip"),
                        repo=str(blocked_repo))
    btn.click()
    wait_until(lambda: getattr(win, "_self_update_dialog", None) is not None)
    dlg = win._self_update_dialog
    check("self-update: a blocked update shows why and cannot be clicked",
          dlg is not None and not dlg.update_btn.isEnabled()
          and "feature/wip" in dlg.blocked_label.text()
          and not dlg.blocked_label.isHidden())
    if dlg is not None:
        dlg.reject()
    pump(30)

    current = make_repo(__version__)
    win.arm_self_update(FakeGit(str(current), remote=__version__),
                        repo=str(current))
    btn.click()
    wait_until(lambda: getattr(win, "_app_update_job", None) is None)
    flash = win._app_update_flash()
    check("self-update: up to date is a quiet flash on the button, no dialog",
          "Up to date" in btn.text() and win._self_update_dialog is None
          and flash.isActive(), btn.text())
    check("self-update: ...shown for 4 seconds",
          flash.interval() == 4000, flash.interval())
    check("self-update: ...and not interactive while it shows",
          not btn.isEnabled() and btn.toolTip() == ""
          and btn.cursor().shape() == Qt.CursorShape.ArrowCursor)
    btn.click()
    pump(30)
    check("self-update: ...a click on the notice starts no new check",
          getattr(win, "_app_update_job", None) is None
          and "Up to date" in btn.text() and flash.isActive(), btn.text())
    flash.timeout.emit()
    check("self-update: ...that returns to the clickable check glyph",
          btn.text() == "" and btn.property("attention") is False
          and btn.isEnabled() and "GitHub" in btn.toolTip()
          and btn.cursor().shape() == Qt.CursorShape.PointingHandCursor)

    win.arm_self_update(FakeGit(str(current), remote=__version__,
                                fetch=(1, "fatal: unable to access")),
                        repo=str(current))
    btn.click()
    wait_until(lambda: getattr(win, "_self_update_dialog", None) is not None)
    check("self-update: a failed check stays clickable (only up to date is "
          "a bare notice)",
          btn.text() == "Check failed" and btn.isEnabled()
          and flash.interval() == win.APP_UPDATE_FLASH_MS, btn.text())
    if win._self_update_dialog is not None:
        win._self_update_dialog.reject()
    pump(30)
    win.close()


class _FakeCli:
    """A recording `Runner` for the CLI-update gate. Every check below drives
    the real `run_gate` through one of these; NO test ever shells out, which is
    the same rule that keeps the suite off the network and off the user's real
    Claude account."""

    def __init__(self, versions=("2.1.224",), available="2.1.224", alive=0,
                 upgrade=(0, ""), update_out=(0, "already on the latest"),
                 timeout_on=(), paths=None, paths_rc=0):
        self.calls = []                 # every argv, in order
        self.versions = list(versions)  # one per `--version`, last repeats
        self.available = available
        self.alive = alive
        self.upgrade = upgrade
        self.update_out = update_out
        self.timeout_on = tuple(timeout_on)
        # what the path pass reports. None = every `alive` process IS the
        # target (the tests' exes are `C:\fake\<image name>`), which is the
        # shape every check written before the desktop-app collision assumed.
        self.paths = paths
        self.paths_rc = paths_rc

    def argvs(self):
        return [list(a) for a, _t in self.calls]

    def __call__(self, argv, timeout):
        argv = list(argv)
        self.calls.append((argv, timeout))
        joined = " ".join(argv).lower()
        if any(token in joined for token in self.timeout_on):
            from app import cli_update
            return cli_update.RC_TIMEOUT, ""
        if argv[0] == "tasklist":
            name = argv[2].split()[-1]
            if self.alive < 0:
                return 1, "ERROR"
            body = "\n".join(f"{name}   {1000 + i} Console  1  285,000 K"
                             for i in range(self.alive))
            return 0, body or ("INFO: No tasks are running which match the "
                               "specified criteria.")
        if argv[0] == "powershell":
            if self.paths_rc != 0:
                return self.paths_rc, "ERROR"
            name = joined.split("name='")[1].split("'")[0]
            lines = self.paths if self.paths is not None else \
                [rf"C:\fake\{name}"] * self.alive
            return 0, "\n".join(lines)
        if argv[-1] == "--version":
            out = self.versions[0]
            if len(self.versions) > 1:
                out = self.versions.pop(0)
            return 0, out
        if argv[0] == "winget" and argv[1] == "show":
            return 0, f"Found Claude Code [Anthropic.ClaudeCode]\n" \
                      f"Version: {self.available}\n"
        if argv[0] == "winget" and argv[1] == "upgrade":
            return self.upgrade
        return self.update_out          # `agy update`


def test_cli_auto_update():
    """The startup CLI auto-update gate.

    Root cause it exists for: `claude.exe` is a single self-contained binary
    and Windows cannot overwrite a running one, while every AI Hive agent IS a
    claude.exe child. An upgrade run with agents up cannot replace the file,
    but winget records the new version in its database anyway, after which the
    old binary nags forever and winget insists there is nothing to do. So the
    gate runs BEFORE any window or agent exists, it decides success by reading
    `--version` off the resolved binary rather than by believing the installer,
    and it refuses to run an upgrade at all while a target process is alive.

    Everything here is driven through an injected runner: the suite must never
    upgrade the user's CLI."""
    from PySide6.QtWidgets import QApplication
    from app import cli_update
    from app.cli_update import Status
    from app.session_store import SessionStore
    from app.widgets.main_window import TopBar
    from app.workspace_manager import SESSION_VERSION
    from main import create_main_window

    app = QApplication.instance() or QApplication([])

    claude = cli_update.Target(
        key="claude", label="Claude Code", exe=r"C:\fake\claude.exe",
        process_names=("claude.exe",), winget_id="Anthropic.ClaudeCode")
    agy = cli_update.Target(
        key="gemini", label="Gemini (agy)", exe=r"C:\fake\agy.exe",
        process_names=("agy.exe",), self_update=("update",))

    def upgrade_calls(runner):
        """Every argv that could MUTATE anything."""
        return [a for a in runner.argvs()
                if "upgrade" in a or "install" in a or "update" in a]

    # --- 1. parse_version reads all three real output shapes ---------------
    check("cli-update: parses the Claude CLI's own version line",
          cli_update.parse_version("2.1.224 (Claude Code)") == "2.1.224")
    check("cli-update: parses agy's bare version",
          cli_update.parse_version("1.1.11") == "1.1.11")
    check("cli-update: prefers winget's Version: line over other numbers",
          cli_update.parse_version(
              "Found Claude Code [Anthropic.ClaudeCode]\n"
              "Version: 2.1.231\nRelease Notes Url: https://x/v1.2.3")
          == "2.1.231")
    check("cli-update: garbage parses to nothing",
          cli_update.parse_version("no version here") == "")
    # ...and an unparseable version can never be the REASON to install: a parse
    # failure fails open in both directions
    check("cli-update: an unparseable version never triggers an install",
          not cli_update.needs_update("garbage", "2.1.231")
          and not cli_update.needs_update("2.1.224", "garbage")
          and cli_update.needs_update("2.1.224", "2.1.231"))
    check("cli-update: version ordering is numeric, not lexical",
          cli_update.version_tuple("2.1.99") < cli_update.version_tuple("2.1.224"))

    # --- 2. the toggle off means the machine is never touched --------------
    runner = _FakeCli()
    outs = cli_update.run_gate([claude, agy], runner, enabled=False)
    check("cli-update: switched off, not one command is run",
          runner.calls == [] and [o.status for o in outs]
          == [Status.DISABLED, Status.DISABLED], runner.argvs())
    check("cli-update: a disabled target writes no audit line",
          cli_update.audit_lines(outs[0]) == [])

    # --- 3. already current: the upgrade command is never issued -----------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: installed == available is UP_TO_DATE",
          out.status is Status.UP_TO_DATE and out.before == "2.1.224", out)
    check("cli-update: nothing to do means no upgrade command at all",
          upgrade_calls(runner) == [], runner.argvs())
    check("cli-update: the manifest is read with `winget show`, never `upgrade`",
          ["winget", "show", "--id", "Anthropic.ClaudeCode", "--exact",
           "--accept-source-agreements"] in runner.argvs(), runner.argvs())

    # --- 4. a real update, decided by the FILE both times ------------------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a version that actually moved is UPDATED",
          out.status is Status.UPDATED and (out.before, out.after)
          == ("2.1.224", "2.1.231"), out)
    check("cli-update: the transition is audited",
          "UPDATE claude 2.1.224 -> 2.1.231" in cli_update.audit_lines(out),
          cli_update.audit_lines(out))
    check("cli-update: the check itself is audited with both versions",
          "UPDATE-CHECK claude installed=2.1.224 available=2.1.231"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))
    check("cli-update: success is read back off the binary, not off winget",
          runner.argvs().count([r"C:\fake\claude.exe", "--version"]) == 2,
          runner.argvs())

    # --- 5. THE REPORTED BUG: winget claims success, the file is unchanged --
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      upgrade=(0, "Successfully installed"))
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: an installer that reports success but changes nothing "
          "is REPORTED_BUT_UNCHANGED",
          out.status is Status.REPORTED_BUT_UNCHANGED, out)
    check("cli-update: that is audited precisely",
          any(line.startswith("UPDATE-UNCHANGED claude")
              for line in cli_update.audit_lines(out)),
          cli_update.audit_lines(out))
    check("cli-update: and it earns the pill",
          "Claude Code" in cli_update.pill_text([out]),
          cli_update.pill_text([out]))

    # --- 6. THE CAUSE: a live claude.exe means no upgrade command runs -----
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      alive=3)
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a target process alive blocks the update",
          out.status is Status.BLOCKED_PROCESSES and "3 claude.exe" in out.detail,
          out)
    check("cli-update: blocked means NO winget command at all, so its database "
          "can never be poisoned by us",
          not any(a[0] == "winget" for a in runner.argvs()), runner.argvs())
    check("cli-update: the skip names the count",
          "UPDATE-SKIP claude (3 claude.exe alive)"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))
    # ...and if we cannot even ask, we still refuse (better a missed update
    # than an upgrade run against a file we cannot prove is unlocked)
    runner = _FakeCli(alive=-1, available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: an unanswerable process check blocks too",
          out.status is Status.BLOCKED_PROCESSES
          and not any(a[0] == "winget" for a in runner.argvs()), out)

    # --- 6b. an image NAME is not an identity (live: the gate counted the
    # Claude DESKTOP app's Claude.exe as a locked CLI and skipped forever) ---
    desktop = [r"C:\Program Files\WindowsApps\Claude_1.26832.0.0_x64__p\app"
               r"\Claude.exe"] * 8
    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231", alive=8, paths=desktop)
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: eight processes sharing the image name but NOT the "
          "path do not block the update",
          out.status is Status.UPDATED, out)
    check("cli-update: the cheap name pass runs first and the path pass only "
          "when it found something",
          [a[0] for a in runner.argvs()][:2] == ["tasklist", "powershell"],
          runner.argvs())
    # the same eight, plus one that really is ours: still blocked
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      alive=9, paths=desktop + [r"C:\fake\claude.exe"])
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: one genuine CLI among them still blocks",
          out.status is Status.BLOCKED_PROCESSES and "1 claude.exe" in out.detail,
          out)
    # nothing by that name at all: no reason to pay for the path pass
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    cli_update.run_gate([claude], runner)
    check("cli-update: nothing running means the path pass is never run",
          not any(a[0] == "powershell" for a in runner.argvs()),
          runner.argvs())
    # every ambiguity leans towards over-counting: a skipped update is
    # reportable, an upgrade against a locked file poisons the database
    for label, kwargs in (
            ("a path query that fails", dict(paths_rc=1)),
            ("a path that cannot be read", dict(paths=["?"] * 3)),
            ("a path pass that sees less than the name pass", dict(paths=[]))):
        runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                          available="2.1.231", alive=3, **kwargs)
        out = cli_update.run_gate([claude], runner)[0]
        check(f"cli-update: {label} still blocks",
              out.status is Status.BLOCKED_PROCESSES
              and not any(a[0] == "winget" for a in runner.argvs()), out)
    # two spellings of one file must agree: the live paths differed in case
    # alone (`claude.EXE` from the resolver, `claude.exe` from the listing)
    check("cli-update: path identity ignores case",
          cli_update._canonical(r"C:\Fake\CLAUDE.EXE")
          == cli_update._canonical(r"c:\fake\claude.exe"),
          cli_update._canonical(r"C:\Fake\CLAUDE.EXE"))
    # winget also installs a symlink shim, and a session launched through it
    # reports the LINK -- a plain string compare would call it somebody else's
    tmp = tempfile.mkdtemp(prefix="aihive-cliupd-")
    real = os.path.join(tmp, "claude.exe")
    link = os.path.join(tmp, "link-claude.exe")
    with open(real, "wb") as fh:
        fh.write(b"x")
    linked = True
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError, AttributeError):
        linked = False          # Windows without developer mode / admin
    if linked:
        shim_target = cli_update.Target(
            key="claude", label="Claude Code", exe=real,
            process_names=("claude.exe",), winget_id="Anthropic.ClaudeCode")
        runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                          available="2.1.231", alive=1, paths=[link])
        out = cli_update.run_gate([shim_target], runner)[0]
        check("cli-update: a process launched through the winget shim is "
              "recognised as the same binary",
              out.status is Status.BLOCKED_PROCESSES, out)
    shutil.rmtree(tmp, ignore_errors=True)

    # --- 7. the poisoned database, reported but never forced --------------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.231",
                      upgrade=(0, "No available upgrade found."))
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: behind the manifest but 'no upgrade found' is DB_STALE",
          out.status is Status.DB_STALE, out)
    check("cli-update: DB_STALE reports and does NOT force a reinstall",
          not any("--force" in a for a in runner.argvs()), runner.argvs())
    check("cli-update: the stale record is audited with both versions",
          any("2.1.224 < 2.1.231" in line
              for line in cli_update.audit_lines(out)),
          cli_update.audit_lines(out))
    check("cli-update: the pill carries the one command to run by hand",
          "winget install --id Anthropic.ClaudeCode --exact --force"
          in cli_update.pill_tooltip([out]), cli_update.pill_tooltip([out]))

    # --- 8. a hung check fails OPEN to launch ------------------------------
    runner = _FakeCli(timeout_on=("winget show",), available="2.1.231")
    out = cli_update.run_gate([claude], runner)[0]
    check("cli-update: a check that outruns its budget is TIMEOUT",
          out.status is Status.TIMEOUT, out)
    check("cli-update: a timeout is audited and shows no pill (launch wins)",
          cli_update.audit_lines(out) == [
              "UPDATE-CHECK claude installed=2.1.224",
              "UPDATE-TIMEOUT claude check exceeded 5s"]
          and cli_update.pill_text([out]) == "",
          cli_update.audit_lines(out))
    check("cli-update: the check phase is bounded, so no upgrade followed",
          upgrade_calls(runner) == [], runner.argvs())

    # --- 9. serial: two multi-hundred-MB installers never overlap ----------
    runner = _FakeCli(versions=("2.1.224 (Claude Code)",), available="2.1.224")
    outs = cli_update.run_gate([claude, agy], runner)
    seen = [a[0] for a in runner.argvs() if a[0] != "tasklist"]
    first_agy = next(i for i, a in enumerate(runner.argvs())
                     if "agy.exe" in a[0])
    claude_after = [i for i, a in enumerate(runner.argvs())
                    if a[0] == "winget" or "claude.exe" in a[0]]
    check("cli-update: agy's commands never interleave with Claude's",
          all(i < first_agy for i in claude_after), (first_agy, claude_after))
    check("cli-update: both targets are reported, in order",
          [o.target for o in outs] == ["claude", "gemini"], outs)
    # a self-updating CLI has no dry run, so `agy update` IS the check, and an
    # unchanged version afterwards means it was already current, NOT a failure
    check("cli-update: agy self-updates unconditionally (there is no dry run)",
          [r"C:\fake\agy.exe", "update"] in runner.argvs(), runner.argvs())
    check("cli-update: an unchanged agy after a clean self-update is up to date",
          outs[1].status is Status.UP_TO_DATE and cli_update.pill_text(outs) == "",
          outs[1])
    runner = _FakeCli(versions=("1.1.11", "1.2.0"), update_out=(0, "updated"))
    out = cli_update.run_gate([agy], runner)[0]
    check("cli-update: an agy version that moved is UPDATED",
          out.status is Status.UPDATED and out.after == "1.2.0", out)
    runner = _FakeCli(versions=("1.1.11",), update_out=(1, "network down"))
    out = cli_update.run_gate([agy], runner)[0]
    check("cli-update: a failed self-update is FAILED, with the rc audited",
          out.status is Status.FAILED
          and "UPDATE-FAIL gemini rc=1 network down"
          in cli_update.audit_lines(out), cli_update.audit_lines(out))

    # a target that is not installed is skipped in silence
    missing = cli_update.Target(key="claude", label="Claude Code", exe="")
    out = cli_update.run_gate([missing], _FakeCli())[0]
    check("cli-update: an uninstalled CLI is skipped without any command",
          out.status is Status.NOT_INSTALLED
          and cli_update.pill_text([out]) == "", out)

    # --- 11. the pill speaks for exactly four statuses ---------------------
    def one(status):
        return cli_update.Outcome("claude", status, before="2.1.224",
                                  after="2.1.224", available="2.1.231",
                                  detail="rc=1 boom", label="Claude Code")

    speaks = [s for s in Status if cli_update.pill_text([one(s)])]
    check("cli-update: only the four reporting statuses show the pill",
          set(speaks) == {Status.BLOCKED_PROCESSES, Status.DB_STALE,
                          Status.REPORTED_BUT_UNCHANGED, Status.FAILED},
          speaks)
    check("cli-update: a clean gate says nothing at all",
          cli_update.pill_text([one(Status.UPDATED), one(Status.UP_TO_DATE),
                                one(Status.TIMEOUT), one(Status.DISABLED),
                                one(Status.NOT_INSTALLED)]) == "")

    # --- 12. no em dash reaches the reader (the global check covers the
    # module; these are the strings it actually composes at runtime) --------
    composed = cli_update.pill_text([one(Status.DB_STALE)], ("gemini",)) + \
        cli_update.pill_tooltip([one(Status.DB_STALE)], ("gemini",))
    bar = TopBar()
    bar.set_auto_update(True)
    on_tip = bar.auto_update_btn.toolTip()
    bar.set_auto_update(False)
    off_tip = bar.auto_update_btn.toolTip()
    check("cli-update: no em dash in the pill or either tooltip",
          "\u2014" not in composed + on_tip + off_tip)
    check("cli-update: both tooltips say what arming this does",
          "installs it BEFORE any agent launches" in on_tip
          and "changes installed software" in off_tip)

    # --- the switch: default OFF, persisted, and its own Manage door ------
    # On the bar there was room for ONE update control, so the button had to be
    # the door to the Updates panel and the preference lived on a checkbox
    # inside it. The Options panel has room for both, so the switch is a switch
    # and `updates_manage_btn` is the door. That is not two controls for one
    # setting: `open_updates_panel` drives BOTH from the panel's own signal.
    check("cli-update toggle: defaults to OFF (it installs software)",
          not bar.auto_update() and not bar.auto_update_btn.isChecked())
    emitted, opened = [], []
    bar.autoUpdateToggled.connect(emitted.append)
    bar.updatesPanelRequested.connect(lambda: opened.append(True))
    bar.auto_update_btn.click()
    check("cli-update toggle: the switch arms the gate and emits True",
          emitted == [True] and opened == [] and bar.auto_update()
          and bar.auto_update_btn.isChecked(), (emitted, opened))
    bar.updates_manage_btn.click()
    check("cli-update toggle: Manage opens the panel and changes no preference",
          opened == [True] and emitted == [True] and bar.auto_update())
    bar.set_auto_update(False)
    check("cli-update toggle: set_auto_update does not re-emit",
          emitted == [True] and not bar.auto_update()
          and not bar.auto_update_btn.isChecked())
    bar.note_update_pending("")
    check("cli-update pill: hidden when there is nothing to report",
          not bar.update_pill.isVisibleTo(bar.options_panel)
          and not bar.options_btn.property("attention"))
    bar.note_update_pending("something", "the long form")
    check("cli-update pill: shown with its tooltip when there is",
          bar.update_pill.text() == "something"
          and bar.update_pill.toolTip() == "the long form"
          and bar.update_pill.isVisibleTo(bar.options_panel))
    check("cli-update pill: it also lights the Options button, so a warning "
          "behind a closed panel is not a secret",
          bar.options_btn.property("attention") is True
          and "something" in bar.options_btn.toolTip())
    # the detected install method is stated in full under the switch, not
    # crammed into a tooltip
    bar.note_install_state("Claude Code, WinGet package")
    check("cli-update: the install method is named under the switch",
          bar.install_label.text() == "Claude Code, WinGet package"
          and bar.install_label.isVisibleTo(bar.options_panel))
    bar.deleteLater()

    # --- 10. ui.auto_update round-trips, defaults False, no version bump ---
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-cliupdate-"))
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win._save_timer.stop()
    check("cli-update: a session that predates the feature defaults it OFF",
          not win._auto_update and not win.top_bar.auto_update())
    check("cli-update: the preference is persisted under ui",
          win._session_payload()["ui"]["auto_update"] is False)
    win._on_auto_update_toggled(True)
    check("cli-update: flipping the preference marks the session dirty",
          win._save_timer.isActive() and win._auto_update)
    check("cli-update: ...and it is what gets written",
          win._session_payload()["ui"]["auto_update"] is True)
    win._restore_ui_state({"ui": {"auto_update": True}})
    check("cli-update: the preference restores onto the window and the button",
          win._auto_update and win.top_bar.auto_update())
    win._restore_ui_state({"ui": {}})
    check("cli-update: a missing key still means OFF",
          not win._auto_update and not win.top_bar.auto_update())

    # a real close/reopen, which is the only way to catch a default assigned
    # AFTER _restore_ui_state has run (that exact bug hit the taskbar toggle)
    win._auto_update = True
    win.top_bar.set_auto_update(True)
    win._save_session()
    win.close()
    app.processEvents()
    again = create_main_window(SessionStore(path=tmp / "session.json"))
    check("cli-update: ON survives a close and reopen",
          again._auto_update and again.top_bar.auto_update())

    # --- the report, and the outcomes never touching session state ---------
    again._save_timer.stop()
    blocked = cli_update.Outcome("claude", Status.BLOCKED_PROCESSES,
                                 detail="3 claude.exe alive", label="Claude Code")
    again.note_update_outcomes([blocked])
    check("cli-update: the gate's report reaches the top-bar pill",
          again.top_bar.update_pill.isVisibleTo(
              again.top_bar.options_panel)
          and "Claude Code" in again.top_bar.update_pill.text(),
          again.top_bar.update_pill.text())
    check("cli-update: reporting an outcome NEVER marks the session dirty "
          "(outcomes are transient, only the preference persists)",
          not again._save_timer.isActive())
    check("cli-update: the window keeps the outcomes so the Updates panel can "
          "show them after the splash has closed",
          again._update_outcomes == (blocked,)
          and "Claude Code" in cli_update.last_check_summary(
              again._update_outcomes))
    check("cli-update: ...and keeping them is transient too",
          not again._save_timer.isActive()
          and "update_outcomes" not in str(again._session_payload()))
    again.note_update_outcomes([cli_update.Outcome("claude", Status.UP_TO_DATE)])
    check("cli-update: a clean gate leaves the pill hidden",
          not again.top_bar.update_pill.isVisibleTo(
              again.top_bar.options_panel))

    # --- 6.1: skipping while an install runs holds those agents back -------
    # `start` is stubbed rather than really called: the question is which
    # agents the autostart DECIDES to launch, and no test should spawn a CLI
    # whose binary this feature is notionally rewriting.
    from app.process_worker import AgentKind, build_spec
    ws = again.manager.create_workspace("CliUpdate", str(tmp))
    gemini_agent = again.manager.add_terminal(
        ws.id, build_spec(AgentKind.GEMINI, "G1", cwd=str(tmp)),
        autostart=False)
    claude_agent = again.manager.add_terminal(
        ws.id, build_spec(AgentKind.CLAUDE, "C1", cwd=str(tmp)),
        autostart=False)
    started = []
    for a in again.manager.all_agents():
        a.autostart_on_restore = True
        a.start = (lambda agent=a: started.append(agent.spec.name))

    again.note_update_outcomes([blocked], installing=("gemini",))
    check("cli-update: the pill says which agents are being held back",
          "Gemini (agy)" in again.top_bar.update_pill.text(),
          again.top_bar.update_pill.text())
    again.autostart_active_workspace()
    check("cli-update: an agent whose CLI is mid-install is NOT started "
          "(it could execute a half written binary)",
          "G1" not in started, started)
    check("cli-update: ...while every other agent starts as usual",
          "C1" in started, started)
    started.clear()
    again.note_update_outcomes([blocked])   # install finished / normal launch
    again.autostart_active_workspace()
    check("cli-update: and with nothing installing, the deferral is gone",
          again._update_installing == () and "G1" in started, started)
    again.close()
    app.processEvents()

    # --- 13. the factory the suite shares does NO update work --------------
    real_runner, real_gate = cli_update.subprocess_runner, cli_update.run_gate
    touched = []
    cli_update.subprocess_runner = lambda *a, **k: touched.append(a) or (0, "")
    cli_update.run_gate = lambda *a, **k: touched.append(a) or []
    try:
        armed = SessionStore(path=tmp / "armed.json")
        armed.save({"ui": {"auto_update": True}})
        spare = create_main_window(armed)
        spare._save_timer.stop()
        check("cli-update: building a window with the toggle ON still runs "
              "nothing (the gate is opted into from main.py alone)",
              touched == [] and spare._auto_update, touched)
        spare.close()
        app.processEvents()
    finally:
        cli_update.subprocess_runner = real_runner
        cli_update.run_gate = real_gate

    # --- the splash + worker thread, end to end, with a fake runner --------
    from app.widgets.update_splash import UpdateSplash, run_update_gate
    audits = []

    class _Audits:
        def audit(self, line):
            audits.append(line)

    runner = _FakeCli(versions=("2.1.224 (Claude Code)", "2.1.231 (Claude Code)"),
                      available="2.1.231")
    result = run_update_gate(_Audits(), targets=[claude], runner=runner,
                             auto_close_ms=0)
    check("cli-update splash: the gate runs off the GUI thread and returns "
          "its outcomes",
          [o.status for o in result.outcomes] == [Status.UPDATED], result)
    check("cli-update splash: nothing was still installing, so no deferral",
          result.installing == ())
    check("cli-update splash: the audit trail reached session.log",
          any(line.startswith("UPDATE claude") for line in audits), audits)
    splash = UpdateSplash([claude, agy])
    splash.set_state("claude", "checking", True)
    check("cli-update splash: a row shows what it is doing",
          splash.row_state("claude") == "checking")
    splash.set_state("claude", "up to date (2.1.224)", False)
    check("cli-update splash: the spinner stops once nothing is busy",
          splash._spin.state() != splash._spin.State.Running)
    splash.close()
    splash.deleteLater()

    # --- 14. what a skipped check MEANS depends on who updates the binary ---
    # Live report: after the native migration the splash flashed "took too
    # long, skipped" for under a second on a launch where nothing was wrong,
    # the CLI updated itself a minute later, and no surface afterwards could
    # say so. A self-updating target that we failed to check is a non event;
    # the same status on a package managed one is a genuinely missed update.
    from app.widgets.update_splash import LINGER_CLOSE_MS, subtitle_for

    native_claude = cli_update.Target(
        key="claude", label="Claude Code", exe=r"C:\fake\claude.exe",
        process_names=("claude.exe",), self_update=("update",))

    def outcome(status, self_updating, **kw):
        return cli_update.Outcome("claude", status, label="Claude Code",
                                  self_updating=self_updating, **kw)

    self_late = outcome(Status.TIMEOUT, True, before="2.1.228",
                        detail="check exceeded 5s")
    winget_late = outcome(Status.TIMEOUT, False, before="2.1.224",
                          detail="check exceeded 5s")
    gemini_ok = cli_update.Outcome("gemini", Status.UP_TO_DATE, before="1.1.12",
                                   label="Gemini (agy)", self_updating=True)

    check("cli-update words: a self-updating CLI we could not check is left to "
          "its own updater, not reported as skipped",
          cli_update.state_text(self_late) == "left to its own updater")
    check("cli-update words: the same status on a package managed CLI still "
          "says the update was missed (nothing else will fetch one)",
          cli_update.state_text(winget_late) == "took too long, skipped")
    check("cli-update words: the shape is stamped on the outcome by check(), "
          "so no caller has to remember to set it",
          cli_update.check(native_claude,
                           _FakeCli(timeout_on=("--version",))).self_updating
          and not cli_update.check(claude,
                                   _FakeCli(timeout_on=("--version",))
                                   ).self_updating)

    check("cli-update pill: a self-updating CLI raises no nag, because every "
          "instruction the nag carries would be unnecessary",
          cli_update.pill_text([self_late, gemini_ok]) == ""
          and cli_update.pill_tooltip([self_late]) == "")
    self_busy = outcome(Status.BLOCKED_PROCESSES, True,
                        detail="3 claude.exe alive")
    winget_busy = outcome(Status.BLOCKED_PROCESSES, False,
                          detail="3 claude.exe alive")
    check("cli-update pill: ...and that holds for a locked file too, since a "
          "self-updating CLI does not need the user to close anything",
          cli_update.pill_text([self_busy]) == ""
          and "Claude Code" in cli_update.pill_text([winget_busy]))
    check("cli-update pill: the splash and the pill can never disagree, so an "
          "outcome the splash calls a non event never raises a nag",
          all(not cli_update.needs_pill(o)
              for o in (self_late, winget_late, self_busy, winget_busy)
              if cli_update.state_text(o)
              in cli_update._SELF_UPDATING_TEXT.values())
          and cli_update.needs_pill(winget_busy))

    check("cli-update linger: a missed update earns a moment to be read",
          cli_update.worth_reading([winget_late])
          and cli_update.worth_reading([winget_busy]))
    check("cli-update linger: ...and a non event does not, or the pause would "
          "manufacture the concern the wording removes",
          not cli_update.worth_reading([self_late, gemini_ok])
          and not cli_update.worth_reading([]))

    # the durable copy. The pill speaks only for what the user can act on, so
    # without this a status glimpsed on the splash has nowhere to be re-read.
    summary = cli_update.last_check_summary([self_late, gemini_ok])
    check("cli-update panel: the last check reports EVERY outcome, including "
          "the ones the pill deliberately withholds",
          "Claude Code: left to its own updater" in summary
          and "Gemini (agy): up to date (1.1.12)" in summary, summary)
    check("cli-update panel: ...and names anything still installing",
          "still installing" in
          cli_update.last_check_summary([self_late], installing=("gemini",)))

    check("cli-update audit: the timeout line records which shape it describes",
          cli_update.audit_lines(self_late)[-1].endswith(
              "(self-updating, left to the CLI)")
          and cli_update.audit_lines(winget_late)[-1].endswith("exceeded 5s"),
          cli_update.audit_lines(self_late))

    check("cli-update splash: the subtitle drops the locked-file claim when "
          "every target updates itself (the native install never touches the "
          "running file)",
          "not in use" not in subtitle_for([native_claude, agy])
          and "before agents start" in subtitle_for([native_claude, agy]))
    check("cli-update splash: ...and keeps it while any target is package "
          "managed, where it is both true and the reason to wait",
          subtitle_for([claude, agy]) ==
          "Now is the only moment these files are not in use.")

    # the linger, end to end through the real event loop. A long linger and a
    # threshold just under it: a 400ms linger against a 350ms line failed the
    # fast case whenever a loaded machine took 350ms to show and close a window
    for target, expect_wait in ((claude, True), (native_claude, False)):
        started = time.time()
        run_update_gate(None, targets=[target],
                        runner=_FakeCli(timeout_on=("--version",)),
                        auto_close_ms=0, linger_ms=1500, show=True)
        waited = time.time() - started
        check("cli-update splash: a missed update holds the window open"
              if expect_wait else
              "cli-update splash: ...and a non event closes as fast as a "
              "clean launch",
              (waited >= 1.4) is expect_wait, (target.key, waited))


class _FakeInstall:
    """A recording `Runner` for the install-method control.

    Same rule as `_FakeCli`: no check below shells out, installs anything, or
    touches the user's real `~/.claude/settings.json`. `versions` maps a
    canonical exe path to what `--version` prints; `lands` is merged in when the
    installer runs, which is how "the installer reported success but the file
    did not change" is expressed."""

    def __init__(self, versions=None, lands=None, install=(0, "installed"),
                 alive=0, paths=None, winget=(0, "")):
        from app.cli_update import _canonical
        self.calls = []
        self._canon = _canonical
        self.versions = {_canonical(k): v for k, v in (versions or {}).items()}
        self.lands = {_canonical(k): v for k, v in (lands or {}).items()}
        self.install = install
        self.alive = alive
        self.paths = paths
        self.winget = winget

    def argvs(self):
        return [list(a) for a in self.calls]

    def mutating(self):
        """Every argv that could change installed software."""
        return [a for a in self.argvs()
                if any(t in " ".join(a).lower()
                       for t in ("install", "uninstall", "upgrade", "irm "))]

    def __call__(self, argv, timeout=0):
        argv = list(argv)
        self.calls.append(argv)
        joined = " ".join(argv)
        if argv[0] == "tasklist":
            name = argv[2].split()[-1]
            body = "\n".join(f"{name}   {1000 + i} Console  1  285,000 K"
                             for i in range(max(0, self.alive)))
            return 0, body or "INFO: No tasks are running"
        if argv[0] == "powershell" and "Get-CimInstance" in joined:
            lines = self.paths if self.paths is not None else []
            return 0, "\n".join(lines)
        if argv[0] == "powershell":            # the documented installer
            self.versions.update(self.lands)
            return self.install
        if argv[-1] == "--version":
            found = self.versions.get(self._canon(argv[0]), "")
            return (0, found) if found else (1, "not found")
        if argv[0] == "winget":
            return self.winget
        return 0, ""


def test_cli_native_migration():
    """The consented switch onto the self-updating native install.

    Root cause it exists for: a package-manager Claude Code does not update
    itself and no setting fixes that, while model aliases resolve CLIENT-SIDE
    from a table baked into the installed binary, so a stale file silently
    cannot launch newer models. The install method IS the behaviour, hence a
    migration rather than a preference.

    Everything is driven through injected runners and temporary directories:
    no check installs anything, shells out, or touches the real settings file.
    """
    import json as _json
    import threading
    from PySide6.QtWidgets import QApplication
    from app import cli_install, cli_update, providers
    from app.cli_install import InstallKind, UpdateState
    from app.process_worker import AgentKind, build_spec
    from app.session_store import SessionStore
    from app.widgets.update_panel import ConsentDialog, UpdatePanel
    from main import create_main_window

    app = QApplication.instance() or QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="ai-hive-migrate-"))
    winget_exe = str(tmp / "Microsoft" / "WinGet" / "Packages"
                     / "Anthropic.ClaudeCode_x" / "claude.exe")
    native_exe = str(tmp / "home" / ".local" / "bin" / "claude.exe")

    # --- 1. classify_install: the PATH is the only thing that tells the two
    # installs apart (identical binary, version string and process name) ------
    check("cli-install: a WinGet-Packages path classifies as WINGET",
          cli_install.classify_install(
              r"C:\Users\x\AppData\Local\Microsoft\WinGet\Packages"
              r"\Anthropic.ClaudeCode_y\claude.exe") is InstallKind.WINGET)
    check("cli-install: a .local\\bin path classifies as NATIVE",
          cli_install.classify_install(r"C:\Users\x\.local\bin\claude.exe")
          is InstallKind.NATIVE)
    check("cli-install: an npm global path classifies as NPM",
          cli_install.classify_install(r"C:\Users\x\AppData\Roaming\npm\claude.cmd")
          is InstallKind.NPM
          and cli_install.classify_install(
              r"C:\x\node_modules\.bin\claude.exe") is InstallKind.NPM)
    check("cli-install: no binary at all is MISSING",
          cli_install.classify_install("") is InstallKind.MISSING
          and cli_install.classify_install("claude.exe") is InstallKind.MISSING)
    # case and symlink: winget also installs a Links shim, so a plain string
    # compare would read the user's own session as somebody else's program
    real = tmp / "packages" / "Microsoft" / "WinGet" / "Packages" / "A_x"
    real.mkdir(parents=True, exist_ok=True)
    (real / "claude.exe").write_text("x", encoding="utf-8")
    check("cli-install: classification ignores case",
          cli_install.classify_install(
              str(real / "claude.exe").upper()) is InstallKind.WINGET)
    link = tmp / "Links" / "claude.exe"
    link.parent.mkdir(parents=True, exist_ok=True)
    linked = True
    try:
        os.symlink(real / "claude.exe", link)
    except (OSError, NotImplementedError, AttributeError):
        linked = False       # unprivileged Windows cannot create a symlink
    check("cli-install: a symlink shim resolves to its target's install kind",
          (not linked) or cli_install.classify_install(str(link))
          is InstallKind.WINGET)

    # --- 2. detect: one state per machine, and never a boolean --------------
    settings = tmp / "settings.json"
    settings.write_text(_json.dumps({"model": "opus", "permissions": {"a": 1}}),
                        encoding="utf-8")

    def situation(exe, policies=()):
        return cli_install.detect(exe, str(settings), policies=list(policies))

    got = situation(winget_exe)
    check("cli-install: a winget install with no policy offers the migration",
          got.state is UpdateState.MANAGED and got.actionable(), got)
    got = situation(native_exe)
    check("cli-install: a native install with no disabling key is SELF_ACTIVE",
          got.state is UpdateState.SELF_ACTIVE and got.paused_key == "", got)
    settings.write_text(_json.dumps(
        {"model": "opus", "env": {"DISABLE_AUTOUPDATER": "1"}}),
        encoding="utf-8")
    got = situation(native_exe)
    check("cli-install: DISABLE_AUTOUPDATER reads as SELF_PAUSED, and the key "
          "is named",
          got.state is UpdateState.SELF_PAUSED
          and got.paused_key == "DISABLE_AUTOUPDATER", got)
    settings.write_text(_json.dumps({"env": {"DISABLE_UPDATES": "1"}}),
                        encoding="utf-8")
    check("cli-install: DISABLE_UPDATES also reads as SELF_PAUSED",
          situation(native_exe).state is UpdateState.SELF_PAUSED
          and situation(native_exe).paused_key == "DISABLE_UPDATES")
    settings.write_text(_json.dumps({}), encoding="utf-8")
    got = situation(r"C:\Users\x\AppData\Roaming\npm\claude.cmd")
    check("cli-install: an npm install already auto-updates, so nothing is "
          "offered",
          got.state is UpdateState.NOT_APPLICABLE and not got.actionable(), got)
    got = situation(native_exe, policies=[{"autoUpdatesChannel": "stable"}])
    check("cli-install: managed settings enforcing updates lock the control, "
          "with the reason shown",
          got.state is UpdateState.LOCKED_BY_POLICY and not got.actionable()
          and "managed settings" in got.detail, got)
    check("cli-install: a policy pinning the env key locks it too",
          situation(native_exe,
                    policies=[{"env": {"DISABLE_UPDATES": "1"}}]).state
          is UpdateState.LOCKED_BY_POLICY)
    check("cli-install: the managed path read is the documented one, not a "
          "guess",
          cli_install.managed_settings_path().endswith(
              os.path.join("ClaudeCode", "managed-settings.json")),
          cli_install.managed_settings_path())

    # --- 3. the installer line is the documented one, and only that ---------
    argv = cli_install.install_argv()
    check("cli-install: the installer is Anthropic's documented one",
          argv[0] == "powershell" and "irm https://claude.ai/install.ps1 | iex"
          in " ".join(argv), argv)
    check("cli-install: ...and it never runs winget",
          "winget" not in " ".join(argv).lower(), argv)
    # CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE is deliberately unsupported: it
    # makes a RUNNING Claude Code invoke `winget upgrade` on itself, which is
    # the exact act that writes the false winget database record cli_update.py
    # exists to prevent. It is named in the module docstring as a decision and
    # must never become code, so this asserts the module sets no env at all.
    module_src = (ROOT / "app" / "cli_install.py").read_text(encoding="utf-8")
    panel_src = (ROOT / "app" / "widgets" / "update_panel.py").read_text(encoding="utf-8")
    check("cli-install: no code path ever sets an environment variable, so "
          "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE can never be turned on",
          "os.environ[" not in module_src and "putenv" not in module_src
          and "CLAUDE_CODE_PACKAGE_MANAGER_AUTO_UPDATE" not in panel_src)

    # --- 4. migrate decides success by RE-READING THE FILE ------------------
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: a migration whose file really moved is ok",
          out.ok and out.after == "2.1.231", out)
    check("cli-install: the transition is audited",
          cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          cli_install.audit_lines(out))
    runner = _FakeInstall()          # installer says rc 0, nothing landed
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: an installer that reports success while the file did "
          "not change is a FAILURE",
          not out.ok and not out.after, out)
    check("cli-install: ...and the failure is audited, not the success",
          cli_install.audit_lines(out)[0].startswith("CLI-MIGRATE-FAIL"))
    runner = _FakeInstall(lands={native_exe: "2.1.200"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: a native build OLDER than the one you had is refused",
          not out.ok and "older" in out.detail, out)
    slow = _FakeInstall(install=(cli_update.RC_TIMEOUT, ""))
    out = cli_install.migrate(slow, launcher=native_exe, before="2.1.224")
    check("cli-install: an installer that never finished claims no version",
          not out.ok and out.after == "" and out.before == "2.1.224", out)

    # --- 4.1 a successful migrate fixes the USER'S OWN terminal's PATH too --
    # `path_updater` is injected exactly like `runner`: the suite must never
    # touch the real Windows User PATH registry key, so these drive FAKE
    # updaters and never call the real `ensure_native_on_path`.
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224")
    check("cli-install: with no path_updater given, nothing is added and "
          "nothing is audited about PATH (the default the suite exercises)",
          out.path_added is False
          and cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          (out, cli_install.audit_lines(out)))
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=lambda: True)
    check("cli-install: a path_updater that changed PATH is reflected on the "
          "result and audited",
          out.ok and out.path_added is True
          and cli_install.audit_lines(out) == [
              "CLI-MIGRATE-OK 2.1.224 -> 2.1.231",
              "CLI-MIGRATE-PATH added .local\\bin to the User PATH"],
          (out, cli_install.audit_lines(out)))
    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=lambda: False)
    check("cli-install: a path_updater reporting 'already there' adds nothing",
          out.ok and out.path_added is False
          and cli_install.audit_lines(out) == ["CLI-MIGRATE-OK 2.1.224 -> 2.1.231"],
          out)

    def _boom():
        raise OSError("registry is locked")

    runner = _FakeInstall(lands={native_exe: "2.1.231 (Claude Code)"})
    out = cli_install.migrate(runner, launcher=native_exe, before="2.1.224",
                              path_updater=_boom)
    check("cli-install: a PATH nicety that raises never takes the migration "
          "itself down with it",
          out.ok and out.after == "2.1.231" and out.path_added is False, out)

    # `_compute_updated_path` is the pure decision `ensure_native_on_path`
    # makes before ever touching the registry, and is tested directly for
    # that reason.
    check("cli-install: an empty PATH becomes just the target",
          cli_install._compute_updated_path("", r"C:\u\.local\bin")
          == r"C:\u\.local\bin")
    check("cli-install: the target is appended after existing entries",
          cli_install._compute_updated_path(
              r"C:\a;C:\b", r"C:\u\.local\bin")
          == r"C:\a;C:\b;C:\u\.local\bin")
    check("cli-install: an exact match already on PATH changes nothing",
          cli_install._compute_updated_path(
              r"C:\a;C:\u\.local\bin;C:\b", r"C:\u\.local\bin") is None)
    check("cli-install: matching is case- and trailing-backslash-insensitive, "
          "like Windows PATH lookups are",
          cli_install._compute_updated_path(
              r"C:\a;" + r"C:\U\.LOCAL\BIN" + "\\", r"C:\u\.local\bin") is None)

    # --- 5. resolve_claude prefers the NATIVE launcher (the §4.1 trap) ------
    # MEASURED: the winget package directory is on PATH directly, and
    # %USERPROFILE%\.local\bin is not, so `shutil.which` would keep answering
    # with the stale copy and the migration would appear to do nothing.
    home = tmp / "home"
    (home / ".local" / "bin").mkdir(parents=True, exist_ok=True)
    Path(native_exe).write_text("native", encoding="utf-8")
    real_which, real_home = providers.shutil.which, os.environ.get("USERPROFILE")
    try:
        providers.shutil.which = lambda name: winget_exe
        os.environ["USERPROFILE"] = str(home)
        check("cli-install: resolve_claude prefers the native launcher even "
              "when PATH would answer with the winget copy",
              cli_update._canonical(providers.resolve_claude())
              == cli_update._canonical(native_exe),
              providers.resolve_claude())
        os.environ["USERPROFILE"] = str(tmp / "nowhere")
        check("cli-install: ...and falls back to PATH when there is no native "
              "install",
              providers.resolve_claude() == winget_exe)
    finally:
        providers.shutil.which = real_which
        if real_home is None:
            os.environ.pop("USERPROFILE", None)
        else:
            os.environ["USERPROFILE"] = real_home

    # --- 6. the settings file: other writers, and they are OURS -------------
    doc = {"model": "opus", "effortLevel": "high",
           "hooks": {"Stop": [{"x": 1}]}, "somethingWeNeverHeardOf": [1, 2]}
    settings.write_text(_json.dumps(doc, indent=2), encoding="utf-8")
    out = cli_install.set_paused(str(settings), True)
    after = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: pausing writes exactly one key and preserves every "
          "other, including unknown ones",
          out.ok and after["env"] == {"DISABLE_AUTOUPDATER": "1"}
          and {k: after[k] for k in doc} == doc, after)
    check("cli-install: a settings write keeps one .bak generation",
          (tmp / "settings.json.bak").is_file())
    out = cli_install.set_paused(str(settings), False)
    check("cli-install: resuming restores the original document shape",
          out.ok and _json.loads(settings.read_text(encoding="utf-8")) == doc,
          settings.read_text(encoding="utf-8"))
    settings.write_text(_json.dumps({"env": {"DISABLE_UPDATES": "1",
                                             "FOO": "bar"}}), encoding="utf-8")
    cli_install.set_paused(str(settings), False)
    check("cli-install: resuming clears the STRICT key too, and leaves the "
          "user's own env alone",
          _json.loads(settings.read_text(encoding="utf-8"))
          == {"env": {"FOO": "bar"}})

    broken = tmp / "broken.json"
    broken.write_text("{ this is not json", encoding="utf-8")
    before_bytes = broken.read_bytes()
    out = cli_install.set_paused(str(broken), True)
    check("cli-install: an unparseable settings.json is REFUSED, never "
          "rewritten or repaired",
          not out.ok and broken.read_bytes() == before_bytes
          and not (tmp / "broken.json.bak").exists(), out)

    # THE LOST-UPDATE CHECK. `~/.claude/settings.json` has other writers and
    # they are ours: every agent's `/model` and `/config` writes it, and the
    # panel can sit open for a minute between the read and the write.
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    stale, _reason = cli_install.read_settings(str(settings))
    settings.write_text(_json.dumps({"model": "opus", "savedByAnAgent": True}),
                        encoding="utf-8")
    cli_install.set_paused(str(settings), True)
    landed = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: write_settings RE-READS, so a key an agent saved while "
          "the panel was open survives",
          landed.get("savedByAnAgent") is True
          and landed["env"]["DISABLE_AUTOUPDATER"] == "1"
          and "savedByAnAgent" not in stale, landed)

    guard = tmp / "guard.json"
    guard.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    real_read = cli_install.read_settings
    reads = {"n": 0}

    def racing_read(path):
        reads["n"] += 1
        if reads["n"] == 2:            # the re-read inside write_settings
            guard.write_text("{ broken now", encoding="utf-8")
        return real_read(path)

    cli_install.read_settings = racing_read
    try:
        cli_install.read_settings(str(guard))         # the caller's read
        out = cli_install.set_paused(str(guard), True)
    finally:
        cli_install.read_settings = real_read
    check("cli-install: a re-read that no longer parses ABORTS the write",
          not out.ok and guard.read_text(encoding="utf-8") == "{ broken now",
          out)

    # --- 7. the channel is NEVER written without its floor ------------------
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "stable", "2.1.226")
    doc = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: stable writes the channel AND a minimumVersion floor",
          out.ok and doc == {"model": "opus", "autoUpdatesChannel": "stable",
                             "minimumVersion": "2.1.226"}, doc)
    check("cli-install: the channel change is audited with its floor",
          cli_install.audit_lines(out)
          == ["CLI-MIGRATE-CHANNEL stable floor=2.1.226"])
    settings.write_text(_json.dumps({"model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "stable", "")
    check("cli-install: a channel with no readable floor is REFUSED and "
          "writes nothing (stable on its own can move you BACKWARDS)",
          not out.ok
          and _json.loads(settings.read_text(encoding="utf-8"))
          == {"model": "opus"}, out)
    settings.write_text(_json.dumps(
        {"autoUpdatesChannel": "stable", "minimumVersion": "2.1.226",
         "model": "opus"}), encoding="utf-8")
    out = cli_install.set_channel(str(settings), "latest", "2.1.231")
    doc = _json.loads(settings.read_text(encoding="utf-8"))
    check("cli-install: going back to latest REMOVES the floor, so the pin "
          "cannot outlive the reason for it",
          out.ok and "minimumVersion" not in doc
          and doc["autoUpdatesChannel"] == "latest", doc)
    body = (ROOT / "app" / "cli_install.py").read_text(encoding="utf-8")
    check("cli-install: requiredMinimumVersion is never WRITTEN (it stops "
          "Claude Code starting at all, and is not ours to set)",
          'doc["requiredMinimumVersion"]' not in body
          and "requiredMinimumVersion\"] =" not in body
          and all("requiredM" not in str(v)
                  for v in _json.loads(
                      settings.read_text(encoding="utf-8")).keys()))

    # --- 8. the update TARGET follows the install method --------------------
    native_target = cli_update._claude_target(native_exe)
    winget_target = cli_update._claude_target(winget_exe)
    check("cli-install: a native Claude Code is a self-updating target",
          native_target.self_update == ("update",)
          and native_target.winget_id is None, native_target)
    check("cli-install: ...and nothing about it mentions winget",
          "winget" not in " ".join(
              cli_update.upgrade_argv(native_target)
              + cli_update.check_argv(native_target)).lower())
    check("cli-install: a winget Claude Code still goes through winget",
          winget_target.winget_id == "Anthropic.ClaudeCode"
          and winget_target.self_update is None, winget_target)
    stale_runner = _FakeCli(versions=("2.1.224 (Claude Code)",),
                            update_out=(0, "No available upgrade found"))
    out = cli_update.run_gate([native_target], stale_runner)[0]
    check("cli-install: DB_STALE is structurally unreachable on a native "
          "install (there is no package database to go stale)",
          out.status is cli_update.Status.UP_TO_DATE, out)

    # --- 9. cleanup counts against the RECORDED WinGet path -----------------
    desktop = r"C:\Users\x\AppData\Local\AnthropicClaude\app-1.0\claude.exe"
    runner = _FakeInstall(alive=1, paths=[winget_exe])
    out = cli_install.cleanup(runner, winget_exe)
    check("cli-install: a live session on the WINGET binary blocks cleanup",
          not out.ok and "still using" in out.detail, out)
    check("cli-install: ...and no kill command is ever issued",
          not any("taskkill" in " ".join(a).lower() or "stop-process"
                  in " ".join(a).lower() for a in runner.argvs())
          and runner.mutating() == [], runner.argvs())
    runner = _FakeInstall(alive=1, paths=[desktop])
    out = cli_install.cleanup(runner, winget_exe)
    check("cli-install: the Claude DESKTOP app shares the image name but not "
          "the path, so it does not block cleanup",
          out.ok, out)
    check("cli-install: ...which is the one command that removes the package",
          runner.mutating() == [["winget", "uninstall", "--id",
                                 "Anthropic.ClaudeCode", "--exact"]],
          runner.mutating())
    runner = _FakeInstall(alive=1, paths=[native_exe])
    out = cli_install.cleanup(runner, native_exe)
    check("cli-install: cleanup counts the path it was GIVEN, so passing the "
          "resolved (native) binary is what would under-count",
          not out.ok and out.after == native_exe, out)
    check("cli-install: the cleanup line names the file it was about",
          cli_install.audit_lines(out)[0].endswith("exe=" + native_exe),
          cli_install.audit_lines(out))

    # --- 9b. the FULL revert, whose ORDER is not interchangeable ------------
    # The WinGet copy goes back and is verified FIRST, so a failed reinstall
    # leaves the user with the working native install rather than with nothing.
    rev = tmp / "revert"
    pkg = (rev / "local" / "Microsoft" / "WinGet" / "Packages"
           / "Anthropic.ClaudeCode_z")
    pkg.mkdir(parents=True, exist_ok=True)
    native_bin = rev / "home" / ".local" / "bin"
    native_bin.mkdir(parents=True, exist_ok=True)
    (native_bin / "claude.exe").write_text("native", encoding="utf-8")
    share = rev / "home" / ".local" / "share" / "claude" / "versions"
    share.mkdir(parents=True, exist_ok=True)
    (share / "2.1.231").write_text("payload", encoding="utf-8")
    real_home, real_local = (os.environ.get("USERPROFILE"),
                             os.environ.get("LOCALAPPDATA"))
    try:
        os.environ["USERPROFILE"] = str(rev / "home")
        os.environ["LOCALAPPDATA"] = str(rev / "local")
        runner = _FakeInstall(alive=1, paths=[str(native_bin / "claude.exe")])
        out = cli_install.revert(runner, home=str(rev / "home"))
        check("cli-install: a revert is refused while a native session is "
              "alive, and never kills one",
              not out.ok and runner.mutating() == []
              and (native_bin / "claude.exe").is_file(), out)
        runner = _FakeInstall(alive=0)
        out = cli_install.revert(runner, home=str(rev / "home"))
        check("cli-install: a WinGet copy that did not come back leaves the "
              "native install in place",
              not out.ok and (native_bin / "claude.exe").is_file(), out)
        (pkg / "claude.exe").write_text("winget", encoding="utf-8")
        runner = _FakeInstall(alive=0,
                              versions={str(pkg / "claude.exe"): "2.1.224"})
        out = cli_install.revert(runner, home=str(rev / "home"))
        check("cli-install: a verified revert puts WinGet back and removes the "
              "native install",
              out.ok and out.after == "2.1.224"
              and not (native_bin / "claude.exe").exists()
              and not share.parent.exists()
              and (pkg / "claude.exe").is_file(), out)
        check("cli-install: the revert is audited",
              cli_install.audit_lines(out) == ["CLI-MIGRATE-REVERT ok 2.1.224"])
    finally:
        for name, value in (("USERPROFILE", real_home),
                            ("LOCALAPPDATA", real_local)):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # --- 10. the live-spec rebuild (§5 step 5) ------------------------------
    store = SessionStore(path=tmp / "session.json")
    win = create_main_window(store)
    win._save_timer.stop()
    ws = win.manager.create_workspace("Migrate", str(tmp))
    real_resolve = providers.resolve_claude
    try:
        providers.resolve_claude = lambda: winget_exe
        spec = build_spec(AgentKind.CLAUDE, "C1", cwd=str(tmp), model="opus",
                          effort="high", permission_mode="plan")
        agent = win.manager.add_terminal(ws.id, spec, autostart=False)
        check("cli-install: a spec built before the migration carries the "
              "WinGet path",
              agent.spec.program == winget_exe, agent.spec.program)
        disturbed = []
        agent.start = lambda *a, **k: disturbed.append("start")
        agent.stop = lambda *a, **k: disturbed.append("stop")
        agent.restart = lambda *a, **k: disturbed.append("restart")
        providers.resolve_claude = lambda: native_exe
        win._save_timer.stop()
        moved = win.rebind_claude_specs()
        check("cli-install: the rebuild repoints every live Claude spec at the "
              "new binary",
              moved == 1 and agent.spec.program == native_exe,
              agent.spec.program)
        check("cli-install: ...preserving everything the user chose",
              agent.spec.model == "opus" and agent.spec.effort == "high"
              and agent.spec.permission_mode == "plan"
              and "--permission-mode" in agent.spec.args
              and agent.spec.user_program == "" and agent.spec.user_args == [])
        check("cli-install: ...without stopping or restarting a running agent",
              disturbed == [], disturbed)
        check("cli-install: ...and without marking the session dirty "
              "(program/args are derived, never persisted)",
              not win._save_timer.isActive()
              and "program" not in agent.spec.to_dict())
        check("cli-install: the rebuild is idempotent, so a second migration "
              "attempt is harmless",
              win.rebind_claude_specs() == 0)
    finally:
        providers.resolve_claude = real_resolve

    # --- 11. the panel: state in, one action out ----------------------------
    panel = UpdatePanel(situation(winget_exe), auto_update=False, runner=None,
                        winget_exe=winget_exe, settings_file=str(settings),
                        parent=win)
    check("cli-install panel: a winget machine is offered the migration, and "
          "the cleanup/revert controls that belong to a native install are "
          "not shown",
          panel.action_btn.text() == "Enable automatic updates"
          and not panel.revert_btn.isVisibleTo(panel)
          and not panel.cleanup_btn.isVisibleTo(panel))
    check("cli-install panel: with no runner armed it can show but not act",
          not panel.action_btn.isEnabled())
    check("cli-install panel: with no gate report it claims no check happened",
          not panel.last_check_label.isVisibleTo(panel)
          and panel.last_check_label.text() == "")
    strings = _dialog_strings(panel)
    panel.close()
    panel.deleteLater()

    # the splash is the most fleeting surface in the app: it closes itself and
    # leaves nothing behind, so this is the one place an outcome can be read
    # again afterwards
    reported = UpdatePanel(situation(winget_exe), runner=None,
                           winget_exe=winget_exe, settings_file=str(settings),
                           last_check="Claude Code: left to its own updater",
                           parent=win)
    check("cli-install panel: the startup gate's report is readable here long "
          "after the splash has gone",
          reported.last_check_label.isVisibleTo(reported)
          and "left to its own updater" in reported.last_check_label.text())
    reported.close()
    reported.deleteLater()

    blocker = threading.Event()
    released = []

    def never_returns(argv, timeout=0):
        released.append(list(argv))
        blocker.wait(10)
        return 0, ""

    native_panel = UpdatePanel(situation(native_exe), runner=never_returns,
                               winget_exe=winget_exe,
                               settings_file=str(settings), parent=win)
    check("cli-install panel: a native machine is offered pause, not another "
          "install, plus the two acts about the OLD copy",
          native_panel.action_btn.text() == "Pause automatic updates"
          and native_panel.revert_btn.isVisibleTo(native_panel)
          and native_panel.cleanup_btn.isVisibleTo(native_panel))
    # the install NEVER runs on the GUI thread: CLAUDE.md records what an
    # inline ~3.0s subprocess did to this app, and an install can run minutes
    started = time.time()
    native_panel._on_cleanup()
    check("cli-install panel: a command that never returns does not block the "
          "GUI thread, and the panel says so by disabling its actions",
          # the runner blocks 10 s: anything well under that was not inline
          time.time() - started < 5.0 and not native_panel.action_btn.isEnabled()
          and not native_panel.cleanup_btn.isEnabled())
    native_panel.close()      # Cancel/Close is the escape hatch, never a kill
    check("cli-install panel: closing mid-command stops the poll and claims "
          "no outcome",
          not native_panel._poll.isActive()
          and not native_panel.log.toPlainText().strip().endswith("removed."))
    blocker.set()
    native_panel.deleteLater()

    locked = UpdatePanel(situation(native_exe,
                                   policies=[{"requiredMinimumVersion": "1"}]),
                         runner=None, settings_file=str(settings), parent=win)
    check("cli-install panel: a policy-locked machine is offered nothing, "
          "with the reason shown",
          not locked.action_btn.isVisibleTo(locked)
          and "managed settings" in locked.state_label.text())
    locked.close()
    locked.deleteLater()

    consent = ConsentDialog(winget_exe, win)
    check("cli-install consent: the action is unavailable until the checkbox "
          "is ticked",
          not consent.action_btn.isEnabled())
    consent.agree.setChecked(True)
    check("cli-install consent: ticking it enables the action",
          consent.action_btn.isEnabled())
    check("cli-install consent: the exact command is shown, and both levels "
          "of undo are stated BEFORE agreeing",
          any("irm https://claude.ai/install.ps1 | iex" in s
              for s in _dialog_strings(consent))
          and any("Full revert" in s for s in _dialog_strings(consent))
          and any("rollback" in s for s in _dialog_strings(consent)))
    strings += _dialog_strings(consent)
    check("cli-install: no em dash in any panel or modal string",
          not any("\u2014" in s for s in strings),
          [s for s in strings if "\u2014" in s])
    consent.close()
    consent.deleteLater()
    check("cli-install: the panel writes nothing to session.json",
          "auto_update" in win._session_payload()["ui"]
          and not any(k.startswith("install") or k.startswith("cli_")
                      for k in win._session_payload()["ui"]),
          list(win._session_payload()["ui"]))
    win.close()
    app.processEvents()

    # --- 12. the factory the suite shares does none of this -----------------
    import inspect
    import main as main_module
    src = inspect.getsource(main_module.create_main_window)
    check("cli-install: create_main_window contains no install work at all",
          "cli_install" not in src and "arm_cli_install" not in src)
    check("cli-install: ...and the real runner is armed from main.py alone",
          "arm_cli_install" in inspect.getsource(main_module.main))


def _dialog_strings(widget) -> list:
    """Every user-visible string a dialog composes at runtime."""
    out = []
    for child in widget.findChildren(object):
        for attr in ("text", "toolTip"):
            fn = getattr(child, attr, None)
            if callable(fn):
                try:
                    out.append(str(fn()))
                except TypeError:
                    pass
    return out


def test_startup_update():
    """The launch pulls whatever merged on GitHub (self_update.startup_update,
    called by main.py before it imports the app). Real git against a temp
    origin and clone, never this clone: 0.28.2 sat unseen for hours because
    the user's folder was left on a merged branch and nothing pulled."""
    import subprocess
    from app import self_update

    root = Path(tempfile.mkdtemp(prefix="ai-hive-launchupd-"))

    def git(cwd, *args):
        res = subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t",
             "-c", "init.defaultBranch=main", "-c", "advice.detachedHead=0",
             *args], cwd=cwd, capture_output=True, text=True)
        return res.stdout.strip()

    def write(cwd, rel, text):
        p = Path(cwd) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(cwd, rel, text, msg):
        write(cwd, rel, text)
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", msg)

    def version(cwd):
        return self_update.read_installed_version(str(cwd))

    def branch(cwd):
        return git(cwd, "rev-parse", "--abbrev-ref", "HEAD")

    def setup(name):
        """origin with 0.1.0, the user's clone of it, and a dev clone that
        then merges 0.1.1 to origin's main."""
        base = root / name
        origin, user, dev = base / "origin.git", base / "user", base / "dev"
        base.mkdir()
        git(base, "init", "-q", "--bare", "-b", "main", str(origin))
        git(base, "clone", "-q", str(origin), str(dev))
        commit(dev, "app/__init__.py", '__version__ = "0.1.0"\n', "0.1.0")
        commit(dev, "notes.txt", "a\n", "notes")
        git(dev, "push", "-q", "origin", "main")
        git(base, "clone", "-q", str(origin), str(user))
        return origin, user, dev

    def merge_new_version(dev):
        commit(dev, "app/__init__.py", '__version__ = "0.1.1"\n', "0.1.1")
        git(dev, "push", "-q", "origin", "main")

    run = self_update.subprocess_runner

    _, user, dev = setup("plain")
    merge_new_version(dev)
    res = self_update.startup_update(run, str(user))
    check("launch-update: a clean main fast-forwards to what merged",
          res is not None and res.ok and version(user) == "0.1.1"
          and branch(user) == "main", (res, version(user)))
    check("launch-update: a folder already current does nothing",
          self_update.startup_update(run, str(user)) is None)

    # the 0.28.2 case: a session committed on a branch in the user's folder,
    # the same change reached main under another hash, and main moved on
    _, user, dev = setup("merged-branch")
    git(user, "switch", "-q", "-c", "fix/tray")
    commit(user, "notes.txt", "a\nfix\n", "fix: tray")
    write(user, "untracked.md", "keep me\n")
    commit(dev, "notes.txt", "a\nfix\n", "fix: tray")
    merge_new_version(dev)
    res = self_update.startup_update(run, str(user))
    check("launch-update: a clean branch whose commits are all on main "
          "switches back to main and updates",
          res is not None and res.ok and branch(user) == "main"
          and version(user) == "0.1.1", (res, branch(user), version(user)))
    check("launch-update: untracked files survive the switch",
          (user / "untracked.md").read_text(encoding="utf-8") == "keep me\n")

    _, user, dev = setup("unmerged-branch")
    git(user, "switch", "-q", "-c", "feat/wip")
    commit(user, "notes.txt", "a\nwip\n", "feat: wip")
    merge_new_version(dev)
    res = self_update.startup_update(run, str(user))
    check("launch-update: a branch with unmerged commits is left alone",
          res is not None and not res.ok and branch(user) == "feat/wip"
          and version(user) == "0.1.0"
          and "wip" in (user / "notes.txt").read_text(encoding="utf-8"),
          (res, branch(user)))

    # git cherry skips merges: a branch whose plain commits all reached main
    # but that holds its own merge (a conflict resolution) must stay put
    _, user, dev = setup("merge-commit")
    git(user, "switch", "-q", "-c", "fix/side")
    commit(user, "side.txt", "side\n", "fix: side")
    git(user, "switch", "-q", "-c", "fix/resolve", "main")
    commit(user, "notes.txt", "a\nresolve\n", "fix: resolve")
    git(user, "merge", "-q", "--no-ff", "-m", "merge side", "fix/side")
    commit(dev, "side.txt", "side\n", "fix: side")
    commit(dev, "notes.txt", "a\nresolve\n", "fix: resolve")
    merge_new_version(dev)
    res = self_update.startup_update(run, str(user))
    check("launch-update: a branch with its own merge commit is left alone",
          res is not None and not res.ok and branch(user) == "fix/resolve"
          and version(user) == "0.1.0", (res, branch(user)))

    _, user, dev = setup("dirty")
    git(user, "switch", "-q", "-c", "fix/done")
    commit(user, "notes.txt", "a\ndone\n", "fix: done")
    commit(dev, "notes.txt", "a\ndone\n", "fix: done")
    merge_new_version(dev)
    write(user, "notes.txt", "a\ndone\nunsaved\n")
    res = self_update.startup_update(run, str(user))
    check("launch-update: local changes block the switch and the update",
          res is not None and not res.ok and branch(user) == "fix/done"
          and "unsaved" in (user / "notes.txt").read_text(encoding="utf-8"),
          (res, branch(user)))

    _, user, dev = setup("dirty-main")
    merge_new_version(dev)
    write(user, "notes.txt", "a\nunsaved\n")
    res = self_update.startup_update(run, str(user))
    check("launch-update: local changes on main block the update",
          res is not None and not res.ok and version(user) == "0.1.0"
          and "unsaved" in (user / "notes.txt").read_text(encoding="utf-8"),
          res)

    origin, user, dev = setup("offline")
    git(user, "remote", "set-url", "origin", str(origin) + "-gone")
    res = self_update.startup_update(run, str(user))
    check("launch-update: a failed fetch is reported and changes nothing",
          res is not None and not res.ok and "Checking GitHub" in res.detail
          and version(user) == "0.1.0", res)

    # pulled code whose new packages failed to install would die on import
    # with no window to say why (GPT-6-Luna review, round 3)
    A = self_update.Applied
    broken = self_update.startup_failure(
        A(True, version="0.1.1", deps_changed=True, deps_ok=False))
    check("launch-update: a failed package install stops the launch with "
          "the command that fixes it",
          "v0.1.1" in broken and "pip install -r requirements.txt" in broken
          and "—" not in broken, broken)
    check("launch-update: nothing else stops the launch",
          not any(self_update.startup_failure(r) for r in (
              None, A(False, detail="blocked"), A(True, version="0.1.1"),
              A(True, version="0.1.1", deps_changed=True, deps_ok=True))))

    src = (ROOT / "main.py").read_text(encoding="utf-8")
    launched = src.find('if __name__ == "__main__":\n    _LAUNCH_GUARD = '
                        '_single_instance_guard()\n    if _LAUNCH_GUARD is '
                        'not None:\n')
    pull = src.find("startup_update(", max(launched, 0))
    check("launch-update: main.py pulls only when launched, and before it "
          "imports the app",
          0 < launched < pull < src.find("from app.process_worker import"),
          (launched, pull))
    # a second launch exits at the instance check; pulling first would
    # change files under the instance that is running (GPT-6-Luna review)
    check("launch-update: only the launch that holds the instance lock pulls, "
          "and main() reuses that lock",
          launched > 0 and "guard = _LAUNCH_GUARD or _single_instance_guard()"
          in src, launched)
    check("launch-update: main.py stops before importing the pulled app "
          "when its packages failed",
          pull < src.find("_fatal(_self_update.startup_failure(_result))")
          < src.find("from app.process_worker import"))
    shutil.rmtree(root, ignore_errors=True)
