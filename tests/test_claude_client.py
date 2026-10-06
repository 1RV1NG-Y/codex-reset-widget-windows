from __future__ import annotations

import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from codex_widget import __version__
from codex_widget.claude_client import ClaudeClient, ClaudeClientError, USAGE_URL, detect_claude


def response(data):
    return io.BytesIO(json.dumps(data).encode())


class ClaudeClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / ".credentials.json"
        self.path.write_text(json.dumps({
            "otherCredential": {"preserve": True},
            "claudeAiOauth": {
                "accessToken": "test-access", "refreshToken": "test-refresh",
                "expiresAt": 9_999_999_999_999, "subscriptionType": "pro",
            },
        }))
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.data = {
            "five_hour": {"utilization": 12.5, "resets_at": "2026-10-04T23:00:00Z"},
            "seven_day": {"utilization": 42, "resets_at": "2026-10-09T22:00:00+00:00"},
        }

    def client(self, opener):
        return ClaudeClient(credentials_path=self.path, opener=opener)

    def test_parses_separate_windows_without_inventing_reset_credits(self):
        usage = ClaudeClient._parse_snapshot(self.data)
        self.assertEqual(usage.used_percent, 42)
        self.assertEqual(usage.five_hour_used_percent, 12.5)
        self.assertEqual(usage.five_hour_reset_at, datetime(2026, 10, 4, 23, tzinfo=UTC))
        self.assertEqual(usage.five_hour_window_minutes, 300)
        self.assertIsNone(usage.banked_resets)

    def test_null_windows_and_invalid_values_are_not_zero_usage(self):
        usage = ClaudeClient._parse_snapshot({"five_hour": None, "seven_day": None})
        self.assertIsNone(usage.used_percent)
        self.assertIsNone(usage.five_hour_used_percent)
        for value in (True, "0", float("nan"), float("inf"), -1, 101):
            with self.subTest(value=value):
                usage = ClaudeClient._parse_snapshot({"five_hour": {"utilization": value}})
                self.assertIsNone(usage.five_hour_used_percent)

    def test_rejects_malformed_response(self):
        for data in ({}, {"error": "failure"}, {"five_hour": [1]}):
            with self.subTest(data=data), self.assertRaises(ClaudeClientError):
                ClaudeClient._parse_snapshot(data)

    def test_reads_usage_without_persisting_tokens_and_caches_normal_refresh(self):
        opener = Mock(side_effect=lambda *_a, **_kw: response(self.data))
        client = self.client(opener)
        before = self.path.read_text()
        first = client.read_rate_limits()
        self.assertIs(first, client.read_rate_limits())
        self.assertEqual(opener.call_count, 1)
        request = opener.call_args.args[0]
        self.assertEqual(request.full_url, USAGE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-access")
        self.assertEqual(self.path.read_text(), before)
        client.read_rate_limits(force=True)
        self.assertEqual(opener.call_count, 2)

    def test_refreshes_expired_credentials_and_preserves_other_fields(self):
        document = json.loads(self.path.read_text())
        document["claudeAiOauth"]["expiresAt"] = 0
        document["claudeAiOauth"]["scopes"] = ["user:profile", "user:inference"]
        self.path.write_text(json.dumps(document))
        opener = Mock(side_effect=[
            HTTPError(USAGE_URL, 401, "Unauthorized", {}, None),
            response({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600,
                      "refresh_token_expires_in": 7200}),
            response(self.data),
        ])
        with patch("codex_widget.claude_client.time.time", return_value=1000):
            self.client(opener).read_rate_limits()
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["claudeAiOauth"]["accessToken"], "new-access")
        self.assertEqual(saved["claudeAiOauth"]["refreshToken"], "new-refresh")
        self.assertEqual(saved["claudeAiOauth"]["expiresAt"], 4_600_000)
        self.assertEqual(saved["claudeAiOauth"]["refreshTokenExpiresAt"], 8_200_000)
        self.assertEqual(saved["claudeAiOauth"]["subscriptionType"], "pro")
        self.assertEqual(saved["otherCredential"], {"preserve": True})
        if os.name == "posix":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer new-access")
        renewal = opener.call_args_list[1].args[0]
        self.assertEqual(json.loads(renewal.data)["scope"], "user:profile user:inference")
        self.assertEqual(renewal.get_header("User-agent"), f"codex-widget/{__version__}")

    def test_stale_local_expiry_does_not_interrupt_working_usage(self):
        document = json.loads(self.path.read_text())
        document["claudeAiOauth"]["expiresAt"] = 0
        self.path.write_text(json.dumps(document))
        opener = Mock(return_value=response(self.data))
        self.assertEqual(self.client(opener).read_rate_limits().used_percent, 42)
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(opener.call_args.args[0].full_url, USAGE_URL)

    def test_unauthorized_usage_refreshes_once(self):
        opener = Mock(side_effect=[
            HTTPError(USAGE_URL, 401, "Unauthorized", {}, None),
            response({"access_token": "new-access", "expires_in": 3600}),
            response(self.data),
        ])
        self.assertEqual(self.client(opener).read_rate_limits().used_percent, 42)
        self.assertEqual(opener.call_count, 3)

    def test_failed_login_never_leaks_token_error_bodies(self):
        opener = Mock(side_effect=[
            HTTPError(USAGE_URL, 401, "test-access", {}, None),
            HTTPError(USAGE_URL, 400, "test-refresh", {}, response({
                "error": "invalid_grant", "error_description": "test-refresh",
            })),
        ])
        with self.assertRaisesRegex(ClaudeClientError, "claude auth login") as caught:
            self.client(opener).read_rate_limits()
        self.assertNotIn("test-access", str(caught.exception))
        self.assertNotIn("test-refresh", str(caught.exception))

    def test_temporary_renewal_failures_do_not_claim_logout(self):
        for status, body in (
            (403, {"status": 403, "cloudflare_error": True, "detail": "test-refresh"}),
            (500, {"error": "server_error"}),
            (400, {"error": "invalid_request"}),
        ):
            with self.subTest(status=status):
                before = self.path.read_text()
                opener = Mock(side_effect=[
                    HTTPError(USAGE_URL, 401, "Unauthorized", {}, None),
                    HTTPError(USAGE_URL, status, "test-refresh", {}, response(body)),
                ])
                with self.assertRaisesRegex(ClaudeClientError, f"HTTP {status}") as caught:
                    self.client(opener).read_rate_limits()
                self.assertNotIn("expired", str(caught.exception))
                self.assertNotIn("claude auth login", str(caught.exception))
                self.assertNotIn("test-refresh", str(caught.exception))
                self.assertEqual(self.path.read_text(), before)

    def test_forbidden_usage_does_not_claim_logout(self):
        opener = Mock(side_effect=HTTPError(USAGE_URL, 403, "Forbidden", {}, None))
        with self.assertRaisesRegex(ClaudeClientError, "access denied") as caught:
            self.client(opener).read_rate_limits()
        self.assertNotIn("claude auth login", str(caught.exception))

    def test_picks_up_cli_token_rotation_without_restart_or_cached_old_usage(self):
        opener = Mock(side_effect=[response(self.data), response(self.data)])
        client = self.client(opener)
        client.read_rate_limits()
        document = json.loads(self.path.read_text())
        document["claudeAiOauth"]["accessToken"] = "cli-new-access"
        self.path.write_text(json.dumps(document))
        client.read_rate_limits()
        self.assertEqual(opener.call_count, 2)
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer cli-new-access")

    def test_reuses_cli_token_rotated_during_failed_renewal(self):
        def renewed_by_cli(*_args, **_kwargs):
            document = json.loads(self.path.read_text())
            document["claudeAiOauth"]["accessToken"] = "cli-new-access"
            self.path.write_text(json.dumps(document))
            raise HTTPError(USAGE_URL, 400, "Bad Request", {}, response({"error": "invalid_grant"}))

        def request(req, **kwargs):
            if req.get_method() == "POST":
                return renewed_by_cli(req, **kwargs)
            if req.get_header("Authorization") == "Bearer cli-new-access":
                return response(self.data)
            raise HTTPError(USAGE_URL, 401, "Unauthorized", {}, None)
        opener = Mock(side_effect=request)
        client = self.client(opener)
        self.assertEqual(client.read_rate_limits().used_percent, 42)
        self.assertEqual(opener.call_args.args[0].get_header("Authorization"), "Bearer cli-new-access")

    def test_renewal_rate_limit_blocks_forced_reads(self):
        opener = Mock(side_effect=[
            HTTPError(USAGE_URL, 401, "Unauthorized", {}, None),
            HTTPError(USAGE_URL, 429, "Slow down", {}, response({"error": "rate_limited"})),
        ])
        client = self.client(opener)
        for force in (False, True):
            with self.assertRaisesRegex(ClaudeClientError, "rate limited"):
                client.read_rate_limits(force=force)
        self.assertEqual(opener.call_count, 2)

    def test_rate_limit_blocks_even_forced_verification_reads(self):
        opener = Mock(side_effect=HTTPError(USAGE_URL, 429, "slow down", {}, None))
        client = self.client(opener)
        for force in (False, False, True):
            with self.assertRaisesRegex(ClaudeClientError, "rate limited"):
                client.read_rate_limits(force=force)
        self.assertEqual(opener.call_count, 1)

    def test_rate_limit_honors_server_retry_after(self):
        for retry_after, expected, wait in (
            ("3547", 3547, "1h"),
            ("30", 300, "5m"),
            (None, 300, "5m"),
            ("not-a-date", 300, "5m"),
            ("999999", 6 * 60 * 60, "6h"),
        ):
            with self.subTest(retry_after=retry_after):
                headers = {} if retry_after is None else {"Retry-After": retry_after}
                opener = Mock(side_effect=HTTPError(USAGE_URL, 429, "slow down", headers, None))
                client = self.client(opener)
                with patch("codex_widget.claude_client.time.monotonic", return_value=1000.0):
                    with self.assertRaisesRegex(ClaudeClientError, f"retrying in {wait}$"):
                        client.read_rate_limits()
                self.assertEqual(client._retry_at, 1000.0 + expected)

    def test_rate_limit_reports_remaining_wait(self):
        opener = Mock(side_effect=HTTPError(
            USAGE_URL, 429, "slow down", {"Retry-After": "3600"}, None
        ))
        client = self.client(opener)
        with patch("codex_widget.claude_client.time.monotonic", return_value=1000.0):
            with self.assertRaises(ClaudeClientError):
                client.read_rate_limits()
        with patch("codex_widget.claude_client.time.monotonic", return_value=1000.0 + 3300):
            with self.assertRaisesRegex(ClaudeClientError, "retrying in 5m$"):
                client.read_rate_limits(force=True)
        self.assertEqual(opener.call_count, 1)

    def test_rejected_login_is_not_resent_until_credentials_change(self):
        def request(req, **_kwargs):
            if req.get_method() == "POST":
                raise HTTPError(USAGE_URL, 400, "Bad Request", {}, response({"error": "invalid_grant"}))
            if req.get_header("Authorization") == "Bearer cli-new-access":
                return response(self.data)
            raise HTTPError(USAGE_URL, 401, "Unauthorized", {}, None)

        opener = Mock(side_effect=request)
        client = self.client(opener)
        for force in (False, False, True):
            with self.assertRaisesRegex(ClaudeClientError, "session expired; run claude auth login"):
                client.read_rate_limits(force=force)
        self.assertEqual(opener.call_count, 2)
        document = json.loads(self.path.read_text())
        document["claudeAiOauth"]["accessToken"] = "cli-new-access"
        self.path.write_text(json.dumps(document))
        self.assertEqual(client.read_rate_limits().used_percent, 42)
        self.assertEqual(opener.call_count, 3)

    def test_login_without_renewal_token_is_not_resent(self):
        document = json.loads(self.path.read_text())
        del document["claudeAiOauth"]["refreshToken"]
        self.path.write_text(json.dumps(document))
        opener = Mock(side_effect=HTTPError(USAGE_URL, 401, "Unauthorized", {}, None))
        client = self.client(opener)
        for _ in range(3):
            with self.assertRaisesRegex(ClaudeClientError, "run claude auth login"):
                client.read_rate_limits()
        self.assertEqual(opener.call_count, 1)

    def test_rejected_environment_token_is_not_resent(self):
        opener = Mock(side_effect=HTTPError(USAGE_URL, 401, "Unauthorized", {}, None))
        client = self.client(opener)
        with patch.dict(os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "env-token"}):
            for _ in range(2):
                with self.assertRaisesRegex(ClaudeClientError, "run claude auth login"):
                    client.read_rate_limits()
        self.assertEqual(opener.call_count, 1)

    def test_network_errors_are_actionable(self):
        opener = Mock(side_effect=URLError("network down"))
        with self.assertRaisesRegex(ClaudeClientError, "connection"):
            self.client(opener).read_rate_limits()

    def test_missing_credentials_requests_login(self):
        self.path.unlink()
        with self.assertRaisesRegex(ClaudeClientError, "claude auth login"):
            self.client(Mock()).read_rate_limits()

    def test_activation_isolated_from_api_billing_and_local_tools(self):
        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key", "CLAUDE_CODE_USE_VERTEX": "1"}),
            patch("codex_widget.claude_client.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run,
        ):
            self.client(Mock()).activate_five_hour_window()
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["claude", "--print"])
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertIn("--no-session-persistence", args)
        self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])
        self.assertNotIn("CLAUDE_CODE_USE_VERTEX", run.call_args.kwargs["env"])
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)

    def test_installed_claude_detected_without_running_inference(self):
        with patch("codex_widget.claude_client.shutil.which", return_value="/test/claude"):
            self.assertEqual(detect_claude(), "/test/claude")

    def test_detects_native_install_outside_service_path_and_rejects_missing_cli(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("codex_widget.claude_client.shutil.which", return_value=None),
            patch("codex_widget.claude_client.Path.home", return_value=Path(directory)),
        ):
            self.assertIsNone(detect_claude())
            native = Path(directory) / ".local" / "bin" / (
                "claude.exe" if os.name == "nt" else "claude"
            )
            native.parent.mkdir(parents=True)
            native.touch()
            if os.name == "posix":
                self.assertIsNone(detect_claude())
                native.chmod(0o755)
            self.assertEqual(detect_claude(), str(native))


if __name__ == "__main__":
    unittest.main()
