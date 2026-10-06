from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

try:
    from gi.repository import GLib
except ImportError:  # Windows: windows_app installs its Tk-backed equivalent.
    GLib = None

from .codex_client import CodexClientError
from .models import ResetEvent, UsageSnapshot, utc_now
from .state import StateStore

_WINDOW_KEEPER_GRACE_SECONDS = 10
_WINDOW_KEEPER_RETRY_SECONDS = 60
_WINDOW_KEEPER_PROPAGATION_SECONDS = 60
_WINDOW_KEEPER_WATCHDOG_SECONDS = 60
_WINDOW_KEEPER_DEFAULT_WINDOW_SECONDS = 5 * 60 * 60
_WINDOW_KEEPER_IDLE_RESET_TOLERANCE_SECONDS = 120


def _keeper_view_is_visible(keeper: Any) -> bool:
    return (
        keeper.window is not None
        and getattr(keeper, "selected_provider", "codex")
        == getattr(keeper, "provider", "codex")
    )


class WindowKeeperMixin:
    """Shared scheduler; ``codex`` is the legacy name of its usage client."""

    def _read_and_store_usage(self) -> UsageSnapshot:
        with self._account_query_lock:
            return self._read_and_store_usage_unlocked()

    def _read_and_store_usage_unlocked(self) -> UsageSnapshot:
        usage = self.codex.read_rate_limits()
        with self._state_lock:
            state = self.state_store.load()
            state.last_known_usage = usage
            self.state_store.save(state)
        return usage

    def set_window_keeper_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            state = self.state_store.load()
            state.keep_five_hour_window_active = enabled
            if not enabled:
                state.next_window_keeper_due_at = None
                state.next_window_keeper_retry_at = None
            self.state_store.save(state)
        if enabled:
            self._start_window_keeper_watchdog()
            self._set_window_keeper_message("Checking the 5-hour window…")
            self._refresh_window_keeper_schedule()
        else:
            self._cancel_window_keeper_timer()
            self._cancel_window_keeper_watchdog()
            self._set_window_keeper_message("Automatic 5-hour rolling is off")

    def _window_keeper_enabled(self) -> bool:
        with self._state_lock:
            return self.state_store.load().keep_five_hour_window_active

    @staticmethod
    def _window_keeper_window_seconds(usage: UsageSnapshot | None = None) -> int:
        if (
            usage is not None
            and usage.five_hour_window_minutes is not None
            and usage.five_hour_window_minutes > 0
        ):
            return usage.five_hour_window_minutes * 60
        return _WINDOW_KEEPER_DEFAULT_WINDOW_SECONDS

    def _usage_has_active_five_hour_window(
        self, usage: UsageSnapshot, now: datetime
    ) -> bool:
        return (
            usage.five_hour_used_percent is not None
            and usage.five_hour_used_percent > 0
            and usage.five_hour_reset_at is not None
            and usage.five_hour_reset_at > now
        )

    def _usage_looks_like_idle_five_hour_window(
        self, usage: UsageSnapshot, now: datetime
    ) -> bool:
        if usage.five_hour_reset_at is None or usage.five_hour_reset_at <= now:
            return True
        if usage.five_hour_used_percent is None or usage.five_hour_used_percent > 0:
            return False
        remaining = (usage.five_hour_reset_at - usage.checked_at).total_seconds()
        return (
            remaining
            >= self._window_keeper_window_seconds(usage)
            - _WINDOW_KEEPER_IDLE_RESET_TOLERANCE_SECONDS
        )

    def _choose_window_keeper_due_at(
        self, usage: UsageSnapshot, state: object, now: datetime
    ) -> datetime:
        observed_target = (
            usage.five_hour_reset_at
            + timedelta(seconds=_WINDOW_KEEPER_GRACE_SECONDS)
            if usage.five_hour_reset_at is not None
            and usage.five_hour_reset_at > now
            else None
        )
        stored_target = getattr(state, "next_window_keeper_due_at", None)
        if self._usage_has_active_five_hour_window(usage, now) and observed_target:
            if stored_target is None or stored_target <= now:
                return observed_target
            return min(stored_target, observed_target)
        if stored_target is not None:
            return stored_target
        if self._usage_looks_like_idle_five_hour_window(usage, now):
            last_success = getattr(state, "last_window_keeper_success_at", None)
            if last_success is not None:
                return last_success + timedelta(
                    seconds=self._window_keeper_window_seconds(usage)
                    + _WINDOW_KEEPER_GRACE_SECONDS
                )
            return now + timedelta(seconds=1)
        if observed_target is not None:
            return observed_target
        return now + timedelta(seconds=1)

    def _refresh_window_keeper_schedule(self) -> None:
        if self._window_keeper_busy or not self._window_keeper_enabled():
            return
        self._window_keeper_busy = True
        self._run_async(
            self._read_and_store_usage,
            self._window_keeper_schedule_refreshed,
        )

    def _window_keeper_schedule_refreshed(
        self,
        usage: UsageSnapshot | None,
        error: BaseException | None,
    ) -> None:
        self._window_keeper_busy = False
        if not self._window_keeper_enabled():
            return
        if error is not None or usage is None:
            detail = str(error) if error is not None else "usage unavailable"
            self._schedule_window_keeper_retry(
                _WINDOW_KEEPER_RETRY_SECONDS,
                f"5-hour auto-roll unavailable; retrying in 1m: {detail}",
            )
            return
        self._schedule_window_keeper_from_usage(usage)

    def _schedule_window_keeper_from_usage(self, usage: UsageSnapshot) -> None:
        if not self._window_keeper_enabled():
            return
        now = utc_now()
        with self._state_lock:
            state = self.state_store.load()
        if (
            usage.used_percent is not None
            and usage.used_percent >= 100
            and usage.reset_at is not None
            and usage.reset_at > now
        ):
            target = usage.reset_at + timedelta(
                seconds=_WINDOW_KEEPER_GRACE_SECONDS
            )
            self._schedule_window_keeper_at(
                target,
                "5-hour auto-roll paused at the weekly limit; "
                f"resumes {target.astimezone():%a %H:%M}",
            )
            return
        if state.next_window_keeper_retry_at is not None:
            if state.next_window_keeper_retry_at > now:
                self._schedule_window_keeper_retry_at(
                    state.next_window_keeper_retry_at,
                    "5-hour auto-roll waiting for retry backoff"
                    f"{self._window_keeper_history_suffix()}",
                )
                return
        target = self._choose_window_keeper_due_at(usage, state, now)
        if target > now:
            self._schedule_window_keeper_at(
                target,
                "5-hour auto-roll on · "
                f"next tiny request {target.astimezone():%a %H:%M}"
                f"{self._window_keeper_history_suffix()}",
            )
            return
        self._schedule_window_keeper_at(
            target,
            "5-hour auto-roll on · activating an overdue window"
            f"{self._window_keeper_history_suffix()}",
        )

    def _schedule_window_keeper_at(
        self, target: datetime, message: str
    ) -> None:
        self._cancel_window_keeper_timer()
        with self._state_lock:
            state = self.state_store.load()
            state.next_window_keeper_due_at = target
            state.next_window_keeper_retry_at = None
            self.state_store.save(state)
        seconds = max(1, math.ceil((target - utc_now()).total_seconds()))
        self._window_keeper_source = GLib.timeout_add_seconds(
            seconds,
            self._window_keeper_timer_fired,
        )
        self._set_window_keeper_message(message)

    def _schedule_window_keeper_retry(self, seconds: int, message: str) -> None:
        self._schedule_window_keeper_retry_at(
            utc_now() + timedelta(seconds=seconds), message
        )

    def _schedule_window_keeper_retry_at(
        self, target: datetime, message: str
    ) -> None:
        self._cancel_window_keeper_timer()
        with self._state_lock:
            state = self.state_store.load()
            state.next_window_keeper_retry_at = target
            self.state_store.save(state)
        seconds = max(1, math.ceil((target - utc_now()).total_seconds()))
        self._window_keeper_source = GLib.timeout_add_seconds(
            seconds,
            self._window_keeper_retry_fired,
        )
        self._set_window_keeper_message(message)

    def _cancel_window_keeper_timer(self) -> None:
        if self._window_keeper_source is not None:
            GLib.source_remove(self._window_keeper_source)
            self._window_keeper_source = None

    def _start_window_keeper_watchdog(self) -> None:
        if self._window_keeper_watchdog_source is None:
            self._window_keeper_watchdog_source = GLib.timeout_add_seconds(
                _WINDOW_KEEPER_WATCHDOG_SECONDS,
                self._window_keeper_watchdog_tick,
            )

    def _cancel_window_keeper_watchdog(self) -> None:
        if self._window_keeper_watchdog_source is not None:
            GLib.source_remove(self._window_keeper_watchdog_source)
            self._window_keeper_watchdog_source = None

    def _window_keeper_watchdog_tick(self) -> bool:
        if not self._window_keeper_enabled():
            self._window_keeper_watchdog_source = None
            return GLib.SOURCE_REMOVE
        if self._window_keeper_busy:
            return GLib.SOURCE_CONTINUE
        with self._state_lock:
            state = self.state_store.load()
            usage = state.last_known_usage
            due_at = state.next_window_keeper_due_at
            retry_at = state.next_window_keeper_retry_at
        now = utc_now()
        if (
            usage is not None
            and usage.used_percent is not None
            and usage.used_percent >= 100
            and usage.reset_at is not None
            and usage.reset_at > now
        ):
            return GLib.SOURCE_CONTINUE
        if retry_at is not None and retry_at > now:
            if self._window_keeper_source is None:
                self._schedule_window_keeper_retry_at(
                    retry_at,
                    "5-hour auto-roll waiting for retry backoff"
                    f"{self._window_keeper_history_suffix()}",
                )
            return GLib.SOURCE_CONTINUE
        if retry_at is not None and retry_at <= now:
            self._cancel_window_keeper_timer()
            self._refresh_window_keeper_schedule()
            return GLib.SOURCE_CONTINUE
        if due_at is not None:
            if due_at <= now:
                self._cancel_window_keeper_timer()
                if (
                    usage is not None
                    and self._usage_has_active_five_hour_window(usage, now)
                ):
                    self._schedule_window_keeper_from_usage(usage)
                else:
                    self._window_keeper_timer_fired()
                return GLib.SOURCE_CONTINUE
            self._schedule_window_keeper_at(
                due_at,
                "5-hour auto-roll on · "
                f"next tiny request {due_at.astimezone():%a %H:%M}"
                f"{self._window_keeper_history_suffix()}",
            )
            return GLib.SOURCE_CONTINUE
        if (
            usage is None
            or usage.five_hour_reset_at is None
            or usage.five_hour_reset_at
            + timedelta(seconds=_WINDOW_KEEPER_GRACE_SECONDS)
            <= now
        ):
            self._refresh_window_keeper_schedule()
        return GLib.SOURCE_CONTINUE

    def _window_keeper_timer_fired(self) -> bool:
        self._window_keeper_source = None
        if not self._window_keeper_enabled() or self._window_keeper_busy:
            return GLib.SOURCE_REMOVE
        self._window_keeper_busy = True
        self._set_window_keeper_message("Sending the tiny 5-hour activation request…")
        self._run_async(
            self._activate_and_read_usage,
            self._window_keeper_activation_finished,
        )
        return GLib.SOURCE_REMOVE

    def _window_keeper_retry_fired(self) -> bool:
        self._window_keeper_source = None
        self._refresh_window_keeper_schedule()
        return GLib.SOURCE_REMOVE

    def _activate_and_read_usage(self) -> UsageSnapshot | None:
        with self._account_query_lock:
            if not self._window_keeper_enabled():
                return None
            attempted_at = utc_now()
            self._record_window_keeper_outcome(attempted_at=attempted_at)
            try:
                self.codex.activate_five_hour_window()
                usage = self._verify_window_keeper()
                if usage is None:
                    return None
            except Exception as exc:
                self._record_window_keeper_outcome(
                    attempted_at=attempted_at,
                    error=str(exc),
                )
                raise
            self._record_window_keeper_outcome(
                attempted_at=attempted_at,
                succeeded_at=utc_now(),
            )
            with self._state_lock:
                state = self.state_store.load()
                state.next_window_keeper_due_at = usage.five_hour_reset_at + timedelta(
                    seconds=_WINDOW_KEEPER_GRACE_SECONDS
                )
                self.state_store.save(state)
            return usage

    def _verify_window_keeper(self) -> UsageSnapshot | None:
        samples = []
        for index in range(3):
            if index:
                time.sleep(15)
            if not self._window_keeper_enabled():
                return None
            samples.append(self._read_and_store_usage_unlocked())
        resets = [sample.five_hour_reset_at for sample in samples]
        if any(reset is None or reset <= utc_now() for reset in resets):
            raise CodexClientError("Activation unconfirmed: no future reset reported")
        drift = (max(resets) - min(resets)).total_seconds()
        if drift > 3:
            raise CodexClientError(
                f"Activation unconfirmed: reset moved {drift:.0f}s during 30s verification"
            )
        return samples[-1]

    def _record_window_keeper_outcome(
        self,
        *,
        attempted_at: datetime,
        succeeded_at: datetime | None = None,
        error: str | None = None,
    ) -> None:
        with self._state_lock:
            state = self.state_store.load()
            state.last_window_keeper_attempt_at = attempted_at
            if succeeded_at is not None:
                state.last_window_keeper_success_at = succeeded_at
                state.last_window_keeper_verified_at = succeeded_at
                state.window_keeper_failures = 0
                state.next_window_keeper_due_at = succeeded_at + timedelta(
                    seconds=_WINDOW_KEEPER_DEFAULT_WINDOW_SECONDS
                    + _WINDOW_KEEPER_GRACE_SECONDS
                )
                state.next_window_keeper_retry_at = None
            if error:
                state.window_keeper_failures += 1
            state.last_window_keeper_error = error[:500] if error else None
            self.state_store.save(state)

    def _window_keeper_history_suffix(self) -> str:
        with self._state_lock:
            state = self.state_store.load()
        if state.last_window_keeper_error:
            return f" · {state.last_window_keeper_error}"
        if state.last_window_keeper_verified_at is not None:
            return (
                " · last verified "
                f"{state.last_window_keeper_verified_at.astimezone():%H:%M}"
            )
        return " · window activation not verified"

    def _window_keeper_activation_finished(
        self,
        usage: UsageSnapshot | None,
        error: BaseException | None,
    ) -> None:
        self._window_keeper_busy = False
        if not self._window_keeper_enabled():
            return
        if error is not None or usage is None:
            detail = str(error) if error is not None else "usage unavailable"
            with self._state_lock:
                failures = self.state_store.load().window_keeper_failures
            cooldown = failures > 0 and failures % 3 == 0
            delay = 5 * 60 * 60 if cooldown else _WINDOW_KEEPER_RETRY_SECONDS
            wait = "5h cooldown after three failures" if cooldown else "retrying in 1m"
            self._schedule_window_keeper_retry(
                delay,
                f"5-hour activation unconfirmed; {wait}: {detail}",
            )
            return
        if (
            usage.five_hour_reset_at is None
            or usage.five_hour_reset_at
            <= utc_now() + timedelta(seconds=_WINDOW_KEEPER_GRACE_SECONDS)
        ):
            self._schedule_window_keeper_retry(
                _WINDOW_KEEPER_PROPAGATION_SECONDS,
                "Activation sent; waiting for the new 5-hour window",
            )
            return
        self._schedule_window_keeper_from_usage(usage)
        if _keeper_view_is_visible(self) and self.window.get_visible():
            state = self.state_store.load()
            self.window.show_usage(usage, state.last_global_reset)

    def _set_window_keeper_message(self, message: str) -> None:
        self._window_keeper_message = message
        if _keeper_view_is_visible(self):
            self.window.set_window_keeper_state(
                self._window_keeper_enabled(),
                message,
            )


class ClaudeWindowKeeper(WindowKeeperMixin):
    provider = "claude"

    def __init__(self, application: Any, client: Any, state_store: StateStore) -> None:
        self.application = application
        self.codex = client
        self.state_store = state_store
        self._state_lock = threading.Lock()
        self._account_query_lock = threading.Lock()
        self._window_keeper_source = None
        self._window_keeper_watchdog_source = None
        self._window_keeper_busy = False
        self._window_keeper_message = "Automatic 5-hour rolling is off"
        self._verifying = False

    @property
    def window(self) -> Any:
        return self.application.window

    @property
    def selected_provider(self) -> str:
        return self.application.selected_provider

    def _run_async(self, work: Callable, done: Callable) -> None:
        self.application._run_async(work, done)

    def start(self) -> None:
        if self._window_keeper_enabled():
            self._start_window_keeper_watchdog()
            self._refresh_window_keeper_schedule()

    def _verify_window_keeper(self) -> UsageSnapshot | None:
        self._verifying = True
        try:
            return super()._verify_window_keeper()
        finally:
            self._verifying = False

    def _read_and_store_usage_unlocked(self) -> UsageSnapshot:
        usage = self.codex.read_rate_limits(force=self._verifying)
        event = None
        with self._state_lock:
            state = self.state_store.load()
            previous = state.last_known_usage
            if previous is not None:
                for name, before, after in (
                    ("weekly", previous.used_percent, usage.used_percent),
                    ("five-hour", previous.five_hour_used_percent, usage.five_hour_used_percent),
                ):
                    if before is not None and after is not None and before > after + 1:
                        event = ResetEvent(
                            event_id=f"claude-{usage.checked_at.isoformat()}",
                            announced_at=usage.checked_at,
                            effective_at=usage.checked_at,
                            summary=f"Claude {name} usage reset observed",
                            url="https://claude.ai/settings/usage",
                        )
                        state.last_global_reset = event
                        break
            state.last_known_usage = usage
            self.state_store.save(state)
        if event is not None and usage.checked_at - previous.checked_at < timedelta(minutes=10):
            GLib.idle_add(self.application.claude_reset_observed, event)
        return usage
