# HANKO // Thermal Suite

Authentic industrial Windows hardware telemetry, `powercfg` thermal policy engine, and multi-core thermal load verification utility.

Modeled after precision instrumentation (`FanControl` and `HWiNFO64`) with a flat matte-charcoal interface (`#111317` base, `#181B20` modular panels, `#00B4D8` / `#38BDF8` Victus steel-cyan accents) and an explicit **zero-fabrication telemetry policy**.

---

## Core Architecture

### 1. Live Hardware Sensor Tree (`hardware_engine.py`)
Reads real hardware sensors via `psutil`, `nvidia-smi`, Windows CIM/WMI (using one persistent low-overhead hidden PowerShell session), and optional `LibreHardwareMonitor` / `OpenHardwareMonitor` WMI namespaces if active:

| Channel | Backing Source | Zero-Fabrication Behavior |
|---|---|---|
| CPU Package / ACPI Temp | `LibreHardwareMonitor` -> `MSAcpi_ThermalZoneTemperature` -> `ThermalZoneInformation` | Displays `N/A (Sensor Locked)` if BIOS blocks thermal zones |
| CPU Core Clock | `Win32_PerfFormattedData_Counters_ProcessorInformation` (`% Processor Performance`) -> `psutil.cpu_freq` | Tracks real dynamic Turbo Boost frequency |
| CPU Load & Peak Thread | `psutil.cpu_percent(percpu=True)` | Real per-logical-core utilization |
| GPU Temp / Clock / VRAM / P-State | `nvidia-smi` CSV telemetry | Parses `[N/A]` safely without crashing |
| GPU Power Draw | `nvidia-smi` (`power.draw`) | Shows `N/A (Not Exposed)` on mobile GPUs without PMIC telemetry |
| Fan RPM / Duty | `LibreHardwareMonitor` / `Win32_Fan` / `nvidia-smi` / `nvml.dll` | Shows `N/A (OEM EC Locked)` on locked laptops — never invents RPM values |

### 2. Direct Windows Thermal & Power Policy Controls
Live AC+DC execution via `powercfg` on `SCHEME_CURRENT` (`SUB_PROCESSOR = 54533251-82be-4824-96c1-47b60b740d00`):

| Control | `powercfg` Alias & GUID | Supported Indices |
|---|---|---|
| Maximum Processor State | `PROCTHROTTLEMAX` (`bc5038f7-23e0-4960-96da-33abaf5935ec`) | `50%` – `100%` (live slider) |
| Processor Boost Mode | `PERFBOOSTMODE` (`be337238-0d82-4146-a960-4f3749d470c7`) | `0` = Disabled, `3` = Efficient Enabled, `2` = Aggressive |
| System Cooling Policy | `SYSCOOLPOL` (`94d3a615-a899-4ac5-ae2b-e4d8f634367f`) | `0` = Passive (Throttle First), `1` = Active (Fan Ramp First) |

### 3. PWM Fan Duty & Piecewise Linear Fan Curve
- Automatically probes `nvml.dll` (`nvmlDeviceGetNumFans`, `nvmlDeviceGetFanSpeed_v2`, `nvmlDeviceSetFanControlPolicy`, `nvmlDeviceSetFanSpeed_v2`) at startup.
- **Unlocked Desktop GPUs:** Enables direct manual duty slider (`0%–100%`) and interactive 5-node piecewise linear curve editor (`30°C–100°C`).
- **Locked OEM Laptops (HP Pavilion / Victus / Envy EC):** Cleanly locks controls and displays `Locked by OEM BIOS EC` rather than simulating fake fan adjustments.

### 4. Multi-Core Thermal Load Test (`stress_worker.py`)
- Launches isolated `multiprocessing` floating-point math workers across `25%`, `50%`, `75%`, or `100%` of logical threads so you can physically test clock scaling, temperature delta, and acoustic fan behavior across power profiles.

---

## Installation & Usage

### Option A — Portable Setup (`setup_and_run.bat`) *(Recommended — Smart App Control Immune)*
1. Download `HANKO_Portable_Setup.zip` from [GitHub Releases](https://github.com/hankobanko391/HANKO-Thermal-Suite/releases).
2. Extract to a folder and run **`setup_and_run.bat`**.
3. The launcher automatically:
   - Requests Administrator UAC privileges (required by `powercfg`),
   - Installs `customtkinter`, `psutil`, and `pillow` if missing,
   - Creates a **`HANKO Thermal Suite`** Desktop shortcut with the Victus 'V' `.ico` emblem,
   - Starts the GUI cleanly via `pythonw.exe`.

### Option B — Standalone Executable (`HANKO_Thermal_Suite.exe`)
1. Download `HANKO_Thermal_Suite.exe` from [GitHub Releases](https://github.com/hankobanko391/HANKO-Thermal-Suite/releases).
2. Launch as Administrator (manifest includes `--uac-admin`).
3. *Note:* On Windows 11 with **Smart App Control** enabled, unsigned `.exe` downloads may be blocked; use Option A (`setup_and_run.bat`) in that environment.

### Building from Source
```powershell
python -m pip install -r requirements.txt pyinstaller
.\build_release.ps1
```

---

## Project Layout

- `main.py` — `customtkinter` two-column industrial UI, Victus 'V' canvas emblem, and interactive fan curve editor
- `hardware_engine.py` — CIM/WMI bridge, `nvidia-smi` / `nvml.dll` telemetry & PWM probe, `powercfg` policy engine
- `stress_worker.py` — Picklable multi-core floating-point thermal load generator
- `generate_icon.py` — Supersampled `1024px` Pillow icon builder (`assets/hanko_victus.ico`)
- `setup_and_run.bat` — Self-elevating Windows launcher & Desktop shortcut installer
- `build_release.ps1` — Reproducible PyInstaller `--onefile` & portable ZIP build script
- `index.html` — Engineering GitHub Pages landing site

## Links

- **GitHub Pages:** https://hankobanko391.github.io/HANKO-Thermal-Suite/
- **Releases:** https://github.com/hankobanko391/HANKO-Thermal-Suite/releases
- **License:** [MIT](LICENSE)
