"""AI Hive — multi-agent control center.

Run with the project venv (or double-click "AI Hive.bat" / "AI Hive.lnk"):
    .venv\\Scripts\\python.exe main.py
"""

import os
import sys


def _fatal(message: str) -> None:
    """Report a startup failure even without a console (pythonw launch)."""
    print(message, file=sys.stderr)
    if sys.platform == "win32" and not os.environ.get("AI_HIVE_NO_MSGBOX"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "AI Hive",
                                             0x10)  # MB_ICONERROR
        except Exception:
            pass
    sys.exit(1)


try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QFontDatabase, QGuiApplication
    from PySide6.QtWidgets import QApplication
except ImportError:
    _fatal(
        "PySide6 is not installed for this Python interpreter.\n\n"
        "This happens when main.py is opened with the system Python "
        "(e.g. by double-clicking it) instead of the project's .venv.\n\n"
        "Launch AI Hive with \"AI Hive.bat\" (or the \"AI Hive\" shortcut) "
        "in this folder, or run:\n"
        "    .venv\\Scripts\\python.exe main.py\n\n"
        "To (re)install dependencies:\n"
        "    .venv\\Scripts\\python.exe -m pip install -r requirements.txt")

from app.process_worker import AgentKind, build_spec
from app.pty_worker import HAS_CONPTY
from app.session_store import SessionStore
from app.ui_theme import build_qss
from app.widgets.main_window import MainWindow
from app.workspace_manager import WorkspaceManager


def _app_icon():
    """Window/taskbar icon: the bundled multi-resolution .ico (baked from
    app/assets/icons/app_logo.png by generate_app_icon.py) so the SAME
    artwork appears in the title bar, the taskbar while running, and — once
    the shortcut's IconLocation points at it too — the pinned taskbar slot.
    Falls back to the raw source PNG, then a runtime-painted placeholder, if
    the .ico asset hasn't been (re)generated yet."""
    from PySide6.QtGui import QIcon

    base_dir = os.path.dirname(os.path.abspath(__file__))
    ico_path = os.path.join(base_dir, "app", "assets", "icons", "app_icon.ico")
    if os.path.isfile(ico_path):
        return QIcon(ico_path)

    logo_path = os.path.join(base_dir, "app", "assets", "icons", "app_logo.png")
    if os.path.isfile(logo_path):
        return QIcon(logo_path)

    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

    from app.ui_theme import Palette

    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("transparent"))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(Palette.BG_PANEL))
    painter.setPen(QColor(Palette.BORDER))
    painter.drawRoundedRect(QRectF(1, 1, 62, 62), 12, 12)
    painter.setPen(QColor(Palette.ACCENT_ORANGE))
    painter.setFont(QFont("Segoe UI Symbol", 34, 700))
    painter.drawText(pixmap.rect(), 0x84, "⬡")  # AlignCenter
    painter.end()
    return QIcon(pixmap)


def _load_bundled_fonts() -> None:
    """Register the manuscript fonts (Cinzel/EB Garamond/Spectral) so themes
    that name them resolve exactly instead of falling back to a system serif.
    Best-effort: missing files just mean the QSS fallback chain kicks in."""
    from PySide6.QtGui import QFontDatabase
    fonts_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "app", "assets", "fonts")
    if not os.path.isdir(fonts_dir):
        return
    for name in os.listdir(fonts_dir):
        if name.lower().endswith((".ttf", ".otf")):
            QFontDatabase.addApplicationFont(os.path.join(fonts_dir, name))


def setup_application(app: QApplication) -> None:
    app.setOrganizationName("AIHive")
    app.setApplicationName("AI Hive")
    app.setStyle("Fusion")  # stable base for QSS on Windows
    app.setWindowIcon(_app_icon())
    _load_bundled_fonts()
    chrome = "Inter" if "Inter" in QFontDatabase.families() else "Segoe UI"
    app.setProperty("chromeFamily", chrome)  # reused by live font rescaling
    app.setStyleSheet(build_qss(chrome))


def create_main_window(store: SessionStore | None = None) -> MainWindow:
    """Factory used by both __main__ and the offscreen smoke test."""
    store = store or SessionStore()
    manager = WorkspaceManager()
    session = store.load()
    manager.load_session_dict(session)

    # snapshot every pinned conversation BEFORE any agent launches: whatever
    # a resume might do to Claude's one-and-only transcript file, the copy
    # taken here survives it (a concurrent resume once truncated one)
    from app import transcripts
    transcripts.backup_for_agents(
        manager.all_agents(), str(store.path.parent / "transcripts"))

    # ...and give every restored pty agent back the screen it had at close,
    # BEFORE the window builds its cards: TerminalCard replays pty_replay()
    # in its constructor, so seeding after this point would leave the launch
    # cards blank and only paint on a later retile. An agent about to
    # autostart is seeded too — its conversation is on screen from the first
    # frame instead of after the TUI finishes drawing.
    from app import screen_snapshot
    for agent in manager.all_agents():
        if getattr(agent, "is_pty", False):
            agent.seed_pty_replay(screen_snapshot.load(
                str(store.path.parent), agent.spec.cwd, agent.spec.session_id))

    first_run = not manager.workspaces
    if first_run:
        ws = manager.create_workspace("Workspace 1")
        manager.set_active(ws.id)

    window = MainWindow(manager, store, session=session)

    if first_run:
        agent = manager.add_terminal(
            ws.id, build_spec(AgentKind.POWERSHELL, "Agent 1",
                              cwd=ws.project_path, pty=HAS_CONPTY),
            autostart=False)
        agent.autostart_on_restore = True
    else:
        from app.providers import RESUME_PROVIDERS
        for ws in manager.workspaces:
            for agent in ws.agents:
                # everything that was running comes back in EVERY workspace,
                # and every restored Claude/Gemini agent reclaims its prior
                # conversation (--continue) on its NEXT start — whether that
                # is the launch autostart or a later press-any-key wake.
                # resume is one-shot: consumed at first start, so a manual
                # restart after that is a deliberate fresh session.
                if agent.spec.provider in RESUME_PROVIDERS:
                    agent.spec.resume = True
                    # this resume is a RESTORE: verify the pinned conversation
                    # still exists and recover it if a stale/never-used id
                    # would otherwise make --resume error on a dead terminal
                    agent._verify_resume_target = True
                if agent.autostart_on_restore:
                    agent.notice("[resuming previous session…]")
                else:
                    agent.notice("[session restored; press any key to start]")
    return window


def _single_instance_guard(
        name: str = "Local\\ai-hive-single-instance") -> "object | None":
    """Return a held handle if we're the only instance, else None.

    A second instance would race the session file (last writer wins) and
    silently clobber agents the running instance hasn't saved yet — this is
    how workspace agents have been lost. A kernel named mutex is atomic:
    no probe timeout to race, no listen() result to forget to check, and the
    OS destroys it automatically when the owning process dies (no stale-lock
    state after a crash). Fails CLOSED: if exclusivity can't be proven, we
    refuse to run rather than risk the user's session."""
    if sys.platform != "win32":
        return object()  # non-Windows dev run; no mutex namespace
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, False, name)
    already = kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS
    if not handle:
        return None  # can't verify exclusivity -> fail closed
    if already:
        kernel32.CloseHandle(handle)
        return None  # another instance holds it
    return handle  # held (never closed) for the life of this process


def _set_app_user_model_id() -> None:
    """Give the process its own AppUserModelID (AUMID) instead of inheriting
    the shared 'python'/'pythonw' one. Windows taskbar pinning/grouping keys
    off this id: without an explicit one, a pinned shortcut and the running
    process don't reliably match, so launching opens a SEPARATE unmerged
    taskbar icon (to the left of the pinned one) instead of attaching to the
    pinned slot the way Chrome/Claude do (they set this at install time).
    Must run before any window is created — first thing in main()."""
    if sys.platform != "win32":
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "AIHive.DesktopApp")
    except Exception:
        pass


def _register_relaunch_properties(hwnd: int) -> None:
    """Stamp the main window with its own relaunch command/icon/name via the
    Windows property store, so ANY future 'Pin to taskbar' — including one
    done by right-clicking the running window, which is how a user actually
    pins it — captures the right thing.

    Without this, Windows builds a pin by inspecting the OWNING PROCESS
    image directly. Since Python 3.13's venv `pythonw.exe` is a lightweight
    redirector that re-execs the base interpreter as a CHILD process, the
    window's owning process is the BASE interpreter with no arguments —
    Windows pinned a shortcut straight to `pythonw.exe` with no `main.py`
    argument and no icon (verified live: it produced a 'Python.lnk' pin that
    doesn't even relaunch AI Hive, and periodically got shown INSTEAD of the
    live window's icon, which is what made the taskbar icon appear to
    randomly revert to the generic Python icon mid-session even though the
    live window's own HICON never changed). Setting these RelaunchCommand/
    RelaunchIconResource/RelaunchDisplayNameResource properties gives
    Windows an explicit answer instead of guessing from process ancestry —
    the same mechanism installed apps like Chrome set up at install time.
    Best-effort: requires pywin32 and Vista+ shell APIs, never fatal."""
    if sys.platform != "win32":
        return
    try:
        from win32com.propsys import propsys

        base_dir = os.path.dirname(os.path.abspath(__file__))
        venv_pythonw = os.path.join(base_dir, ".venv", "Scripts", "pythonw.exe")
        icon_path = os.path.join(base_dir, "app", "assets", "icons",
                                 "app_icon.ico")
        if not os.path.isfile(venv_pythonw):
            return  # not the documented launch layout; nothing safe to set

        main_py = os.path.join(base_dir, "main.py")
        relaunch_command = f'"{venv_pythonw}" "{main_py}"'
        relaunch_icon = f"{icon_path},0" if os.path.isfile(icon_path) else \
            f"{venv_pythonw},0"

        store = propsys.SHGetPropertyStoreForWindow(hwnd)
        for name, value in (
            ("System.AppUserModel.ID", "AIHive.DesktopApp"),
            ("System.AppUserModel.RelaunchCommand", relaunch_command),
            ("System.AppUserModel.RelaunchDisplayNameResource", "AI Hive"),
            ("System.AppUserModel.RelaunchIconResource", relaunch_icon),
        ):
            key = propsys.PSGetPropertyKeyFromName(name)
            store.SetValue(key, propsys.PROPVARIANTType(value))
        store.Commit()
    except Exception:
        pass


def main() -> int:
    _set_app_user_model_id()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    guard = _single_instance_guard()  # noqa: F841 — held until process exit
    if guard is None:
        _fatal("AI Hive is already running.\n\nOnly one instance can run at a "
               "time (a second would overwrite the first's saved session). "
               "Use the window that's already open.")
    setup_application(app)
    # ---- CLI auto-update gate (opt-in, before anything else exists) --------
    # Windows cannot overwrite a running .exe, and every agent IS a claude.exe
    # child held alive by the Job Object, so THIS is the only moment those
    # binaries are unlocked: above create_main_window, and far above
    # autostart_active_workspace. Opting in here rather than in the factory is
    # the same rule as start_usage_polling / recover_blocked_at_startup: the
    # offscreen suite shares the factory and must never shell out.
    store = SessionStore()
    session = store.load()
    gate = None
    if session.get("ui", {}).get("auto_update", False):
        from app.widgets.update_splash import run_update_gate
        gate = run_update_gate(store)
    window = create_main_window(store)
    window.quit_on_close = True  # closed window == dead process, always
    # opt in to the plan-usage readout here, not in the factory: the smoke
    # suite shares create_main_window and must never hit the network
    window.start_usage_polling()
    if gate is not None:
        window.note_update_outcomes(gate.outcomes, gate.installing)
    window.show()
    _register_relaunch_properties(int(window.winId()))
    window.autostart_active_workspace()
    # ...then look for agents a spent plan limit stopped BEFORE this run and
    # arm them to be resumed. Follows the autostart so the ordinary restore
    # happens first and this only has to start the stragglers — an agent the
    # LIMIT stopped is started here even if the user's card was left stopped,
    # since it was not the user who stopped it. Reads the user's real
    # transcripts and types into real agents, so like the usage poll it is
    # opted into here rather than in the factory the smoke suite shares.
    window.recover_blocked_at_startup()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
