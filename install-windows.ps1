[CmdletBinding()]
param(
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'CodexWidget'),
    # Retained for callers of older installers; startup is now the default.
    [switch]$EnableStartup,
    [switch]$NoStartup,
    [switch]$NoLaunch
)
$ErrorActionPreference = 'Stop'
if ($EnableStartup -and $NoStartup) { throw 'Choose either -EnableStartup or -NoStartup.' }

# Use the actual interpreter, not a Store execution alias, in the shortcuts.
$python = Get-Command python.exe -ErrorAction SilentlyContinue
if (-not $python) { throw 'Install Python 3.11 or newer from python.org, including Tcl/Tk and the Python launcher.' }
$pythonExe = & $python.Source -c 'import sys, tkinter; assert sys.version_info >= (3, 11); print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Python 3.11+ with Tcl/Tk is required.' }
$pythonw = Join-Path (Split-Path $pythonExe) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) { throw "Cannot find $pythonw" }

$taskName = 'Codex Widget'
$launcherName = 'launch-codex-widget.pyw'

function Get-ExistingInstallDir {
    # The logon task and Start menu shortcut record the physical install path.
    $arguments = @()
    $task = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($task) { $arguments += @($task.Actions | ForEach-Object { $_.Arguments }) }
    $shortcutPath = Join-Path ([Environment]::GetFolderPath('Programs')) 'Codex Widget.lnk'
    if (Test-Path -LiteralPath $shortcutPath) {
        $arguments += (New-Object -ComObject WScript.Shell).CreateShortcut($shortcutPath).Arguments
    }
    foreach ($argument in $arguments) {
        if ($argument -match '^"([^"]+)"') {
            $candidate = $Matches[1]
            if ((Split-Path -Leaf $candidate) -eq $launcherName -and (Test-Path -LiteralPath $candidate)) {
                return Split-Path -Parent $candidate
            }
        }
    }
    return $null
}

function Get-WidgetProcesses([string]$Launcher) {
    @(Get-CimInstance Win32_Process -Filter "Name like 'python%'" | Where-Object {
        $_.CommandLine -and $_.CommandLine.IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0
    })
}

# Updates stay where the widget is installed, keeping its state, even when this
# installer runs inside a packaged app that redirects %LOCALAPPDATA%.
$existing = $null
if (-not $PSBoundParameters.ContainsKey('InstallDir')) {
    $existing = Get-ExistingInstallDir
    if ($existing) { $InstallDir = $existing }
}

$InstallDir = [IO.Path]::GetFullPath($InstallDir)
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir 'src') | Out-Null
# Packaged terminals can redirect AppData writes. Windows logon tasks do not
# inherit that redirection, so persist the actual on-disk path in their actions.
$InstallDir = & $pythonExe -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).resolve(strict=True))' $InstallDir
if ($LASTEXITCODE -ne 0) { throw 'Cannot resolve the installed application directory.' }
if (-not $PSBoundParameters.ContainsKey('InstallDir') -and -not $existing -and $InstallDir -match '\\Packages\\[^\\]+\\LocalCache\\') {
    Write-Warning "This shell redirects AppData into an app package, so the widget is installed in $InstallDir. Run the installer from a regular PowerShell window, or pass -InstallDir, to choose another location."
}
$launcher = Join-Path $InstallDir $launcherName
if (Test-Path -LiteralPath $launcher) {
    & $pythonExe $launcher --quit
    # A new launch would hand its command to an instance that is still exiting.
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-WidgetProcesses $launcher).Count -gt 0 -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    $remaining = Get-WidgetProcesses $launcher
    if ($remaining.Count -gt 0) {
        Write-Warning 'The running widget did not exit within 15 seconds; stopping it.'
        $remaining | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    }
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'src/codex_widget') -Destination (Join-Path $InstallDir 'src') -Recurse -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'launch-codex-widget.pyw') -Destination $launcher -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'LICENSE') -Destination $InstallDir -Force
Set-Content -LiteralPath (Join-Path $InstallDir 'installed.marker') -Value 'Keep widget state in this installation directory.'

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
$startupShortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Codex Widget.lnk'
if ($NoStartup) {
    $existingTask = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    if ($existingTask) {
        Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    }
    if (Test-Path -LiteralPath $startupShortcut) {
        Remove-Item -LiteralPath $startupShortcut
    }
} else {
    # Run in the signed-in user's desktop session so the tray icon is visible.
    # Wait for Explorer, retry failed launches, and never stop after the default
    # three-day task limit or when a laptop switches to battery power.
    $userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    # The launcher resolves its source from __file__; it does not need a task
    # working directory (which Windows validates before launching Python).
    $action = New-ScheduledTaskAction -Execute $pythonw -Argument ('"' + $launcher + '" --daemon')
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $userSid
    $trigger.Delay = 'PT15S'
    $principal = New-ScheduledTaskPrincipal -UserId $userSid -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Keep Codex Widget in the notification area after Windows sign-in.' -Force | Out-Null
    # Remove the old Startup-folder mechanism only after registration succeeds.
    if (Test-Path -LiteralPath $startupShortcut) {
        Remove-Item -LiteralPath $startupShortcut
    }
}
if (-not $NoLaunch) {
    if ($NoStartup) {
        Start-Process -FilePath $pythonw -ArgumentList ('"' + $launcher + '"') -WorkingDirectory $InstallDir -WindowStyle Hidden
    } else {
        Start-ScheduledTask -TaskName $taskName
    }
}
Write-Output "Installed to $InstallDir. Open Codex Widget from the Start menu."
if (-not $NoStartup) { Write-Output 'Startup enabled: the Codex Widget logon task starts the tray watcher 15 seconds after sign-in.' }
