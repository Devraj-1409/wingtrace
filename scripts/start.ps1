# Runs WingTrace the way it runs online: one server at http://localhost:8000 serves both the
# website (built and compressed) and the flight data. Rebuilds the website first if it changed.
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot
$url = "http://localhost:8000"
$Host.UI.RawUI.WindowTitle = "WingTrace"

try {
    Invoke-WebRequest "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
    Write-Host "WingTrace is already running (or something else is using port 8000)."
    Write-Host "Close its window first, then start again."
    exit 1
} catch {
    # Nothing there yet: good.
}

. "$PSScriptRoot\setup.ps1"

$frontend = "$root\frontend"
$built = "$frontend\dist\index.html"
$sources = @(Get-ChildItem "$frontend\src", "$frontend\public" -Recurse -File) +
    @(Get-Item "$frontend\index.html", "$frontend\vite.config.ts", "$frontend\package.json")
$newest = ($sources | Sort-Object LastWriteTime | Select-Object -Last 1).LastWriteTime
if (-not (Test-Path $built) -or (Get-Item $built).LastWriteTime -lt $newest) {
    Write-Host "Building the website (a few seconds)..."
    Push-Location $frontend
    npm.cmd run build
    $ok = $LASTEXITCODE -eq 0
    Pop-Location
    if (-not $ok) { throw "The website build failed (see above)." }
}

# Open the browser as soon as the server answers.
Start-Job -ArgumentList $url {
    param($url)
    foreach ($i in 1..60) {
        try {
            Invoke-WebRequest "http://127.0.0.1:8000/api/health" -UseBasicParsing -TimeoutSec 2 | Out-Null
            Start-Process $url
            return
        } catch {
            Start-Sleep -Seconds 1
        }
    }
} | Out-Null

Write-Host ""
Write-Host "WingTrace is starting at $url (your browser opens by itself)."
Write-Host "Keep this window open while you use it; close it to stop WingTrace."
Write-Host ""
$env:FRONTEND_DIST = "$frontend\dist"
Set-Location "$root\backend"
& ".venv\Scripts\python.exe" -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8000
