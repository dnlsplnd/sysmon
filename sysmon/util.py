"""Formatting and small filesystem helpers shared by the collectors."""

from __future__ import annotations

import os

# ---------------------------------------------------------------- sysfs reads


def read_text(path: str, default: str | None = None) -> str | None:
    """Read a sysfs/procfs file, returning ``default`` if it is missing or unreadable.

    Sysfs is full of files that exist but raise EACCES (RAPL energy counters) or
    EIO (sensors on a powered-down device), so every read goes through here.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except (OSError, ValueError):
        return default
    if raw is None:
        # Some hwmon nodes (seen on mt7921 Wi-Fi) hand back a non-blocking
        # handle whose read() yields None when the value is not ready yet.
        # Without this guard the .decode() below raises AttributeError and
        # takes the entire sample down over one flaky sensor.
        return default
    return raw.decode("utf-8", "replace").strip()


def read_int(path: str, default: int | None = None) -> int | None:
    raw = read_text(path)
    if raw is None:
        return default
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return default


def read_float(path: str, default: float | None = None) -> float | None:
    raw = read_text(path)
    if raw is None:
        return default
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return default


def listdir(path: str) -> list[str]:
    try:
        return sorted(os.listdir(path))
    except OSError:
        return []


# ------------------------------------------------------------------ formatting

_BYTE_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
_RATE_UNITS = ("B/s", "KiB/s", "MiB/s", "GiB/s", "TiB/s")


def _scale(value: float, units: tuple[str, ...], step: float) -> tuple[float, str]:
    magnitude = abs(value)
    index = 0
    while magnitude >= step and index < len(units) - 1:
        magnitude /= step
        index += 1
    return (magnitude if value >= 0 else -magnitude), units[index]


def fmt_bytes(value: float | None, precision: int | None = None) -> str:
    """Format a byte count with binary units, e.g. ``11.2 GiB``."""
    if value is None:
        return "--"
    scaled, unit = _scale(float(value), _BYTE_UNITS, 1024.0)
    if precision is None:
        # Keep the number roughly three significant digits wide.
        precision = 0 if unit == "B" else (2 if abs(scaled) < 10 else 1)
    return f"{scaled:.{precision}f} {unit}"


def fmt_rate(value: float | None) -> str:
    """Format a per-second byte rate, e.g. ``12.4 MiB/s``."""
    if value is None:
        return "--"
    scaled, unit = _scale(float(value), _RATE_UNITS, 1024.0)
    precision = 0 if unit == "B/s" else (2 if abs(scaled) < 10 else 1)
    return f"{scaled:.{precision}f} {unit}"


def fmt_bits(value: float | None) -> str:
    """Format a per-second *bit* rate with SI units -- how links are marketed."""
    if value is None:
        return "--"
    bits = float(value) * 8.0
    for unit in ("bit/s", "kbit/s", "Mbit/s", "Gbit/s"):
        if abs(bits) < 1000.0 or unit == "Gbit/s":
            precision = 0 if unit == "bit/s" else (2 if abs(bits) < 10 else 1)
            return f"{bits:.{precision}f} {unit}"
        bits /= 1000.0
    return f"{bits:.1f} Gbit/s"


def fmt_hz(mhz: float | None) -> str:
    """Format a megahertz value, promoting to GHz past 1000."""
    if mhz is None:
        return "--"
    if mhz >= 1000.0:
        return f"{mhz / 1000.0:.2f} GHz"
    return f"{mhz:.0f} MHz"


def fmt_pct(value: float | None, precision: int = 0) -> str:
    if value is None:
        return "--"
    return f"{value:.{precision}f}%"


def fmt_temp(celsius: float | None) -> str:
    if celsius is None:
        return "--"
    return f"{celsius:.0f}°C"


def fmt_watts(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.1f} W" if value < 100 else f"{value:.0f} W"


def fmt_duration(seconds: float | None) -> str:
    """Format a span as ``3d 14h 22m`` / ``14h 22m`` / ``22m 05s``."""
    if seconds is None or seconds < 0:
        return "--"
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def fmt_count(value: float | None) -> str:
    """Compact SI-ish count for large integers (packets, IOPS, context switches)."""
    if value is None:
        return "--"
    magnitude = abs(value)
    for unit, threshold in (("G", 1e9), ("M", 1e6), ("k", 1e3)):
        if magnitude >= threshold:
            return f"{value / threshold:.1f}{unit}"
    return f"{value:.0f}"


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))
