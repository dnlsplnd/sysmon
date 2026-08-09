"""Disk collector: per-device throughput/IOPS/utilisation, filesystems, health."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

import psutil

from .base import Collector, RateTracker
from ..util import listdir, read_int, read_text

# /proc/diskstats always reports in 512-byte sectors regardless of the device's
# real logical block size -- this is a fixed kernel ABI, not a device property.
_SECTOR_BYTES = 512

# Column offsets within a /proc/diskstats line, after major/minor/name.
_READS_DONE = 0
_SECTORS_READ = 2
_MS_READING = 3
_WRITES_DONE = 4
_SECTORS_WRITTEN = 6
_MS_WRITING = 7
_IOS_IN_PROGRESS = 8
_IO_TICKS = 9

_HWMON_RE = re.compile(r"hwmon\d+")

# Ticks between drive-temperature refreshes; see DiskCollector._temperature.
_TEMP_EVERY = 5


class DiskCollector(Collector):
    name = "disk"

    def __init__(self) -> None:
        super().__init__()
        self._rates = RateTracker()
        self._smart_cache: dict[str, dict[str, Any]] = {}
        self._temps: dict[str, float] = {}
        self._temp_tick = 0
        self.devices = self._discover()

    # -------------------------------------------------------------- discovery

    @staticmethod
    def _is_physical(name: str) -> bool:
        """Keep real block devices; drop partitions, loopbacks and device-mapper.

        A device is "whole" if sysfs gives it a queue directory and it is not
        itself a partition (partitions carry a `partition` file).
        """
        base = f"/sys/block/{name}"
        if name.startswith(("loop", "ram", "dm-", "sr")):
            return False
        return read_text(f"{base}/queue/logical_block_size") is not None

    def _discover(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for name in listdir("/sys/block"):
            if not self._is_physical(name):
                continue
            base = f"/sys/block/{name}"
            sectors = read_int(f"{base}/size") or 0
            if sectors == 0:
                # A card reader with no media, or a drive that just went away.
                # There is nothing to monitor until it reports a size.
                continue
            rotational = read_int(f"{base}/queue/rotational")
            model = (
                read_text(f"{base}/device/model")
                or read_text(f"{base}/device/name")
                or ""
            ).strip()
            found[name] = {
                "name": name,
                "model": model or name,
                "size": sectors * _SECTOR_BYTES,
                "rotational": bool(rotational),
                "kind": self._kind(name, rotational),
                "scheduler": self._scheduler(base),
                "hwmon": self._hwmon_for(name),
            }
        return found

    @staticmethod
    def _kind(name: str, rotational: int | None) -> str:
        if name.startswith("nvme"):
            return "NVMe SSD"
        if name.startswith("zram"):
            return "zram"
        if rotational:
            return "HDD"
        return "SSD"

    @staticmethod
    def _scheduler(base: str) -> str | None:
        raw = read_text(f"{base}/queue/scheduler")
        if not raw:
            return None
        # Format is "none [mq-deadline] kyber"; the active one is bracketed.
        for token in raw.split():
            if token.startswith("["):
                return token.strip("[]")
        return raw.split()[0] if raw.split() else None

    @staticmethod
    def _hwmon_for(name: str) -> str | None:
        """Find a hwmon node reporting this drive's temperature.

        NVMe controllers expose one; SATA drives get one from the `drivetemp`
        module, if it is loaded. Both are readable without root, unlike SMART,
        so this is the temperature source we poll every tick.

        Two sysfs layouts exist and both occur in the wild: the node may sit in
        a `hwmon/` subdirectory, or be linked directly as `hwmonN` beside it.
        """
        roots = (
            f"/sys/block/{name}/device",
            f"/sys/block/{name}/device/device",
        )
        for root in roots:
            # Layout A: <root>/hwmon/hwmonN
            for entry in listdir(f"{root}/hwmon"):
                if entry.startswith("hwmon"):
                    return f"{root}/hwmon/{entry}"
            # Layout B: <root>/hwmonN
            for entry in listdir(root):
                if _HWMON_RE.fullmatch(entry):
                    return f"{root}/{entry}"
        return None

    # ----------------------------------------------------------------- sampling

    def _diskstats(self) -> dict[str, list[int]]:
        stats: dict[str, list[int]] = {}
        text = read_text("/proc/diskstats", "") or ""
        for line in text.splitlines():
            parts = line.split()
            if len(parts) < 14:
                continue
            name = parts[2]
            if name in self.devices or name.startswith("zram"):
                try:
                    stats[name] = [int(value) for value in parts[3:]]
                except ValueError:
                    continue
        return stats

    def _temperature(self, device: dict[str, Any]) -> float | None:
        """Drive temperature, refreshed every few ticks rather than every tick.

        Reading an NVMe composite temperature is a command round-trip to the
        drive controller costing milliseconds, not a memory read. Drive
        temperatures also move slowly, so polling once per second buys nothing
        and would dominate this collector's cost.
        """
        hwmon = device.get("hwmon")
        if not hwmon:
            return None
        name = device["name"]
        if self._temp_tick % _TEMP_EVERY != 0 and name in self._temps:
            return self._temps[name]
        # temp1 is the composite/primary sensor on both NVMe and drivetemp.
        millidegrees = read_int(f"{hwmon}/temp1_input")
        value = millidegrees / 1000.0 if millidegrees is not None else None
        if value is not None:
            self._temps[name] = value
        return value

    def _filesystems(self) -> list[dict[str, Any]]:
        result = []
        for part in psutil.disk_partitions(all=False):
            try:
                usage = psutil.disk_usage(part.mountpoint)
            except (OSError, PermissionError):
                continue
            result.append(
                {
                    "device": part.device,
                    "mountpoint": part.mountpoint,
                    "fstype": part.fstype,
                    "opts": part.opts,
                    "total": usage.total,
                    "used": usage.used,
                    "free": usage.free,
                    "percent": usage.percent,
                }
            )
        result.sort(key=lambda item: item["total"], reverse=True)
        return result

    def _rediscover_if_changed(self) -> None:
        """Pick up hot-plugged drives and removable media without a restart.

        Comparing the /sys/block listing is a single cheap readdir, so this can
        run every tick; the expensive metadata walk only reruns when the set of
        block devices actually changed.
        """
        current = {name for name in listdir("/sys/block") if self._is_physical(name)}
        known = set(self.devices)
        # A newly-inserted device also has to report a non-zero size before
        # _discover keeps it, so re-scan whenever the raw sets disagree.
        if current != known:
            self.devices = self._discover()

    def sample(self, now: float) -> dict[str, Any]:
        self._rediscover_if_changed()
        self._temp_tick += 1
        stats = self._diskstats()
        devices = []

        for name, meta in self.devices.items():
            row = stats.get(name)
            if row is None:
                continue

            read_bps = self._rates.update((name, "r"), row[_SECTORS_READ] * _SECTOR_BYTES, now)
            write_bps = self._rates.update((name, "w"), row[_SECTORS_WRITTEN] * _SECTOR_BYTES, now)
            read_iops = self._rates.update((name, "ri"), row[_READS_DONE], now)
            write_iops = self._rates.update((name, "wi"), row[_WRITES_DONE], now)

            # io_ticks counts milliseconds during which the queue was non-empty,
            # so its delta over the wall-clock interval is utilisation.
            busy_ms = self._rates.delta((name, "busy"), row[_IO_TICKS], now)
            elapsed_ms = self._rates.delta((name, "clock"), now * 1000.0, now)
            utilisation = None
            if busy_ms is not None and elapsed_ms and elapsed_ms > 0:
                utilisation = min(100.0, 100.0 * busy_ms / elapsed_ms)

            # Average service latency: milliseconds spent divided by ops completed.
            read_ms = self._rates.delta((name, "rms"), row[_MS_READING], now)
            reads_done = self._rates.delta((name, "rc"), row[_READS_DONE], now)
            write_ms = self._rates.delta((name, "wms"), row[_MS_WRITING], now)
            writes_done = self._rates.delta((name, "wc"), row[_WRITES_DONE], now)

            devices.append(
                {
                    **meta,
                    "read_bps": read_bps,
                    "write_bps": write_bps,
                    "read_iops": read_iops,
                    "write_iops": write_iops,
                    "total_bps": (read_bps or 0.0) + (write_bps or 0.0),
                    "utilisation": utilisation,
                    "in_flight": row[_IOS_IN_PROGRESS],
                    "read_latency_ms": (read_ms / reads_done) if reads_done else None,
                    "write_latency_ms": (write_ms / writes_done) if writes_done else None,
                    "read_total": row[_SECTORS_READ] * _SECTOR_BYTES,
                    "write_total": row[_SECTORS_WRITTEN] * _SECTOR_BYTES,
                    "temp": self._temperature(meta),
                    "smart": self._smart_cache.get(name),
                }
            )

        devices.sort(key=lambda item: item["name"])
        return {"devices": devices, "filesystems": self._filesystems()}

    # -------------------------------------------------------------- SMART data

    def fetch_smart(self, name: str, interactive: bool = True) -> dict[str, Any] | None:
        """Pull SMART health for one device. Requires root, so this is on demand.

        Results are cached and merged into subsequent samples; the per-tick path
        never shells out.
        """
        binary = shutil.which("smartctl")
        if not binary:
            return {"error": "smartctl not installed"}

        command = [binary, "-j", "-H", "-A", "-i", f"/dev/{name}"]
        if interactive and shutil.which("pkexec"):
            command = [shutil.which("pkexec")] + command

        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=60, check=False
            )
            payload = json.loads(completed.stdout or "{}")
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            return {"error": str(exc)}

        if not payload:
            return {"error": "no SMART data (needs root)"}

        health = payload.get("smart_status", {}).get("passed")
        result: dict[str, Any] = {
            "passed": health,
            "model": payload.get("model_name"),
            "serial": payload.get("serial_number"),
            "firmware": payload.get("firmware_version"),
            "attributes": {},
        }

        # NVMe and ATA report health through completely different structures.
        nvme = payload.get("nvme_smart_health_information_log")
        if nvme:
            result["attributes"] = {
                "Power-on hours": nvme.get("power_on_hours"),
                "Power cycles": nvme.get("power_cycles"),
                "Unsafe shutdowns": nvme.get("unsafe_shutdowns"),
                "Percentage used": nvme.get("percentage_used"),
                "Available spare": nvme.get("available_spare"),
                "Media errors": nvme.get("media_errors"),
                "Data read": (nvme.get("data_units_read") or 0) * 512 * 1000,
                "Data written": (nvme.get("data_units_written") or 0) * 512 * 1000,
            }
        else:
            for attr in payload.get("ata_smart_attributes", {}).get("table", []):
                result["attributes"][attr.get("name", "?")] = attr.get("raw", {}).get("value")
            hours = payload.get("power_on_time", {}).get("hours")
            if hours is not None:
                result["attributes"]["Power-on hours"] = hours

        self._smart_cache[name] = result
        return result
