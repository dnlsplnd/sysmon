"""The sampling hub: one worker thread, one snapshot per tick, history on the UI thread."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from gi.repository import GLib

from .collectors import (
    CpuCollector,
    DiskCollector,
    GpuCollector,
    MemoryCollector,
    NetworkCollector,
    ProcessCollector,
    SensorCollector,
)
from .history import History

# Five minutes of one-second samples. Long enough to see a build finish, short
# enough that the deques stay small and Cairo can redraw the whole span cheaply.
DEFAULT_CAPACITY = 300


class Hub:
    """Owns the collectors, the worker thread and the shared history.

    Threading contract, which the rest of the app depends on:

    * :meth:`_run` is the only code on the worker thread. It touches collectors
      and nothing else -- never GTK, never the history buffers.
    * History is updated inside :meth:`_publish`, which GLib runs on the main
      loop, so widgets can read the deques without locking.
    """

    def __init__(self, interval: float = 1.0, capacity: int = DEFAULT_CAPACITY) -> None:
        self.interval = interval
        self.history = History(capacity)
        self.snapshot: dict[str, Any] = {}
        self.tick = 0
        self.started_at = time.time()

        self.cpu = CpuCollector()
        self.memory = MemoryCollector()
        self.disk = DiskCollector()
        self.network = NetworkCollector()
        self.gpu = GpuCollector()
        self.sensors = SensorCollector()
        self.processes = ProcessCollector()

        self._collectors = {
            "cpu": self.cpu,
            "memory": self.memory,
            "disk": self.disk,
            "network": self.network,
            "gpu": self.gpu,
            "sensors": self.sensors,
            "processes": self.processes,
        }

        self._subscribers: list[Callable[[dict[str, Any]], None]] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._sample_ms = 0.0

    # -------------------------------------------------------------- lifecycle

    def subscribe(self, callback: Callable[[dict[str, Any]], None]) -> None:
        """Register a main-thread callback invoked with each new snapshot."""
        self._subscribers.append(callback)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="sysmon-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2.0)

    def set_interval(self, seconds: float) -> None:
        self.interval = max(0.2, seconds)
        # Cut the current sleep short so the new rate takes effect immediately.
        self._wake.set()

    # ----------------------------------------------------------------- worker

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.perf_counter()
            now = time.time()

            data: dict[str, Any] = {}
            for name, collector in self._collectors.items():
                data[name] = collector.safe_sample(now)

            data["meta"] = {
                "time": now,
                "tick": self.tick,
                "interval": self.interval,
                "sample_ms": (time.perf_counter() - started) * 1000.0,
                "uptime": now - self.started_at,
            }
            self._sample_ms = data["meta"]["sample_ms"]
            self.tick += 1

            GLib.idle_add(self._publish, data, priority=GLib.PRIORITY_DEFAULT_IDLE)

            # Subtract the work we just did so the cadence stays on the wall
            # clock instead of drifting by however long sampling took.
            delay = max(0.05, self.interval - (time.perf_counter() - started))
            self._wake.wait(delay)
            self._wake.clear()

    # ------------------------------------------------------- main-thread side

    def _publish(self, data: dict[str, Any]) -> bool:
        self.snapshot = data
        try:
            self._record(data)
        except Exception:  # noqa: BLE001 - history must never break the UI
            pass
        for callback in list(self._subscribers):
            try:
                callback(data)
            except Exception:  # noqa: BLE001 - one bad page must not stop the rest
                import traceback

                traceback.print_exc()
        return GLib.SOURCE_REMOVE

    def _record(self, data: dict[str, Any]) -> None:
        """Fan the snapshot out into the named series the graphs read."""
        history = self.history
        # Everything below contributes exactly one sample to this frame.
        history.begin_frame()

        cpu = data.get("cpu", {})
        history.push("cpu.usage", cpu.get("usage"))
        history.push("cpu.freq", cpu.get("freq_mhz"))
        history.push("cpu.temp", cpu.get("temp"))
        breakdown = cpu.get("breakdown") or {}
        history.push("cpu.user", breakdown.get("user"))
        history.push("cpu.system", breakdown.get("system"))
        history.push("cpu.iowait", breakdown.get("iowait"))
        for index, value in enumerate(cpu.get("per_core") or []):
            history.push(f"cpu.core{index}", value)

        memory = data.get("memory", {})
        history.push("mem.percent", memory.get("percent"))
        history.push("mem.used", memory.get("used"))
        history.push("mem.cached", memory.get("cached"))
        history.push("mem.swap", memory.get("swap_percent"))

        network = data.get("network", {})
        totals = network.get("totals") or {}
        history.push("net.rx", totals.get("rx_bps"))
        history.push("net.tx", totals.get("tx_bps"))
        for interface in network.get("interfaces") or []:
            history.push(f"net.{interface['name']}.rx", interface.get("rx_bps"))
            history.push(f"net.{interface['name']}.tx", interface.get("tx_bps"))

        disk = data.get("disk", {})
        read_total = write_total = 0.0
        for device in disk.get("devices") or []:
            history.push(f"disk.{device['name']}.read", device.get("read_bps"))
            history.push(f"disk.{device['name']}.write", device.get("write_bps"))
            history.push(f"disk.{device['name']}.util", device.get("utilisation"))
            read_total += device.get("read_bps") or 0.0
            write_total += device.get("write_bps") or 0.0
        history.push("disk.read", read_total)
        history.push("disk.write", write_total)

        gpu = data.get("gpu", {})
        for card in gpu.get("cards") or []:
            key = card["card"]
            history.push(f"gpu.{key}.busy", card.get("busy"))
            history.push(f"gpu.{key}.freq", card.get("freq_mhz"))
            history.push(f"gpu.{key}.temp", card.get("temp"))
            history.push(f"gpu.{key}.power", card.get("power_w"))
            history.push(f"gpu.{key}.vram", card.get("vram_used"))
            for engine, value in (card.get("engine_raw") or {}).items():
                history.push(f"gpu.{key}.engine.{engine}", value)

        for chip in (data.get("sensors", {}) or {}).get("chips") or []:
            for reading in chip["values"]:
                if reading["kind"] in ("temperature", "fan"):
                    history.push(f"sensor.{reading['key']}", reading["value"])

    # ------------------------------------------------------------------ misc

    @property
    def sample_cost_ms(self) -> float:
        return self._sample_ms
