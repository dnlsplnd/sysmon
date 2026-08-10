"""Disk collector: diskstats arithmetic, device classification, health parsing."""

from __future__ import annotations

from collections import namedtuple
from types import SimpleNamespace

import pytest

from sysmon.collectors import disk as disk_module
from sysmon.collectors.disk import DiskCollector

Part = namedtuple("Part", "device mountpoint fstype opts")
Usage = namedtuple("Usage", "total used free percent")

# major minor name, then: reads, merges, sectors-read, ms-reading, writes,
# merges, sectors-written, ms-writing, in-flight, io_ticks, time-in-queue, ...
_STATS_FIRST = "259 0 nvme0n1 1000 0 20000 500 2000 0 40000 800 0 5000 6000 0 0 0 0 0\n"

# One second later: +100 reads, +20000 sectors read, +200 ms reading,
# +200 writes, +20000 sectors written, +200 ms writing, +500 ms of busy queue.
_STATS_SECOND = "259 0 nvme0n1 1100 0 40000 700 2200 0 60000 1000 3 5500 6500 0 0 0 0 0\n"


def add_nvme(kernel, name="nvme0n1", sectors=2_000_409_264, rotational="0"):
    kernel.write_many(
        f"/sys/block/{name}/queue",
        {"logical_block_size": "512", "rotational": rotational, "scheduler": "[none] mq-deadline"},
    )
    kernel.write(f"/sys/block/{name}/size", str(sectors))
    kernel.write(f"/sys/block/{name}/device/model", "Samsung SSD 980 PRO 1TB")


@pytest.fixture
def box(kernel, monkeypatch):
    add_nvme(kernel)
    kernel.write("/proc/diskstats", _STATS_FIRST)
    monkeypatch.setattr(
        disk_module,
        "psutil",
        SimpleNamespace(
            disk_partitions=lambda all=False: [
                Part("/dev/nvme0n1p2", "/", "btrfs", "rw,relatime"),
                Part("/dev/nvme0n1p1", "/boot/efi", "vfat", "rw"),
            ],
            disk_usage=lambda mountpoint: {
                "/": Usage(1_000_000_000_000, 400_000_000_000, 600_000_000_000, 40.0),
                "/boot/efi": Usage(600_000_000, 60_000_000, 540_000_000, 10.0),
            }[mountpoint],
        ),
    )
    kernel.patch(disk_module)
    return kernel


# ------------------------------------------------------------- classification


def test_a_whole_disk_is_kept(box):
    collector = DiskCollector()
    assert list(collector.devices) == ["nvme0n1"]
    assert collector.devices["nvme0n1"]["model"] == "Samsung SSD 980 PRO 1TB"


@pytest.mark.parametrize("name", ["loop0", "ram0", "dm-0", "sr0"])
def test_virtual_and_optical_devices_are_dropped(kernel, name):
    add_nvme(kernel, name=name)
    kernel.patch(disk_module)
    assert DiskCollector().devices == {}


def test_a_partition_is_dropped(kernel):
    """Partitions have no queue directory of their own."""
    kernel.write("/sys/block/nvme0n1p1/size", "1000")
    kernel.write("/sys/block/nvme0n1p1/partition", "1")
    kernel.patch(disk_module)
    assert DiskCollector().devices == {}


def test_a_card_reader_with_no_media_is_dropped(kernel):
    add_nvme(kernel, name="mmcblk0", sectors=0)
    kernel.patch(disk_module)
    assert DiskCollector().devices == {}


@pytest.mark.parametrize(
    "name, rotational, expected",
    [("nvme0n1", "0", "NVMe SSD"), ("sda", "1", "HDD"), ("sdb", "0", "SSD")],
)
def test_device_kind(kernel, name, rotational, expected):
    add_nvme(kernel, name=name, rotational=rotational)
    kernel.patch(disk_module)
    assert DiskCollector().devices[name]["kind"] == expected


def test_size_uses_the_fixed_512_byte_sector(box):
    """sysfs reports size in 512-byte sectors whatever the device's real block size."""
    assert DiskCollector().devices["nvme0n1"]["size"] == 2_000_409_264 * 512


def test_the_active_scheduler_is_the_bracketed_one(box):
    assert DiskCollector().devices["nvme0n1"]["scheduler"] == "none"


def test_a_scheduler_list_without_brackets_falls_back_to_the_first(kernel):
    add_nvme(kernel)
    kernel.write("/sys/block/nvme0n1/queue/scheduler", "none")
    kernel.patch(disk_module)
    assert DiskCollector().devices["nvme0n1"]["scheduler"] == "none"


def test_the_model_falls_back_to_the_device_name(kernel):
    add_nvme(kernel)
    (kernel.root / "sys/block/nvme0n1/device/model").unlink()
    kernel.patch(disk_module)
    assert DiskCollector().devices["nvme0n1"]["model"] == "nvme0n1"


# -------------------------------------------------------------------- hwmon


def test_hwmon_nested_under_the_device(box):
    """Layout A: <device>/hwmon/hwmonN."""
    box.write("/sys/block/nvme0n1/device/hwmon/hwmon3/temp1_input", "42000")
    assert DiskCollector().sample(now=0.0)["devices"][0]["temp"] == pytest.approx(42.0)


def test_hwmon_linked_beside_the_device(kernel, monkeypatch):
    """Layout B: <device>/hwmonN, which also occurs in the wild."""
    add_nvme(kernel)
    kernel.write("/proc/diskstats", _STATS_FIRST)
    kernel.write("/sys/block/nvme0n1/device/hwmon2/temp1_input", "38000")
    monkeypatch.setattr(disk_module, "psutil", SimpleNamespace(disk_partitions=lambda all=False: []))
    kernel.patch(disk_module)
    assert DiskCollector().sample(now=0.0)["devices"][0]["temp"] == pytest.approx(38.0)


def test_no_hwmon_means_no_temperature(box):
    assert DiskCollector().sample(now=0.0)["devices"][0]["temp"] is None


def test_drive_temperature_is_throttled_to_every_fifth_tick(box):
    """An NVMe composite temperature is a controller round-trip, not a memory read."""
    box.write("/sys/block/nvme0n1/device/hwmon/hwmon3/temp1_input", "42000")
    collector = DiskCollector()
    assert collector.sample(now=0.0)["devices"][0]["temp"] == pytest.approx(42.0)

    box.write("/sys/block/nvme0n1/device/hwmon/hwmon3/temp1_input", "50000")
    for tick in range(1, 4):
        assert collector.sample(now=float(tick))["devices"][0]["temp"] == pytest.approx(42.0)
    assert collector.sample(now=4.0)["devices"][0]["temp"] == pytest.approx(50.0)


# ----------------------------------------------------------------- sampling


def test_the_first_tick_has_no_rates(box):
    device = DiskCollector().sample(now=0.0)["devices"][0]
    assert device["read_bps"] is None
    assert device["write_bps"] is None
    assert device["utilisation"] is None


def test_throughput_uses_512_byte_sectors(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    box.write("/proc/diskstats", _STATS_SECOND)
    device = collector.sample(now=1.0)["devices"][0]
    assert device["read_bps"] == pytest.approx(20000 * 512)
    assert device["write_bps"] == pytest.approx(20000 * 512)
    assert device["total_bps"] == pytest.approx(2 * 20000 * 512)


def test_iops(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    box.write("/proc/diskstats", _STATS_SECOND)
    device = collector.sample(now=1.0)["devices"][0]
    assert device["read_iops"] == pytest.approx(100.0)
    assert device["write_iops"] == pytest.approx(200.0)


def test_utilisation_is_io_ticks_over_wall_clock(box):
    """io_ticks counts milliseconds with a non-empty queue: 500 of 1000 is 50%."""
    collector = DiskCollector()
    collector.sample(now=0.0)
    box.write("/proc/diskstats", _STATS_SECOND)
    assert collector.sample(now=1.0)["devices"][0]["utilisation"] == pytest.approx(50.0)


def test_utilisation_cannot_exceed_a_hundred(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    # io_ticks jumps 5000 ms in a 1000 ms interval, which a queue can do.
    box.write("/proc/diskstats", "259 0 nvme0n1 1100 0 40000 700 2200 0 60000 1000 3 10000 6500 0 0 0 0 0\n")
    assert collector.sample(now=1.0)["devices"][0]["utilisation"] == pytest.approx(100.0)


def test_service_latency_is_milliseconds_over_operations(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    box.write("/proc/diskstats", _STATS_SECOND)
    device = collector.sample(now=1.0)["devices"][0]
    assert device["read_latency_ms"] == pytest.approx(2.0)   # 200 ms / 100 reads
    assert device["write_latency_ms"] == pytest.approx(1.0)  # 200 ms / 200 writes


def test_latency_is_absent_when_nothing_happened(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    device = collector.sample(now=1.0)["devices"][0]
    assert device["read_latency_ms"] is None
    assert device["write_latency_ms"] is None


def test_in_flight_is_reported_as_is(box):
    collector = DiskCollector()
    collector.sample(now=0.0)
    box.write("/proc/diskstats", _STATS_SECOND)
    assert collector.sample(now=1.0)["devices"][0]["in_flight"] == 3


def test_a_truncated_diskstats_line_is_skipped(box):
    box.write("/proc/diskstats", "259 0 nvme0n1 1000 0 20000\n")
    assert DiskCollector().sample(now=0.0)["devices"] == []


def test_a_device_missing_from_diskstats_is_skipped(box):
    box.write("/proc/diskstats", "8 0 sda 1 2 3 4 5 6 7 8 9 10 11 0 0 0 0 0\n")
    assert DiskCollector().sample(now=0.0)["devices"] == []


# ------------------------------------------------------------- rediscovery


def _count_discoveries(collector):
    calls = []
    original = collector._discover
    collector._discover = lambda: (calls.append(1), original())[1]
    return calls


def test_a_steady_set_of_drives_is_not_rescanned(box):
    collector = DiskCollector()
    calls = _count_discoveries(collector)
    for tick in range(5):
        collector.sample(now=float(tick))
    assert calls == []


def test_an_empty_card_reader_does_not_cause_a_rescan_every_tick(box):
    """A slot with no media is physical but sizeless, so _discover drops it.

    Comparing without the size check would leave the two sets permanently
    disagreeing, and the expensive metadata walk would rerun on every tick for
    as long as the slot stayed empty.
    """
    add_nvme(box, name="mmcblk0", sectors=0)
    collector = DiskCollector()
    calls = _count_discoveries(collector)
    for tick in range(5):
        collector.sample(now=float(tick))
    assert calls == []


def test_inserting_media_is_picked_up(box):
    add_nvme(box, name="mmcblk0", sectors=0)
    collector = DiskCollector()
    assert "mmcblk0" not in collector.devices

    box.write("/sys/block/mmcblk0/size", "62521344")
    collector.sample(now=1.0)
    assert "mmcblk0" in collector.devices


def test_a_removed_drive_is_dropped(box):
    collector = DiskCollector()
    assert "nvme0n1" in collector.devices

    import shutil as _shutil
    _shutil.rmtree(box.root / "sys/block/nvme0n1")
    collector.sample(now=1.0)
    assert collector.devices == {}


# ------------------------------------------------------------- filesystems


def test_filesystems_are_sorted_largest_first(box):
    filesystems = DiskCollector().sample(now=0.0)["filesystems"]
    assert [fs["mountpoint"] for fs in filesystems] == ["/", "/boot/efi"]
    assert filesystems[0]["percent"] == 40.0
    assert filesystems[0]["fstype"] == "btrfs"


def test_an_unreadable_mountpoint_is_skipped(kernel, monkeypatch):
    def explode(mountpoint):
        raise PermissionError(mountpoint)

    add_nvme(kernel)
    kernel.write("/proc/diskstats", _STATS_FIRST)
    monkeypatch.setattr(
        disk_module,
        "psutil",
        SimpleNamespace(
            disk_partitions=lambda all=False: [Part("/dev/sdz", "/mnt/locked", "nfs", "rw")],
            disk_usage=explode,
        ),
    )
    kernel.patch(disk_module)
    assert DiskCollector().sample(now=0.0)["filesystems"] == []


# ------------------------------------------------------------------- SMART


def test_nvme_health_is_read_from_the_nvme_log(box, monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "model_name": "Samsung SSD 980 PRO 1TB",
        "serial_number": "S1234",
        "firmware_version": "5B2QGXA7",
        "nvme_smart_health_information_log": {
            "power_on_hours": 4200,
            "power_cycles": 900,
            "percentage_used": 3,
            "media_errors": 0,
            "data_units_read": 1000,
            "data_units_written": 2000,
        },
    }
    _stub_smartctl(monkeypatch, payload)

    result = DiskCollector().fetch_smart("nvme0n1", interactive=False)
    assert result["passed"] is True
    assert result["serial"] == "S1234"
    assert result["attributes"]["Power-on hours"] == 4200
    # Data units are 512 bytes x 1000, per the NVMe spec.
    assert result["attributes"]["Data read"] == 1000 * 512 * 1000


def test_ata_health_is_read_from_the_attribute_table(box, monkeypatch):
    payload = {
        "smart_status": {"passed": True},
        "model_name": "WDC WD40EFRX",
        "ata_smart_attributes": {
            "table": [
                {"name": "Reallocated_Sector_Ct", "raw": {"value": 0}},
                {"name": "Temperature_Celsius", "raw": {"value": 38}},
            ]
        },
        "power_on_time": {"hours": 51234},
    }
    _stub_smartctl(monkeypatch, payload)

    result = DiskCollector().fetch_smart("sda", interactive=False)
    assert result["attributes"]["Reallocated_Sector_Ct"] == 0
    assert result["attributes"]["Power-on hours"] == 51234


def test_smart_results_are_cached_into_later_samples(box, monkeypatch):
    _stub_smartctl(monkeypatch, {"smart_status": {"passed": True}, "model_name": "x"})
    collector = DiskCollector()
    collector.fetch_smart("nvme0n1", interactive=False)
    assert collector.sample(now=0.0)["devices"][0]["smart"]["passed"] is True


def test_missing_smartctl_is_reported_not_raised(box, monkeypatch):
    monkeypatch.setattr(disk_module, "shutil", SimpleNamespace(which=lambda _: None))
    assert DiskCollector().fetch_smart("nvme0n1") == {"error": "smartctl not installed"}


def test_unparseable_smart_output_is_reported(box, monkeypatch):
    _stub_smartctl(monkeypatch, None, stdout="not json at all")
    result = DiskCollector().fetch_smart("nvme0n1", interactive=False)
    assert "error" in result


def _stub_smartctl(monkeypatch, payload, stdout=None):
    import json

    monkeypatch.setattr(disk_module, "shutil", SimpleNamespace(which=lambda name: f"/usr/sbin/{name}"))
    monkeypatch.setattr(
        disk_module,
        "subprocess",
        SimpleNamespace(
            run=lambda *a, **kw: SimpleNamespace(
                returncode=0, stdout=stdout if stdout is not None else json.dumps(payload)
            ),
            SubprocessError=Exception,
        ),
    )
