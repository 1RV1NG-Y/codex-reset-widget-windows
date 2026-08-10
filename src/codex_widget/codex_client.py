from __future__ import annotations

import json
import selectors
import subprocess
import time
from datetime import UTC, datetime
from typing import Any

from .models import UsageSnapshot


class CodexClientError(RuntimeError):
    pass


def _epoch_timestamp(value: object) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


class CodexClient:
    def __init__(self, executable: str = "codex", *, timeout: float = 10.0) -> None:
        self.executable = executable
        self.timeout = timeout

    def read_rate_limits(self) -> UsageSnapshot:
        try:
            process = subprocess.Popen(
                [self.executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise CodexClientError(f"cannot start Codex app-server: {exc}") from exc

        try:
            self._send(
                process,
                {
                    "method": "initialize",
                    "id": 1,
                    "params": {
                        "clientInfo": {
                            "name": "codex-widget",
                            "title": "Codex Widget",
                            "version": "0.1.0",
                        }
                    },
                },
            )
            deadline = time.monotonic() + self.timeout
            self._wait_for_response(process, 1, deadline)
            self._send(process, {"method": "initialized"})
            self._send(process, {"method": "account/rateLimits/read", "id": 2})
            response = self._wait_for_response(process, 2, deadline)
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexClientError("Codex returned no rate-limit result")
            return self._parse_snapshot(result)
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    @staticmethod
    def _send(process: subprocess.Popen[str], message: dict[str, Any]) -> None:
        if process.stdin is None:
            raise CodexClientError("Codex app-server stdin is unavailable")
        try:
            process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise CodexClientError("Codex app-server closed unexpectedly") from exc

    @staticmethod
    def _wait_for_response(
        process: subprocess.Popen[str], request_id: int, deadline: float
    ) -> dict[str, Any]:
        if process.stdout is None:
            raise CodexClientError("Codex app-server stdout is unavailable")
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CodexClientError("Codex app-server request timed out")
                if not selector.select(remaining):
                    raise CodexClientError("Codex app-server request timed out")
                line = process.stdout.readline()
                if not line:
                    raise CodexClientError("Codex app-server exited before responding")
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                error = message.get("error")
                if error is not None:
                    raise CodexClientError(f"Codex app-server error: {error}")
                return message
        finally:
            selector.close()

    @staticmethod
    def _parse_snapshot(result: dict[str, Any]) -> UsageSnapshot:
        by_id = result.get("rateLimitsByLimitId")
        limits = by_id.get("codex") if isinstance(by_id, dict) else None
        if not isinstance(limits, dict):
            limits = result.get("rateLimits")
        if not isinstance(limits, dict):
            raise CodexClientError("Codex response contains no Codex rate limit")

        windows = [
            candidate
            for key in ("primary", "secondary")
            if isinstance((candidate := limits.get(key)), dict)
        ]
        weekly = next(
            (
                candidate
                for candidate in windows
                if candidate.get("windowDurationMins") == 7 * 24 * 60
            ),
            windows[0] if windows else {},
        )
        window = weekly.get("windowDurationMins")
        window_minutes = (
            int(window)
            if isinstance(window, (int, float)) and not isinstance(window, bool)
            else None
        )

        reset_credits = result.get("rateLimitResetCredits")
        banked_resets: int | None = None
        expirations: list[datetime] = []
        if isinstance(reset_credits, dict):
            count = reset_credits.get("availableCount")
            if isinstance(count, int) and not isinstance(count, bool):
                banked_resets = count
            credits = reset_credits.get("credits")
            if isinstance(credits, list):
                for credit in credits:
                    if not isinstance(credit, dict):
                        continue
                    expiration = _epoch_timestamp(
                        credit.get("expiresAt") or credit.get("expires_at")
                    )
                    if expiration is not None:
                        expirations.append(expiration)

        return UsageSnapshot(
            used_percent=_number(weekly.get("usedPercent")),
            reset_at=_epoch_timestamp(weekly.get("resetsAt")),
            window_minutes=window_minutes,
            banked_resets=banked_resets,
            banked_reset_expirations=tuple(sorted(expirations)),
        )
