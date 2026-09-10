# Builds the Open Feedback Forms .msi from scratch.
#
# One-time setup this needs on the build machine (not the machine that
# installs the .msi -- that one needs nothing):
#   winget install Microsoft.DotNet.SDK.8
#   dotnet tool install --global wix
#   pip install -r ..\requirements.txt
#
# Run from anywhere; it figures out the project root from its own location.
$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

Write-Host "== Open Feedback Forms - MSI build ==" -ForegroundColor Cyan

# Make sure `wix` is on PATH even in a fresh shell that hasn't picked up
# the dotnet tools folder yet.
$dotnetTools = "$env:USERPROFILE\.dotnet\tools"
if ($env:PATH -notlike "*$dotnetTools*") { $env:PATH += ";$dotnetTools" }

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python was not found. Install Python 3 and make sure it's on PATH."
}
if (-not (Get-Command wix -ErrorAction SilentlyContinue)) {
    throw "The WiX CLI was not found. Run: dotnet tool install --global wix  (needs the .NET SDK, not just the runtime)"
}

Write-Host "`n[1/3] Bundling the app with PyInstaller..." -ForegroundColor Yellow
if (Test-Path "$root\build") { Remove-Item "$root\build" -Recurse -Force }
if (Test-Path "$root\dist")  { Remove-Item "$root\dist"  -Recurse -Force }

python -m PyInstaller --noconfirm --name OpenFeedbackForms --onedir `
    --add-data "public;public" --add-data "admin;admin" `
    --console server.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

$distDir = "$root\dist\OpenFeedbackForms"
if (-not (Test-Path "$distDir\OpenFeedbackForms.exe")) {
    throw "PyInstaller did not produce the expected exe at $distDir"
}

Write-Host "`n[2/3] Compiling the installer with WiX..." -ForegroundColor Yellow
$outDir = "$root\packaging\out"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$msiPath = "$outDir\OpenFeedbackForms.msi"

wix build "$root\packaging\app.wxs" `
    -d "DistDir=$distDir" `
    -arch x64 `
    -out $msiPath
if ($LASTEXITCODE -ne 0) { throw "WiX build failed." }

Write-Host "`n[3/3] Done." -ForegroundColor Green
Write-Host "Installer: $msiPath"
Get-Item $msiPath | Select-Object Name, @{n="Size (MB)"; e={[math]::Round($_.Length/1MB,1)}}
