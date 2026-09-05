from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_widget.codex_client import (
    CodexClient,
    CodexClientError,
    _resolve_executable,
)


def _fake_codex_executable(directory: Path, script: str) -> Path:
    fake_dir = directory / "codex path with spaces"
    fake_dir.mkdir()
    script_path = fake_dir / "fake_codex.py"
    script_path.write_text(textwrap.dedent(script), encoding="utf-8")

    if os.name == "nt":
        executable = fake_dir / "codex.cmd"
        executable.write_text(
            f'@echo off\n"{sys.executable}" "{script_path}" %*\n',
            encoding="utf-8",
        )
    else:
        executable = fake_dir / "codex"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script_path}" "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    return executable


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
        self.assertEqual(
            arguments[arguments.index("--cd") + 1], tempfile.gettempdir()
        )
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

    def test_read_rate_limits_uses_real_process_and_ignores_other_output(self):
        with tempfile.TemporaryDirectory(prefix="codex fake ") as directory:
            executable = _fake_codex_executable(
                Path(directory),
                """
                import json
                import sys

                if sys.argv[1:] != ["app-server", "--stdio"]:
                    sys.exit(3)

                for line in sys.stdin:
                    message = json.loads(line)
                    if message.get("method") == "initialize":
                        print("plain startup noise", flush=True)
                        print(json.dumps({"id": 99, "result": {}}), flush=True)
                        print(
                            json.dumps({"id": message["id"], "result": {}}),
                            flush=True,
                        )
                    elif message.get("method") == "initialized":
                        pass
                    elif message.get("method") == "account/rateLimits/read":
                        print(
                            json.dumps({"method": "window/progress", "params": {}}),
                            flush=True,
                        )
                        print(
                            json.dumps(
                                {
                                    "id": message["id"],
                                    "result": {
                                        "rateLimitsByLimitId": {
                                            "codex": {
                                                "primary": {
                                                    "usedPercent": 8,
                                                    "windowDurationMins": 300,
                                                    "resetsAt": 1_786_100_000,
                                                },
                                                "secondary": {
                                                    "usedPercent": 71,
                                                    "windowDurationMins": 10080,
                                                    "resetsAt": 1_786_868_740,
                                                },
                                            }
                                        },
                                        "rateLimitResetCredits": {
                                            "availableCount": 3,
                                            "credits": [],
                                        },
                                    },
                                }
                            ),
                            flush=True,
                        )
                        break
                """,
            )

            snapshot = CodexClient(executable=str(executable), timeout=5).read_rate_limits()

        self.assertEqual(snapshot.used_percent, 71)
        self.assertEqual(snapshot.window_minutes, 10080)
        self.assertEqual(snapshot.five_hour_used_percent, 8)
        self.assertEqual(snapshot.banked_resets, 3)

    def test_read_rate_limits_times_out_real_process(self):
        with tempfile.TemporaryDirectory(prefix="codex fake ") as directory:
            executable = _fake_codex_executable(
                Path(directory),
                """
                import json
                import sys
                import time

                if sys.argv[1:] != ["app-server", "--stdio"]:
                    sys.exit(3)

                for line in sys.stdin:
                    message = json.loads(line)
                    if message.get("method") == "initialize":
                        print(
                            json.dumps({"id": message["id"], "result": {}}),
                            flush=True,
                        )
                    elif message.get("method") == "account/rateLimits/read":
                        time.sleep(30)
                """,
            )

            started_at = time.monotonic()
            with self.assertRaisesRegex(CodexClientError, "timed out"):
                CodexClient(executable=str(executable), timeout=1).read_rate_limits()
            self.assertLess(time.monotonic() - started_at, 5)

    def test_windows_app_install_fallback_uses_newest_codex_exe(self):
        with tempfile.TemporaryDirectory(prefix="codex local app ") as directory:
            local_app_data = Path(directory) / "Local App Data"
            old = (
                local_app_data
                / "OpenAI"
                / "Codex"
                / "bin"
                / "oldhash"
                / "codex.exe"
            )
            new = (
                local_app_data
                / "OpenAI"
                / "Codex"
                / "bin"
                / "newhash"
                / "codex.exe"
            )
            old.parent.mkdir(parents=True)
            new.parent.mkdir(parents=True)
            old.write_text("", encoding="utf-8")
            new.write_text("", encoding="utf-8")
            os.utime(old, (100, 100))
            os.utime(new, (200, 200))

            with (
                patch("codex_widget.codex_client.os.name", "nt"),
                patch("codex_widget.codex_client.shutil.which", return_value=None),
                patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}),
            ):
                self.assertEqual(_resolve_executable("codex"), str(new))


if __name__ == "__main__":
    unittest.main()
