from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import sys

if sys.platform == "win32":
    from codex_widget import windows_app as app_module
else:
    from codex_widget import app as app_module

CodexWidgetApplication = app_module.CodexWidgetApplication
APP = app_module.__name__
from codex_widget.models import AppState, UsageSnapshot, utc_now
from codex_widget.state import StateStore


class ProviderTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        with patch(f"{APP}.detect_claude", return_value="/test/claude"):
            self.app = CodexWidgetApplication()
        self.app.state_store = StateStore(Path(directory.name) / "state.json")
        self.app.claude_keeper.state_store = StateStore(Path(directory.name) / "claude-state.json")
        self.app.selected_provider = "codex"
        self.app.window = Mock()
        self.app.window.get_visible.return_value = True
        self.app.show_widget = Mock()
        self.usage = UsageSnapshot(
            used_percent=3, reset_at=utc_now() + timedelta(days=5),
            window_minutes=10080, banked_resets=None,
            five_hour_used_percent=2, five_hour_reset_at=utc_now() + timedelta(hours=5),
            five_hour_window_minutes=300,
        )

    def test_header_switch_persists_provider_and_toggles_back(self):
        with patch(f"{APP}.detect_claude", return_value="/test/claude"):
            self.app.toggle_provider()
            self.assertEqual(self.app.selected_provider, "claude")
            self.assertEqual(self.app.state_store.load().selected_provider, "claude")
            self.app.toggle_provider()
        self.assertEqual(self.app.selected_provider, "codex")
        self.assertEqual(self.app.show_widget.call_count, 2)

    def test_switch_is_disabled_without_claude(self):
        with patch(f"{APP}.detect_claude", return_value=None):
            self.app.toggle_provider()
        self.app.show_widget.assert_not_called()
        self.assertEqual(self.app.selected_provider, "codex")

    def test_late_codex_reply_does_not_overwrite_claude_view(self):
        self.app.selected_provider = "claude"
        self.app._usage_refreshing = True
        self.app._usage_refresh_finished(self.usage, None)
        self.app.window.show_usage.assert_not_called()
        self.app.window.show_error.assert_not_called()
        self.assertFalse(self.app._usage_refreshing)

    def test_late_claude_reply_does_not_overwrite_codex_view(self):
        self.app._claude_refreshing = True
        self.app._claude_usage_finished(self.usage, None)
        self.app.window.show_usage.assert_not_called()
        self.assertFalse(self.app._claude_refreshing)

    def test_codex_tracker_error_does_not_pollute_claude_status(self):
        self.app.selected_provider = "claude"
        self.app._poll_finished(None, RuntimeError("tracker offline"))
        self.app.window.status.set_text.assert_not_called()

    def test_auto_roll_settings_are_independent_and_default_off_for_claude(self):
        self.app._start_window_keeper_watchdog = Mock()
        self.app._refresh_window_keeper_schedule = Mock()
        self.app.claude_keeper._start_window_keeper_watchdog = Mock()
        self.app.claude_keeper._refresh_window_keeper_schedule = Mock()
        self.app.set_window_keeper_enabled(True)
        self.assertTrue(self.app.state_store.load().keep_five_hour_window_active)
        self.assertFalse(self.app.claude_keeper.state_store.load().keep_five_hour_window_active)
        self.app.selected_provider = "claude"
        self.app.set_window_keeper_enabled(True)
        self.assertTrue(self.app.claude_keeper.state_store.load().keep_five_hour_window_active)
        self.app.set_window_keeper_enabled(False)
        self.assertTrue(self.app.state_store.load().keep_five_hour_window_active)
        self.assertFalse(self.app.claude_keeper.state_store.load().keep_five_hour_window_active)

    def test_claude_verification_uses_three_fresh_samples_and_separate_store(self):
        keeper = self.app.claude_keeper
        keeper.state_store.save(AppState(keep_five_hour_window_active=True))
        keeper.codex = Mock()
        keeper.codex.read_rate_limits.return_value = self.usage
        with patch("codex_widget.window_keeper.time.sleep"):
            self.assertEqual(keeper._verify_window_keeper(), self.usage)
        self.assertEqual(keeper.codex.read_rate_limits.call_count, 3)
        for call in keeper.codex.read_rate_limits.call_args_list:
            self.assertTrue(call.kwargs["force"])
        self.assertEqual(keeper.state_store.load().last_known_usage, self.usage)
        self.assertIsNone(self.app.state_store.load().last_known_usage)
        self.assertFalse(keeper._verifying)

    def test_observed_claude_reset_is_persisted_and_notified_once(self):
        keeper = self.app.claude_keeper
        before = UsageSnapshot(
            used_percent=3, reset_at=self.usage.reset_at, window_minutes=10080,
            banked_resets=None, checked_at=utc_now() - timedelta(minutes=5),
            five_hour_used_percent=50,
        )
        keeper.state_store.save(AppState(last_known_usage=before))
        keeper.codex = Mock()
        keeper.codex.read_rate_limits.return_value = self.usage
        with patch("codex_widget.window_keeper.GLib.idle_add") as notify:
            keeper._read_and_store_usage_unlocked()
            keeper._read_and_store_usage_unlocked()
        self.assertEqual(notify.call_count, 1)
        self.assertIn("five-hour", keeper.state_store.load().last_global_reset.summary)
        self.assertIsNone(self.app.state_store.load().last_global_reset)

    def test_codex_keeper_cannot_replace_claude_controls(self):
        self.app.selected_provider = "claude"
        self.app._set_window_keeper_message("Codex schedule")
        self.app.window.set_window_keeper_state.assert_not_called()
        self.app.claude_keeper._set_window_keeper_message("Claude schedule")
        self.app.window.set_window_keeper_state.assert_called_once_with(False, "Claude schedule")


if __name__ == "__main__":
    unittest.main()
