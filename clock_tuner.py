"""GPU MHz clock locking (nvidia-smi) and CPU GHz frequency control (powercfg).

All subprocess invocations execute silently with CREATE_NO_WINDOW so no console
windows flash on Windows 10/11.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass

import psutil

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

GPU_CLOCK_MIN_MHZ = 800
GPU_CLOCK_MAX_MHZ = 1850
GPU_CLOCK_STEP_MHZ = 15

CPU_FREQ_MIN_GHZ = 1.80
CPU_FREQ_MAX_GHZ = 4.50
CPU_FREQ_STEP_GHZ = 0.10

SUB_PROCESSOR = "SUB_PROCESSOR"
SUB_PROCESSOR_GUID = "54533251-82be-4824-96c1-47b60b740d00"
PROCFREQMAX = "PROCFREQMAX"

_NA_TOKENS = {"", "n/a", "[n/a]", "not supported", "[not supported]", "none", "null"}


def snap_gpu_clock_mhz(mhz: float | int) -> int:
    """Clamp GPU core clock to [800, 1850] MHz and snap to 15 MHz increments."""
    clamped = max(float(GPU_CLOCK_MIN_MHZ), min(float(GPU_CLOCK_MAX_MHZ), float(mhz)))
    steps = round((clamped - GPU_CLOCK_MIN_MHZ) / GPU_CLOCK_STEP_MHZ)
    snapped = GPU_CLOCK_MIN_MHZ + steps * GPU_CLOCK_STEP_MHZ
    return int(max(GPU_CLOCK_MIN_MHZ, min(GPU_CLOCK_MAX_MHZ, snapped)))


def snap_cpu_freq_ghz(ghz: float) -> float:
    """Clamp CPU frequency cap to [1.80, 4.50] GHz and snap to 0.10 GHz increments."""
    clamped = max(CPU_FREQ_MIN_GHZ, min(CPU_FREQ_MAX_GHZ, float(ghz)))
    steps = round((clamped - CPU_FREQ_MIN_GHZ) / CPU_FREQ_STEP_GHZ)
    snapped = round(CPU_FREQ_MIN_GHZ + steps * CPU_FREQ_STEP_GHZ, 2)
    return max(CPU_FREQ_MIN_GHZ, min(CPU_FREQ_MAX_GHZ, snapped))


def ghz_to_mhz(ghz: float) -> int:
    """Convert snapped GHz float to integer MHz for Windows powercfg PROCFREQMAX."""
    return int(round(snap_cpu_freq_ghz(ghz) * 1000.0))


def find_nvidia_smi() -> str | None:
    """Locate nvidia-smi.exe on the host system."""
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for candidate in (
        r"C:\Windows\System32\nvidia-smi.exe",
        r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


def _run_hidden(cmd: list[str], timeout: float = 6.0) -> tuple[int, str, str]:
    """Execute a subprocess silently with CREATE_NO_WINDOW."""
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=CREATE_NO_WINDOW,
        )
        return res.returncode, (res.stdout or "").strip(), (res.stderr or "").strip()
    except Exception as exc:
        return -1, "", str(exc)


def lock_gpu_clock(mhz: int | float) -> tuple[bool, str, int]:
    """Lock NVIDIA GPU graphics clock via nvidia-smi --lock-gpu-clocks=<mhz>,<mhz>."""
    target = snap_gpu_clock_mhz(mhz)
    smi = find_nvidia_smi()
    if not smi:
        return False, "nvidia-smi not found on system", target

    rc, out, err = _run_hidden([smi, f"--lock-gpu-clocks={target},{target}"], timeout=6.0)
    if rc == 0:
        return True, f"GPU clock locked to {target} MHz", target
    detail = err or out or f"exit code {rc}"
    return False, f"nvidia-smi lock failed: {detail}", target


def reset_gpu_clock() -> tuple[bool, str]:
    """Restore stock VBIOS GPU clock scaling via nvidia-smi --reset-gpu-clocks."""
    smi = find_nvidia_smi()
    if not smi:
        return False, "nvidia-smi not found on system"

    rc, out, err = _run_hidden([smi, "--reset-gpu-clocks"], timeout=6.0)
    if rc == 0:
        return True, "GPU clock reset to stock VBIOS auto"
    detail = err or out or f"exit code {rc}"
    return False, f"nvidia-smi reset failed: {detail}"


def read_live_gpu_clock_mhz() -> float | None:
    """Query current live GPU core clock in MHz via nvidia-smi."""
    smi = find_nvidia_smi()
    if not smi:
        return None
    rc, out, _ = _run_hidden(
        [smi, "--query-gpu=clocks.current.graphics", "--format=csv,noheader,nounits"],
        timeout=3.0,
    )
    if rc != 0 or not out:
        return None
    token = out.splitlines()[0].strip()
    if token.lower() in _NA_TOKENS:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def set_cpu_max_freq_ghz(ghz: float) -> tuple[bool, str, float, int]:
    """Apply PROCFREQMAX ceiling in MHz across AC and DC on SCHEME_CURRENT."""
    snapped_ghz = snap_cpu_freq_ghz(ghz)
    mhz = ghz_to_mhz(snapped_ghz)
    mhz_str = str(mhz)

    rc_ac, out_ac, err_ac = _run_hidden(
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, PROCFREQMAX, mhz_str]
    )
    rc_dc, out_dc, err_dc = _run_hidden(
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, PROCFREQMAX, mhz_str]
    )
    rc_act, out_act, err_act = _run_hidden(["powercfg", "/setactive", "SCHEME_CURRENT"])

    if rc_ac == 0 and rc_dc == 0 and rc_act == 0:
        return True, f"CPU PROCFREQMAX set to {snapped_ghz:.2f} GHz ({mhz} MHz) [AC+DC]", snapped_ghz, mhz

    detail = err_ac or out_ac or err_dc or out_dc or err_act or out_act or "powercfg rejected value"
    return False, f"PROCFREQMAX failed: {detail}", snapped_ghz, mhz


def reset_cpu_max_freq() -> tuple[bool, str]:
    """Reset PROCFREQMAX to 0 (Turbo Auto) across AC and DC on SCHEME_CURRENT."""
    rc_ac, out_ac, err_ac = _run_hidden(
        ["powercfg", "/setacvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, PROCFREQMAX, "0"]
    )
    rc_dc, out_dc, err_dc = _run_hidden(
        ["powercfg", "/setdcvalueindex", "SCHEME_CURRENT", SUB_PROCESSOR, PROCFREQMAX, "0"]
    )
    rc_act, out_act, err_act = _run_hidden(["powercfg", "/setactive", "SCHEME_CURRENT"])

    if rc_ac == 0 and rc_dc == 0 and rc_act == 0:
        return True, "CPU PROCFREQMAX reset to 0 (Turbo Auto) [AC+DC]"

    detail = err_ac or out_ac or err_dc or out_dc or err_act or out_act or "powercfg rejected reset"
    return False, f"PROCFREQMAX reset failed: {detail}"


def read_cpu_target_freq_mhz() -> int | None:
    """Query active PROCFREQMAX setting index in MHz (0 indicates Turbo Auto)."""
    rc, out, _ = _run_hidden(
        ["powercfg", "/qh", "SCHEME_CURRENT", SUB_PROCESSOR, PROCFREQMAX],
        timeout=5.0,
    )
    if rc != 0 or not out:
        return None
    match = re.search(r"Current AC Power Setting Index:\s*0x([0-9a-fA-F]+)", out)
    if not match:
        return None
    try:
        return int(match.group(1), 16)
    except ValueError:
        return None


def read_live_cpu_clock_ghz() -> float | None:
    """Read live CPU frequency in GHz via PerfCounter (% Processor Performance) or psutil fallback."""
    ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if ps_exe:
        ps_cmd = (
            "$c = Get-CimInstance -ClassName Win32_Processor | Select-Object -First 1 MaxClockSpeed; "
            "$p = Get-CimInstance -Namespace root\\CIMV2 -ClassName Win32_PerfFormattedData_Counters_ProcessorInformation "
            "-Filter \"Name='_Total'\" | Select-Object -First 1 PercentProcessorPerformance, ProcessorFrequency; "
            "if ($p -and $c -and $p.PercentProcessorPerformance -gt 0 -and $c.MaxClockSpeed -gt 0) { "
            "  [math]::Round(($c.MaxClockSpeed * ($p.PercentProcessorPerformance / 100.0)) / 1000.0, 2) "
            "} elseif ($p -and $p.ProcessorFrequency -gt 0) { "
            "  [math]::Round($p.ProcessorFrequency / 1000.0, 2) "
            "}"
        )
        rc, out, _ = _run_hidden(
            [ps_exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            timeout=4.0,
        )
        if rc == 0 and out:
            try:
                val = float(out.splitlines()[0].strip().replace(",", "."))
                if val > 0:
                    return round(val, 2)
            except ValueError:
                pass

    freq = psutil.cpu_freq()
    if freq and freq.current and freq.current > 0:
        return round(float(freq.current) / 1000.0, 2)
    return None


@dataclass
class ClockState:
    """Combined live and target clock state for GPU and CPU tuning UI."""

    live_gpu_mhz: float | None
    target_gpu_mhz: int | None  # None = STOCK
    live_cpu_ghz: float | None
    target_cpu_ghz: float | None  # None = TURBO AUTO
    target_cpu_mhz: int  # 0 = TURBO AUTO
    nvidia_available: bool


class ClockTuner:
    """Thread-safe coordinator tracking live vs target GPU MHz and CPU GHz settings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nvidia_available = find_nvidia_smi() is not None
        self._target_gpu_mhz: int | None = None
        initial_cpu_mhz = read_cpu_target_freq_mhz()
        self._target_cpu_mhz: int = initial_cpu_mhz if initial_cpu_mhz is not None else 0

    @property
    def nvidia_available(self) -> bool:
        return self._nvidia_available

    def apply_gpu_clock(self, mhz: int | float) -> tuple[bool, str]:
        ok, msg, applied_mhz = lock_gpu_clock(mhz)
        if ok:
            with self._lock:
                self._target_gpu_mhz = applied_mhz
        return ok, msg

    def reset_gpu(self) -> tuple[bool, str]:
        ok, msg = reset_gpu_clock()
        if ok:
            with self._lock:
                self._target_gpu_mhz = None
        return ok, msg

    def apply_cpu_freq(self, ghz: float) -> tuple[bool, str]:
        ok, msg, _snapped_ghz, applied_mhz = set_cpu_max_freq_ghz(ghz)
        if ok:
            with self._lock:
                self._target_cpu_mhz = applied_mhz
        return ok, msg

    def reset_cpu(self) -> tuple[bool, str]:
        ok, msg = reset_cpu_max_freq()
        if ok:
            with self._lock:
                self._target_cpu_mhz = 0
        return ok, msg

    def sync_cpu_target(self) -> int:
        mhz = read_cpu_target_freq_mhz()
        with self._lock:
            if mhz is not None:
                self._target_cpu_mhz = mhz
            return self._target_cpu_mhz

    def get_state(
        self,
        live_cpu_ghz: float | None = None,
        live_gpu_mhz: float | None = None,
    ) -> ClockState:
        with self._lock:
            target_gpu = self._target_gpu_mhz
            target_cpu_mhz = self._target_cpu_mhz
        target_cpu_ghz = round(target_cpu_mhz / 1000.0, 2) if target_cpu_mhz > 0 else None
        return ClockState(
            live_gpu_mhz=live_gpu_mhz,
            target_gpu_mhz=target_gpu,
            live_cpu_ghz=live_cpu_ghz,
            target_cpu_ghz=target_cpu_ghz,
            target_cpu_mhz=target_cpu_mhz,
            nvidia_available=self._nvidia_available,
        )


if __name__ == "__main__":
    assert snap_gpu_clock_mhz(700) == 800
    assert snap_gpu_clock_mhz(800) == 800
    assert snap_gpu_clock_mhz(808) == 815
    assert snap_gpu_clock_mhz(1850) == 1850
    assert snap_gpu_clock_mhz(2000) == 1850

    assert snap_cpu_freq_ghz(1.2) == 1.80
    assert snap_cpu_freq_ghz(3.14) == 3.10
    assert snap_cpu_freq_ghz(3.16) == 3.20
    assert snap_cpu_freq_ghz(4.8) == 4.50
    assert ghz_to_mhz(3.20) == 3200
    assert ghz_to_mhz(1.80) == 1800
    assert ghz_to_mhz(4.50) == 4500

    live_gpu = read_live_gpu_clock_mhz()
    live_cpu = read_live_cpu_clock_ghz()
    active_cap = read_cpu_target_freq_mhz()
    print(f"Snapping assertions : OK")
    print(f"nvidia-smi found    : {find_nvidia_smi()}")
    print(f"Live GPU Clock (MHz): {live_gpu}")
    print(f"Live CPU Clock (GHz): {live_cpu}")
    print(f"PROCFREQMAX (MHz)   : {active_cap}")
