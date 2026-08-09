"""Memory collector: /proc/meminfo, swap, zram compression, DMI module info."""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any

from .base import Collector
from ..util import listdir, read_int, read_text

# zram's mm_stat columns, in kernel order. Trailing columns appeared over
# several releases, so a short row is normal on older kernels.
_ZRAM_FIELDS = (
    "orig_data_size",
    "compr_data_size",
    "mem_used_total",
    "mem_limit",
    "mem_used_max",
    "same_pages",
    "pages_compacted",
    "huge_pages",
    "huge_pages_since",
)


class MemoryCollector(Collector):
    name = "memory"

    def __init__(self) -> None:
        super().__init__()
        self.modules: list[dict[str, Any]] = []
        self.dimm_error: str | None = None
        # DMI is root-only on a locked-down kernel, so this usually no-ops at
        # startup; the UI offers an explicit pkexec fetch instead of nagging.
        self.load_modules(interactive=False)

    # -------------------------------------------------------------- DIMM info

    def load_modules(self, interactive: bool = False) -> bool:
        """Populate physical module info (size, speed, part number) via dmidecode.

        Returns True when modules were found. ``interactive=True`` routes through
        pkexec so the user gets a polkit prompt; without it we only try paths
        that can succeed silently.
        """
        binary = shutil.which("dmidecode")
        if not binary:
            self.dimm_error = "dmidecode not installed"
            return False

        if interactive:
            pkexec = shutil.which("pkexec")
            command = [pkexec, binary, "-t", "memory"] if pkexec else [binary, "-t", "memory"]
        else:
            # -n keeps sudo from ever blocking on a password prompt.
            sudo = shutil.which("sudo")
            command = [sudo, "-n", binary, "-t", "memory"] if sudo else [binary, "-t", "memory"]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=60 if interactive else 5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.dimm_error = str(exc)
            return False

        if completed.returncode != 0:
            self.dimm_error = "requires root"
            return False

        self.modules = self._parse_dmidecode(completed.stdout)
        self.dimm_error = None if self.modules else "no populated slots reported"
        return bool(self.modules)

    @staticmethod
    def _parse_dmidecode(text: str) -> list[dict[str, Any]]:
        modules: list[dict[str, Any]] = []
        for block in text.split("\n\n"):
            if "Memory Device" not in block:
                continue
            fields = {}
            for line in block.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()
            size = fields.get("Size", "")
            if not size or "No Module" in size:
                continue

            def mhz(raw: str | None) -> int | None:
                match = re.search(r"(\d+)", raw or "")
                return int(match.group(1)) if match else None

            modules.append(
                {
                    "locator": fields.get("Locator", "?"),
                    "size": size,
                    "type": fields.get("Type"),
                    # "Configured Memory Speed" is what the DIMM actually runs at;
                    # "Speed" is only what the part is rated for.
                    "speed_mhz": mhz(fields.get("Configured Memory Speed")) or mhz(fields.get("Speed")),
                    "rated_mhz": mhz(fields.get("Speed")),
                    "manufacturer": fields.get("Manufacturer"),
                    "part": fields.get("Part Number"),
                }
            )
        return modules

    # ----------------------------------------------------------------- sampling

    @staticmethod
    def _meminfo() -> dict[str, int]:
        info: dict[str, int] = {}
        text = read_text("/proc/meminfo", "") or ""
        for line in text.splitlines():
            key, _, rest = line.partition(":")
            parts = rest.split()
            if not parts:
                continue
            try:
                value = int(parts[0])
            except ValueError:
                continue
            # Everything in meminfo is kB except a few page counts; the keys we
            # use all carry the kB suffix.
            info[key] = value * 1024 if len(parts) > 1 and parts[1] == "kB" else value
        return info

    @staticmethod
    def _zram() -> list[dict[str, Any]]:
        devices = []
        for entry in listdir("/sys/block"):
            if not entry.startswith("zram"):
                continue
            base = f"/sys/block/{entry}"
            disksize = read_int(f"{base}/disksize") or 0
            if not disksize:
                continue
            raw = (read_text(f"{base}/mm_stat") or "").split()
            stats = {}
            for name, value in zip(_ZRAM_FIELDS, raw):
                try:
                    stats[name] = int(value)
                except ValueError:
                    stats[name] = 0
            original = stats.get("orig_data_size", 0)
            compressed = stats.get("compr_data_size", 0)
            devices.append(
                {
                    "name": entry,
                    "disksize": disksize,
                    "original": original,
                    "compressed": compressed,
                    "used_total": stats.get("mem_used_total", 0),
                    # Ratio is only meaningful once something is actually stored.
                    "ratio": (original / compressed) if compressed else None,
                    "algorithm": (read_text(f"{base}/comp_algorithm") or "").strip(),
                }
            )
        return devices

    def sample(self, now: float) -> dict[str, Any]:
        info = self._meminfo()
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        # "Used" mirrors free(1): what is neither free nor reclaimable.
        used = max(0, total - available)

        swap_total = info.get("SwapTotal", 0)
        swap_free = info.get("SwapFree", 0)
        swap_used = max(0, swap_total - swap_free)

        speeds = [m["speed_mhz"] for m in self.modules if m.get("speed_mhz")]

        return {
            "total": total,
            "available": available,
            "used": used,
            "free": info.get("MemFree", 0),
            "cached": info.get("Cached", 0) + info.get("SReclaimable", 0),
            "buffers": info.get("Buffers", 0),
            "shmem": info.get("Shmem", 0),
            "dirty": info.get("Dirty", 0),
            "writeback": info.get("Writeback", 0),
            "slab": info.get("Slab", 0),
            "page_tables": info.get("PageTables", 0),
            "committed": info.get("Committed_AS", 0),
            "commit_limit": info.get("CommitLimit", 0),
            "anon": info.get("AnonPages", 0),
            "mapped": info.get("Mapped", 0),
            "hugepages_total": info.get("HugePages_Total", 0),
            "percent": (100.0 * used / total) if total else 0.0,
            "swap_total": swap_total,
            "swap_used": swap_used,
            "swap_free": swap_free,
            "swap_percent": (100.0 * swap_used / swap_total) if swap_total else 0.0,
            "zram": self._zram(),
            "modules": self.modules,
            "dimm_error": self.dimm_error,
            "speed_mhz": max(speeds) if speeds else None,
        }
