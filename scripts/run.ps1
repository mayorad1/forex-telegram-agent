# Start the Forex Telegram Agent (Windows)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

$Py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    Write-Host "Creating venv..."
    py -3 -m venv .venv
    & $Py -m pip install --upgrade pip
    & $Py -m pip install -r requirements.txt
}

if (-not (Test-Path (Join-Path $Root ".env"))) {
    Write-Host "Copy .env.example to .env and set TELEGRAM_BOT_TOKEN + TELEGRAM_ALLOWED_USERS"
    Copy-Item (Join-Path $Root ".env.example") (Join-Path $Root ".env")
    Write-Host "Edit .env then re-run this script."
    exit 1
}

& $Py -m src.main
