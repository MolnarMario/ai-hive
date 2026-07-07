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
    """Render the hive hexagon into an icon (taskbar / window chrome)."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPixmap

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
                    agent.notice("— resuming previous session… —")
                else:
                    agent.notice("— session restored — press any key "
                                 "or Start (▶) —")
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


def main() -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    guard = _single_instance_guard()  # noqa: F841 — held until process exit
    if guard is None:
        _fatal("AI Hive is already running.\n\nOnly one instance can run at a "
               "time (a second would overwrite the first's saved session). "
               "Use the window that's already open.")
    setup_application(app)
    window = create_main_window()
    window.quit_on_close = True  # closed window == dead process, always
    window.show()
    window.autostart_active_workspace()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
