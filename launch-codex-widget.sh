#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export CODEX_WIDGET_NO_SERVICE=1
exec /usr/bin/python3 -m codex_widget.cli "$@"
