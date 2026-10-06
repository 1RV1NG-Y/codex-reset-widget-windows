from __future__ import annotations

import os
import subprocess
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3  # noqa: E402
except (ImportError, ValueError):
    AyatanaAppIndicator3 = None

from .claude_client import ClaudeClient, detect_claude
from .codex_client import CodexClient, CodexClientError
from .models import ResetEvent, UsageSnapshot, utc_now
from .state import StateStore
from .tracker import TrackerClient, TrackerError
from .ui import WidgetWindow
from .watcher import PollOutcome, ResetWatcher, account_reset_observed
from .window_keeper import ClaudeWindowKeeper, WindowKeeperMixin

_APPLICATION_ID = "io.github.codex_widget.CodexWidget"
_DEFAULT_POLL_SECONDS = 60
_USAGE_REFRESH_SECONDS = 60
_RESET_NOTIFICATION_MAX_AGE = timedelta(hours=6)


class CodexWidgetApplication(WindowKeeperMixin, Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=_APPLICATION_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.window: WidgetWindow | None = None
        self.state_store = StateStore()
        self.codex = CodexClient()
        self.claude_executable = detect_claude()
        self.selected_provider = self.state_store.load().selected_provider
        if not self.claude_executable:
            self.selected_provider = "codex"
        self.claude_keeper = ClaudeWindowKeeper(
            self,
            ClaudeClient(self.claude_executable or "claude"),
            StateStore(self.state_store.path.with_name("claude-state.json")),
        )
        self._claude_refreshing = False
        self._claude_poll_source: int | None = None
        self.watcher = ResetWatcher(TrackerClient(), self.state_store)
        self._resident = False
        self._polling = False
        self._state_lock = threading.Lock()
        self._account_query_lock = threading.Lock()
        self._notification_ids: dict[str, int] = {}
        self._indicator: Any | None = None
        self._tray_menu: Gtk.Menu | None = None
        self._usage_refresh_source: int | None = None
        self._usage_refreshing = False
        self._window_keeper_source: int | None = None
        self._window_keeper_watchdog_source: int | None = None
        self._window_keeper_busy = False
        self._window_keeper_message = "Automatic 5-hour rolling is off"

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        show_action = Gio.SimpleAction.new("show", None)
        show_action.connect("activate", lambda *_args: self.show_widget())
        self.add_action(show_action)
        for provider in ("codex", "claude"):
            action = Gio.SimpleAction.new(f"show-{provider}", None)
            action.connect("activate", lambda *_args, name=provider: self._show_provider(name))
            self.add_action(action)
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_args: self.quit())
        self.add_action(quit_action)
        self._create_tray_indicator()

    def _create_tray_indicator(self) -> None:
        menu = Gtk.Menu()
        open_item = Gtk.MenuItem(label="Open Codex Widget")
        open_item.connect("activate", lambda *_args: self.show_widget())
        menu.append(open_item)

        check_item = Gtk.MenuItem(label="Check usage and resets now")
        check_item.connect("activate", lambda *_args: self._check_now())
        menu.append(check_item)
        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit Codex Widget")
        quit_item.connect("activate", lambda *_args: self.quit())
        menu.append(quit_item)
        menu.show_all()
        self._tray_menu = menu

        icon_path = Path(__file__).with_name("assets")
        icon_file = icon_path / "codex-widget-symbolic.svg"
        if AyatanaAppIndicator3 is not None:
            indicator = AyatanaAppIndicator3.Indicator.new(
                _APPLICATION_ID,
                "codex-widget-symbolic",
                AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
            )
            indicator.set_icon_theme_path(str(icon_path))
            indicator.set_title("Codex Widget")
            indicator.set_menu(menu)
            indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
            self._indicator = indicator
            return

        indicator = Gtk.StatusIcon.new_from_file(str(icon_file))
        indicator.set_title("Codex Widget")
        indicator.set_tooltip_text("Codex usage and reset status")
        indicator.connect("activate", lambda *_args: self.show_widget())
        indicator.connect("popup-menu", self._show_status_icon_menu)
        indicator.set_visible(True)
        self._indicator = indicator

    def _show_status_icon_menu(
        self,
        icon: Gtk.StatusIcon,
        button: int,
        activate_time: int,
    ) -> None:
        if self._tray_menu is not None:
            self._tray_menu.popup(
                None,
                None,
                Gtk.StatusIcon.position_menu,
                icon,
                button,
                activate_time,
            )

    def do_activate(self) -> None:
        self._ensure_resident()
        self.show_widget()

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        arguments = list(command_line.get_arguments())[1:]
        if "--help" in arguments:
            command_line.print_literal(
                "Usage: codex-widget [--daemon | --demo-reset | --quit]\n"
                "Without options, show the widget and keep its reset watcher resident.\n"
                "--demo-reset shows the two-stage reset notification without changing state.\n"
            )
            return 0
        unknown = [
            argument
            for argument in arguments
            if argument not in {"--daemon", "--demo-reset", "--quit"}
        ]
        if unknown:
            command_line.printerr_literal(f"Unknown option: {unknown[0]}\n")
            return 2
        if "--quit" in arguments:
            self.quit()
            return 0
        self._ensure_resident()
        if "--daemon" in arguments:
            command_line.print_literal("codex-widget: watcher running\n")
        elif "--demo-reset" in arguments:
            self.show_reset_demo()
            command_line.print_literal(
                "codex-widget: showing reset notification demo\n"
            )
        else:
            self.show_widget()
        return 0

    def _ensure_resident(self) -> None:
        if self._resident:
            return
        self._resident = True
        self.hold()
        interval = self._poll_interval()
        GLib.timeout_add_seconds(interval, self._poll_tick)
        self._poll_tick()
        if self.state_store.load().keep_five_hour_window_active:
            self._start_window_keeper_watchdog()
            self._set_window_keeper_message("Checking the 5-hour window…")
            self._refresh_window_keeper_schedule()
        if self.claude_executable:
            self.claude_keeper.start()
            self._start_claude_polling()

    @staticmethod
    def _poll_interval() -> int:
        raw = os.environ.get("CODEX_WIDGET_POLL_SECONDS")
        try:
            return max(30, int(raw)) if raw is not None else _DEFAULT_POLL_SECONDS
        except ValueError:
            return _DEFAULT_POLL_SECONDS

    def _poll_tick(self) -> bool:
        if not self._polling:
            self._polling = True
            self._run_async(self._poll_once, self._poll_finished)
        return GLib.SOURCE_CONTINUE

    def _check_now(self) -> None:
        self._poll_tick()
        self._refresh_visible_usage()

    def _start_claude_polling(self) -> None:
        if self._claude_poll_source is None:
            self._claude_poll_source = GLib.timeout_add_seconds(300, self._claude_poll_tick)

    def _poll_once(self) -> tuple[PollOutcome, ResetEvent, UsageSnapshot | None]:
        with self._state_lock:
            outcome, event, state = self.watcher.poll_once()
        return outcome, event, state.last_known_usage

    def _poll_finished(
        self,
        result: tuple[PollOutcome, ResetEvent, UsageSnapshot | None] | None,
        error: BaseException | None,
    ) -> None:
        self._polling = False
        if error is not None:
            if self.window is not None and self.window.get_visible() and self.selected_provider == "codex":
                self.window.status.set_text(f"Reset tracker unavailable: {error}")
            return
        if result is None:
            return
        outcome, event, previous_usage = result
        if outcome is not PollOutcome.NEW_RESET:
            return
        if event.announced_at < utc_now() - _RESET_NOTIFICATION_MAX_AGE:
            if (
                self.window is not None
                and self.window.get_visible()
                and self.selected_provider == "codex"
                and previous_usage is not None
            ):
                self.window.show_usage(previous_usage, event)
            return
        self._send_reset_notification(
            event,
            "🙏 Tibo announced a global reset. Checking your Codex account…",
            final=False,
        )
        self._run_async(
            lambda: self._read_and_store_usage(),
            lambda usage, exc: self._reset_account_check_finished(
                event, previous_usage, usage, exc
            ),
        )

    def show_widget(self) -> None:
        # Recheck on open so installing Claude does not require a daemon restart.
        self.claude_executable = detect_claude()
        if self.claude_executable:
            self.claude_keeper.codex.executable = self.claude_executable
            self._start_claude_polling()
        if not self.claude_executable:
            self.selected_provider = "codex"
        if self.window is None:
            self.window = WidgetWindow(self)
            self.window.connect("hide", self._on_widget_hidden)
        self.window.set_provider(self.selected_provider, self.claude_executable is not None)
        keeper = self.claude_keeper if self.selected_provider == "claude" else self
        state = keeper.state_store.load()
        self.window.set_window_keeper_state(
            state.keep_five_hour_window_active,
            keeper._window_keeper_message,
        )
        self.window.show_loading(state.last_global_reset)
        self.window.present_widget()
        self._start_usage_refresh()

    def _start_usage_refresh(self) -> None:
        if self._usage_refresh_source is None:
            self._usage_refresh_source = GLib.timeout_add_seconds(
                _USAGE_REFRESH_SECONDS,
                self._usage_refresh_tick,
            )
        self._refresh_visible_usage()

    def _usage_refresh_tick(self) -> bool:
        if self.window is None or not self.window.get_visible():
            self._usage_refresh_source = None
            return GLib.SOURCE_REMOVE
        self._refresh_visible_usage()
        return GLib.SOURCE_CONTINUE

    def _refresh_visible_usage(self) -> None:
        if getattr(self, "selected_provider", "codex") == "claude":
            if self.window is not None and self.window.get_visible():
                self._refresh_claude_usage()
            return
        if (
            self._usage_refreshing
            or self.window is None
            or not self.window.get_visible()
        ):
            return
        self._usage_refreshing = True
        self._run_async(self._read_and_store_usage, self._usage_refresh_finished)

    def _on_widget_hidden(self, _window: WidgetWindow) -> None:
        if self._usage_refresh_source is not None:
            GLib.source_remove(self._usage_refresh_source)
            self._usage_refresh_source = None

    def _usage_refresh_finished(
        self, usage: UsageSnapshot | None, error: BaseException | None
    ) -> None:
        self._usage_refreshing = False
        if (
            self.window is None
            or not self.window.get_visible()
            or self.selected_provider != "codex"
        ):
            return
        state = self.state_store.load()
        if error is not None or usage is None:
            message = str(error) if error is not None else "Codex account is unavailable"
            self.window.show_error(message, state.last_global_reset)
            return
        self.window.show_usage(usage, state.last_global_reset)
        if self._window_keeper_enabled() and not self._window_keeper_busy:
            self._schedule_window_keeper_from_usage(usage)

    def _reset_account_check_finished(
        self,
        event: ResetEvent,
        previous: UsageSnapshot | None,
        usage: UsageSnapshot | None,
        error: BaseException | None,
    ) -> None:
        if error is not None or usage is None:
            self._send_reset_notification(
                event,
                "⚠️ Global reset confirmed. Your Codex account could not be checked.",
                final=True,
            )
            return
        if account_reset_observed(previous, usage):
            body = "✅ Global reset confirmed. Your account has reset too."
        else:
            body = "⏳ Tibo announced a reset. Your account is still updating."
        self._send_reset_notification(event, body, final=True)
        if self.window is not None and self.window.get_visible() and self.selected_provider == "codex":
            self.window.show_usage(usage, event)

    def toggle_provider(self) -> None:
        if not detect_claude():
            return
        self._show_provider("claude" if self.selected_provider == "codex" else "codex")

    def _show_provider(self, provider: str) -> None:
        self.selected_provider = (
            "claude" if provider == "claude" and detect_claude() else "codex"
        )
        with self._state_lock:
            state = self.state_store.load()
            state.selected_provider = self.selected_provider
            self.state_store.save(state)
        self.show_widget()

    def set_window_keeper_enabled(self, enabled: bool) -> None:
        if getattr(self, "selected_provider", "codex") == "claude":
            self.claude_keeper.set_window_keeper_enabled(enabled)
        else:
            WindowKeeperMixin.set_window_keeper_enabled(self, enabled)

    def _claude_poll_tick(self) -> bool:
        if detect_claude():
            self._refresh_claude_usage()
        return GLib.SOURCE_CONTINUE

    def _refresh_claude_usage(self) -> None:
        if self._claude_refreshing:
            return
        self._claude_refreshing = True
        self._run_async(self.claude_keeper._read_and_store_usage, self._claude_usage_finished)

    def _claude_usage_finished(self, usage: UsageSnapshot | None, error: BaseException | None) -> None:
        self._claude_refreshing = False
        keeper = self.claude_keeper
        if usage is not None and keeper._window_keeper_enabled() and not keeper._window_keeper_busy:
            keeper._schedule_window_keeper_from_usage(usage)
        if self.window is None or not self.window.get_visible() or self.selected_provider != "claude":
            return
        state = keeper.state_store.load()
        if error is not None or usage is None:
            self.window.show_error(str(error) if error else "Claude account is unavailable", state.last_global_reset)
        else:
            self.window.show_usage(usage, state.last_global_reset)

    def claude_reset_observed(self, event: ResetEvent) -> bool:
        notification = Gio.Notification.new("Claude account reset")
        notification.set_body(event.summary)
        notification.set_default_action("app.show-claude")
        self.send_notification(event.event_id, notification)
        return GLib.SOURCE_REMOVE

    def show_reset_demo(self) -> None:
        event = ResetEvent(
            event_id="demo",
            announced_at=utc_now(),
            summary="Demonstration reset event",
            url="",
        )
        self._send_reset_notification(
            event,
            "DEMO • 🙏 Tibo announced a global reset. Checking your account…",
            final=False,
        )
        GLib.timeout_add_seconds(4, self._finish_reset_demo, event)

    def _finish_reset_demo(self, event: ResetEvent) -> bool:
        self._send_reset_notification(
            event,
            "DEMO • ✅ Global reset confirmed. Your account has reset too.",
            final=True,
        )
        return GLib.SOURCE_REMOVE

    def _send_reset_notification(
        self, event: ResetEvent, body: str, *, final: bool
    ) -> None:
        title = "🔥 Codex reset" if final else "🔥 Codex reset announced"
        notification_id = self._notification_ids.get(event.event_id)
        arguments = [
            "notify-send",
            "--app-name=Codex Widget",
            "--urgency=critical",
            "--transient",
            "--print-id",
        ]
        if notification_id is not None:
            arguments.append(f"--replace-id={notification_id}")
        arguments.extend((title, body))
        try:
            completed = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                text=True,
                timeout=3,
            )
            returned_id = completed.stdout.strip()
            if returned_id.isdigit():
                self._notification_ids[event.event_id] = int(returned_id)
            if final:
                self._notification_ids.pop(event.event_id, None)
            return
        except (OSError, subprocess.SubprocessError):
            pass

        notification = Gio.Notification.new(title)
        notification.set_body(body)
        notification.set_priority(Gio.NotificationPriority.URGENT)
        notification.set_default_action("app.show-codex")
        self.send_notification(f"reset-{event.event_id}", notification)

    @staticmethod
    def _run_async(
        work: Callable[[], Any],
        done: Callable[[Any | None, BaseException | None], None],
    ) -> None:
        def runner() -> None:
            try:
                value = work()
            except (CodexClientError, TrackerError, OSError, ValueError) as exc:
                GLib.idle_add(done, None, exc)
            except BaseException as exc:
                GLib.idle_add(done, None, exc)
            else:
                GLib.idle_add(done, value, None)

        threading.Thread(target=runner, daemon=True).start()
