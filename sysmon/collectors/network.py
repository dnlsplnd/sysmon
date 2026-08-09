"""Network collector: per-interface rates, cumulative counters, sockets, wireless."""

from __future__ import annotations

import socket
import time
from typing import Any

import psutil

from .base import Collector, RateTracker
from ..util import listdir, read_int, read_text

# Counters exposed under /sys/class/net/<iface>/statistics that we track.
_STATS = (
    "rx_bytes",
    "tx_bytes",
    "rx_packets",
    "tx_packets",
    "rx_errors",
    "tx_errors",
    "rx_dropped",
    "tx_dropped",
)

_FAMILY_NAMES = {
    socket.AF_INET: "IPv4",
    socket.AF_INET6: "IPv6",
    getattr(socket, "AF_PACKET", -1): "Link",
}


class NetworkCollector(Collector):
    name = "network"

    def __init__(self) -> None:
        super().__init__()
        self._rates = RateTracker()
        # Baselines let the UI show "since this app started" alongside the
        # kernel's own since-boot totals.
        self._session_start = time.time()
        self._baseline: dict[str, dict[str, int]] = {}
        self._peak: dict[str, dict[str, float]] = {}

    # -------------------------------------------------------------- interfaces

    @staticmethod
    def _interfaces() -> list[str]:
        return [name for name in listdir("/sys/class/net") if name != "bonding_masters"]

    @staticmethod
    def _kind(name: str) -> str:
        base = f"/sys/class/net/{name}"
        if read_text(f"{base}/wireless/link") is not None or listdir(f"{base}/wireless"):
            return "Wi-Fi"
        if name == "lo":
            return "Loopback"
        if read_text(f"{base}/tun_flags") is not None:
            return "VPN/TUN"
        if listdir(f"{base}/bridge"):
            return "Bridge"
        if (read_text(f"{base}/type") or "") == "1":
            return "Ethernet"
        return "Other"

    @staticmethod
    def _addresses(name: str, snapshot: dict[str, list[Any]]) -> list[dict[str, str]]:
        result = []
        for addr in snapshot.get(name, []):
            family = _FAMILY_NAMES.get(addr.family, str(addr.family))
            # Link-layer entries repeat the MAC we already read from sysfs.
            if family == "Link":
                continue
            result.append(
                {
                    "family": family,
                    "address": addr.address.split("%")[0],
                    "netmask": addr.netmask or "",
                }
            )
        return result

    def _wireless(self, name: str) -> dict[str, Any] | None:
        """Signal quality from /proc/net/wireless, which needs no privileges."""
        text = read_text("/proc/net/wireless", "") or ""
        for line in text.splitlines():
            if not line.strip().startswith(f"{name}:"):
                continue
            parts = line.split()
            try:
                return {
                    # Values carry a trailing '.' in this file's fixed-width layout.
                    "quality": float(parts[2].rstrip(".")),
                    "signal_dbm": float(parts[3].rstrip(".")),
                    "noise_dbm": float(parts[4].rstrip(".")),
                }
            except (IndexError, ValueError):
                return None
        return None

    # ----------------------------------------------------------------- sampling

    def sample(self, now: float) -> dict[str, Any]:
        try:
            addr_snapshot = psutil.net_if_addrs()
        except OSError:
            addr_snapshot = {}

        interfaces = []
        totals = {"rx_bps": 0.0, "tx_bps": 0.0, "rx_bytes": 0, "tx_bytes": 0}

        names = self._interfaces()
        for name in names:
            base = f"/sys/class/net/{name}/statistics"
            counters = {key: (read_int(f"{base}/{key}") or 0) for key in _STATS}

            baseline = self._baseline.setdefault(name, dict(counters))
            peak = self._peak.setdefault(name, {"rx": 0.0, "tx": 0.0})

            rx_bps = self._rates.update((name, "rx"), counters["rx_bytes"], now)
            tx_bps = self._rates.update((name, "tx"), counters["tx_bytes"], now)
            rx_pps = self._rates.update((name, "rxp"), counters["rx_packets"], now)
            tx_pps = self._rates.update((name, "txp"), counters["tx_packets"], now)

            if rx_bps is not None:
                peak["rx"] = max(peak["rx"], rx_bps)
            if tx_bps is not None:
                peak["tx"] = max(peak["tx"], tx_bps)

            speed = read_int(f"/sys/class/net/{name}/speed")
            # The kernel reports -1 (or errors out) for a down or virtual link.
            link_mbit = speed if speed and speed > 0 else None
            state = read_text(f"/sys/class/net/{name}/operstate") or "unknown"

            interfaces.append(
                {
                    "name": name,
                    "kind": self._kind(name),
                    "state": state,
                    "up": state == "up",
                    "mac": read_text(f"/sys/class/net/{name}/address"),
                    "mtu": read_int(f"/sys/class/net/{name}/mtu"),
                    "duplex": read_text(f"/sys/class/net/{name}/duplex"),
                    "link_mbit": link_mbit,
                    "addresses": self._addresses(name, addr_snapshot),
                    "wireless": self._wireless(name),
                    "rx_bps": rx_bps,
                    "tx_bps": tx_bps,
                    "rx_pps": rx_pps,
                    "tx_pps": tx_pps,
                    "peak_rx_bps": peak["rx"],
                    "peak_tx_bps": peak["tx"],
                    # Utilisation only means something against a known link rate.
                    "rx_utilisation": (
                        100.0 * (rx_bps or 0) * 8 / (link_mbit * 1e6) if link_mbit else None
                    ),
                    "tx_utilisation": (
                        100.0 * (tx_bps or 0) * 8 / (link_mbit * 1e6) if link_mbit else None
                    ),
                    "rx_bytes": counters["rx_bytes"],
                    "tx_bytes": counters["tx_bytes"],
                    "rx_packets": counters["rx_packets"],
                    "tx_packets": counters["tx_packets"],
                    "rx_errors": counters["rx_errors"],
                    "tx_errors": counters["tx_errors"],
                    "rx_dropped": counters["rx_dropped"],
                    "tx_dropped": counters["tx_dropped"],
                    "session_rx": counters["rx_bytes"] - baseline["rx_bytes"],
                    "session_tx": counters["tx_bytes"] - baseline["tx_bytes"],
                }
            )

            if name != "lo":
                totals["rx_bps"] += rx_bps or 0.0
                totals["tx_bps"] += tx_bps or 0.0
                totals["rx_bytes"] += counters["rx_bytes"]
                totals["tx_bytes"] += counters["tx_bytes"]

        self._rates.retain({(name, suffix) for name in names for suffix in ("rx", "tx", "rxp", "txp")})

        # Physical, up interfaces first; they are what the user usually watches.
        interfaces.sort(key=lambda item: (item["name"] == "lo", not item["up"], item["name"]))

        session_rx = sum(i["session_rx"] for i in interfaces if i["name"] != "lo")
        session_tx = sum(i["session_tx"] for i in interfaces if i["name"] != "lo")
        elapsed = max(1e-6, now - self._session_start)

        return {
            "interfaces": interfaces,
            "totals": totals,
            "session": {
                "rx": session_rx,
                "tx": session_tx,
                "total": session_rx + session_tx,
                "elapsed": elapsed,
                "avg_rx_bps": session_rx / elapsed,
                "avg_tx_bps": session_tx / elapsed,
                "started": self._session_start,
            },
        }

    def reset_session(self) -> None:
        """Re-baseline the session counters to 'now'."""
        self._baseline.clear()
        self._session_start = time.time()

    # ------------------------------------------------------------- connections

    def connections(self) -> list[dict[str, Any]]:
        """Snapshot of active sockets, annotated with the owning process.

        Kept off the sampling tick because enumerating every socket and mapping
        PIDs to names is far heavier than reading a handful of sysfs counters.
        """
        try:
            sockets = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, OSError):
            return []

        names: dict[int, str] = {}
        rows = []
        for conn in sockets:
            pid = conn.pid
            if pid and pid not in names:
                try:
                    names[pid] = psutil.Process(pid).name()
                except (psutil.Error, OSError):
                    names[pid] = "?"
            rows.append(
                {
                    "proto": "TCP" if conn.type == socket.SOCK_STREAM else "UDP",
                    "family": "IPv6" if conn.family == socket.AF_INET6 else "IPv4",
                    "local": f"{conn.laddr.ip}:{conn.laddr.port}" if conn.laddr else "",
                    "remote": f"{conn.raddr.ip}:{conn.raddr.port}" if conn.raddr else "",
                    "status": conn.status,
                    "pid": pid,
                    "process": names.get(pid, "") if pid else "",
                }
            )

        # Established sockets carry the most information, so surface them first.
        rows.sort(key=lambda row: (row["status"] != "ESTABLISHED", row["process"], row["remote"]))
        return rows
