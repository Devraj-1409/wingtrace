@echo off
rem Starts WingTrace: double-click this file. Your browser opens by itself.
rem Runs scripts\start.ps1 without needing to change Windows' script policy.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start.ps1"
if errorlevel 1 pause
