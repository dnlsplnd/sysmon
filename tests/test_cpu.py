"""CPU collector: /proc/stat arithmetic and the choice of frequency file."""

from __future__ import annotations

import pytest

from sysmon.collectors import cpu as cpu_module
from sysmon.collectors.cpu import CpuCollector

# Per-core counters at rest. The second tick adds a known delta to each, which
# is what the percentages below are derived from.
_BASE_CORE = "1000 0 500 20000 100 0 0 0 0 0"

_STAT_FIRST = f"""\
cpu  4000 0 2000 80000 400 0 0 0 0 0
cpu0 {_BASE_CORE}
cpu1 {_BASE_CORE}
cpu2 {_BASE_CORE}
cpu3 {_BASE_CORE}
intr 5000 1 2 3 4 5 6
ctxt 100000
btime 1700000000
processes 4000
procs_running 2
procs_blocked 1
"""

# Deltas, per core: cpu0 20% busy, cpu1 10%, cpu2 30%, cpu3 10%.
# Aggregate: user 400, system 300, idle 3000, iowait 300 of 4000 -> 17.5% busy.
_STAT_SECOND = """\
cpu  4400 0 2300 83000 700 0 0 0 0 0
cpu0 1100 0 600 20700 200 0 0 0 0 0
cpu1 1050 0 550 20850 150 0 0 0 0 0
cpu2 1200 0 600 20600 200 0 0 0 0 0
cpu3 1050 0 550 20850 150 0 0 0 0 0
intr 8000 1 2 3 4 5 6
ctxt 150000
btime 1700000000
processes 4200
procs_running 3
procs_blocked 0
"""


@pytest.fixture
def box(kernel):
    """A four-thread, two-core machine with both frequency files present."""
    kernel.cpu_count = 4
    kernel.write("/proc/cpuinfo", "processor\t: 0\nmodel name\t: AMD Ryzen 7 1700X Eight-Core Processor\n")
    kernel.write("/proc/stat", _STAT_FIRST)
    kernel.write("/proc/loadavg", "6.86 8.15 10.01 2/1234 5678")
    kernel.write("/proc/uptime", "2229.53 12345.67")

    # Two physical cores, two threads each.
    for index, core_id in enumerate((0, 0, 1, 1)):
        base = f"/sys/devices/system/cpu/cpu{index}/topology"
        kernel.write(f"{base}/physical_package_id", "0")
        kernel.write(f"{base}/core_id", str(core_id))

    # cpuinfo_avg_freq is what the cores actually averaged; scaling_cur_freq is
    # only the P-state the governor asked for, and here they disagree sharply.
    for index, measured in enumerate((3_700_000, 3_700_000, 2_200_000, 2_200_000)):
        base = f"/sys/devices/system/cpu/cpu{index}/cpufreq"
        kernel.write(f"{base}/cpuinfo_avg_freq", str(measured))
        kernel.write(f"{base}/scaling_cur_freq", "2200000")
        kernel.write(f"{base}/cpuinfo_max_freq", "3700000")
        kernel.write(f"{base}/scaling_governor", "performance")

    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {"name": "k10temp", "temp1_input": "70000", "temp1_label": "Tctl",
         "temp2_input": "65000", "temp2_label": "Tdie"},
    )
    kernel.patch(cpu_module)
    return kernel


# ------------------------------------------------------------- static info


def test_model_comes_from_cpuinfo(box):
    assert CpuCollector().model == "AMD Ryzen 7 1700X Eight-Core Processor"


def test_model_falls_back_when_cpuinfo_says_nothing(kernel):
    kernel.write("/proc/cpuinfo", "processor\t: 0\n")
    kernel.patch(cpu_module)
    assert CpuCollector().model == "Unknown CPU"


def test_topology_counts_physical_cores_not_threads(box):
    assert CpuCollector().topology == {"threads": 4, "cores": 2, "threads_per_core": 2}


def test_topology_falls_back_to_thread_count_without_sysfs(kernel):
    kernel.cpu_count = 4
    kernel.patch(cpu_module)
    assert CpuCollector().topology == {"threads": 4, "cores": 4, "threads_per_core": 1}


# --------------------------------------------------------- frequency source


def test_the_measured_frequency_file_wins_over_the_requested_one(box):
    """cpuinfo_avg_freq is derived from APERF/MPERF, so it beats scaling_cur_freq.

    Both files exist here and disagree: the measured one averages 2950 MHz
    across the four threads, the governor's says a flat 2200.
    """
    collector = CpuCollector()
    assert collector.freq_source == "cpuinfo_avg_freq"

    collector.sample(now=0.0)
    sample = collector.sample(now=1.0)
    assert sample["freq_mhz"] == 2950.0
    assert sample["freq_per_core"] == [3700.0, 3700.0, 2200.0, 2200.0]


def test_it_falls_back_to_the_governors_file_when_nothing_better_exists(kernel):
    kernel.cpu_count = 2
    kernel.write("/proc/stat", _STAT_FIRST)
    for index in range(2):
        kernel.write(f"/sys/devices/system/cpu/cpu{index}/cpufreq/scaling_cur_freq", "2200000")
    kernel.patch(cpu_module)

    collector = CpuCollector()
    assert collector.freq_source == "scaling_cur_freq"
    assert collector.sample(now=0.0)["freq_per_core"] == [2200.0, 2200.0]


def test_no_cpufreq_at_all_reports_no_source(kernel):
    kernel.cpu_count = 2
    kernel.write("/proc/stat", _STAT_FIRST)
    kernel.patch(cpu_module)

    collector = CpuCollector()
    assert collector.freq_source == ""
    sample = collector.sample(now=0.0)
    assert sample["freq_per_core"] == [None, None]
    assert sample["freq_mhz"] is None


def test_max_frequency_is_reported_in_megahertz(box):
    assert CpuCollector().max_freq_mhz == 3700.0


# ------------------------------------------------------------------ usage


def test_the_first_tick_has_no_usage(box):
    """Nothing to diff against, so a dash is correct and zero would be a lie."""
    sample = CpuCollector().sample(now=0.0)
    assert sample["usage"] is None
    assert sample["breakdown"] is None
    assert sample["per_core"] == [None, None, None, None]


def test_usage_counts_iowait_as_idle(box):
    """The htop/top convention: waiting on I/O is not the CPU doing work."""
    collector = CpuCollector()
    collector.sample(now=0.0)
    box.write("/proc/stat", _STAT_SECOND)
    sample = collector.sample(now=1.0)

    assert sample["usage"] == pytest.approx(17.5)
    breakdown = sample["breakdown"]
    assert breakdown["user"] == pytest.approx(10.0)
    assert breakdown["system"] == pytest.approx(7.5)
    assert breakdown["iowait"] == pytest.approx(7.5)
    assert breakdown["idle"] == pytest.approx(75.0)
    # busy excludes iowait even though iowait is not in the idle column.
    assert breakdown["busy"] == pytest.approx(17.5)


def test_per_core_usage_is_tracked_separately(box):
    collector = CpuCollector()
    collector.sample(now=0.0)
    box.write("/proc/stat", _STAT_SECOND)
    sample = collector.sample(now=1.0)
    assert sample["per_core"] == pytest.approx([20.0, 10.0, 30.0, 10.0])


def test_an_idle_interval_reports_no_usage(box):
    """Identical counters mean no elapsed jiffies, which is not 0% busy."""
    collector = CpuCollector()
    collector.sample(now=0.0)
    sample = collector.sample(now=1.0)
    assert sample["usage"] is None


# --------------------------------------------------------------- the rest


def test_intr_uses_only_the_total_column(box):
    """The intr line carries hundreds of per-IRQ columns after the total."""
    collector = CpuCollector()
    collector.sample(now=0.0)
    box.write("/proc/stat", _STAT_SECOND)
    sample = collector.sample(now=1.0)
    # 8000 - 5000 over one second, ignoring the per-IRQ columns entirely.
    assert sample["intr_per_s"] == pytest.approx(3000.0)


def test_counter_rates_are_divided_by_the_elapsed_time(box):
    """Per-second, not per-tick: the same load must read the same at any interval.

    50000 context switches over two seconds is 25000/s. Reporting the raw
    increment would make the figure depend on --interval.
    """
    collector = CpuCollector()
    collector.sample(now=0.0)
    box.write("/proc/stat", _STAT_SECOND)
    sample = collector.sample(now=2.0)

    assert sample["ctxt_per_s"] == pytest.approx(25000.0)
    assert sample["forks_per_s"] == pytest.approx(100.0)


def test_counter_rates_do_not_depend_on_the_sampling_interval(box):
    """The same counters over half the time must report double the rate."""
    fast = CpuCollector()
    fast.sample(now=0.0)
    box.write("/proc/stat", _STAT_SECOND)
    assert fast.sample(now=0.5)["ctxt_per_s"] == pytest.approx(100000.0)


def test_the_first_tick_has_no_counter_rates(box):
    sample = CpuCollector().sample(now=0.0)
    assert sample["ctxt_per_s"] is None
    assert sample["intr_per_s"] is None
    assert sample["forks_per_s"] is None


def test_process_state_counts_come_from_proc_stat(box):
    sample = CpuCollector().sample(now=0.0)
    assert sample["procs_running"] == 2
    assert sample["procs_blocked"] == 1


def test_temperatures_are_labelled_and_tdie_wins(box):
    """Both rails are reported, but the headline figure prefers Tdie over Tctl."""
    sample = CpuCollector().sample(now=0.0)
    assert sample["temps"] == {"Tctl": 70.0, "Tdie": 65.0}
    assert sample["temp"] == 65.0


def test_a_hwmon_without_a_known_name_is_ignored(kernel):
    kernel.cpu_count = 1
    kernel.write("/proc/stat", _STAT_FIRST)
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme", "temp1_input": "45000"})
    kernel.patch(cpu_module)
    assert CpuCollector().sample(now=0.0)["temps"] == {}


def test_the_right_hwmon_is_picked_out_of_several(kernel):
    kernel.cpu_count = 1
    kernel.write("/proc/stat", _STAT_FIRST)
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme", "temp1_input": "45000"})
    kernel.write_many("/sys/class/hwmon/hwmon1", {"name": "iwlwifi_1", "temp1_input": "30000"})
    kernel.write_many("/sys/class/hwmon/hwmon2", {"name": "k10temp", "temp1_input": "70000"})
    kernel.patch(cpu_module)
    # No _label file, so the rail is named after the file it came from.
    assert CpuCollector().sample(now=0.0)["temps"] == {"temp1": 70.0}


def test_loadavg_and_uptime(box):
    sample = CpuCollector().sample(now=0.0)
    assert sample["loadavg"] == [6.86, 8.15, 10.01]
    assert sample["uptime"] == 2229.53


def test_a_short_loadavg_is_dropped_rather_than_guessed(kernel):
    kernel.cpu_count = 1
    kernel.write("/proc/stat", _STAT_FIRST)
    kernel.write("/proc/loadavg", "6.86")
    kernel.patch(cpu_module)
    assert CpuCollector().sample(now=0.0)["loadavg"] == []


def test_governor_is_reported(box):
    assert CpuCollector().sample(now=0.0)["governor"] == "performance"
