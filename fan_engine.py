"""LibreHardwareMonitorLib (PawnIO) multi-fan telemetry engine.

Scans Motherboard, SuperIO, Embedded Controller (EC), Controller, and GPU nodes
for all SensorType.Fan channels and maps them to live RPM and load percentages.
Never crashes if firmware locks EC telemetry or if the driver is unavailable.
"""

from __future__ import annotations

import math
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Force desktop .NET Framework 4.x CLR before importing pythonnet on Windows.
os.environ.setdefault("PYTHONNET_RUNTIME", "netfx")

try:
    import clr  # type: ignore
    _CLR_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - defensive fallback
    clr = None  # type: ignore
    _CLR_IMPORT_ERROR = str(exc)

EC_LOCKED_MESSAGE = "EC TELEMETRY LOCKED BY HP BIOS"
DEFAULT_MAX_RPM_LAPTOP = 5500.0
DEFAULT_MAX_RPM_CHASSIS = 3000.0


@dataclass
class FanChannel:
    """Single detected hardware cooling fan telemetry channel."""

    name: str
    hardware: str
    node_type: str  # "Motherboard" | "SuperIO" | "EC" | "GPU" | "Controller"
    rpm: float
    load_pct: float
    identifier: str
    pct_source: str  # "PWM Control" | "RPM Est."
    max_rpm_ref: float


@dataclass
class FanScanResult:
    """Result returned from a complete multi-node fan telemetry scan."""

    fan_count: int
    fans: list[FanChannel]
    status_message: str
    backend: str
    dll_loaded: bool
    error_detail: str = ""


def _resolve_lhm_dll() -> Path | None:
    """Locate LibreHardwareMonitorLib.dll across PyInstaller and source paths."""
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "LibreHardwareMonitorLib.dll")
    candidates.append(Path(sys.executable).resolve().parent / "LibreHardwareMonitorLib.dll")
    candidates.append(Path(__file__).resolve().parent / "LibreHardwareMonitorLib.dll")
    candidates.append(Path.cwd() / "LibreHardwareMonitorLib.dll")

    seen: set[Path] = set()
    for path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    return None


def _classify_node_type(hw_type_str: str) -> str:
    low = hw_type_str.lower()
    if "embeddedcontroller" in low or low == "ec":
        return "EC"
    if "superio" in low:
        return "SuperIO"
    if "motherboard" in low:
        return "Motherboard"
    if "gpu" in low:
        return "GPU"
    if "cooler" in low or "controller" in low:
        return "Controller"
    return "Controller"


def _normalize_sensor_key(name: str) -> str:
    text = name.lower().strip()
    for token in ("control", "fan", "#", "-", "_"):
        text = text.replace(token, " ")
    return " ".join(text.split())


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


class FanEngine:
    """Thread-safe LibreHardwareMonitorLib.dll scanner for system fan sensors."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._computer: Any = None
        self._sensor_type_enum: Any = None
        self._dll_loaded = False
        self._opened = False
        self._last_error = _CLR_IMPORT_ERROR
        self._peak_rpm: dict[str, float] = {}

    def open(self) -> bool:
        """Load LibreHardwareMonitorLib.dll and open hardware sensor nodes."""
        with self._lock:
            return self._open_locked()

    def _open_locked(self) -> bool:
        if self._opened and self._computer is not None:
            return True

        if clr is None:
            self._last_error = _CLR_IMPORT_ERROR or "pythonnet (clr) is not installed"
            self._dll_loaded = False
            return False

        dll_path = _resolve_lhm_dll()
        if dll_path is None:
            self._last_error = "LibreHardwareMonitorLib.dll not found"
            self._dll_loaded = False
            return False

        dll_dir = str(dll_path.parent)
        if dll_dir not in sys.path:
            sys.path.insert(0, dll_dir)

        try:
            try:
                clr.AddReference(str(dll_path))
            except Exception:
                clr.AddReference("LibreHardwareMonitorLib")

            from LibreHardwareMonitor.Hardware import Computer, SensorType  # type: ignore

            self._sensor_type_enum = SensorType
            self._dll_loaded = True

            computer = Computer()
            computer.IsMotherboardEnabled = True
            computer.IsControllerEnabled = True
            computer.IsGpuEnabled = True
            computer.IsCpuEnabled = False
            computer.IsMemoryEnabled = False
            computer.IsNetworkEnabled = False
            computer.IsStorageEnabled = False
            computer.IsPsuEnabled = False
            computer.IsBatteryEnabled = False

            computer.Open()
            self._computer = computer
            self._opened = True
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = str(exc)
            self._computer = None
            self._opened = False
            return False

    def _collect_node(
        self,
        hw: Any,
        seen_ids: set[str],
        fans_out: list[FanChannel],
    ) -> None:
        if hw is None or self._sensor_type_enum is None:
            return

        try:
            hw.Update()
        except Exception:
            # A locked EC or SuperIO node must never abort scanning other nodes.
            pass

        hw_name = str(getattr(hw, "Name", "") or "Hardware").strip()
        hw_type_str = str(getattr(hw, "HardwareType", "") or "")
        node_type = _classify_node_type(hw_type_str)

        fan_type = self._sensor_type_enum.Fan
        ctrl_type = self._sensor_type_enum.Control

        controls_by_index: dict[int, float] = {}
        controls_by_name: dict[str, float] = {}
        controls_by_norm: dict[str, float] = {}
        all_controls: list[float] = []

        sensors = []
        try:
            sensors = list(hw.Sensors)
        except Exception:
            sensors = []

        for sensor in sensors:
            try:
                if sensor.SensorType != ctrl_type:
                    continue
                val = _safe_float(sensor.Value)
                if val is None:
                    continue
                clamped = max(0.0, min(100.0, val))
                idx = int(getattr(sensor, "Index", -1))
                sname = str(getattr(sensor, "Name", "") or "").strip()
                if idx >= 0:
                    controls_by_index[idx] = clamped
                if sname:
                    controls_by_name[sname.lower()] = clamped
                    norm = _normalize_sensor_key(sname)
                    if norm:
                        controls_by_norm[norm] = clamped
                all_controls.append(clamped)
            except Exception:
                continue

        fan_sensors_on_node: list[Any] = []
        for sensor in sensors:
            try:
                if sensor.SensorType == fan_type:
                    fan_sensors_on_node.append(sensor)
            except Exception:
                continue

        for sensor in fan_sensors_on_node:
            try:
                rpm_val = _safe_float(sensor.Value)
                if rpm_val is None or rpm_val < 0.0:
                    continue

                ident = str(getattr(sensor, "Identifier", "") or "")
                sname = str(getattr(sensor, "Name", "") or "Fan").strip()
                if not ident:
                    ident = f"/{node_type.lower()}/{hw_name}/{sname}"
                if ident in seen_ids:
                    continue
                seen_ids.add(ident)

                rpm_clean = round(rpm_val, 0)
                idx = int(getattr(sensor, "Index", -1))
                sname_low = sname.lower()
                snorm = _normalize_sensor_key(sname)

                ctrl_pct: float | None = None
                if idx in controls_by_index:
                    ctrl_pct = controls_by_index[idx]
                elif sname_low in controls_by_name:
                    ctrl_pct = controls_by_name[sname_low]
                elif snorm and snorm in controls_by_norm:
                    ctrl_pct = controls_by_norm[snorm]
                elif len(all_controls) == 1 and len(fan_sensors_on_node) == 1:
                    ctrl_pct = all_controls[0]

                default_max = (
                    DEFAULT_MAX_RPM_CHASSIS
                    if node_type in ("Motherboard", "SuperIO")
                    else DEFAULT_MAX_RPM_LAPTOP
                )
                prev_peak = self._peak_rpm.get(ident, 0.0)
                peak = max(prev_peak, rpm_clean)
                self._peak_rpm[ident] = peak
                max_ref = max(peak, default_max)

                if ctrl_pct is not None:
                    load_pct = round(max(0.0, min(100.0, ctrl_pct)), 1)
                    pct_source = "PWM Control"
                elif rpm_clean <= 0.0:
                    load_pct = 0.0
                    pct_source = "RPM Est."
                else:
                    load_pct = round(min(100.0, (rpm_clean / max_ref) * 100.0), 1)
                    pct_source = "RPM Est."

                if hw_name and hw_name.lower() not in sname_low:
                    display_name = f"[{node_type}] {hw_name} - {sname}"
                else:
                    display_name = f"[{node_type}] {sname}"

                fans_out.append(
                    FanChannel(
                        name=display_name,
                        hardware=hw_name,
                        node_type=node_type,
                        rpm=rpm_clean,
                        load_pct=load_pct,
                        identifier=ident,
                        pct_source=pct_source,
                        max_rpm_ref=max_ref,
                    )
                )
            except Exception:
                continue

        sub_list = []
        try:
            sub_list = list(hw.SubHardware)
        except Exception:
            sub_list = []

        for sub in sub_list:
            self._collect_node(sub, seen_ids, fans_out)

    def scan_fans(self) -> FanScanResult:
        """Scan all Motherboard, SuperIO, EC, Controller, and GPU fan sensors."""
        with self._lock:
            if not self._open_locked() or self._computer is None:
                return FanScanResult(
                    fan_count=0,
                    fans=[],
                    status_message=EC_LOCKED_MESSAGE,
                    backend="LibreHardwareMonitorLib (PawnIO)" if self._dll_loaded else "LHM Unavailable",
                    dll_loaded=self._dll_loaded,
                    error_detail=self._last_error,
                )

            fans: list[FanChannel] = []
            seen_ids: set[str] = set()
            try:
                hw_nodes = list(self._computer.Hardware)
            except Exception as exc:
                self._last_error = str(exc)
                hw_nodes = []

            for hw in hw_nodes:
                self._collect_node(hw, seen_ids, fans)

            if fans:
                count = len(fans)
                return FanScanResult(
                    fan_count=count,
                    fans=fans,
                    status_message=f"LHM ACTIVE ({count} FAN{'S' if count != 1 else ''})",
                    backend="LibreHardwareMonitorLib (PawnIO)",
                    dll_loaded=True,
                    error_detail="",
                )

            return FanScanResult(
                fan_count=0,
                fans=[],
                status_message=EC_LOCKED_MESSAGE,
                backend="LibreHardwareMonitorLib (PawnIO)",
                dll_loaded=self._dll_loaded,
                error_detail=self._last_error,
            )

    def get_fans(self) -> tuple[int, list[FanChannel], str]:
        """Return (fan_count, fans, status_message)."""
        res = self.scan_fans()
        return res.fan_count, res.fans, res.status_message

    def close(self) -> None:
        """Close the LibreHardwareMonitor computer session safely."""
        with self._lock:
            if self._computer is not None:
                try:
                    self._computer.Close()
                except Exception:
                    pass
                self._computer = None
            self._opened = False


if __name__ == "__main__":
    engine = FanEngine()
    try:
        result = engine.scan_fans()
        print(f"Backend    : {result.backend}")
        print(f"DLL Loaded : {result.dll_loaded}")
        print(f"DETECTED FANS: {result.fan_count}")
        print(f"Status     : {result.status_message}")
        if result.error_detail:
            print(f"Note       : {result.error_detail}")
        for idx, fan in enumerate(result.fans, start=1):
            print(
                f"  #{idx} {fan.name:<40} | "
                f"{fan.rpm:5.0f} RPM | {fan.load_pct:5.1f}% ({fan.pct_source}) | {fan.identifier}"
            )
    finally:
        engine.close()
