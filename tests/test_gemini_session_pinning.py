"""Unit test for Gemini (Antigravity CLI) session pinning and resume in AI Hive."""
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.process_worker import AgentKind, build_spec
from app import session_sync
from app.workspace_manager import WorkspaceManager


class TestGeminiSessionPinning(unittest.TestCase):

    def test_effective_args_with_conversation(self):
        spec = build_spec(AgentKind.GEMINI, "GeminiAgent", cwd=str(ROOT), model="Gemini 3.6 Flash (High)")
        spec.session_id = "da3a8077-8c47-4e46-92a4-f40637e999ad"
        spec.resume = True

        args = spec.effective_args()
        self.assertIn("--conversation", args)
        idx = args.index("--conversation")
        self.assertEqual(args[idx + 1], "da3a8077-8c47-4e46-92a4-f40637e999ad")
        self.assertNotIn("--continue", args)

    def test_effective_args_fallback_to_continue(self):
        spec = build_spec(AgentKind.GEMINI, "GeminiAgent", cwd=str(ROOT))
        spec.session_id = ""
        spec.resume = True

        args = spec.effective_args()
        self.assertIn("--continue", args)
        self.assertNotIn("--conversation", args)

    def test_gemini_session_sync_discovery(self):
        live_sid = session_sync.get_gemini_live_session(str(ROOT))
        self.assertIsNotNone(live_sid)
        self.assertTrue(session_sync.is_session_id(live_sid))

    def test_workspace_manager_syncs_gemini(self):
        wm = WorkspaceManager()
        ws = wm.create_workspace("TestWS", str(ROOT))
        spec = build_spec(AgentKind.GEMINI, "GeminiAgent", cwd=str(ROOT))
        spec.session_id = ""
        agent = wm.add_terminal(ws.id, spec, autostart=False)
        self.assertTrue(bool(agent.spec.session_id))

    def test_gemini_provider_display_name_and_no_efforts(self):
        from app import providers
        prov = providers.get("gemini")
        self.assertIsNotNone(prov)
        self.assertEqual(prov.display, "Gemini CLI")
        self.assertEqual(prov.efforts, ())

    def test_gemini_token_usage_reading(self):
        from app import transcripts
        # use active session ID if present
        used, window = transcripts.latest_gemini_token_usage(str(ROOT), "fe9968f6-f1b2-4f19-be20-da0408eb4247")
        self.assertEqual(window, 1_000_000)
        self.assertGreater(used, 0)

    def test_gemini_ai_title_reading(self):
        import tempfile
        import json
        from app import transcripts

        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json") as tf:
            data = {
                "conversations": {
                    "test-session-123": {
                        "summary": {
                            "ID": "test-session-123",
                            "Title": "Custom Title",
                            "Preview": "Preview Text"
                        }
                    },
                    "test-session-456": {
                        "summary": {
                            "ID": "test-session-456",
                            "Title": "",
                            "Preview": "Preview Fallback"
                        }
                    }
                }
            }
            json.dump(data, tf)
            tmp_path = tf.name

        try:
            self.assertEqual(transcripts._read_gemini_ai_title(tmp_path, "test-session-123"), "Custom Title")
            self.assertEqual(transcripts._read_gemini_ai_title(tmp_path, "test-session-456"), "Preview Fallback")
            self.assertEqual(transcripts._read_gemini_ai_title(tmp_path, "nonexistent"), "")
        finally:
            os.remove(tmp_path)

    def test_workspace_manager_refreshes_gemini_ai_title(self):
        wm = WorkspaceManager()
        ws = wm.create_workspace("TestWS2", str(ROOT))
        spec = build_spec(AgentKind.GEMINI, "GeminiAgent", cwd=str(ROOT))
        spec.session_id = "682c52f8-47dd-4d3f-891b-98d696624029"
        agent = wm.add_terminal(ws.id, spec, autostart=False)
        wm.refresh_ai_titles()
        self.assertEqual(agent.summary(), "AI Hive Sync Issues")


if __name__ == "__main__":
    unittest.main()
