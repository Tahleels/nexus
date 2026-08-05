# install_service.ps1
# Installs the Nexus app as a Windows service using NSSM.
# The service runs under a specified service account so it inherits that
# account's file-system permissions, including access to UNC/network shares.
#
# Prerequisites:
#   1. Download NSSM from https://nssm.cc/download and place nssm.exe in PATH
#      (or set $NssmExe below to the full path)
#   2. Run this script as Administrator
#
# Usage:
#   .\install_service.ps1
# To remove the service later:
#   nssm remove NexusApp confirm

# ── CONFIGURE THESE ───────────────────────────────────────────────────────────
$ServiceName   = "NexusApp"
$DisplayName   = "Nexus Reasoning Agent"
$AppDir        = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonExe     = Join-Path $AppDir "venv\Scripts\python.exe"
$AppScript     = Join-Path $AppDir "app.py"
$NssmExe       = "nssm"          # or full path e.g. "C:\tools\nssm.exe"

# Service account that has access to shared paths and network resources.
# Use "LocalSystem" to run as the machine account (has broad local access).
# Use "DOMAIN\username" for a domain service account (recommended for network shares).
$AccountUser   = "DOMAIN\svc_nexus"    # <-- change this
$AccountPass   = "YourPassword"         # <-- change this
# ─────────────────────────────────────────────────────────────────────────────

# Require elevation
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "Run this script as Administrator."
    exit 1
}

# Check NSSM is available
if (-not (Get-Command $NssmExe -ErrorAction SilentlyContinue)) {
    Write-Error "nssm.exe not found. Download from https://nssm.cc/download and add to PATH."
    exit 1
}

# Check venv Python exists
if (-not (Test-Path $PythonExe)) {
    Write-Error "Python not found at: $PythonExe`nActivate/create the venv first."
    exit 1
}

Write-Host "Installing service '$ServiceName'..." -ForegroundColor Cyan

# Remove existing service if present
$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "  Removing existing service..."
    & $NssmExe stop    $ServiceName confirm 2>$null
    & $NssmExe remove  $ServiceName confirm
}

# Install the service
& $NssmExe install $ServiceName $PythonExe $AppScript

# Configure working directory and environment
& $NssmExe set $ServiceName AppDirectory   $AppDir
& $NssmExe set $ServiceName DisplayName    $DisplayName
& $NssmExe set $ServiceName Description    "Nexus AI Reasoning Agent (Flask)"
& $NssmExe set $ServiceName Start          SERVICE_AUTO_START

# Redirect stdout / stderr to log files
& $NssmExe set $ServiceName AppStdout      (Join-Path $AppDir "service_stdout.log")
& $NssmExe set $ServiceName AppStderr      (Join-Path $AppDir "service_stderr.log")
& $NssmExe set $ServiceName AppRotateFiles 1
& $NssmExe set $ServiceName AppRotateBytes 10485760   # 10 MB

# Set service account — this is the key line that grants network/share access
if ($AccountUser -eq "LocalSystem") {
    & $NssmExe set $ServiceName ObjectName LocalSystem
} else {
    & $NssmExe set $ServiceName ObjectName $AccountUser $AccountPass
}

Write-Host "  Service account set to: $AccountUser" -ForegroundColor Green

# Start the service
Write-Host "Starting service..."
Start-Service -Name $ServiceName
$svc = Get-Service -Name $ServiceName
Write-Host "  Status: $($svc.Status)" -ForegroundColor Green

Write-Host ""
Write-Host "Done. The app is now running as '$AccountUser'." -ForegroundColor Cyan
Write-Host "Any path that account can access (including \\server\share\...) will work in all tools."
Write-Host ""
Write-Host "Useful commands:"
Write-Host "  Get-Service $ServiceName          # check status"
Write-Host "  Stop-Service $ServiceName         # stop"
Write-Host "  Restart-Service $ServiceName      # restart after code changes"
Write-Host "  nssm edit $ServiceName            # GUI editor"
