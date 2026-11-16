# PharmDesk Control Center — Windows PowerShell 5.1.
# FastAPI serves BOTH the website (frontend) and the backend at 127.0.0.1:8000.
[CmdletBinding()]
param(
    [ValidateSet("Menu","Start","Stop","Open","OpenMatching","Status","Diagnose","Backup","Update","Tests","Logs","Folder")]
    [string]$Action="Menu"
)
$ErrorActionPreference="Stop"
Set-StrictMode -Version Latest
$root=Split-Path -Parent $MyInvocation.MyCommand.Path
$runDir=Join-Path $root ".run"
$pidFile=Join-Path $runDir "server.pid"
$env:PYTHONUTF8="1"
try { [Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false) } catch {}

function Get-Python {
    $path=Join-Path $root ".venv\Scripts\python.exe"
    if(-not (Test-Path -LiteralPath $path)){
        throw "Среда Python отсутствует. В пульте выберите «Обновить программу», затем повторите запуск."
    }
    return $path
}
function Test-Port {
    $client=New-Object System.Net.Sockets.TcpClient
    $operation=$null
    try {
        $operation=$client.BeginConnect("127.0.0.1",8000,$null,$null)
        if(-not $operation.AsyncWaitHandle.WaitOne(400)){ return $false }
        $client.EndConnect($operation)
        return $true
    } catch { return $false }
    finally {
        if($null -ne $operation){$operation.AsyncWaitHandle.Close()}
        $client.Close()
    }
}
function Get-Health {
    try {
        $health=Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 2
        if($health.status -eq "ok" -and -not [string]::IsNullOrWhiteSpace($health.version)){
            return $health
        }
    } catch {}
    return $null
}
function Get-ManagedProcess {
    if(-not (Test-Path -LiteralPath $pidFile)){return $null}
    $stored=(Get-Content -LiteralPath $pidFile -TotalCount 1 -ErrorAction Stop).Trim()
    $number=0
    if(-not [int]::TryParse($stored,[ref]$number) -or $number -le 0){return $null}
    $proc=Get-CimInstance Win32_Process -Filter ("ProcessId="+$number) -ErrorAction SilentlyContinue
    if($null -eq $proc){return $null}
    $expected=Join-Path $root ".venv\Scripts\python.exe"
    if($null -eq $proc.ExecutablePath -or
       -not [string]::Equals($proc.ExecutablePath,$expected,[System.StringComparison]::OrdinalIgnoreCase)){
        return $null
    }
    if($null -eq $proc.CommandLine -or
       $proc.CommandLine -notmatch "uvicorn\s+app\.main:app" -or
       $proc.CommandLine -notmatch "\-\-port\s+8000"){
        return $null
    }
    return $proc
}
function Get-Version {
    $file=Join-Path $root "app\version.py"
    if(-not (Test-Path -LiteralPath $file)){return "не определена"}
    $value=Select-String -Path $file -Pattern 'APP_VERSION\s*=\s*"([^"]+)"' | Select-Object -First 1
    if($null -ne $value -and $value.Matches.Count -gt 0){return $value.Matches[0].Groups[1].Value}
    return "не определена"
}
function Open-Browser([string]$Path="/") {
    if($null -eq (Get-Health)){
        throw "Сначала запустите сервер PharmDesk через пункт 1."
    }
    Start-Process ("http://127.0.0.1:8000"+$Path) | Out-Null
}
function Start-Service {
    $healthy=Get-Health
    if($null -ne $healthy){
        Write-Host ("PharmDesk уже запущен: v"+$healthy.version) -ForegroundColor Green
        Open-Browser "/"
        return
    }
    if(Test-Port){
        throw "Порт 8000 занят другой программой либо сервер не отвечает. Не запускаем вторую копию."
    }
    $python=Get-Python
    if(-not (Test-Path -LiteralPath $runDir)){New-Item -ItemType Directory -Path $runDir | Out-Null}
    $timestamp=Get-Date -Format "yyyyMMdd_HHmmss"
    $stdout=Join-Path $runDir ("server_"+$timestamp+".out.log")
    $stderr=Join-Path $runDir ("server_"+$timestamp+".err.log")
    Write-Host "Запускаю сервер (он же обслуживает сайт)..." -ForegroundColor Cyan
    $process=Start-Process -FilePath $python -ArgumentList @(
        "-X","utf8","-m","uvicorn","app.main:app",
        "--host","127.0.0.1","--port","8000"
    ) -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
    [System.IO.File]::WriteAllText($pidFile,[string]$process.Id,[System.Text.Encoding]::ASCII)
    for($i=0;$i -lt 30;$i++){
        Start-Sleep -Milliseconds 500
        $healthy=Get-Health
        if($null -ne $healthy){
            Write-Host ("Сервер готов. Версия: "+$healthy.version) -ForegroundColor Green
            Write-Host "Сайт: http://127.0.0.1:8000"
            Open-Browser "/"
            return
        }
        if($process.HasExited){break}
    }
    Write-Host ("Лог ошибок: "+$stderr) -ForegroundColor Yellow
    if(Test-Path -LiteralPath $stderr){
        Get-Content -LiteralPath $stderr -Tail 25 | ForEach-Object {Write-Host $_}
    }
    throw "Сервер не смог запуститься. Данные не изменены пультом; проверьте журнал."
}
function Stop-Service {
    $proc=Get-ManagedProcess
    if($null -eq $proc){
        if(Test-Port){
            Write-Host "На порту 8000 работает процесс без подтверждённого идентификатора пульта." -ForegroundColor Yellow
            Write-Host "Остановите его в прежнем окне PowerShell сочетанием Ctrl+C."
        } else {
            Write-Host "Управляемый сервер не запущен."
        }
        return
    }
    Write-Host ("Останавливаю только сервер PharmDesk, PID "+$proc.ProcessId) -ForegroundColor Yellow
    Stop-Process -Id ([int]$proc.ProcessId) -ErrorAction Stop
    Start-Sleep -Milliseconds 600
    if(Test-Port){
        Write-Host "Порт 8000 пока занят. Проверьте, нет ли второго сервера." -ForegroundColor Yellow
    } else {
        Write-Host "Сервер остановлен." -ForegroundColor Green
    }
    if(Test-Path -LiteralPath $pidFile){Remove-Item -LiteralPath $pidFile -Force}
}
function Run-Maintenance([string]$Mode) {
    $python=Get-Python
    Push-Location -LiteralPath $root
    try {
        if($Mode -eq "Tests"){
            & $python -X utf8 -m pytest -q
        } else {
            & $python -X utf8 -m app.maintenance $Mode.ToLowerInvariant()
        }
        if($LASTEXITCODE -ne 0){throw "Проверка или команда $Mode завершилась с ошибкой ($LASTEXITCODE)."}
    } finally {Pop-Location}
}
function Show-Status {
    Write-Host ""
    Write-Host "=== Состояние PharmDesk ===" -ForegroundColor Cyan
    Write-Host ("Рабочий каталог: "+$root)
    Write-Host ("Установлена версия: "+(Get-Version))
    $server=Get-Health
    if($null -ne $server){
        Write-Host ("Сайт и API: работают, версия "+$server.version) -ForegroundColor Green
    } elseif(Test-Port) {
        Write-Host "Порт 8000 занят, но /health PharmDesk не отвечает." -ForegroundColor Yellow
    } else {
        Write-Host "Сайт и API: остановлены." -ForegroundColor Yellow
    }
    $proc=Get-ManagedProcess
    if($null -ne $proc){Write-Host ("Управляемый процесс PID: "+$proc.ProcessId)}
    $python=Join-Path $root ".venv\Scripts\python.exe"
    Write-Host ("Python: "+$(if(Test-Path -LiteralPath $python){"установлен"}else{"отсутствует"}))
    Write-Host ("База: "+(Join-Path $root "pharmdesk.sqlite3"))
    if(-not [string]::IsNullOrWhiteSpace($env:DATABASE_URL)){
        Write-Host "ВНИМАНИЕ: DATABASE_URL задан отдельно — используется нестандартная база." -ForegroundColor Yellow
    }
    if(Get-Command git -ErrorAction SilentlyContinue){
        Push-Location -LiteralPath $root
        try {
            $branch=(& git branch --show-current | Out-String).Trim()
            $revision=(& git log -1 --format="%h %s" | Out-String).Trim()
            Write-Host ("Git: "+$branch+" · "+$revision)
            $modified=@(& git status --short --untracked-files=no)
            if($modified.Count -gt 0){Write-Host ("Изменённых файлов кода: "+$modified.Count) -ForegroundColor Yellow}
        } finally {Pop-Location}
    }
}
function Run-Action([string]$Name) {
    switch($Name){
        "Start" {Start-Service}
        "Stop" {Stop-Service}
        "Open" {Open-Browser "/"}
        "OpenMatching" {Open-Browser "/manage/matching"}
        "Status" {Show-Status}
        "Diagnose" {Show-Status; Run-Maintenance "diagnose"}
        "Backup" {Run-Maintenance "backup"}
        "Update" {
            if(Test-Port){throw "Остановите сервер через пункт 9 либо Ctrl+C, прежде чем обновлять."}
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root "update-windows.ps1")
            if($LASTEXITCODE -ne 0){throw "Обновление остановлено; изучите сообщения скрипта."}
        }
        "Tests" {Run-Maintenance "Tests"}
        "Logs" {
            if(-not (Test-Path -LiteralPath $runDir)){throw "Журналов пока нет — сервер ещё не запускался через пульт."}
            Start-Process explorer.exe -ArgumentList ('"'+$runDir+'"') | Out-Null
        }
        "Folder" {Start-Process explorer.exe -ArgumentList ('"'+$root+'"') | Out-Null}
    }
}

if($Action -ne "Menu"){
    try {Run-Action $Action; exit 0}
    catch {Write-Host ("Ошибка: "+$_.Exception.Message) -ForegroundColor Red; exit 1}
}
while($true){
    Write-Host ""
    Write-Host "=============================================" -ForegroundColor Cyan
    Write-Host ("       PHARMDESK v"+(Get-Version)+" — ПУЛЬТ") -ForegroundColor Cyan
    Write-Host "=============================================" -ForegroundColor Cyan
    $h=Get-Health
    if($null -ne $h){Write-Host " Сайт и сервер: РАБОТАЮТ" -ForegroundColor Green}
    elseif(Test-Port){Write-Host " Порт занят, сервер не отвечает" -ForegroundColor Yellow}
    else{Write-Host " Сайт и сервер: остановлены" -ForegroundColor Yellow}
    Write-Host ""
    Write-Host "1. Запустить сайт + сервер и открыть браузер"
    Write-Host "2. Открыть сайт"
    Write-Host "3. Проверить всю систему и базу"
    Write-Host "4. Создать резервную копию SQLite и фотографий"
    Write-Host "5. Обновить программу из GitHub"
    Write-Host "6. Запустить автоматические тесты"
    Write-Host "7. Открыть массовую проверку ГРЛС"
    Write-Host "8. Открыть журналы и папку проекта"
    Write-Host "9. Остановить запущенный через пульт сервер"
    Write-Host "0. Выход (сервер продолжает работать)"
    $choice=Read-Host "Выберите действие"
    if($choice -eq "0"){Write-Host "Выход из пульта."; break}
    try {
        switch($choice){
            "1" {Run-Action "Start"}
            "2" {Run-Action "Open"}
            "3" {Run-Action "Diagnose"}
            "4" {Run-Action "Backup"}
            "5" {Run-Action "Update"}
            "6" {Run-Action "Tests"}
            "7" {Run-Action "OpenMatching"}
            "8" {Run-Action "Logs"; Run-Action "Folder"}
            "9" {Run-Action "Stop"}
            default {Write-Host "Введите число от 0 до 9." -ForegroundColor Yellow}
        }
    } catch {
        Write-Host ("Ошибка: "+$_.Exception.Message) -ForegroundColor Red
    }
    [void](Read-Host "Нажмите Enter для возврата в пульт")
}
