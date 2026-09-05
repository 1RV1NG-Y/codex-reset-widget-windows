"""Console-free launcher, usable from the checkout or a per-user installation."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from codex_widget.cli import main

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        import os
        import traceback
        import ctypes

        log = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexWidget" / "error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(traceback.format_exc(), encoding="utf-8")
        ctypes.windll.user32.MessageBoxW(None, f"Codex Widget could not start. Details: {log}", "Codex Widget", 0x10)
