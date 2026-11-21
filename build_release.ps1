# Build HANKO_Thermal_Suite.exe and HANKO_Portable_Setup.zip
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$LhmDll = Join-Path $Root "LibreHardwareMonitorLib.dll"
if (-not (Test-Path $LhmDll)) {
    throw "Required LibreHardwareMonitorLib.dll was not found in $Root"
}

Write-Host "[1/4] Generating Victus V multi-resolution icon..."
python generate_icon.py
if ($LASTEXITCODE -ne 0) { throw "Icon generation failed." }

Write-Host "[2/4] Cleaning previous build/dist outputs..."
if (Test-Path "$Root\build") { Remove-Item "$Root\build" -Recurse -Force }
if (Test-Path "$Root\dist") { Remove-Item "$Root\dist" -Recurse -Force }

Write-Host "[3/4] Compiling standalone HANKO_Thermal_Suite.exe via PyInstaller..."
$DllFiles = Get-ChildItem -Path $Root -Filter "*.dll" -File | Sort-Object Name
$PyInstallerArgs = @(
    "--noconfirm",
    "--clean",
    "--onefile",
    "--windowed",
    "--uac-admin",
    "--noupx",
    "--collect-all", "customtkinter",
    "--collect-all", "pythonnet",
    "--collect-all", "clr_loader",
    "--hidden-import", "clr",
    "--name", "HANKO_Thermal_Suite",
    "--icon", "assets\hanko_victus.ico",
    "--add-data", "assets\hanko_victus.ico;assets"
)
foreach ($dll in $DllFiles) {
    $PyInstallerArgs += "--add-binary"
    $PyInstallerArgs += "$($dll.FullName);."
}
$PyInstallerArgs += "main.py"

& pyinstaller @PyInstallerArgs
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }

Write-Host "[4/4] Packaging HANKO_Portable_Setup.zip..."
$StageDir = "$Root\build\portable_stage"
New-Item -ItemType Directory -Force -Path "$StageDir\assets" | Out-Null

$FilesToCopy = @(
    "main.py",
    "hardware_engine.py",
    "fan_engine.py",
    "clock_tuner.py",
    "stress_worker.py",
    "generate_icon.py",
    "requirements.txt",
    "setup_and_run.bat",
    "README.md",
    "LICENSE"
)
foreach ($f in $FilesToCopy) {
    Copy-Item -Path "$Root\$f" -Destination "$StageDir\$f" -Force
}
foreach ($dll in $DllFiles) {
    Copy-Item -Path $dll.FullName -Destination (Join-Path $StageDir $dll.Name) -Force
}
Copy-Item -Path "$Root\assets\hanko_victus.ico" -Destination "$StageDir\assets\hanko_victus.ico" -Force

$ZipPath = "$Root\dist\HANKO_Portable_Setup.zip"
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Add-Type -AssemblyName System.IO.Compression.FileSystem
[System.IO.Compression.ZipFile]::CreateFromDirectory($StageDir, $ZipPath)

Get-Item "$Root\dist\HANKO_Thermal_Suite.exe", $ZipPath | Format-Table Name, Length, LastWriteTime
