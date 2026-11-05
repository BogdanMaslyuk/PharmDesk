# PharmDesk - local Windows PowerShell 5.1 console.
# Use pharmdesk.bat if your machine blocks direct .ps1 execution.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root
$env:PYTHONUTF8 = "1"
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
} catch {
    # Consoles that do not allow changing encoding still work.
}

function Get-AppPython {
    $path = Join-Path $root ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $path)) {
        throw "Среда Python не создана. Сначала выберите пункт 2 (обновление и установка)."
    }
    return $path
}

function Test-LocalServer {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect("127.0.0.1", 8000, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(350)) { return $false }
        $client.EndConnect($result)
        return $true
    } catch { return $false }
    finally {
        if ($null -ne $result) { $result.AsyncWaitHandle.Close() }
        $client.Close()
    }
}

function Run-LocalCommand {
    param([string]$Command)
    $python = Get-AppPython
    if ($Command -eq "backup") {
        & $python -X utf8 -m app.maintenance backup
    }
    elseif ($Command -eq "diagnose") {
        & $python -X utf8 -m app.maintenance diagnose
    }
    elseif ($Command -eq "tests") {
        & $python -X utf8 -m pytest -q
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Команда завершилась с ошибкой (код $LASTEXITCODE)."
    }
}

while ($true) {
    Write-Host ""
    Write-Host "======================================" -ForegroundColor Cyan
    # Read the actual installed application version, not a stale menu label.
    $versionFile = Join-Path $root "app\version.py"
    $version = "unknown"
    if (Test-Path -LiteralPath $versionFile) {
        $line = Select-String -Path $versionFile -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
        if ($null -ne $line -and $line.Matches.Count -gt 0) {
            $version = $line.Matches[0].Groups[1].Value
        }
    }
    Write-Host ("         PHARMDESK v" + $version)
    Write-Host "======================================" -ForegroundColor Cyan
    Write-Host "1. Запустить приложение"
    Write-Host "2. Обновить программу из GitHub"
    Write-Host "3. Создать резервную копию"
    Write-Host "4. Проверить состояние системы"
    Write-Host "5. Запустить автоматические тесты"
    Write-Host "0. Выход"
    Write-Host ""
    $choice = Read-Host "Выберите действие"
    try {
        switch ($choice.Trim()) {
            "1" {
                if (Test-LocalServer) {
                    Write-Host "Порт 8000 уже занят. Закройте ранее запущенный PharmDesk." -ForegroundColor Yellow
                } else {
                    & (Join-Path $root "start-windows.bat")
                }
            }
            "2" {
                # Child process prevents the updater's exit code from closing this menu.
                & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "update-windows.ps1")
                if ($LASTEXITCODE -ne 0) { throw "Обновление не завершилось; проверьте сообщения выше." }
            }
            "3" { Run-LocalCommand "backup" }
            "4" { Run-LocalCommand "diagnose" }
            "5" { Run-LocalCommand "tests" }
            "0" { Write-Host "Выход."; exit 0 }
            default { Write-Host "Выберите номер от 0 до 5." -ForegroundColor Yellow }
        }
    } catch {
        Write-Host ("Ошибка: " + $_.Exception.Message) -ForegroundColor Red
    }
    if ($choice.Trim() -ne "0") {
        [void](Read-Host "Нажмите Enter для возврата в меню")
    }
}
