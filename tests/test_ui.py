from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gi.repository import Gdk, GLib

from codex_widget.ui import WidgetWindow


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


class FakeWindow:
    _dismiss = WidgetWindow._dismiss
    _hide_if_inactive = WidgetWindow._hide_if_inactive

    def __init__(self, *, pinned: bool = False, active: bool = False):
        self.pin_button = FakeButton(pinned)
        self.active = active
        self.hidden = False
        self.move = None

    def begin_move_drag(self, button, x_root, y_root, timestamp):
        self.move = (button, x_root, y_root, timestamp)

    def hide(self):
        self.hidden = True

    def is_active(self):
        return self.active


class WidgetInteractionTests(unittest.TestCase):
    def test_primary_button_drags_the_window(self):
        window = FakeWindow()
        event = SimpleNamespace(button=1, x_root=120.8, y_root=240.2, time=55)

        handled = WidgetWindow._on_drag_start(window, None, event)

        self.assertTrue(handled)
        self.assertEqual(window.move, (1, 120, 240, 55))

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

    def test_escape_unpins_and_dismisses(self):
        window = FakeWindow(pinned=True)
        event = SimpleNamespace(keyval=Gdk.KEY_Escape)

        handled = WidgetWindow._on_key_press(window, None, event)

        self.assertTrue(handled)
        self.assertFalse(window.pin_button.get_active())
        self.assertTrue(window.hidden)

    def test_pin_button_explains_current_state(self):
        button = FakeButton(active=True)
        WidgetWindow._on_pin_toggled(None, button)
        self.assertEqual(button.label, "PINNED")
        self.assertIn("Unpin", button.tooltip)

        button.active = False
        WidgetWindow._on_pin_toggled(None, button)
        self.assertEqual(button.label, "PIN")
        self.assertIn("visible", button.tooltip)


if __name__ == "__main__":
    unittest.main()
