import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys

if sys.platform == "win32":
    from codex_widget import windows_app as app_module
else:
    from codex_widget import app as app_module

CodexWidgetApplication = app_module.CodexWidgetApplication
APP = app_module.__name__
from codex_widget.models import AppState, UsageSnapshot
from codex_widget.codex_client import CodexClientError

class ActivationVerificationTests(unittest.TestCase):
    def verify(self, offsets, enabled=None):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        samples = [
            UsageSnapshot(
                0, None, 10080, 0,
                five_hour_reset_at=None if offset is None else now + timedelta(seconds=offset),
                five_hour_used_percent=0,
            )
            for offset in offsets
        ]
        app = SimpleNamespace(
            _window_keeper_enabled=Mock(side_effect=enabled) if enabled else lambda: True,
            _read_and_store_usage_unlocked=Mock(side_effect=samples),
        )
        with patch('codex_widget.window_keeper.time.sleep'), patch('codex_widget.window_keeper.utc_now', return_value=now):
            return CodexWidgetApplication._verify_window_keeper(app)

    def test_fixed_reset_accepts_rounded_zero_usage(self):
        self.assertIsNotNone(self.verify([18000, 18001, 18000]))

    def test_sliding_reset_is_not_success(self):
        with self.assertRaisesRegex(CodexClientError, 'reset moved 30s'):
            self.verify([18000, 18015, 18030])

    def test_missing_reset_is_not_success(self):
        with self.assertRaisesRegex(CodexClientError, 'no future reset'):
            self.verify([18000, None, 18000])

    def test_expired_reset_is_not_success(self):
        with self.assertRaisesRegex(CodexClientError, 'no future reset'):
            self.verify([0, 0, 0])

    def test_disable_during_verification_cancels_confirmation(self):
        self.assertIsNone(self.verify([18000], enabled=[True, False]))


    def test_three_unconfirmed_attempts_enter_persisted_cooldown(self):
        now = datetime(2026, 9, 8, tzinfo=UTC)
        app = CodexWidgetApplication()
        state = AppState(keep_five_hour_window_active=True)
        app.state_store = SimpleNamespace(load=lambda: state, save=lambda value: None)
        app.codex = SimpleNamespace(activate_five_hour_window=Mock())
        app._read_and_store_usage_unlocked = Mock(side_effect=[
            UsageSnapshot(0, None, 10080, 0, five_hour_reset_at=now + timedelta(seconds=offset))
            for offset in [18000, 18015, 18030] * 3
        ])
        with patch('codex_widget.window_keeper.time.sleep'), patch('codex_widget.window_keeper.utc_now', return_value=now), patch('codex_widget.window_keeper.GLib.source_remove'), patch('codex_widget.window_keeper.GLib.timeout_add_seconds', return_value=1) as timer:
            for _ in range(3):
                try:
                    app._activate_and_read_usage()
                except CodexClientError as error:
                    app._window_keeper_activation_finished(None, error)
            self.assertEqual(app.codex.activate_five_hour_window.call_count, 3)
            self.assertEqual(state.window_keeper_failures, 3)
            self.assertIsNone(state.last_window_keeper_verified_at)
            self.assertEqual(state.next_window_keeper_retry_at, now + timedelta(hours=5))
            self.assertEqual(timer.call_args.args[0], 18000)