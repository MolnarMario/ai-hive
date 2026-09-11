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
        # Dynamically test against any existing Gemini DB or a synthetic DB
        live_sid = session_sync.get_gemini_live_session(str(ROOT))
        if live_sid:
            used, window = transcripts.latest_gemini_token_usage(str(ROOT), live_sid)
            self.assertIn(window, (1_000_000, 2_000_000))
            self.assertGreaterEqual(used, 0)
        else:
            used, window = transcripts.latest_gemini_token_usage(str(ROOT), "nonexistent")
            self.assertEqual(used, 0)

    def test_gemini_model_effort_parsing(self):
        from app import transcripts
        m, e = transcripts.parse_gemini_model_effort("Gemini 3.8 Flash (High)")
        self.assertEqual(m, "Gemini 3.8 Flash")
        self.assertEqual(e, "high")

        m, e = transcripts.parse_gemini_model_effort("Gemini 3.8 Flash (Low)")
        self.assertEqual(m, "Gemini 3.8 Flash")
        self.assertEqual(e, "low")

        m, e = transcripts.parse_gemini_model_effort("gemini-3.8-flash-medium")
        self.assertEqual(m, "Gemini 3.8 Flash")
        self.assertEqual(e, "medium")

        m, e = transcripts.parse_gemini_model_effort("Gemini 3.7 Flash (High)")
        self.assertEqual(m, "Gemini 3.7 Flash")
        self.assertEqual(e, "high")

        m, e = transcripts.parse_gemini_model_effort("gemini-3.7-flash-control")
        self.assertEqual(m, "Gemini 3.7 Flash")

        m, e = transcripts.parse_gemini_model_effort("Claude Sonnet 4.6 (Thinking)")
        self.assertEqual(m, "Claude Sonnet 4.6")
        self.assertEqual(e, "thinking")

    def test_gemini_build_spec_loads_effort(self):
        spec = build_spec(AgentKind.GEMINI, "Agent", model="Gemini 3.8 Flash (Low)")
        self.assertEqual(spec.model, "Gemini 3.8 Flash (Low)")
        self.assertEqual(spec.effort, "low")
        self.assertEqual(spec.args, ["--model", "Gemini 3.8 Flash (Low)"])

    def test_gemini_latest_models_list(self):
        from app import providers
        labels = [lbl for lbl, _ in providers.GEMINI_MODELS]
        self.assertIn("Gemini 3.8 Flash (High)", labels)
        self.assertIn("Gemini 3.8 Flash (Medium)", labels)
        self.assertIn("Gemini 3.8 Flash (Low)", labels)
        self.assertIn("Gemini 3.7 Flash (High)", labels)
        self.assertNotIn("Gemini 3.5 Flash (High)", labels)

    def test_gemini_permission_mode_display(self):
        from app import providers
        self.assertEqual(providers.gemini_permission_mode_display("accept-edits"), "auto")
        self.assertEqual(providers.gemini_permission_mode_display("always-proceed"), "bypass")
        self.assertEqual(providers.gemini_permission_mode_display("plan"), "plan")
        self.assertEqual(providers.gemini_permission_mode_display(""), "manual")

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
        finally:
            os.remove(tmp_path)

    def test_gemini_agent_header_badges(self):
        wm = WorkspaceManager()
        ws = wm.create_workspace("TestWS2", str(ROOT))
        spec = build_spec(AgentKind.GEMINI, "GeminiAgent", cwd=str(ROOT), model="Gemini 3.7 Flash (High)")
        live_sid = session_sync.get_gemini_live_session(str(ROOT))
        if live_sid:
            spec.session_id = live_sid
        agent = wm.add_terminal(ws.id, spec, autostart=False)
        # Verify model badge is properly seeded
        self.assertIn("Gemini 3.7 Flash", agent.model_badge())
        self.assertIn("high", agent.model_badge())

        wm.refresh_ai_titles()
        wm.refresh_model_effort()
        self.assertTrue(bool(agent.model_badge()))


if __name__ == "__main__":
    unittest.main()
