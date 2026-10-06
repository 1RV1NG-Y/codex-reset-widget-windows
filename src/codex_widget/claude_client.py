from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import __version__
from .codex_client import _hidden_subprocess_kwargs
from .models import UsageSnapshot

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_PAGE = "https://claude.ai/settings/usage"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Public client identifier shipped in Claude Code's OAuth configuration.
_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_MAX_BYTES = 1_000_000
_USER_AGENT = f"codex-widget/{__version__}"


class ClaudeClientError(RuntimeError):
    pass


def detect_claude() -> str | None:
    installed = shutil.which("claude")
    if installed:
        return installed
    native = Path.home() / ".local" / "bin" / ("claude.exe" if os.name == "nt" else "claude")
    return str(native) if native.is_file() and os.access(native, os.X_OK) else None


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    with open(path, "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        lock.seek(0)
        try:  # LK_LOCK retries for about ten seconds before failing.
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            raise ClaudeClientError("Claude login renewal is busy; retrying automatically") from None
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return None


def _percent(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= 100 else None


class ClaudeClient:
    def __init__(
        self,
        executable: str = "claude",
        *,
        credentials_path: Path | None = None,
        timeout: float = 10,
        opener: Callable[..., Any] = urlopen,
    ) -> None:
        self.executable = executable
        self.credentials_path = credentials_path or Path(
            os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        ) / ".credentials.json"
        self.timeout = timeout
        self._opener = opener
        self._cached: UsageSnapshot | None = None
        self._cache_token: str | None = None
        self._next_fetch = 0.0
        self._retry_at = 0.0

    def _credentials(self) -> tuple[dict[str, Any], dict[str, Any]]:
        try:
            document = json.loads(self.credentials_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ClaudeClientError("Sign in to Claude Code with claude auth login") from None
        oauth = document.get("claudeAiOauth") if isinstance(document, dict) else None
        if not isinstance(oauth, dict) or not isinstance(oauth.get("accessToken"), str):
            raise ClaudeClientError("Claude subscription usage requires claude auth login")
        return document, oauth

    def _request(self, request: Request) -> dict[str, Any]:
        with self._opener(request, timeout=self.timeout) as response:
            payload = response.read(_MAX_BYTES + 1)
        if len(payload) > _MAX_BYTES:
            raise ClaudeClientError("Claude usage response is unexpectedly large")
        try:
            document = json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            raise ClaudeClientError("Claude returned invalid usage data") from None
        if not isinstance(document, dict):
            raise ClaudeClientError("Claude returned an unexpected usage response")
        return document

    def _refresh_token(self, old_token: str) -> str:
        # Serialize widget refreshes and re-read before updating Claude's own file.
        lock_path = self.credentials_path.with_name(".codex-widget-oauth.lock")
        with _exclusive_lock(lock_path):
            document, oauth = self._credentials()
            if oauth["accessToken"] != old_token:
                return oauth["accessToken"]
            refresh = oauth.get("refreshToken")
            if not isinstance(refresh, str) or not refresh:
                raise ClaudeClientError("Claude session expired; run claude auth login")
            try:
                payload = {
                    "grant_type": "refresh_token",
                    "refresh_token": refresh,
                    "client_id": _CLIENT_ID,
                }
                scopes = oauth.get("scopes")
                if isinstance(scopes, list) and scopes and all(isinstance(s, str) for s in scopes):
                    payload["scope"] = " ".join(scopes)
                result = self._request(Request(
                    _TOKEN_URL,
                    data=json.dumps(payload).encode(),
                    headers={
                        "Content-Type": "application/json", "Accept": "application/json",
                        "User-Agent": _USER_AGENT,
                    },
                    method="POST",
                ))
            except HTTPError as exc:
                # Only an explicit OAuth rejection means the saved login expired.
                # Edge-service failures (including 403) must not be called logout.
                try:
                    failure = json.loads(exc.read(_MAX_BYTES + 1))
                except (UnicodeDecodeError, ValueError, OSError):
                    failure = None
                finally:
                    exc.close()
                # Claude Code may have refreshed concurrently; never expose a token body.
                _, latest = self._credentials()
                if latest["accessToken"] != old_token:
                    return latest["accessToken"]
                if isinstance(failure, dict) and failure.get("error") == "invalid_grant":
                    raise ClaudeClientError("Claude session expired; run claude auth login") from None
                if exc.code == 429:
                    self._retry_at = time.monotonic() + 300
                    raise ClaudeClientError("Claude login renewal rate limited; retrying in 5m") from None
                raise ClaudeClientError(
                    f"Claude login renewal failed (HTTP {exc.code}); retrying automatically"
                ) from None
            token = result.get("access_token")
            expiry = result.get("expires_in")
            if (
                not isinstance(token, str) or not token
                or type(expiry) not in (int, float)
                or not math.isfinite(expiry) or expiry <= 0
            ):
                raise ClaudeClientError("Claude returned invalid login renewal data; retrying automatically")
            document, latest = self._credentials()
            if latest["accessToken"] != old_token:
                return latest["accessToken"]
            latest["accessToken"] = token
            latest["expiresAt"] = int((time.time() + expiry) * 1000)
            if isinstance(result.get("refresh_token"), str):
                latest["refreshToken"] = result["refresh_token"]
            if isinstance(result.get("scope"), str):
                latest["scopes"] = result["scope"].split()
            refresh_expiry = result.get("refresh_token_expires_in")
            if (
                type(refresh_expiry) in (int, float)
                and math.isfinite(refresh_expiry) and refresh_expiry > 0
            ):
                latest["refreshTokenExpiresAt"] = int((time.time() + refresh_expiry) * 1000)
            fd, temporary = tempfile.mkstemp(dir=self.credentials_path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as output:
                    json.dump(document, output)
                os.replace(temporary, self.credentials_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            return token

    def read_rate_limits(self, *, force: bool = False) -> UsageSnapshot:
        try:
            if time.monotonic() < self._retry_at:
                raise ClaudeClientError("Claude usage rate limited; retrying in a few minutes")
            env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
            if env_token:
                token = env_token
            else:
                _, oauth = self._credentials()
                token = oauth["accessToken"]
                # Ask usage first: the server decides whether this token is still
                # usable. A stale local expiry must not interrupt working usage.
            if not force and token == self._cache_token and time.monotonic() < self._next_fetch:
                if self._cached is not None:
                    return self._cached
                raise ClaudeClientError("Claude usage rate limited; retrying in a few minutes")
            for attempt in range(2):
                try:
                    document = self._request(Request(USAGE_URL, headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "User-Agent": _USER_AGENT,
                        "Accept": "application/json",
                    }))
                    break
                except HTTPError as exc:
                    exc.close()
                    if exc.code == 401 and attempt == 0 and not env_token:
                        token = self._refresh_token(token)
                        continue
                    if exc.code == 401:
                        raise ClaudeClientError("Claude login cannot read usage; run claude auth login") from None
                    if exc.code == 403:
                        raise ClaudeClientError("Claude usage access denied (HTTP 403); retrying automatically") from None
                    if exc.code == 429:
                        self._retry_at = time.monotonic() + 300
                        raise ClaudeClientError("Claude usage rate limited; retrying in 5m") from None
                    raise ClaudeClientError(f"Claude usage request failed (HTTP {exc.code})") from None
            snapshot = self._parse_snapshot(document)
            self._cached = snapshot
            self._cache_token = token
            self._next_fetch = time.monotonic() + 60
            return snapshot
        except (URLError, OSError, TimeoutError):
            raise ClaudeClientError("Cannot reach Claude usage; check your connection") from None

    @staticmethod
    def _parse_snapshot(document: dict[str, Any]) -> UsageSnapshot:
        if not any(key in document for key in ("five_hour", "seven_day")):
            raise ClaudeClientError("Claude returned no subscription usage windows")
        five = document.get("five_hour") or {}
        week = document.get("seven_day") or {}
        if not isinstance(five, dict) or not isinstance(week, dict):
            raise ClaudeClientError("Claude returned invalid usage windows")
        return UsageSnapshot(
            used_percent=_percent(week.get("utilization")),
            reset_at=_time(week.get("resets_at")),
            window_minutes=10080,
            banked_resets=None,  # The OAuth usage interface does not expose reset offers.
            five_hour_used_percent=_percent(five.get("utilization")),
            five_hour_reset_at=_time(five.get("resets_at")),
            five_hour_window_minutes=300,
        )

    def activate_five_hour_window(self) -> None:
        environment = os.environ.copy()
        for key in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
        ):
            environment.pop(key, None)
        try:
            completed = subprocess.run([
                self.executable, "--print", "--model", "haiku", "--effort", "low",
                "--tools", "", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                "--setting-sources", "", "--no-session-persistence",
                "--system-prompt", "Reply exactly OK.", "Reply exactly OK.",
            ], cwd=tempfile.gettempdir(), env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60,
                **_hidden_subprocess_kwargs())
        except (OSError, subprocess.TimeoutExpired):
            raise ClaudeClientError("Cannot send the tiny Claude activation request") from None
        if completed.returncode:
            raise ClaudeClientError("Claude activation failed; check your login and usage limits")
        # Verification must make fresh reads rather than reuse a pre-activation sample.
        self._next_fetch = 0
