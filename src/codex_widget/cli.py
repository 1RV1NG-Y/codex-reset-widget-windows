from __future__ import annotations

import os
import sys
from collections.abc import Sequence


def _prefer_x11_backend() -> None:
    if os.environ.get("DISPLAY"):
        os.environ.setdefault("GDK_BACKEND", "x11")


def main(argv: Sequence[str] | None = None) -> int:
    _prefer_x11_backend()
    try:
        from .app import CodexWidgetApplication
    except (ImportError, ValueError) as exc:
        print(
            "codex-widget requires GTK 3 and PyGObject "
            f"(for example, package 'python-gobject'): {exc}",
            file=sys.stderr,
        )
        return 1

    arguments = [sys.argv[0], *(argv if argv is not None else sys.argv[1:])]
    return CodexWidgetApplication().run(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
