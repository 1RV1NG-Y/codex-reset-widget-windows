from __future__ import annotations

import json
import math
import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import queue
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
except ImportError:  # pragma: no cover - exercised only on broken Python installs.
    tk = None  # type: ignore[assignment]

try:
    import winsound
except ImportError:  # pragma: no cover - non-Windows import compatibility.
    winsound = None  # type: ignore[assignment]

from .codex_client import CodexClient, CodexClientError
from .models import ResetEvent, UsageSnapshot, utc_now
from .state import StateStore
from .tracker import TrackerClient, TrackerError
from .watcher import PollOutcome, ResetWatcher, account_reset_observed

_DEFAULT_POLL_SECONDS = 60
_USAGE_REFRESH_SECONDS = 60
_RESET_NOTIFICATION_MAX_AGE = timedelta(hours=6)
_WINDOW_KEEPER_GRACE_SECONDS = 10
_WINDOW_KEEPER_RETRY_SECONDS = 60
_WINDOW_KEEPER_PROPAGATION_SECONDS = 60
_WINDOW_KEEPER_WATCHDOG_SECONDS = 60
_USAGE_TEXT = (
    "Usage: codex-widget [--daemon | --demo-reset | --quit]\n"
    "Without options, show the widget and keep its reset watcher resident.\n"
    "--demo-reset shows the two-stage reset notification without changing state.\n"
)
_VALID_COMMANDS = {"show", "daemon", "demo-reset", "quit", "check"}


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


def _write_stream(name: str, text: str | None) -> None:
    if not text:
        return
    stream = getattr(sys, name, None)
    if stream is None:
        return
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError):
        pass


@dataclass(frozen=True)
class _ParsedArguments:
    command: str
    exit_code: int | None = None
    message: str | None = None
    error: str | None = None


def _parse_arguments(arguments: Sequence[str]) -> _ParsedArguments:
    flags = list(arguments)[1:]
    if "--help" in flags:
        return _ParsedArguments("help", exit_code=0, message=_USAGE_TEXT)
    unknown = [
        argument
        for argument in flags
        if argument not in {"--daemon", "--demo-reset", "--quit"}
    ]
    if unknown:
        return _ParsedArguments(
            "error",
            exit_code=2,
            error=f"Unknown option: {unknown[0]}\n",
        )
    if "--quit" in flags:
        return _ParsedArguments("quit")
    if "--daemon" in flags:
        return _ParsedArguments("daemon", message="codex-widget: watcher running\n")
    if "--demo-reset" in flags:
        return _ParsedArguments(
            "demo-reset",
            message="codex-widget: showing reset notification demo\n",
        )
    return _ParsedArguments("show")


def _user_data_dir() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "CodexWidget"
    return Path.home() / "AppData" / "Local" / "CodexWidget"


@dataclass(frozen=True)
class _CommandEndpoint:
    path: Path
    port: int | None = None
    token: str | None = None

    @classmethod
    def default(cls) -> _CommandEndpoint:
        return cls(_user_data_dir() / "command-endpoint.json")

    def load(self) -> _CommandEndpoint | None:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict):
            return None
        port = document.get("port")
        token = document.get("token")
        if (
            isinstance(port, int)
            and 0 < port < 65536
            and isinstance(token, str)
            and token
        ):
            return _CommandEndpoint(self.path, port, token)
        return None

    def publish(self, port: int, token: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{os.getpid()}.tmp")
        document = {"host": "127.0.0.1", "port": port, "token": token, "pid": os.getpid()}
        temporary.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
        os.replace(temporary, self.path)

    def clear(self, token: str) -> None:
        endpoint = self.load()
        if endpoint is not None and endpoint.token != token:
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def send(self, command: str, *, timeout: float = 1.0) -> bool:
        endpoint = self.load()
        if endpoint is None or endpoint.port is None or endpoint.token is None:
            return False
        try:
            with socket.create_connection(("127.0.0.1", endpoint.port), timeout) as client:
                client.settimeout(timeout)
                payload = json.dumps(
                    {"token": endpoint.token, "command": command},
                    separators=(",", ":"),
                ).encode("utf-8")
                client.sendall(payload + b"\n")
                return client.recv(64).startswith(b"ok")
        except OSError:
            self.clear(endpoint.token)
            return False


class _CommandRequestHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server  # type: ignore[assignment]
        data = b""
        while b"\n" not in data and len(data) < 4096:
            chunk = self.request.recv(4096)
            if not chunk:
                break
            data += chunk
        try:
            document = json.loads(data.decode("utf-8").strip())
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.request.sendall(b"error\n")
            return
        if not isinstance(document, dict):
            self.request.sendall(b"error\n")
            return
        if document.get("token") != server.token:
            self.request.sendall(b"error\n")
            return
        command = document.get("command")
        if command not in _VALID_COMMANDS:
            self.request.sendall(b"error\n")
            return
        server.application.handle_external_command(command)
        self.request.sendall(b"ok\n")


class _ThreadedCommandServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _CommandServer:
    def __init__(
        self,
        application: CodexWidgetApplication,
        endpoint: _CommandEndpoint | None = None,
    ) -> None:
        self.application = application
        self.endpoint = endpoint or _CommandEndpoint.default()
        self.token = secrets.token_hex(24)
        self._server: _ThreadedCommandServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return
        server = _ThreadedCommandServer(("127.0.0.1", 0), _CommandRequestHandler)
        server.application = self.application  # type: ignore[attr-defined]
        server.token = self.token  # type: ignore[attr-defined]
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self.endpoint.publish(server.server_address[1], self.token)

    def stop(self) -> None:
        server = self._server
        if server is None:
            return
        self._server = None
        self.endpoint.clear(self.token)
        server.shutdown()
        server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=1)
        self._thread = None


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _LRESULT = getattr(wintypes, "LRESULT", ctypes.c_ssize_t)
    _HICON = getattr(wintypes, "HICON", wintypes.HANDLE)
    _HCURSOR = getattr(wintypes, "HCURSOR", wintypes.HANDLE)
    _HBRUSH = getattr(wintypes, "HBRUSH", wintypes.HANDLE)
    _HINSTANCE = getattr(wintypes, "HINSTANCE", wintypes.HANDLE)
    _HMENU = getattr(wintypes, "HMENU", wintypes.HANDLE)
    _WNDPROC = ctypes.WINFUNCTYPE(
        _LRESULT,
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    )

    class _WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", _WNDPROC),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", _HINSTANCE),
            ("hIcon", _HICON),
            ("hCursor", _HCURSOR),
            ("hbrBackground", _HBRUSH),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", wintypes.BYTE * 8),
        ]

    class _NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", _HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", _GUID),
            ("hBalloonIcon", _HICON),
        ]

    _WM_DESTROY = 0x0002
    _WM_COMMAND = 0x0111
    _WM_CLOSE = 0x0010
    _WM_LBUTTONUP = 0x0202
    _WM_RBUTTONUP = 0x0205
    _WM_CONTEXTMENU = 0x007B
    _WM_USER = 0x0400
    _WM_APP = 0x8000
    _NIM_ADD = 0x00000000
    _NIM_MODIFY = 0x00000001
    _NIM_DELETE = 0x00000002
    _NIM_SETVERSION = 0x00000004
    _NIF_MESSAGE = 0x00000001
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004
    _NIF_INFO = 0x00000010
    _NOTIFYICON_VERSION_4 = 4
    _NIIF_INFO = 0x00000001
    _NIIF_WARNING = 0x00000002
    _MF_STRING = 0x00000000
    _MF_SEPARATOR = 0x00000800
    _TPM_RIGHTBUTTON = 0x0002
    _IDI_APPLICATION = 32512
    _NIN_SELECT = _WM_USER
    _NIN_KEYSELECT = _WM_USER + 1

    _TRAY_CALLBACK = _WM_APP + 1
    _TRAY_SHUTDOWN = _WM_APP + 2
    _ID_OPEN = 1001
    _ID_CHECK = 1002
    _ID_QUIT = 1003
    _ERROR_ALREADY_EXISTS = 183

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    _kernel32.GetModuleHandleW.restype = _HINSTANCE
    _kernel32.GetCurrentThreadId.argtypes = []
    _kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    _kernel32.CreateMutexW.restype = wintypes.HANDLE
    _kernel32.ReleaseMutex.argtypes = [wintypes.HANDLE]
    _kernel32.ReleaseMutex.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _user32.RegisterClassW.argtypes = [ctypes.POINTER(_WNDCLASSW)]
    _user32.RegisterClassW.restype = wintypes.ATOM
    _user32.CreateWindowExW.argtypes = [
        wintypes.DWORD,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        _HMENU,
        _HINSTANCE,
        wintypes.LPVOID,
    ]
    _user32.CreateWindowExW.restype = wintypes.HWND
    _user32.DefWindowProcW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.DefWindowProcW.restype = _LRESULT
    _user32.DestroyWindow.argtypes = [wintypes.HWND]
    _user32.DestroyWindow.restype = wintypes.BOOL
    _user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    _user32.PostMessageW.restype = wintypes.BOOL
    _user32.PostQuitMessage.argtypes = [ctypes.c_int]
    _user32.PostQuitMessage.restype = None
    _user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG),
        wintypes.HWND,
        wintypes.UINT,
        wintypes.UINT,
    ]
    _user32.GetMessageW.restype = ctypes.c_int
    _user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.TranslateMessage.restype = wintypes.BOOL
    _user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    _user32.DispatchMessageW.restype = _LRESULT
    _user32.LoadIconW.argtypes = [_HINSTANCE, wintypes.LPCWSTR]
    _user32.LoadIconW.restype = _HICON
    _user32.CreatePopupMenu.argtypes = []
    _user32.CreatePopupMenu.restype = _HMENU
    _user32.AppendMenuW.argtypes = [_HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
    _user32.AppendMenuW.restype = wintypes.BOOL
    _user32.DestroyMenu.argtypes = [_HMENU]
    _user32.DestroyMenu.restype = wintypes.BOOL
    _user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _user32.GetCursorPos.restype = wintypes.BOOL
    _user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _user32.SetForegroundWindow.restype = wintypes.BOOL
    _user32.TrackPopupMenu.argtypes = [
        _HMENU,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.HWND,
        wintypes.LPVOID,
    ]
    _user32.TrackPopupMenu.restype = wintypes.BOOL
    _user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
    _user32.RegisterWindowMessageW.restype = wintypes.UINT

    _shell32.Shell_NotifyIconW.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(_NOTIFYICONDATAW),
    ]
    _shell32.Shell_NotifyIconW.restype = wintypes.BOOL

    def _make_int_resource(value: int) -> wintypes.LPCWSTR:
        return ctypes.cast(ctypes.c_void_p(value), wintypes.LPCWSTR)

    def _limited(text: str, limit: int) -> str:
        return text if len(text) < limit else text[: limit - 1]

    class WindowsTrayIcon:
        def __init__(self, application: CodexWidgetApplication) -> None:
            self.application = application
            self._hwnd: int | None = None
            self._menu: int | None = None
            self._icon: int | None = None
            self._icon_added = False
            self._ready = threading.Event()
            self._lock = threading.Lock()
            self._thread_id = 0
            self._class_name = f"CodexWidgetTrayWindow-{os.getpid()}-{id(self)}"
            self._taskbar_created = 0
            self._wndproc_ref = _WNDPROC(self._wndproc)
            self._thread = threading.Thread(target=self._message_loop, daemon=True)
            self._thread.start()
            self._ready.wait(timeout=2)

        @property
        def available(self) -> bool:
            return self._icon_added

        def notify(self, title: str, body: str, *, urgent: bool = False) -> bool:
            if not self._ready.wait(timeout=1):
                return False
            with self._lock:
                if self._hwnd is None or not self._icon_added:
                    return False
                data = self._notify_icon_data(_NIF_INFO)
                data.szInfoTitle = _limited(title, 64)
                data.szInfo = _limited(body, 256)
                data.dwInfoFlags = _NIIF_WARNING if urgent else _NIIF_INFO
                return bool(_shell32.Shell_NotifyIconW(_NIM_MODIFY, ctypes.byref(data)))

        def stop(self) -> None:
            hwnd = self._hwnd
            if hwnd is not None:
                _user32.PostMessageW(hwnd, _TRAY_SHUTDOWN, 0, 0)
                self._thread.join(timeout=2)

        def _message_loop(self) -> None:
            self._thread_id = int(_kernel32.GetCurrentThreadId())
            instance = _kernel32.GetModuleHandleW(None)
            window_class = _WNDCLASSW()
            window_class.lpfnWndProc = self._wndproc_ref
            window_class.hInstance = instance
            window_class.lpszClassName = self._class_name
            if not _user32.RegisterClassW(ctypes.byref(window_class)):
                self._ready.set()
                return
            hwnd = _user32.CreateWindowExW(
                0,
                self._class_name,
                "Codex Widget",
                0,
                0,
                0,
                0,
                0,
                None,
                None,
                instance,
                None,
            )
            if not hwnd:
                self._ready.set()
                return
            self._hwnd = int(hwnd)
            self._taskbar_created = int(_user32.RegisterWindowMessageW("TaskbarCreated"))
            self._icon = int(_user32.LoadIconW(None, _make_int_resource(_IDI_APPLICATION)) or 0)
            self._create_menu()
            self._add_icon()
            self._ready.set()

            message = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(message))
                _user32.DispatchMessageW(ctypes.byref(message))

        def _wndproc(
            self,
            hwnd: wintypes.HWND,
            message: wintypes.UINT,
            wparam: wintypes.WPARAM,
            lparam: wintypes.LPARAM,
        ) -> int:
            if message == _TRAY_CALLBACK:
                event = int(lparam) & 0xFFFF
                if event in {_WM_LBUTTONUP, _NIN_SELECT, _NIN_KEYSELECT}:
                    self.application.call_soon(self.application.show_widget)
                elif event in {_WM_RBUTTONUP, _WM_CONTEXTMENU}:
                    self._show_menu()
                return 0
            if message == _WM_COMMAND:
                command_id = int(wparam) & 0xFFFF
                if command_id == _ID_OPEN:
                    self.application.call_soon(self.application.show_widget)
                elif command_id == _ID_CHECK:
                    self.application.call_soon(self.application._poll_tick)
                elif command_id == _ID_QUIT:
                    self.application.call_soon(self.application.quit)
                return 0
            if self._taskbar_created and message == self._taskbar_created:
                self._add_icon()
                return 0
            if message in {_TRAY_SHUTDOWN, _WM_CLOSE}:
                _user32.DestroyWindow(hwnd)
                return 0
            if message == _WM_DESTROY:
                self._delete_icon()
                if self._menu is not None:
                    _user32.DestroyMenu(self._menu)
                    self._menu = None
                _user32.PostQuitMessage(0)
                return 0
            return int(_user32.DefWindowProcW(hwnd, message, wparam, lparam))

        def _create_menu(self) -> None:
            menu = _user32.CreatePopupMenu()
            if not menu:
                return
            _user32.AppendMenuW(menu, _MF_STRING, _ID_OPEN, "Open Codex Widget")
            _user32.AppendMenuW(menu, _MF_STRING, _ID_CHECK, "Check for resets now")
            _user32.AppendMenuW(menu, _MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(menu, _MF_STRING, _ID_QUIT, "Quit Codex Widget")
            self._menu = int(menu)

        def _show_menu(self) -> None:
            if self._menu is None or self._hwnd is None:
                return
            point = wintypes.POINT()
            if not _user32.GetCursorPos(ctypes.byref(point)):
                return
            _user32.SetForegroundWindow(self._hwnd)
            _user32.TrackPopupMenu(
                self._menu,
                _TPM_RIGHTBUTTON,
                point.x,
                point.y,
                0,
                self._hwnd,
                None,
            )

        def _notify_icon_data(self, flags: int) -> _NOTIFYICONDATAW:
            data = _NOTIFYICONDATAW()
            data.cbSize = ctypes.sizeof(_NOTIFYICONDATAW)
            data.hWnd = self._hwnd or 0
            data.uID = 1
            data.uFlags = flags
            data.uCallbackMessage = _TRAY_CALLBACK
            data.hIcon = self._icon or 0
            if flags & _NIF_TIP:
                data.szTip = "Codex usage and reset status"
            return data

        def _add_icon(self) -> None:
            with self._lock:
                if self._hwnd is None:
                    return
                data = self._notify_icon_data(_NIF_MESSAGE | _NIF_ICON | _NIF_TIP)
                self._icon_added = bool(
                    _shell32.Shell_NotifyIconW(_NIM_ADD, ctypes.byref(data))
                )
                if self._icon_added:
                    data.uVersion = _NOTIFYICON_VERSION_4
                    _shell32.Shell_NotifyIconW(_NIM_SETVERSION, ctypes.byref(data))

        def _delete_icon(self) -> None:
            with self._lock:
                if self._hwnd is not None and self._icon_added:
                    data = self._notify_icon_data(0)
                    _shell32.Shell_NotifyIconW(_NIM_DELETE, ctypes.byref(data))
                self._icon_added = False

else:

    class WindowsTrayIcon:
        def __init__(self, application: CodexWidgetApplication) -> None:
            self.application = application

        @property
        def available(self) -> bool:
            return False

        def notify(self, title: str, body: str, *, urgent: bool = False) -> bool:
            return False

        def stop(self) -> None:
            return


class _SingleInstanceLock:
    _name = "Local\\CodexResetWidget"

    def __init__(self, handle: int | None, acquired: bool) -> None:
        self._handle = handle
        self.acquired = acquired

    @classmethod
    def acquire(cls) -> _SingleInstanceLock:
        if os.name != "nt":
            return cls(None, True)
        handle = _kernel32.CreateMutexW(None, True, cls._name)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateMutexW failed")
        acquired = ctypes.get_last_error() != _ERROR_ALREADY_EXISTS
        return cls(int(handle), acquired)

    def release(self) -> None:
        if os.name == "nt" and self._handle is not None:
            if self.acquired:
                _kernel32.ReleaseMutex(self._handle)
            _kernel32.CloseHandle(self._handle)
        self._handle = None
        self.acquired = False


class _TextHandle:
    def __init__(
        self,
        parent: Any,
        text: str = "",
        *,
        foreground: str = "#f4f5f7",
        font: tuple[str, int, str] | tuple[str, int] = ("Segoe UI", 9),
        anchor: str = "w",
        wraplength: int = 0,
    ) -> None:
        self.variable = tk.StringVar(value=text)
        self.widget = tk.Label(
            parent,
            textvariable=self.variable,
            bg="#111318",
            fg=foreground,
            font=font,
            anchor=anchor,
            justify="left",
            wraplength=wraplength,
        )

    def set_text(self, text: str) -> None:
        self.variable.set(text)


class _ProgressHandle:
    def __init__(self, parent: Any) -> None:
        self._fraction = 0.0
        self.widget = tk.Canvas(
            parent,
            width=298,
            height=8,
            bg="#292d36",
            highlightthickness=0,
            bd=0,
        )
        self._fill = self.widget.create_rectangle(0, 0, 0, 8, fill="#60a5fa", outline="")
        self.widget.bind("<Configure>", self._redraw)

    def set_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        self._redraw()

    def _redraw(self, _event: Any | None = None) -> None:
        width = max(1, self.widget.winfo_width())
        height = max(1, self.widget.winfo_height())
        self.widget.coords(self._fill, 0, 0, width * self._fraction, height)


class _Tooltip:
    def __init__(self, widget: Any, text: str) -> None:
        self.widget = widget
        self.text = text
        self._after_id: str | None = None
        self._tip: Any | None = None
        widget.bind("<Enter>", self._schedule)
        widget.bind("<Leave>", self._hide)
        widget.bind("<ButtonPress>", self._hide)

    def set_text(self, text: str) -> None:
        self.text = text
        if self._tip is not None:
            self._hide()

    def _schedule(self, _event: Any | None = None) -> None:
        self._hide()
        self._after_id = self.widget.after(450, self._show)

    def _show(self) -> None:
        self._after_id = None
        if not self.text:
            return
        x = self.widget.winfo_rootx() + 16
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        tip = tk.Toplevel(self.widget)
        tip.overrideredirect(True)
        tip.configure(bg="#2d313b")
        tip.geometry(f"+{x}+{y}")
        label = tk.Label(
            tip,
            text=self.text,
            bg="#20242d",
            fg="#f4f5f7",
            padx=8,
            pady=5,
            font=("Segoe UI", 8),
        )
        label.pack()
        self._tip = tip

    def _hide(self, _event: Any | None = None) -> None:
        if self._after_id is not None:
            self.widget.after_cancel(self._after_id)
            self._after_id = None
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None


class _ToggleButton:
    def __init__(
        self,
        parent: Any,
        label: str,
        command: Callable[[_ToggleButton], None],
        *,
        normal_bg: str = "#20242d",
        active_bg: str = "#2563eb",
        padx: int = 8,
        pady: int = 3,
    ) -> None:
        self._active = False
        self._normal_bg = normal_bg
        self._active_bg = active_bg
        self._command = command
        self.widget = tk.Button(
            parent,
            text=label,
            command=self._clicked,
            bg=normal_bg,
            fg="#aeb4bf",
            activebackground="#2b63b8",
            activeforeground="#ffffff",
            bd=0,
            highlightthickness=1,
            highlightbackground="#3a404c",
            highlightcolor="#60a5fa",
            padx=padx,
            pady=pady,
            relief="flat",
            cursor="hand2",
            font=("Segoe UI", 8, "bold"),
        )
        self._tooltip = _Tooltip(self.widget, "")

    def get_active(self) -> bool:
        return self._active

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self.widget.configure(
            bg=self._active_bg if self._active else self._normal_bg,
            fg="#ffffff" if self._active else "#aeb4bf",
        )

    def set_label(self, label: str) -> None:
        self.widget.configure(text=label)

    def set_tooltip_text(self, tooltip: str) -> None:
        self._tooltip.set_text(tooltip)

    def _clicked(self) -> None:
        self.set_active(not self._active)
        self._command(self)


def _row(parent: Any, label: str) -> tuple[Any, _TextHandle]:
    row = tk.Frame(parent, bg="#111318")
    left = tk.Label(
        row,
        text=label,
        bg="#111318",
        fg="#aeb4bf",
        font=("Segoe UI", 9),
        anchor="w",
    )
    value = _TextHandle(
        row,
        "—",
        foreground="#f4f5f7",
        font=("Segoe UI", 9, "bold"),
        anchor="e",
    )
    left.pack(side="left", fill="x", expand=True)
    value.widget.pack(side="right")
    return row, value


class WidgetWindow:
    def __init__(self, application: CodexWidgetApplication, root: Any) -> None:
        self.application = application
        self._root = root
        self._window = tk.Toplevel(root)
        self._window.title("Codex Widget")
        self._window.withdraw()
        self._window.overrideredirect(True)
        self._window.configure(bg="#111318", highlightthickness=1, highlightbackground="#2d313b")
        self._window.resizable(False, False)
        self._window.protocol("WM_DELETE_WINDOW", self._dismiss)
        self._window.bind("<Escape>", self._on_key_press)
        self._window.bind("<FocusOut>", self._on_focus_out)
        try:
            self._window.attributes("-toolwindow", True)
        except tk.TclError:
            pass

        self._dragging = False
        self._drag_origin = (0, 0)
        self._pointer_origin = (0, 0)
        self._keeper_syncing = False

        card = tk.Frame(self._window, bg="#111318", padx=20, pady=18)
        card.pack(fill="both", expand=True)

        header = tk.Frame(card, bg="#111318")
        header.pack(fill="x", pady=(0, 10))
        header_text = tk.Frame(header, bg="#111318")
        header_text.pack(side="left", fill="x", expand=True)
        dot = tk.Label(
            header_text,
            text="●",
            bg="#111318",
            fg="#60a5fa",
            font=("Segoe UI", 10, "bold"),
        )
        dot.pack(side="left", padx=(0, 7))
        brand = tk.Label(
            header_text,
            text="CODEX",
            bg="#111318",
            fg="#f4f5f7",
            font=("Segoe UI", 10, "bold"),
        )
        brand.pack(side="left")
        hint = tk.Label(
            header_text,
            text="DRAG ANYWHERE",
            bg="#111318",
            fg="#8c93a2",
            font=("Segoe UI", 8),
        )
        hint.pack(side="right", padx=(0, 8))

        self.pin_button = _ToggleButton(header, "PIN", self._on_pin_toggled)
        self.pin_button.set_tooltip_text("Keep the widget visible when focus changes")
        self.pin_button.widget.pack(side="right")

        five_hour_row, self.five_hour_value = _row(card, "5-hour used")
        five_hour_row.pack(fill="x", pady=(0, 5))
        self.five_hour_progress = _ProgressHandle(card)
        self.five_hour_progress.widget.pack(fill="x", pady=(0, 9))
        five_hour_reset_row, self.five_hour_reset_value = _row(card, "5-hour resets in")
        five_hour_reset_row.pack(fill="x", pady=(0, 7))

        usage_row, self.usage_value = _row(card, "Weekly used")
        usage_row.pack(fill="x", pady=(0, 5))
        self.progress = _ProgressHandle(card)
        self.progress.widget.pack(fill="x", pady=(0, 9))

        reset_row, self.reset_value = _row(card, "Weekly resets in")
        banked_row, self.banked_value = _row(card, "Banked resets")
        global_row, self.global_value = _row(card, "🙏 Last Tibo reset")
        reset_row.pack(fill="x", pady=(0, 6))
        banked_row.pack(fill="x", pady=(0, 6))
        global_row.pack(fill="x", pady=(0, 12))

        self.keeper_button = _ToggleButton(
            card,
            "ENABLE 5H AUTO-ROLL",
            self._on_keeper_toggled,
            active_bg="#163a66",
            padx=10,
            pady=7,
        )
        self.keeper_button.set_tooltip_text(
            "Uses one tiny Codex request after each five-hour reset"
        )
        self.keeper_button.widget.pack(fill="x", pady=(0, 7))

        self.keeper_status = _TextHandle(
            card,
            "Automatic 5-hour rolling is off",
            foreground="#8c93a2",
            font=("Segoe UI", 8),
            wraplength=298,
        )
        self.keeper_status.widget.pack(fill="x", pady=(0, 8))

        self.status = _TextHandle(
            card,
            "",
            foreground="#8c93a2",
            font=("Segoe UI", 8),
            wraplength=298,
        )
        self.status.widget.pack(fill="x")

        self._drag_exclusions = {self.pin_button.widget, self.keeper_button.widget}
        self._bind_drag(card)

    def show_loading(self, last_reset: ResetEvent | None) -> None:
        self.usage_value.set_text("Checking…")
        self.five_hour_value.set_text("Checking…")
        self.five_hour_progress.set_fraction(0)
        self.five_hour_reset_value.set_text("—")
        self.reset_value.set_text("—")
        self.banked_value.set_text("—")
        self._set_last_reset(last_reset)
        self.status.set_text("Reading your Codex account")
        self.show_all()
        self.present()

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
        was_visible = self.get_visible()
        self.show_all()
        if not was_visible:
            self.present()

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
        was_visible = self.get_visible()
        self.show_all()
        if not was_visible:
            self.present()

    def set_window_keeper_state(self, enabled: bool, message: str) -> None:
        self._keeper_syncing = True
        self.keeper_button.set_active(enabled)
        self.keeper_button.set_label(
            "5H AUTO-ROLL: ON" if enabled else "ENABLE 5H AUTO-ROLL"
        )
        self.keeper_status.set_text(message)
        self._keeper_syncing = False

    def get_visible(self) -> bool:
        return self._window.state() != "withdrawn"

    def show_all(self) -> None:
        self._window.deiconify()
        self._window.update_idletasks()
        width = max(340, self._window.winfo_reqwidth())
        height = max(1, self._window.winfo_reqheight())
        if not getattr(self, "_positioned", False):
            screen_width = self._window.winfo_screenwidth()
            screen_height = self._window.winfo_screenheight()
            x = max(0, (screen_width - width) // 2)
            y = max(0, (screen_height - height) // 3)
            self._positioned = True
        else:
            x = self._window.winfo_x()
            y = self._window.winfo_y()
        self._window.geometry(f"{width}x{height}+{x}+{y}")

    def present(self) -> None:
        self._window.deiconify()
        self._window.lift()
        try:
            self._window.focus_force()
        except tk.TclError:
            pass

    def hide(self) -> None:
        self._window.withdraw()
        self.application._on_widget_hidden(self)

    def destroy(self) -> None:
        self._window.destroy()

    def _on_keeper_toggled(self, button: _ToggleButton) -> None:
        if self._keeper_syncing:
            return
        self.application.set_window_keeper_enabled(button.get_active())

    def _set_last_reset(self, event: ResetEvent | None) -> None:
        occurred_at = (
            event.effective_at or event.announced_at if event is not None else None
        )
        self.global_value.set_text(
            _relative_time(occurred_at) + " ago" if occurred_at is not None else "Unknown"
        )

    def _bind_drag(self, widget: Any) -> None:
        if widget in self._drag_exclusions:
            return
        widget.bind("<ButtonPress-1>", self._on_drag_press, add="+")
        widget.bind("<B1-Motion>", self._on_drag_motion, add="+")
        widget.bind("<ButtonRelease-1>", self._on_drag_release, add="+")
        for child in widget.winfo_children():
            self._bind_drag(child)

    def _drag_excluded(self, widget: Any) -> bool:
        current = widget
        while current is not None:
            if current in self._drag_exclusions:
                return True
            current = getattr(current, "master", None)
        return False

    def _on_drag_press(self, event: Any) -> str | None:
        if getattr(event, "num", 1) != 1 or self._drag_excluded(event.widget):
            return None
        self._drag_origin = (self._window.winfo_x(), self._window.winfo_y())
        self._pointer_origin = (event.x_root, event.y_root)
        self._dragging = True
        return "break"

    def _on_drag_motion(self, event: Any) -> str | None:
        if not self._dragging:
            return None
        x = self._drag_origin[0] + int(event.x_root - self._pointer_origin[0])
        y = self._drag_origin[1] + int(event.y_root - self._pointer_origin[1])
        self._window.geometry(f"+{x}+{y}")
        return "break"

    def _on_drag_release(self, event: Any) -> str | None:
        if getattr(event, "num", 1) != 1 or not self._dragging:
            return None
        self._window.after(120, self._finish_drag)
        return "break"

    def _finish_drag(self) -> None:
        self._dragging = False

    def _on_pin_toggled(self, button: _ToggleButton) -> None:
        pinned = button.get_active()
        self._window.attributes("-topmost", pinned)
        if pinned:
            button.set_label("PINNED")
            button.set_tooltip_text("Unpin to restore click-outside dismissal")
            self.present()
        else:
            button.set_label("PIN")
            button.set_tooltip_text("Keep the widget above other windows")

    def _dismiss(self) -> None:
        self.pin_button.set_active(False)
        self._on_pin_toggled(self.pin_button)
        self.hide()

    def _on_key_press(self, _event: Any) -> str:
        self._dismiss()
        return "break"

    def _on_focus_out(self, _event: Any) -> None:
        if not self.pin_button.get_active() and not self._dragging:
            self._window.after(120, self._hide_if_inactive)

    def _hide_if_inactive(self) -> None:
        if self.pin_button.get_active() or self._dragging:
            return
        focus = self._window.focus_get()
        if focus is None or not self._is_descendant(focus):
            self.hide()

    def _is_descendant(self, widget: Any) -> bool:
        current = widget
        while current is not None:
            if current is self._window:
                return True
            current = getattr(current, "master", None)
        return False


class CodexWidgetApplication:
    def __init__(self) -> None:
        self.window: WidgetWindow | None = None
        self.state_store = StateStore()
        self.codex = CodexClient()
        self.watcher = ResetWatcher(TrackerClient(), self.state_store)
        self._resident = False
        self._polling = False
        self._state_lock = threading.Lock()
        self._account_query_lock = threading.Lock()
        self._notification_ids: dict[str, int] = {}
        self._usage_refresh_source: str | None = None
        self._usage_refreshing = False
        self._window_keeper_source: str | None = None
        self._window_keeper_watchdog_source: str | None = None
        self._window_keeper_busy = False
        self._window_keeper_message = "Automatic 5-hour rolling is off"
        self._poll_source: str | None = None
        self._root: Any | None = None
        self._tray: WindowsTrayIcon | None = None
        self._command_server: _CommandServer | None = None
        self._instance_lock: _SingleInstanceLock | None = None
        self._dispatch_queue: queue.Queue[
            tuple[Callable[..., Any], tuple[Any, ...]]
        ] = queue.Queue()
        self._dispatch_source: str | None = None
        self._main_thread_id = threading.get_ident()
        self._quitting = False

    def run(self, arguments: Sequence[str]) -> int:
        parsed = _parse_arguments(arguments)
        if parsed.exit_code is not None:
            _write_stream("stderr" if parsed.error else "stdout", parsed.error or parsed.message)
            return parsed.exit_code
        endpoint = _CommandEndpoint.default()
        if parsed.command == "quit":
            endpoint.send("quit")
            return 0
        if endpoint.send(parsed.command):
            _write_stream("stdout", parsed.message)
            return 0

        lock = _SingleInstanceLock.acquire()
        if not lock.acquired:
            try:
                if self._forward_when_ready(endpoint, parsed.command):
                    _write_stream("stdout", parsed.message)
                    return 0
                _write_stream(
                    "stderr",
                    "codex-widget: another instance is starting but did not accept commands\n",
                )
                return 1
            finally:
                lock.release()
        self._instance_lock = lock
        try:
            self._ensure_tk_root()
            self._ensure_resident()
            if parsed.command == "daemon":
                _write_stream("stdout", parsed.message)
            elif parsed.command == "demo-reset":
                self.show_reset_demo()
                _write_stream("stdout", parsed.message)
            else:
                self.show_widget()
            self._root.mainloop()
            return 0
        finally:
            if not self._quitting:
                self.quit()

    def _forward_when_ready(
        self,
        endpoint: _CommandEndpoint,
        command: str,
        *,
        timeout: float = 3.0,
    ) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if endpoint.send(command):
                return True
            time.sleep(0.05)
        return False

    def handle_external_command(self, command: str) -> None:
        if command == "show":
            self.call_soon(self.show_widget)
        elif command == "check":
            self.call_soon(self._poll_tick)
        elif command == "demo-reset":
            self.call_soon(self.show_reset_demo)
        elif command == "daemon":
            self.call_soon(self._ensure_resident)
        elif command == "quit":
            self.call_soon(self.quit)

    def call_soon(self, callback: Callable[..., Any], *args: Any) -> None:
        if self._root is None:
            if threading.get_ident() == self._main_thread_id:
                callback(*args)
            return
        if self._quitting:
            return
        self._dispatch_queue.put((callback, args))

    def _schedule_dispatch_queue(self) -> None:
        if self._root is None or self._quitting or self._dispatch_source is not None:
            return
        try:
            self._dispatch_source = self._root.after(50, self._drain_dispatch_queue)
        except (RuntimeError, tk.TclError):
            self._dispatch_source = None

    def _drain_dispatch_queue(self) -> None:
        self._dispatch_source = None
        while not self._dispatch_queue.empty() and not self._quitting:
            callback, args = self._dispatch_queue.get_nowait()
            try:
                callback(*args)
            except BaseException as exc:
                _write_stream("stderr", f"codex-widget: callback failed: {exc}\n")
        self._schedule_dispatch_queue()

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        for source in (
            self._poll_source,
            self._usage_refresh_source,
            self._window_keeper_source,
            self._window_keeper_watchdog_source,
            self._dispatch_source,
        ):
            self._cancel_source(source)
        self._poll_source = None
        self._usage_refresh_source = None
        self._window_keeper_source = None
        self._window_keeper_watchdog_source = None
        self._dispatch_source = None
        if self._command_server is not None:
            self._command_server.stop()
            self._command_server = None
        if self._tray is not None:
            self._tray.stop()
            self._tray = None
        if self.window is not None:
            try:
                self.window.destroy()
            except tk.TclError:
                pass
            self.window = None
        if self._root is not None:
            try:
                self._root.quit()
                self._root.destroy()
            except tk.TclError:
                pass
            self._root = None
        if self._instance_lock is not None:
            self._instance_lock.release()
            self._instance_lock = None

    def show_widget(self) -> None:
        self._ensure_tk_root()
        if self.window is None:
            self.window = WidgetWindow(self, self._root)
        state = self.state_store.load()
        self.window.set_window_keeper_state(
            state.keep_five_hour_window_active,
            self._window_keeper_message,
        )
        self.window.show_loading(state.last_global_reset)
        self._start_usage_refresh()

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
        self._after_seconds(4, self._finish_reset_demo, event)

    def _finish_reset_demo(self, event: ResetEvent) -> None:
        self._send_reset_notification(
            event,
            "DEMO • ✅ Global reset confirmed. Your account has reset too.",
            final=True,
        )

    def _ensure_tk_root(self) -> None:
        if self._root is not None:
            return
        if tk is None:
            raise RuntimeError("codex-widget for Windows requires Python with Tcl/Tk")
        root = tk.Tk()
        root.withdraw()
        root.title("Codex Widget")
        root.protocol("WM_DELETE_WINDOW", self.quit)
        self._root = root
        self._schedule_dispatch_queue()

    def _ensure_resident(self) -> None:
        self._ensure_tk_root()
        if self._resident:
            return
        if self._instance_lock is None:
            lock = _SingleInstanceLock.acquire()
            if not lock.acquired:
                lock.release()
                raise RuntimeError("another Codex Widget instance is already running")
            self._instance_lock = lock
        self._resident = True
        self._command_server = _CommandServer(self)
        self._command_server.start()
        self._tray = WindowsTrayIcon(self)
        self._schedule_poll_loop()
        self._poll_tick()
        if self.state_store.load().keep_five_hour_window_active:
            self._start_window_keeper_watchdog()
            self._set_window_keeper_message("Checking the 5-hour window…")
            self._refresh_window_keeper_schedule()

    @staticmethod
    def _poll_interval() -> int:
        raw = os.environ.get("CODEX_WIDGET_POLL_SECONDS")
        try:
            return max(30, int(raw)) if raw is not None else _DEFAULT_POLL_SECONDS
        except ValueError:
            return _DEFAULT_POLL_SECONDS

    def _schedule_poll_loop(self) -> None:
        if self._poll_source is None and not self._quitting:
            self._poll_source = self._after_seconds(
                self._poll_interval(),
                self._poll_timer_fired,
            )

    def _poll_timer_fired(self) -> None:
        self._poll_source = None
        if self._quitting:
            return
        self._poll_tick()
        self._schedule_poll_loop()

    def _poll_tick(self) -> None:
        if not self._polling:
            self._polling = True
            self._run_async(self._poll_once, self._poll_finished)

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
            if self.window is not None and self.window.get_visible():
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

    def _start_usage_refresh(self) -> None:
        if self._usage_refresh_source is None:
            self._usage_refresh_source = self._after_seconds(
                _USAGE_REFRESH_SECONDS,
                self._usage_refresh_tick,
            )
        self._refresh_visible_usage()

    def _usage_refresh_tick(self) -> None:
        self._usage_refresh_source = None
        if self.window is None or not self.window.get_visible():
            return
        self._refresh_visible_usage()
        self._usage_refresh_source = self._after_seconds(
            _USAGE_REFRESH_SECONDS,
            self._usage_refresh_tick,
        )

    def _refresh_visible_usage(self) -> None:
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
            self._cancel_source(self._usage_refresh_source)
            self._usage_refresh_source = None

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
        if usage.five_hour_reset_at is not None and usage.five_hour_reset_at > now:
            target = usage.five_hour_reset_at + timedelta(
                seconds=_WINDOW_KEEPER_GRACE_SECONDS
            )
            self._schedule_window_keeper_at(
                target,
                "5-hour auto-roll on · "
                f"next tiny request {target.astimezone():%a %H:%M}"
                f"{self._window_keeper_history_suffix()}",
            )
            return
        self._schedule_window_keeper_at(
            now + timedelta(seconds=1),
            "5-hour auto-roll on · activating an idle window"
            f"{self._window_keeper_history_suffix()}",
        )

    def _schedule_window_keeper_at(
        self, target: datetime, message: str
    ) -> None:
        self._cancel_window_keeper_timer()
        seconds = max(1, math.ceil((target - utc_now()).total_seconds()))
        self._window_keeper_source = self._after_seconds(
            seconds,
            self._window_keeper_timer_fired,
        )
        self._set_window_keeper_message(message)

    def _schedule_window_keeper_retry(self, seconds: int, message: str) -> None:
        self._cancel_window_keeper_timer()
        self._window_keeper_source = self._after_seconds(
            seconds,
            self._window_keeper_retry_fired,
        )
        self._set_window_keeper_message(message)

    def _cancel_window_keeper_timer(self) -> None:
        if self._window_keeper_source is not None:
            self._cancel_source(self._window_keeper_source)
            self._window_keeper_source = None

    def _start_window_keeper_watchdog(self) -> None:
        if self._window_keeper_watchdog_source is None:
            self._window_keeper_watchdog_source = self._after_seconds(
                _WINDOW_KEEPER_WATCHDOG_SECONDS,
                self._window_keeper_watchdog_tick,
            )

    def _cancel_window_keeper_watchdog(self) -> None:
        if self._window_keeper_watchdog_source is not None:
            self._cancel_source(self._window_keeper_watchdog_source)
            self._window_keeper_watchdog_source = None

    def _window_keeper_watchdog_tick(self) -> None:
        self._window_keeper_watchdog_source = None
        if not self._window_keeper_enabled():
            return
        if self._window_keeper_busy:
            self._start_window_keeper_watchdog()
            return
        with self._state_lock:
            usage = self.state_store.load().last_known_usage
        now = utc_now()
        if (
            usage is not None
            and usage.used_percent is not None
            and usage.used_percent >= 100
            and usage.reset_at is not None
            and usage.reset_at > now
        ):
            self._start_window_keeper_watchdog()
            return
        if (
            usage is None
            or usage.five_hour_reset_at is None
            or usage.five_hour_reset_at
            + timedelta(seconds=_WINDOW_KEEPER_GRACE_SECONDS)
            <= now
        ):
            self._refresh_window_keeper_schedule()
        self._start_window_keeper_watchdog()

    def _window_keeper_timer_fired(self) -> None:
        self._window_keeper_source = None
        if not self._window_keeper_enabled() or self._window_keeper_busy:
            return
        self._window_keeper_busy = True
        self._set_window_keeper_message("Sending the tiny 5-hour activation request…")
        self._run_async(
            self._activate_and_read_usage,
            self._window_keeper_activation_finished,
        )

    def _window_keeper_retry_fired(self) -> None:
        self._window_keeper_source = None
        self._refresh_window_keeper_schedule()

    def _activate_and_read_usage(self) -> UsageSnapshot:
        with self._account_query_lock:
            attempted_at = utc_now()
            self._record_window_keeper_outcome(attempted_at=attempted_at)
            try:
                self.codex.activate_five_hour_window()
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
            return self._read_and_store_usage_unlocked()

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
            state.last_window_keeper_error = error[:500] if error else None
            self.state_store.save(state)

    def _window_keeper_history_suffix(self) -> str:
        with self._state_lock:
            state = self.state_store.load()
        if state.last_window_keeper_error:
            return " · last attempt failed"
        if state.last_window_keeper_success_at is not None:
            return (
                " · last sent "
                f"{state.last_window_keeper_success_at.astimezone():%H:%M}"
            )
        return ""

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
            self._schedule_window_keeper_retry(
                _WINDOW_KEEPER_RETRY_SECONDS,
                f"5-hour activation failed; retrying in 1m: {detail}",
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
        if self.window is not None and self.window.get_visible():
            state = self.state_store.load()
            self.window.show_usage(usage, state.last_global_reset)

    def _set_window_keeper_message(self, message: str) -> None:
        self._window_keeper_message = message
        if self.window is not None:
            self.window.set_window_keeper_state(
                self._window_keeper_enabled(),
                message,
            )

    def _usage_refresh_finished(
        self, usage: UsageSnapshot | None, error: BaseException | None
    ) -> None:
        self._usage_refreshing = False
        if self.window is None or not self.window.get_visible():
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
        if self.window is not None and self.window.get_visible():
            self.window.show_usage(usage, event)

    def _send_reset_notification(
        self, event: ResetEvent, body: str, *, final: bool
    ) -> None:
        title = "🔥 Codex reset" if final else "🔥 Codex reset announced"
        if self._tray is not None and self._tray.notify(title, body, urgent=True):
            if final:
                self._notification_ids.pop(event.event_id, None)
            else:
                self._notification_ids[event.event_id] = 1
            return
        if winsound is not None:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except RuntimeError:
                pass
        if final:
            self._notification_ids.pop(event.event_id, None)
        else:
            self._notification_ids[event.event_id] = 1

    def _after_seconds(
        self,
        seconds: int | float,
        callback: Callable[..., Any],
        *args: Any,
    ) -> str:
        self._ensure_tk_root()
        milliseconds = max(1, int(math.ceil(seconds * 1000)))
        return self._root.after(milliseconds, callback, *args)

    def _cancel_source(self, source: str | None) -> None:
        if source is None or self._root is None:
            return
        try:
            self._root.after_cancel(source)
        except tk.TclError:
            pass

    def _run_async(
        self,
        work: Callable[[], Any],
        done: Callable[[Any | None, BaseException | None], None],
    ) -> None:
        def runner() -> None:
            try:
                value = work()
            except (CodexClientError, TrackerError, OSError, ValueError) as exc:
                self.call_soon(done, None, exc)
            except BaseException as exc:
                self.call_soon(done, None, exc)
            else:
                self.call_soon(done, value, None)

        threading.Thread(target=runner, daemon=True).start()
