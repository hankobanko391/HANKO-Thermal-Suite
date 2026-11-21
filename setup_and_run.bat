@echo off
setlocal EnableExtensions

:: Auto-elevate to Administrator (required for live Windows powercfg thermal policies)
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo [HANKO] Requesting Administrator privileges...
    set "HANKO_BAT=%~f0"
    set "HANKO_DIR=%~dp0"
    powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process -FilePath $env:HANKO_BAT -WorkingDirectory $env:HANKO_DIR -Verb RunAs"
    exit /b 0
)

cd /d "%~dp0"
title HANKO Thermal Suite - Setup & Launcher

:: Verify Python 3 availability
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3 was not found in PATH.
    echo Install Python 3.10+ from https://www.python.org/downloads/ and re-run this script.
    pause
    exit /b 1
)

:: Install missing runtime dependencies
python -c "import customtkinter, psutil, PIL, clr" >nul 2>&1
if %errorlevel% neq 0 (
    echo [HANKO] Installing required dependencies ^(customtkinter, psutil, pillow, pythonnet^)...
    python -m pip install --upgrade pip >nul 2>&1
    python -m pip install -r "%~dp0requirements.txt"
    if %errorlevel% neq 0 (
        echo [ERROR] Failed to install Python dependencies.
        pause
        exit /b 1
    )
)

:: Ensure Victus V icon exists
if not exist "%~dp0assets\hanko_victus.ico" (
    echo [HANKO] Generating Victus V desktop icon...
    python "%~dp0generate_icon.py"
)

:: Create Desktop shortcut with custom Victus V icon (supports OneDrive Desktop redirection)
set "HANKO_ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$desktop = [Environment]::GetFolderPath('Desktop');" ^
  "if ($desktop) {" ^
  "  $ws = New-Object -ComObject WScript.Shell;" ^
  "  $lnk = $ws.CreateShortcut((Join-Path $desktop 'HANKO Thermal Suite.lnk'));" ^
  "  $lnk.TargetPath = (Join-Path $env:HANKO_ROOT 'setup_and_run.bat');" ^
  "  $lnk.WorkingDirectory = $env:HANKO_ROOT.TrimEnd('\');" ^
  "  $lnk.IconLocation = (Join-Path $env:HANKO_ROOT 'assets\hanko_victus.ico') + ',0';" ^
  "  $lnk.Description = 'HANKO Thermal Suite - Industrial Hardware Telemetry & Power Control';" ^
  "  $lnk.Save();" ^
  "}" >nul 2>&1

:: Launch GUI cleanly without leaving a terminal window open (immune to Smart App Control)
where pythonw >nul 2>&1
if %errorlevel% equ 0 (
    start "" pythonw.exe "%~dp0main.py"
) else (
    start "" python.exe "%~dp0main.py"
)
exit /b 0
