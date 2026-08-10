"""Sensor collector: every hwmon rail on the box -- temps, fans, voltages, power."""

from __future__ import annotations

import re
import statistics
import time
from collections import deque
from typing import Any

from .base import Collector
from ..util import listdir, read_int, read_text

# Most hwmon rails are a memory read and cost microseconds. A few are not: an
# NVMe composite temperature is a command round-trip to the drive controller
# and costs milliseconds. Rails slower than this are polled less often so one
# chatty device cannot dominate the sampling budget.
_SLOW_RAIL_SECONDS = 0.002
_SLOW_RAIL_EVERY = 5

# Probes per rail when deciding that. One is not enough: the decision is made
# once and then governs every later tick, so a single sample taken during a
# scheduling hiccup throttles a cheap rail for the life of the process. Three
# costs about 27 ms more at startup on a box with ten rails, which is nothing
# next to building the window.
_SLOW_RAIL_PROBES = 3

# Startup timing only measures what a rail *typically* costs, which is the wrong
# question for a rail that is cheap almost always and dreadful now and then --
# this box's Wi-Fi temperature reads in 1 ms and occasionally in 100. Such a rail
# passes the startup check honestly and then dominates the tail anyway. So the
# cost of every unthrottled read is watched, and a rail that exceeds the
# threshold this often within this many reads joins the throttled set.
_SLOW_RAIL_WINDOW = 20
_SLOW_RAIL_STRIKES = 3

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
        # Recent read costs per unthrottled rail, for promotion. Throttled rails
        # are not tracked: they have already been judged.
        self._costs: dict[str, deque[float]] = {}

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

    @staticmethod
    def _probe(path: str) -> float | None:
        """Median seconds to read a rail, or None if it cannot be read.

        Timed repeatedly on purpose. This one measurement decides how often the
        rail is polled for the rest of the process's life, so a single sample is
        at the mercy of whatever the machine was doing in that instant -- start
        the monitor on a busy box and a microsecond-cheap rail can measure slow
        and stay throttled forever.

        The median rather than the mean or the minimum: the first read of an
        NVMe rail costs several times the ones after it, and a mean would let
        that one cold read decide, while a minimum would ignore a rail that is
        usually slow and occasionally cached.
        """
        costs = []
        for _ in range(_SLOW_RAIL_PROBES):
            started = time.perf_counter()
            if read_int(path) is None:
                return None
            costs.append(time.perf_counter() - started)
        return statistics.median(costs)

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
                # RAPL-style counters, EIO on a powered-down device), and time
                # the ones that do read so slow rails can be throttled.
                cost = self._probe(f"{base}/{filename}")
                if cost is None:
                    continue
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

    def _note_cost(self, reading: dict[str, Any], cost: float) -> None:
        """Watch an unthrottled rail, and throttle it if it keeps being slow.

        Judging a rail on how it usually behaves misses the one that is usually
        instant and occasionally blocks for a tenth of a second, so the decision
        made at startup is not the last word.

        Promotion is one way only. A rail that has demonstrated it can stall has
        not stopped being able to, whatever its next few reads look like, and
        demoting it again would only put the spike back on an unpredictable tick.
        """
        window = self._costs.setdefault(reading["key"], deque(maxlen=_SLOW_RAIL_WINDOW))
        window.append(cost)
        if sum(1 for value in window if value > _SLOW_RAIL_SECONDS) >= _SLOW_RAIL_STRIKES:
            reading["slow"] = True
            del self._costs[reading["key"]]

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
                    started = time.perf_counter()
                    raw = read_int(reading["path"])
                    if raw is None:
                        continue
                    # Only unthrottled reads are timed, and the promotion this
                    # may trigger takes effect immediately: the value read here
                    # is then cached by the branch below, so the very next tick
                    # already skips the rail.
                    if not reading["slow"]:
                        self._note_cost(reading, time.perf_counter() - started)
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
