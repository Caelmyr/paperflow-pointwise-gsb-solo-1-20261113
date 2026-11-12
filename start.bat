@echo off
chcp 65001 >nul
set "PAPERFLOW_PORT=8888"
if not "%~1"=="" set "PAPERFLOW_PORT=%~1"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" -Port %PAPERFLOW_PORT%
if errorlevel 1 pause
