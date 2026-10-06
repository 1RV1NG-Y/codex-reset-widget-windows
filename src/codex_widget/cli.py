from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence


def _prefer_x11_backend() -> None:
    if os.environ.get("DISPLAY"):
        os.environ.setdefault("GDK_BACKEND", "x11")


def _start_installed_service(arguments: Sequence[str]) -> None:
    # The service must claim the application bus name before the short-lived
    # launcher registers. Type=dbus makes `start` wait for that readiness.
    if (
        os.environ.get("CODEX_WIDGET_NO_SERVICE") == "1"
        or any(argument != "--demo-reset" for argument in arguments)
    ):
        return
    try:
        state = subprocess.run(
            ["systemctl", "--user", "show", "codex-widget.service", "--property=UnitFileState", "--value"],
            capture_output=True, text=True, timeout=3,
        )
        if state.returncode or state.stdout.strip() not in {"enabled", "enabled-runtime"}:
            return
        started = subprocess.run(
            ["systemctl", "--user", "start", "codex-widget.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if started.returncode:
            print("codex-widget: background service unavailable; launching directly", file=sys.stderr)
    except FileNotFoundError:
        return  # Running without systemd is supported.
    except (OSError, subprocess.TimeoutExpired):
        print("codex-widget: background service unavailable; launching directly", file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    if sys.platform == "win32":
        from .windows_app import CodexWidgetApplication

        arguments = [sys.argv[0], *(argv if argv is not None else sys.argv[1:])]
        return CodexWidgetApplication().run(arguments)
    _prefer_x11_backend()
    options = list(argv if argv is not None else sys.argv[1:])
    _start_installed_service(options)
    try:
        from .app import CodexWidgetApplication
    except (ImportError, ValueError) as exc:
        print(
            "codex-widget requires GTK 3 and PyGObject "
            f"(for example, package 'python-gobject'): {exc}",
            file=sys.stderr,
        )
        return 1

    arguments = [sys.argv[0], *options]
    return CodexWidgetApplication().run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
