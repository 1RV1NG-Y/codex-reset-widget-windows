from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from codex_widget.models import AppState, ResetEvent, UsageSnapshot
from codex_widget.state import StateStore


class StateStoreTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            state = AppState(
                last_seen_reset_id="tweet-42",
                last_global_reset=ResetEvent(
                    event_id="tweet-42",
                    announced_at=datetime(2026, 8, 10, 8, tzinfo=UTC),
                    summary="Reset announced",
                    url="https://example.test/tweet-42",
                    effective_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
                ),
                last_known_usage=UsageSnapshot(
                    used_percent=38,
                    reset_at=datetime(2026, 8, 16, 8, tzinfo=UTC),
                    window_minutes=10080,
                    banked_resets=1,
                    banked_reset_expirations=(
                        datetime(2026, 8, 20, 8, tzinfo=UTC),
                    ),
                    checked_at=datetime(2026, 8, 10, 9, tzinfo=UTC),
                    five_hour_used_percent=12,
                    five_hour_reset_at=datetime(2026, 8, 10, 12, tzinfo=UTC),
                    five_hour_window_minutes=300,
                ),
            )

            store.save(state)

            self.assertEqual(store.load(), state)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_invalid_state_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            self.assertEqual(store.load(), AppState())

            store.path.write_text("not json")
            self.assertEqual(store.load(), AppState())


if __name__ == "__main__":
    unittest.main()
