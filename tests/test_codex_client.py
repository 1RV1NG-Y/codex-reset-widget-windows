from __future__ import annotations

import unittest
from datetime import UTC, datetime

from codex_widget.codex_client import CodexClient


class CodexClientTests(unittest.TestCase):
    def test_parses_weekly_window_by_duration_and_banked_credits(self):
        snapshot = CodexClient._parse_snapshot(
            {
                "rateLimits": {
                    "primary": {
                        "usedPercent": 10,
                        "windowDurationMins": 300,
                        "resetsAt": 1_786_100_000,
                    }
                },
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 10,
                            "windowDurationMins": 300,
                            "resetsAt": 1_786_100_000,
                        },
                        "secondary": {
                            "usedPercent": 42,
                            "windowDurationMins": 10080,
                            "resetsAt": 1_786_868_740,
                        },
                    }
                },
                "rateLimitResetCredits": {
                    "availableCount": 2,
                    "credits": [{"expiresAt": 1_787_000_000}],
                },
            }
        )

        self.assertEqual(snapshot.used_percent, 42)
        self.assertEqual(snapshot.window_minutes, 10080)
        self.assertEqual(
            snapshot.reset_at, datetime.fromtimestamp(1_786_868_740, UTC)
        )
        self.assertEqual(snapshot.banked_resets, 2)
        self.assertEqual(
            snapshot.banked_reset_expirations,
            (datetime.fromtimestamp(1_787_000_000, UTC),),
        )


if __name__ == "__main__":
    unittest.main()
