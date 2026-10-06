@echo off
rem For working on the code: backend + live-reloading website in two windows (see scripts\dev.ps1).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\dev.ps1"
