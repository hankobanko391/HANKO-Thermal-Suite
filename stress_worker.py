"""Multi-core thermal load generator for real-time cooling & power policy verification."""

from __future__ import annotations

import atexit
import math
import multiprocessing
import os
import time
from typing import Any


def _thermal_burn_worker(stop_event: Any) -> None:
    """Execute sustained floating-point CPU math until stop_event is signaled."""
    acc = 1.0001234567
    while not stop_event.is_set():
        # Run a tight batch before checking the cross-process event to maximize ALU heat.
        for i in range(1, 4001):
            acc = math.sin(acc + i * 0.0001) * math.cos(acc) + math.sqrt(abs(acc) + 1.137)
            if acc > 1e6:
                acc = 1.0001234567


class ThermalStressController:
    """Lifecycle manager for multi-core CPU thermal stress worker processes."""

    def __init__(self) -> None:
        self._stop_event: Any = None
        self._processes: list[multiprocessing.Process] = []
        self._started_at: float | None = None
        atexit.register(self.stop)

    @property
    def is_running(self) -> bool:
        self._processes = [p for p in self._processes if p.is_alive()]
        if not self._processes:
            self._started_at = None
            return False
        return True

    @property
    def active_workers(self) -> int:
        self._processes = [p for p in self._processes if p.is_alive()]
        return len(self._processes)

    @property
    def elapsed_seconds(self) -> float:
        if not self.is_running or self._started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._started_at)

    def start(self, worker_count: int | None = None) -> int:
        """Spawn worker processes across logical cores; returns active worker count."""
        self.stop()
        logical_total = os.cpu_count() or 4
        if worker_count is None:
            count = logical_total
        else:
            count = max(1, min(int(worker_count), logical_total))

        ctx = multiprocessing.get_context("spawn")
        stop_event = ctx.Event()
        procs: list[multiprocessing.Process] = []
        for idx in range(count):
            proc = ctx.Process(
                target=_thermal_burn_worker,
                args=(stop_event,),
                name=f"HANKOThermalBurn-{idx}",
                daemon=True,
            )
            proc.start()
            procs.append(proc)

        self._stop_event = stop_event
        self._processes = procs
        self._started_at = time.monotonic()
        return len(procs)

    def stop(self) -> None:
        """Signal all stress workers to exit and terminate any stragglers cleanly."""
        if self._stop_event is not None:
            try:
                self._stop_event.set()
            except Exception:
                pass

        deadline = time.monotonic() + 1.5
        for proc in self._processes:
            remaining = max(0.05, deadline - time.monotonic())
            proc.join(timeout=remaining)

        for proc in self._processes:
            if proc.is_alive():
                try:
                    proc.terminate()
                    proc.join(timeout=0.5)
                except Exception:
                    pass

        self._processes = []
        self._stop_event = None
        self._started_at = None
