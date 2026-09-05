# Codex Reset Widget for Windows

Windows port of [codex-reset-widget](https://github.com/1RV1NG-Y/codex-reset-widget).

## Windows port

Windows 10/11 is supported using Python's Tk interface and the native Windows
notification area. No GTK, systemd, pip packages, or administrator access is needed.
The original Linux frontend remains available.

Requirements: Python 3.11+ with Tcl/Tk (the normal python.org installer includes it),
and an authenticated Codex CLI. The Windows Codex desktop CLI is also discovered
automatically.

From PowerShell:

```powershell
git clone https://github.com/1RV1NG-Y/codex-reset-widget-windows.git
cd codex-reset-widget-windows
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-windows.ps1
```

This copies the app into `%LOCALAPPDATA%\CodexWidget`, adds **Codex Widget** to
the Start menu, and opens it without a console window. The checkout can then be
moved independently. To also start the watcher when you sign in, run the same
installer with `-EnableStartup`. To install without opening it, use `-NoLaunch`.

For a portable run, double-click `launch-codex-widget.pyw`, or use:

```powershell
python .\launch-codex-widget.pyw
python .\launch-codex-widget.pyw --daemon
python .\launch-codex-widget.pyw --demo-reset
python .\launch-codex-widget.pyw --quit
```

Click the tray icon to open the card; right-click for Open, Check Now, and Quit.
Windows may put the icon in its tray overflow. PIN keeps the card above other
windows; Escape hides and unpins it. Reset alerts use Windows tray balloon
notifications and follow Windows notification settings. Five-hour auto-roll is
off by default; enabling it sends a small Codex request when a window expires.

State lives in `%LOCALAPPDATA%\CodexWidget\state.json`. If a console-free launch
fails, details are saved alongside it in `error.log`. To update, rerun the installer.
To uninstall, quit through the tray, delete the Codex Widget shortcuts from the
Start menu and `shell:startup`, then delete `%LOCALAPPDATA%\CodexWidget` (including
saved state).

Run Windows tests with:

```powershell
$env:PYTHONPATH = 'src'
python -m unittest discover -s tests -v
```

GTK-specific tests are skipped on Windows; the shared backend and Windows frontend
have their own tests.

## Original Linux version

A tiny resident Linux utility for monitoring Codex usage and global reset announcements. It stays out of the way in the GNOME status tray, opens as a compact transient card, and surfaces reset notifications without requiring a tracker website to remain open.

<p align="center">
  <img src="widget.png" width="340" alt="Codex Widget showing weekly usage, reset countdown, banked resets, and the latest global reset">
</p>

## Features

- Polls the verified reset feed once per minute and fails over to a separate tracker service when the primary is unavailable or malformed.
- Accepts verified archive records when the live feed changes shape, deduplicates events, and never replaces a newer saved reset with older source data.
- Refreshes real account usage every 60 seconds while the widget is visible.
- Shows five-hour and weekly usage with separate reset countdowns, plus banked resets and the latest global reset.
- Optionally keeps the five-hour window rolling with one tiny ephemeral Codex request after each reset; this is off by default.
- Correlates an active Tibo reset signal with a fresh low-usage account observation when the tracker has not confirmed it yet.
- Lives in the GNOME AppIndicator tray with Open, Check Now, and Quit actions.
- Drag from anywhere except the **PIN** control; pinned mode keeps the widget above other windows.
- Unpinned widgets hide on focus loss; Escape always dismisses and unpins.
- Starts automatically through a systemd user service.
- Stores only minimal local state under the XDG state directory.

## Notifications

<table>
  <tr>
    <td align="center"><strong>Reset announced</strong></td>
    <td align="center"><strong>Account confirmed</strong></td>
  </tr>
  <tr>
    <td><img src="Screenshot%20From%202026-08-10%2005-54-25.png" alt="Codex reset announced notification"></td>
    <td><img src="Screenshot%20From%202026-08-10%2005-53-49.png" alt="Codex reset confirmed notification"></td>
  </tr>
</table>

The indicator remains available in the desktop status area:

<p align="center">
  <img src="Screenshot%20From%202026-08-10%2006-14-33.png" alt="Codex Widget tray indicator in the GNOME panel">
</p>

## Requirements

- Linux with Python 3.11 or newer
- GTK 3 and PyGObject
- Ayatana AppIndicator, or GTK StatusIcon as a fallback
- A GNOME AppIndicator/KStatusNotifierItem extension for tray support
- `notify-send` from libnotify
- An installed and authenticated `codex` CLI with app-server support

On Arch-based distributions, the relevant system packages include `python`, `python-gobject`, `gtk3`, `libayatana-appindicator`, and `libnotify`.

## Install

```bash
git clone https://github.com/1RV1NG-Y/codex-reset-widget.git
cd codex-reset-widget
./install-user.sh
```

The installer creates an isolated application environment under `~/.local/share/codex-widget`, installs the GNOME launcher and tray icon, and enables the background user service. The installed application does not depend on the cloned repository afterward.

After installation, search for **Codex Widget** in the GNOME application menu or run:

```bash
codex-widget
```

## Usage

```bash
codex-widget               # open the usage widget
codex-widget --demo-reset  # preview the two-stage reset notification
codex-widget --daemon      # run silently in the background
codex-widget --quit        # stop the resident application
```

The tray menu also provides **Open Codex Widget**, **Check for resets now**, and **Quit Codex Widget**.

### Optional five-hour auto-roll

Use **ENABLE 5H AUTO-ROLL** in the widget to keep a five-hour window active
continuously. The opt-in is persisted across restarts. When enabled, the resident
process reads the current limits and schedules one ephemeral, read-only
`gpt-5.6-luna` request for ten seconds after the active five-hour window resets.
If no five-hour window is active, it starts one immediately.

Each activation consumes a tiny but nonzero amount of weekly usage. The scheduler
pauses at the weekly limit, retries failures after one minute, and runs an
independent one-minute watchdog so a missed long timer cannot leave the window
idle. The widget shows both the next action and the last successful request.
Disabling the toggle cancels the pending activation.

Manage automatic startup with:

```bash
systemctl --user status codex-widget.service
systemctl --user restart codex-widget.service
systemctl --user disable --now codex-widget.service
```

## How it works

```mermaid
flowchart LR
    Feed[codex-reset.com feed] -->|60 second poll| Watcher
    Watcher -->|new confirmed event| Notification
    Notification -->|one account check| AppServer[Codex app-server]
    Launcher[GNOME menu or tray] --> Widget
    Widget -->|on demand| AppServer
    AutoRoll[Optional 5-hour auto-roll] -->|one tiny request after reset| CodexExec[codex exec]
    CodexExec --> AppServer
    Watcher --> State[XDG state JSON]
    AppServer --> Widget
```

The global event source and the personal account source intentionally remain separate:

- `https://codex-reset.com/api/feed` answers whether a reset was publicly announced.
- `account/rateLimits/read` on the local Codex app-server answers what the current account actually reports.

## Resource usage

At idle, the resident GTK/Python process measured approximately 67 MB RSS and 0.0% CPU on the development system. Network activity is one small primary reset-feed request per minute, plus one fallback request only when the primary fails. Codex account queries run when the widget opens, once per minute while it remains visible, and once after a new reset event; visible-widget queries stop immediately when the widget hides. If five-hour auto-roll is enabled, the daemon also reads limits to schedule the next activation and sends one tiny Codex request per five-hour window.

## Development

Run the source tree without installing:

```bash
PYTHONPATH=src python3 -m codex_widget.cli
```

Run the test suite:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## License

MIT
