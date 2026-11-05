@echo off
rem Self-elevate once so the monitor can also close LibreHardwareMonitor on exit
rem (paths go through env vars so quotes/apostrophes in the path can't break PowerShell)
%SystemRoot%\System32\fltmc.exe >nul 2>&1 || (
  set "SELF=%~f0"
  powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
  exit /b
)
cd /d "%~dp0"
set "ZIP=%~dp0LibreHardwareMonitor.zip"
set "DEST=%~dp0."
rem The zip is published as a GitHub Release asset instead of being kept in git
set "ZIP_URL=https://github.com/lixia-ii/hardware-monitor-ui/releases/latest/download/LibreHardwareMonitor.zip"
if exist "%~dp0LibreHardwareMonitor\LibreHardwareMonitor.exe" goto run_lhm

if not exist "%ZIP%" (
  echo Downloading LibreHardwareMonitor.zip ...
  rem Download to .part first so an interrupted download never looks like a valid zip
  powershell -NoProfile -Command "$ProgressPreference = 'SilentlyContinue'; [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; try { Invoke-WebRequest -Uri $env:ZIP_URL -OutFile ($env:ZIP + '.part') -UseBasicParsing; Move-Item -Force ($env:ZIP + '.part') $env:ZIP } catch { Remove-Item -ErrorAction SilentlyContinue ($env:ZIP + '.part'); exit 1 }"
)
if not exist "%ZIP%" (
  echo.
  echo Download failed. Please download LibreHardwareMonitor.zip manually from
  echo   https://github.com/lixia-ii/hardware-monitor-ui/releases
  echo put it next to start.bat, then run start.bat again.
  pause
  exit /b 1
)
rem Validate the downloaded/cached archive before extraction, including the expected executable entry.
powershell -NoProfile -Command "try { Add-Type -AssemblyName System.IO.Compression.FileSystem; $z = [IO.Compression.ZipFile]::OpenRead($env:ZIP); try { if (-not ($z.Entries | Where-Object { $_.FullName -match '^LibreHardwareMonitor[\\/]LibreHardwareMonitor\.exe$' })) { exit 2 } } finally { $z.Dispose() } } catch { exit 1 }"
if errorlevel 1 (
  echo.
  echo LibreHardwareMonitor.zip is invalid or incomplete and has been deleted.
  echo Run start.bat again to download it again.
  del "%ZIP%" 2>nul
  pause
  exit /b 1
)
echo Extracting LibreHardwareMonitor.zip ...
powershell -NoProfile -Command "Expand-Archive -LiteralPath $env:ZIP -DestinationPath $env:DEST -Force"
if not exist "%~dp0LibreHardwareMonitor\LibreHardwareMonitor.exe" (
  echo.
  echo Extraction failed. Delete LibreHardwareMonitor.zip and run start.bat again.
  pause
  exit /b 1
)

:run_lhm
set OWN=
%SystemRoot%\System32\tasklist.exe /fi "imagename eq LibreHardwareMonitor.exe" | %SystemRoot%\System32\find.exe /i "LibreHardwareMonitor.exe" >nul
if errorlevel 1 (
  start "" /min "%~dp0LibreHardwareMonitor\LibreHardwareMonitor.exe"
  set OWN=--own-libre
)
rem pythonw may be missing when Python was installed with only the py launcher
where pythonw >nul 2>&1
if errorlevel 1 (
  start "" pyw "%~dp0app\monitor.py" %OWN%
) else (
  start "" pythonw "%~dp0app\monitor.py" %OWN%
)
