[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'CodexWidget'),
    [switch]$EnableStartup,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'

# Use the actual interpreter, not a Store execution alias, in the shortcuts.
$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { throw 'Install Python 3.11 or newer from python.org, including Tcl/Tk and the Python launcher.' }
$pythonExe = & $python.Source -c 'import sys, tkinter; assert sys.version_info >= (3, 11); print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ with Tcl/Tk is required.' }
$pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Cannot find $pythonw" }

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'src') | Out-Null
$launcher = Join-Path $InstallDir 'launch-codex-widget.pyw'
if (Test-Path -LiteralPath $launcher) {
    & $pythonExe $launcher --quit
    Start-Sleep -Milliseconds 700
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'src/codex_widget') -Destination (Join-Path $InstallDir 'src') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launch-codex-widget.pyw') -Destination $launcher -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $InstallDir -Force

$shortcutShell = New-Object -ComObject WScript.Shell
function New-WidgetShortcut([string]$Path, [string]$ExtraArgs) {
    $shortcut = $shortcutShell.CreateShortcut($Path)
    $shortcut.TargetPath = $pythonw
    $shortcut.Arguments = '"' + $launcher + '"' + $ExtraArgs
    $shortcut.WorkingDirectory = $InstallDir
    $shortcut.Description = 'Codex usage and global reset notifications'
    $shortcut.WindowStyle = 7
    $shortcut.Save()
}
$programs = [Environment]::GetFolderPath('Programs')
New-WidgetShortcut (Join-Path $programs 'Codex Widget.lnk') ''
if ($EnableStartup) {
    New-WidgetShortcut (Join-Path ([Environment]::GetFolderPath('Startup')) 'Codex Widget.lnk') ' --daemon'
}
if (-not $NoLaunch) {
    Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $InstallDir -WindowStyle Hidden
}
Write-Output "Installed to $InstallDir. Open Codex Widget from the Start menu."
