from __future__ import annotations

import tempfile
import unittest
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from codex_widget.models import AppState, ResetEvent, UsageSnapshot
from codex_widget.state import StateStore, _default_state_path


class StateStoreTests(unittest.TestCase):
    def test_state_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            store = StateStore(path)
            state = AppState(
                keep_five_hour_window_active=True,
                next_window_keeper_due_at=datetime(
                    2026, 8, 10, 15, 0, 10, tzinfo=UTC
                ),
                next_window_keeper_retry_at=datetime(
                    2026, 8, 10, 10, 1, tzinfo=UTC
                ),
                last_window_keeper_attempt_at=datetime(
                    2026, 8, 10, 10, tzinfo=UTC
                ),
                last_window_keeper_success_at=datetime(
                    2026, 8, 10, 10, 0, 4, tzinfo=UTC
                ),
                last_window_keeper_error="temporary network failure",
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
            if os.name == "nt":
                self.assertTrue(path.exists())
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_missing_or_invalid_state_is_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.json")
            self.assertEqual(store.load(), AppState())

            store.path.write_text("not json")
            self.assertEqual(store.load(), AppState())

    def test_default_state_path_uses_local_app_data_on_windows(self):
        with tempfile.TemporaryDirectory(prefix="codex local app ") as directory:
            local_app_data = Path(directory) / "Local App Data"
            with (
                patch("codex_widget.state.os.name", "nt"),
                patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}),
            ):
                self.assertEqual(
                    _default_state_path(),
                    local_app_data / "CodexWidget" / "state.json",
                )


if __name__ == "__main__":
    unittest.main()
