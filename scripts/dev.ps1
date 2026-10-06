# For working on the code: the backend (http://127.0.0.1:8000) and a live-reloading website
# (http://localhost:5173) in two new windows. Close a window to stop that part.
# To just use WingTrace, run start.cmd instead: it loads much faster.
$root = Split-Path $PSScriptRoot
. "$PSScriptRoot\setup.ps1"

Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", "Set-Location '$root\backend'; .venv\Scripts\python.exe -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000"
Start-Process powershell -ArgumentList "-NoExit", "-ExecutionPolicy", "Bypass", "-Command", "Set-Location '$root\frontend'; npm.cmd run dev"
Write-Host "Starting... open http://localhost:5173 in a few seconds."
