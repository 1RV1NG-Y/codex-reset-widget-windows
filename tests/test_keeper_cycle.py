from datetime import UTC, datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_widget.models import AppState, UsageSnapshot
from codex_widget.state import StateStore
from codex_widget.windows_app import CodexWidgetApplication


class KeeperCycleTests(unittest.TestCase):
    def test_two_real_scheduler_cycles_with_sliding_idle_observations(self):
        """Exercise the real callback/record/schedule chain without spending usage."""
        now = datetime(2026, 9, 6, 12, tzinfo=UTC)
        requests = []
        timers = []

        class FakeCodex:
            def activate_five_hour_window(self):
                requests.append(now)

            def read_rate_limits(self):
                return UsageSnapshot(
                    used_percent=25, reset_at=now + timedelta(days=1),
                    window_minutes=10080, banked_resets=2,
                    checked_at=now, five_hour_used_percent=0,
                    five_hour_reset_at=now + timedelta(hours=5),
                    five_hour_window_minutes=300,
                )

        with tempfile.TemporaryDirectory() as directory:
            app = CodexWidgetApplication()
            app.state_store = StateStore(Path(directory) / 'state.json')
            app.state_store.save(AppState(keep_five_hour_window_active=True))
            app.codex = FakeCodex()
            app._after_seconds = lambda seconds, callback: timers.append((seconds, callback)) or str(len(timers))
            app._run_async = lambda work, done: done(work(), None)

            with patch('codex_widget.windows_app.utc_now', side_effect=lambda: now):
                app._schedule_window_keeper_from_usage(app.codex.read_rate_limits())
                now += timedelta(seconds=timers[-1][0])
                timers[-1][1]()
                self.assertEqual(len(requests), 1)
                due = app.state_store.load().next_window_keeper_due_at
                for _ in range(299):
                    now += timedelta(minutes=1)
                    app._schedule_window_keeper_from_usage(app.codex.read_rate_limits())
                    self.assertEqual(app.state_store.load().next_window_keeper_due_at, due)
                now = due
                timers[-1][1]()
                self.assertEqual(len(requests), 2)
                self.assertGreater(app.state_store.load().next_window_keeper_due_at, due)
