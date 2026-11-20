"""Genuine Windows hardware telemetry, powercfg policy control, and NVML PWM engine.

Zero-fabrication contract:
- Never invents, interpolates, or hardcodes sensor readings when hardware or BIOS EC
  locks access. Missing telemetry returns None so the UI can display honest states
  such as 'N/A (OEM EC Locked)'.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import psutil

CREATE_NO_WINDOW = 0x08000000

SUB_PROCESSOR = "54533251-82be-4824-96c1-47b60b740d00"
GUID_PROCTHROTTLEMAX = "bc5038f7-23e0-4960-96da-33abaf5935ec"
GUID_PERFBOOSTMODE = "be337238-0d82-4146-a960-4f3749d470c7"
GUID_SYSCOOLPOL = "94d3a615-a899-4ac5-ae2b-e4d8f634367f"

BOOST_LABELS: dict[int, str] = {
    0: "Disabled (Cool/Quiet)",
    3: "Efficient Enabled",
    2: "Aggressive (Full Boost)",
}
BOOST_MODES_BY_LABEL: dict[str, int] = {v: k for k, v in BOOST_LABELS.items()}

COOLING_LABELS: dict[int, str] = {
    0: "Passive (Throttle First)",
    1: "Active (Fan Ramp First)",
}
COOLING_POLICIES_BY_LABEL: dict[str, int] = {v: k for k, v in COOLING_LABELS.items()}

DEFAULT_FAN_CURVE: list[tuple[float, float]] = [
    (40.0, 25.0),
    (55.0, 40.0),
    (70.0, 65.0),
    (82.0, 85.0),
    (92.0, 100.0),
]

_NA_TOKENS = {
    "",
    "n/a",
    "[n/a]",
    "not supported",
    "[not supported]",
    "unknown error",
    "[unknown error]",
    "none",
    "null",
}


def is_admin() -> bool:
    """Return True if the current process holds Administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _parse_float(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text.lower() in _NA_TOKENS:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def interpolate_fan_curve(temp_c: float, points: list[tuple[float, float]]) -> float:
    """Evaluate a piecewise linear fan curve (temperature C -> PWM duty %)."""
    if not points:
        return 50.0
    ordered = sorted((float(t), max(0.0, min(100.0, float(d)))) for t, d in points)
    if temp_c <= ordered[0][0]:
        return ordered[0][1]
    if temp_c >= ordered[-1][0]:
        return ordered[-1][1]
    for (t0, d0), (t1, d1) in zip(ordered, ordered[1:]):
        if t0 <= temp_c <= t1:
            if t1 <= t0:
                return d1
            ratio = (temp_c - t0) / (t1 - t0)
            return round(d0 + ratio * (d1 - d0), 1)
    return ordered[-1][1]


@dataclass
class StaticHardwareInfo:
    cpu_name: str = "Unknown Processor"
    cpu_cores_physical: int = 0
    cpu_cores_logical: int = 0
    cpu_base_mhz: float = 0.0
    gpu_name: str = "No Discrete GPU Detected"
    gpu_driver: str = "N/A"
    gpu_vram_total_mb: float | None = None
    nvidia_available: bool = False
    os_version: str = "Windows"
    is_admin: bool = False
    active_scheme_guid: str = ""
    active_scheme_name: str = "Unknown"
    pwm_control_available: bool = False
    pwm_backend: str = "None"
    pwm_lock_reason: str = "Locked by OEM BIOS EC"


@dataclass
class FanReading:
    name: str
    rpm: float | None = None
    duty_pct: float | None = None
    source: str = ""
    status: str = ""


@dataclass
class SensorSnapshot:
    timestamp: float = field(default_factory=time.time)
    cpu_temp_c: float | None = None
    cpu_temp_source: str = "Unavailable"
    cpu_clock_ghz: float | None = None
    cpu_clock_source: str = "psutil"
    cpu_load_pct: float = 0.0
    cpu_peak_core_pct: float = 0.0
    gpu_temp_c: float | None = None
    gpu_clock_mhz: float | None = None
    gpu_max_clock_mhz: float | None = None
    gpu_power_w: float | None = None
    gpu_load_pct: float | None = None
    gpu_vram_used_mb: float | None = None
    gpu_vram_total_mb: float | None = None
    gpu_pstate: str | None = None
    fans: list[FanReading] = field(default_factory=list)


@dataclass
class PowerPolicyState:
    scheme_guid: str = ""
    scheme_name: str = "Unknown"
    proc_throttle_max: int | None = None
    perf_boost_mode: int | None = None
    sys_cool_pol: int | None = None


class MinMaxTracker:
    """Tracks session minimum and maximum values for numeric telemetry channels."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._mins: dict[str, float] = {}
        self._maxs: dict[str, float] = {}

    def update(self, key: str, value: float | None) -> None:
        if value is None:
            return
        with self._lock:
            if key not in self._mins or value < self._mins[key]:
                self._mins[key] = value
            if key not in self._maxs or value > self._maxs[key]:
                self._maxs[key] = value

    def get(self, key: str) -> tuple[float | None, float | None]:
        with self._lock:
            return self._mins.get(key), self._maxs.get(key)

    def reset(self) -> None:
        with self._lock:
            self._mins.clear()
            self._maxs.clear()


_PS_BRIDGE_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
$ProgressPreference = 'SilentlyContinue'

$script:LhmNamespace = $null
foreach ($ns in @('root\LibreHardwareMonitor', 'root\OpenHardwareMonitor')) {
    $probe = Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($null -ne $probe) {
        $script:LhmNamespace = $ns
        break
    }
}

while ($true) {
    $cmd = [Console]::In.ReadLine()
    if ($null -eq $cmd -or $cmd -eq 'EXIT') { break }

    if ($cmd -eq 'STATIC') {
        $cpu = $null
        $gpu = $null
        $osInfo = $null
        try {
            $cpu = Get-CimInstance -ClassName Win32_Processor -ErrorAction Stop |
                Select-Object -First 1 Name, MaxClockSpeed, CurrentClockSpeed, NumberOfCores, NumberOfLogicalProcessors
        } catch {}
        try {
            $gpu = @(Get-CimInstance -ClassName Win32_VideoController -ErrorAction Stop |
                Select-Object Name, AdapterRAM, DriverVersion)
        } catch {}
        try {
            $osInfo = Get-CimInstance -ClassName Win32_OperatingSystem -ErrorAction Stop |
                Select-Object -First 1 Caption, Version, BuildNumber
        } catch {}

        $out = [ordered]@{
            cpu = $cpu
            gpus = $gpu
            os = $osInfo
            lhm_ns = $script:LhmNamespace
        }
        [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress -Depth 4))
        [Console]::Out.Flush()
    }
    elseif ($cmd -eq 'POLL') {
        $acpiTemps = @()
        $perfTemps = @()
        $procPerf = $null
        $winFans = @()
        $lhmSensors = @()

        try {
            $acpiTemps = @(Get-CimInstance -Namespace root\WMI -ClassName MSAcpi_ThermalZoneTemperature -ErrorAction Stop |
                Select-Object InstanceName, CurrentTemperature)
        } catch {}

        if ($acpiTemps.Count -eq 0) {
            try {
                $perfTemps = @(Get-CimInstance -Namespace root\CIMV2 -ClassName Win32_PerfFormattedData_Counters_ThermalZoneInformation -ErrorAction Stop |
                    Select-Object Name, HighPrecisionTemperature, Temperature)
            } catch {}
        }

        try {
            $procPerf = Get-CimInstance -Namespace root\CIMV2 -ClassName Win32_PerfFormattedData_Counters_ProcessorInformation -Filter "Name='_Total'" -ErrorAction Stop |
                Select-Object -First 1 PercentProcessorPerformance, ProcessorFrequency
        } catch {}

        try {
            $winFans = @(Get-CimInstance -Namespace root\CIMV2 -ClassName Win32_Fan -ErrorAction Stop |
                Select-Object Name, DeviceID, DesiredSpeed, Status)
        } catch {}

        if ($null -ne $script:LhmNamespace) {
            try {
                $lhmSensors = @(Get-CimInstance -Namespace $script:LhmNamespace -ClassName Sensor -ErrorAction Stop |
                    Where-Object { $_.SensorType -in @('Temperature', 'Fan', 'Control') } |
                    Select-Object Name, SensorType, Value, Identifier)
            } catch {}
        }

        $out = [ordered]@{
            acpi_temps = $acpiTemps
            perf_temps = $perfTemps
            proc_perf = $procPerf
            win_fans = $winFans
            lhm_sensors = $lhmSensors
        }
        [Console]::Out.WriteLine(($out | ConvertTo-Json -Compress -Depth 4))
        [Console]::Out.Flush()
    }
}
"""


class _CimBridge:
    """Single persistent hidden PowerShell CIM session with hard query timeouts."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._line_queue: queue.Queue[str] = queue.Queue()
        self._reader_thread: threading.Thread | None = None
        self._closed = False
        self._encoded_script = base64.b64encode(_PS_BRIDGE_SCRIPT.encode("utf-16-le")).decode("ascii")

    def _stdout_reader(self, proc: subprocess.Popen[str], out_queue: queue.Queue[str]) -> None:
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                out_queue.put(line)
        except Exception:
            pass

    def _ensure_started(self) -> bool:
        if self._closed:
            return False
        if self._proc is not None and self._proc.poll() is None:
            return True
        ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
        if not ps_exe:
            return False
        try:
            self._line_queue = queue.Queue()
            proc = subprocess.Popen(
                [
                    ps_exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-EncodedCommand",
                    self._encoded_script,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=CREATE_NO_WINDOW,
            )
            if proc.stdin is None or proc.stdout is None:
                proc.kill()
                return False
            self._proc = proc
            self._reader_thread = threading.Thread(
                target=self._stdout_reader,
                args=(proc, self._line_queue),
                name="HANKOCimStdoutReader",
                daemon=True,
            )
            self._reader_thread.start()
            return True
        except Exception:
            self._proc = None
            return False

    def query(self, command: str, timeout: float = 2.5) -> dict[str, Any]:
        with self._lock:
            if not self._ensure_started() or self._proc is None:
                return {}
            assert self._proc.stdin is not None
            # Drain any stale output lines before issuing a fresh command.
            while not self._line_queue.empty():
                try:
                    self._line_queue.get_nowait()
                except queue.Empty:
                    break
            try:
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.flush()
                line = self._line_queue.get(timeout=timeout)
                if not line:
                    return {}
                data = json.loads(line)
                return data if isinstance(data, dict) else {}
            except (queue.Empty, Exception):
                self._restart_locked()
                return {}

    def _restart_locked(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=0.5)
            except Exception:
                pass
            self._proc = None
            self._reader_thread = None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._proc is not None:
                try:
                    if self._proc.stdin is not None:
                        self._proc.stdin.write("EXIT\n")
                        self._proc.stdin.flush()
                    self._proc.wait(timeout=0.8)
                except Exception:
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=0.5)
                    except Exception:
                        pass
                self._proc = None
                self._reader_thread = None


class _NvmlFanController:
    """Direct NVML PWM probe & fan speed writer (only enabled on unlocked desktop GPUs)."""

    def __init__(self) -> None:
        self._nvml: Any = None
        self._handle: wt.HANDLE | None = None
        self._fan_count: int = 0
        self.available: bool = False
        self.backend_name: str = "None"
        self.lock_reason: str = "Locked by OEM BIOS EC"
        self._probe()

    def _probe(self) -> None:
        candidates = [
            "nvml.dll",
            r"C:\Windows\System32\nvml.dll",
            r"C:\Program Files\NVIDIA Corporation\NVSMI\nvml.dll",
        ]
        dll = None
        for path in candidates:
            try:
                dll = ctypes.WinDLL(path)
                break
            except OSError:
                continue

        if dll is None:
            self.lock_reason = "Locked by OEM BIOS EC"
            return

        try:
            init_fn = getattr(dll, "nvmlInit_v2", None) or getattr(dll, "nvmlInit", None)
            if init_fn is None or init_fn() != 0:
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            get_handle = getattr(dll, "nvmlDeviceGetHandleByIndex_v2", None)
            get_num_fans = getattr(dll, "nvmlDeviceGetNumFans", None)
            get_fan_v2 = getattr(dll, "nvmlDeviceGetFanSpeed_v2", None)
            set_fan_v2 = getattr(dll, "nvmlDeviceSetFanSpeed_v2", None)
            if not all([get_handle, get_num_fans, get_fan_v2, set_fan_v2]):
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            handle = ctypes.c_void_p()
            if get_handle(0, ctypes.byref(handle)) != 0:
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            fan_count = ctypes.c_uint(0)
            rc_fans = get_num_fans(handle, ctypes.byref(fan_count))
            if rc_fans != 0 or fan_count.value == 0:
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            speed = ctypes.c_uint(0)
            rc_spd = get_fan_v2(handle, 0, ctypes.byref(speed))
            if rc_spd != 0:
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            # Probe manual fan policy capability if exposed by driver
            set_policy = getattr(dll, "nvmlDeviceSetFanControlPolicy", None)
            if set_policy is None:
                self.lock_reason = "Locked by OEM BIOS EC"
                return

            self._nvml = dll
            self._handle = handle
            self._fan_count = int(fan_count.value)
            self.available = True
            self.backend_name = f"NVML ({self._fan_count} PWM header{'s' if self._fan_count > 1 else ''})"
            self.lock_reason = ""
        except Exception:
            self.available = False
            self.lock_reason = "Locked by OEM BIOS EC"

    def set_fan_duty(self, duty_pct: int) -> tuple[bool, str]:
        if not self.available or self._nvml is None or self._handle is None:
            return False, "Locked by OEM BIOS EC"
        pct = max(0, min(100, int(duty_pct)))
        try:
            set_policy = getattr(self._nvml, "nvmlDeviceSetFanControlPolicy", None)
            set_fan_v2 = getattr(self._nvml, "nvmlDeviceSetFanSpeed_v2", None)
            for idx in range(self._fan_count):
                if set_policy is not None:
                    # 1 = NVML_FAN_POLICY_MANUAL
                    set_policy(self._handle, idx, 1)
                rc = set_fan_v2(self._handle, idx, pct)
                if rc != 0:
                    return False, f"NVML fan write rejected (code {rc})"
            return True, f"NVML PWM duty set to {pct}%"
        except Exception as exc:
            return False, f"NVML write error: {exc}"

    def restore_auto(self) -> tuple[bool, str]:
        if not self.available or self._nvml is None or self._handle is None:
            return False, "No unlocked PWM controller"
        try:
            set_default = getattr(self._nvml, "nvmlDeviceSetDefaultFanSpeed_v2", None)
            set_policy = getattr(self._nvml, "nvmlDeviceSetFanControlPolicy", None)
            for idx in range(self._fan_count):
                if set_default is not None:
                    set_default(self._handle, idx)
                elif set_policy is not None:
                    # 0 = NVML_FAN_POLICY_TEMPERATURE_CONTOLLED
                    set_policy(self._handle, idx, 0)
            return True, "NVML PWM restored to VBIOS auto curve"
        except Exception as exc:
            return False, f"NVML restore error: {exc}"

    def close(self) -> None:
        if self._nvml is not None:
            try:
                shutdown = getattr(self._nvml, "nvmlShutdown", None)
                if shutdown is not None:
                    shutdown()
            except Exception:
                pass
            self._nvml = None


def _run_powercfg(args: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(
            ["powercfg", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=6.0,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode, (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        return -1, str(exc)


def _parse_powercfg_ac_index(output: str) -> int | None:
    match = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", output)
    if not match:
        return None
    try:
        return int(match.group(1), 16)
    except ValueError:
        return None


def read_power_policy() -> PowerPolicyState:
    """Query active Windows power scheme and processor thermal/boost indices."""
    state = PowerPolicyState()
    rc, scheme_out = _run_powercfg(["/getactivescheme"])
    if rc == 0:
        m = re.search(r"Power Scheme GUID:\s*([0-9a-fA-F-]+)\s*\(([^)]+)\)", scheme_out)
        if m:
            state.scheme_guid = m.group(1).strip()
            state.scheme_name = m.group(2).strip()

    for attr_name, guid in (
        ("proc_throttle_max", GUID_PROCTHROTTLEMAX),
        ("perf_boost_mode", GUID_PERFBOOSTMODE),
        ("sys_cool_pol", GUID_SYSCOOLPOL),
    ):
        rc_q, out_q = _run_powercfg(["/qh", "SCHEME_CURRENT", SUB_PROCESSOR, guid])
        if rc_q == 0:
            setattr(state, attr_name, _parse_powercfg_ac_index(out_q))

    return state


def set_proc_throttle_max(pct: int) -> tuple[bool, str]:
    """Apply PROCTHROTTLEMAX (50..100%) to both AC and DC on active power scheme."""
    clamped = max(50, min(100, int(pct)))
    rc_ac, out_ac = _run_powercfg(["/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_PROCTHROTTLEMAX, str(clamped)])
    rc_dc, out_dc = _run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_PROCTHROTTLEMAX, str(clamped)])
    rc_act, out_act = _run_powercfg(["/setactive", "SCHEME_CURRENT"])
    if rc_ac == 0 and rc_dc == 0 and rc_act == 0:
        return True, f"PROCTHROTTLEMAX={clamped}% applied [AC+DC]"
    err = (out_ac or out_dc or out_act).strip() or "powercfg rejected value"
    return False, f"PROCTHROTTLEMAX failed: {err}"


def set_perf_boost_mode(mode_index: int) -> tuple[bool, str]:
    """Apply PERFBOOSTMODE (0=Disabled, 3=Efficient Enabled, 2=Aggressive)."""
    if mode_index not in (0, 2, 3):
        return False, f"Unsupported PERFBOOSTMODE index: {mode_index}"
    rc_ac, out_ac = _run_powercfg(["/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_PERFBOOSTMODE, str(mode_index)])
    rc_dc, out_dc = _run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_PERFBOOSTMODE, str(mode_index)])
    rc_act, out_act = _run_powercfg(["/setactive", "SCHEME_CURRENT"])
    label = BOOST_LABELS.get(mode_index, str(mode_index))
    if rc_ac == 0 and rc_dc == 0 and rc_act == 0:
        return True, f"PERFBOOSTMODE={mode_index} ({label}) applied [AC+DC]"
    err = (out_ac or out_dc or out_act).strip() or "powercfg rejected value"
    return False, f"PERFBOOSTMODE failed: {err}"


def set_sys_cool_pol(policy_index: int) -> tuple[bool, str]:
    """Apply SYSCOOLPOL (0=Passive, 1=Active)."""
    if policy_index not in (0, 1):
        return False, f"Unsupported SYSCOOLPOL index: {policy_index}"
    rc_ac, out_ac = _run_powercfg(["/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_SYSCOOLPOL, str(policy_index)])
    rc_dc, out_dc = _run_powercfg(["/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, GUID_SYSCOOLPOL, str(policy_index)])
    rc_act, out_act = _run_powercfg(["/setactive", "SCHEME_CURRENT"])
    label = COOLING_LABELS.get(policy_index, str(policy_index))
    if rc_ac == 0 and rc_dc == 0 and rc_act == 0:
        return True, f"SYSCOOLPOL={policy_index} ({label}) applied [AC+DC]"
    err = (out_ac or out_dc or out_act).strip() or "powercfg rejected value"
    return False, f"SYSCOOLPOL failed: {err}"


class HardwareEngine:
    """Thread-safe hardware telemetry & thermal control coordinator."""

    def __init__(self, poll_interval_s: float = 1.0) -> None:
        self.poll_interval_s = max(0.5, poll_interval_s)
        self._cim = _CimBridge()
        self._nvml = _NvmlFanController()
        self._nvidia_smi_path = self._find_nvidia_smi()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_status: Callable[[str], None] | None = None

        self.min_max = MinMaxTracker()
        self.static_info = self._discover_static_info()
        self.latest_snapshot = SensorSnapshot()

        self._fan_mode: str = "AUTO"  # AUTO | MANUAL | CURVE
        self._manual_duty_pct: int = 50
        self._curve_points: list[tuple[float, float]] = list(DEFAULT_FAN_CURVE)
        self._last_applied_duty: int | None = None

        # Prime psutil per-CPU utilization baseline so first sample isn't 0.0 everywhere.
        psutil.cpu_percent(interval=None, percpu=True)

    @staticmethod
    def _find_nvidia_smi() -> str | None:
        found = shutil.which("nvidia-smi")
        if found:
            return found
        fallback = r"C:\Windows\System32\nvidia-smi.exe"
        return fallback if os.path.exists(fallback) else None

    def set_status_callback(self, cb: Callable[[str], None]) -> None:
        self._on_status = cb

    def _emit_status(self, msg: str) -> None:
        if self._on_status is not None:
            try:
                self._on_status(msg)
            except Exception:
                pass

    def _discover_static_info(self) -> StaticHardwareInfo:
        info = StaticHardwareInfo()
        info.is_admin = is_admin()
        info.cpu_cores_physical = psutil.cpu_count(logical=False) or 0
        info.cpu_cores_logical = psutil.cpu_count(logical=True) or 0

        freq = psutil.cpu_freq()
        if freq and freq.max:
            info.cpu_base_mhz = float(freq.max)

        policy = read_power_policy()
        info.active_scheme_guid = policy.scheme_guid
        info.active_scheme_name = policy.scheme_name

        cim_static = self._cim.query("STATIC", timeout=4.0)
        cpu = cim_static.get("cpu")
        if isinstance(cpu, dict):
            name = str(cpu.get("Name") or "").strip()
            if name:
                info.cpu_name = re.sub(r"\s+", " ", name)
            max_mhz = _parse_float(cpu.get("MaxClockSpeed"))
            if max_mhz and max_mhz > 0:
                info.cpu_base_mhz = max_mhz
            phys = cpu.get("NumberOfCores")
            logi = cpu.get("NumberOfLogicalProcessors")
            if isinstance(phys, int) and phys > 0:
                info.cpu_cores_physical = phys
            if isinstance(logi, int) and logi > 0:
                info.cpu_cores_logical = logi

        os_obj = cim_static.get("os")
        if isinstance(os_obj, dict):
            cap = str(os_obj.get("Caption") or "").replace("Microsoft ", "").strip()
            build = str(os_obj.get("BuildNumber") or "").strip()
            if cap:
                info.os_version = f"{cap} (Build {build})" if build else cap

        gpus = cim_static.get("gpus")
        if isinstance(gpus, list) and gpus:
            chosen = None
            for g in gpus:
                if isinstance(g, dict) and "nvidia" in str(g.get("Name", "")).lower():
                    chosen = g
                    break
            if chosen is None and isinstance(gpus[0], dict):
                chosen = gpus[0]
            if chosen:
                info.gpu_name = str(chosen.get("Name") or info.gpu_name).strip()
                drv = str(chosen.get("DriverVersion") or "").strip()
                if drv:
                    info.gpu_driver = drv
                ram = _parse_float(chosen.get("AdapterRAM"))
                if ram and ram > 0:
                    info.gpu_vram_total_mb = round(ram / (1024 * 1024), 0)

        nv = self._poll_nvidia()
        if nv:
            info.nvidia_available = True
            info.gpu_name = nv["name"] or info.gpu_name
            info.gpu_driver = nv["driver"] or info.gpu_driver
            if nv["vram_total"] is not None:
                info.gpu_vram_total_mb = nv["vram_total"]

        info.pwm_control_available = self._nvml.available
        info.pwm_backend = self._nvml.backend_name
        info.pwm_lock_reason = self._nvml.lock_reason
        return info

    def _poll_nvidia(self) -> dict[str, Any] | None:
        if not self._nvidia_smi_path:
            return None
        try:
            res = subprocess.run(
                [
                    self._nvidia_smi_path,
                    "--query-gpu=name,driver_version,temperature.gpu,clocks.current.graphics,"
                    "clocks.max.graphics,power.draw,fan.speed,utilization.gpu,memory.used,"
                    "memory.total,pstate",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.5,
                creationflags=CREATE_NO_WINDOW,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None
            line = res.stdout.strip().splitlines()[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) < 11:
                return None
            pstate = parts[10] if parts[10].lower() not in _NA_TOKENS else None
            return {
                "name": parts[0],
                "driver": parts[1],
                "temp": _parse_float(parts[2]),
                "clock": _parse_float(parts[3]),
                "max_clock": _parse_float(parts[4]),
                "power": _parse_float(parts[5]),
                "fan_pct": _parse_float(parts[6]),
                "util": _parse_float(parts[7]),
                "vram_used": _parse_float(parts[8]),
                "vram_total": _parse_float(parts[9]),
                "pstate": pstate,
            }
        except Exception:
            return None

    def poll_once(self) -> SensorSnapshot:
        """Collect one genuine telemetry snapshot from psutil, CIM/WMI, and nvidia-smi."""
        snap = SensorSnapshot(timestamp=time.time())

        # 1. CPU Utilization via psutil
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        if per_core:
            snap.cpu_load_pct = round(sum(per_core) / len(per_core), 1)
            snap.cpu_peak_core_pct = round(max(per_core), 1)
        else:
            snap.cpu_load_pct = round(psutil.cpu_percent(interval=None), 1)
            snap.cpu_peak_core_pct = snap.cpu_load_pct

        # 2. WMI/CIM Poll (CPU temp, dynamic clock, LHM/OHM sensors, Win32_Fan)
        cim_poll = self._cim.query("POLL", timeout=2.5)
        fans: list[FanReading] = []

        # 2a. Check LibreHardwareMonitor / OpenHardwareMonitor if user runs it in background
        lhm_sensors = cim_poll.get("lhm_sensors")
        if isinstance(lhm_sensors, list):
            for s in lhm_sensors:
                if not isinstance(s, dict):
                    continue
                stype = s.get("SensorType")
                sname = str(s.get("Name") or "")
                sval = _parse_float(s.get("Value"))
                ident = str(s.get("Identifier") or "").lower()
                if stype == "Temperature" and sval is not None and 10.0 <= sval <= 120.0:
                    if "cpu" in ident and ("package" in sname.lower() or snap.cpu_temp_c is None):
                        snap.cpu_temp_c = round(sval, 1)
                        snap.cpu_temp_source = "LibreHardwareMonitor"
                elif stype == "Fan" and sval is not None and sval >= 0:
                    fans.append(
                        FanReading(
                            name=sname or "Fan",
                            rpm=round(sval, 0),
                            source="LHM/OHM",
                            status="Active" if sval > 0 else "Idle",
                        )
                    )

        # 2b. MSAcpi_ThermalZoneTemperature (tenths of Kelvin)
        if snap.cpu_temp_c is None:
            acpi_list = cim_poll.get("acpi_temps")
            valid_c: list[float] = []
            if isinstance(acpi_list, list):
                for item in acpi_list:
                    if not isinstance(item, dict):
                        continue
                    raw_k = _parse_float(item.get("CurrentTemperature"))
                    if raw_k is not None and raw_k > 2000:
                        celsius = (raw_k / 10.0) - 273.15
                        if 10.0 <= celsius <= 120.0:
                            valid_c.append(celsius)
            if valid_c:
                snap.cpu_temp_c = round(max(valid_c), 1)
                snap.cpu_temp_source = "MSAcpi_ThermalZoneTemperature"

        # 2c. Fallback Win32_PerfFormattedData_Counters_ThermalZoneInformation
        if snap.cpu_temp_c is None:
            perf_list = cim_poll.get("perf_temps")
            valid_c = []
            if isinstance(perf_list, list):
                for item in perf_list:
                    if not isinstance(item, dict):
                        continue
                    hp = _parse_float(item.get("HighPrecisionTemperature"))
                    std = _parse_float(item.get("Temperature"))
                    celsius = None
                    if hp is not None and hp > 2000:
                        celsius = (hp / 10.0) - 273.15
                    elif std is not None and std > 200:
                        celsius = std - 273.15
                    if celsius is not None and 10.0 <= celsius <= 120.0:
                        valid_c.append(celsius)
            if valid_c:
                snap.cpu_temp_c = round(max(valid_c), 1)
                snap.cpu_temp_source = "ThermalZoneInformation"

        # 2d. Dynamic CPU Core Clock (supports Turbo Boost via % Processor Performance)
        proc_perf = cim_poll.get("proc_perf")
        if isinstance(proc_perf, dict):
            pct_perf = _parse_float(proc_perf.get("PercentProcessorPerformance"))
            perf_freq = _parse_float(proc_perf.get("ProcessorFrequency"))
            base_mhz = self.static_info.cpu_base_mhz or perf_freq or 0.0
            if pct_perf is not None and pct_perf > 0 and base_mhz > 0:
                snap.cpu_clock_ghz = round((base_mhz * (pct_perf / 100.0)) / 1000.0, 2)
                snap.cpu_clock_source = "PerfCounter (% Processor Performance)"
            elif perf_freq is not None and perf_freq > 0:
                snap.cpu_clock_ghz = round(perf_freq / 1000.0, 2)
                snap.cpu_clock_source = "PerfCounter (Frequency)"

        if snap.cpu_clock_ghz is None:
            ps_freq = psutil.cpu_freq()
            if ps_freq and ps_freq.current:
                snap.cpu_clock_ghz = round(ps_freq.current / 1000.0, 2)
                snap.cpu_clock_source = "psutil.cpu_freq"

        # 2e. Genuine Win32_Fan rows (only if Win32_Fan exposes real rows)
        win_fans = cim_poll.get("win_fans")
        if isinstance(win_fans, list):
            for wf in win_fans:
                if not isinstance(wf, dict):
                    continue
                fname = str(wf.get("Name") or wf.get("DeviceID") or "ACPI System Fan").strip()
                desired = _parse_float(wf.get("DesiredSpeed"))
                status_str = str(wf.get("Status") or "Unknown").strip()
                # DesiredSpeed in Win32_Fan is often null or unpopulated on OEM laptops; never fake RPM.
                rpm_val = desired if (desired is not None and desired > 0) else None
                fans.append(
                    FanReading(
                        name=fname,
                        rpm=rpm_val,
                        source="Win32_Fan",
                        status=status_str if rpm_val is not None else "N/A (OEM EC Locked)",
                    )
                )

        # 3. NVIDIA GPU Telemetry via nvidia-smi
        nv = self._poll_nvidia()
        if nv:
            snap.gpu_temp_c = nv["temp"]
            snap.gpu_clock_mhz = nv["clock"]
            snap.gpu_max_clock_mhz = nv["max_clock"]
            snap.gpu_power_w = nv["power"]
            snap.gpu_load_pct = nv["util"]
            snap.gpu_vram_used_mb = nv["vram_used"]
            snap.gpu_vram_total_mb = nv["vram_total"]
            snap.gpu_pstate = nv["pstate"]
            if nv["fan_pct"] is not None:
                fans.append(
                    FanReading(
                        name=f"{nv['name']} Fan",
                        rpm=None,
                        duty_pct=nv["fan_pct"],
                        source="nvidia-smi",
                        status=f"{nv['fan_pct']:.0f}% Duty",
                    )
                )

        snap.fans = fans

        # Update session min/max
        self.min_max.update("cpu_temp_c", snap.cpu_temp_c)
        self.min_max.update("cpu_clock_ghz", snap.cpu_clock_ghz)
        self.min_max.update("cpu_load_pct", snap.cpu_load_pct)
        self.min_max.update("cpu_peak_core_pct", snap.cpu_peak_core_pct)
        self.min_max.update("gpu_temp_c", snap.gpu_temp_c)
        self.min_max.update("gpu_clock_mhz", snap.gpu_clock_mhz)
        self.min_max.update("gpu_power_w", snap.gpu_power_w)
        self.min_max.update("gpu_load_pct", snap.gpu_load_pct)
        self.min_max.update("gpu_vram_used_mb", snap.gpu_vram_used_mb)

        with self._lock:
            self.latest_snapshot = snap

        self._apply_curve_if_active(snap)
        return snap

    def _apply_curve_if_active(self, snap: SensorSnapshot) -> None:
        if not self._nvml.available or self._fan_mode != "CURVE":
            return
        ref_temp = snap.gpu_temp_c if snap.gpu_temp_c is not None else snap.cpu_temp_c
        if ref_temp is None:
            return
        target = int(round(interpolate_fan_curve(ref_temp, self._curve_points)))
        if self._last_applied_duty is None or abs(target - self._last_applied_duty) >= 2:
            ok, msg = self._nvml.set_fan_duty(target)
            if ok:
                self._last_applied_duty = target
                self._emit_status(f"Curve PWM: {ref_temp:.1f}°C -> {target}%")
            else:
                self._emit_status(msg)

    def set_pwm_mode(self, mode: str, manual_duty: int | None = None, curve_points: list[tuple[float, float]] | None = None) -> tuple[bool, str]:
        if curve_points is not None:
            self._curve_points = list(curve_points)
        if manual_duty is not None:
            self._manual_duty_pct = max(0, min(100, int(manual_duty)))
        self._fan_mode = mode

        if not self._nvml.available:
            return False, "Locked by OEM BIOS EC"
        if mode == "AUTO":
            self._last_applied_duty = None
            return self._nvml.restore_auto()
        if mode == "MANUAL":
            self._last_applied_duty = self._manual_duty_pct
            return self._nvml.set_fan_duty(self._manual_duty_pct)
        if mode == "CURVE":
            snap = self.get_snapshot()
            ref_temp = snap.gpu_temp_c if snap.gpu_temp_c is not None else snap.cpu_temp_c
            if ref_temp is not None:
                target = int(round(interpolate_fan_curve(ref_temp, self._curve_points)))
                self._last_applied_duty = target
                return self._nvml.set_fan_duty(target)
            return True, "Fan curve armed"
        return False, f"Unknown fan mode: {mode}"

    def get_snapshot(self) -> SensorSnapshot:
        with self._lock:
            return self.latest_snapshot

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, name="HANKOTelemetryLoop", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.poll_once()
            except Exception:
                pass
            self._stop_event.wait(self.poll_interval_s)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        if self._nvml.available and self._fan_mode != "AUTO":
            self._nvml.restore_auto()
        self._nvml.close()
        self._cim.close()
