from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from datetime import UTC, datetime
from pathlib import Path

from codex_widget.models import AppState, ResetEvent, UsageSnapshot
from codex_widget.state import StateStore
from codex_widget.watcher import PollOutcome, ResetWatcher, account_reset_observed


class FakeTracker:
    def __init__(self, event):
        self.event = event

    def fetch_latest_relevant(self):
        return self.event


def event(event_id: str) -> ResetEvent:
    return ResetEvent(
        event_id=event_id,
        announced_at=datetime(2026, 8, 10, tzinfo=UTC),
        summary="Reset",
        url="https://example.test/reset",
    )


def usage(percent: float | None) -> UsageSnapshot:
    return UsageSnapshot(percent, None, 10080, 0)


class ResetWatcherTests(unittest.TestCase):
    def test_first_observation_seeds_then_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = FakeTracker(event("one"))
            watcher = ResetWatcher(
                tracker, StateStore(Path(directory) / "state.json")
            )

            first, _, _ = watcher.poll_once()
            duplicate, _, _ = watcher.poll_once()
            tracker.event = event("two")
            changed, latest, state = watcher.poll_once()

            self.assertIs(first, PollOutcome.SEEDED)
            self.assertIs(duplicate, PollOutcome.UNCHANGED)
            self.assertIs(changed, PollOutcome.NEW_RESET)
            self.assertEqual(latest.event_id, "two")
            self.assertEqual(state.last_seen_reset_id, "two")

    def test_active_tibo_signal_is_confirmed_by_fresh_low_usage(self):
        now = datetime(2026, 8, 11, 0, 16, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            store.save(
                AppState(
                    last_seen_reset_id="old",
                    last_global_reset=event("old"),
                    last_known_usage=UsageSnapshot(
                        4,
                        datetime(2026, 8, 18, 0, 16, tzinfo=UTC),
                        10080,
                        0,
                        checked_at=now,
                    ),
                )
            )
            pending = ResetEvent(
                event_id="pending",
                announced_at=datetime(2026, 8, 8, 20, tzinfo=UTC),
                summary="Reset planned for Monday",
                url="https://example.test/pending",
                confirmed=False,
            )
            watcher = ResetWatcher(FakeTracker(pending), store)
            with patch(
                "codex_widget.watcher.utc_now",
                return_value=now,
            ):
                outcome, latest, state = watcher.poll_once()

        self.assertIs(outcome, PollOutcome.NEW_RESET)
        self.assertTrue(latest.confirmed)
        self.assertEqual(latest.effective_at, now)
        self.assertEqual(state.last_seen_reset_id, "pending")
        self.assertEqual(state.last_global_reset, latest)

    def test_account_reset_requires_low_or_decreased_usage(self):
        self.assertTrue(account_reset_observed(None, usage(0)))
        self.assertTrue(account_reset_observed(usage(70), usage(2)))
        self.assertFalse(account_reset_observed(None, usage(20)))
        self.assertFalse(account_reset_observed(usage(20), usage(20)))
        self.assertFalse(account_reset_observed(usage(None), usage(None)))


if __name__ == "__main__":
    unittest.main()
