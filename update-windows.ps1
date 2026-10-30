# PharmDesk local Git updater (Windows PowerShell 5.1+).
# Run only after closing the local PharmDesk server.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

try {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    Set-Location -LiteralPath $root

    Write-Host "[1/5] Checking Git..." -ForegroundColor Cyan
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not installed or not in PATH. Install Git for Windows and reopen the terminal."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
        throw "This directory is not a Git clone. Clone the repo first, then run the updater from the cloned directory."
    }

    $origin = (& git remote get-url origin 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $origin -notmatch 'github\.com[:/]BogdanMaslyuk/PharmDesk(\.git)?$') {
        throw "Unexpected Git origin. Expected BogdanMaslyuk/PharmDesk; refusing to update another repository."
    }

    $branch = (& git branch --show-current 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
        throw "Switch to the main branch before updating."
    }

    $trackedChanges = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw "Could not check Git status." }
    if ($trackedChanges.Count -gt 0) {
        Write-Host "Tracked files have local changes:" -ForegroundColor Yellow
        $trackedChanges | ForEach-Object { Write-Host $_ }
        throw "Update cancelled. Save/commit your code changes first; nothing was discarded."
    }

    Write-Host "[2/5] Backing up the local database..." -ForegroundColor Cyan
    $dbPath = Join-Path $root "pharmdesk.sqlite3"
    if (Test-Path -LiteralPath $dbPath) {
        $backupDirectory = Join-Path $root "backups"
        New-Item -ItemType Directory -Path $backupDirectory -Force | Out-Null
        $backupName = "pharmdesk_" + (Get-Date -Format "yyyyMMdd_HHmmss") + "_" + $PID + ".sqlite3"
        $backupPath = Join-Path $backupDirectory $backupName
        Copy-Item -LiteralPath $dbPath -Destination $backupPath -ErrorAction Stop
        Write-Host "Database backup: $backupPath"
    }
    else {
        Write-Host "No local database found; nothing to back up."
    }

    Write-Host "[3/5] Updating application from GitHub..." -ForegroundColor Cyan
    & git pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        throw "Git pull failed. Local data was not deleted. See the Git message above."
    }

    Write-Host "[4/5] Checking Python environment..." -ForegroundColor Cyan
    $pythonPath = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv ".venv"
            if ($LASTEXITCODE -ne 0) { throw "Unable to create Python environment." }
        }
        elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv ".venv"
            if ($LASTEXITCODE -ne 0) { throw "Unable to create Python environment." }
        }
        else {
            throw "Python not found. Install Python 3.11+ and rerun the updater."
        }
    }

    & $pythonPath -m pip install -r "requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed. Check your network and Python installation."
    }

    Write-Host "[5/5] Running automated tests..." -ForegroundColor Cyan
    & $pythonPath -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "Automated tests failed after update. Please report the output. The database backup is preserved."
    }

    Write-Host ""
    Write-Host "SUCCESS: PharmDesk code is up to date." -ForegroundColor Green
    Write-Host "Your database was NOT replaced, and it was NOT uploaded to GitHub."
    Write-Host "Next: run start-windows.bat and open http://127.0.0.1:8000"
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("UPDATE STOPPED: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "No Git reset or forced update was performed."
    exit 1
}
