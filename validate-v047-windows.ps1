param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("Prepare", "Run")]
    [string]$Action
)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$env:PYTHONUTF8 = "1"
try {
    if ((Split-Path -Leaf $root) -ne "PharmDesk-v047") {
        throw "Run this script from the isolated PharmDesk-v047 clone."
    }
    $venv = Join-Path $root ".venv\Scripts\python.exe"
    Push-Location -LiteralPath $root
    try {
        if ($Action -eq "Prepare") {
            if (-not (Test-Path -LiteralPath $venv)) {
                Write-Host "Creating separate Python environment..." -ForegroundColor Cyan
                if (Get-Command py -ErrorAction SilentlyContinue) {
                    & py -3 -m venv ".venv"
                } elseif (Get-Command python -ErrorAction SilentlyContinue) {
                    & python -m venv ".venv"
                } else {
                    throw "Python 3.11 or later is required."
                }
                if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed." }
            }
            Write-Host "Installing dependencies into isolated environment..." -ForegroundColor Cyan
            & $venv -m pip install -r "requirements.txt"
            if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }
            Write-Host "Running synthetic checks before accessing any production DB..." -ForegroundColor Cyan
            & $venv -X utf8 -m pytest -q
            if ($LASTEXITCODE -ne 0) { throw "Regression checks failed." }
        } elseif (-not (Test-Path -LiteralPath $venv)) {
            throw "Prepare must complete before Run."
        }
        if ($Action -eq "Prepare") {
            & $venv -X utf8 -m scripts.validation_v047 prepare
        } else {
            & $venv -X utf8 -m scripts.validation_v047 run
        }
        if ($LASTEXITCODE -ne 0) { throw "Validation action failed safely." }
        Write-Host ("Validation " + $Action + " completed successfully.") -ForegroundColor Green
    } finally {
        Pop-Location
    }
} catch {
    Write-Host ("STOPPED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
