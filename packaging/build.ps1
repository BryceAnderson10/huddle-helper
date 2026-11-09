# Build dist\HuddleHelper\ with PyInstaller.
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1            # app + bundled base.en and diarization models
#   powershell -ExecutionPolicy Bypass -File packaging\build.ps1 -NoModels  # app only; models download on first run
param([switch]$NoModels, [string]$Model = "base.en")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$py = Join-Path $root ".venv\Scripts\python.exe"

& $py -m pip install -q -e ".[dev]"
& $py -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\huddle_helper.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
# build\ is PyInstaller's scratch area. Its HuddleHelper.exe has no _internal\ folder
# beside it and fails with "Failed to load Python DLL", so don't leave it lying around.
Remove-Item -Recurse -Force (Join-Path $root "build") -ErrorAction SilentlyContinue

if (-not $NoModels) {
    $env:PYTHONPATH = "src"
    & $py -c "from huddle_helper import models; models.install_all('$Model', True, progress=print)"
    $src = Join-Path $env:LOCALAPPDATA "HuddleHelper\models"
    $dst = Join-Path $root "dist\HuddleHelper\models"
    New-Item -ItemType Directory -Force "$dst\whisper", "$dst\diarization" | Out-Null
    Copy-Item -Recurse -Force "$src\whisper\faster-whisper-$Model" "$dst\whisper\"
    Copy-Item -Recurse -Force "$src\diarization\*" "$dst\diarization\"
}
Write-Host "Built dist\HuddleHelper\HuddleHelper.exe"
