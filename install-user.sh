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
DESKTOP_FILE="$DESKTOP_DIR/io.github.codex_widget.CodexWidget.desktop"
DESKTOP_CONTENT=$(sed "s|^Exec=.*|Exec=$BIN_DIR/codex-widget|" \
  "$PROJECT_DIR/data/io.github.codex_widget.CodexWidget.desktop")
DESKTOP_CHANGED=false
if [ ! -f "$DESKTOP_FILE" ] || [ "$(cat "$DESKTOP_FILE")" != "$DESKTOP_CONTENT" ]; then
  DESKTOP_TEMP="$DESKTOP_FILE.new"
  printf '%s\n' "$DESKTOP_CONTENT" > "$DESKTOP_TEMP"
  mv -f "$DESKTOP_TEMP" "$DESKTOP_FILE"
  DESKTOP_CHANGED=true
fi

ICON_SOURCE="$PROJECT_DIR/src/codex_widget/assets/codex-widget-symbolic.svg"
ICON_FILE="$ICON_DIR/codex-widget-symbolic.svg"
ICON_CHANGED=false
if ! cmp -s "$ICON_SOURCE" "$ICON_FILE"; then
  install -m 644 "$ICON_SOURCE" "$ICON_FILE"
  ICON_CHANGED=true
fi

SERVICE_FILE="$SYSTEMD_DIR/codex-widget.service"
SERVICE_CONTENT=$(sed "s|^ExecStart=.*|ExecStart=$BIN_DIR/codex-widget --daemon|" \
  "$PROJECT_DIR/data/codex-widget.service")
SERVICE_CHANGED=false
if [ ! -f "$SERVICE_FILE" ] || [ "$(cat "$SERVICE_FILE")" != "$SERVICE_CONTENT" ]; then
  SERVICE_TEMP="$SERVICE_FILE.new"
  printf '%s\n' "$SERVICE_CONTENT" > "$SERVICE_TEMP"
  mv -f "$SERVICE_TEMP" "$SERVICE_FILE"
  SERVICE_CHANGED=true
fi

if [ "$DESKTOP_CHANGED" = true ] && command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR"
fi
if [ "$ICON_CHANGED" = true ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null
fi
if [ "$SERVICE_CHANGED" = true ]; then
  systemctl --user daemon-reload
fi
systemctl --user enable --now codex-widget.service

printf '%s\n' "Codex Widget installed. Open it from GNOME or run: $BIN_DIR/codex-widget"
