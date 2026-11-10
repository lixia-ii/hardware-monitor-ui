$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath '.build/venv/Scripts/python.exe')) {
    python -m venv .build/venv
    if ($LASTEXITCODE) { throw 'Failed to create build environment' }
}
& ./.build/venv/Scripts/python.exe -m pip install -e ".[build]"
if ($LASTEXITCODE) { throw 'Failed to install build dependencies' }
$env:PYINSTALLER_CONFIG_DIR = Join-Path (Get-Location).Path '.build/pyinstaller-cache'
New-Item -ItemType Directory -Path .build/spec -Force | Out-Null
$taskIcon = Join-Path (Get-Location).Path 'src/hwmonitor/ui/assets/app.ico'
& ./.build/venv/Scripts/python.exe -m PyInstaller --noconfirm --onefile --windowed --noupx --name HardwareMonitorUI --workpath .build/work --specpath .build/spec --distpath dist --exclude-module setuptools --icon $taskIcon --paths src --add-data ($taskIcon + ':hwmonitor/ui/assets') src/hwmonitor/__main__.py
if ($LASTEXITCODE) { throw 'Build failed' }
Copy-Item -LiteralPath start.bat,README.md,LICENSE -Destination dist
Copy-Item -LiteralPath licenses -Destination dist -Recurse -Force
# 硬件库放在 EXE 旁 (不打进单文件), 便于按 LibreHardwareMonitor 新版本替换
Remove-Item -LiteralPath dist/LibreHardwareMonitor -Recurse -Force -ErrorAction SilentlyContinue
Copy-Item -LiteralPath vendor/LibreHardwareMonitor -Destination dist -Recurse -Force
Compress-Archive -LiteralPath dist/HardwareMonitorUI.exe,dist/start.bat,dist/README.md,dist/LICENSE,dist/licenses,dist/LibreHardwareMonitor -DestinationPath dist/HardwareMonitorUI-windows-x64.zip -Force
Get-FileHash -Algorithm SHA256 -LiteralPath dist/HardwareMonitorUI.exe
