@echo off
setlocal
cd /d "%~dp0"
if not exist "packaging\windows\build.ps1" (
  echo Extract the complete Build Kit and run BUILD_WINDOWS.cmd at its root.
  pause
  exit /b 2
)
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\windows\build.ps1"
if errorlevel 1 (
  echo Build failed. See the error above. No portable release was produced.
  pause
  exit /b 1
)
echo Build finished. See dist\PlanktonTime-Windows-x64.zip
pause
