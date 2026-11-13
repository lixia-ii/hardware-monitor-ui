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
$taskWebAssets = Join-Path (Get-Location).Path 'src/hwmonitor/system/web'
# --onedir: 启动时不必每次把运行时解压到临时目录; --uac-admin: 直接以管理员启动, 免去普通权限进程再提权重启
& ./.build/venv/Scripts/python.exe -m PyInstaller --noconfirm --onedir --uac-admin --windowed --noupx --name HardwareMonitorUI --workpath .build/work --specpath .build/spec --distpath dist --exclude-module setuptools --icon $taskIcon --paths src --add-data ($taskIcon + ':hwmonitor/ui/assets') --add-data ($taskWebAssets + ':hwmonitor/system/web') src/hwmonitor/__main__.py
if ($LASTEXITCODE) { throw 'Build failed' }
$app = 'dist/HardwareMonitorUI'
Remove-Item -LiteralPath dist/HardwareMonitorUI.exe,dist/start.bat,dist/HardwareMonitorUI/start.bat,dist/README.md,dist/LICENSE,dist/licenses,dist/LibreHardwareMonitor -Recurse -Force -ErrorAction SilentlyContinue  # 旧版单文件布局及改名前的启动脚本
Copy-Item -LiteralPath start-windows.bat,README.md,LICENSE -Destination $app
Copy-Item -LiteralPath licenses -Destination $app -Recurse -Force
# 硬件库放在 EXE 旁 (不打进 _internal), 便于按 LibreHardwareMonitor 新版本替换
Copy-Item -LiteralPath vendor/LibreHardwareMonitor -Destination $app -Recurse -Force
Compress-Archive -Path "$app/*" -DestinationPath dist/HardwareMonitorUI-windows-x64.zip -Force
Get-FileHash -Algorithm SHA256 -LiteralPath dist/HardwareMonitorUI-windows-x64.zip
