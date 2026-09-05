from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import UsageSnapshot


class CodexClientError(RuntimeError):
    pass


_STDOUT_EOF = object()
_VERSION_PARTS = re.compile(r"\d+")


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


def _looks_like_path(value: str) -> bool:
    return any(separator in value for separator in ("/", "\\"))


def _windows_codex_app_executable() -> str | None:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        return None

    bin_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
    candidates: list[Path] = []
    direct = bin_root / "codex.exe"
    if direct.is_file():
        candidates.append(direct)
    try:
        candidates.extend(
            candidate
            for candidate in bin_root.glob("*/codex.exe")
            if candidate.is_file()
        )
    except OSError:
        pass
    if not candidates:
        return None

    def sort_key(candidate: Path) -> tuple[float, tuple[int, ...], str]:
        version = tuple(
            int(part) for part in _VERSION_PARTS.findall(candidate.parent.name)
        )
        try:
            modified_at = candidate.stat().st_mtime
        except OSError:
            modified_at = 0.0
        return modified_at, version, str(candidate)

    return str(max(candidates, key=sort_key))


def _resolve_executable(executable: str) -> str:
    if _looks_like_path(executable):
        return executable

    resolved = shutil.which(executable)
    if resolved is not None:
        return resolved

    if os.name == "nt" and executable.lower() in {"codex", "codex.exe", "codex.cmd"}:
        fallback = _windows_codex_app_executable()
        if fallback is not None:
            return fallback

    return executable


def _hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}

    kwargs: dict[str, Any] = {}
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


class _ProcessLineReader:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        if process.stdout is None:
            raise CodexClientError("Codex app-server stdout is unavailable")
        self._stdout = process.stdout
        self._queue: queue.Queue[str | BaseException | object] = queue.Queue()
        self._thread = threading.Thread(
            target=self._read_lines,
            name="codex-widget-app-server-stdout",
            daemon=True,
        )
        self._thread.start()

    def readline(self, deadline: float) -> str:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CodexClientError("Codex app-server request timed out")
        try:
            item = self._queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise CodexClientError("Codex app-server request timed out") from exc
        if item is _STDOUT_EOF:
            raise CodexClientError("Codex app-server exited before responding")
        if isinstance(item, BaseException):
            raise CodexClientError("Codex app-server stdout read failed") from item
        return item

    def close(self) -> None:
        self._thread.join(timeout=1)
        if not self._thread.is_alive():
            try:
                if not self._stdout.closed:
                    self._stdout.close()
            except OSError:
                pass

    def _read_lines(self) -> None:
        try:
            while True:
                line = self._stdout.readline()
                if line == "":
                    self._queue.put(_STDOUT_EOF)
                    return
                self._queue.put(line)
        except (OSError, ValueError) as exc:
            self._queue.put(exc)


class CodexClient:
    def __init__(
        self,
        executable: str = "codex",
        *,
        timeout: float = 10.0,
        activation_timeout: float = 60.0,
        activation_model: str = "gpt-5.6-luna",
    ) -> None:
        self.executable = _resolve_executable(executable)
        self.timeout = timeout
        self.activation_timeout = activation_timeout
        self.activation_model = activation_model

    def read_rate_limits(self) -> UsageSnapshot:
        reader: _ProcessLineReader | None = None
        failed = True
        try:
            process = subprocess.Popen(
                [self.executable, "app-server", "--stdio"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                **_hidden_subprocess_kwargs(),
            )
        except OSError as exc:
            raise CodexClientError(f"cannot start Codex app-server: {exc}") from exc

        try:
            reader = _ProcessLineReader(process)
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
            self._wait_for_response(reader, 1, deadline)
            self._send(process, {"method": "initialized"})
            self._send(process, {"method": "account/rateLimits/read", "id": 2})
            response = self._wait_for_response(reader, 2, deadline)
            result = response.get("result")
            if not isinstance(result, dict):
                raise CodexClientError("Codex returned no rate-limit result")
            snapshot = self._parse_snapshot(result)
            failed = False
            return snapshot
        finally:
            self._close_app_server(process, reader, force=failed)

    def activate_five_hour_window(self) -> None:
        arguments = [
            self.executable,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-rules",
            "--ignore-user-config",
            "--sandbox",
            "read-only",
            "--model",
            self.activation_model,
            "-c",
            'model_reasoning_effort="low"',
            "--color",
            "never",
            "--cd",
            tempfile.gettempdir(),
            "Reply exactly OK.",
        ]
        try:
            completed = subprocess.run(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.activation_timeout,
                **_hidden_subprocess_kwargs(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CodexClientError(
                f"cannot activate five-hour window: {exc}"
            ) from exc
        if completed.returncode == 0:
            return
        detail = completed.stderr.strip().splitlines()
        reason = detail[-1] if detail else f"exit status {completed.returncode}"
        raise CodexClientError(f"five-hour activation failed: {reason}")

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
        reader: _ProcessLineReader, request_id: int, deadline: float
    ) -> dict[str, Any]:
        while True:
            line = reader.readline(deadline)
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

    @staticmethod
    def _close_app_server(
        process: subprocess.Popen[str],
        reader: _ProcessLineReader | None,
        *,
        force: bool,
    ) -> None:
        if force and process.poll() is None:
            CodexClient._terminate_app_server(process)

        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                CodexClient._terminate_app_server(process)

        if reader is not None:
            reader.close()
        elif process.stdout is not None and not process.stdout.closed:
            process.stdout.close()

    @staticmethod
    def _terminate_app_server(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    **_hidden_subprocess_kwargs(),
                )
            except (OSError, subprocess.TimeoutExpired):
                pass

        try:
            process.terminate()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()

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
        five_hour = next(
            (
                candidate
                for candidate in windows
                if candidate.get("windowDurationMins") == 5 * 60
            ),
            {},
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
            five_hour_used_percent=_number(five_hour.get("usedPercent")),
            five_hour_reset_at=_epoch_timestamp(five_hour.get("resetsAt")),
            five_hour_window_minutes=(
                300
                if five_hour.get("windowDurationMins") == 300
                else None
            ),
        )
