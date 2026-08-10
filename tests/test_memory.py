"""Memory collector: meminfo arithmetic, zram compression, DIMM parsing."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sysmon.collectors import memory as memory_module
from sysmon.collectors.memory import MemoryCollector

KB = 1024

_MEMINFO = """\
MemTotal:       32000000 kB
MemFree:         2000000 kB
MemAvailable:    8000000 kB
Buffers:          500000 kB
Cached:          6000000 kB
SwapCached:            0 kB
AnonPages:      20000000 kB
Mapped:          1000000 kB
Shmem:            300000 kB
SReclaimable:    1000000 kB
Slab:            1500000 kB
PageTables:       200000 kB
SwapTotal:       8000000 kB
SwapFree:        6000000 kB
Dirty:              1000 kB
Writeback:             0 kB
CommitLimit:    24000000 kB
Committed_AS:   30000000 kB
HugePages_Total:       0
"""


@pytest.fixture
def box(kernel, monkeypatch):
    """Meminfo in place, and dmidecode unavailable.

    Stubbing shutil matters: MemoryCollector probes for DIMM data at startup,
    and without this the suite would shell out to `sudo -n dmidecode` on the
    machine running the tests.
    """
    monkeypatch.setattr(memory_module, "shutil", SimpleNamespace(which=lambda _: None))
    kernel.write("/proc/meminfo", _MEMINFO)
    kernel.patch(memory_module)
    return kernel


# --------------------------------------------------------------- meminfo


def test_kilobyte_values_become_bytes(box):
    sample = MemoryCollector().sample(now=0.0)
    assert sample["total"] == 32000000 * KB
    assert sample["free"] == 2000000 * KB
    assert sample["buffers"] == 500000 * KB


def test_values_without_a_unit_are_left_alone(box):
    """HugePages_Total is a page count, not kilobytes."""
    assert MemoryCollector().sample(now=0.0)["hugepages_total"] == 0


def test_used_is_total_minus_available_not_total_minus_free(box):
    """Matches free(1): reclaimable page cache is not counted as used.

    Total minus free would report 30 GB used here rather than 24 GB.
    """
    sample = MemoryCollector().sample(now=0.0)
    assert sample["used"] == 24000000 * KB
    assert sample["percent"] == pytest.approx(75.0)


def test_cached_includes_reclaimable_slab(box):
    assert MemoryCollector().sample(now=0.0)["cached"] == 7000000 * KB


def test_available_falls_back_to_free_on_an_ancient_kernel(kernel, monkeypatch):
    monkeypatch.setattr(memory_module, "shutil", SimpleNamespace(which=lambda _: None))
    kernel.write("/proc/meminfo", "MemTotal:  1000 kB\nMemFree:  400 kB\n")
    kernel.patch(memory_module)
    sample = MemoryCollector().sample(now=0.0)
    assert sample["available"] == 400 * KB
    assert sample["used"] == 600 * KB


def test_swap(box):
    sample = MemoryCollector().sample(now=0.0)
    assert sample["swap_total"] == 8000000 * KB
    assert sample["swap_used"] == 2000000 * KB
    assert sample["swap_percent"] == pytest.approx(25.0)


def test_no_swap_does_not_divide_by_zero(kernel, monkeypatch):
    monkeypatch.setattr(memory_module, "shutil", SimpleNamespace(which=lambda _: None))
    kernel.write("/proc/meminfo", "MemTotal: 1000 kB\nMemAvailable: 400 kB\nSwapTotal: 0 kB\n")
    kernel.patch(memory_module)
    assert MemoryCollector().sample(now=0.0)["swap_percent"] == 0.0


def test_an_empty_meminfo_does_not_divide_by_zero(kernel, monkeypatch):
    monkeypatch.setattr(memory_module, "shutil", SimpleNamespace(which=lambda _: None))
    kernel.patch(memory_module)
    sample = MemoryCollector().sample(now=0.0)
    assert sample["total"] == 0
    assert sample["percent"] == 0.0


# ------------------------------------------------------------------ zram


def test_zram_compression_ratio(box):
    box.write_many(
        "/sys/block/zram0",
        {
            "disksize": str(8 * 1024**3),
            # orig, compressed, mem_used_total, then the rest.
            "mm_stat": "8589934592 2147483648 2300000000 0 2400000000 100 0 5 0",
            "comp_algorithm": "[zstd] lzo lz4",
        },
    )
    zram = MemoryCollector().sample(now=0.0)["zram"]
    assert len(zram) == 1
    assert zram[0]["name"] == "zram0"
    assert zram[0]["original"] == 8589934592
    assert zram[0]["compressed"] == 2147483648
    assert zram[0]["ratio"] == pytest.approx(4.0)


def test_zram_with_nothing_stored_has_no_ratio(box):
    """Dividing by a zero compressed size would be a crash, not a ratio."""
    box.write_many(
        "/sys/block/zram0",
        {"disksize": str(8 * 1024**3), "mm_stat": "0 0 0 0 0 0 0 0 0", "comp_algorithm": "zstd"},
    )
    assert MemoryCollector().sample(now=0.0)["zram"][0]["ratio"] is None


def test_an_unconfigured_zram_device_is_skipped(box):
    box.write_many("/sys/block/zram0", {"disksize": "0", "mm_stat": "0 0 0"})
    assert MemoryCollector().sample(now=0.0)["zram"] == []


def test_a_short_mm_stat_row_is_tolerated(box):
    """Trailing columns were added over several kernel releases."""
    box.write_many(
        "/sys/block/zram0",
        {"disksize": str(1024**3), "mm_stat": "1000 500 600", "comp_algorithm": "lzo"},
    )
    zram = MemoryCollector().sample(now=0.0)["zram"]
    assert zram[0]["ratio"] == pytest.approx(2.0)
    assert zram[0]["used_total"] == 600


def test_non_zram_block_devices_are_ignored(box):
    box.write_many("/sys/block/nvme0n1", {"disksize": "1000", "mm_stat": "1 1 1"})
    assert MemoryCollector().sample(now=0.0)["zram"] == []


# -------------------------------------------------------------- dmidecode

_DMIDECODE = """\
# dmidecode 3.5
Getting SMBIOS data from sysfs.
SMBIOS 3.0.0 present.

Handle 0x0035, DMI type 17, 40 bytes
Memory Device
\tArray Handle: 0x0034
\tSize: 16 GB
\tLocator: DIMM_A1
\tType: DDR4
\tSpeed: 3200 MT/s
\tConfigured Memory Speed: 2933 MT/s
\tManufacturer: G.Skill
\tPart Number: F4-3200C16-16GVK

Handle 0x0036, DMI type 17, 40 bytes
Memory Device
\tArray Handle: 0x0034
\tSize: No Module Installed
\tLocator: DIMM_A2
"""


def test_dmidecode_parsing_skips_empty_slots():
    modules = MemoryCollector._parse_dmidecode(_DMIDECODE)
    assert len(modules) == 1
    assert modules[0]["locator"] == "DIMM_A1"
    assert modules[0]["size"] == "16 GB"
    assert modules[0]["type"] == "DDR4"
    assert modules[0]["part"] == "F4-3200C16-16GVK"
    assert modules[0]["manufacturer"] == "G.Skill"


def test_configured_speed_beats_the_rated_speed():
    """"Speed" is what the part is rated for; the DIMM runs at the configured one."""
    modules = MemoryCollector._parse_dmidecode(_DMIDECODE)
    assert modules[0]["speed_mhz"] == 2933
    assert modules[0]["rated_mhz"] == 3200


def test_rated_speed_is_used_when_nothing_is_configured():
    text = "Handle 0x1, DMI type 17\nMemory Device\n\tSize: 8 GB\n\tSpeed: 2400 MT/s\n"
    assert MemoryCollector._parse_dmidecode(text)[0]["speed_mhz"] == 2400


def test_dmidecode_output_with_no_devices_is_empty():
    assert MemoryCollector._parse_dmidecode("# dmidecode 3.5\nNo SMBIOS nor DMI entry point found\n") == []


def test_missing_dmidecode_is_reported_not_raised(box):
    sample = MemoryCollector().sample(now=0.0)
    assert sample["modules"] == []
    assert sample["dimm_error"] == "dmidecode not installed"
    assert sample["speed_mhz"] is None
