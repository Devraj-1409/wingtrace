# First run only: installs what WingTrace needs (used by start.ps1 and dev.ps1).
$root = Split-Path $PSScriptRoot

if (-not (Test-Path "$root\backend\.venv")) {
    Write-Host "First run: creating the Python environment..."
    py -3.12 -m venv "$root\backend\.venv"
    & "$root\backend\.venv\Scripts\python.exe" -m pip install -r "$root\backend\requirements-dev.txt"
}
if (-not (Test-Path "$root\backend\data\reference.sqlite")) {
    Write-Host "First run: downloading routes, airlines and airports..."
    Push-Location "$root\backend"
    & ".venv\Scripts\python.exe" -m app.refdata_build
    Pop-Location
}
if (-not (Test-Path "$root\frontend\node_modules")) {
    Write-Host "First run: installing website dependencies..."
    Push-Location "$root\frontend"
    # npm.cmd rather than npm: the npm.ps1 shim is blocked where Windows disallows scripts.
    npm.cmd install
    Pop-Location
}
