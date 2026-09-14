"""Console-free launcher, usable from the checkout or a per-user installation."""
from pathlib import Path
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

def run() -> int:
    installation = Path(__file__).resolve().parent
    if (installation / "installed.marker").is_file():
        os.environ.setdefault("CODEX_WIDGET_DATA_DIR", str(installation))
    data_dir = Path(os.environ["CODEX_WIDGET_DATA_DIR"]) if os.environ.get("CODEX_WIDGET_DATA_DIR") else Path(os.environ.get("LOCALAPPDATA", Path.home())) / "CodexWidget"
    log_path = data_dir / "launcher.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.Logger("codex-widget-launcher", logging.INFO)
    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    daemon = "--daemon" in sys.argv[1:]
    logger.info("Starting pid=%s daemon=%s python=%s", os.getpid(), daemon, sys.executable)
    try:
        try:
            # Import failures need the same diagnostics/restart behavior as run failures.
            from codex_widget.cli import main

            code = main() or 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except Exception:
            logger.exception("Widget failed")
            code = 1
            # A modal dialog would leave a failed logon task running forever.
            if not daemon:
                try:
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(None, f"Codex Widget could not start. Details: {log_path}", "Codex Widget", 0x10)
                except Exception:
                    logger.exception("Could not display launch error")
        logger.info("Exiting pid=%s code=%s", os.getpid(), code)
        return code
    finally:
        handler.close()
        logger.removeHandler(handler)


if __name__ == "__main__":
    raise SystemExit(run())
