import tempfile
import unittest
from pathlib import Path

from tools import flow_log


class FlowLogTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_path = flow_log.DEFAULT_PATH
        flow_log.DEFAULT_PATH = Path(self.temp_dir.name) / "flow.jsonl"

    def tearDown(self):
        flow_log.DEFAULT_PATH = self.old_path
        self.temp_dir.cleanup()

    def test_after_id_returns_only_new_events(self):
        self.assertTrue(flow_log.emit(
            "client", "state", "info", trace_id="voice-1",
            message="state=listening",
        ))
        first_id = flow_log.read_events(limit=10)[0]["id"]
        flow_log.emit(
            "asr", "final", "ok", trace_id="voice-1",
            message="chars=4",
        )

        events = flow_log.read_events(limit=10, after_id=first_id)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["component"], "asr")

    def test_filters_and_health(self):
        flow_log.emit(
            "notify", "claimed", "info", trace_id="notify-9",
            notify_id=9,
        )
        flow_log.emit(
            "tts", "error", "error", trace_id="notify-9",
            notify_id=9, message="synthesis_failed",
        )

        events = flow_log.read_events(
            limit=10, notify_id=9, status="error",
        )
        health = flow_log.health()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["step"], "error")
        self.assertTrue(health["ok"])
        self.assertGreater(health["size"], 0)

    def test_nested_secret_fields_are_redacted(self):
        flow_log.emit(
            "server", "test",
            detail={"api_key": "not-a-real-key", "value": "visible"},
        )

        event = flow_log.read_events(limit=1)[0]

        self.assertEqual(event["detail"]["api_key"], "<redacted>")
        self.assertEqual(event["detail"]["value"], "visible")
        self.assertEqual(event["schema"], 1)

    def test_stage_progress_assets_cover_audio_and_status_colors(self):
        root = Path(__file__).resolve().parents[1] / "tools" / "flowview"
        script = (root / "flowview.js").read_text(encoding="utf-8")
        styles = (root / "flowview.css").read_text(encoding="utf-8")

        self.assertIn('event.step === "first_audio"', script)
        self.assertIn("is-reached", script)
        self.assertIn("is-current", script)
        self.assertIn("stagePulse", styles)
        self.assertIn("grid-auto-flow: column", styles)


if __name__ == "__main__":
    unittest.main()
