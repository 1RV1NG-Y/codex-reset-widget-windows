from __future__ import annotations

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
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
