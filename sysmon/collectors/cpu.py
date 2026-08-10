"""CPU collector: per-core load, measured frequency, time breakdown, thermals."""

from __future__ import annotations

import os
import re
from typing import Any

from .base import Collector, RateTracker
from ..util import listdir, read_int, read_text

# /proc/stat column order after the "cpuN" label.
_FIELDS = (
    "user",
    "nice",
    "system",
    "idle",
    "iowait",
    "irq",
    "softirq",
    "steal",
    "guest",
    "guest_nice",
)

_CPUFREQ = "/sys/devices/system/cpu/cpu{}/cpufreq"


class CpuCollector(Collector):
    name = "cpu"

    def __init__(self) -> None:
        super().__init__()
        self._prev_cores: dict[str, list[int]] = {}
        self._counters = RateTracker()
        self.core_count = os.cpu_count() or 1
        self.model = self._read_model()
        self.topology = self._read_topology()
        self.freq_source = self._pick_freq_source()
        self.max_freq_mhz = self._read_max_freq()
        self._k10temp = self._find_k10temp()

    # ------------------------------------------------------------- static info

    @staticmethod
    def _read_model() -> str:
        text = read_text("/proc/cpuinfo", "") or ""
        match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        return match.group(1).strip() if match else "Unknown CPU"

    def _read_topology(self) -> dict[str, int]:
        """Physical core count and threads-per-core, derived from sysfs topology."""
        physical: set[tuple[str, str]] = set()
        for cpu in range(self.core_count):
            base = f"/sys/devices/system/cpu/cpu{cpu}/topology"
            package = read_text(f"{base}/physical_package_id")
            core = read_text(f"{base}/core_id")
            if package is not None and core is not None:
                physical.add((package, core))
        cores = len(physical) or self.core_count
        return {
            "threads": self.core_count,
            "cores": cores,
            "threads_per_core": max(1, self.core_count // cores),
        }

    def _pick_freq_source(self) -> str:
        """Choose the most truthful per-CPU frequency file available.

        Order matters. ``cpuinfo_avg_freq`` is derived from the APERF/MPERF
        counters, so it reports the frequency the core *actually ran at*.
        ``scaling_cur_freq`` only reports the P-state the governor last asked
        for, which diverges badly on a machine with a fixed BIOS overclock --
        this box reports ~2.1 GHz there while genuinely running at 3.7 GHz.
        Prefer the measured file whenever the kernel exposes it.
        """
        base = _CPUFREQ.format(0)
        for candidate in ("cpuinfo_avg_freq", "cpuinfo_cur_freq", "scaling_cur_freq"):
            if read_int(f"{base}/{candidate}") is not None:
                return candidate
        return ""

    def _read_max_freq(self) -> float | None:
        base = _CPUFREQ.format(0)
        for candidate in ("cpuinfo_max_freq", "scaling_max_freq"):
            value = read_int(f"{base}/{candidate}")
            if value:
                return value / 1000.0
        return None

    @staticmethod
    def _find_k10temp() -> str | None:
        """Locate the AMD die-temperature hwmon node."""
        for entry in listdir("/sys/class/hwmon"):
            path = f"/sys/class/hwmon/{entry}"
            if (read_text(f"{path}/name") or "") in ("k10temp", "zenpower", "coretemp"):
                return path
        return None

    # ----------------------------------------------------------------- sampling

    def _read_stat(self) -> tuple[dict[str, list[int]], dict[str, int]]:
        text = read_text("/proc/stat", "") or ""
        cores: dict[str, list[int]] = {}
        extras: dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split()
            if not parts:
                continue
            if parts[0].startswith("cpu"):
                cores[parts[0]] = [int(value) for value in parts[1:]]
            elif parts[0] in ("ctxt", "processes", "procs_running", "procs_blocked", "intr"):
                # "intr" carries hundreds of per-IRQ columns; only the total matters.
                extras[parts[0]] = int(parts[1])
        return cores, extras

    def _usage_from(self, key: str, values: list[int]) -> dict[str, float] | None:
        """Convert a jiffy counter row into percentages of the elapsed interval."""
        previous = self._prev_cores.get(key)
        self._prev_cores[key] = values
        if previous is None or len(previous) != len(values):
            return None
        deltas = [max(0, now - was) for now, was in zip(values, previous)]
        total = sum(deltas)
        if total <= 0:
            return None
        named = dict(zip(_FIELDS, deltas))
        # Time spent waiting on I/O is not the CPU doing work, so it counts as
        # idle here -- the same convention htop and top use.
        idle = named.get("idle", 0) + named.get("iowait", 0)
        breakdown = {field: 100.0 * named.get(field, 0) / total for field in _FIELDS}
        breakdown["busy"] = 100.0 * (total - idle) / total
        return breakdown

    def _frequencies(self) -> list[float | None]:
        if not self.freq_source:
            return [None] * self.core_count
        result: list[float | None] = []
        for cpu in range(self.core_count):
            khz = read_int(f"{_CPUFREQ.format(cpu)}/{self.freq_source}")
            result.append(khz / 1000.0 if khz else None)
        return result

    def _temperatures(self) -> dict[str, float]:
        temps: dict[str, float] = {}
        if not self._k10temp:
            return temps
        for entry in listdir(self._k10temp):
            if not entry.endswith("_input"):
                continue
            millidegrees = read_int(f"{self._k10temp}/{entry}")
            if millidegrees is None:
                continue
            label = read_text(f"{self._k10temp}/{entry.replace('_input', '_label')}")
            temps[label or entry.replace("_input", "")] = millidegrees / 1000.0
        return temps

    def sample(self, now: float) -> dict[str, Any]:
        cores, extras = self._read_stat()

        aggregate = self._usage_from("cpu", cores.get("cpu", []))
        per_core: list[float | None] = []
        for index in range(self.core_count):
            usage = self._usage_from(f"cpu{index}", cores.get(f"cpu{index}", []))
            per_core.append(usage["busy"] if usage else None)

        frequencies = self._frequencies()
        known = [freq for freq in frequencies if freq is not None]
        temps = self._temperatures()

        # Counters in /proc/stat are cumulative. Dividing by the elapsed time
        # rather than reporting the raw increment is what makes these per-second
        # figures rather than per-tick ones, which only agree at --interval 1.
        rates: dict[str, float | None] = {}
        for key in ("ctxt", "processes", "intr"):
            value = extras.get(key)
            rates[key] = None if value is None else self._counters.update(key, value, now)

        loadavg = (read_text("/proc/loadavg", "") or "").split()
        uptime_raw = (read_text("/proc/uptime", "") or "0").split()

        return {
            "model": self.model,
            "topology": self.topology,
            "usage": aggregate["busy"] if aggregate else None,
            "breakdown": aggregate,
            "per_core": per_core,
            "freq_mhz": (sum(known) / len(known)) if known else None,
            "freq_per_core": frequencies,
            "freq_max_mhz": self.max_freq_mhz,
            "freq_source": self.freq_source,
            "governor": read_text(f"{_CPUFREQ.format(0)}/scaling_governor"),
            "temps": temps,
            "temp": temps.get("Tdie") or temps.get("Tctl") or (max(temps.values()) if temps else None),
            "loadavg": [float(value) for value in loadavg[:3]] if len(loadavg) >= 3 else [],
            "procs_running": extras.get("procs_running"),
            "procs_blocked": extras.get("procs_blocked"),
            "ctxt_per_s": rates.get("ctxt"),
            "intr_per_s": rates.get("intr"),
            "forks_per_s": rates.get("processes"),
            "uptime": float(uptime_raw[0]) if uptime_raw else None,
        }
