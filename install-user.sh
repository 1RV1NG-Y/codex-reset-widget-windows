#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-"$HOME/.local/share"}
CONFIG_HOME=${XDG_CONFIG_HOME:-"$HOME/.config"}
BIN_DIR="$HOME/.local/bin"
APP_HOME="$DATA_HOME/codex-widget"
VENV_DIR="$APP_HOME/venv"
DESKTOP_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/scalable/status"
SYSTEMD_DIR="$CONFIG_HOME/systemd/user"

python3 -m venv --system-site-packages "$VENV_DIR"
"$VENV_DIR/bin/pip" install --no-deps --upgrade "$PROJECT_DIR"

mkdir -p "$BIN_DIR" "$DESKTOP_DIR" "$ICON_DIR" "$SYSTEMD_DIR"
ln -sfn "$VENV_DIR/bin/codex-widget" "$BIN_DIR/codex-widget"
sed "s|^Exec=.*|Exec=$BIN_DIR/codex-widget|" \
  "$PROJECT_DIR/data/io.github.codex_widget.CodexWidget.desktop" \
  > "$DESKTOP_DIR/io.github.codex_widget.CodexWidget.desktop"
install -m 644 \
  "$PROJECT_DIR/src/codex_widget/assets/codex-widget-symbolic.svg" \
  "$ICON_DIR/codex-widget-symbolic.svg"
sed "s|^ExecStart=.*|ExecStart=$BIN_DIR/codex-widget --daemon|" \
  "$PROJECT_DIR/data/codex-widget.service" \
  > "$SYSTEMD_DIR/codex-widget.service"

command -v update-desktop-database >/dev/null 2>&1 && \
  update-desktop-database "$DESKTOP_DIR"
command -v gtk-update-icon-cache >/dev/null 2>&1 && \
  gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null
systemctl --user daemon-reload
systemctl --user enable --now codex-widget.service

printf '%s\n' "Codex Widget installed. Open it from GNOME or run: $BIN_DIR/codex-widget"
