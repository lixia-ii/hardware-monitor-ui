@echo off
cd /d "%~dp0"
rem Hardware sensors are read in-process; the UI asks for administrator rights itself.
if exist "%~dp0HardwareMonitorUI.exe" (
  start "" "%~dp0HardwareMonitorUI.exe"
  exit /b
)
where pythonw >nul 2>&1
if not errorlevel 1 (
  start "" pythonw "%~dp0src\hwmonitor\__main__.py"
  exit /b
)
where pyw >nul 2>&1
if not errorlevel 1 (
  start "" pyw "%~dp0src\hwmonitor\__main__.py"
  exit /b
)
echo Python was not found. Install Python 3 or download the portable EXE release.
pause
exit /b 1
