# HANKO // Thermal Suite v2.0

Authentic industrial Windows hardware telemetry, PawnIO multi-fan RPM array, `nvidia-smi` / `powercfg` direct clock tuner, and multi-core thermal load verification utility.

Modeled after precision instrumentation (`HWiNFO64`, `FanControl`, and `HP Victus`) with a flat matte-charcoal interface (`#0D0F12` base, `#15181E` panels, crisp `1px` `#282D37` borders, `#00D8FF` Telemetry Cyan readouts, `#FF8C00` Thermal Amber clock controls, fixed-width right-aligned `Consolas` metrics) and an explicit **zero-fabrication telemetry policy**.

---

## Core Architecture (v2.0.0)

### 1. Multi-Fan Telemetry Array (`fan_engine.py`)
Scans **Motherboard**, **SuperIO**, **Embedded Controller (EC)**, **Controller**, and **GPU** nodes via `pythonnet` (`clr`) and PawnIO-compatible `LibreHardwareMonitorLib.dll` (`v0.9.6`) for all `SensorType.Fan` channels:
- Dynamically updates the UI header (`DETECTED FANS: <count>`).
- Displays every detected fan channel with its node tag, fixed-width live RPM speed, load percentage (`PWM Control` or `RPM Est.`), and a 20-segment flat progress meter.
- On HP / OEM laptops where BIOS locks Embedded Controller tachometer registers, gracefully displays **`EC TELEMETRY LOCKED BY HP BIOS`** with zero crashes or fabricated RPMs.

### 2. Direct GPU MHz & CPU GHz Clock Tuner (`clock_tuner.py`)
All external clock commands execute silently (`CREATE_NO_WINDOW`) with live vs. target frequency tracking:

| Control | Backing Command & Range | Reset / Stock Behavior |
|---|---|---|
| **GPU Core Clock (MHz)** | `nvidia-smi --lock-gpu-clocks=<mhz>,<mhz>` (`800–1850 MHz`, `15 MHz` step) | `nvidia-smi --reset-gpu-clocks` restores stock VBIOS boost |
| **CPU Max Frequency (GHz)** | `powercfg /setacvalueindex` + `/setdcvalueindex SCHEME_CURRENT SUB_PROCESSOR PROCFREQMAX <mhz>` (`1.80–4.50 GHz`, `0.10 GHz` step) | Sets `PROCFREQMAX` to `0` (`Turbo Auto`) across AC+DC |

### 3. Live Hardware Sensor Tree & Power Policy (`hardware_engine.py`)
Reads real hardware sensors via `psutil`, `nvidia-smi`, Windows CIM/WMI (one persistent hidden PowerShell session), and PawnIO `LibreHardwareMonitorLib.dll`:

| Channel | Backing Source | Zero-Fabrication Behavior |
|---|---|---|
| CPU Package / ACPI Temp | `LibreHardwareMonitor` -> `MSAcpi_ThermalZoneTemperature` -> `ThermalZoneInformation` | Displays `N/A (Locked)` if BIOS blocks thermal zones |
| CPU Core Clock | `Win32_PerfFormattedData_Counters_ProcessorInformation` (`% Processor Performance`) -> `psutil.cpu_freq` | Tracks real dynamic Turbo Boost frequency |
| CPU Load & Peak Thread | `psutil.cpu_percent(percpu=True)` | Real per-logical-core utilization |
| GPU Temp / Clock / VRAM / P-State | `nvidia-smi` CSV telemetry | Parses `[N/A]` safely without crashing |
| GPU Power Draw | `nvidia-smi` (`power.draw`) | Shows `N/A (No PMIC)` on mobile GPUs without PMIC telemetry |
| Thermal & Boost Policies | `powercfg` (`PROCTHROTTLEMAX`, `PERFBOOSTMODE`, `SYSCOOLPOL`) | Applied live to both AC and DC on `SCHEME_CURRENT` |

### 4. Multi-Core Thermal Load Verification (`stress_worker.py`)
- Launches isolated `multiprocessing` floating-point math workers across `25%`, `50%`, `75%`, or `100%` of logical threads so you can physically test clock scaling, temperature delta, and acoustic fan behavior under real load.

---

## Installation & Usage

### Option A — Portable Setup (`setup_and_run.bat`) *(Recommended — Smart App Control Immune)*
1. Download `HANKO_Portable_Setup.zip` from [GitHub Releases](https://github.com/hankobanko391/HANKO-Thermal-Suite/releases/latest).
2. Extract to a folder (contains `LibreHardwareMonitorLib.dll` and runtime dependencies) and run **`setup_and_run.bat`**.
3. The launcher automatically:
   - Requests Administrator UAC privileges (required by `powercfg`, `nvidia-smi` clock locks, and PawnIO sensor reads),
   - Installs `customtkinter`, `psutil`, `pillow`, and `pythonnet` if missing,
   - Creates a **`HANKO Thermal Suite`** Desktop shortcut with the Victus 'V' `.ico` emblem,
   - Starts the GUI cleanly via `pythonw.exe`.

### Option B — Standalone Executable (`HANKO_Thermal_Suite.exe`)
1. Download `HANKO_Thermal_Suite.exe` from [GitHub Releases](https://github.com/hankobanko391/HANKO-Thermal-Suite/releases/latest).
2. Launch as Administrator (manifest includes `--uac-admin`; bundles `LibreHardwareMonitorLib.dll` and `pythonnet`).
3. *Note:* On Windows 11 with **Smart App Control** enabled, unsigned `.exe` downloads may be blocked; use Option A (`setup_and_run.bat`) in that environment.

### Building from Source
```powershell
python -m pip install -r requirements.txt pyinstaller
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_release.ps1
```

---

## Project Layout

- `main.py` — `customtkinter` v2.0 industrial UI, Victus 'V' canvas emblem, segmented fan array meters, and GPU MHz / CPU GHz clock tuner panels
- `fan_engine.py` — `pythonnet` (`clr`) + PawnIO `LibreHardwareMonitorLib.dll` multi-node `SensorType.Fan` scanner with `EC TELEMETRY LOCKED BY HP BIOS` fallback
- `clock_tuner.py` — `nvidia-smi --lock-gpu-clocks` (MHz) and `powercfg PROCFREQMAX` (GHz -> MHz) frequency controller with `CREATE_NO_WINDOW`
- `hardware_engine.py` — CIM/WMI bridge, `nvidia-smi` telemetry, `FanEngine` integration, and `powercfg` policy engine
- `stress_worker.py` — Picklable multi-core floating-point thermal load generator
- `LibreHardwareMonitorLib.dll` — PawnIO-compatible `.NET Framework 4.7.2` hardware telemetry assembly (+ runtime dependencies)
- `generate_icon.py` — Supersampled `1024px` Pillow icon builder (`assets/hanko_victus.ico`)
- `setup_and_run.bat` — Self-elevating Windows launcher & Desktop shortcut installer
- `build_release.ps1` — Reproducible PyInstaller `--onefile --uac-admin` & portable ZIP build script
- `index.html` — Engineering GitHub Pages landing site

## Links

- **GitHub Pages:** https://hankobanko391.github.io/HANKO-Thermal-Suite/
- **Releases:** https://github.com/hankobanko391/HANKO-Thermal-Suite/releases
- **License:** [MIT](LICENSE)

---

## Author & Related Projects by Yassin Haitham (`hankobanko391`)

- **Author:** **Yassin Haitham** ([@hankobanko391](https://github.com/hankobanko391))
- **Portfolio Hub:** https://hankobanko391.github.io/
- **Public Windows Hardware Utilities Ecosystem:**
  - [**HANKO Thermal Suite**](https://github.com/hankobanko391/HANKO-Thermal-Suite) ([Live Site](https://hankobanko391.github.io/HANKO-Thermal-Suite/)) — Industrial Windows hardware telemetry, PawnIO multi-fan RPM array, GPU/CPU clock tuner, and multi-core thermal load verification utility.
  - [**BANKO Performance Monitor**](https://github.com/hankobanko391/BANKO-Performance-Monitor) ([Live Site](https://hankobanko391.github.io/BANKO-Performance-Monitor/)) — Universal Windows 10/11 GPU, CPU, and RAM performance monitor, mini gaming overlay, DWM FPS counter, and one-click Boost System.
  - [**BANKO Safe OC Tuners (BANKO Tuner)**](https://github.com/hankobanko391/BANKO-Safe-OC-Tuners) ([Live Site](https://hankobanko391.github.io/BANKO-Safe-OC-Tuners/)) — Brand-locked HP and Dell laptop GPU & performance tuners with an 82°C automatic thermal cutoff and zero-voltage safety lock.
