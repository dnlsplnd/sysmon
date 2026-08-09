"""Process collector: the sortable table, with per-process I/O rates."""

from __future__ import annotations

import os
from typing import Any

import psutil

from .base import Collector, RateTracker


class ProcessCollector(Collector):
    name = "processes"

    # Fetched in a single pass. io_counters belongs in this list rather than in
    # a separate call per process: it raises AccessDenied on every process we
    # do not own, and letting psutil absorb that internally is far cheaper than
    # propagating hundreds of Python exceptions per tick.
    _ATTRS = (
        "pid",
        "name",
        "username",
        "status",
        "memory_info",
        "memory_percent",
        "num_threads",
        "nice",
        "create_time",
        "cpu_percent",
        "io_counters",
    )

    def __init__(self) -> None:
        super().__init__()
        self._io = RateTracker()
        self._core_count = os.cpu_count() or 1
        self._primed = False
        # Building the full table costs ~85 ms on a busy machine, which is most
        # of the app's sampling budget. Only the Processes page needs it, so it
        # stays off until that page asks for it.
        self.detailed = False

    @staticmethod
    def _cheap_counts() -> dict[str, int]:
        """Process total without touching any per-process file.

        Counting the numeric entries in /proc is a single readdir, versus the
        ~490 stat/status reads a full table costs. The per-state breakdown is
        left at zero here; the CPU collector already publishes runnable and
        blocked counts from /proc/stat for the pages that show them.
        """
        try:
            total = sum(1 for entry in os.listdir("/proc") if entry.isdigit())
        except OSError:
            total = 0
        return {"total": total, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}

    def sample(self, now: float) -> dict[str, Any]:
        if not self.detailed:
            return {
                "processes": [],
                "counts": self._cheap_counts(),
                "primed": self._primed,
                "core_count": self._core_count,
                "detailed": False,
            }

        rows: list[dict[str, Any]] = []
        alive: set[int] = set()
        counts = {"total": 0, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}

        for proc in psutil.process_iter(self._ATTRS, ad_value=None):
            try:
                info = proc.info
            except psutil.Error:
                continue

            pid = info.get("pid")
            if pid is None:
                continue
            alive.add(pid)
            counts["total"] += 1

            status = info.get("status") or "unknown"
            if status in counts:
                counts[status] += 1

            memory = info.get("memory_info")
            rss = getattr(memory, "rss", 0) or 0
            vms = getattr(memory, "vms", 0) or 0

            # psutil reports CPU as a share of one core; the first reading after
            # a process is seen is always 0.0 because it has no baseline yet.
            cpu_percent = info.get("cpu_percent") or 0.0

            read_bps = write_bps = None
            io_counters = info.get("io_counters")
            if io_counters is not None:
                read_bps = self._io.update((pid, "r"), io_counters.read_bytes, now)
                write_bps = self._io.update((pid, "w"), io_counters.write_bytes, now)

            rows.append(
                {
                    "pid": pid,
                    "name": info.get("name") or "?",
                    "user": info.get("username") or "?",
                    "status": status,
                    "cpu": cpu_percent,
                    # Normalised against all cores, so a fully-busy box reads 100%.
                    "cpu_normalised": cpu_percent / self._core_count,
                    "rss": rss,
                    "vms": vms,
                    "mem_percent": info.get("memory_percent") or 0.0,
                    "threads": info.get("num_threads") or 0,
                    "nice": info.get("nice"),
                    "started": info.get("create_time"),
                    "read_bps": read_bps,
                    "write_bps": write_bps,
                    "io_bps": (read_bps or 0.0) + (write_bps or 0.0),
                }
            )

        # Processes die constantly; drop their rate history so the dict stays bounded.
        self._io.retain({(pid, suffix) for pid in alive for suffix in ("r", "w")})

        primed = self._primed
        self._primed = True

        return {
            "processes": rows,
            "counts": counts,
            # The UI uses this to explain why every CPU column reads 0.0 on the
            # very first tick rather than looking broken.
            "primed": primed,
            "core_count": self._core_count,
            "detailed": True,
        }

    # ----------------------------------------------------------------- actions

    @staticmethod
    def terminate(pid: int, force: bool = False) -> str | None:
        """Signal a process. Returns None on success, or an error message."""
        try:
            proc = psutil.Process(pid)
            if force:
                proc.kill()
            else:
                proc.terminate()
            return None
        except psutil.NoSuchProcess:
            return "process already exited"
        except psutil.AccessDenied:
            return "permission denied (process owned by another user)"
        except psutil.Error as exc:
            return str(exc)
