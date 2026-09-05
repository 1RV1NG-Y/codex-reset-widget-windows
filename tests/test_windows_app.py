from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from codex_widget.models import AppState, ResetEvent, UsageSnapshot
from codex_widget.state import StateStore
from codex_widget.windows_app import (
    CodexWidgetApplication,
    _CommandEndpoint,
    _CommandServer,
    _parse_arguments,
)


class MemoryStateStore:
    def __init__(self) -> None:
        self.state = AppState()

    def load(self) -> AppState:
        return self.state

    def save(self, state: AppState) -> None:
        self.state = state


class TimerApplication:
    _schedule_window_keeper_from_usage = (
        CodexWidgetApplication._schedule_window_keeper_from_usage
    )
    _schedule_window_keeper_retry = (
        CodexWidgetApplication._schedule_window_keeper_retry
    )
    _schedule_window_keeper_at = CodexWidgetApplication._schedule_window_keeper_at
    _cancel_window_keeper_timer = CodexWidgetApplication._cancel_window_keeper_timer
    _window_keeper_history_suffix = CodexWidgetApplication._window_keeper_history_suffix

    def __init__(self) -> None:
        self.state_store = MemoryStateStore()
        self._state_lock = threading.Lock()
        self._window_keeper_source = None
        self.messages: list[str] = []
        self.scheduled: list[tuple[int | float, object, tuple[object, ...]]] = []
        self.window = None

    def _window_keeper_enabled(self) -> bool:
        return True

    def _after_seconds(self, seconds, callback, *args):
        self.scheduled.append((seconds, callback, args))
        return f"after-{len(self.scheduled)}"

    def _cancel_source(self, _source):
        return None

    def _set_window_keeper_message(self, message: str) -> None:
        self.messages.append(message)

    def _window_keeper_timer_fired(self) -> None:
        return None


class NotificationApplication:
    _send_reset_notification = CodexWidgetApplication._send_reset_notification

    def __init__(self) -> None:
        self._notification_ids: dict[str, int] = {}
        self._tray = SimpleNamespace(
            notify=lambda title, body, urgent=False: self.calls.append(
                (title, body, urgent)
            )
            or True
        )
        self.calls: list[tuple[str, str, bool]] = []


class WindowsAppUnitTests(unittest.TestCase):
    def test_argument_parsing_matches_linux_precedence(self) -> None:
        self.assertEqual(_parse_arguments(["codex-widget"]).command, "show")
        self.assertEqual(
            _parse_arguments(["codex-widget", "--daemon", "--demo-reset"]).command,
            "daemon",
        )
        self.assertEqual(_parse_arguments(["codex-widget", "--quit"]).command, "quit")
        self.assertEqual(_parse_arguments(["codex-widget", "--help"]).exit_code, 0)
        parsed = _parse_arguments(["codex-widget", "--bogus"])
        self.assertEqual(parsed.exit_code, 2)
        self.assertIn("--bogus", parsed.error or "")

    def test_command_endpoint_forwards_to_resident_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            endpoint = _CommandEndpoint(Path(directory) / "command.json")
            received: list[str] = []
            application = SimpleNamespace(
                handle_external_command=lambda command: received.append(command)
            )
            server = _CommandServer(application, endpoint)
            try:
                server.start()
                self.assertTrue(endpoint.send("show"))
                self.assertTrue(endpoint.send("check"))
            finally:
                server.stop()

        self.assertEqual(received, ["show", "check"])

    def test_window_keeper_schedule_uses_tk_after_adapter(self) -> None:
        now = datetime(2026, 8, 31, 10, tzinfo=UTC)
        application = TimerApplication()
        usage = UsageSnapshot(
            used_percent=25,
            reset_at=None,
            window_minutes=10080,
            banked_resets=0,
            five_hour_used_percent=1,
            five_hour_reset_at=now + timedelta(hours=1),
            five_hour_window_minutes=300,
        )

        with patch("codex_widget.windows_app.utc_now", return_value=now):
            application._schedule_window_keeper_from_usage(usage)

        self.assertEqual(application.scheduled[0][0], 3610)
        self.assertIn("next tiny request", application.messages[-1])

    def test_notification_keeps_two_stage_event_state(self) -> None:
        application = NotificationApplication()
        event = ResetEvent("tweet-1", datetime(2026, 8, 10, tzinfo=UTC), "Reset", "")

        application._send_reset_notification(event, "Checking", final=False)
        application._send_reset_notification(event, "Done", final=True)

        self.assertEqual(application.calls[0][0], "🔥 Codex reset announced")
        self.assertEqual(application.calls[1][0], "🔥 Codex reset")
        self.assertEqual(application._notification_ids, {})


@unittest.skipUnless(
    sys.platform == "win32" and os.environ.get("CODEX_WIDGET_WINDOWS_SMOKE") == "1",
    "Windows Tk/tray smoke test is opt-in",
)
class WindowsNativeSmokeTests(unittest.TestCase):
    def test_tk_tray_widget_and_second_instance_ipc(self) -> None:
        script = r"""
import os
import sys
import struct
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.environ["CODEX_WIDGET_SRC"])
os.environ["LOCALAPPDATA"] = os.environ["CODEX_WIDGET_LOCALAPPDATA"]
os.environ["CODEX_WIDGET_POLL_SECONDS"] = "30"

from codex_widget.models import ResetEvent, UsageSnapshot
import codex_widget.windows_app as windows_app
from codex_widget.windows_app import CodexWidgetApplication

events = []

class FakeWatcher:
    def __init__(self):
        self.count = 0
    def poll_once(self):
        from codex_widget.watcher import PollOutcome
        self.count += 1
        event = ResetEvent("seed", datetime.now(UTC), "Reset", "")
        return PollOutcome.SEEDED, event, type("State", (), {"last_known_usage": None})()

class FakeCodex:
    def read_rate_limits(self):
        return UsageSnapshot(
            used_percent=12,
            reset_at=datetime.now(UTC) + timedelta(days=2),
            window_minutes=10080,
            banked_resets=1,
            five_hour_used_percent=3,
            five_hour_reset_at=datetime.now(UTC) + timedelta(hours=4),
            five_hour_window_minutes=300,
        )
    def activate_five_hour_window(self):
        raise AssertionError("smoke test must not consume a reset window")

def write_window_png(widget, path):
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    hwnd = widget._window.winfo_id()
    rect = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise OSError("GetWindowRect failed")
    width = rect.right - rect.left
    height = rect.bottom - rect.top
    window_dc = user32.GetWindowDC(hwnd)
    memory_dc = gdi32.CreateCompatibleDC(window_dc)
    bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
    old_bitmap = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 2):
            gdi32.BitBlt(memory_dc, 0, 0, width, height, window_dc, 0, 0, 0x00CC0020)
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        buffer = ctypes.create_string_buffer(width * height * 4)
        if not gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), 0):
            raise OSError("GetDIBits failed")
    finally:
        gdi32.SelectObject(memory_dc, old_bitmap)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, window_dc)

    raw = bytearray()
    pixels = buffer.raw
    for y in range(height):
        raw.append(0)
        offset = y * width * 4
        for x in range(width):
            b, g, r, _a = pixels[offset + x * 4 : offset + x * 4 + 4]
            raw.extend((r, g, b))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(png)

app = CodexWidgetApplication()
watcher = FakeWatcher()
app.watcher = watcher
app.codex = FakeCodex()
app._send_reset_notification = lambda *args, **kwargs: None
app._ensure_tk_root()
app._ensure_resident()
assert app._tray is not None and app._tray.available
app.show_widget()

def pin_on():
    app.window.pin_button._clicked()
    events.append("pin-on" if app.window.pin_button.get_active() else "pin-missing")

def pin_off():
    app.window.pin_button._clicked()
    events.append("pin-off" if not app.window.pin_button.get_active() else "pin-stuck")

def hide_widget():
    app.window.hide()
    events.append("hidden" if not app.window.get_visible() else "hide-failed")

def show_widget():
    app.show_widget()
    events.append("shown" if app.window.get_visible() else "show-failed")

def tray_check():
    app._poll_tick = lambda: events.append("tray-check")
    app._tray._wndproc(app._tray._hwnd, windows_app._WM_COMMAND, windows_app._ID_CHECK, 0)

def tray_select():
    app._tray._wndproc(app._tray._hwnd, windows_app._TRAY_CALLBACK, 0, windows_app._NIN_SELECT)
    events.append("tray-select")

def screenshot():
    target = os.environ.get("CODEX_WIDGET_SCREENSHOT")
    if target:
        write_window_png(app.window, target)
        events.append("screenshot")

def verify():
    required = {"pin-on", "pin-off", "hidden", "shown", "tray-check", "tray-select", "screenshot"}
    missing = sorted(required - set(events))
    if missing:
        raise AssertionError(f"missing smoke events: {missing}; got {events}")
    if watcher.count < 1:
        raise AssertionError("resident poll did not run")
    app.quit()

app._root.after(250, pin_on)
app._root.after(500, pin_off)
app._root.after(700, hide_widget)
app._root.after(900, show_widget)
app._root.after(1100, tray_check)
app._root.after(1300, tray_select)
app._root.after(1500, screenshot)
app._root.after(1800, verify)
app._root.mainloop()
print("ready")
"""
        with tempfile.TemporaryDirectory() as localappdata:
            screenshot = Path.cwd() / "artifacts" / "windows-widget-smoke.png"
            try:
                screenshot.unlink()
            except FileNotFoundError:
                pass
            environment = os.environ.copy()
            environment["CODEX_WIDGET_SRC"] = str(Path(__file__).resolve().parents[1] / "src")
            environment["CODEX_WIDGET_LOCALAPPDATA"] = localappdata
            environment["CODEX_WIDGET_SCREENSHOT"] = str(screenshot)
            first = subprocess.Popen(
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                deadline = time.monotonic() + 5
                endpoint = _CommandEndpoint(
                    Path(localappdata) / "CodexWidget" / "command-endpoint.json"
                )
                while time.monotonic() < deadline and endpoint.load() is None:
                    time.sleep(0.05)
                self.assertIsNotNone(endpoint.load())
                self.assertTrue(endpoint.send("show"))
                self.assertTrue(endpoint.send("check"))
                stdout, stderr = first.communicate(timeout=6)
            finally:
                if first.poll() is None:
                    first.terminate()
                    first.wait(timeout=3)
            self.assertIn("ready", stdout)
            self.assertEqual(stderr, "")
            self.assertTrue(screenshot.exists())
            self.assertGreater(screenshot.stat().st_size, 1000)


if __name__ == "__main__":
    unittest.main()
