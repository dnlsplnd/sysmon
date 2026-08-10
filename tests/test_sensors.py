"""Sensor collector: hwmon discovery, per-class scaling, implausible thresholds."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sysmon.collectors import sensors as sensors_module
from sysmon.collectors.sensors import SensorCollector


@pytest.fixture
def box(kernel):
    """Three chips: a CPU, an NVMe with a sentinel threshold, and a superio."""
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {
            "name": "k10temp",
            "temp1_input": "70000",
            "temp1_label": "Tctl",
            "temp1_max": "95000",
            "temp1_crit": "115000",
        },
    )
    kernel.write_many(
        "/sys/class/hwmon/hwmon1",
        {
            "name": "nvme",
            "temp1_input": "45000",
            "temp1_label": "Composite",
            # "No limit", expressed as an absurd number rather than an absent file.
            "temp1_max": "65261000",
            "temp2_input": "48000",
        },
    )
    kernel.write_many(
        "/sys/class/hwmon/hwmon2",
        {
            "name": "nct6775",
            "fan1_input": "1200",
            "in0_input": "1224",
            "curr1_input": "500",
            "power1_input": "20940000",
        },
    )
    kernel.patch(sensors_module)
    return kernel


def _rail(sample, chip_name, label):
    for chip in sample["chips"]:
        if chip["name"] == chip_name:
            for value in chip["values"]:
                if value["label"] == label:
                    return value
    raise AssertionError(f"no {chip_name}/{label} in sample")


# ------------------------------------------------------------------ scaling


def test_each_class_uses_its_own_scale(box):
    sample = SensorCollector().sample(now=0.0)
    assert _rail(sample, "k10temp", "Tctl")["value"] == pytest.approx(70.0)
    assert _rail(sample, "nct6775", "fan1")["value"] == pytest.approx(1200.0)
    assert _rail(sample, "nct6775", "in0")["value"] == pytest.approx(1.224)
    assert _rail(sample, "nct6775", "curr1")["value"] == pytest.approx(0.5)
    assert _rail(sample, "nct6775", "power1")["value"] == pytest.approx(20.94)


def test_units_are_reported_alongside_the_value(box):
    sample = SensorCollector().sample(now=0.0)
    assert _rail(sample, "k10temp", "Tctl")["unit"] == "°C"
    assert _rail(sample, "nct6775", "fan1")["unit"] == "RPM"
    assert _rail(sample, "nct6775", "power1")["unit"] == "W"


def test_a_rail_class_we_do_not_understand_is_ignored(kernel):
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {"name": "intel-rapl", "energy1_input": "123456", "temp1_input": "40000"},
    )
    kernel.patch(sensors_module)
    sample = SensorCollector().sample(now=0.0)
    labels = [value["label"] for value in sample["chips"][0]["values"]]
    assert labels == ["temp1"]


# --------------------------------------------------------------- thresholds


def test_an_implausible_threshold_is_dropped(box):
    """This NVMe publishes temp1_max as 65261 C to mean "no limit".

    Scaling a bar against that would make every reading look like nothing.
    """
    assert _rail(SensorCollector().sample(now=0.0), "nvme", "Composite")["max"] is None


def test_a_real_threshold_is_kept(box):
    rail = _rail(SensorCollector().sample(now=0.0), "k10temp", "Tctl")
    assert rail["max"] == pytest.approx(95.0)
    assert rail["crit"] == pytest.approx(115.0)


def test_a_threshold_below_the_plausible_floor_is_dropped(kernel):
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {"name": "k10temp", "temp1_input": "50000", "temp1_max": "0"},
    )
    kernel.patch(sensors_module)
    assert SensorCollector().sample(now=0.0)["chips"][0]["values"][0]["max"] is None


def test_plausibility_only_applies_to_temperatures(kernel):
    """A 5000 RPM fan limit is not a temperature and must not be range-checked."""
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {"name": "nct6775", "fan1_input": "1200", "fan1_max": "5000"},
    )
    kernel.patch(sensors_module)
    assert SensorCollector().sample(now=0.0)["chips"][0]["values"][0]["max"] == pytest.approx(5000.0)


# ------------------------------------------------------------------ labels


def test_a_rail_without_a_label_is_named_after_its_file(box):
    assert _rail(SensorCollector().sample(now=0.0), "nvme", "temp2")["value"] == pytest.approx(48.0)


def test_chip_names_are_mapped_to_something_recognisable(box):
    sample = SensorCollector().sample(now=0.0)
    labels = {chip["name"]: chip["label"] for chip in sample["chips"]}
    assert labels["k10temp"] == "CPU (AMD)"
    assert labels["nvme"] == "NVMe SSD"
    assert labels["nct6775"] == "Mainboard (Nuvoton)"


def test_an_unknown_chip_keeps_its_own_name(kernel):
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "acpitz", "temp1_input": "40000"})
    kernel.patch(sensors_module)
    assert SensorCollector().sample(now=0.0)["chips"][0]["label"] == "acpitz"


def test_a_suffixed_chip_name_still_maps(kernel):
    """Drivers register as e.g. iwlwifi_1; the mapping keys off the stem."""
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme_pcie", "temp1_input": "45000"})
    kernel.patch(sensors_module)
    assert SensorCollector().sample(now=0.0)["chips"][0]["label"] == "NVMe SSD"


# ----------------------------------------------------------------- hottest


def test_hottest_is_the_highest_temperature_across_every_chip(box):
    hottest = SensorCollector().sample(now=0.0)["hottest"]
    assert hottest == {"label": "CPU (AMD) Tctl", "value": pytest.approx(70.0)}


def test_hottest_ignores_non_temperature_rails(kernel):
    """A 1200 RPM fan must not out-rank a 45 C drive."""
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {"name": "nct6775", "fan1_input": "1200", "temp1_input": "45000"},
    )
    kernel.patch(sensors_module)
    hottest = SensorCollector().sample(now=0.0)["hottest"]
    assert hottest["value"] == pytest.approx(45.0)


def test_no_sensors_at_all_is_reported_as_unavailable(kernel):
    kernel.patch(sensors_module)
    collector = SensorCollector()
    assert collector.available is False
    assert collector.sample(now=0.0) == {"chips": [], "hottest": None}


# ------------------------------------------------------------------- misc


def test_a_chip_with_no_readable_rails_is_dropped(kernel):
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "asus", "temp1_input": "N/A"})
    kernel.write_many("/sys/class/hwmon/hwmon1", {"name": "k10temp", "temp1_input": "70000"})
    kernel.patch(sensors_module)
    sample = SensorCollector().sample(now=0.0)
    assert [chip["name"] for chip in sample["chips"]] == ["k10temp"]


def test_a_hwmon_directory_without_a_name_is_skipped(kernel):
    kernel.write("/sys/class/hwmon/hwmon0/temp1_input", "70000")
    kernel.patch(sensors_module)
    assert SensorCollector().sample(now=0.0)["chips"] == []


def test_readings_are_grouped_by_kind_then_label(kernel):
    kernel.write_many(
        "/sys/class/hwmon/hwmon0",
        {
            "name": "nct6775",
            "temp1_input": "40000",
            "temp1_label": "SYSTIN",
            "fan2_input": "900",
            "fan1_input": "1200",
            "in0_input": "1224",
        },
    )
    kernel.patch(sensors_module)
    values = SensorCollector().sample(now=0.0)["chips"][0]["values"]
    assert [(v["kind"], v["label"]) for v in values] == [
        ("fan", "fan1"),
        ("fan", "fan2"),
        ("temperature", "SYSTIN"),
        ("voltage", "in0"),
    ]


def test_a_slow_rail_is_served_from_cache_between_refreshes(box):
    """An NVMe composite temperature is a round-trip to the drive controller.

    Rails timed as slow at startup are polled every fifth tick; in between they
    keep reporting their last value rather than blinking out of the table.
    """
    collector = SensorCollector()
    for chip in collector.chips:
        for reading in chip["readings"]:
            reading["slow"] = True

    assert _rail(collector.sample(now=0.0), "nvme", "Composite")["value"] == pytest.approx(45.0)

    box.write("/sys/class/hwmon/hwmon1/temp1_input", "55000")
    for tick in range(1, 4):
        cached = _rail(collector.sample(now=float(tick)), "nvme", "Composite")
        assert cached["value"] == pytest.approx(45.0)

    # Fifth tick: the throttle lets a real read through.
    assert _rail(collector.sample(now=4.0), "nvme", "Composite")["value"] == pytest.approx(55.0)


# ------------------------------------------------- slow-rail classification


def _fake_clock(monkeypatch, durations):
    """Make each probe appear to take the given number of seconds.

    _probe calls perf_counter twice per probe, so each duration becomes a pair.
    """
    ticks = []
    at = 0.0
    for duration in durations:
        ticks += [at, at + duration]
        at += duration + 1.0
    monkeypatch.setattr(
        sensors_module, "time", SimpleNamespace(perf_counter=lambda: ticks.pop(0))
    )


def test_a_rail_is_timed_more_than_once(kernel):
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme", "temp1_input": "45000"})
    kernel.patch(sensors_module)

    reads = []
    original = sensors_module.read_int
    kernel.monkeypatch.setattr(
        sensors_module, "read_int", lambda p, d=None: (reads.append(p), original(p, d))[1]
    )
    SensorCollector()
    probes = [p for p in reads if p.endswith("temp1_input")]
    assert len(probes) == sensors_module._SLOW_RAIL_PROBES


def test_one_slow_probe_does_not_throttle_a_fast_rail(kernel, monkeypatch):
    """The bug this replaced: a hiccup during the single probe was permanent.

    A 50 ms first reading followed by two cheap ones is a fast rail that was
    unlucky, not a slow one, so the median must decide and the rail must stay
    unthrottled. The outlier goes first deliberately: that is where it lands in
    practice, on a cold read or a busy startup.
    """
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nct6775", "temp1_input": "40000"})
    kernel.patch(sensors_module)
    _fake_clock(monkeypatch, [0.050, 0.0001, 0.0001])

    assert SensorCollector().chips[0]["readings"][0]["slow"] is False


def test_one_fast_probe_does_not_rescue_a_slow_rail(kernel, monkeypatch):
    """The mirror image: one cheap first read must not unthrottle an NVMe rail."""
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme", "temp1_input": "45000"})
    kernel.patch(sensors_module)
    _fake_clock(monkeypatch, [0.0001, 0.004, 0.004])

    assert SensorCollector().chips[0]["readings"][0]["slow"] is True


def test_a_consistently_cheap_rail_is_not_throttled(kernel, monkeypatch):
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "k10temp", "temp1_input": "70000"})
    kernel.patch(sensors_module)
    _fake_clock(monkeypatch, [0.0001, 0.0001, 0.0001])

    assert SensorCollector().chips[0]["readings"][0]["slow"] is False


def test_a_rail_that_stops_reading_partway_through_is_dropped(kernel):
    """Probing must abandon the rail rather than average in a failed read."""
    kernel.write_many("/sys/class/hwmon/hwmon0", {"name": "nvme", "temp1_input": "45000"})
    kernel.patch(sensors_module)

    calls = {"n": 0}
    original = sensors_module.read_int

    def flaky(path, default=None):
        if path.endswith("temp1_input"):
            calls["n"] += 1
            if calls["n"] > 1:
                return None
        return original(path, default)

    kernel.monkeypatch.setattr(sensors_module, "read_int", flaky)
    assert SensorCollector().chips == []


def test_a_fast_rail_is_read_every_tick(box):
    collector = SensorCollector()
    assert _rail(collector.sample(now=0.0), "k10temp", "Tctl")["value"] == pytest.approx(70.0)
    box.write("/sys/class/hwmon/hwmon0/temp1_input", "80000")
    assert _rail(collector.sample(now=1.0), "k10temp", "Tctl")["value"] == pytest.approx(80.0)
