from __future__ import annotations

import subprocess
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

from codex_widget.codex_client import CodexClient, CodexClientError


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
        self.assertEqual(snapshot.five_hour_used_percent, 10)
        self.assertEqual(snapshot.five_hour_window_minutes, 300)
        self.assertEqual(
            snapshot.five_hour_reset_at,
            datetime.fromtimestamp(1_786_100_000, UTC),
        )
        self.assertEqual(snapshot.banked_resets, 2)
        self.assertEqual(
            snapshot.banked_reset_expirations,
            (datetime.fromtimestamp(1_787_000_000, UTC),),
        )

    def test_activation_uses_ephemeral_read_only_lightweight_command(self):
        client = CodexClient(
            executable="/test/codex",
            activation_timeout=23,
            activation_model="gpt-5.6-luna",
        )

        with patch(
            "codex_widget.codex_client.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stderr=""),
        ) as run:
            client.activate_five_hour_window()

        arguments = run.call_args.args[0]
        self.assertEqual(arguments[:2], ["/test/codex", "exec"])
        self.assertIn("--ephemeral", arguments)
        self.assertIn("--ignore-rules", arguments)
        self.assertIn("--ignore-user-config", arguments)
        self.assertEqual(arguments[arguments.index("--sandbox") + 1], "read-only")
        self.assertEqual(arguments[arguments.index("--model") + 1], "gpt-5.6-luna")
        self.assertEqual(arguments[-1], "Reply exactly OK.")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["timeout"], 23)

    def test_activation_reports_codex_failure(self):
        client = CodexClient()

        with patch(
            "codex_widget.codex_client.subprocess.run",
            return_value=SimpleNamespace(
                returncode=1,
                stderr="warning\nmodel unavailable\n",
            ),
        ):
            with self.assertRaisesRegex(CodexClientError, "model unavailable"):
                client.activate_five_hour_window()


if __name__ == "__main__":
    unittest.main()
