# Replace old OCR inventory records from a reviewed CSV/XLSX file.
# User must close PharmDesk server before running; no network access required.
$ErrorActionPreference = "Stop"

try {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    Set-Location -LiteralPath $root
    $python = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $python)) {
        throw "Python environment is missing. Run update-windows.bat first."
    }
    Write-Host ""
    Write-Host "PHARMDESK - REPLACE OUTDATED INVENTORY" -ForegroundColor Cyan
    Write-Host "Close the PharmDesk server BEFORE replacing the database." -ForegroundColor Yellow
    Write-Host "A backup will be created in the backups directory."
    Write-Host "This operation replaces all inventory rows for drawers LISTED in the selected file."
    Write-Host "Other drawers are not changed."
    Write-Host ""

    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "Select the REVIEWED PharmDesk inventory CSV or XLSX"
    $dialog.Filter = "PharmDesk files (*.csv;*.xlsx)|*.csv;*.xlsx|CSV files (*.csv)|*.csv|Excel files (*.xlsx)|*.xlsx"
    $dialog.Multiselect = $false
    if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        Write-Host "Cancelled. No inventory was changed."
        exit 0
    }
    $file = $dialog.FileName
    Write-Host "Selected: $file"
    Write-Host ""
    & $python -m app.cli replace-inventory $file
    if ($LASTEXITCODE -ne 0) {
        throw "Preview failed. Inventory was not changed."
    }

    Write-Host ""
    Write-Host "Check the counts above. Old rows in these drawers WILL be deleted." -ForegroundColor Yellow
    $answer = Read-Host "Type REPLACE to confirm, or press Enter to cancel"
    if ($answer -cne "REPLACE") {
        Write-Host "Cancelled. No inventory was changed."
        exit 0
    }
    & $python -m app.cli replace-inventory $file --confirm REPLACE
    if ($LASTEXITCODE -ne 0) {
        throw "Replacement failed. Check messages. The backup is preserved."
    }
    Write-Host ""
    Write-Host "Replacement complete. You may start PharmDesk." -ForegroundColor Green
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("FAILED: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
