$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath '.build/venv/Scripts/python.exe')) {
    python -m venv .build/venv
    if ($LASTEXITCODE) { throw 'Failed to create build environment' }
}
& ./.build/venv/Scripts/python.exe -m pip install -r scripts/requirements-build.txt
if ($LASTEXITCODE) { throw 'Failed to install build dependencies' }
$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location).Path '.build/pyinstaller-cache'
New-Item -ItemType Directory -Path .build/spec -Force | Out-Null
$taskIcon = Join-Path (Get-Location).Path 'app/app.ico'
& ./.build/venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --windowed --noupx --name HardwareMonitorUI --workpath .build/work --specpath .build/spec --distpath dist --icon $taskIcon --add-data ($taskIcon + ':.') app/monitor.py
if ($LASTEXITCODE) { throw 'Build failed' }
Copy-Item -LiteralPath start.bat,README.md,LICENSE -Destination dist
Copy-Item -LiteralPath THIRD_PARTY -Destination dist -Recurse -Force
Compress-Archive -LiteralPath dist/HardwareMonitorUI.exe,dist/start.bat,dist/README.md,dist/LICENSE,dist/THIRD_PARTY -DestinationPath dist/HardwareMonitorUI-windows-x64.zip -Force
Get-FileHash -Algorithm SHA256 -LiteralPath dist/HardwareMonitorUI.exe
