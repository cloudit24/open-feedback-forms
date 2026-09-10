# Open Feedback Forms — feedback receiver + admin panel
# Run:  .\start.ps1
#
# The database connection and admin account aren't set here — they're
# configured from the admin panel on first run (http://127.0.0.1:8080/admin).
# This file only controls network/runtime settings.

# --- settings -------------------------------------------------------

# Port the admin panel listens on.
$env:ADMIN_PORT = "8080"

# Port given to the very first form the admin panel creates. Later forms
# get their own ports too — auto-assigned, or chosen when you create them.
$env:FORM_PORT = "8081"

# Bind address. Leave 127.0.0.1: a tunnel (e.g. Cloudflare Tunnel) reaches
# it there, and nothing on the local network can. Only change this if you
# know why you need to — and never expose it directly to the internet.
$env:HOST = "127.0.0.1"

# Cloudflare Turnstile SECRET key (dash.cloudflare.com > Turnstile).
# Leave empty and bot checking stays off.
$env:TURNSTILE_SECRET = ""

# How many submissions one visitor may send per hour, per form.
$env:RATE_LIMIT = "5"

# --------------------------------------------------------------------

Set-Location $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "Python was not found on this machine." -ForegroundColor Red
    Write-Host "Install it from python.org and tick 'Add python.exe to PATH'."
    exit 1
}

if ($env:TURNSTILE_SECRET -eq "") {
    Write-Host "Note: TURNSTILE_SECRET is empty, so bot checking is switched off." -ForegroundColor Yellow
}

python server.py
