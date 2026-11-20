"""HANKO Thermal Suite — Industrial Windows Hardware Telemetry & Thermal Control Utility."""

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

from hardware_engine import (
    BOOST_LABELS,
    BOOST_MODES_BY_LABEL,
    COOLING_LABELS,
    COOLING_POLICIES_BY_LABEL,
    DEFAULT_FAN_CURVE,
    FanReading,
    HardwareEngine,
    PowerPolicyState,
    SensorSnapshot,
    interpolate_fan_curve,
    read_power_policy,
    set_perf_boost_mode,
    set_proc_throttle_max,
    set_sys_cool_pol,
)
from stress_worker import ThermalStressController

# --- Visual Tokens: Flat Matte-Charcoal Precision Engineering Palette ---
BG_BASE = "#111317"
BG_PANEL = "#181B20"
BG_HEADER = "#15181E"
BG_RECESSED = "#13161B"
BORDER_COLOR = "#262A33"
DIVIDER_COLOR = "#1F232B"
SLATE_POLYGON = "#1E242D"

TEXT_PRIMARY = "#E5E7EB"
TEXT_SECONDARY = "#9CA3AF"
TEXT_MUTED = "#6B7280"

ACCENT_CYAN = "#00B4D8"
ACCENT_BRIGHT = "#38BDF8"

STATUS_OK = "#22C55E"
STATUS_WARN = "#F59E0B"
STATUS_CRIT = "#EF4444"

FONT_MONO = ("Consolas", 11)
FONT_MONO_SM = ("Consolas", 10)
FONT_MONO_XS = ("Consolas", 9)
FONT_MONO_BOLD = ("Consolas", 11, "bold")
FONT_TITLE = ("Consolas", 15, "bold")
FONT_HEADER = ("Segoe UI", 11, "bold")
FONT_UI = ("Segoe UI", 11)
FONT_UI_SM = ("Segoe UI", 10)


def resource_path(relative_path: str) -> Path:
    """Resolve bundled resources cleanly for both Python source and PyInstaller --onefile."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / relative_path


def _fmt_num(val: float | None, fmt: str, suffix: str = "", na_text: str = "N/A") -> str:
    if val is None:
        return na_text
    return f"{format(val, fmt)}{suffix}"


def _temp_color(temp_c: float | None) -> str:
    if temp_c is None:
        return TEXT_MUTED
    if temp_c >= 88.0:
        return STATUS_CRIT
    if temp_c >= 75.0:
        return STATUS_WARN
    return ACCENT_BRIGHT


class VictusEmblemCanvas(tk.Canvas):
    """Razor-sharp architectural Victus 'V' emblem rendered with dual-tone polygons."""

    def __init__(self, master: Any, width: int = 48, height: int = 40) -> None:
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

        # Subtle outer framing ticks
        self.create_line(sx(1), sy(9), sx(1), sy(1), sx(9), sy(1), fill=BORDER_COLOR, width=1)
        self.create_line(sx(39), sy(39), sx(47), sy(39), sx(47), sy(31), fill=BORDER_COLOR, width=1)

        # Matte-slate structural backing facet
        self.create_polygon(
            sx(5), sy(7),
            sx(17), sy(7),
            sx(24), sy(23),
            sx(31), sy(7),
            sx(43), sy(7),
            sx(27.5), sy(36),
            sx(20.5), sy(36),
            fill=SLATE_POLYGON,
            outline="",
        )
        # Left sweeping steel-cyan blade (#00B4D8)
        self.create_polygon(
            sx(7.5), sy(5),
            sx(15.5), sy(5),
            sx(24), sy(24.5),
            sx(24), sy(34.5),
            sx(21.5), sy(34.5),
            fill=ACCENT_CYAN,
            outline="",
        )
        # Right counter-blade (#38BDF8)
        self.create_polygon(
            sx(40.5), sy(5),
            sx(32.5), sy(5),
            sx(24), sy(24.5),
            sx(24), sy(34.5),
            sx(26.5), sy(34.5),
            fill=ACCENT_BRIGHT,
            outline="",
        )
        # Center crisp seam line
        self.create_line(sx(24), sy(24.5), sx(24), sy(35), fill=BG_HEADER, width=1)


class FanCurveCanvas(tk.Canvas):
    """Interactive piecewise linear fan curve editor (30-100C vs 0-100% PWM)."""

    TEMP_MIN = 30.0
    TEMP_MAX = 100.0
    DUTY_MIN = 0.0
    DUTY_MAX = 100.0

    def __init__(
        self,
        master: Any,
        points: list[tuple[float, float]],
        unlocked: bool,
        on_curve_changed: Any,
    ) -> None:
        super().__init__(
            master,
            height=178,
            bg=BG_RECESSED,
            highlightthickness=1,
            highlightbackground=BORDER_COLOR,
            bd=0,
        )
        self.points: list[list[float]] = [[float(t), float(d)] for t, d in points]
        self.unlocked = unlocked
        self.on_curve_changed = on_curve_changed
        self.current_temp: float | None = None
        self._drag_index: int | None = None

        self.pad_l = 38
        self.pad_r = 14
        self.pad_t = 12
        self.pad_b = 24

        self.bind("<Configure>", lambda _e: self.redraw())
        if self.unlocked:
            self.bind("<Button-1>", self._on_press)
            self.bind("<B1-Motion>", self._on_drag)
            self.bind("<ButtonRelease-1>", self._on_release)

    def set_operating_temp(self, temp_c: float | None) -> None:
        self.current_temp = temp_c
        self.redraw()

    def _plot_bounds(self) -> tuple[float, float, float, float]:
        w = max(240, self.winfo_width())
        h = max(140, self.winfo_height())
        return self.pad_l, self.pad_t, w - self.pad_r, h - self.pad_b

    def _data_to_px(self, temp: float, duty: float) -> tuple[float, float]:
        x0, y0, x1, y1 = self._plot_bounds()
        tx = (temp - self.TEMP_MIN) / (self.TEMP_MAX - self.TEMP_MIN)
        dy = (duty - self.DUTY_MIN) / (self.DUTY_MAX - self.DUTY_MIN)
        return x0 + tx * (x1 - x0), y1 - dy * (y1 - y0)

    def _px_to_data(self, px: float, py: float) -> tuple[float, float]:
        x0, y0, x1, y1 = self._plot_bounds()
        tx = max(0.0, min(1.0, (px - x0) / max(1.0, (x1 - x0))))
        dy = max(0.0, min(1.0, (y1 - py) / max(1.0, (y1 - y0))))
        temp = self.TEMP_MIN + tx * (self.TEMP_MAX - self.TEMP_MIN)
        duty = self.DUTY_MIN + dy * (self.DUTY_MAX - self.DUTY_MIN)
        return round(temp, 0), round(duty, 0)

    def _on_press(self, event: tk.Event[Any]) -> None:
        best_idx = None
        best_dist = 18.0
        for idx, (t, d) in enumerate(self.points):
            px, py = self._data_to_px(t, d)
            dist = ((event.x - px) ** 2 + (event.y - py) ** 2) ** 0.5
            if dist <= best_dist:
                best_dist = dist
                best_idx = idx
        self._drag_index = best_idx

    def _on_drag(self, event: tk.Event[Any]) -> None:
        if self._drag_index is None:
            return
        idx = self._drag_index
        t_new, d_new = self._px_to_data(event.x, event.y)
        min_t = self.TEMP_MIN if idx == 0 else self.points[idx - 1][0] + 2.0
        max_t = self.TEMP_MAX if idx == len(self.points) - 1 else self.points[idx + 1][0] - 2.0
        self.points[idx][0] = max(min_t, min(max_t, t_new))
        self.points[idx][1] = max(0.0, min(100.0, d_new))
        self.redraw()

    def _on_release(self, _event: tk.Event[Any]) -> None:
        if self._drag_index is not None:
            self._drag_index = None
            if self.on_curve_changed:
                self.on_curve_changed([(t, d) for t, d in self.points])

    def redraw(self) -> None:
        self.delete("all")
        x0, y0, x1, y1 = self._plot_bounds()

        # Precision grid
        for t_tick in range(30, 101, 10):
            gx, _ = self._data_to_px(t_tick, 0)
            self.create_line(gx, y0, gx, y1, fill=DIVIDER_COLOR, width=1)
            self.create_text(gx, y1 + 11, text=f"{t_tick}C", fill=TEXT_MUTED, font=FONT_MONO_XS)

        for d_tick in (0, 25, 50, 75, 100):
            _, gy = self._data_to_px(30, d_tick)
            self.create_line(x0, gy, x1, gy, fill=DIVIDER_COLOR, width=1)
            self.create_text(x0 - 6, gy, text=f"{d_tick}%", anchor="e", fill=TEXT_MUTED, font=FONT_MONO_XS)

        # Plot border
        self.create_rectangle(x0, y0, x1, y1, outline=BORDER_COLOR, width=1)

        # Curve coordinates (clamped edges + nodes)
        ordered = sorted((t, d) for t, d in self.points)
        curve_pts: list[tuple[float, float]] = [(self.TEMP_MIN, ordered[0][1])]
        curve_pts.extend(ordered)
        curve_pts.append((self.TEMP_MAX, ordered[-1][1]))

        px_pts = [self._data_to_px(t, d) for t, d in curve_pts]
        fill_poly = [(x0, y1), *px_pts, (x1, y1)]
        flat_fill = [coord for pt in fill_poly for coord in pt]
        self.create_polygon(
            flat_fill,
            fill="#15232D" if self.unlocked else "#171A20",
            outline="",
        )

        flat_line = [coord for pt in px_pts for coord in pt]
        self.create_line(
            flat_line,
            fill=ACCENT_CYAN if self.unlocked else "#4B5563",
            width=2,
        )

        # Live operating temperature marker
        if self.current_temp is not None:
            t_clamped = max(self.TEMP_MIN, min(self.TEMP_MAX, self.current_temp))
            op_duty = interpolate_fan_curve(t_clamped, ordered)
            mx, my = self._data_to_px(t_clamped, op_duty)
            self.create_line(mx, y0, mx, y1, fill=ACCENT_BRIGHT if self.unlocked else "#4B5563", dash=(3, 3), width=1)
            self.create_rectangle(mx - 3, my - 3, mx + 3, my + 3, fill=ACCENT_BRIGHT if self.unlocked else TEXT_MUTED, outline=BG_RECESSED)

        # Control points
        for idx, (t, d) in enumerate(ordered):
            px, py = self._data_to_px(t, d)
            fill_c = ACCENT_BRIGHT if self.unlocked else "#4B5563"
            out_c = BG_BASE
            self.create_rectangle(px - 4, py - 4, px + 4, py + 4, fill=fill_c, outline=out_c, width=1)
            if self.unlocked and (idx == self._drag_index):
                self.create_text(px, py - 11, text=f"{int(t)}C:{int(d)}%", fill=ACCENT_BRIGHT, font=FONT_MONO_XS)

        # Honest OEM EC Lock Overlay
        if not self.unlocked:
            cx = (x0 + x1) / 2.0
            cy = (y0 + y1) / 2.0
            self.create_rectangle(cx - 112, cy - 15, cx + 112, cy + 15, fill=BG_PANEL, outline=STATUS_WARN, width=1)
            self.create_text(
                cx,
                cy,
                text="LOCKED BY OEM BIOS EC",
                fill=STATUS_WARN,
                font=("Consolas", 10, "bold"),
            )


class SensorRow:
    """Single tabular telemetry row in the Left Sensor Tree."""

    def __init__(self, parent: ctk.CTkFrame, label: str, with_bar: bool = False) -> None:
        self.frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=0, height=24)
        self.frame.pack(fill="x", padx=8, pady=1)
        self.frame.grid_columnconfigure(0, weight=5, uniform="sr")
        self.frame.grid_columnconfigure(1, weight=3, uniform="sr")
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
            text="--",
            font=FONT_MONO_BOLD,
            text_color=TEXT_PRIMARY,
            anchor="e",
        )
        self.lbl_curr.grid(row=0, column=1, sticky="ew", padx=4)

        self.lbl_min = ctk.CTkLabel(
            self.frame,
            text="--",
            font=FONT_MONO_SM,
            text_color=TEXT_MUTED,
            anchor="e",
        )
        self.lbl_min.grid(row=0, column=2, sticky="ew", padx=4)

        self.lbl_max = ctk.CTkLabel(
            self.frame,
            text="--",
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
                fg_color=BORDER_COLOR,
                progress_color=ACCENT_CYAN,
            )
            self.bar.set(0.0)
            self.bar.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=(1, 3))

    def update(
        self,
        curr_text: str,
        min_text: str = "--",
        max_text: str = "--",
        curr_color: str = TEXT_PRIMARY,
        bar_ratio: float | None = None,
    ) -> None:
        self.lbl_curr.configure(text=curr_text, text_color=curr_color)
        self.lbl_min.configure(text=min_text)
        self.lbl_max.configure(text=max_text)
        if self.bar is not None and bar_ratio is not None:
            self.bar.set(max(0.0, min(1.0, bar_ratio)))


class HankoThermalApp(ctk.CTk):
    """Main desktop application window for HANKO Thermal Suite."""

    def __init__(self, smoke_test: bool = False) -> None:
        super().__init__()
        self._smoke_test = smoke_test
        self._status_queue: queue.Queue[str] = queue.Queue()
        self._throttle_debounce_id: str | None = None
        self._heartbeat_state = False
        self._stress_baseline_temp: float | None = None
        self._fan_rows: list[SensorRow] = []
        self._fan_signature: tuple[str, ...] = ()

        ctk.set_appearance_mode("dark")
        self.title("HANKO // THERMAL SUITE")
        self.geometry("1120x740")
        self.minsize(1020, 680)
        self.configure(fg_color=BG_BASE)

        ico_path = resource_path(os.path.join("assets", "hanko_victus.ico"))
        if ico_path.exists():
            try:
                self.iconbitmap(str(ico_path))
            except Exception:
                pass

        self.engine = HardwareEngine(poll_interval_s=1.0)
        self.engine.set_status_callback(lambda msg: self._status_queue.put(msg))
        self.stress = ThermalStressController()
        self.policy_state: PowerPolicyState = read_power_policy()

        self._build_layout()
        self._sync_policy_widgets(self.policy_state)

        # Start telemetry in background daemon thread immediately so UI never blocks on startup.
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
            height=58,
        )
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(1, weight=1)

        emblem_wrap = tk.Frame(header, bg=BG_HEADER, bd=0, highlightthickness=0)
        emblem_wrap.grid(row=0, column=0, padx=(14, 10), pady=9, sticky="w")
        self.emblem = VictusEmblemCanvas(emblem_wrap, width=48, height=40)
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
            text="HARDWARE TELEMETRY & POWER POLICY ENGINE  |  v1.0.0",
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")

        meta_col = ctk.CTkFrame(header, fg_color="transparent")
        meta_col.grid(row=0, column=2, padx=14, sticky="e")

        priv_text = "PRIV: ADMIN" if self.engine.static_info.is_admin else "PRIV: STANDARD"
        priv_color = STATUS_OK if self.engine.static_info.is_admin else STATUS_WARN
        self.lbl_priv = ctk.CTkLabel(
            meta_col,
            text=priv_text,
            font=FONT_MONO_XS,
            text_color=priv_color,
            fg_color=BG_RECESSED,
            corner_radius=2,
            padx=8,
            pady=3,
        )
        self.lbl_priv.pack(side="left", padx=(0, 8))

        scheme = self.engine.static_info.active_scheme_name or "Unknown"
        self.lbl_plan = ctk.CTkLabel(
            meta_col,
            text=f"PLAN: {scheme.upper()}",
            font=FONT_MONO_XS,
            text_color=ACCENT_BRIGHT,
            fg_color=BG_RECESSED,
            corner_radius=2,
            padx=8,
            pady=3,
        )
        self.lbl_plan.pack(side="left", padx=(0, 8))

        self.heartbeat_canvas = tk.Canvas(meta_col, width=10, height=10, bg=BG_HEADER, highlightthickness=0)
        self.heartbeat_canvas.pack(side="left", padx=(2, 0))
        self._hb_rect = self.heartbeat_canvas.create_rectangle(1, 1, 9, 9, fill=ACCENT_CYAN, outline="")

        header_border = tk.Frame(self, bg=BORDER_COLOR, height=1)
        header_border.place(in_=header, relx=0, rely=1.0, anchor="sw", relwidth=1.0)

        # === TWO-COLUMN WORKSPACE ===
        body = ctk.CTkFrame(self, fg_color=BG_BASE, corner_radius=0)
        body.grid(row=1, column=0, sticky="nsew", padx=12, pady=10)
        body.grid_rowconfigure(0, weight=1)
        body.grid_columnconfigure(0, weight=5, uniform="cols")
        body.grid_columnconfigure(1, weight=6, uniform="cols")

        self._build_left_sensor_column(body)
        self._build_right_control_column(body)

        # === FOOTER CONSOLE ===
        footer = ctk.CTkFrame(self, fg_color=BG_HEADER, corner_radius=0, height=26)
        footer.grid(row=2, column=0, sticky="ew")
        footer.grid_propagate(False)
        tk.Frame(footer, bg=BORDER_COLOR, height=1).pack(fill="x", side="top")

        self.lbl_console = ctk.CTkLabel(
            footer,
            text=self._fmt_console("Telemetry engine initialized; genuine hardware polling active"),
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        )
        self.lbl_console.pack(fill="x", padx=12, pady=2)

    def _build_left_sensor_column(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(
            parent,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        panel.grid_rowconfigure(2, weight=1)
        panel.grid_columnconfigure(0, weight=1)

        # Top bar
        top = ctk.CTkFrame(panel, fg_color=BG_HEADER, corner_radius=0, height=34)
        top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        top.grid_propagate(False)
        ctk.CTkLabel(
            top,
            text="LIVE HARDWARE SENSOR TREE",
            font=FONT_MONO_BOLD,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=6)

        ctk.CTkButton(
            top,
            text="RESET MIN/MAX",
            width=96,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_RECESSED,
            hover_color=SLATE_POLYGON,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_XS,
            command=self._on_reset_minmax,
        ).pack(side="right", padx=8, pady=6)

        # Table Column Headers
        hdr = ctk.CTkFrame(panel, fg_color=BG_RECESSED, corner_radius=0, height=24)
        hdr.grid(row=1, column=0, sticky="ew", padx=1)
        hdr.grid_columnconfigure(0, weight=5, uniform="sr")
        hdr.grid_columnconfigure(1, weight=3, uniform="sr")
        hdr.grid_columnconfigure(2, weight=2, uniform="sr")
        hdr.grid_columnconfigure(3, weight=2, uniform="sr")

        for col_idx, (txt, anchor, px) in enumerate(
            [("SENSOR", "w", (12, 4)), ("CURRENT", "e", 4), ("MIN", "e", 4), ("MAX", "e", (4, 12))]
        ):
            ctk.CTkLabel(
                hdr,
                text=txt,
                font=FONT_MONO_XS,
                text_color=TEXT_MUTED,
                anchor=anchor,
            ).grid(row=0, column=col_idx, sticky="ew", padx=px, pady=2)

        # Scrollable/clean tree container
        tree = ctk.CTkFrame(panel, fg_color="transparent", corner_radius=0)
        tree.grid(row=2, column=0, sticky="nsew", padx=2, pady=4)

        info = self.engine.static_info

        # CPU Group
        cpu_title = f"CPU // {info.cpu_name} ({info.cpu_cores_physical}C/{info.cpu_cores_logical}T)"
        self._section_label(tree, cpu_title)
        self.row_cpu_temp = SensorRow(tree, "Package / ACPI Temp")
        self.row_cpu_clock = SensorRow(tree, "Core Clock (Dynamic)")
        self.row_cpu_load = SensorRow(tree, "Total CPU Load", with_bar=True)
        self.row_cpu_peak = SensorRow(tree, "Peak Thread Load")

        # GPU Group
        gpu_title = f"GPU // {info.gpu_name}"
        self._section_label(tree, gpu_title)
        self.row_gpu_temp = SensorRow(tree, "GPU Core Temp")
        self.row_gpu_clock = SensorRow(tree, "GPU Core Clock")
        self.row_gpu_power = SensorRow(tree, "GPU Power Draw")
        self.row_gpu_load = SensorRow(tree, "GPU Utilization", with_bar=True)
        self.row_gpu_vram = SensorRow(tree, "VRAM / P-State")

        # Fans Group
        self._section_label(tree, "COOLING // DETECTED FAN RPMS")
        self.fan_container = ctk.CTkFrame(tree, fg_color="transparent", corner_radius=0)
        self.fan_container.pack(fill="x")

        # Bottom Hardware Inventory Strip
        inv = ctk.CTkFrame(panel, fg_color=BG_RECESSED, corner_radius=2, border_width=1, border_color=DIVIDER_COLOR)
        inv.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        inv.grid_columnconfigure(0, weight=1)

        pwm_text = info.pwm_backend if info.pwm_control_available else info.pwm_lock_reason
        self.lbl_inventory = ctk.CTkLabel(
            inv,
            text=(
                f"OS: {info.os_version}\n"
                f"GPU DRV: {info.gpu_driver}  |  PWM: {pwm_text}"
            ),
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            justify="left",
            anchor="w",
        )
        self.lbl_inventory.pack(fill="x", padx=8, pady=6)

    def _section_label(self, parent: ctk.CTkFrame, text: str) -> None:
        bar = ctk.CTkFrame(parent, fg_color=DIVIDER_COLOR, corner_radius=0, height=22)
        bar.pack(fill="x", padx=6, pady=(7, 2))
        bar.pack_propagate(False)
        ctk.CTkLabel(
            bar,
            text=text,
            font=("Consolas", 10, "bold"),
            text_color=ACCENT_BRIGHT,
            anchor="w",
        ).pack(side="left", padx=6, pady=1)

    def _build_right_control_column(self, parent: ctk.CTkFrame) -> None:
        panel = ctk.CTkFrame(
            parent,
            fg_color=BG_PANEL,
            border_color=BORDER_COLOR,
            border_width=1,
            corner_radius=2,
        )
        panel.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        top = ctk.CTkFrame(panel, fg_color=BG_HEADER, corner_radius=0, height=34)
        top.grid(row=0, column=0, sticky="ew", padx=1, pady=(1, 0))
        top.grid_propagate(False)
        ctk.CTkLabel(
            top,
            text="DIRECT HARDWARE & THERMAL CONTROLS",
            font=FONT_MONO_BOLD,
            text_color=TEXT_PRIMARY,
        ).pack(side="left", padx=10, pady=6)

        ctk.CTkButton(
            top,
            text="SYNC OS POLICY",
            width=102,
            height=22,
            corner_radius=2,
            border_width=1,
            border_color=BORDER_COLOR,
            fg_color=BG_RECESSED,
            hover_color=SLATE_POLYGON,
            text_color=TEXT_SECONDARY,
            font=FONT_MONO_XS,
            command=self._on_sync_policy,
        ).pack(side="right", padx=8, pady=6)

        # --- Module A: Windows PowerCfg Controls ---
        mod_a = ctk.CTkFrame(panel, fg_color=BG_RECESSED, corner_radius=2, border_width=1, border_color=DIVIDER_COLOR)
        mod_a.grid(row=1, column=0, sticky="ew", padx=10, pady=(10, 6))
        mod_a.grid_columnconfigure(0, weight=1)

        # Max Processor State Slider
        row_t = ctk.CTkFrame(mod_a, fg_color="transparent")
        row_t.pack(fill="x", padx=10, pady=(8, 2))
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
            text_color=ACCENT_BRIGHT,
        )
        self.lbl_throttle_val.pack(side="right")

        self.slider_throttle = ctk.CTkSlider(
            mod_a,
            from_=50,
            to=100,
            number_of_steps=50,
            height=16,
            corner_radius=2,
            button_corner_radius=2,
            button_length=10,
            fg_color=BORDER_COLOR,
            progress_color=ACCENT_CYAN,
            button_color=ACCENT_BRIGHT,
            button_hover_color=TEXT_PRIMARY,
            command=self._on_throttle_drag,
        )
        self.slider_throttle.pack(fill="x", padx=10, pady=(2, 0))
        self.slider_throttle.bind("<ButtonRelease-1>", self._on_throttle_release)

        ticks = ctk.CTkFrame(mod_a, fg_color="transparent")
        ticks.pack(fill="x", padx=10, pady=(0, 6))
        for idx, tlabel in enumerate(("50%", "65%", "80%", "95%", "100%")):
            ticks.grid_columnconfigure(idx, weight=1)
            anc = "w" if idx == 0 else ("e" if idx == 4 else "center")
            ctk.CTkLabel(ticks, text=tlabel, font=FONT_MONO_XS, text_color=TEXT_MUTED, anchor=anc).grid(
                row=0, column=idx, sticky="ew"
            )

        # Processor Boost Mode
        ctk.CTkLabel(
            mod_a,
            text="PROCESSOR BOOST MODE [PERFBOOSTMODE]",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(2, 3))

        self.seg_boost = ctk.CTkSegmentedButton(
            mod_a,
            values=list(BOOST_MODES_BY_LABEL.keys()),
            height=28,
            corner_radius=2,
            border_width=1,
            fg_color=BG_PANEL,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_BRIGHT,
            unselected_color=BG_PANEL,
            unselected_hover_color=SLATE_POLYGON,
            text_color=TEXT_PRIMARY,
            font=FONT_UI_SM,
            dynamic_resizing=False,
            command=self._on_boost_select,
        )
        self.seg_boost.pack(fill="x", padx=10, pady=(0, 8))

        # System Cooling Policy
        ctk.CTkLabel(
            mod_a,
            text="SYSTEM COOLING POLICY [SYSCOOLPOL]",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
            anchor="w",
        ).pack(fill="x", padx=10, pady=(0, 3))

        self.seg_cooling = ctk.CTkSegmentedButton(
            mod_a,
            values=list(COOLING_POLICIES_BY_LABEL.keys()),
            height=28,
            corner_radius=2,
            border_width=1,
            fg_color=BG_PANEL,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_BRIGHT,
            unselected_color=BG_PANEL,
            unselected_hover_color=SLATE_POLYGON,
            text_color=TEXT_PRIMARY,
            font=FONT_UI_SM,
            dynamic_resizing=False,
            command=self._on_cooling_select,
        )
        self.seg_cooling.pack(fill="x", padx=10, pady=(0, 8))

        # Quick Policy Strip
        qbar = ctk.CTkFrame(mod_a, fg_color="transparent")
        qbar.pack(fill="x", padx=10, pady=(0, 8))
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
                fg_color=BG_PANEL,
                hover_color=SLATE_POLYGON,
                text_color=TEXT_SECONDARY,
                font=FONT_MONO_XS,
                command=lambda p=pct, b=boost, c=cool: self._apply_preset(p, b, c),
            ).grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 3, 0 if col == 2 else 3))

        # --- Module B: PWM Fan Duty & Piecewise Curve Editor ---
        mod_b = ctk.CTkFrame(panel, fg_color=BG_RECESSED, corner_radius=2, border_width=1, border_color=DIVIDER_COLOR)
        mod_b.grid(row=2, column=0, sticky="nsew", padx=10, pady=6)
        mod_b.grid_rowconfigure(2, weight=1)
        mod_b.grid_columnconfigure(0, weight=1)

        unlocked = self.engine.static_info.pwm_control_available

        fb_hdr = ctk.CTkFrame(mod_b, fg_color="transparent")
        fb_hdr.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            fb_hdr,
            text="PWM FAN DUTY & PIECEWISE LINEAR CURVE",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
        ).pack(side="left")

        badge_text = "PWM ACTIVE (UNLOCKED)" if unlocked else "LOCKED BY OEM BIOS EC"
        badge_color = STATUS_OK if unlocked else STATUS_WARN
        ctk.CTkLabel(
            fb_hdr,
            text=badge_text,
            font=FONT_MONO_XS,
            text_color=badge_color,
            fg_color=BG_PANEL,
            corner_radius=2,
            padx=6,
            pady=2,
        ).pack(side="right")

        duty_row = ctk.CTkFrame(mod_b, fg_color="transparent")
        duty_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        duty_row.grid_columnconfigure(1, weight=1)

        self.seg_fan_mode = ctk.CTkSegmentedButton(
            duty_row,
            values=["AUTO", "MANUAL", "CURVE"],
            width=180,
            height=24,
            corner_radius=2,
            border_width=1,
            fg_color=BG_PANEL,
            selected_color=ACCENT_CYAN if unlocked else BORDER_COLOR,
            unselected_color=BG_PANEL,
            text_color=TEXT_PRIMARY if unlocked else TEXT_MUTED,
            font=FONT_MONO_XS,
            dynamic_resizing=False,
            state="normal" if unlocked else "disabled",
            command=self._on_fan_mode_select,
        )
        self.seg_fan_mode.set("AUTO")
        self.seg_fan_mode.grid(row=0, column=0, padx=(0, 8))

        self.slider_pwm = ctk.CTkSlider(
            duty_row,
            from_=0,
            to=100,
            number_of_steps=100,
            height=14,
            corner_radius=2,
            button_corner_radius=2,
            button_length=10,
            fg_color=BORDER_COLOR,
            progress_color=ACCENT_CYAN if unlocked else "#374151",
            button_color=ACCENT_BRIGHT if unlocked else "#4B5563",
            state="normal" if unlocked else "disabled",
            command=self._on_pwm_drag,
        )
        self.slider_pwm.set(50)
        self.slider_pwm.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        if unlocked:
            self.slider_pwm.bind("<ButtonRelease-1>", self._on_pwm_release)

        self.lbl_pwm_val = ctk.CTkLabel(
            duty_row,
            text="50%" if unlocked else "EC LOCK",
            width=58,
            font=FONT_MONO_BOLD,
            text_color=ACCENT_BRIGHT if unlocked else STATUS_WARN,
            anchor="e",
        )
        self.lbl_pwm_val.grid(row=0, column=2)

        self.curve_canvas = FanCurveCanvas(
            mod_b,
            points=DEFAULT_FAN_CURVE,
            unlocked=unlocked,
            on_curve_changed=self._on_curve_changed,
        )
        self.curve_canvas.grid(row=2, column=0, sticky="nsew", padx=10, pady=(0, 8))

        # --- Module C: Thermal Load Test ---
        mod_c = ctk.CTkFrame(panel, fg_color=BG_RECESSED, corner_radius=2, border_width=1, border_color=DIVIDER_COLOR)
        mod_c.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        mod_c.grid_columnconfigure(0, weight=1)

        c_top = ctk.CTkFrame(mod_c, fg_color="transparent")
        c_top.pack(fill="x", padx=10, pady=(8, 4))
        ctk.CTkLabel(
            c_top,
            text="THERMAL LOAD TEST (MULTI-CORE STRESS)",
            font=FONT_MONO_XS,
            text_color=TEXT_SECONDARY,
        ).pack(side="left")

        self.seg_stress_width = ctk.CTkSegmentedButton(
            c_top,
            values=["25%", "50%", "75%", "100%"],
            width=180,
            height=22,
            corner_radius=2,
            border_width=1,
            fg_color=BG_PANEL,
            selected_color=ACCENT_CYAN,
            selected_hover_color=ACCENT_BRIGHT,
            unselected_color=BG_PANEL,
            text_color=TEXT_PRIMARY,
            font=FONT_MONO_XS,
            dynamic_resizing=False,
        )
        self.seg_stress_width.set("100%")
        self.seg_stress_width.pack(side="right")

        c_action = ctk.CTkFrame(mod_c, fg_color="transparent")
        c_action.pack(fill="x", padx=10, pady=(2, 8))
        c_action.grid_columnconfigure(0, weight=3)
        c_action.grid_columnconfigure(1, weight=2)

        self.btn_stress = ctk.CTkButton(
            c_action,
            text="START MULTI-CORE LOAD TEST",
            height=32,
            corner_radius=2,
            border_width=1,
            border_color=ACCENT_CYAN,
            fg_color=SLATE_POLYGON,
            hover_color="#232B36",
            text_color=ACCENT_BRIGHT,
            font=FONT_MONO_BOLD,
            command=self._on_toggle_stress,
        )
        self.btn_stress.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        self.lbl_stress_stats = ctk.CTkLabel(
            c_action,
            text="ELAPSED 00:00  |  WORKERS 0\nTEMP DELTA: --",
            font=FONT_MONO_XS,
            text_color=TEXT_MUTED,
            justify="right",
            anchor="e",
        )
        self.lbl_stress_stats.grid(row=0, column=1, sticky="ew")

    # === POLICY CONTROL HANDLERS ===

    def _sync_policy_widgets(self, state: PowerPolicyState) -> None:
        if state.proc_throttle_max is not None:
            pct = max(50, min(100, state.proc_throttle_max))
            self.slider_throttle.set(pct)
            self.lbl_throttle_val.configure(text=f"{pct}%")
        else:
            self.lbl_throttle_val.configure(text="N/A")

        if state.perf_boost_mode in BOOST_LABELS:
            self.seg_boost.set(BOOST_LABELS[state.perf_boost_mode])
        elif state.perf_boost_mode is not None:
            # Windows has additional intermediate boost indices (1,4,5,6); map conservatively.
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
        self.lbl_throttle_val.configure(text=f"{pct}%")
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
        self.lbl_throttle_val.configure(text=f"{pct}%")
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
            self.after(0, lambda: self._sync_policy_widgets(state))
            self._status_queue.put(
                f"Synced OS powercfg: max={state.proc_throttle_max}%, "
                f"boost={state.perf_boost_mode}, cool={state.sys_cool_pol}"
            )

        threading.Thread(target=_sync, daemon=True).start()

    # === PWM & CURVE HANDLERS ===

    def _on_fan_mode_select(self, mode: str) -> None:
        duty = int(round(self.slider_pwm.get()))
        curve = [(float(t), float(d)) for t, d in self.curve_canvas.points]
        self._run_bg(lambda: self.engine.set_pwm_mode(mode, manual_duty=duty, curve_points=curve))

    def _on_pwm_drag(self, val: float) -> None:
        duty = int(round(val))
        self.lbl_pwm_val.configure(text=f"{duty}%")

    def _on_pwm_release(self, _event: Any) -> None:
        duty = int(round(self.slider_pwm.get()))
        self.seg_fan_mode.set("MANUAL")
        self._run_bg(lambda: self.engine.set_pwm_mode("MANUAL", manual_duty=duty))

    def _on_curve_changed(self, points: list[tuple[float, float]]) -> None:
        self.seg_fan_mode.set("CURVE")
        self._run_bg(lambda: self.engine.set_pwm_mode("CURVE", curve_points=points))

    # === STRESS TEST HANDLER ===

    def _on_toggle_stress(self) -> None:
        if self.stress.is_running:
            self.stress.stop()
            self._stress_baseline_temp = None
            self.btn_stress.configure(
                text="START MULTI-CORE LOAD TEST",
                border_color=ACCENT_CYAN,
                fg_color=SLATE_POLYGON,
                hover_color="#232B36",
                text_color=ACCENT_BRIGHT,
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
        # Drain status messages
        while not self._status_queue.empty():
            try:
                msg = self._status_queue.get_nowait()
                self.lbl_console.configure(text=self._fmt_console(msg))
            except queue.Empty:
                break

        snap = self.engine.get_snapshot()
        mm = self.engine.min_max

        # Heartbeat pulse
        self._heartbeat_state = not self._heartbeat_state
        self.heartbeat_canvas.itemconfig(
            self._hb_rect,
            fill=ACCENT_BRIGHT if self._heartbeat_state else SLATE_POLYGON,
        )

        # CPU rows
        cmin, cmax = mm.get("cpu_temp_c")
        self.row_cpu_temp.update(
            _fmt_num(snap.cpu_temp_c, ".1f", " °C", "N/A (Sensor Locked)"),
            _fmt_num(cmin, ".1f", "°"),
            _fmt_num(cmax, ".1f", "°"),
            curr_color=_temp_color(snap.cpu_temp_c),
        )

        fmin, fmax = mm.get("cpu_clock_ghz")
        self.row_cpu_clock.update(
            _fmt_num(snap.cpu_clock_ghz, ".2f", " GHz"),
            _fmt_num(fmin, ".2f", "G"),
            _fmt_num(fmax, ".2f", "G"),
        )

        lmin, lmax = mm.get("cpu_load_pct")
        self.row_cpu_load.update(
            f"{snap.cpu_load_pct:.1f} %",
            _fmt_num(lmin, ".1f", "%"),
            _fmt_num(lmax, ".1f", "%"),
            curr_color=ACCENT_BRIGHT if snap.cpu_load_pct > 80 else TEXT_PRIMARY,
            bar_ratio=snap.cpu_load_pct / 100.0,
        )

        pmin, pmax = mm.get("cpu_peak_core_pct")
        self.row_cpu_peak.update(
            f"{snap.cpu_peak_core_pct:.1f} %",
            _fmt_num(pmin, ".1f", "%"),
            _fmt_num(pmax, ".1f", "%"),
        )

        # GPU rows
        gmin, gmax = mm.get("gpu_temp_c")
        self.row_gpu_temp.update(
            _fmt_num(snap.gpu_temp_c, ".0f", " °C"),
            _fmt_num(gmin, ".0f", "°"),
            _fmt_num(gmax, ".0f", "°"),
            curr_color=_temp_color(snap.gpu_temp_c),
        )

        gcmin, gcmax = mm.get("gpu_clock_mhz")
        self.row_gpu_clock.update(
            _fmt_num(snap.gpu_clock_mhz, ".0f", " MHz"),
            _fmt_num(gcmin, ".0f", "M"),
            _fmt_num(gcmax, ".0f", "M"),
        )

        gpmin, gpmax = mm.get("gpu_power_w")
        self.row_gpu_power.update(
            _fmt_num(snap.gpu_power_w, ".1f", " W", "N/A (Not Exposed)"),
            _fmt_num(gpmin, ".1f", "W"),
            _fmt_num(gpmax, ".1f", "W"),
            curr_color=TEXT_PRIMARY if snap.gpu_power_w is not None else TEXT_MUTED,
        )

        glmin, glmax = mm.get("gpu_load_pct")
        self.row_gpu_load.update(
            _fmt_num(snap.gpu_load_pct, ".0f", " %"),
            _fmt_num(glmin, ".0f", "%"),
            _fmt_num(glmax, ".0f", "%"),
            bar_ratio=(snap.gpu_load_pct / 100.0) if snap.gpu_load_pct is not None else 0.0,
        )

        if snap.gpu_vram_used_mb is not None and snap.gpu_vram_total_mb is not None:
            vram_text = f"{int(snap.gpu_vram_used_mb)} / {int(snap.gpu_vram_total_mb)} MB"
            pstate_text = snap.gpu_pstate or "--"
            self.row_gpu_vram.update(vram_text, pstate_text, "--")
        else:
            self.row_gpu_vram.update("N/A", "--", "--", curr_color=TEXT_MUTED)

        # Fans tree (honest reporting)
        self._update_fan_rows(snap.fans)

        # Update curve temperature cursor
        op_temp = snap.gpu_temp_c if snap.gpu_temp_c is not None else snap.cpu_temp_c
        self.curve_canvas.set_operating_temp(op_temp)

        # Update thermal stress telemetry
        if self.stress.is_running:
            el = int(self.stress.elapsed_seconds)
            mins, secs = divmod(el, 60)
            cur_temp = snap.cpu_temp_c if snap.cpu_temp_c is not None else snap.gpu_temp_c
            if cur_temp is not None and self._stress_baseline_temp is not None:
                delta_str = f"{cur_temp - self._stress_baseline_temp:+.1f}°C"
            else:
                delta_str = "ACTIVE"
            self.lbl_stress_stats.configure(
                text=f"ELAPSED {mins:02d}:{secs:02d}  |  WORKERS {self.stress.active_workers}\nTEMP DELTA: {delta_str}",
                text_color=STATUS_WARN,
            )
        else:
            self.lbl_stress_stats.configure(
                text="ELAPSED 00:00  |  WORKERS 0\nTEMP DELTA: --",
                text_color=TEXT_MUTED,
            )

        # Refresh inventory source summary
        info = self.engine.static_info
        pwm_text = info.pwm_backend if info.pwm_control_available else info.pwm_lock_reason
        self.lbl_inventory.configure(
            text=(
                f"OS: {info.os_version}  |  TEMP SRC: {snap.cpu_temp_source}\n"
                f"GPU DRV: {info.gpu_driver}  |  PWM: {pwm_text}"
            )
        )

        self.after(1000, self._update_ui_loop)

    def _update_fan_rows(self, fans: list[FanReading]) -> None:
        if not fans:
            sig = ("__LOCKED__",)
            if sig != self._fan_signature:
                for w in self.fan_container.winfo_children():
                    w.destroy()
                row = SensorRow(self.fan_container, "Chassis / CPU Fan")
                self._fan_rows = [row]
                self._fan_signature = sig
            self._fan_rows[0].update("N/A (OEM EC Locked)", "--", "--", curr_color=STATUS_WARN)
            return

        sig = tuple(f.name for f in fans)
        if sig != self._fan_signature:
            for w in self.fan_container.winfo_children():
                w.destroy()
            self._fan_rows = [SensorRow(self.fan_container, f.name) for f in fans]
            self._fan_signature = sig

        for row, f in zip(self._fan_rows, fans):
            if f.rpm is not None:
                row.update(f"{int(round(f.rpm))} RPM", "--", "--", curr_color=ACCENT_BRIGHT)
            elif f.duty_pct is not None:
                row.update(f"{f.duty_pct:.0f}% Duty", "--", "--", curr_color=ACCENT_BRIGHT)
            else:
                row.update(f.status or "N/A (OEM EC Locked)", "--", "--", curr_color=STATUS_WARN)

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
