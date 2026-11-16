# PharmDesk Desktop workspace organizer. Windows PowerShell 5.1.
# Default is preview-only. Apply requires explicit interactive confirmation.
[CmdletBinding()]
param([switch]$Apply,[string]$DesktopPath="",[string]$Approval="")
$ErrorActionPreference="Stop"
Set-StrictMode -Version Latest

function Test-LocalPort {
    $client=New-Object System.Net.Sockets.TcpClient
    $connect=$null
    try {
        $connect=$client.BeginConnect("127.0.0.1",8000,$null,$null)
        if (-not $connect.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($connect)
        return $true
    } catch { return $false }
    finally {
        if ($null -ne $connect) { $connect.AsyncWaitHandle.Close() }
        $client.Close()
    }
}

function Assert-RealDirectory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item=Get-Item -LiteralPath $Path -Force
    if (-not $item.PSIsContainer) { throw "Ожидалась папка: $Path" }
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Папка является ссылкой или точкой подключения: $Path"
    }
}

function Show-Directory([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Write-Host "Не найдена: $Path" -ForegroundColor DarkGray
        return
    }
    Assert-RealDirectory $Path
    Write-Host "Найдена: $Path" -ForegroundColor Cyan
    if (Test-Path -LiteralPath (Join-Path $Path ".git")) { Write-Host "  Git-репозиторий: да" }
    foreach($name in @("pharmdesk.sqlite3","private_data","backups",".venv")){
        $entry=Join-Path $Path $name
        if(Test-Path -LiteralPath $entry){
            $item=Get-Item -LiteralPath $entry -Force
            if ($item.PSIsContainer) {
                Write-Host ("  {0}: папка, изменена {1}" -f $name,$item.LastWriteTime)
            } else {
                Write-Host ("  {0}: {1:N1} МБ, изменён {2}" -f $name,($item.Length/1MB),$item.LastWriteTime)
            }
        }
    }
}
try {
    if ([string]::IsNullOrWhiteSpace($DesktopPath)) {
        $desktop=Join-Path $env:USERPROFILE "Desktop"
        if (-not (Test-Path -LiteralPath $desktop)) {
            $desktop=[Environment]::GetFolderPath("Desktop")
        }
    } else { $desktop=$DesktopPath }
    $desktop=(Resolve-Path -LiteralPath $desktop).Path
    $project=Join-Path $desktop "PharmDesk_Project"
    $liveSource=Join-Path $desktop "PharmDesk-git"
    $liveDest=Join-Path $project "PharmDesk-git"
    $legacyDir=Join-Path $project "_Old_versions"
    $oldNames=@("PharmDesk-main","PharmDesk-git-before-fix")
    Write-Host ""
    Write-Host "=== PharmDesk: безопасная организация ===" -ForegroundColor Cyan
    Show-Directory $liveSource
    Show-Directory $liveDest
    foreach($name in $oldNames){
        Show-Directory (Join-Path $desktop $name)
        Show-Directory (Join-Path $legacyDir $name)
    }
    Write-Host ""
    Write-Host "Планируемая структура:"
    Write-Host "  $project"
    Write-Host "    PharmDesk-Control.bat — пульт"
    Write-Host "    PharmDesk-git — действующая программа и её SQLite"
    Write-Host "    _Old_versions — прежние копии целиком, без удаления"
    if(-not $Apply){
        Write-Host ""
        Write-Host "Только просмотр. Никакие файлы не изменены." -ForegroundColor Green
        Write-Host "После проверки запустите снова с параметром -Apply."
        exit 0
    }
    if(Test-LocalPort){ throw "Порт 8000 занят. Сначала остановите PharmDesk." }
    Assert-RealDirectory $project
    Assert-RealDirectory $legacyDir
    $srcExists=Test-Path -LiteralPath $liveSource
    $dstExists=Test-Path -LiteralPath $liveDest
    if($srcExists -and $dstExists){throw "Обнаружены две рабочие копии. Невозможно безопасно выбрать одну."}
    if(-not $srcExists -and -not $dstExists){throw "Не найден PharmDesk-git ни на столе, ни в рабочем пространстве."}
    $live=if($srcExists){$liveSource}else{$liveDest}
    Assert-RealDirectory $live
    if(-not (Test-Path -LiteralPath (Join-Path $live ".git"))){
        throw "У выбранной рабочей копии отсутствует .git. Ничего не перенесено."
    }
    if(-not (Test-Path -LiteralPath (Join-Path $live "app\database.py"))){
        throw "Отсутствует исходный код проекта. Ничего не перенесено."
    }
    foreach($name in $oldNames){
        if((Test-Path -LiteralPath (Join-Path $desktop $name)) -and
           (Test-Path -LiteralPath (Join-Path $legacyDir $name))){
            throw "Архивная папка $name уже существует, поэтому перенос остановлен."
        }
    }
    Write-Host ""
    Write-Host "Будут ПЕРЕМЕЩЕНЫ, а не удалены, существующие папки." -ForegroundColor Yellow
    Write-Host "Убедитесь, что окна Python/редакторы и сервер PharmDesk закрыты."
    Write-Host "Подтверждение: ПЕРЕНЕСТИ или MOVE (если консоль не поддерживает русские символы)."
    $confirm=if([string]::IsNullOrWhiteSpace($Approval)){
        Read-Host "Напишите ПЕРЕНЕСТИ для подтверждения"
    } else { $Approval }
    if($confirm -cne "ПЕРЕНЕСТИ" -and $confirm -cne "MOVE"){
        Write-Host "Отменено. Ничего не менялось."
        exit 0
    }
    if($srcExists -and (Test-Path -LiteralPath (Join-Path $liveSource "pharmdesk.sqlite3"))){
        $python=Join-Path $liveSource ".venv\Scripts\python.exe"
        if(-not (Test-Path -LiteralPath $python)){
            throw "Нужен Python .venv для согласованной резервной копии SQLite. Перенос отменён."
        }
        Write-Host "Создание проверенной резервной копии..." -ForegroundColor Cyan
        Push-Location -LiteralPath $liveSource
        try {
            & $python -X utf8 -m app.maintenance backup
            if($LASTEXITCODE -ne 0){throw "Резервная копия не создана. Перенос отменён."}
        } finally { Pop-Location }
    }
    if(-not (Test-Path -LiteralPath $project)){
        New-Item -ItemType Directory -Path $project | Out-Null
    }
    Set-Location -LiteralPath $desktop
    if($srcExists){
        Write-Host "Перенос действующей программы..." -ForegroundColor Cyan
        Move-Item -LiteralPath $liveSource -Destination $liveDest -ErrorAction Stop
    }
    $toArchive=@($oldNames | Where-Object { Test-Path -LiteralPath (Join-Path $desktop $_) })
    if($toArchive.Count -gt 0 -and -not (Test-Path -LiteralPath $legacyDir)){
        New-Item -ItemType Directory -Path $legacyDir | Out-Null
    }
    foreach($name in $toArchive){
        Write-Host "Сохранение старой версии в архиве: $name" -ForegroundColor Cyan
        Move-Item -LiteralPath (Join-Path $desktop $name) -Destination (Join-Path $legacyDir $name) -ErrorAction Stop
    }
    $launcher=Join-Path $project "PharmDesk-Control.bat"
    if(-not (Test-Path -LiteralPath $launcher)){
        $batch='@echo off'+[Environment]::NewLine+
               'call "%~dp0PharmDesk-git\control-windows.bat"'+[Environment]::NewLine
        [System.IO.File]::WriteAllText($launcher,$batch,[System.Text.Encoding]::ASCII)
    }
    $readme=Join-Path $project "README-Workspace.txt"
    if(-not (Test-Path -LiteralPath $readme)){
        $note=@'
PharmDesk — единая папка проекта

PharmDesk-Control.bat       = единый пульт (сайт/сервер, диагностика, обновления, резервные копии)
PharmDesk-git               = ТОЛЬКО ЭТА папка содержит действующую программу и рабочую SQLite
PharmDesk-git\backups       = сохранённые копии базы и фотографий
PharmDesk-git\private_data  = локальные фотографии
_Old_versions               = старые папки, оставлены для проверки, НЕ УДАЛЯТЬ

Фронтенд обслуживается тем же FastAPI-сервером, что и бэкенд: http://127.0.0.1:8000.
После переноса проверьте диагностику и резервное копирование через пульт.
Приватные файлы и архивы не размещайте в открытом GitHub.
'@
        [System.IO.File]::WriteAllText($readme,$note,(New-Object System.Text.UTF8Encoding($true)))
    }
    Write-Host ""
    Write-Host "ГОТОВО: $project" -ForegroundColor Green
    Write-Host "Открывайте только: $launcher"
    Write-Host "Старые папки сохранены без очистки."
} catch {
    Write-Host ("ПЕРЕНОС ОСТАНОВЛЕН: "+$_.Exception.Message) -ForegroundColor Red
    Write-Host "Ничего не удалялось, базы не объединялись и не заменялись."
    exit 1
}
