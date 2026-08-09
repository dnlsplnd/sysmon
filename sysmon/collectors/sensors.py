"""Sensor collector: every hwmon rail on the box -- temps, fans, voltages, power."""

from __future__ import annotations

import re
import time
from typing import Any

from .base import Collector
from ..util import listdir, read_int, read_text

# Most hwmon rails are a memory read and cost microseconds. A few are not: an
# NVMe composite temperature is a command round-trip to the drive controller
# and costs milliseconds. Rails slower than this are polled less often so one
# chatty device cannot dominate the sampling budget.
_SLOW_RAIL_SECONDS = 0.002
_SLOW_RAIL_EVERY = 5

# hwmon input files follow "<class><index>_input"; each class has its own scale.
_CLASSES = {
    "temp": {"unit": "°C", "scale": 1000.0, "kind": "temperature"},
    "fan": {"unit": "RPM", "scale": 1.0, "kind": "fan"},
    "in": {"unit": "V", "scale": 1000.0, "kind": "voltage"},
    "curr": {"unit": "A", "scale": 1000.0, "kind": "current"},
    "power": {"unit": "W", "scale": 1000000.0, "kind": "power"},
}

_INPUT_RE = re.compile(r"^(?P<cls>[a-z]+)(?P<index>\d+)_input$")

# Chip names mapped to something a human recognises on this class of hardware.
_CHIP_LABELS = {
    "k10temp": "CPU (AMD)",
    "zenpower": "CPU (AMD)",
    "coretemp": "CPU (Intel)",
    "i915": "GPU (Intel)",
    "xe": "GPU (Intel)",
    "amdgpu": "GPU (AMD)",
    "nvme": "NVMe SSD",
    "drivetemp": "SATA drive",
    "asus": "Mainboard (ASUS)",
    "nct6775": "Mainboard (Nuvoton)",
    "it87": "Mainboard (ITE)",
}


class SensorCollector(Collector):
    name = "sensors"

    def __init__(self) -> None:
        super().__init__()
        self.chips = self._discover()
        self.available = bool(self.chips)
        self._tick = 0
        # Last good value for throttled rails, so they keep reporting on the
        # ticks they are skipped rather than blinking out.
        self._cached: dict[str, float] = {}

    @staticmethod
    def _plausible(raw: int | None, spec: dict[str, Any], scale: float) -> int | None:
        """Reject sentinel thresholds that are not real limits.

        Drives publish "no limit" as an absurd number rather than omitting the
        file -- this NVMe reports temp2_max as 65261 C. Scaling a bar against
        that makes every reading look like nothing, so treat anything outside
        a physically sensible range as absent.
        """
        if raw is None:
            return None
        if spec["kind"] == "temperature":
            celsius = raw / scale
            if not 20.0 <= celsius <= 150.0:
                return None
        return raw

    def _discover(self) -> list[dict[str, Any]]:
        chips = []
        for entry in listdir("/sys/class/hwmon"):
            base = f"/sys/class/hwmon/{entry}"
            chip_name = read_text(f"{base}/name")
            if not chip_name:
                continue

            readings = []
            for filename in listdir(base):
                match = _INPUT_RE.match(filename)
                if not match:
                    continue
                cls = match.group("cls")
                spec = _CLASSES.get(cls)
                if not spec:
                    continue
                # Skip rails the kernel exposes but refuses to read (EACCES on
                # RAPL-style counters, EIO on a powered-down device). Time the
                # probe so slow rails can be identified and throttled.
                probe_started = time.perf_counter()
                if read_int(f"{base}/{filename}") is None:
                    continue
                cost = time.perf_counter() - probe_started
                prefix = filename[: -len("_input")]
                label = read_text(f"{base}/{prefix}_label") or prefix
                scale = spec["scale"]
                # Alarm thresholds are fixed properties of the chip, so read
                # them once here instead of on every tick.
                maximum = self._plausible(read_int(f"{base}/{prefix}_max"), spec, scale)
                critical = self._plausible(read_int(f"{base}/{prefix}_crit"), spec, scale)
                readings.append(
                    {
                        "key": f"{entry}/{prefix}",
                        "path": f"{base}/{filename}",
                        "label": label,
                        "kind": spec["kind"],
                        "unit": spec["unit"],
                        "scale": scale,
                        "max": maximum / scale if maximum else None,
                        "crit": critical / scale if critical else None,
                        "slow": cost > _SLOW_RAIL_SECONDS,
                    }
                )

            if readings:
                readings.sort(key=lambda item: (item["kind"], item["label"]))
                chips.append(
                    {
                        "id": entry,
                        "name": chip_name,
                        "label": _CHIP_LABELS.get(chip_name.split("_")[0], chip_name),
                        "readings": readings,
                    }
                )
        return chips

    def sample(self, now: float) -> dict[str, Any]:
        chips = []
        hottest: tuple[str, float] | None = None
        self._tick += 1
        refresh_slow = self._tick % _SLOW_RAIL_EVERY == 0

        for chip in self.chips:
            values = []
            for reading in chip["readings"]:
                key = reading["key"]
                if reading["slow"] and not refresh_slow and key in self._cached:
                    value = self._cached[key]
                else:
                    raw = read_int(reading["path"])
                    if raw is None:
                        continue
                    value = raw / reading["scale"]
                    if reading["slow"]:
                        self._cached[key] = value
                values.append(
                    {
                        "key": reading["key"],
                        "label": reading["label"],
                        "kind": reading["kind"],
                        "unit": reading["unit"],
                        "value": value,
                        "max": reading["max"],
                        "crit": reading["crit"],
                    }
                )
                if reading["kind"] == "temperature":
                    if hottest is None or value > hottest[1]:
                        hottest = (f"{chip['label']} {reading['label']}", value)

            if values:
                chips.append({**{k: chip[k] for k in ("id", "name", "label")}, "values": values})

        return {
            "chips": chips,
            "hottest": {"label": hottest[0], "value": hottest[1]} if hottest else None,
        }
