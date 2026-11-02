@echo off
rem Self-elevate once so the monitor can also close LibreHardwareMonitor on exit
%SystemRoot%\System32\fltmc.exe >nul 2>&1 || (
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
if not exist "%~dp0LibreHardwareMonitor\LibreHardwareMonitor.exe" (
  echo Extracting LibreHardwareMonitor.zip ...
  powershell -NoProfile -Command "Expand-Archive -LiteralPath '%~dp0LibreHardwareMonitor.zip' -DestinationPath '%~dp0' -Force"
)
set OWN=
%SystemRoot%\System32\tasklist.exe /fi "imagename eq LibreHardwareMonitor.exe" | %SystemRoot%\System32\find.exe /i "LibreHardwareMonitor.exe" >nul
if errorlevel 1 (
  start "" /min "%~dp0LibreHardwareMonitor\LibreHardwareMonitor.exe"
  set OWN=--own-libre
  %SystemRoot%\System32\timeout.exe /t 5 /nobreak >nul
)
start "" pythonw "%~dp0app\monitor.py" %OWN%
