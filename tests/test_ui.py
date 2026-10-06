from __future__ import annotations

import unittest
import sys
from types import SimpleNamespace
from unittest.mock import Mock, patch

if sys.platform == "win32":
    raise unittest.SkipTest("GTK UI tests run on Linux")

import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib

from codex_widget.ui import WidgetWindow
from codex_widget.models import ResetEvent
from codex_widget.models import UsageSnapshot


class FakeButton:
    def __init__(self, active: bool = False):
        self.active = active
        self.label = "PIN"
        self.tooltip = ""

    def get_active(self):
        return self.active

    def set_active(self, active):
        self.active = active

    def set_label(self, label):
        self.label = label

    def set_tooltip_text(self, tooltip):
        self.tooltip = tooltip

    def is_ancestor(self, _widget):
        return False



class FakeWindow:
    _dismiss = WidgetWindow._dismiss
    _hide_if_inactive = WidgetWindow._hide_if_inactive
    _finish_drag = WidgetWindow._finish_drag
    _cancel_pending_hide = WidgetWindow._cancel_pending_hide

    def __init__(self, *, pinned: bool = False, active: bool = False):
        self.pin_button = FakeButton(pinned)
        self.keeper_button = FakeButton()
        self.provider_button = FakeButton()
        self.reset_offers_button = FakeButton()
        self.active = active
        self.hidden = False
        self.moved_to = None
        self.position = (50, 60)
        self._drag_origin = (0, 0)
        self._pointer_origin = (0.0, 0.0)
        self._dragging = False
        self.keep_above = False
        self.presented = False
        self._hide_source = None
        self._focused_since_open = True
        self._dismiss_after = 0.0

    def get_visible(self):
        return not self.hidden

    def get_position(self):
        return self.position

    def move(self, x, y):
        self.moved_to = (x, y)

    def hide(self):
        self.hidden = True

    def is_active(self):
        return self.active

    def set_keep_above(self, keep_above):
        self.keep_above = keep_above

    def present(self):
        self.presented = True


class WidgetInteractionTests(unittest.TestCase):
    def test_event_box_moves_from_child_content(self):
        window = FakeWindow()
        press = SimpleNamespace(button=1, x_root=100.0, y_root=200.0)
        motion = SimpleNamespace(
            state=Gdk.ModifierType.BUTTON1_MASK,
            x_root=130.8,
            y_root=240.2,
        )

        with patch("codex_widget.ui.Gtk.get_event_widget", return_value=None):
            pressed = WidgetWindow._on_drag_press(window, None, press)
            moved = WidgetWindow._on_drag_motion(window, None, motion)

        self.assertTrue(pressed)
        self.assertTrue(moved)
        self.assertEqual(window.moved_to, (80, 100))
        self.assertTrue(window._dragging)

    def test_last_reset_prefers_observed_effective_time(self):
        announced_at = object()
        effective_at = object()
        event = ResetEvent(
            "pending",
            announced_at,
            "Reset planned for Monday",
            "https://example.test/pending",
            effective_at=effective_at,
        )
        label = Mock()
        window = SimpleNamespace(global_value=label)

        with patch("codex_widget.ui._relative_time", return_value="3m") as relative:
            WidgetWindow._set_last_reset(window, event)

        relative.assert_called_once_with(effective_at)
        label.set_text.assert_called_once_with("3m ago")

    def test_pin_button_is_not_a_drag_target(self):
        window = FakeWindow()
        event = SimpleNamespace(button=1, x_root=100, y_root=200)

        with patch(
            "codex_widget.ui.Gtk.get_event_widget",
            return_value=window.pin_button,
        ):
            handled = WidgetWindow._on_drag_press(window, None, event)

        self.assertFalse(handled)
        self.assertIsNone(window.moved_to)

    def test_keeper_button_is_not_a_drag_target(self):
        window = FakeWindow()
        event = SimpleNamespace(button=1, x_root=100, y_root=200)

        with patch(
            "codex_widget.ui.Gtk.get_event_widget",
            return_value=window.keeper_button,
        ):
            handled = WidgetWindow._on_drag_press(window, None, event)

        self.assertFalse(handled)
        self.assertFalse(window._dragging)

    def test_provider_and_reset_offer_buttons_do_not_start_a_drag(self):
        window = FakeWindow()
        event = SimpleNamespace(button=1, x_root=100, y_root=200)
        for button in (window.provider_button, window.reset_offers_button):
            with patch("codex_widget.ui.Gtk.get_event_widget", return_value=button):
                self.assertFalse(WidgetWindow._on_drag_press(window, None, event))
        self.assertFalse(window._dragging)

    def test_brand_click_switches_provider(self):
        application = Mock()
        window = SimpleNamespace(get_application=lambda: application)
        WidgetWindow._on_provider_clicked(window, None)
        application.toggle_provider.assert_called_once_with()

    def test_keeper_toggle_updates_application_setting(self):
        application = Mock()
        button = FakeButton(active=True)
        window = SimpleNamespace(
            _keeper_syncing=False,
            get_application=lambda: application,
        )

        WidgetWindow._on_keeper_toggled(window, button)

        application.set_window_keeper_enabled.assert_called_once_with(True)

    def test_keeper_state_updates_control_and_status(self):
        button = FakeButton()
        status = Mock()
        window = SimpleNamespace(
            _keeper_syncing=False,
            keeper_button=button,
            keeper_status=status,
        )

        WidgetWindow.set_window_keeper_state(window, True, "Scheduled")

        self.assertTrue(button.active)
        self.assertEqual(button.label, "5H AUTO-ROLL: ON")
        status.set_text.assert_called_once_with("Scheduled")
        self.assertFalse(window._keeper_syncing)

    def test_drag_suppresses_focus_loss_until_release(self):
        window = FakeWindow()
        window._dragging = True

        with patch("codex_widget.ui.GLib.timeout_add") as timeout_add:
            WidgetWindow._on_focus_out(window, None, None)
            handled = WidgetWindow._on_drag_release(
                window, None, SimpleNamespace(button=1)
            )

        self.assertTrue(handled)
        timeout_add.assert_called_once_with(120, window._finish_drag)
        self.assertEqual(window._finish_drag(), GLib.SOURCE_REMOVE)
        self.assertFalse(window._dragging)

    def test_pin_prevents_focus_loss_dismissal(self):
        window = FakeWindow(pinned=True)

        with patch("codex_widget.ui.GLib.timeout_add") as timeout_add:
            WidgetWindow._on_focus_out(window, None, None)

        timeout_add.assert_not_called()
        self.assertFalse(window.hidden)

    def test_unpinned_inactive_window_hides(self):
        window = FakeWindow(pinned=False, active=False)

        result = WidgetWindow._hide_if_inactive(window)

        self.assertTrue(window.hidden)
        self.assertEqual(result, GLib.SOURCE_REMOVE)

    def test_focus_loss_before_window_receives_focus_does_not_dismiss(self):
        window = FakeWindow()
        window._focused_since_open = False
        with patch("codex_widget.ui.GLib.timeout_add") as timeout:
            WidgetWindow._on_focus_out(window, None, None)
        timeout.assert_not_called()
        self.assertFalse(window.hidden)

    def test_focus_return_cancels_old_dismissal(self):
        window = FakeWindow()
        window._hide_source = 42
        with patch("codex_widget.ui.GLib.source_remove") as remove:
            WidgetWindow._on_focus_in(window, None, None)
        remove.assert_called_once_with(42)
        self.assertIsNone(window._hide_source)
        self.assertFalse(window.hidden)

    def test_reopening_cancels_dismissal_and_restarts_focus_grace(self):
        window = FakeWindow()
        window._hide_source = 42
        window.show_all = Mock()
        window.get_window = Mock(return_value=None)
        window.present_with_time = Mock()
        with (
            patch("codex_widget.ui.GLib.source_remove") as remove,
            patch("codex_widget.ui.Gtk.get_current_event_time", return_value=1234),
            patch("codex_widget.ui.time.monotonic", return_value=100),
        ):
            WidgetWindow.present_widget(window)
        remove.assert_called_once_with(42)
        self.assertIsNone(window._hide_source)
        self.assertEqual(window._dismiss_after, 100.6)
        window.present_with_time.assert_called_once_with(1234)

    def test_launcher_focus_transition_gets_time_to_settle(self):
        window = FakeWindow()
        window._dismiss_after = 100.6
        with patch("codex_widget.ui.GLib.timeout_add") as timeout, patch("codex_widget.ui.time.monotonic", return_value=100):
            WidgetWindow._on_focus_out(window, None, None)
        delay, callback = timeout.call_args.args
        self.assertGreaterEqual(delay, 600)
        self.assertIs(callback.__func__, window._hide_if_inactive.__func__)

    def test_usage_updates_do_not_reopen_or_raise_window(self):
        fields = (
            "usage_value", "progress", "five_hour_value", "five_hour_progress", "five_hour_reset_value",
            "reset_value", "banked_value", "status",
        )
        window = SimpleNamespace(**{name: Mock() for name in fields},
                                 provider_name="codex", _set_last_reset=Mock(), show_all=Mock(), present=Mock())
        WidgetWindow.show_loading(window, None)
        WidgetWindow.show_usage(window, UsageSnapshot(42, None, 10080, None), None)
        WidgetWindow.show_error(window, "temporary failure", None)
        window.show_all.assert_not_called()
        window.present.assert_not_called()

    def test_escape_unpins_and_dismisses(self):
        window = FakeWindow(pinned=True)
        event = SimpleNamespace(keyval=Gdk.KEY_Escape)

        handled = WidgetWindow._on_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertFalse(window.pin_button.get_active())
        self.assertTrue(window.hidden)

    def test_pin_button_controls_above_window_state(self):
        window = FakeWindow(pinned=True)
        button = window.pin_button
        WidgetWindow._on_pin_toggled(window, button)
        self.assertEqual(button.label, "PINNED")
        self.assertIn("Unpin", button.tooltip)
        self.assertTrue(window.keep_above)
        self.assertTrue(window.presented)

        button.active = False
        WidgetWindow._on_pin_toggled(window, button)
        self.assertEqual(button.label, "PIN")
        self.assertIn("above", button.tooltip)
        self.assertFalse(window.keep_above)


if __name__ == "__main__":
    unittest.main()
