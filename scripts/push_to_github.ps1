# Push forex-telegram-agent to your GitHub account
# Run in PowerShell (normal window, not as background):
#   cd C:\Users\HP\forex-telegram-agent
#   .\scripts\push_to_github.ps1

$ErrorActionPreference = "Stop"
$env:Path = "C:\Program Files\Git\cmd;C:\Program Files\GitHub CLI;" + $env:Path
Set-Location "C:\Users\HP\forex-telegram-agent"

Write-Host "=== GitHub auth ===" -ForegroundColor Cyan
$auth = gh auth status 2>&1 | Out-String
if ($auth -notmatch "Logged in to github.com") {
    Write-Host "A browser window will open. Complete GitHub login, then return here."
    gh auth login -h github.com -p https -w
}

$user = (gh api user --jq .login).Trim()
Write-Host "Logged in as: $user" -ForegroundColor Green

git config user.name $user
git config user.email "$user@users.noreply.github.com"
git branch -M main

# Create repo if missing
$repo = "$user/forex-telegram-agent"
$view = gh repo view $repo 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "Creating public repo $repo ..."
    gh repo create forex-telegram-agent --public --description "Telegram forex trading agent linked to MetaTrader 5 / Exness"
}

# Set remote and push
git remote remove origin 2>$null
git remote add origin "https://github.com/$repo.git"
Write-Host "Pushing main..."
git push -u origin main

Write-Host ""
Write-Host "DONE: https://github.com/$repo" -ForegroundColor Green
gh repo view $repo --web
