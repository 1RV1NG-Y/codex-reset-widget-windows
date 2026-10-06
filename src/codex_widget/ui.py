from __future__ import annotations

import time
from datetime import UTC, datetime

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402

try:
    gi.require_version("GdkX11", "3.0")
    from gi.repository import GdkX11
except (ImportError, ValueError):
    GdkX11 = None

from .models import ResetEvent, UsageSnapshot
from .claude_client import USAGE_PAGE

_CSS = b"""
window.codex-widget {
  background-color: transparent;
}
#card {
  background-color: #111318;
  border: 1px solid #2d313b;
  border-radius: 16px;
  color: #f4f5f7;
  padding: 20px;
  box-shadow: 0 12px 36px rgba(0, 0, 0, 0.45);
}
#brand { color: #f4f5f7; font-size: 15px; font-weight: 800; letter-spacing: 1px; }
#provider-switch { background: transparent; border: none; box-shadow: none; padding: 0; }
#provider-switch:hover #brand { color: #60a5fa; }
#provider-switch:disabled { opacity: 1; }
#reset-offers { background: transparent; border: none; box-shadow: none; padding: 0; color: #aeb4bf; font-size: 13px; }
#reset-offers:hover { color: #fb923c; }
#muted { color: #8c93a2; font-size: 12px; }
#label { color: #aeb4bf; font-size: 13px; }
#value { color: #f4f5f7; font-size: 13px; font-weight: 700; }
#status { color: #8c93a2; font-size: 12px; }
#pin {
  background: #20242d;
  border: 1px solid #3a404c;
  border-radius: 7px;
  color: #aeb4bf;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 7px;
}
#pin:hover { border-color: #60a5fa; color: #f4f5f7; }
#pin:checked { background: #2563eb; border-color: #60a5fa; color: #ffffff; }
#keeper {
  background: #20242d;
  border: 1px solid #3a404c;
  border-radius: 8px;
  color: #aeb4bf;
  font-size: 11px;
  font-weight: 700;
  padding: 7px 10px;
}
#keeper:hover { border-color: #60a5fa; color: #f4f5f7; }
#keeper:checked { background: #163a66; border-color: #60a5fa; color: #ffffff; }
#keeper-status { color: #8c93a2; font-size: 11px; }
progressbar trough { min-height: 8px; border-radius: 8px; background: #292d36; }
progressbar progress { min-height: 8px; border-radius: 8px; background: #60a5fa; border-color: #60a5fa; box-shadow: none; }
window.claude-widget progressbar progress { background: #fb923c; border-color: #fb923c; }
window.claude-widget #provider-switch:hover #brand { color: #fb923c; }
window.claude-widget #pin:hover { border-color: #fb923c; }
window.claude-widget #pin:checked { background: #c45f32; border-color: #fb923c; }
window.claude-widget #keeper:hover { border-color: #fb923c; }
window.claude-widget #keeper:checked { background: #5b301e; border-color: #fb923c; }
"""


def _relative_time(value: datetime | None, *, future: bool = False) -> str:
    if value is None:
        return "Unavailable"
    seconds = int((value.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    if not future:
        seconds = -seconds
    if seconds < 0:
        return "Now" if future else "Just now"
    minutes = seconds // 60
    if minutes < 1:
        return "<1m"
    hours, minutes = divmod(minutes, 60)
    if hours < 1:
        return f"{minutes}m"
    days, hours = divmod(hours, 24)
    if days < 1:
        return f"{hours}h {minutes}m"
    return f"{days}d {hours}h"


def _row(label: str) -> tuple[Gtk.Box, Gtk.Label]:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    left = Gtk.Label(label=label, xalign=0)
    left.set_name("label")
    value = Gtk.Label(label="—", xalign=1)
    value.set_name("value")
    box.pack_start(left, True, True, 0)
    box.pack_end(value, False, False, 0)
    return box, value


class WidgetWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title="Codex Widget")
        self.set_name("codex-widget")
        self.get_style_context().add_class("codex-widget")
        self.set_decorated(False)
        self.set_resizable(False)
        self.set_skip_taskbar_hint(True)
        self.set_skip_pager_hint(True)
        self.set_keep_above(False)
        self.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_default_size(340, -1)
        self.set_border_width(12)
        self._dragging = False
        self._hide_source: int | None = None
        self._focused_since_open = False
        self._dismiss_after = 0.0
        self._pointer_origin = (0.0, 0.0)

        self._keeper_syncing = False
        self.provider_name = "codex"
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual is not None and screen.is_composited():
            self.set_visual(visual)
            self.set_app_paintable(True)

        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=11)
        card.set_name("card")
        card.set_border_width(20)
        self.add(card)
        self._enable_drag_source(card)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        draggable_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.dot = Gtk.Label(label="●")
        self.dot.set_markup('<span foreground="#60a5fa">●</span>')
        self.brand = Gtk.Label(label="CODEX", xalign=0)
        self.brand.set_name("brand")
        self.provider_button = Gtk.Button()
        self.provider_button.set_name("provider-switch")
        self.provider_button.set_relief(Gtk.ReliefStyle.NONE)
        brand_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        brand_box.pack_start(self.dot, False, False, 0)
        brand_box.pack_start(self.brand, False, False, 0)
        self.provider_button.add(brand_box)
        self.provider_button.connect("clicked", self._on_provider_clicked)
        hint = Gtk.Label(label="DRAG ANYWHERE", xalign=1)
        hint.set_name("muted")
        self.pin_button = Gtk.ToggleButton(label="PIN")
        self.pin_button.set_name("pin")
        self.pin_button.set_relief(Gtk.ReliefStyle.NONE)
        self.pin_button.set_tooltip_text("Keep the widget visible when focus changes")
        self.pin_button.connect("toggled", self._on_pin_toggled)
        draggable_header.pack_end(hint, False, False, 2)
        header.pack_start(self.provider_button, False, False, 0)
        header.pack_start(self._draggable(draggable_header), True, True, 0)
        header.pack_end(self.pin_button, False, False, 0)

        card.pack_start(header, False, False, 0)

        five_hour_row, self.five_hour_value = _row("5-hour used")
        card.pack_start(self._draggable(five_hour_row), False, False, 3)
        self.five_hour_progress = Gtk.ProgressBar()
        self.five_hour_progress.set_fraction(0)
        card.pack_start(self._draggable(self.five_hour_progress), False, False, 0)
        five_hour_reset_row, self.five_hour_reset_value = _row(
            "5-hour resets in"
        )
        card.pack_start(
            self._draggable(five_hour_reset_row),
            False,
            False,
            3,
        )
        usage_row, self.usage_value = _row("Weekly used")
        card.pack_start(self._draggable(usage_row), False, False, 3)
        self.progress = Gtk.ProgressBar()
        self.progress.set_fraction(0)
        card.pack_start(self._draggable(self.progress), False, False, 0)

        reset_row, self.reset_value = _row("Weekly resets in")
        banked_row, self.banked_value = _row("Banked resets")
        global_row, self.global_value = _row("🙏 Last Tibo reset")
        self.global_label = global_row.get_children()[0]
        card.pack_start(self._draggable(reset_row), False, False, 3)
        self.banked_row = self._draggable(banked_row)
        card.pack_start(self.banked_row, False, False, 0)
        self.reset_offers_button = Gtk.Button(label="Limit resets · Open Claude Usage ↗")
        self.reset_offers_button.set_name("reset-offers")
        self.reset_offers_button.get_child().show()
        self.reset_offers_button.set_no_show_all(True)
        self.reset_offers_button.connect("clicked", self._on_reset_offers_clicked)
        self.reset_offers_button.set_tooltip_text(
            "Claude's usage API does not expose reset offers. View available resets and their expiry in Claude."
        )
        card.pack_start(self.reset_offers_button, False, False, 0)
        card.pack_start(self._draggable(global_row), False, False, 0)

        self.keeper_button = Gtk.ToggleButton(label="ENABLE 5H AUTO-ROLL")
        self.keeper_button.set_name("keeper")
        self.keeper_button.set_relief(Gtk.ReliefStyle.NONE)
        self.keeper_button.set_tooltip_text(
            "Uses one tiny Codex request after each five-hour reset"
        )
        self.keeper_button.connect("toggled", self._on_keeper_toggled)
        card.pack_start(self.keeper_button, False, False, 4)

        self.keeper_status = Gtk.Label(
            label="Automatic 5-hour rolling is off",
            xalign=0,
        )
        self.keeper_status.set_name("keeper-status")
        self.keeper_status.set_line_wrap(True)
        card.pack_start(self.keeper_status, False, False, 0)

        self.status = Gtk.Label(label="", xalign=0)
        self.status.set_name("status")
        self.status.set_line_wrap(True)
        card.pack_start(self._draggable(self.status), False, False, 4)

        self.connect("key-press-event", self._on_key_press)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("focus-in-event", self._on_focus_in)
        self.connect("delete-event", self._on_delete)

    def set_provider(self, name: str, can_switch: bool) -> None:
        self.provider_name = name
        claude = name == "claude"
        self.brand.set_text(name.upper())
        self.set_title("Claude Widget" if claude else "Codex Widget")
        color = "#fb923c" if claude else "#60a5fa"
        self.dot.set_markup(f'<span foreground="{color}">●</span>')
        context = self.get_style_context()
        if claude:
            context.add_class("claude-widget")
        else:
            context.remove_class("claude-widget")
        self.provider_button.set_sensitive(can_switch)
        self.provider_button.set_tooltip_text(
            f"Switch to {'Codex' if claude else 'Claude'}" if can_switch else "Codex"
        )
        self.banked_row.set_no_show_all(claude)
        self.banked_row.set_visible(not claude)
        self.reset_offers_button.set_visible(claude)
        self.global_label.set_text("Last account reset" if claude else "🙏 Last Tibo reset")
        self.global_value.set_tooltip_text(
            "When the widget last observed usage drop after a Claude reset" if claude else None
        )
        self.keeper_button.set_tooltip_text(
            f"Uses one tiny {name.title()} request after each five-hour reset"
        )

    def _on_provider_clicked(self, _button: Gtk.Button) -> None:
        self.get_application().toggle_provider()

    def _on_reset_offers_clicked(self, _button: Gtk.Button) -> None:
        Gtk.show_uri_on_window(self, USAGE_PAGE, Gdk.CURRENT_TIME)

    def show_loading(self, last_reset: ResetEvent | None) -> None:
        self.usage_value.set_text("Checking…")
        self.progress.set_fraction(0)
        self.five_hour_value.set_text("Checking…")
        self.five_hour_progress.set_fraction(0)
        self.five_hour_reset_value.set_text("—")
        self.reset_value.set_text("—")
        self.banked_value.set_text("—")
        self._set_last_reset(last_reset)
        self.status.set_text(f"Reading your {self.provider_name.title()} account")

    def present_widget(self) -> None:
        self._cancel_pending_hide()
        self._focused_since_open = self.is_active()
        # Let the launcher/overview finish its focus transition before dismissing.
        self._dismiss_after = time.monotonic() + 0.6
        self.show_all()
        timestamp = Gtk.get_current_event_time()
        window = self.get_window()
        if timestamp == Gdk.CURRENT_TIME and GdkX11 is not None and isinstance(window, GdkX11.X11Window):
            timestamp = GdkX11.x11_get_server_time(window)
        self.present_with_time(timestamp)

    def show_usage(
        self, usage: UsageSnapshot, last_reset: ResetEvent | None
    ) -> None:
        if usage.five_hour_used_percent is None:
            self.five_hour_value.set_text("Unavailable")
            self.five_hour_progress.set_fraction(0)
        else:
            self.five_hour_value.set_text(f"{usage.five_hour_used_percent:.0f}%")
            self.five_hour_progress.set_fraction(
                max(0.0, min(1.0, usage.five_hour_used_percent / 100))
            )
        self.five_hour_reset_value.set_text(
            _relative_time(usage.five_hour_reset_at, future=True)
        )
        if usage.used_percent is None:
            self.usage_value.set_text("Unavailable")
            self.progress.set_fraction(0)
        else:
            self.usage_value.set_text(f"{usage.used_percent:.0f}%")
            self.progress.set_fraction(max(0.0, min(1.0, usage.used_percent / 100)))
        self.reset_value.set_text(_relative_time(usage.reset_at, future=True))
        self.banked_value.set_text(
            str(usage.banked_resets) if usage.banked_resets is not None else "Unavailable"
        )
        self._set_last_reset(last_reset)
        self.status.set_text(
            f"Updated {_relative_time(usage.checked_at)} ago · auto-refreshes every 60s"
        )

    def show_error(self, message: str, last_reset: ResetEvent | None) -> None:
        self.usage_value.set_text("Unavailable")
        self.five_hour_value.set_text("Unavailable")
        self.five_hour_progress.set_fraction(0)
        self.five_hour_reset_value.set_text("—")
        self.progress.set_fraction(0)
        self.reset_value.set_text("—")
        self.banked_value.set_text("—")
        self._set_last_reset(last_reset)
        self.status.set_text(message)

    def set_window_keeper_state(self, enabled: bool, message: str) -> None:
        self._keeper_syncing = True
        self.keeper_button.set_active(enabled)
        self.keeper_button.set_label(
            "5H AUTO-ROLL: ON" if enabled else "ENABLE 5H AUTO-ROLL"
        )
        self.keeper_status.set_text(message)
        self._keeper_syncing = False

    def _on_keeper_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._keeper_syncing:
            return
        application = self.get_application()
        application.set_window_keeper_enabled(button.get_active())

    def _set_last_reset(self, event: ResetEvent | None) -> None:
        occurred_at = (
            event.effective_at or event.announced_at if event is not None else None
        )
        self.global_value.set_text(
            _relative_time(occurred_at) + " ago" if occurred_at is not None else "Unknown"
        )

    def _enable_drag_source(self, widget: Gtk.Widget) -> None:
        widget.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        widget.connect("button-press-event", self._on_drag_press)
        widget.connect("motion-notify-event", self._on_drag_motion)
        widget.connect("button-release-event", self._on_drag_release)

    def _draggable(self, child: Gtk.Widget) -> Gtk.EventBox:
        event_box = Gtk.EventBox()
        event_box.set_visible_window(False)
        event_box.set_above_child(True)
        self._enable_drag_source(event_box)
        event_box.add(child)
        return event_box

    def _on_drag_press(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        event_widget = Gtk.get_event_widget(event)
        buttons = (
            self.pin_button, self.keeper_button,
            getattr(self, "provider_button", None),
            getattr(self, "reset_offers_button", None),
        )
        if (
            event.button != 1
            or (
                event_widget is not None
                and any(
                    button is not None
                    and (event_widget is button or button.is_ancestor(event_widget))
                    for button in buttons
                )
            )
        ):
            return False
        self._drag_origin = self.get_position()
        self._pointer_origin = (event.x_root, event.y_root)
        self._dragging = True
        return True

    def _on_drag_motion(self, _widget: Gtk.Widget, event: Gdk.EventMotion) -> bool:
        if not self._dragging or not (
            event.state & Gdk.ModifierType.BUTTON1_MASK
        ):
            return False
        self.move(
            self._drag_origin[0] + int(event.x_root - self._pointer_origin[0]),
            self._drag_origin[1] + int(event.y_root - self._pointer_origin[1]),
        )
        return True

    def _on_drag_release(self, _widget: Gtk.Widget, event: Gdk.EventButton) -> bool:
        if event.button != 1 or not self._dragging:
            return False
        GLib.timeout_add(120, self._finish_drag)
        return True

    def _finish_drag(self) -> bool:
        self._dragging = False
        return GLib.SOURCE_REMOVE

    def _on_pin_toggled(self, button: Gtk.ToggleButton) -> None:
        pinned = button.get_active()
        self.set_keep_above(pinned)
        if pinned:
            self._cancel_pending_hide()
            button.set_label("PINNED")
            button.set_tooltip_text("Unpin to restore click-outside dismissal")
            self.present()
        else:
            button.set_label("PIN")
            button.set_tooltip_text("Keep the widget above other windows")

    def _dismiss(self) -> None:
        self._cancel_pending_hide()
        self.pin_button.set_active(False)
        self.hide()

    def _on_key_press(self, _window: Gtk.Window, event: Gdk.EventKey) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._dismiss()
            return True
        return False

    def _on_focus_out(self, _window: Gtk.Window, _event: Gdk.EventFocus) -> bool:
        if (
            self._focused_since_open and self.get_visible()
            and not self.pin_button.get_active() and not self._dragging
        ):
            self._cancel_pending_hide()
            delay = max(120, int((self._dismiss_after - time.monotonic()) * 1000) + 1)
            self._hide_source = GLib.timeout_add(delay, self._hide_if_inactive)
        return False

    def _cancel_pending_hide(self) -> None:
        if self._hide_source is not None:
            GLib.source_remove(self._hide_source)
            self._hide_source = None

    def _on_focus_in(self, _window: Gtk.Window, _event: Gdk.EventFocus) -> bool:
        self._focused_since_open = True
        self._cancel_pending_hide()
        return False

    def _hide_if_inactive(self) -> bool:
        self._hide_source = None
        if (
            not self.pin_button.get_active()
            and not self._dragging
            and not self.is_active()
        ):
            self.hide()
        return GLib.SOURCE_REMOVE

    def _on_delete(self, _window: Gtk.Window, _event: Gdk.Event) -> bool:
        self._dismiss()
        return True
