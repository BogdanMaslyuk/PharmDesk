# Safely update PharmDesk from the public GitHub repository.
# Existing inventory and review photos remain exclusively on this computer.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Test-LocalServer {
    $client = New-Object System.Net.Sockets.TcpClient
    $connection = $null
    try {
        $connection = $client.BeginConnect("127.0.0.1", 8000, $null, $null)
        if (-not $connection.AsyncWaitHandle.WaitOne(350)) { return $false }
        $client.EndConnect($connection)
        return $true
    } catch { return $false }
    finally {
        if ($null -ne $connection) { $connection.AsyncWaitHandle.Close() }
        $client.Close()
    }
}

try {
    $root = Split-Path -Parent $MyInvocation.MyCommand.Path
    Set-Location -LiteralPath $root
    $env:PYTHONUTF8 = "1"
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch {}

    Write-Host "[1/5] Проверка Git и работающего сервера..." -ForegroundColor Cyan
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git не установлен. Установите Git for Windows."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $root ".git"))) {
        throw "Это не папка Git. Откройте PharmDesk-git."
    }
    $origin = (& git remote get-url origin 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $origin -notmatch 'github\.com[:/]BogdanMaslyuk/PharmDesk(\.git)?$') {
        throw "Неверный репозиторий GitHub; обновление отменено."
    }
    $branch = (& git branch --show-current 2>&1 | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $branch -ne "main") {
        throw "Для обновления перейдите на ветку main; автоматического переключения нет."
    }
    $changes = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw "Не удалось проверить изменения Git." }
    if ($changes.Count -gt 0) {
        $changes | ForEach-Object { Write-Host $_ -ForegroundColor Yellow }
        throw "Есть локальные изменения кода. Они не будут удалены или перезаписаны."
    }
    if (Test-LocalServer) {
        throw "Порт 8000 занят. Остановите PharmDesk через Ctrl+C и повторите обновление."
    }

    Write-Host "[2/5] Резервное копирование локальных данных..." -ForegroundColor Cyan
    $pythonPath = Join-Path $root ".venv\Scripts\python.exe"
    $dbPath = Join-Path $root "pharmdesk.sqlite3"
    $needsBackup = (Test-Path -LiteralPath $dbPath) -or (-not [string]::IsNullOrWhiteSpace($env:DATABASE_URL))
    if ($needsBackup) {
        if (Test-Path -LiteralPath $pythonPath) {
            & $pythonPath -X utf8 -m app.maintenance backup
        }
        elseif (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -X utf8 -m app.maintenance backup
        }
        elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -X utf8 -m app.maintenance backup
        }
        else {
            throw "Не найден Python, необходимый для создания резервной копии."
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Резервная копия не создана. Загрузка обновлений отменена."
        }
    } else {
        Write-Host "База отсутствует — резервное копирование не требуется."
    }

    Write-Host "[3/5] Получение обновлений из GitHub..." -ForegroundColor Cyan
    & git pull --ff-only origin main
    if ($LASTEXITCODE -ne 0) {
        throw "Git не смог безопасно обновить код. Локальные файлы не сбрасывались."
    }

    Write-Host "[4/5] Проверка среды Python и библиотек..." -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $pythonPath)) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv ".venv"
        }
        elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv ".venv"
        }
        else {
            throw "Python не установлен. Требуется версия 3.11 или новее."
        }
        if ($LASTEXITCODE -ne 0) { throw "Не удалось создать среду Python." }
    }

    & $pythonPath -m pip install -r "requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось установить библиотеки Python."
    }

    Write-Host "[5/5] Автоматические тесты..." -ForegroundColor Cyan
    & $pythonPath -X utf8 -m pytest -q
    if ($LASTEXITCODE -ne 0) {
        throw "Тесты не прошли. Запускать программу до выяснения причины не рекомендуется."
    }

    & $pythonPath -X utf8 -m app.maintenance version
    Write-Host ""
    Write-Host "Обновление завершено. База не заменена и не отправлена в GitHub." -ForegroundColor Green
    Write-Host "Запуск: .\pharmdesk.bat или .\start-windows.bat"
    exit 0
}
catch {
    Write-Host ""
    Write-Host ("ОБНОВЛЕНИЕ ОСТАНОВЛЕНО: " + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Принудительного сброса Git и удаления базы не выполнялось."
    exit 1
}
