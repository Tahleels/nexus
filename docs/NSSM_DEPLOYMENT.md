# Deploying Nexus as a Windows Service (NSSM)

## Table of Contents
1. [What is NSSM and why use it](#1-what-is-nssm-and-why-use-it)
2. [Prerequisites](#2-prerequisites)
3. [Install the Service](#3-install-the-service)
4. [Configure the Service Account](#4-configure-the-service-account)
5. [Manage the Service](#5-manage-the-service)
6. [Environment Variables and .env](#6-environment-variables-and-env)
7. [Log Files](#7-log-files)
8. [Deploy Without Sharing Source Code](#8-deploy-without-sharing-source-code)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What is NSSM and why use it

**NSSM** (Non-Sucking Service Manager) wraps any executable — including `python app.py` — as a proper Windows Service. This means:

- The app **starts automatically** when the machine boots
- It runs under a **service account** you choose, so it inherits that account's permissions (network shares, UNC paths, domain resources)
- Windows restarts it automatically if it crashes
- No one needs to be logged in for the app to run
- You manage it like any other Windows service (`Start-Service`, `Stop-Service`, Task Manager, etc.)

---

## 2. Prerequisites

### 2.1 Download NSSM
1. Go to **https://nssm.cc/download**
2. Download the latest release zip
3. Extract and copy `nssm.exe` (from the `win64` folder) to `C:\Windows\System32\`
4. Verify: open PowerShell and run `nssm version` — you should see a version number

### 2.2 Python venv must exist
The service runs `venv\Scripts\python.exe app.py` inside the project directory.  
Make sure the venv is created and all packages installed:
```powershell
cd "C:\path\to\AI Reasoning Agent"
python -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### 2.3 .env file must be present
The service reads `.env` at startup. Make sure it exists in the project root with all required values filled in.

---

## 3. Install the Service

> **Run all commands as Administrator** (right-click PowerShell → Run as Administrator)

### 3.1 Using the provided script (easiest)

Edit `install_service.ps1` and set your service account details:
```powershell
$AccountUser = "DOMAIN\svc_nexus"   # your DOMAIN\username
$AccountPass  = "YourPassword"
```

Then run:
```powershell
.\install_service.ps1
```

### 3.2 Manual step-by-step

```powershell
# 1. Set variables
$AppDir    = "C:\path\to\AI Reasoning Agent"
$PythonExe = "$AppDir\venv\Scripts\python.exe"
$AppScript = "$AppDir\app.py"

# 2. Install the service
nssm install NexusApp $PythonExe $AppScript

# 3. Set working directory (IMPORTANT — app.py reads .env from here)
nssm set NexusApp AppDirectory $AppDir

# 4. Set display name
nssm set NexusApp DisplayName "Nexus Reasoning Agent"

# 5. Auto-start on boot
nssm set NexusApp Start SERVICE_AUTO_START

# 6. Log output to files
nssm set NexusApp AppStdout "$AppDir\service_stdout.log"
nssm set NexusApp AppStderr "$AppDir\service_stderr.log"
nssm set NexusApp AppRotateFiles 1
nssm set NexusApp AppRotateBytes 10485760

# 7. Start it
Start-Service NexusApp
```

---

## 4. Configure the Service Account

This is the key step that gives the app access to network shares and domain resources.

### 4.1 Why it matters
By default Windows services run as `LocalSystem` which has no network access. By setting a domain service account, the app inherits **everything that account can access** — UNC paths, SQL Server with Windows auth, file shares, etc.

### 4.2 Set the account via NSSM

```powershell
# Domain service account (recommended)
nssm set NexusApp ObjectName "DOMAIN\svc_nexus" "YourPassword"

# Or the built-in Network Service (has basic domain access, no password needed)
nssm set NexusApp ObjectName "NT AUTHORITY\NetworkService"

# Or Local System (broad local access, no network share access)
nssm set NexusApp ObjectName LocalSystem
```

### 4.3 Grant the service account the right to log on as a service

1. Open **Local Security Policy** (`secpol.msc`)
2. Navigate to **Security Settings → Local Policies → User Rights Assignment**
3. Open **Log on as a service**
4. Add your service account (`DOMAIN\svc_nexus`)

Or via PowerShell (requires `Carbon` module or `ntrights.exe`):
```powershell
# Using ntrights.exe (part of Windows Server Resource Kit)
ntrights +r SeServiceLogonRight -u "DOMAIN\svc_nexus"
```

### 4.4 What the service account should have access to
Grant the account permissions to:
- The project directory (read/execute on all files, write on `Data\`, logs)
- Any network shares you want the app to access (`\\fileserver\share\...`)
- The SQL Server database (if using Windows Authentication)

---

## 5. Manage the Service

### Start / Stop / Restart
```powershell
Start-Service   NexusApp
Stop-Service    NexusApp
Restart-Service NexusApp
```

### Check status
```powershell
Get-Service NexusApp
```

### Open the NSSM GUI editor (to change any setting visually)
```powershell
nssm edit NexusApp
```

### Remove the service completely
```powershell
nssm stop   NexusApp confirm
nssm remove NexusApp confirm
```

### Check if it survived a reboot
```powershell
# Reboot and then:
Get-Service NexusApp | Select-Object Status, StartType
# Should show: Running, Automatic
```

---

## 6. Environment Variables and .env

The app loads `.env` via `python-dotenv` at startup. Since the service process runs inside the `AppDirectory`, it finds `.env` automatically — no extra configuration needed.

### Point the agent file store to a network share
In `.env`, uncomment and set:
```
AGENT_FILE_STORE=\\fileserver\share\nexus_agent_store
```
Once set, all tools (process CSV, create/load files, folder report) will read and write to that share. The service account must have read/write access to that path.

### Never put .env in source control
`.env` contains secrets (API keys, DB passwords). Keep it on the server only and back it up separately.

---

## 7. Log Files

| File | Contents |
|---|---|
| `service_stdout.log` | Normal app output (Flask startup messages, request logs) |
| `service_stderr.log` | Errors and tracebacks — check this first when something is broken |
| `app_logs.txt` | Application-level logs written by the app itself |

### Tail logs in real time
```powershell
Get-Content "service_stderr.log" -Wait -Tail 50
```

### Rotate logs manually
Logs auto-rotate at 10 MB (configured in the install script). To force a rotation:
```powershell
Restart-Service NexusApp
```

---

## 8. Deploy Without Sharing Source Code

Yes — you can deploy without giving anyone the `.py` source files. There are two approaches:

---

### Option A — PyInstaller (bundle into a single .exe)

PyInstaller packages your entire app and its dependencies into one `.exe`. The source code is not readable.

#### Step 1 — Install PyInstaller in your venv
```powershell
venv\Scripts\pip install pyinstaller
```

#### Step 2 — Build the executable
```powershell
venv\Scripts\pyinstaller `
  --onefile `
  --name NexusApp `
  --add-data "templates;templates" `
  --add-data "static;static" `
  --add-data ".env;." `
  app.py
```

This creates `dist\NexusApp.exe`.

> **Note:** `--onefile` is convenient but slower to start. Use `--onedir` for faster startup — it creates a `dist\NexusApp\` folder instead of a single file.

#### Step 3 — Point NSSM at the exe instead of Python
```powershell
nssm install NexusApp "C:\deploy\NexusApp.exe"
nssm set NexusApp AppDirectory "C:\deploy"
nssm set NexusApp ObjectName "DOMAIN\svc_nexus" "Password"
Start-Service NexusApp
```

#### What to deploy (no source code needed)
```
C:\deploy\
  NexusApp.exe    ← compiled app
  .env                   ← secrets (kept on server, not in the build)
  Data\                  ← database and file store
```

#### Caveats
- Flask templates and static files must be bundled with `--add-data` (shown above)
- `.env` should NOT be bundled in the exe — keep it outside so you can change settings without rebuilding
- If the app imports dynamic plugins or custom tools at runtime, you may need `--collect-all` flags

---

### Option B — Compile to .pyc only (lighter, not fully protected)

This removes readable `.py` source but the `.pyc` bytecode can be decompiled with tools like `uncompyle6`. Use this only to prevent casual reading, not for serious IP protection.

```powershell
# Compile all .py files to .pyc
python -m compileall . -b

# Delete original .py files (keep __init__.py stubs if needed)
Get-ChildItem -Recurse -Filter "*.py" | Where-Object { $_.Name -ne "__init__.py" } | Remove-Item
```

Then run using:
```powershell
python app.pyc
```

---

### Option A vs Option B

| | PyInstaller (.exe) | .pyc only |
|---|---|---|
| Source hidden from casual reading | Yes | Partial |
| Reversible | Hard (but possible with tools) | Easier |
| Deploy complexity | Medium | Low |
| Performance | Same (slightly slower cold start) | Same |
| Works with NSSM | Yes | Yes |
| Recommended for IP protection | Yes | No |

---

## 9. Troubleshooting

### Service won't start
1. Check `service_stderr.log` for the error
2. Run the command manually first to confirm it works:
   ```powershell
   cd "C:\path\to\AI Reasoning Agent"
   venv\Scripts\python.exe app.py
   ```
3. Make sure the service account has **Read & Execute** on the project folder

### App can't access network share
1. Confirm the service account has access: log in as that user and try to access `\\server\share`
2. Confirm the service is running AS that account: `Get-WmiObject Win32_Service -Filter "Name='NexusApp'" | Select-Object StartName`
3. If using `AGENT_FILE_STORE`, make sure the path in `.env` uses double backslashes: `\\\\server\\share\\folder`

### Port already in use
The app probably didn't shut down cleanly. Kill the old process:
```powershell
netstat -ano | findstr :5000
taskkill /PID <pid> /F
```

### Service account password changed
Update NSSM immediately or the service will fail to start after next reboot:
```powershell
nssm set NexusApp ObjectName "DOMAIN\svc_nexus" "NewPassword"
Restart-Service NexusApp
```
