# Backwards-compatible alias: only one PharmDesk control center is maintained.
$ErrorActionPreference="Stop"
& (Join-Path $PSScriptRoot "control-windows.ps1") -Action Menu
