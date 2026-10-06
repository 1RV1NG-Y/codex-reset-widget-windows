from __future__ import annotations

import tempfile
import threading
import unittest
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

if sys.platform == "win32":
    raise unittest.SkipTest("GTK application tests run on Linux")

from gi.repository import GLib

from codex_widget.app import CodexWidgetApplication
from codex_widget.models import AppState, ResetEvent, UsageSnapshot, utc_now
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


class MemoryStateStore:
    def __init__(self):
        self.state = AppState()

    def load(self):
        return self.state

    def save(self, state):
        self.state = state


class WindowKeeperApplication:
    _schedule_window_keeper_from_usage = (
        CodexWidgetApplication._schedule_window_keeper_from_usage
    )
    _choose_window_keeper_due_at = (
        CodexWidgetApplication._choose_window_keeper_due_at
    )
    _usage_has_active_five_hour_window = (
        CodexWidgetApplication._usage_has_active_five_hour_window
    )
    _usage_looks_like_idle_five_hour_window = (
        CodexWidgetApplication._usage_looks_like_idle_five_hour_window
    )
    _window_keeper_window_seconds = staticmethod(
        CodexWidgetApplication._window_keeper_window_seconds
    )
    _schedule_window_keeper_at = CodexWidgetApplication._schedule_window_keeper_at
    _cancel_window_keeper_timer = CodexWidgetApplication._cancel_window_keeper_timer
    _schedule_window_keeper_retry = (
        CodexWidgetApplication._schedule_window_keeper_retry
    )
    _schedule_window_keeper_retry_at = (
        CodexWidgetApplication._schedule_window_keeper_retry_at
    )
    _start_window_keeper_watchdog = (
        CodexWidgetApplication._start_window_keeper_watchdog
    )
    _cancel_window_keeper_watchdog = (
        CodexWidgetApplication._cancel_window_keeper_watchdog
    )
    _window_keeper_watchdog_tick = (
        CodexWidgetApplication._window_keeper_watchdog_tick
    )
    _window_keeper_history_suffix = (
        CodexWidgetApplication._window_keeper_history_suffix
    )
    _record_window_keeper_outcome = (
        CodexWidgetApplication._record_window_keeper_outcome
    )
    _activate_and_read_usage = CodexWidgetApplication._activate_and_read_usage
    _verify_window_keeper = CodexWidgetApplication._verify_window_keeper
    _window_keeper_activation_finished = (
        CodexWidgetApplication._window_keeper_activation_finished
    )
    set_window_keeper_enabled = CodexWidgetApplication.set_window_keeper_enabled

    def __init__(self, state_store=None):
        self.enabled = True
        self._window_keeper_source = None
        self._window_keeper_watchdog_source = None
        self._window_keeper_message = ""
        self._window_keeper_busy = False
        self._state_lock = threading.Lock()
        self._account_query_lock = threading.Lock()
        self.state_store = state_store or MemoryStateStore()
        self.messages = []
        self.refreshes = 0
        self.cancellations = 0
        self.window = None
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


class KeeperCycleApplication:
    _schedule_window_keeper_from_usage = (
        CodexWidgetApplication._schedule_window_keeper_from_usage
    )
    _schedule_window_keeper_at = CodexWidgetApplication._schedule_window_keeper_at
    _schedule_window_keeper_retry = (
        CodexWidgetApplication._schedule_window_keeper_retry
    )
    _cancel_window_keeper_timer = CodexWidgetApplication._cancel_window_keeper_timer
    _cancel_window_keeper_watchdog = (
        CodexWidgetApplication._cancel_window_keeper_watchdog
    )
    _window_keeper_enabled = CodexWidgetApplication._window_keeper_enabled
    _window_keeper_timer_fired = CodexWidgetApplication._window_keeper_timer_fired
    _window_keeper_retry_fired = CodexWidgetApplication._window_keeper_retry_fired
    _refresh_window_keeper_schedule = (
        CodexWidgetApplication._refresh_window_keeper_schedule
    )
    _window_keeper_schedule_refreshed = (
        CodexWidgetApplication._window_keeper_schedule_refreshed
    )
    _activate_and_read_usage = CodexWidgetApplication._activate_and_read_usage
    _verify_window_keeper = CodexWidgetApplication._verify_window_keeper
    _read_and_store_usage = CodexWidgetApplication._read_and_store_usage
    _read_and_store_usage_unlocked = (
        CodexWidgetApplication._read_and_store_usage_unlocked
    )
    _record_window_keeper_outcome = (
        CodexWidgetApplication._record_window_keeper_outcome
    )
    _window_keeper_activation_finished = (
        CodexWidgetApplication._window_keeper_activation_finished
    )
    _window_keeper_history_suffix = (
        CodexWidgetApplication._window_keeper_history_suffix
    )
    set_window_keeper_enabled = CodexWidgetApplication.set_window_keeper_enabled

    def _choose_window_keeper_due_at(self, usage, state, now):
        return CodexWidgetApplication._choose_window_keeper_due_at(
            self, usage, state, now
        )

    def _usage_has_active_five_hour_window(self, usage, now):
        return CodexWidgetApplication._usage_has_active_five_hour_window(
            self, usage, now
        )

    def _usage_looks_like_idle_five_hour_window(self, usage, now):
        return CodexWidgetApplication._usage_looks_like_idle_five_hour_window(
            self, usage, now
        )

    @staticmethod
    def _window_keeper_window_seconds(usage=None):
        return CodexWidgetApplication._window_keeper_window_seconds(usage)

    def _schedule_window_keeper_retry_at(self, target, message):
        return CodexWidgetApplication._schedule_window_keeper_retry_at(
            self, target, message
        )

    def __init__(self, state_store):
        self.state_store = state_store
        self._state_lock = threading.Lock()
        self._account_query_lock = threading.Lock()
        self._window_keeper_source = None
        self._window_keeper_watchdog_source = None
        self._window_keeper_busy = False
        self._window_keeper_message = ""
        self.window = None

    def _run_async(self, work, done):
        try:
            result = work()
        except BaseException as error:
            done(None, error)
            return
        done(result, None)

    def _set_window_keeper_message(self, message):
        self._window_keeper_message = message


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


class WindowKeeperCycleTests(unittest.TestCase):
    def setUp(self):
        sleeper = patch("codex_widget.window_keeper.time.sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_two_scheduler_cycles_survive_sliding_idle_observations(self):
        now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        requests = []
        timers = []

        class FakeCodex:
            def activate_five_hour_window(self):
                requests.append(now)

            def read_rate_limits(self):
                return UsageSnapshot(
                    used_percent=25,
                    reset_at=now + timedelta(days=1),
                    window_minutes=10080,
                    banked_resets=2,
                    checked_at=now,
                    five_hour_used_percent=0,
                    five_hour_reset_at=now + timedelta(hours=5),
                    five_hour_window_minutes=300,
                )

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.save(AppState(keep_five_hour_window_active=True))
            application = KeeperCycleApplication(store)
            application.codex = FakeCodex()

            def schedule(seconds, callback):
                timers.append((seconds, callback))
                return len(timers)

            with (
                patch("codex_widget.window_keeper.utc_now", side_effect=lambda: now),
                patch(
                    "codex_widget.app.GLib.timeout_add_seconds",
                    side_effect=schedule,
                ),
                patch("codex_widget.app.GLib.source_remove"),
            ):
                application._schedule_window_keeper_from_usage(
                    application.codex.read_rate_limits()
                )
                now += timedelta(seconds=timers[-1][0])
                timers[-1][1]()
                self.assertEqual(len(requests), 1)
                due = store.load().next_window_keeper_due_at
                self.assertIsNotNone(due)

                for _ in range(299):
                    now += timedelta(minutes=1)
                    application._schedule_window_keeper_from_usage(
                        application.codex.read_rate_limits()
                    )
                    self.assertEqual(store.load().next_window_keeper_due_at, due)

                now = due
                timers[-1][1]()

            self.assertEqual(len(requests), 2)
            self.assertGreater(store.load().next_window_keeper_due_at, due)

    def test_restart_keeps_due_and_retry_backoff_ahead_of_idle_refresh(self):
        now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        due = now + timedelta(hours=1)
        retry = now + timedelta(minutes=1)
        timers = []
        requests = []

        class FakeCodex:
            def activate_five_hour_window(self):
                requests.append(now)

            def read_rate_limits(self):
                return UsageSnapshot(
                    used_percent=25,
                    reset_at=now + timedelta(days=1),
                    window_minutes=10080,
                    banked_resets=0,
                    checked_at=now,
                    five_hour_used_percent=0,
                    five_hour_reset_at=now + timedelta(hours=5),
                    five_hour_window_minutes=300,
                )

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.save(
                AppState(
                    keep_five_hour_window_active=True,
                    next_window_keeper_due_at=due,
                )
            )

            def schedule(seconds, callback):
                timers.append((seconds, callback))
                return len(timers)

            with (
                patch("codex_widget.window_keeper.utc_now", side_effect=lambda: now),
                patch(
                    "codex_widget.app.GLib.timeout_add_seconds",
                    side_effect=schedule,
                ),
                patch("codex_widget.app.GLib.source_remove"),
            ):
                failed_application = KeeperCycleApplication(store)
                failed_application._window_keeper_activation_finished(
                    None, RuntimeError("offline")
                )
                self.assertEqual(store.load().next_window_keeper_due_at, due)
                self.assertEqual(store.load().next_window_keeper_retry_at, retry)

                application = KeeperCycleApplication(store)
                application.codex = FakeCodex()
                application._schedule_window_keeper_from_usage(
                    application.codex.read_rate_limits()
                )
                self.assertEqual(timers[-1][0], 60)
                self.assertEqual(store.load().next_window_keeper_due_at, due)
                self.assertEqual(store.load().next_window_keeper_retry_at, retry)
                self.assertEqual(requests, [])

                now = retry
                timers[-1][1]()

            self.assertEqual(requests, [])
            self.assertEqual(store.load().next_window_keeper_due_at, due)
            self.assertIsNone(store.load().next_window_keeper_retry_at)
            self.assertEqual(timers[-1][0], 59 * 60)

    def test_disable_clears_deadlines_and_blocks_a_pending_callback(self):
        now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        timers = []
        requests = []

        class FakeCodex:
            def activate_five_hour_window(self):
                requests.append(now)

            def read_rate_limits(self):
                raise AssertionError("disabled callback must not refresh usage")

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.save(
                AppState(
                    keep_five_hour_window_active=True,
                    next_window_keeper_due_at=now + timedelta(hours=5),
                    next_window_keeper_retry_at=now + timedelta(minutes=1),
                )
            )
            application = KeeperCycleApplication(store)
            application.codex = FakeCodex()

            def schedule(seconds, callback):
                timers.append((seconds, callback))
                return len(timers)

            with (
                patch("codex_widget.window_keeper.utc_now", side_effect=lambda: now),
                patch(
                    "codex_widget.app.GLib.timeout_add_seconds",
                    side_effect=schedule,
                ),
                patch("codex_widget.app.GLib.source_remove"),
            ):
                application._schedule_window_keeper_retry(
                    60, "retrying after failure"
                )
                pending_callback = timers[-1][1]
                application.set_window_keeper_enabled(False)
                pending_callback()

            state = store.load()
            self.assertFalse(state.keep_five_hour_window_active)
            self.assertIsNone(state.next_window_keeper_due_at)
            self.assertIsNone(state.next_window_keeper_retry_at)
            self.assertEqual(requests, [])


class WindowKeeperTests(unittest.TestCase):
    def test_active_window_schedules_tiny_request_after_its_reset(self):
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()
        usage = usage_snapshot(five_hour_reset=now + timedelta(hours=1))

        with (
            patch("codex_widget.window_keeper.utc_now", return_value=now),
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
            patch("codex_widget.window_keeper.utc_now", return_value=now),
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
            patch("codex_widget.window_keeper.utc_now", return_value=now),
            patch(
                "codex_widget.app.GLib.timeout_add_seconds",
                return_value=74,
            ) as schedule,
        ):
            application._schedule_window_keeper_from_usage(usage_snapshot())

        schedule.assert_called_once_with(1, application._window_keeper_timer_fired)

    def test_toggle_persists_opt_in_and_starts_or_cancels_scheduler(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            application = WindowKeeperApplication(store)

            with patch(
                "codex_widget.app.GLib.timeout_add_seconds",
                return_value=76,
            ):
                application.set_window_keeper_enabled(True)
            self.assertTrue(store.load().keep_five_hour_window_active)
            self.assertEqual(application.refreshes, 1)

            with patch("codex_widget.app.GLib.source_remove") as remove:
                application.set_window_keeper_enabled(False)
            self.assertFalse(store.load().keep_five_hour_window_active)
            self.assertEqual(application.cancellations, 1)
            remove.assert_called_once_with(76)
            self.assertEqual(
                application.messages[-1],
                "Automatic 5-hour rolling is off",
            )

    def test_activation_failure_surfaces_error_and_retries_in_one_minute(self):
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
            60,
            application._window_keeper_retry_fired,
        )
        self.assertEqual(application._window_keeper_source, 75)
        self.assertIn("offline", application.messages[-1])

    def test_watchdog_recovers_after_the_one_shot_timer_is_missed(self):
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()
        application.state_store.state.last_known_usage = usage_snapshot(
            five_hour_reset=now - timedelta(minutes=2)
        )

        with patch("codex_widget.window_keeper.utc_now", return_value=now):
            result = application._window_keeper_watchdog_tick()

        self.assertEqual(result, GLib.SOURCE_CONTINUE)
        self.assertEqual(application.refreshes, 1)

    def test_successful_activation_persists_auditable_timestamp(self):
        attempted = datetime(2026, 8, 31, 10, tzinfo=UTC)
        succeeded = attempted + timedelta(seconds=4)
        application = WindowKeeperApplication()
        application.codex = SimpleNamespace(activate_five_hour_window=lambda: None)
        expected = usage_snapshot(
            five_hour_reset=attempted + timedelta(hours=5)
        )
        application._read_and_store_usage_unlocked = lambda: expected

        with patch(
            "codex_widget.window_keeper.utc_now",
            side_effect=[attempted, succeeded, succeeded, succeeded, succeeded],
        ):
            result = application._activate_and_read_usage()

        state = application.state_store.load()
        self.assertIs(result, expected)
        self.assertEqual(state.last_window_keeper_attempt_at, attempted)
        self.assertEqual(state.last_window_keeper_success_at, succeeded)
        self.assertIsNone(state.last_window_keeper_error)

    def test_failed_activation_persists_error_for_diagnosis(self):
        attempted = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = WindowKeeperApplication()

        def fail():
            raise RuntimeError("network unavailable")

        application.codex = SimpleNamespace(activate_five_hour_window=fail)
        with (
            patch("codex_widget.window_keeper.utc_now", return_value=attempted),
            self.assertRaisesRegex(RuntimeError, "network unavailable"),
        ):
            application._activate_and_read_usage()

        state = application.state_store.load()
        self.assertEqual(state.last_window_keeper_attempt_at, attempted)
        self.assertIsNone(state.last_window_keeper_success_at)
        self.assertEqual(state.last_window_keeper_error, "network unavailable")


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
