from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import AppState, ResetEvent, UsageSnapshot

_STATE_VERSION = 1


def _default_state_path() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA")
        if root:
            return Path(root) / "CodexWidget" / "state.json"
        return Path.home() / "AppData" / "Local" / "CodexWidget" / "state.json"

    root = os.environ.get("XDG_STATE_HOME")
    if root:
        return Path(root) / "codex-widget" / "state.json"
    return Path.home() / ".local" / "state" / "codex-widget" / "state.json"


def _format_time(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _default_state_path()

    def load(self) -> AppState:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return AppState()
        if not isinstance(document, dict):
            return AppState()

        reset: ResetEvent | None = None
        reset_data = document.get("last_global_reset")
        if isinstance(reset_data, dict):
            event_id = reset_data.get("id")
            announced_at = _parse_time(reset_data.get("announced_at"))
            if isinstance(event_id, str) and announced_at is not None:
                reset = ResetEvent(
                    event_id=event_id,
                    announced_at=announced_at,
                    summary=str(reset_data.get("summary") or ""),
                    url=str(reset_data.get("url") or ""),
                    effective_at=_parse_time(reset_data.get("effective_at")),
                )

        usage: UsageSnapshot | None = None
        usage_data = document.get("last_known_usage")
        if isinstance(usage_data, dict):
            used = usage_data.get("used_percent")
            used_percent = (
                float(used)
                if isinstance(used, (int, float)) and not isinstance(used, bool)
                else None
            )
            five_hour_used = usage_data.get("five_hour_used_percent")
            five_hour_used_percent = (
                float(five_hour_used)
                if isinstance(five_hour_used, (int, float))
                and not isinstance(five_hour_used, bool)
                else None
            )
            window = usage_data.get("window_minutes")
            banked = usage_data.get("banked_resets")
            expirations_data = usage_data.get("banked_reset_expirations")
            expirations = tuple(
                parsed
                for item in expirations_data
                if (parsed := _parse_time(item)) is not None
            ) if isinstance(expirations_data, list) else ()
            usage = UsageSnapshot(
                used_percent=used_percent,
                reset_at=_parse_time(usage_data.get("reset_at")),
                window_minutes=window if isinstance(window, int) else None,
                banked_resets=banked if isinstance(banked, int) else None,
                banked_reset_expirations=expirations,
                five_hour_used_percent=five_hour_used_percent,
                five_hour_reset_at=_parse_time(
                    usage_data.get("five_hour_reset_at")
                ),
                five_hour_window_minutes=(
                    usage_data.get("five_hour_window_minutes")
                    if isinstance(usage_data.get("five_hour_window_minutes"), int)
                    else None
                ),
                checked_at=_parse_time(usage_data.get("checked_at")) or datetime.now(UTC),
            )

        last_seen = document.get("last_seen_reset_id")
        return AppState(
            last_seen_reset_id=last_seen if isinstance(last_seen, str) else None,
            last_global_reset=reset,
            last_known_usage=usage,
            keep_five_hour_window_active=(
                document.get("keep_five_hour_window_active") is True
            ),
            next_window_keeper_due_at=_parse_time(
                document.get("next_window_keeper_due_at")
            ),
            next_window_keeper_retry_at=_parse_time(
                document.get("next_window_keeper_retry_at")
            ),
            last_window_keeper_attempt_at=_parse_time(
                document.get("last_window_keeper_attempt_at")
            ),
            last_window_keeper_success_at=_parse_time(
                document.get("last_window_keeper_success_at")
            ),
            last_window_keeper_error=(
                document.get("last_window_keeper_error")
                if isinstance(document.get("last_window_keeper_error"), str)
                else None
            ),
        )

    def save(self, state: AppState) -> None:
        document: dict[str, Any] = {
            "version": _STATE_VERSION,
            "last_seen_reset_id": state.last_seen_reset_id,
            "last_global_reset": None,
            "last_known_usage": None,
            "keep_five_hour_window_active": state.keep_five_hour_window_active,
            "next_window_keeper_due_at": _format_time(
                state.next_window_keeper_due_at
            ),
            "next_window_keeper_retry_at": _format_time(
                state.next_window_keeper_retry_at
            ),
            "last_window_keeper_attempt_at": _format_time(
                state.last_window_keeper_attempt_at
            ),
            "last_window_keeper_success_at": _format_time(
                state.last_window_keeper_success_at
            ),
            "last_window_keeper_error": state.last_window_keeper_error,
        }
        if state.last_global_reset is not None:
            event = state.last_global_reset
            document["last_global_reset"] = {
                "id": event.event_id,
                "announced_at": _format_time(event.announced_at),
                "effective_at": _format_time(event.effective_at),
                "summary": event.summary,
                "url": event.url,
            }
        if state.last_known_usage is not None:
            usage = state.last_known_usage
            document["last_known_usage"] = {
                "used_percent": usage.used_percent,
                "reset_at": _format_time(usage.reset_at),
                "window_minutes": usage.window_minutes,
                "five_hour_used_percent": usage.five_hour_used_percent,
                "five_hour_reset_at": _format_time(usage.five_hour_reset_at),
                "five_hour_window_minutes": usage.five_hour_window_minutes,
                "banked_resets": usage.banked_resets,
                "banked_reset_expirations": [
                    _format_time(value) for value in usage.banked_reset_expirations
                ],
                "checked_at": _format_time(usage.checked_at),
            }

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
