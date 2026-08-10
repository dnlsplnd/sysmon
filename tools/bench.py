#!/usr/bin/env python3
"""Measure what a sampling tick costs, the way the hub actually incurs it.

This replicates ``Hub._run``: the same collectors, constructed the same way, in
the same order, each handed ``safe_sample(now)`` with ``now`` from
``time.time()``. Two details are deliberate and both change the answer.

The cadence is real. Several collectors defer expensive work to every fourth or
fifth tick, so sampling back-to-back would measure a machine that never pays for
it. Ticks are spaced exactly as the running application spaces them.

Both page states are measured. The process table is built only while the
Processes page is on screen and costs more than everything else combined, so a
single figure for "a tick" is meaningless without saying which state it is.

    python3 tools/bench.py                  # 60 ticks at 1s, both states
    python3 tools/bench.py --ticks 120
    python3 tools/bench.py --interval 0.5
    python3 tools/bench.py --markdown       # tables for the README

The README's Cost section comes from this. After changing what a collector
reads, re-run it and update both the tables and the provenance note there --
the header printed below has everything that note needs.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import subprocess
import sys
import time
from pathlib import Path

# Run from anywhere: the package is a sibling of this file's directory, and is
# deliberately not installed into site-packages.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import sysmon  # noqa: E402
from sysmon.collectors import (  # noqa: E402
    CpuCollector,
    DiskCollector,
    GpuCollector,
    MemoryCollector,
    NetworkCollector,
    ProcessCollector,
    SensorCollector,
)

# Same order as Hub, because a collector's cost can depend on what ran before it.
ORDER = (
    ("cpu", CpuCollector),
    ("memory", MemoryCollector),
    ("disk", DiskCollector),
    ("network", NetworkCollector),
    ("gpu", GpuCollector),
    ("sensors", SensorCollector),
    ("processes", ProcessCollector),
)


def provenance() -> list[str]:
    """Everything the README's note needs to say when these were taken."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        commit = "unknown"

    model = "unknown CPU"
    for line in Path("/proc/cpuinfo").read_text(errors="replace").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    processes = sum(1 for entry in Path("/proc").iterdir() if entry.name.isdigit())
    loadavg = Path("/proc/loadavg").read_text().split()[0]

    return [
        f"date      {time.strftime('%Y-%m-%d')}",
        f"sysmon    {sysmon.__version__} at commit {commit}",
        f"kernel    {platform.release()}",
        f"python    {platform.python_version()}",
        f"cpu       {model}",
        f"state     {processes} processes, load average {loadavg}",
    ]


def measure(detailed: bool, ticks: int, interval: float, warmup: int):
    """Sample every collector for ``ticks`` ticks, returning per-collector costs."""
    collectors = {name: cls() for name, cls in ORDER}
    collectors["processes"].detailed = detailed

    # Rate trackers need a baseline, and psutil's first cpu_percent for a
    # process is always 0.0, so the opening ticks are not representative.
    for _ in range(warmup):
        now = time.time()
        for collector in collectors.values():
            collector.safe_sample(now)
        time.sleep(interval)

    per: dict[str, list[float]] = {name: [] for name in collectors}
    totals: list[float] = []
    for _ in range(ticks):
        started = time.perf_counter()
        now = time.time()
        for name, collector in collectors.items():
            at = time.perf_counter()
            collector.safe_sample(now)
            per[name].append((time.perf_counter() - at) * 1000.0)
        totals.append((time.perf_counter() - started) * 1000.0)
        # Subtract the work just done, exactly as the hub does, so the cadence
        # stays on the wall clock rather than drifting by the sampling cost.
        time.sleep(max(0.05, interval - (time.perf_counter() - started)))
    return per, totals


# Acronyms and plurals that .title() would mangle into "Gpu" and "Disk".
LABELS = {
    "cpu": "CPU", "gpu": "GPU", "disk": "Disks", "memory": "Memory",
    "network": "Network", "sensors": "Sensors", "processes": "Processes",
}


def ms(value: float) -> str:
    """Milliseconds at a precision that does not round a rail away to zero."""
    return f"{value:.1f} ms" if value < 10 else f"{value:.0f} ms"


def stats(values: list[float]) -> tuple[float, float, float, float]:
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
    return statistics.median(values), statistics.mean(values), p95, max(values)


def report(label: str, per, totals, markdown: bool) -> None:
    rows = sorted(per.items(), key=lambda item: -statistics.median(item[1]))
    if markdown:
        print(f"\n**{label}**\n")
        print("| Collector | Median | p95 |")
        print("|---|---|---|")
        for name, values in rows:
            median, _, p95, _ = stats(values)
            print(f"| {LABELS.get(name, name.title())} | {ms(median)} | {ms(p95)} |")
        median, _, p95, _ = stats(totals)
        print(f"| **whole tick** | **{ms(median)}** | **{ms(p95)}** |")
        return

    print(f"\n{label}")
    print(f"  {'collector':<12}{'median':>9}{'mean':>9}{'p95':>9}{'max':>9}")
    for name, values in rows:
        median, mean, p95, worst = stats(values)
        print(f"  {name:<12}{median:8.1f}ms{mean:8.1f}ms{p95:8.1f}ms{worst:8.1f}ms")
    median, mean, p95, worst = stats(totals)
    print(f"  {'TOTAL':<12}{median:8.1f}ms{mean:8.1f}ms{p95:8.1f}ms{worst:8.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="bench.py", description="Measure the cost of one sampling tick."
    )
    parser.add_argument("--ticks", type=int, default=60, help="ticks to measure (default: 60)")
    parser.add_argument(
        "--interval", type=float, default=1.0,
        help="seconds between ticks; must match how the app runs (default: 1.0)",
    )
    parser.add_argument("--warmup", type=int, default=2, help="ticks to discard first (default: 2)")
    parser.add_argument("--markdown", action="store_true", help="emit tables for the README")
    options = parser.parse_args()

    for line in provenance():
        print(("> " if options.markdown else "") + line)

    for detailed, label in ((False, "Processes page closed (steady state)"),
                            (True, "Processes page open (full table)")):
        per, totals = measure(detailed, options.ticks, options.interval, options.warmup)
        report(f"{label} — {options.ticks} ticks at {options.interval:g}s",
               per, totals, options.markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
