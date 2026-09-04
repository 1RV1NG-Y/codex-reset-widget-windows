from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import GLib

from codex_widget.app import CodexWidgetApplication
from codex_widget.models import ResetEvent, UsageSnapshot, utc_now
from codex_widget.state import StateStore
from codex_widget.watcher import PollOutcome


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


class RefreshWindow:
    def __init__(self, visible=True):
        self.visible = visible

    def get_visible(self):
        return self.visible


class RefreshApplication:
    _usage_refresh_tick = CodexWidgetApplication._usage_refresh_tick
    _refresh_visible_usage = CodexWidgetApplication._refresh_visible_usage

    def __init__(self, visible=True):
        self.window = RefreshWindow(visible)
        self._usage_refresh_source = None
        self._usage_refreshing = False
        self.async_calls = []

    def _read_and_store_usage(self):
        raise AssertionError("work should remain asynchronous")

    def _usage_refresh_finished(self, _usage, _error):
        pass

    def _run_async(self, work, done):
        self.async_calls.append((work, done))


class WindowKeeperApplication:
    _schedule_window_keeper_from_usage = (
        CodexWidgetApplication._schedule_window_keeper_from_usage
    )
    _schedule_window_keeper_at = CodexWidgetApplication._schedule_window_keeper_at
    _cancel_window_keeper_timer = CodexWidgetApplication._cancel_window_keeper_timer
    _schedule_window_keeper_retry = (
        CodexWidgetApplication._schedule_window_keeper_retry
    )
    _window_keeper_activation_finished = (
        CodexWidgetApplication._window_keeper_activation_finished
    )
    set_window_keeper_enabled = CodexWidgetApplication.set_window_keeper_enabled

    def __init__(self, state_store=None):
        self.enabled = True
        self._window_keeper_source = None
        self._window_keeper_message = ""
        self._window_keeper_busy = False
        self._state_lock = threading.Lock()
        self.state_store = state_store
        self.messages = []
        self.refreshes = 0
        self.cancellations = 0

    def _window_keeper_enabled(self):
        return self.enabled

    def _window_keeper_timer_fired(self):
        return GLib.SOURCE_REMOVE


    def _window_keeper_retry_fired(self):
        return GLib.SOURCE_REMOVE
    def _set_window_keeper_message(self, message):
        self._window_keeper_message = message
        self.messages.append(message)

    def _refresh_window_keeper_schedule(self):
        self.refreshes += 1

    def _cancel_window_keeper_timer(self):
        self.cancellations += 1
        CodexWidgetApplication._cancel_window_keeper_timer(self)


def usage_snapshot(
    *,
    weekly_used=25,
    weekly_reset=None,
    five_hour_reset=None,
):
    return UsageSnapshot(
        used_percent=weekly_used,
        reset_at=weekly_reset,
        window_minutes=10080,
        banked_resets=0,
        five_hour_used_percent=1,
        five_hour_reset_at=five_hour_reset,
        five_hour_window_minutes=300,
    )


class WindowKeeperTests(unittest.TestCase):
    def test_active_window_schedules_tiny_request_after_its_reset(self):
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()
        usage = usage_snapshot(five_hour_reset=now + timedelta(hours=1))

        with (
            patch("codex_widget.app.utc_now", return_value=now),
            patch(
                "codex_widget.app.GLib.timeout_add_seconds",
                return_value=72,
            ) as schedule,
        ):
            application._schedule_window_keeper_from_usage(usage)

        schedule.assert_called_once_with(3610, application._window_keeper_timer_fired)
        self.assertEqual(application._window_keeper_source, 72)
        self.assertIn("next tiny request", application.messages[-1])

    def test_weekly_limit_pauses_activation_until_weekly_reset(self):
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()
        usage = usage_snapshot(
            weekly_used=100,
            weekly_reset=now + timedelta(hours=2),
            five_hour_reset=now + timedelta(hours=1),
        )

        with (
            patch("codex_widget.app.utc_now", return_value=now),
            patch(
                "codex_widget.app.GLib.timeout_add_seconds",
                return_value=73,
            ) as schedule,
        ):
            application._schedule_window_keeper_from_usage(usage)

        schedule.assert_called_once_with(7210, application._window_keeper_timer_fired)
        self.assertIn("weekly limit", application.messages[-1])

    def test_missing_active_window_schedules_immediate_activation(self):
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()

        with (
            patch("codex_widget.app.utc_now", return_value=now),
            patch(
                "codex_widget.app.GLib.timeout_add_seconds",
                return_value=74,
            ) as schedule,
        ):
            application._schedule_window_keeper_from_usage(usage_snapshot())

        schedule.assert_called_once_with(1, application._window_keeper_timer_fired)
        self.assertIn("activating an idle window", application.messages[-1])

    def test_toggle_persists_opt_in_and_starts_or_cancels_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            application = WindowKeeperApplication(store)

            application.set_window_keeper_enabled(True)
            self.assertTrue(store.load().keep_five_hour_window_active)
            self.assertEqual(application.refreshes, 1)

            application.set_window_keeper_enabled(False)
            self.assertFalse(store.load().keep_five_hour_window_active)
            self.assertEqual(application.cancellations, 1)
            self.assertEqual(
                application.messages[-1],
                "Automatic 5-hour rolling is off",
            )

    def test_activation_failure_surfaces_error_and_retries_in_five_minutes(self):
        application = WindowKeeperApplication()

        with patch(
            "codex_widget.app.GLib.timeout_add_seconds",
            return_value=75,
        ) as schedule:
            application._window_keeper_activation_finished(
                None,
                RuntimeError("offline"),
            )

        schedule.assert_called_once_with(
            300,
            application._window_keeper_retry_fired,
        )
        self.assertEqual(application._window_keeper_source, 75)
        self.assertIn("offline", application.messages[-1])


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

class PollCompletionTests(unittest.TestCase):
    def test_historical_catch_up_updates_without_notification(self):
        application = SimpleNamespace(
            _polling=True,
            window=None,
            _send_reset_notification=lambda *_args, **_kwargs: self.fail(
                "historical reset should not notify"
            ),
        )
        event = ResetEvent(
            "catch-up",
            datetime(2026, 8, 11, tzinfo=UTC),
            "Reset",
            "",
        )

        with patch(
            "codex_widget.app.utc_now",
            return_value=datetime(2026, 8, 23, tzinfo=UTC),
        ):
            CodexWidgetApplication._poll_finished(
                application,
                (PollOutcome.NEW_RESET, event, None),
                None,
            )

        self.assertFalse(application._polling)


class UsageRefreshTests(unittest.TestCase):
    def test_visible_widget_refreshes_immediately_without_overlap(self):
        application = RefreshApplication()

        with patch(
            "codex_widget.app.GLib.timeout_add_seconds",
            return_value=41,
        ) as schedule:
            CodexWidgetApplication._start_usage_refresh(application)
            CodexWidgetApplication._start_usage_refresh(application)

        schedule.assert_called_once_with(60, application._usage_refresh_tick)
        self.assertEqual(application._usage_refresh_source, 41)
        self.assertTrue(application._usage_refreshing)
        self.assertEqual(len(application.async_calls), 1)

    def test_timer_stops_when_widget_is_hidden(self):
        application = RefreshApplication(visible=False)
        application._usage_refresh_source = 41

        result = CodexWidgetApplication._usage_refresh_tick(application)

        self.assertEqual(result, GLib.SOURCE_REMOVE)
        self.assertIsNone(application._usage_refresh_source)
        self.assertEqual(application.async_calls, [])

    def test_hide_removes_pending_refresh_timer(self):
        application = RefreshApplication(visible=False)
        application._usage_refresh_source = 41

        with patch("codex_widget.app.GLib.source_remove") as source_remove:
            CodexWidgetApplication._on_widget_hidden(application, application.window)

        source_remove.assert_called_once_with(41)
        self.assertIsNone(application._usage_refresh_source)



if __name__ == "__main__":
    unittest.main()
