# zip_for_server.ps1
# Creates a clean deployment zip from the project root.
# Excludes venv, __pycache__, .git, .env, logs, and other dev artifacts.
#
# Usage (from anywhere):
#   PowerShell -ExecutionPolicy Bypass -File scripts\zip_for_server.ps1
#
# Output: server_deployment.zip in the project root
# On the server: extract the zip, create .env, then run scripts\start_server.bat

Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ZipPath     = Join-Path $ProjectRoot "server_deployment.zip"

# Prefixes (relative to project root, backslash-separated) that should be excluded.
# Any file whose relative path starts with one of these will be skipped.
$ExcludePrefixes = @(
    "venv\",
    ".git\",
    ".venv\",
    "env\",
    "ENV\",
    "__pycache__\",
    ".cache\",
    ".nlq_schema_cache\",
    ".pytest_cache\",
    ".mypy_cache\",
    "htmlcov\",
    "dist\",
    "build\",
    "node_modules\",
    "Data\lancedb\",
    ".vscode\",
    ".idea\"
)

# Exact relative filenames to exclude
$ExcludeFiles = @(
    ".env",
    "app_logs.txt",
    "server_logs.txt",
    "server_deployment.zip",
    "Thumbs.db",
    ".DS_Store"
)

# Extensions to exclude
$ExcludeExtensions = @(
    ".pyc", ".pyo", ".pyd",
    ".log",
    ".tmp", ".temp", ".bak",
    ".sqlite3", ".db"
)

function Test-ShouldExclude($RelPath) {
    # Check prefixes (folder-level exclusions)
    foreach ($prefix in $ExcludePrefixes) {
        if ($RelPath.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
        # Also catch top-level folder entries like "venv" without trailing slash
        $bare = $prefix.TrimEnd('\')
        if ($RelPath -eq $bare) { return $true }
        # Any sub-folder named __pycache__ anywhere in the tree
        if ($prefix -eq "__pycache__\" -and $RelPath -match '(^|\\)__pycache__\\') {
            return $true
        }
    }

    # Check exact filenames
    $FileName = Split-Path $RelPath -Leaf
    foreach ($f in $ExcludeFiles) {
        if ($FileName -eq $f) { return $true }
    }

    # Check extensions
    $Ext = [System.IO.Path]::GetExtension($RelPath).ToLower()
    if ($ExcludeExtensions -contains $Ext) { return $true }

    return $false
}

# ── Build the zip ─────────────────────────────────────────────────────────────

if (Test-Path $ZipPath) {
    Remove-Item $ZipPath -Force
    Write-Host "  Removed old zip." -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Scanning project files..." -ForegroundColor Cyan

$ZipStream = [System.IO.File]::Open($ZipPath, [System.IO.FileMode]::Create)
$Archive   = New-Object System.IO.Compression.ZipArchive($ZipStream, [System.IO.Compression.ZipArchiveMode]::Create)

$Included = 0
$Skipped  = 0

Get-ChildItem -Path $ProjectRoot -Recurse -File | ForEach-Object {
    $RelPath = $_.FullName.Substring($ProjectRoot.Length + 1)  # e.g. "agents\core\foo.py"

    if (Test-ShouldExclude $RelPath) {
        $Skipped++
        return
    }

    # ZIP entry always uses forward slashes
    $EntryName = $RelPath -replace '\\', '/'
    [System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($Archive, $_.FullName, $EntryName) | Out-Null
    $Included++
}

$Archive.Dispose()
$ZipStream.Dispose()

$SizeMB = [math]::Round((Get-Item $ZipPath).Length / 1MB, 2)

Write-Host ""
Write-Host "Done!" -ForegroundColor Green
Write-Host "  Files included : $Included"
Write-Host "  Files skipped  : $Skipped"
Write-Host "  Output         : $ZipPath ($SizeMB MB)"
Write-Host ""
Write-Host "Next steps on the server:" -ForegroundColor Yellow
Write-Host "  1. Copy server_deployment.zip to the server"
Write-Host "  2. Extract it (e.g. Expand-Archive server_deployment.zip -DestinationPath C:\Apps\Nexus)"
Write-Host "  3. Create .env in the project root with all required keys"
Write-Host "  4. Run: scripts\start_server.bat   (double-click or from CMD)"
