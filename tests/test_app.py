from __future__ import annotations

import unittest
from unittest.mock import patch
from types import SimpleNamespace

from codex_widget.app import CodexWidgetApplication
from codex_widget.models import ResetEvent, utc_now


class FakeApplication:
    def __init__(self):
        self.notifications = []

    def _send_reset_notification(self, event, body, *, final):
        self.notifications.append((event, body, final))

    _finish_reset_demo = CodexWidgetApplication._finish_reset_demo


class NotificationApplication:
    def __init__(self):
        self._notification_ids = {}

    def send_notification(self, *_args):
        raise AssertionError("Gio fallback should not be used")


class ResetDemoTests(unittest.TestCase):
    def test_demo_sends_announcement_then_confirmation(self):
        application = FakeApplication()

        with patch("codex_widget.app.GLib.timeout_add_seconds") as schedule:
            CodexWidgetApplication.show_reset_demo(application)

        event, body, final = application.notifications[0]
        self.assertEqual(event.event_id, "demo")
        self.assertIn("DEMO • 🙏", body)
        self.assertFalse(final)
        schedule.assert_called_once_with(4, application._finish_reset_demo, event)

        self.assertFalse(application._finish_reset_demo(event))
        self.assertEqual(len(application.notifications), 2)
        _, final_body, is_final = application.notifications[1]
        self.assertIn("DEMO • ✅", final_body)
        self.assertTrue(is_final)

    def test_native_notification_replaces_announcement_with_confirmation(self):
        application = NotificationApplication()
        event = ResetEvent("tweet-1", utc_now(), "Reset", "")

        with patch(
            "codex_widget.app.subprocess.run",
            side_effect=[
                SimpleNamespace(stdout="77\n"),
                SimpleNamespace(stdout="77\n"),
            ],
        ) as run:
            CodexWidgetApplication._send_reset_notification(
                application, event, "Checking account", final=False
            )
            CodexWidgetApplication._send_reset_notification(
                application, event, "Account reset", final=True
            )

        first_arguments = run.call_args_list[0].args[0]
        final_arguments = run.call_args_list[1].args[0]
        self.assertIn("--print-id", first_arguments)
        self.assertNotIn("--replace-id=77", first_arguments)
        self.assertIn("--replace-id=77", final_arguments)
        self.assertEqual(first_arguments[-2], "🔥 Codex reset announced")
        self.assertEqual(final_arguments[-2], "🔥 Codex reset")
        self.assertEqual(application._notification_ids, {})


if __name__ == "__main__":
    unittest.main()
