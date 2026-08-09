"""GPU collector for Intel (i915/xe) via DRM fdinfo -- the same source intel_gpu_top uses.

Engine busy time and video memory are published per open DRM file under
``/proc/<pid>/fdinfo/<fd>``. Reading them needs no root and no perf events,
which is why this is preferred over the i915 PMU.
"""

from __future__ import annotations

import os
import re
from typing import Any

from .base import Collector, RateTracker
from ..util import listdir, read_int, read_text

_PROC = "/proc"
_ENGINE_RE = re.compile(r"^drm-engine-(?P<engine>[a-z-]+):\s+(?P<ns>\d+)\s*ns$")
_CAPACITY_RE = re.compile(r"^drm-engine-capacity-(?P<engine>[a-z-]+):\s+(?P<n>\d+)$")
_MEM_RE = re.compile(r"^drm-(?P<kind>resident|total|active)-(?P<region>[a-z]+)\d*:\s+(?P<value>\d+)\s*(?P<unit>\w+)?$")

_UNIT_SCALE = {"KiB": 1024, "MiB": 1024**2, "GiB": 1024**3, None: 1, "": 1}

# Friendly names for the engine classes i915 exposes.
_ENGINE_LABELS = {
    "render": "Render/3D",
    "copy": "Blitter",
    "video": "Video",
    "video-enhance": "Video enhance",
    "compute": "Compute",
}


class GpuCollector(Collector):
    name = "gpu"

    # A full /proc walk readlinks every descriptor of every process, which is
    # the single most expensive thing this collector does. Processes holding a
    # DRM file are rare and long-lived (compositor, browser, players), so the
    # known set is re-checked every tick and rediscovery runs periodically.
    _DISCOVERY_EVERY = 4

    def __init__(self) -> None:
        super().__init__()
        self._rates = RateTracker()
        self.cards = self._discover()
        self.available = bool(self.cards)
        if not self.available:
            self.error = "no supported DRM card found"
        self._known_pids: set[int] = set()
        self._scan_tick = 0

    # -------------------------------------------------------------- discovery

    def _discover(self) -> list[dict[str, Any]]:
        cards = []
        for entry in listdir("/sys/class/drm"):
            if not re.fullmatch(r"card\d+", entry):
                continue
            base = f"/sys/class/drm/{entry}"
            driver_link = f"{base}/device/driver"
            try:
                driver = os.path.basename(os.readlink(driver_link))
            except OSError:
                continue
            if driver not in ("i915", "xe"):
                continue

            pdev = None
            try:
                pdev = os.path.basename(os.readlink(f"{base}/device"))
            except OSError:
                pass

            cards.append(
                {
                    "card": entry,
                    "driver": driver,
                    "pdev": pdev,
                    "sysfs": base,
                    "name": self._model_name(base, pdev),
                    "hwmon": self._hwmon(base),
                    "freq_paths": self._freq_paths(base),
                    "engines": [e for e in listdir(f"{base}/engine")],
                    "lmem_aperture": self._bar_size(f"{base}/device/resource", index=2),
                }
            )
        return cards

    @staticmethod
    def _model_name(base: str, pdev: str | None) -> str:
        """Best-effort marketing name; falls back to the PCI device ID."""
        device_id = read_text(f"{base}/device/device") or ""
        # A small table beats shelling out to lspci on every start.
        known = {
            "0x56a5": "Intel Arc A380",
            "0x56a6": "Intel Arc A310",
            "0x56a0": "Intel Arc A770",
            "0x56a1": "Intel Arc A750",
            "0x5690": "Intel Arc A770M",
        }
        if device_id in known:
            return known[device_id]
        return f"Intel GPU {device_id or pdev or '?'}"

    @staticmethod
    def _hwmon(base: str) -> str | None:
        for entry in listdir(f"{base}/device/hwmon"):
            if entry.startswith("hwmon"):
                return f"{base}/device/hwmon/{entry}"
        return None

    @staticmethod
    def _freq_paths(base: str) -> dict[str, str]:
        """Locate frequency files, which moved between i915 generations.

        Older i915 puts them flat on the card (``gt_act_freq_mhz``); newer
        multi-tile builds nest them under ``gt/gt0/rps_*``. Probe both.
        """
        candidates = {
            "actual": [f"{base}/gt_act_freq_mhz", f"{base}/gt/gt0/rps_act_freq_mhz"],
            "requested": [f"{base}/gt_cur_freq_mhz", f"{base}/gt/gt0/rps_cur_freq_mhz"],
            "max": [f"{base}/gt_RP0_freq_mhz", f"{base}/gt/gt0/rps_RP0_freq_mhz"],
            "min": [f"{base}/gt_RPn_freq_mhz", f"{base}/gt/gt0/rps_RPn_freq_mhz"],
        }
        found = {}
        for key, paths in candidates.items():
            for path in paths:
                if read_int(path) is not None:
                    found[key] = path
                    break
        return found

    @staticmethod
    def _bar_size(resource_path: str, index: int) -> int | None:
        """Size of a PCI BAR, used to report the local-memory aperture."""
        text = read_text(resource_path)
        if not text:
            return None
        lines = text.splitlines()
        if index >= len(lines):
            return None
        parts = lines[index].split()
        if len(parts) < 2:
            return None
        try:
            start, end = int(parts[0], 16), int(parts[1], 16)
        except ValueError:
            return None
        return (end - start + 1) if end > start else None

    # ------------------------------------------------------------ fdinfo scan

    def _scan_fdinfo(self, pdev: str | None) -> tuple[dict[str, int], dict[str, int], dict[int, dict[str, Any]], dict[str, int]]:
        """Walk /proc for DRM clients belonging to one card.

        Returns cumulative engine nanoseconds, engine capacities, per-PID usage
        and memory totals.

        The critical correctness detail is de-duplication by ``drm-client-id``:
        a process can hold the same DRM client on several file descriptors, and
        descriptors can be passed between processes. Counting a client once per
        fd would multiply busy time and VRAM by the number of duplicate fds.
        """
        engine_ns: dict[str, int] = {}
        capacities: dict[str, int] = {}
        memory: dict[str, int] = {}
        per_pid: dict[int, dict[str, Any]] = {}
        seen_clients: set[str] = set()

        # Rediscover the whole process table periodically; in between, re-read
        # only the processes already known to hold a DRM file. A client that
        # starts between discovery passes is picked up within a few seconds,
        # and because rate tracking returns None on a key's first sighting, a
        # late arrival never shows up as a spike.
        self._scan_tick += 1
        full_scan = (
            not self._known_pids or self._scan_tick % self._DISCOVERY_EVERY == 0
        )
        candidates = os.listdir(_PROC) if full_scan else [str(p) for p in self._known_pids]
        found_pids: set[int] = set()

        for pid_entry in candidates:
            if not pid_entry.isdigit():
                continue
            pid = int(pid_entry)
            fd_dir = f"{_PROC}/{pid_entry}/fd"
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                # Process exited mid-scan, or it is not ours to inspect.
                continue

            for fd in fds:
                # Checking the symlink target first is far cheaper than opening
                # and parsing fdinfo for every descriptor a process holds.
                try:
                    target = os.readlink(f"{fd_dir}/{fd}")
                except OSError:
                    continue
                if not target.startswith("/dev/dri/"):
                    continue

                content = read_text(f"{_PROC}/{pid_entry}/fdinfo/{fd}")
                if not content or "drm-client-id" not in content:
                    continue

                fields = dict(
                    line.split(":", 1) for line in content.splitlines() if ":" in line
                )
                if pdev and fields.get("drm-pdev", "").strip() not in ("", pdev):
                    continue

                found_pids.add(pid)

                client_id = fields.get("drm-client-id", "").strip()
                key = f"{fields.get('drm-pdev', '').strip()}/{client_id}"
                if not client_id or key in seen_clients:
                    continue
                seen_clients.add(key)

                entry = per_pid.setdefault(pid, {"pid": pid, "engines": {}, "vram": 0})

                for line in content.splitlines():
                    match = _ENGINE_RE.match(line.strip())
                    if match:
                        engine = match.group("engine")
                        nanoseconds = int(match.group("ns"))
                        engine_ns[engine] = engine_ns.get(engine, 0) + nanoseconds
                        entry["engines"][engine] = entry["engines"].get(engine, 0) + nanoseconds
                        continue

                    match = _CAPACITY_RE.match(line.strip())
                    if match:
                        capacities[match.group("engine")] = int(match.group("n"))
                        continue

                    match = _MEM_RE.match(line.strip())
                    if match and match.group("kind") == "resident":
                        scale = _UNIT_SCALE.get(match.group("unit"), 1)
                        region = match.group("region")
                        size = int(match.group("value")) * scale
                        memory[region] = memory.get(region, 0) + size
                        if region == "local":
                            entry["vram"] += size

        if full_scan:
            self._known_pids = found_pids
        else:
            # Between discovery passes, only drop clients that went away.
            self._known_pids &= found_pids

        return engine_ns, capacities, per_pid, memory

    # ----------------------------------------------------------------- sampling

    def sample(self, now: float) -> dict[str, Any]:
        cards = []
        for card in self.cards:
            engine_ns, capacities, per_pid, memory = self._scan_fdinfo(card["pdev"])

            # Convert cumulative engine nanoseconds into a percentage of wall time.
            elapsed = self._rates.delta((card["card"], "clock"), now, now)
            engines: dict[str, float | None] = {}
            for engine, nanoseconds in engine_ns.items():
                busy_ns = self._rates.delta((card["card"], engine), nanoseconds, now)
                if busy_ns is None or not elapsed or elapsed <= 0:
                    engines[engine] = None
                    continue
                # An engine class with capacity N can accumulate N ns of busy
                # time per ns of wall clock, so normalise by that capacity.
                capacity = max(1, capacities.get(engine, 1))
                engines[engine] = min(100.0, 100.0 * busy_ns / (elapsed * 1e9 * capacity))

            busy_values = [value for value in engines.values() if value is not None]

            hwmon = card["hwmon"]
            power_w = None
            temp = None
            fan_rpm = None
            power_limit = None
            if hwmon:
                microjoules = read_int(f"{hwmon}/energy1_input")
                if microjoules is not None:
                    joules = self._rates.delta((card["card"], "energy"), microjoules / 1e6, now)
                    if joules is not None and elapsed and elapsed > 0:
                        power_w = joules / elapsed
                millidegrees = read_int(f"{hwmon}/temp1_input")
                temp = millidegrees / 1000.0 if millidegrees is not None else None
                fan_rpm = read_int(f"{hwmon}/fan1_input")
                microwatts = read_int(f"{hwmon}/power1_max")
                power_limit = microwatts / 1e6 if microwatts else None

            freq = {
                key: read_int(path) for key, path in card["freq_paths"].items()
            }
            # The actual-frequency file reads 0 whenever the sample lands while
            # the GPU is power-gated (RC6), which happens constantly even on a
            # busy card. That is an artefact of sampling an instantaneous value,
            # not evidence the GPU ran at 0 Hz for the whole interval, so fall
            # back to the requested clock and flag it rather than reporting 0.
            actual = freq.get("actual")
            requested = freq.get("requested")
            gated = actual == 0
            effective = actual if actual else requested

            # Resolve names here rather than leaning on the process collector:
            # its full table is usually switched off, and there are only a
            # handful of GPU clients, so reading comm directly is cheap.
            processes = [
                {
                    "pid": entry["pid"],
                    "name": read_text(f"{_PROC}/{entry['pid']}/comm") or "?",
                    "vram": entry["vram"],
                    "engines": entry["engines"],
                }
                for entry in per_pid.values()
            ]

            cards.append(
                {
                    "name": card["name"],
                    "driver": card["driver"],
                    "card": card["card"],
                    "pdev": card["pdev"],
                    # The headline number is the busiest engine, not the sum:
                    # engines run in parallel, so summing would exceed 100%.
                    "busy": max(busy_values) if busy_values else None,
                    "engines": {
                        _ENGINE_LABELS.get(key, key): value for key, value in engines.items()
                    },
                    "engine_raw": engines,
                    "vram_used": memory.get("local"),
                    "system_used": memory.get("system"),
                    "lmem_aperture": card["lmem_aperture"],
                    "freq_mhz": effective,
                    "freq_actual_mhz": actual,
                    "freq_gated": gated,
                    "freq_requested_mhz": requested,
                    "freq_max_mhz": freq.get("max"),
                    "freq_min_mhz": freq.get("min"),
                    "temp": temp,
                    "power_w": power_w,
                    "power_limit_w": power_limit,
                    "fan_rpm": fan_rpm,
                    "clients": len(per_pid),
                    "processes": processes,
                }
            )

        return {"cards": cards, "available": bool(cards)}
