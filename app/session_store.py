"""Session persistence: atomic JSON in the per-user app-data folder."""

import datetime
import json
import os
import shutil
from pathlib import Path

from PySide6.QtCore import QStandardPaths


class SessionStore:
    def __init__(self, path: Path | None = None):
        if path is None:
            base = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation)
            path = Path(base) / "session.json"
        self.path = Path(path)

    def load(self) -> dict:
        try:
            # utf-8-sig tolerates a leading BOM (e.g. if the file was ever
            # written by a BOM-adding editor/tool) — a BOM must never make us
            # treat a valid session as corrupt and wipe the user's workspaces
            with open(self.path, encoding="utf-8-sig") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except FileNotFoundError:
            pass
        except (json.JSONDecodeError, OSError) as e:
            # keep the corrupt file for post-mortem, start fresh
            self._audit(f"LOAD-FAIL {type(e).__name__}: {e} -> renamed to .bak")
            try:
                self.path.replace(self.path.with_suffix(".json.bak"))
            except OSError:
                pass
        return {}

    def save(self, data: dict) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            # rolling previous-generation copy (copy, not rename: a crash
            # between two renames could leave NO session.json at all). Any
            # bad write — app bug or an external tool editing the file — is
            # recoverable from session.json.1.
            if self.path.exists():
                shutil.copy2(self.path, self.path.with_suffix(".json.1"))
            os.replace(tmp, self.path)
            wss = data.get("workspaces") or []
            self._audit("SAVE ws=%d terminals=%s"
                        % (len(wss), [len(w.get("terminals") or [])
                                      for w in wss]))
            return True
        except (OSError, TypeError, ValueError) as e:
            # a save that fails SILENTLY is how agents vanish without a trace
            # in the log — record the failure itself (an instance once
            # stopped saving and nothing showed why)
            self._audit(f"SAVE-FAIL {type(e).__name__}: {e}")
            return False

    def audit(self, message: str) -> None:
        """Public forensic line for callers upstream of save() (e.g. the
        MainWindow): a save that is SUPPRESSED or fails while BUILDING its
        payload never reaches save()'s own try/except, so without this it
        would leave no trace at all — which is precisely how agents have
        vanished silently before."""
        self._audit(message)

    def _audit(self, message: str) -> None:
        """One forensic line per save / load-failure (timestamp + pid), so a
        clobbered or wiped session is attributable after the fact. Best-effort
        and size-capped."""
        try:
            log = self.path.with_suffix(".log")
            if log.exists() and log.stat().st_size > 256 * 1024:
                log.replace(log.with_suffix(".log.1"))  # keep one generation
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(log, "a", encoding="utf-8") as f:
                f.write(f"{stamp} pid={os.getpid()} {message}\n")
        except OSError:
            pass
