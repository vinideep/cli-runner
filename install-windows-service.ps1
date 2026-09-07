# install-windows-service.ps1
#
# Registers cli-runner as a Windows scheduled task that starts at user logon
# and restarts automatically if the process exits (crash). This gives Windows
# the same "start on boot + auto-restart" behaviour the systemd unit gives
# Linux, using only built-in Task Scheduler — no NSSM or admin rights needed.
#
# Usage (from the project folder):
#   powershell -ExecutionPolicy Bypass -File .\install-windows-service.ps1
#
# Manage the task afterwards:
#   schtasks /query /tn "cli-runner" /v      # status + last result
#   schtasks /run   /tn "cli-runner"         # start it now
#   schtasks /end   /tn "cli-runner"         # stop it
#   schtasks /delete /tn "cli-runner" /f     # uninstall
#
# Logs go to cli-runner-windows.log in the project folder.

$ErrorActionPreference = "Stop"

# ── Configuration — EDIT THESE ────────────────────────────────────────────────
$TaskName = "cli-runner"
$ProjDir  = (Get-Location).Path                      # where this repo lives
$PythonExe = Join-Path $ProjDir ".venv\Scripts\python.exe"
$LogPath   = Join-Path $ProjDir "cli-runner-windows.log"

# CLI_TOKEN MUST match the backend's HOST_CLI_TOKEN.
$EnvLines = @(
    "SET CLI_TOKEN=CHANGE_ME_SAME_AS_BACKEND_HOST_CLI_TOKEN",
    "SET CLI_MAX_CONCURRENCY=5",
    "SET CLI_DEFAULT_TIMEOUT=1800"
)
# ─────────────────────────────────────────────────────────────────────────────

if (-not (Test-Path $PythonExe)) {
    Write-Error "Not found: $PythonExe`nCreate the venv first: python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path (Join-Path $ProjDir "main.py"))) {
    Write-Error "main.py not found in $ProjDir — run this script from the project folder."
}

# Wrapper batch file the task will execute. Setting env vars directly inside
# the XML action is fragile (XML attribute whitespace normalisation mangles
# multi-line commands), so we generate a plain .cmd and point the task at it.
$Wrapper = Join-Path $ProjDir "run-cli-runner.cmd"
@(
    "@echo off",
    "cd /d `"$ProjDir`""
) + $EnvLines + @(
    "`"$PythonExe`" `"$(Join-Path $ProjDir 'main.py')`" >> `"$LogPath`" 2>&1"
) | Set-Content -Path $Wrapper -Encoding ASCII

# The task runs as the CURRENT user (not SYSTEM) because the cmd/agy CLI
# credentials live under this user's profile (%USERPROFILE%). A SYSTEM task
# would find the CLIs unauthenticated.
$UserName = "$env:USERDOMAIN\$env:USERNAME"

$Xml = @"
<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>cli-runner host CLI bridge (starts at logon, restarts on failure)</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>$UserName</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$UserName</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$Wrapper</Command>
      <WorkingDirectory>$ProjDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

$XmlFile = Join-Path $Env:TEMP "cli-runner-task.xml"
[System.IO.File]::WriteAllText($XmlFile, $Xml, (New-Object System.Text.UTF8Encoding($false)))
schtasks /create /f /tn $TaskName /xml $XmlFile

Write-Host ""
Write-Host "Task '$TaskName' registered for user $UserName (wrapper: $Wrapper)."
Write-Host "Start it now with:  schtasks /run /tn `"$TaskName`""
Write-Host "Check status with:  schtasks /query /tn `"$TaskName`" /v"
Write-Host "Logs: $LogPath"
