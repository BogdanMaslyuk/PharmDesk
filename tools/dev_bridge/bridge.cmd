@echo off
setlocal
pushd "%~dp0\..\.."
py -V:3.14 -m tools.dev_bridge %*
set "BRIDGE_EXIT=%ERRORLEVEL%"
popd
exit /b %BRIDGE_EXIT%
