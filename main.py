"""HANKO Thermal Suite v2.0 — Industrial Windows Hardware Telemetry & Clock Tuner."""

from __future__ import annotations

import multiprocessing
import os
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from typing import Any

import customtkinter as ctk

from clock_tuner import (
    CPU_FREQ_MAX_GHZ,
    CPU_FREQ_MIN_GHZ,
    GPU_CLOCK_MAX_MHZ,
    GPU_CLOCK_MIN_MHZ,
    ClockTuner,
    ghz_to_mhz,
    snap_cpu_freq_ghz,
    snap_gpu_clock_mhz,
)
from fan_engine import EC_LOCKED_MESSAGE, FanChannel, FanScanResult
from hardware_engine import (
    BOOST_LABELS,
    BOOST_MODES_BY_LABEL,
    COOLING_LABELS,
    COOLING_POLICIES_BY_LABEL,
    HardwareEngine,
    PowerPolicyState,
    read_power_policy,
    set_perf_boost_mode,
    set_proc_throttle_max,
    set_sys_cool_pol,
)
from stress_worker import ThermalStressController

APP_VERSION = "2.0.0"

# --- Visual Tokens: Flat Matte-Charcoal Precision Engineering Palette ---
BG_BASE = "#0D0F12"
BG_PANEL = "#15181E"
BG_HEADER = "#111419"
BG_RECESSED = "#0F1217"
BORDER_COLOR = "#282D37"
DIVIDER_COLOR = "#1E222B"
SLATE_SURFACE = "#1C2029"

TEXT_PRIMARY = "#E6EAF0"
TEXT_SECONDARY = "#9CA3AF"
TEXT_MUTED = "#6B7280"

ACCENT_CYAN = "#00D8FF"
ACCENT_CYAN_ALT = "#38E4FF"
ACCENT_AMBER = "#FF8C00"
ACCENT_AMBER_HOVER = "#FFA12E"

STATUS_OK = "#22C55E"
STATUS_WARN = "#FF8C00"
STATUS_CRIT = "#EF4444"

FONT_TITLE = ("Consolas", 15, "bold")
FONT_HEADER = ("Consolas", 11, "bold")
FONT_MONO = ("Consolas", 11)
FONT_MONO_BOLD = ("Consolas", 11, "bold")
FONT_MONO_VAL = ("Consolas", 15, "bold")
FONT_MONO_SM = ("Consolas", 10)
FONT_MONO_SM_BOLD = ("Consolas", 10, "bold")
FONT_MONO_XS = ("Consolas", 9)
FONT_MONO_XS_BOLD = ("Consolas", 9, "bold")
FONT_UI = ("Segoe UI", 10)
FONT_UI_SM = ("Segoe UI", 10)


def resource_path(relative_path: str) -> Path:
    """Resolve bundled resources cleanly for both Python source and PyInstaller --onefile."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def _fmt_fixed(val: float | None, fmt: str, suffix: str = "", width: int = 0, na_text: str = "N/A") -> str:
    if val is None:
        return na_text
    core = f"{format(val, fmt)}{suffix}"
    return core.rjust(width) if width > 0 else core


def _temp_color(temp_c: float | None) -> str:
    if temp_c is None:
        return TEXT_MUTED
    if temp_c >= 88.0:
        return STATUS_CRIT
    if temp_c >= 75.0:
        return ACCENT_AMBER
    return ACCENT_CYAN


class VictusEmblemCanvas(tk.Canvas):
    """Razor-sharp architectural Victus 'V' emblem rendered with dual-tone polygons."""

    def __init__(self, master: Any, width: int = 44, height: int = 38) -> None:
        super().__init__(
            master,
            width=width,
            height=height,
            bg=BG_HEADER,
            highlightthickness=0,
            bd=0,
        )
        self._draw_emblem(width, height)

    def _draw_emblem(self, w: int, h: int) -> None:
        def sx(x: float) -> float:
            return x * w / 48.0

        def sy(y: float) -> float:
            return y * h / 40.0

        self.create_line(sx(1), sy(9), sx(1), sy(1), sx(9), sy(1), fill=BORDER_COLOR, width=1)
        self.create_line(sx(39), sy(39), sx(47), sy(39), sx(47), sy(31), fill=BORDER_COLOR, width=1)

        self.create_polygon(
            sx(5), sy(7),
            sx(17), sy(7),
            sx(24), sy(23),
            sx(31), sy(7),
            sx(43), sy(7),
            sx(27.5), sy(36),
            sx(20.5), sy(36),
            fill=SLATE_SURFACE,
            outline="",
        )
        self.create_polygon(
            sx(7.5), sy(5),
            sx(15.5), sy(5),
            sx(24), sy(24.5),
            sx(24), sy(34.5),
            sx(21.5), sy(34.5),
            fill=ACCENT_CYAN,
            outline="",
        )
        self.create_polygon(
            sx(40.5), sy(5),
            sx(32.5), sy(5),
            sx(24), sy(24.5),
            sx(24), sy(34.5),
            sx(26.5), sy(34.5),
            fill=ACCENT_CYAN_ALT,
            outline="",
        )
        self.create_line(sx(24), sy(24.5), sx(24), sy(35), fill=BG_HEADER, width=1)


class SegmentedProgressBar(tk.Canvas):
    """Flat 20-segment industrial progress meter with crisp 1px border."""

    SEGMENTS = 20

    def __init__(self, master: Any, height: int = 12) -> None:
        super().__init__(
            master,
            height=height,
            bg=BG_RECESSED,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
            bd=0,
        )
        self._ratio = 0.0
        self.bind("<Configure>", lambda _e: self._redraw())

    def set_ratio(self, ratio: float) -> None:
        clamped = max(0.0, min(1.0, float(ratio)))
        if abs(clamped - self._ratio) >= 0.005 or clamped in (0.0, 1.0):
            self._ratio = clamped
            self._redraw()

    def _redraw(self) -> None:
        self.delete("all")
        w = max(80, self.winfo_width())
        h = max(10, self.winfo_height())

        pad_x = 3.0
        pad_y = 2.0
        gap = 2.0
        usable_w = max(20.0, w - 2.0 * pad_x - (self.SEGMENTS - 1) * gap)
        seg_w = usable_w / self.SEGMENTS
        y0 = pad_y
        y1 = max(pad_y + 2.0, h - pad_y)

        lit_count = int(round(self._ratio * self.SEGMENTS))
        if self._ratio >= 0.90:
            active_color = STATUS_CRIT
        elif self._ratio >= 0.75:
            active_color = ACCENT_AMBER
        else:
            active_color = ACCENT_CYAN

        for idx in range(self.SEGMENTS):
            x0 = pad_x + idx * (seg_w + gap)
            x1 = x0 + seg_w
            fill = active_color if idx < lit_count else DIVIDER_COLOR
            self.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")


class SensorRow:
    """Single tabular telemetry row in the HWiNFO64-style Left Sensor Tree."""

    def __init__(self, parent: ctk.CTkFrame, label: str, with_bar: bool = False) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0, height=22)
        self.frame.pack(fill="x", padx=6, pady=1)
        self.frame.grid_columnconfigure(0, weight=5, uniform="sr")
        self.frame.grid_columnconfigure(1, weight=4, uniform="sr")
        self.frame.grid_columnconfigure(2, weight=2, uniform="sr")
        self.frame.grid_columnconfigure(3, weight=2, uniform="sr")

        self.lbl_name = ctk.CTkLabel(
            self.frame,
            text=label,
            font=FONT_UI_SM,
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.lbl_name.grid(row=0, column=0, sticky="ew", padx=(4, 4))

        self.lbl_curr = ctk.CTkLabel(
            self.frame,
            text="      --",
            font=FONT_MONO_BOLD,
            text_color=TEXT_PRIMARY,
            anchor="e",
        )
        self.lbl_curr.grid(row=0, column=1, sticky="ew", padx=4)

        self.lbl_min = ctk.CTkLabel(
            self.frame,
            text="    --",
            font=FONT_MONO_SM,
            text_color=TEXT_MUTED,
            anchor="e",
        )
        self.lbl_min.grid(row=0, column=2, sticky="ew", padx=4)

        self.lbl_max = ctk.CTkLabel(
            self.frame,
            text="    --",
            font=FONT_MONO_SM,
            text_color=TEXT_MUTED,
            anchor="e",
        )
        self.lbl_max.grid(row=0, column=3, sticky="ew", padx=(4, 4))

        self.bar: ctk.CTkProgressBar | None = None
        if with_bar:
            self.bar = ctk.CTkProgressBar(
                self.frame,
                height=4,
                corner_radius=0,
                fg_color=DIVIDER_COLOR,
                progress_color=ACCENT_CYAN,
            )
            self.bar.set(0.0)
            self.bar.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=(1, 2))

    def update(
        self,
        curr_text: str,
        min_text: str = "    --",
        max_text: str = "    --",
        curr_color: str = TEXT_PRIMARY,
        bar_ratio: float | None = None,
    ) -> None:
        self.lbl_curr.configure(text=curr_text, text_color=curr_color)
        self.lbl_min.configure(text=min_text)
        self.lbl_max.configure(text=max_text)
        if self.bar is not None and bar_ratio is not None:
            self.bar.set(max(0.0, min(1.0, bar_ratio)))


class FanArrayRow:
    """Single detected fan channel row with RPM, load %, and segmented meter."""

    def __init__(self, parent: Any, channel_name: str, source_tag: str) -> None:
        self.frame = ctk.CTkFrame(
            parent,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        self.frame.pack(fill="x", padx=2, pady=(0, 5))
        self.frame.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self.frame, fg_color="transparent", corner_radius=0)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(5, 3))
        top.grid_columnconfigure(0, weight=1)

        self.lbl_name = ctk.CTkLabel(
            top,
            text=channel_name,
            font=FONT_MONO_SM_BOLD,
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        self.lbl_name.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.lbl_src = ctk.CTkLabel(
            top,
            text=source_tag,
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            anchor="e",
            width=78,
        )
        self.lbl_src.grid(row=0, column=1, sticky="e", padx=(0, 8))

        self.lbl_rpm = ctk.CTkLabel(
            top,
            text="    0 RPM",
            font=FONT_MONO_BOLD,
            text_color=ACCENT_CYAN,
            anchor="e",
            width=82,
        )
        self.lbl_rpm.grid(row=0, column=2, sticky="e", padx=(0, 8))

        self.lbl_pct = ctk.CTkLabel(
            top,
            text="  0.0 %",
            font=FONT_MONO_SM_BOLD,
            text_color=TEXT_PRIMARY,
            anchor="e",
            width=58,
        )
        self.lbl_pct.grid(row=0, column=3, sticky="e")

        self.seg_bar = SegmentedProgressBar(self.frame, height=12)
        self.seg_bar.grid(row=1, column=0, sticky="ew", padx=8, pady=(0, 6))

    def update(self, channel: FanChannel) -> None:
        self.lbl_name.configure(text=channel.name)
        self.lbl_src.configure(text=channel.pct_source.upper())
        self.lbl_rpm.configure(text=f"{channel.rpm:5.0f} RPM")
        self.lbl_pct.configure(text=f"{channel.load_pct:5.1f} %")
        self.seg_bar.set_ratio(channel.load_pct / 100.0)


class HankoThermalApp(ctk.CTk):
    """Main desktop application window for HANKO Thermal Suite v2.0."""

    def __init__(self, smoke_test: bool = False) -> None:
        super().__init__()
        self._smoke_test = smoke_test
        self._status_queue: queue.Queue[str] = queue.Queue()
        self._throttle_debounce_id: str | None = None
        self._heartbeat_state = False
        self._stress_baseline_temp: float | None = None
        self._fan_rows: list[FanArrayRow] = []
        self._fan_signature: tuple[str, ...] = ()

        ctk.set_appearance_mode("dark")
        self.title("HANKO // THERMAL SUITE v2.0")
        self.geometry("1220x780")
        self.minsize(1100, 700)
        self.configure(fg_color=BG_BASE)

        ico_path = resource_path(os.path.join("assets", "hanko_victus.ico"))
        if ico_path.exists():
            try:
                self.iconbitmap(str(ico_path))
            except Exception:
                pass

        self.engine = HardwareEngine(poll_interval_s=1.0)
        self.engine.set_status_callback(lambda msg: self._status_queue.put(msg))
        self.clock_tuner = ClockTuner()
        self.stress = ThermalStressController()
        self.policy_state: PowerPolicyState = read_power_policy()

        self._gpu_slider_mhz: int = 1355
        self._cpu_slider_ghz: float = 3.20

        self._build_layout()
        self._sync_policy_widgets(self.policy_state)
        self._sync_clock_widgets()

        self.engine.start()
        self._update_ui_loop()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        if self._smoke_test:
            self.after(1500, self._finish_smoke_test)

    def _build_layout(self) -> None:
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

        # === TOP HEADER BAR ===
        header = ctk.CTkFrame(
            self,
            fg_color=BG_HEADER,
            corner_radius=0,
            border_width=0,
            height=60,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        emblem_wrap = tk.Frame(header, bg=BG_HEADER, bd=0, highlightthickness=0)
        emblem_wrap.grid(row=0, column=0, padx=(14, 10), pady=10, sticky="w")
        self.emblem = VictusEmblemCanvas(emblem_wrap, width=44, height=38)
        self.emblem.pack()

        title_col = ctk.CTkFrame(header, fg_color="transparent")
        title_col.grid(row=0, column=1, sticky="w", pady=8)
        ctk.CTkLabel(
            title_col,
            text="HANKO // THERMAL SUITE",
            font=FONT_TITLE,
            text_color=TEXT_PRIMARY,
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_col,
            text=f"v{APP_VERSION}  |  LHM PAWNIO TELEMETRY & DIRECT CLOCK TUNER",
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")

        # Live hardware summary bar in center-right
        summary_box = ctk.CTkFrame(
            header,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        summary_box.grid(row=0, column=2, padx=(8, 10), pady=12, sticky="e")
        self.lbl_hw_summary = ctk.CTkLabel(
            summary_box,
            text="CPU:   --.- °C   -.-- GHz  |  GPU:   -- °C   ---- MHz  |  FANS: 0",
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_CYAN,
            padx=10,
            pady=4,
        )
        self.lbl_hw_summary.pack()

        meta_col = ctk.CTkFrame(header, fg_color="transparent")
        meta_col.grid(row=0, column=3, padx=(0, 14), sticky="e")

        priv_ok = self.engine.static_info.is_admin
        priv_text = "ADMIN PRIVILEGE: VERIFIED" if priv_ok else "ADMIN PRIVILEGE: STANDARD"
        priv_color = STATUS_OK if priv_ok else ACCENT_AMBER
        priv_frame = ctk.CTkFrame(
            meta_col,
            fg_color=BG_RECESSED,
            border_color=priv_color,
            border_width=1,
            corner_radius=2,
        )
        priv_frame.pack(side="left", padx=(0, 6))
        self.lbl_priv = ctk.CTkLabel(
            priv_frame,
            text=priv_text,
            font=FONT_MONO_XS_BOLD,
            text_color=priv_color,
            padx=8,
            pady=3,
        )
        self.lbl_priv.pack()

        # Crisp 1px framed plan badge
        plan_frame = ctk.CTkFrame(
            meta_col,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        plan_frame.pack(side="left", padx=(0, 8))
        scheme = (self.engine.static_info.active_scheme_name or "UNKNOWN").upper()
        self.lbl_plan = ctk.CTkLabel(
            plan_frame,
            text=f"PLAN: {scheme}",
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_CYAN,
            padx=8,
            pady=3,
        )
        self.lbl_plan.pack()

        sys_frame = ctk.CTkFrame(
            meta_col,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        sys_frame.pack(side="left")
        ctk.CTkLabel(
            sys_frame,
            text="SYS: LIVE",
            font=FONT_MONO_XS_BOLD,
            text_color=TEXT_SECONDARY,
            padx=6,
            pady=3,
        ).pack(side="left")
        self.heartbeat_canvas = tk.Canvas(
            sys_frame,
            width=10,
            height=10,
            bg=BG_RECESSED,
            highlightthickness=0,
            bd=0,
        )
        self.heartbeat_canvas.pack(side="left", padx=(0, 6))
        self._hb_rect = self.heartbeat_canvas.create_rectangle(1, 1, 9, 9, fill=ACCENT_CYAN, outline="")

        tk.Frame(self, bg=BORDER_COLOR, height=1).place(in_=header, relx=0, rely=1.0, anchor="sw", relwidth=1.0)

        # === TWO-COLUMN WORKSPACE ===
        body = ctk.CTkFrame(self, fg_color=BG_BASE, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=10, pady=8)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=5, uniform="cols")
        body.grid_columnconfigure(1, weight=6, uniform="cols")

        self._build_left_telemetry_column(body)
        self._build_right_control_column(body)

        # === FOOTER CONSOLE ===
        footer = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=26)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        tk.Frame(footer, bg=BORDER_COLOR, height=1).pack(fill="x", side="top")

        self.lbl_console = ctk.CTkLabel(
            footer,
            text=self._fmt_console("HANKO v2.0 telemetry & clock engine initialized"),
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.lbl_console.pack(fill="x", padx=12, pady=2)

    def _build_left_telemetry_column(self, parent: ctk.CTkFrame) -> None:
        left_col = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 5))
        left_col.grid_columnconfigure(0, weight=1)
        left_col.grid_rowconfigure(0, weight=1)
        left_col.grid_rowconfigure(1, weight=1)

        # === PANEL 1: LIVE HARDWARE SENSOR TREE ===
        sensor_panel = ctk.CTkFrame(
            left_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        sensor_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        sensor_panel.grid_columnconfigure(0, weight=1)
        sensor_panel.grid_rowconfigure(2, weight=1)

        s_top = ctk.CTkFrame(sensor_panel, fg_color=BG_HEADER, corner_radius=0, height=32)
        s_top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        s_top.grid_propagate(False)
        ctk.CTkLabel(
            s_top,
            text="LIVE HARDWARE SENSOR TREE",
            font=FONT_HEADER,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=5)

        ctk.CTkButton(
            s_top,
            text="RESET MIN/MAX",
            width=96,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_RECESSED,
            hover_color=SLATE_SURFACE,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_XS,
            command=self._on_reset_minmax,
        ).pack(side="right", padx=8, pady=5)

        # Column headers
        hdr = ctk.CTkFrame(sensor_panel, fg_color=BG_RECESSED, corner_radius=0, height=22)
        hdr.grid(row=1, column=0, sticky="ew", padx=1)
        hdr.grid_columnconfigure(0, weight=5, uniform="sr")
        hdr.grid_columnconfigure(1, weight=4, uniform="sr")
        hdr.grid_columnconfigure(2, weight=2, uniform="sr")
        hdr.grid_columnconfigure(3, weight=2, uniform="sr")

        for col_idx, (txt, anchor, px) in enumerate(
            [("SENSOR", "w", (10, 4)), ("CURRENT", "e", 4), ("MIN", "e", 4), ("MAX", "e", (4, 10))]
        ):
            ctk.CTkLabel(
                hdr,
                text=txt,
                font=FONT_MONO_XS,
                text_color=TEXT_MUTED,
                anchor=anchor,
            ).grid(row=0, column=col_idx, sticky="ew", padx=px, pady=1)

        tree = ctk.CTkFrame(sensor_panel, fg_color="transparent", corner_radius=0)
        tree.grid(row=2, column=0, sticky="nsew", padx=2, pady=(2, 4))

        info = self.engine.static_info

        cpu_title = f"CPU // {info.cpu_name} ({info.cpu_cores_physical}C/{info.cpu_cores_logical}T)"
        self._section_label(tree, cpu_title)
        self.row_cpu_temp = SensorRow(tree, "Package / ACPI Temp")
        self.row_cpu_clock = SensorRow(tree, "Core Clock (Dynamic)")
        self.row_cpu_load = SensorRow(tree, "Total CPU Load", with_bar=True)
        self.row_cpu_peak = SensorRow(tree, "Peak Thread Load")

        gpu_title = f"GPU // {info.gpu_name}"
        self._section_label(tree, gpu_title)
        self.row_gpu_temp = SensorRow(tree, "GPU Core Temp")
        self.row_gpu_clock = SensorRow(tree, "GPU Core Clock")
        self.row_gpu_power = SensorRow(tree, "GPU Power Draw")
        self.row_gpu_load = SensorRow(tree, "GPU Utilization", with_bar=True)
        self.row_gpu_vram = SensorRow(tree, "VRAM / P-State")

        # === PANEL 2: COOLING & FAN ARRAY ===
        fan_panel = ctk.CTkFrame(
            left_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        fan_panel.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        fan_panel.grid_columnconfigure(0, weight=1)
        fan_panel.grid_rowconfigure(1, weight=1)

        f_top = ctk.CTkFrame(fan_panel, fg_color=BG_HEADER, corner_radius=0, height=32)
        f_top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        f_top.grid_propagate(False)

        ctk.CTkLabel(
            f_top,
            text="COOLING & FAN ARRAY",
            font=FONT_HEADER,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=5)

        self.lbl_fan_count = ctk.CTkLabel(
            f_top,
            text="DETECTED FANS: 0",
            font=FONT_MONO_SM_BOLD,
            text_color=ACCENT_AMBER,
            fg_color=BG_RECESSED,
            corner_radius=2,
            padx=8,
            pady=2,
        )
        self.lbl_fan_count.pack(side="right", padx=8, pady=5)

        self.fan_scroll = ctk.CTkScrollableFrame(
            fan_panel,
            fg_color="transparent",
            corner_radius=0,
            scrollbar_button_color=BORDER_COLOR,
            scrollbar_button_hover_color=SLATE_SURFACE,
        )
        self.fan_scroll.grid(row=1, column=0, sticky="nsew", padx=6, pady=6)

        # === PANEL 3: HARDWARE INVENTORY STRIP ===
        inv = ctk.CTkFrame(
            left_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        inv.grid(row=2, column=0, sticky="ew")
        self.lbl_inventory = ctk.CTkLabel(
            inv,
            text=f"OS: {info.os_version}  |  GPU DRV: {info.gpu_driver}\nTEMP SRC: Initializing  |  FAN BUS: LibreHardwareMonitorLib (PawnIO)",
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            justify="left",
            anchor="w",
        )
        self.lbl_inventory.pack(fill="x", padx=10, pady=6)

    def _section_label(self, parent: ctk.CTkFrame, text: str) -> None:
        bar = ctk.CTkFrame(parent, fg_color=DIVIDER_COLOR, corner_radius=0, height=20)
        bar.pack(fill="x", padx=6, pady=(6, 2))
        bar.pack_propagate(False)
        ctk.CTkLabel(
            bar,
            text=text,
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_CYAN,
            anchor="w",
        ).pack(side="left", padx=6, pady=1)

    def _build_right_control_column(self, parent: ctk.CTkFrame) -> None:
        right_col = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0)
        right_col.grid(row=0, column=1, sticky="nsew", padx=(5, 0))
        right_col.grid_columnconfigure(0, weight=1)
        right_col.grid_rowconfigure(0, weight=1)

        # === MODULE A: CLOCK TUNING & OVERCLOCKING ===
        clock_panel = ctk.CTkFrame(
            right_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        clock_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        clock_panel.grid_columnconfigure(0, weight=1)
        clock_panel.grid_rowconfigure(1, weight=1)

        c_top = ctk.CTkFrame(clock_panel, fg_color=BG_HEADER, corner_radius=0, height=32)
        c_top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        c_top.grid_propagate(False)
        ctk.CTkLabel(
            c_top,
            text="CLOCK TUNING & OVERCLOCKING",
            font=FONT_HEADER,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=5)

        ctk.CTkLabel(
            c_top,
            text="DIRECT FREQUENCY CONTROL",
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_AMBER,
            fg_color=BG_RECESSED,
            corner_radius=2,
            padx=8,
            pady=2,
        ).pack(side="right", padx=8, pady=5)

        tuner_grid = ctk.CTkFrame(clock_panel, fg_color="transparent", corner_radius=0)
        tuner_grid.grid(row=1, column=0, sticky="nsew", padx=8, pady=8)
        tuner_grid.grid_rowconfigure(0, weight=1)
        tuner_grid.grid_columnconfigure(0, weight=1, uniform="tuner")
        tuner_grid.grid_columnconfigure(1, weight=1, uniform="tuner")

        # --- Subpanel Left: GPU CORE CLOCK (MHz) ---
        gpu_card = ctk.CTkFrame(
            tuner_grid,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        gpu_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        gpu_card.grid_columnconfigure(0, weight=1)

        g_hdr = ctk.CTkFrame(gpu_card, fg_color=DIVIDER_COLOR, corner_radius=0, height=26)
        g_hdr.pack(fill="x", padx=1, pady=(1, 6))
        g_hdr.pack_propagate(False)
        ctk.CTkLabel(
            g_hdr,
            text="GPU CORE CLOCK (MHz)",
            font=FONT_MONO_SM_BOLD,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=8)
        self.lbl_gpu_lock_badge = ctk.CTkLabel(
            g_hdr,
            text="STOCK",
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_CYAN,
        )
        self.lbl_gpu_lock_badge.pack(side="right", padx=8)

        g_readouts = ctk.CTkFrame(gpu_card, fg_color=BG_PANEL, border_color=BORDER_COLOR, border_width=1, corner_radius=2)
        g_readouts.pack(fill="x", padx=8, pady=(0, 8))
        g_readouts.grid_columnconfigure((0, 1), weight=1, uniform="gr")

        g_live_cell = ctk.CTkFrame(g_readouts, fg_color="transparent")
        g_live_cell.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(g_live_cell, text="LIVE CLOCK", font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor="w").pack(fill="x")
        self.lbl_gpu_live = ctk.CTkLabel(
            g_live_cell,
            text="---- MHz",
            font=FONT_MONO_VAL,
            text_color=ACCENT_CYAN,
            anchor="e",
        )
        self.lbl_gpu_live.pack(fill="x")

        g_tgt_cell = ctk.CTkFrame(g_readouts, fg_color="transparent")
        g_tgt_cell.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(g_tgt_cell, text="TARGET CLOCK", font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor="w").pack(fill="x")
        self.lbl_gpu_target = ctk.CTkLabel(
            g_tgt_cell,
            text="   STOCK",
            font=FONT_MONO_VAL,
            text_color=ACCENT_AMBER,
            anchor="e",
        )
        self.lbl_gpu_target.pack(fill="x")

        g_sp_row = ctk.CTkFrame(gpu_card, fg_color="transparent")
        g_sp_row.pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(g_sp_row, text="SETPOINT [15 MHz STEP]", font=FONT_MONO_XS, text_color=TEXT_SECONDARY).pack(side="left")
        self.lbl_gpu_setpoint = ctk.CTkLabel(
            g_sp_row,
            text="1355 MHz",
            font=FONT_MONO_SM_BOLD,
            text_color=ACCENT_AMBER,
            anchor="e",
        )
        self.lbl_gpu_setpoint.pack(side="right")

        gpu_ok = self.clock_tuner.nvidia_available
        self.slider_gpu_clock = ctk.CTkSlider(
            gpu_card,
            from_=GPU_CLOCK_MIN_MHZ,
            to=GPU_CLOCK_MAX_MHZ,
            number_of_steps=70,
            height=16,
            corner_radius=2,
            button_corner_radius=2,
            button_length=10,
            fg_color=BORDER_COLOR,
            progress_color=ACCENT_AMBER if gpu_ok else DIVIDER_COLOR,
            button_color=ACCENT_AMBER_HOVER if gpu_ok else TEXT_MUTED,
            button_hover_color=TEXT_PRIMARY,
            state="normal" if gpu_ok else "disabled",
            command=self._on_gpu_slider_drag,
        )
        self.slider_gpu_clock.set(self._gpu_slider_mhz)
        self.slider_gpu_clock.pack(fill="x", padx=8, pady=(2, 0))

        g_ticks = ctk.CTkFrame(gpu_card, fg_color="transparent")
        g_ticks.pack(fill="x", padx=8, pady=(0, 8))
        for idx, tlabel in enumerate(("800", "1150", "1500", "1850")):
            g_ticks.grid_columnconfigure(idx, weight=1)
            anc = "w" if idx == 0 else ("e" if idx == 3 else "center")
            ctk.CTkLabel(g_ticks, text=tlabel, font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor=anc).grid(
                row=0, column=idx, sticky="ew"
            )

        g_btns = ctk.CTkFrame(gpu_card, fg_color="transparent")
        g_btns.pack(fill="x", padx=8, pady=(2, 8))
        g_btns.grid_columnconfigure((0, 1), weight=1, uniform="gb")
        self.btn_gpu_apply = ctk.CTkButton(
            g_btns,
            text="APPLY",
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=ACCENT_AMBER,
            fg_color=SLATE_SURFACE,
            hover_color="#2A2116",
            text_color=ACCENT_AMBER,
            font=FONT_MONO_SM_BOLD,
            state="normal" if gpu_ok else "disabled",
            command=self._on_gpu_apply,
        )
        self.btn_gpu_apply.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self.btn_gpu_reset = ctk.CTkButton(
            g_btns,
            text="RESET STOCK",
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_PANEL,
            hover_color=SLATE_SURFACE,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_SM_BOLD,
            state="normal" if gpu_ok else "disabled",
            command=self._on_gpu_reset,
        )
        self.btn_gpu_reset.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # --- Subpanel Right: CPU MAX FREQUENCY (GHz) ---
        cpu_card = ctk.CTkFrame(
            tuner_grid,
            fg_color=BG_RECESSED,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        cpu_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        cpu_card.grid_columnconfigure(0, weight=1)

        c_hdr = ctk.CTkFrame(cpu_card, fg_color=DIVIDER_COLOR, corner_radius=0, height=26)
        c_hdr.pack(fill="x", padx=1, pady=(1, 6))
        c_hdr.pack_propagate(False)
        ctk.CTkLabel(
            c_hdr,
            text="CPU MAX FREQUENCY (GHz)",
            font=FONT_MONO_SM_BOLD,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=8)
        self.lbl_cpu_cap_badge = ctk.CTkLabel(
            c_hdr,
            text="TURBO AUTO",
            font=FONT_MONO_XS_BOLD,
            text_color=ACCENT_CYAN,
        )
        self.lbl_cpu_cap_badge.pack(side="right", padx=8)

        c_readouts = ctk.CTkFrame(cpu_card, fg_color=BG_PANEL, border_color=BORDER_COLOR, border_width=1, corner_radius=2)
        c_readouts.pack(fill="x", padx=8, pady=(0, 8))
        c_readouts.grid_columnconfigure((0, 1), weight=1, uniform="cr")

        c_live_cell = ctk.CTkFrame(c_readouts, fg_color="transparent")
        c_live_cell.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(c_live_cell, text="LIVE CLOCK", font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor="w").pack(fill="x")
        self.lbl_cpu_live = ctk.CTkLabel(
            c_live_cell,
            text="-.-- GHz",
            font=FONT_MONO_VAL,
            text_color=ACCENT_CYAN,
            anchor="e",
        )
        self.lbl_cpu_live.pack(fill="x")

        c_tgt_cell = ctk.CTkFrame(c_readouts, fg_color="transparent")
        c_tgt_cell.grid(row=0, column=1, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(c_tgt_cell, text="TARGET CLOCK", font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor="w").pack(fill="x")
        self.lbl_cpu_target = ctk.CTkLabel(
            c_tgt_cell,
            text="    AUTO",
            font=FONT_MONO_VAL,
            text_color=ACCENT_AMBER,
            anchor="e",
        )
        self.lbl_cpu_target.pack(fill="x")

        c_sp_row = ctk.CTkFrame(cpu_card, fg_color="transparent")
        c_sp_row.pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(c_sp_row, text="SETPOINT [0.10 GHz STEP]", font=FONT_MONO_XS, text_color=TEXT_SECONDARY).pack(side="left")
        self.lbl_cpu_setpoint = ctk.CTkLabel(
            c_sp_row,
            text="3.20 GHz (3200 MHz)",
            font=FONT_MONO_SM_BOLD,
            text_color=ACCENT_AMBER,
            anchor="e",
        )
        self.lbl_cpu_setpoint.pack(side="right")

        self.slider_cpu_freq = ctk.CTkSlider(
            cpu_card,
            from_=CPU_FREQ_MIN_GHZ,
            to=CPU_FREQ_MAX_GHZ,
            number_of_steps=27,
            height=16,
            corner_radius=2,
            button_corner_radius=2,
            button_length=10,
            fg_color=BORDER_COLOR,
            progress_color=ACCENT_AMBER,
            button_color=ACCENT_AMBER_HOVER,
            button_hover_color=TEXT_PRIMARY,
            command=self._on_cpu_slider_drag,
        )
        self.slider_cpu_freq.set(self._cpu_slider_ghz)
        self.slider_cpu_freq.pack(fill="x", padx=8, pady=(2, 0))

        c_ticks = ctk.CTkFrame(cpu_card, fg_color="transparent")
        c_ticks.pack(fill="x", padx=8, pady=(0, 8))
        for idx, tlabel in enumerate(("1.80", "2.70", "3.60", "4.50")):
            c_ticks.grid_columnconfigure(idx, weight=1)
            anc = "w" if idx == 0 else ("e" if idx == 3 else "center")
            ctk.CTkLabel(c_ticks, text=tlabel, font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor=anc).grid(
                row=0, column=idx, sticky="ew"
            )

        c_btns = ctk.CTkFrame(cpu_card, fg_color="transparent")
        c_btns.pack(fill="x", padx=8, pady=(2, 8))
        c_btns.grid_columnconfigure((0, 1), weight=1, uniform="cb")
        self.btn_cpu_apply = ctk.CTkButton(
            c_btns,
            text="APPLY",
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=ACCENT_AMBER,
            fg_color=SLATE_SURFACE,
            hover_color="#2A2116",
            text_color=ACCENT_AMBER,
            font=FONT_MONO_SM_BOLD,
            command=self._on_cpu_apply,
        )
        self.btn_cpu_apply.grid(row=0, column=0, sticky="ew", padx=(0, 3))

        self.btn_cpu_reset = ctk.CTkButton(
            c_btns,
            text="RESET STOCK",
            height=28,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_PANEL,
            hover_color=SLATE_SURFACE,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_SM_BOLD,
            command=self._on_cpu_reset,
        )
        self.btn_cpu_reset.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # === MODULE B: WINDOWS THERMAL & POWER POLICY ===
        policy_panel = ctk.CTkFrame(
            right_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        policy_panel.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        policy_panel.grid_columnconfigure(0, weight=1)

        p_top = ctk.CTkFrame(policy_panel, fg_color=BG_HEADER, corner_radius=0, height=30)
        p_top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        p_top.grid_propagate(False)
        ctk.CTkLabel(
            p_top,
            text="WINDOWS THERMAL & POWER POLICY [POWERCFG]",
            font=FONT_HEADER,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=4)

        ctk.CTkButton(
            p_top,
            text="SYNC OS POLICY",
            width=102,
            height=20,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_RECESSED,
            hover_color=SLATE_SURFACE,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_XS,
            command=self._on_sync_policy,
        ).pack(side="right", padx=8, pady=4)

        p_body = ctk.CTkFrame(policy_panel, fg_color="transparent")
        p_body.grid(row=1, column=0, sticky="ew", padx=10, pady=8)

        row_t = ctk.CTkFrame(p_body, fg_color="transparent")
        row_t.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            row_t,
            text="MAXIMUM PROCESSOR STATE [PROCTHROTTLEMAX]",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
        ).pack(side="left")
        self.lbl_throttle_val = ctk.CTkLabel(
            row_t,
            text="100%",
            font=FONT_MONO_BOLD,
            text_color=ACCENT_CYAN,
            anchor="e",
            width=48,
        )
        self.lbl_throttle_val.pack(side="right")

        self.slider_throttle = ctk.CTkSlider(
            p_body,
            from_=50,
            to=100,
            number_of_steps=50,
            height=15,
            corner_radius=2,
            button_corner_radius=2,
            button_length=10,
            fg_color=BORDER_COLOR,
            progress_color=ACCENT_CYAN,
            button_color=ACCENT_CYAN_ALT,
            button_hover_color=TEXT_PRIMARY,
            command=self._on_throttle_drag,
        )
        self.slider_throttle.pack(fill="x", pady=(0, 6))
        self.slider_throttle.bind("<ButtonRelease-1>", self._on_throttle_release)

        ctk.CTkLabel(
            p_body,
            text="PROCESSOR BOOST MODE [PERFBOOSTMODE]",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))

        self.seg_boost = ctk.CTkSegmentedButton(
            p_body,
            values=list(BOOST_MODES_BY_LABEL.keys()),
            height=26,
            corner_radius=2,
            border_width=1,
            fg_color=BG_RECESSED,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_CYAN_ALT,
            unselected_color=BG_RECESSED,
            unselected_hover_color=SLATE_SURFACE,
            text_color=TEXT_PRIMARY,
            font=FONT_UI_SM,
            dynamic_resizing=False,
            command=self._on_boost_select,
        )
        self.seg_boost.pack(fill="x", pady=(0, 6))

        ctk.CTkLabel(
            p_body,
            text="SYSTEM COOLING POLICY [SYSCOOLPOL]",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", pady=(0, 2))

        self.seg_cooling = ctk.CTkSegmentedButton(
            p_body,
            values=list(COOLING_POLICIES_BY_LABEL.keys()),
            height=26,
            corner_radius=2,
            border_width=1,
            fg_color=BG_RECESSED,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_CYAN_ALT,
            unselected_color=BG_RECESSED,
            unselected_hover_color=SLATE_SURFACE,
            text_color=TEXT_PRIMARY,
            font=FONT_UI_SM,
            dynamic_resizing=False,
            command=self._on_cooling_select,
        )
        self.seg_cooling.pack(fill="x", pady=(0, 8))

        qbar = ctk.CTkFrame(p_body, fg_color="transparent")
        qbar.pack(fill="x")
        qbar.grid_columnconfigure((0, 1, 2), weight=1, uniform="qp")

        presets = [
            ("QUIET [70% / DIS / PAS]", 70, 0, 0),
            ("BALANCED [95% / EFF / ACT]", 95, 3, 1),
            ("FULL COOL [100% / AGG / ACT]", 100, 2, 1),
        ]
        for col, (label, pct, boost, cool) in enumerate(presets):
            ctk.CTkButton(
                qbar,
                text=label,
                height=24,
                corner_radius=2,
                border_width=1,
                border_color=BORDER_COLOR,
                fg_color=BG_RECESSED,
                hover_color=SLATE_SURFACE,
                text_color=TEXT_SECONDARY,
                font=FONT_MONO_XS,
                command=lambda p=pct, b=boost, c=cool: self._apply_preset(p, b, c),
            ).grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0 if col == 2 else 3))

        # === MODULE C: THERMAL LOAD VERIFICATION ===
        stress_panel = ctk.CTkFrame(
            right_col,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        stress_panel.grid(row=2, column=0, sticky="ew")
        stress_panel.grid_columnconfigure(0, weight=1)

        st_top = ctk.CTkFrame(stress_panel, fg_color="transparent")
        st_top.pack(fill="x", padx=10, pady=(7, 4))
        ctk.CTkLabel(
            st_top,
            text="THERMAL LOAD VERIFICATION (MULTI-CORE STRESS)",
            font=FONT_MONO_XS_BOLD,
            text_color=TEXT_SECONDARY,
        ).pack(side="left")

        self.seg_stress_width = ctk.CTkSegmentedButton(
            st_top,
            values=["25%", "50%", "75%", "100%"],
            width=180,
            height=22,
            corner_radius=2,
            border_width=1,
            fg_color=BG_RECESSED,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_CYAN_ALT,
            unselected_color=BG_RECESSED,
            text_color=TEXT_PRIMARY,
            font=FONT_MONO_XS,
            dynamic_resizing=False,
        )
        self.seg_stress_width.set("100%")
        self.seg_stress_width.pack(side="right")

        st_action = ctk.CTkFrame(stress_panel, fg_color="transparent")
        st_action.pack(fill="x", padx=10, pady=(2, 8))
        st_action.grid_columnconfigure(0, weight=3)
        st_action.grid_columnconfigure(1, weight=2)

        self.btn_stress = ctk.CTkButton(
            st_action,
            text="START MULTI-CORE LOAD TEST",
            height=30,
            corner_radius=2,
            border_width=1,
            border_color=ACCENT_CYAN,
            fg_color=SLATE_SURFACE,
            hover_color="#1A2633",
            text_color=ACCENT_CYAN,
            font=FONT_MONO_BOLD,
            command=self._on_toggle_stress,
        )
        self.btn_stress.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.lbl_stress_stats = ctk.CTkLabel(
            st_action,
            text="ELAPSED 00:00  |  WORKERS  0\nTEMP DELTA:    --",
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            justify="right",
            anchor="e",
        )
        self.lbl_stress_stats.grid(row=0, column=1, sticky="ew")

    # === CLOCK TUNING HANDLERS ===

    def _sync_clock_widgets(self) -> None:
        state = self.clock_tuner.get_state()
        if not state.nvidia_available:
            self.lbl_gpu_lock_badge.configure(text="NO NVIDIA GPU", text_color=TEXT_MUTED)
            self.lbl_gpu_target.configure(text="     N/A", text_color=TEXT_MUTED)
        elif state.target_gpu_mhz is not None:
            self.lbl_gpu_lock_badge.configure(text="LOCKED", text_color=ACCENT_AMBER)
            self.lbl_gpu_target.configure(text=f"{state.target_gpu_mhz:4d} MHz", text_color=ACCENT_AMBER)
        else:
            self.lbl_gpu_lock_badge.configure(text="STOCK", text_color=ACCENT_CYAN)
            self.lbl_gpu_target.configure(text="   STOCK", text_color=TEXT_SECONDARY)

        if state.target_cpu_ghz is not None and state.target_cpu_mhz > 0:
            snapped = snap_cpu_freq_ghz(state.target_cpu_ghz)
            self._cpu_slider_ghz = snapped
            self.slider_cpu_freq.set(snapped)
            self.lbl_cpu_setpoint.configure(text=f"{snapped:4.2f} GHz ({state.target_cpu_mhz:4d} MHz)")
            self.lbl_cpu_cap_badge.configure(text="CAPPED", text_color=ACCENT_AMBER)
            self.lbl_cpu_target.configure(text=f"{snapped:4.2f} GHz", text_color=ACCENT_AMBER)
        else:
            self.lbl_cpu_cap_badge.configure(text="TURBO AUTO", text_color=ACCENT_CYAN)
            self.lbl_cpu_target.configure(text="    AUTO", text_color=TEXT_SECONDARY)

    def _on_gpu_slider_drag(self, val: float) -> None:
        snapped = snap_gpu_clock_mhz(val)
        self._gpu_slider_mhz = snapped
        self.lbl_gpu_setpoint.configure(text=f"{snapped:4d} MHz")

    def _on_gpu_apply(self) -> None:
        target = snap_gpu_clock_mhz(self.slider_gpu_clock.get())
        self._gpu_slider_mhz = target
        self.lbl_gpu_setpoint.configure(text=f"{target:4d} MHz")

        def _task() -> tuple[bool, str]:
            ok, msg = self.clock_tuner.apply_gpu_clock(target)
            self.after(0, self._sync_clock_widgets)
            return ok, msg

        self._run_bg(_task)

    def _on_gpu_reset(self) -> None:
        def _task() -> tuple[bool, str]:
            ok, msg = self.clock_tuner.reset_gpu()
            self.after(0, self._sync_clock_widgets)
            return ok, msg

        self._run_bg(_task)

    def _on_cpu_slider_drag(self, val: float) -> None:
        snapped = snap_cpu_freq_ghz(val)
        self._cpu_slider_ghz = snapped
        mhz = ghz_to_mhz(snapped)
        self.lbl_cpu_setpoint.configure(text=f"{snapped:4.2f} GHz ({mhz:4d} MHz)")

    def _on_cpu_apply(self) -> None:
        snapped = snap_cpu_freq_ghz(self.slider_cpu_freq.get())
        self._cpu_slider_ghz = snapped
        mhz = ghz_to_mhz(snapped)
        self.lbl_cpu_setpoint.configure(text=f"{snapped:4.2f} GHz ({mhz:4d} MHz)")

        def _task() -> tuple[bool, str]:
            ok, msg = self.clock_tuner.apply_cpu_freq(snapped)
            self.after(0, self._sync_clock_widgets)
            return ok, msg

        self._run_bg(_task)

    def _on_cpu_reset(self) -> None:
        def _task() -> tuple[bool, str]:
            ok, msg = self.clock_tuner.reset_cpu()
            self.after(0, self._sync_clock_widgets)
            return ok, msg

        self._run_bg(_task)

    # === POLICY CONTROL HANDLERS ===

    def _sync_policy_widgets(self, state: PowerPolicyState) -> None:
        if state.proc_throttle_max is not None:
            pct = max(50, min(100, state.proc_throttle_max))
            self.slider_throttle.set(pct)
            self.lbl_throttle_val.configure(text=f"{pct:3d}%")
        else:
            self.lbl_throttle_val.configure(text=" N/A")

        if state.perf_boost_mode in BOOST_LABELS:
            self.seg_boost.set(BOOST_LABELS[state.perf_boost_mode])
        elif state.perf_boost_mode is not None:
            fallback = "Aggressive (Full Boost)" if state.perf_boost_mode in (1, 2, 4, 5, 6) else "Disabled (Cool/Quiet)"
            self.seg_boost.set(fallback)

        if state.sys_cool_pol in COOLING_LABELS:
            self.seg_cooling.set(COOLING_LABELS[state.sys_cool_pol])

        if state.scheme_name:
            self.lbl_plan.configure(text=f"PLAN: {state.scheme_name.upper()}")

    def _run_bg(self, fn: Any) -> None:
        def _worker() -> None:
            ok, msg = fn()
            self._status_queue.put(msg if ok else f"ERR: {msg}")

        threading.Thread(target=_worker, daemon=True).start()

    def _on_throttle_drag(self, val: float) -> None:
        pct = int(round(val))
        self.lbl_throttle_val.configure(text=f"{pct:3d}%")
        if self._throttle_debounce_id is not None:
            self.after_cancel(self._throttle_debounce_id)
        self._throttle_debounce_id = self.after(180, lambda p=pct: self._apply_throttle(p))

    def _on_throttle_release(self, _event: Any) -> None:
        if self._throttle_debounce_id is not None:
            self.after_cancel(self._throttle_debounce_id)
            self._throttle_debounce_id = None
        pct = int(round(self.slider_throttle.get()))
        self._apply_throttle(pct)

    def _apply_throttle(self, pct: int) -> None:
        self._throttle_debounce_id = None
        self._run_bg(lambda: set_proc_throttle_max(pct))

    def _on_boost_select(self, label: str) -> None:
        idx = BOOST_MODES_BY_LABEL.get(label)
        if idx is not None:
            self._run_bg(lambda: set_perf_boost_mode(idx))

    def _on_cooling_select(self, label: str) -> None:
        idx = COOLING_POLICIES_BY_LABEL.get(label)
        if idx is not None:
            self._run_bg(lambda: set_sys_cool_pol(idx))

    def _apply_preset(self, pct: int, boost: int, cool: int) -> None:
        self.slider_throttle.set(pct)
        self.lbl_throttle_val.configure(text=f"{pct:3d}%")
        self.seg_boost.set(BOOST_LABELS[boost])
        self.seg_cooling.set(COOLING_LABELS[cool])

        def _batch() -> tuple[bool, str]:
            r1, m1 = set_proc_throttle_max(pct)
            r2, m2 = set_perf_boost_mode(boost)
            r3, m3 = set_sys_cool_pol(cool)
            if r1 and r2 and r3:
                return True, f"Preset applied: {pct}% / {BOOST_LABELS[boost]} / {COOLING_LABELS[cool]}"
            return False, f"{m1} | {m2} | {m3}"

        self._run_bg(_batch)

    def _on_sync_policy(self) -> None:
        def _sync() -> None:
            state = read_power_policy()
            cpu_mhz = self.clock_tuner.sync_cpu_target()
            self.after(0, lambda: self._sync_policy_widgets(state))
            self.after(0, self._sync_clock_widgets)
            self._status_queue.put(
                f"Synced OS powercfg: max={state.proc_throttle_max}%, "
                f"boost={state.perf_boost_mode}, cool={state.sys_cool_pol}, procfreqmax={cpu_mhz}MHz"
            )

        threading.Thread(target=_sync, daemon=True).start()

    # === STRESS TEST HANDLER ===

    def _on_toggle_stress(self) -> None:
        if self.stress.is_running:
            self.stress.stop()
            self._stress_baseline_temp = None
            self.btn_stress.configure(
                text="START MULTI-CORE LOAD TEST",
                border_color=ACCENT_CYAN,
                fg_color=SLATE_SURFACE,
                hover_color="#1A2633",
                text_color=ACCENT_CYAN,
            )
            self._status_queue.put("Thermal load test stopped; worker processes terminated")
        else:
            logical = os.cpu_count() or 4
            pct_str = self.seg_stress_width.get().rstrip("%")
            ratio = int(pct_str) / 100.0
            workers = max(1, round(logical * ratio))
            snap = self.engine.get_snapshot()
            self._stress_baseline_temp = snap.cpu_temp_c if snap.cpu_temp_c is not None else snap.gpu_temp_c
            started = self.stress.start(workers)
            self.btn_stress.configure(
                text=f"STOP LOAD TEST [ACTIVE: {started}T]",
                border_color=STATUS_CRIT,
                fg_color="#2A1619",
                hover_color="#3A1B1F",
                text_color="#FCA5A5",
            )
            self._status_queue.put(f"Thermal load test active across {started}/{logical} logical threads")

    def _on_reset_minmax(self) -> None:
        self.engine.min_max.reset()
        self._status_queue.put("Session MIN/MAX sensor statistics cleared")

    # === LIVE TELEMETRY REFRESH LOOP ===

    def _fmt_console(self, msg: str) -> str:
        stamp = time.strftime("%H:%M:%S")
        return f"[{stamp}] {msg}"

    def _update_ui_loop(self) -> None:
        while not self._status_queue.empty():
            try:
                msg = self._status_queue.get_nowait()
                self.lbl_console.configure(text=self._fmt_console(msg))
            except queue.Empty:
                break

        snap = self.engine.get_snapshot()
        mm = self.engine.min_max

        # Heartbeat indicator
        self._heartbeat_state = not self._heartbeat_state
        self.heartbeat_canvas.itemconfig(
            self._hb_rect,
            fill=ACCENT_CYAN if self._heartbeat_state else SLATE_SURFACE,
        )

        # Header live hardware summary (fixed width)
        cpu_t = f"{snap.cpu_temp_c:5.1f}°C" if snap.cpu_temp_c is not None else "  --.-°C"
        cpu_f = f"{snap.cpu_clock_ghz:4.2f}GHz" if snap.cpu_clock_ghz is not None else "-.--GHz"
        gpu_t = f"{snap.gpu_temp_c:3.0f}°C" if snap.gpu_temp_c is not None else " --°C"
        gpu_f = f"{snap.gpu_clock_mhz:4.0f}MHz" if snap.gpu_clock_mhz is not None else "----MHz"
        fan_cnt = snap.fan_scan.fan_count
        self.lbl_hw_summary.configure(
            text=f"CPU: {cpu_t} {cpu_f}  |  GPU: {gpu_t} {gpu_f}  |  FANS: {fan_cnt:2d}"
        )

        # CPU sensor rows (fixed width, right-aligned)
        cmin, cmax = mm.get("cpu_temp_c")
        self.row_cpu_temp.update(
            _fmt_fixed(snap.cpu_temp_c, "5.1f", " °C", 9, "N/A (Locked)"),
            _fmt_fixed(cmin, "5.1f", "°", 6, "    --"),
            _fmt_fixed(cmax, "5.1f", "°", 6, "    --"),
            curr_color=_temp_color(snap.cpu_temp_c),
        )

        fmin, fmax = mm.get("cpu_clock_ghz")
        self.row_cpu_clock.update(
            _fmt_fixed(snap.cpu_clock_ghz, "4.2f", " GHz", 8),
            _fmt_fixed(fmin, "4.2f", "G", 6, "    --"),
            _fmt_fixed(fmax, "4.2f", "G", 6, "    --"),
            curr_color=ACCENT_CYAN if snap.cpu_clock_ghz is not None else TEXT_MUTED,
        )

        lmin, lmax = mm.get("cpu_load_pct")
        self.row_cpu_load.update(
            f"{snap.cpu_load_pct:5.1f} %",
            _fmt_fixed(lmin, "5.1f", "%", 6, "    --"),
            _fmt_fixed(lmax, "5.1f", "%", 6, "    --"),
            curr_color=ACCENT_AMBER if snap.cpu_load_pct >= 85.0 else TEXT_PRIMARY,
            bar_ratio=snap.cpu_load_pct / 100.0,
        )

        pmin, pmax = mm.get("cpu_peak_core_pct")
        self.row_cpu_peak.update(
            f"{snap.cpu_peak_core_pct:5.1f} %",
            _fmt_fixed(pmin, "5.1f", "%", 6, "    --"),
            _fmt_fixed(pmax, "5.1f", "%", 6, "    --"),
        )

        # GPU sensor rows
        gmin, gmax = mm.get("gpu_temp_c")
        self.row_gpu_temp.update(
            _fmt_fixed(snap.gpu_temp_c, "3.0f", " °C", 7),
            _fmt_fixed(gmin, "3.0f", "°", 5, "    --"),
            _fmt_fixed(gmax, "3.0f", "°", 5, "    --"),
            curr_color=_temp_color(snap.gpu_temp_c),
        )

        gcmin, gcmax = mm.get("gpu_clock_mhz")
        self.row_gpu_clock.update(
            _fmt_fixed(snap.gpu_clock_mhz, "4.0f", " MHz", 8),
            _fmt_fixed(gcmin, "4.0f", "M", 5, "    --"),
            _fmt_fixed(gcmax, "4.0f", "M", 5, "    --"),
            curr_color=ACCENT_CYAN if snap.gpu_clock_mhz is not None else TEXT_MUTED,
        )

        gpmin, gpmax = mm.get("gpu_power_w")
        self.row_gpu_power.update(
            _fmt_fixed(snap.gpu_power_w, "5.1f", " W", 7, "N/A (No PMIC)"),
            _fmt_fixed(gpmin, "5.1f", "W", 6, "    --"),
            _fmt_fixed(gpmax, "5.1f", "W", 6, "    --"),
            curr_color=TEXT_PRIMARY if snap.gpu_power_w is not None else TEXT_MUTED,
        )

        glmin, glmax = mm.get("gpu_load_pct")
        self.row_gpu_load.update(
            _fmt_fixed(snap.gpu_load_pct, "3.0f", " %", 5),
            _fmt_fixed(glmin, "3.0f", "%", 5, "    --"),
            _fmt_fixed(glmax, "3.0f", "%", 5, "    --"),
            bar_ratio=(snap.gpu_load_pct / 100.0) if snap.gpu_load_pct is not None else 0.0,
        )

        if snap.gpu_vram_used_mb is not None and snap.gpu_vram_total_mb is not None:
            vram_text = f"{int(snap.gpu_vram_used_mb):4d}/{int(snap.gpu_vram_total_mb):4d} MB"
            pstate_text = (snap.gpu_pstate or "--").rjust(5)
            self.row_gpu_vram.update(vram_text, pstate_text, "    --")
        else:
            self.row_gpu_vram.update("     N/A", "    --", "    --", curr_color=TEXT_MUTED)

        # Cooling & Fan Array
        self._update_fan_array(snap.fan_scan)

        # Live Clock Readouts in Clock Tuning Panel (fixed width)
        if snap.gpu_clock_mhz is not None:
            self.lbl_gpu_live.configure(text=f"{snap.gpu_clock_mhz:4.0f} MHz", text_color=ACCENT_CYAN)
        else:
            self.lbl_gpu_live.configure(text="---- MHz", text_color=TEXT_MUTED)

        if snap.cpu_clock_ghz is not None:
            self.lbl_cpu_live.configure(text=f"{snap.cpu_clock_ghz:4.2f} GHz", text_color=ACCENT_CYAN)
        else:
            self.lbl_cpu_live.configure(text="-.-- GHz", text_color=TEXT_MUTED)

        # Thermal load test telemetry
        if self.stress.is_running:
            el = int(self.stress.elapsed_seconds)
            mins, secs = divmod(el, 60)
            cur_temp = snap.cpu_temp_c if snap.cpu_temp_c is not None else snap.gpu_temp_c
            if cur_temp is not None and self._stress_baseline_temp is not None:
                delta_str = f"{cur_temp - self._stress_baseline_temp:+5.1f}°C"
            else:
                delta_str = "ACTIVE"
            self.lbl_stress_stats.configure(
                text=f"ELAPSED {mins:02d}:{secs:02d}  |  WORKERS {self.stress.active_workers:2d}\nTEMP DELTA: {delta_str}",
                text_color=ACCENT_AMBER,
            )
        else:
            self.lbl_stress_stats.configure(
                text="ELAPSED 00:00  |  WORKERS  0\nTEMP DELTA:    --",
                text_color=TEXT_MUTED,
            )

        # Inventory strip
        info = self.engine.static_info
        fan_bus = f"{snap.fan_scan.backend} ({snap.fan_scan.fan_count} ch)"
        self.lbl_inventory.configure(
            text=(
                f"OS: {info.os_version}  |  GPU DRV: {info.gpu_driver}\n"
                f"TEMP SRC: {snap.cpu_temp_source}  |  FAN BUS: {fan_bus}"
            )
        )

        self.after(1000, self._update_ui_loop)

    def _update_fan_array(self, fan_scan: FanScanResult) -> None:
        count = fan_scan.fan_count
        self.lbl_fan_count.configure(
            text=f"DETECTED FANS: {count}",
            text_color=ACCENT_CYAN if count > 0 else ACCENT_AMBER,
        )

        if count == 0 or not fan_scan.fans:
            sig = ("__LOCKED__", fan_scan.status_message)
            if sig != self._fan_signature:
                for child in self.fan_scroll.winfo_children():
                    child.destroy()
                self._fan_rows = []
                self._fan_signature = sig

                lock_card = ctk.CTkFrame(
                    self.fan_scroll,
                    fg_color=BG_RECESSED,
                    border_color=ACCENT_AMBER,
                    border_width=1,
                    corner_radius=2,
                )
                lock_card.pack(fill="x", padx=2, pady=2)

                hdr = ctk.CTkFrame(lock_card, fg_color="transparent")
                hdr.pack(fill="x", padx=10, pady=(8, 3))
                ctk.CTkLabel(
                    hdr,
                    text=fan_scan.status_message or EC_LOCKED_MESSAGE,
                    font=FONT_MONO_BOLD,
                    text_color=ACCENT_AMBER,
                    anchor="w",
                ).pack(side="left")
                ctk.CTkLabel(
                    hdr,
                    text="   0 RPM  |  0.0 %",
                    font=FONT_MONO_SM,
                    text_color=TEXT_MUTED,
                    anchor="e",
                ).pack(side="right")

                ctk.CTkLabel(
                    lock_card,
                    text=(
                        f"{fan_scan.backend}: 0 SensorType.Fan channels exposed by firmware.\n"
                        "Tachometer registers are write/read-protected by OEM Embedded Controller."
                    ),
                    font=FONT_MONO_XS,
                    text_color=TEXT_SECONDARY,
                    justify="left",
                    anchor="w",
                ).pack(fill="x", padx=10, pady=(0, 6))

                empty_bar = SegmentedProgressBar(lock_card, height=12)
                empty_bar.pack(fill="x", padx=10, pady=(0, 8))
                empty_bar.set_ratio(0.0)
            return

        sig = tuple(f.identifier for f in fan_scan.fans)
        if sig != self._fan_signature:
            for child in self.fan_scroll.winfo_children():
                child.destroy()
            self._fan_rows = [
                FanArrayRow(self.fan_scroll, f.name, f.pct_source.upper())
                for f in fan_scan.fans
            ]
            self._fan_signature = sig

        for row, channel in zip(self._fan_rows, fan_scan.fans):
            row.update(channel)

    def _finish_smoke_test(self) -> None:
        print("SMOKE_TEST_OK", flush=True)
        self._on_close()

    def _on_close(self) -> None:
        try:
            self.stress.stop()
        finally:
            self.engine.stop()
            self.destroy()


def main() -> None:
    smoke_test = "--smoke-test" in sys.argv
    app = HankoThermalApp(smoke_test=smoke_test)
    app.mainloop()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
