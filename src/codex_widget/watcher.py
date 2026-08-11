from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from enum import Enum

from .models import AppState, ResetEvent, UsageSnapshot, utc_now
from .state import StateStore
from .tracker import TrackerClient
_PENDING_RESET_MAX_USAGE_PERCENT = 5


class PollOutcome(Enum):
    SEEDED = "seeded"
    UNCHANGED = "unchanged"
    NEW_RESET = "new_reset"


class ResetWatcher:
    def __init__(self, tracker: TrackerClient, state_store: StateStore) -> None:
        self.tracker = tracker
        self.state_store = state_store

    def poll_once(self) -> tuple[PollOutcome, ResetEvent, AppState]:
        event = self.tracker.fetch_latest_relevant()
        state = self.state_store.load()
        if not event.confirmed:
            usage = state.last_known_usage
            if not _fresh_low_usage(usage):
                return PollOutcome.UNCHANGED, event, state
            event = replace(
                event,
                effective_at=usage.checked_at,
                confirmed=True,
            )
        if state.last_seen_reset_id == event.event_id:
            return PollOutcome.UNCHANGED, event, state

        first_observation = state.last_seen_reset_id is None
        state.last_seen_reset_id = event.event_id
        state.last_global_reset = event
        self.state_store.save(state)
        outcome = PollOutcome.SEEDED if first_observation else PollOutcome.NEW_RESET
        return outcome, event, state


def _fresh_low_usage(usage: UsageSnapshot | None) -> bool:
    if (
        usage is None
        or usage.used_percent is None
        or usage.used_percent > _PENDING_RESET_MAX_USAGE_PERCENT
    ):
        return False
    age = utc_now() - usage.checked_at
    return -timedelta(seconds=30) <= age <= timedelta(minutes=3)


def account_reset_observed(
    previous: UsageSnapshot | None, current: UsageSnapshot
) -> bool:
    if current.used_percent is None:
        return False
    if current.used_percent <= 1:
        return True
    if previous is None or previous.used_percent is None:
        return False
    return current.used_percent + 1 < previous.used_percent
