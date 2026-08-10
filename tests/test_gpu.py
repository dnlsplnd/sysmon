"""GPU collector: DRM fdinfo accounting, engine normalisation, clock fallback.

The de-duplication and capacity tests are the important ones. Both produce
plausible-looking numbers when they are wrong -- a doubled busy figure or a
100%-pegged engine -- so nothing but an arranged fixture catches them.
"""

from __future__ import annotations

import pytest

from sysmon.collectors import gpu as gpu_module
from sysmon.collectors.gpu import GpuCollector

PDEV = "0000:03:00.0"
DEVICE = f"/sys/devices/pci0000:00/{PDEV}"

# A PCI BAR 2 of exactly 8 GiB -- a resizable aperture, not the card's capacity.
_RESOURCE = """\
0x0000000000000000 0x0000000000000000 0x0000000000000000
0x00000000d0000000 0x00000000d0ffffff 0x000000000014220c
0x0000000060000000 0x000000025fffffff 0x000000000014220c
"""


def fdinfo(client_id, engines=(("render", 0),), capacities=(), vram_kib=None, pdev=PDEV):
    """One DRM fdinfo file, in the layout i915 publishes."""
    lines = [
        "pos:\t0",
        "flags:\t02100002",
        "drm-driver:\ti915",
        f"drm-pdev:\t{pdev}",
        f"drm-client-id:\t{client_id}",
    ]
    for engine, nanoseconds in engines:
        lines.append(f"drm-engine-{engine}:\t{nanoseconds} ns")
    for engine, capacity in capacities:
        lines.append(f"drm-engine-capacity-{engine}:\t{capacity}")
    if vram_kib is not None:
        lines.append(f"drm-resident-local0:\t{vram_kib} KiB")
    return "\n".join(lines) + "\n"


def add_card(kernel, driver="i915", device_id="0x56a5", actual="1550", requested="1550"):
    """A DRM card, wired up the way sysfs really is: device is a symlink."""
    kernel.mkdir(f"{DEVICE}")
    # As in real sysfs: card0/device is a symlink up into /sys/devices, and the
    # driver is identified by the basename of a further link.
    kernel.symlink("/sys/class/drm/card0/device", f"../../../devices/pci0000:00/{PDEV}")
    kernel.symlink(f"{DEVICE}/driver", f"../../../bus/pci/drivers/{driver}")
    kernel.write(f"{DEVICE}/device", device_id)
    kernel.write(f"{DEVICE}/resource", _RESOURCE)
    kernel.mkdir("/sys/class/drm/card0/engine/rcs0")
    kernel.write("/sys/class/drm/card0/gt_act_freq_mhz", actual)
    kernel.write("/sys/class/drm/card0/gt_cur_freq_mhz", requested)
    kernel.write("/sys/class/drm/card0/gt_RP0_freq_mhz", "2400")
    kernel.write("/sys/class/drm/card0/gt_RPn_freq_mhz", "300")


def add_client(kernel, pid, fds):
    """A process holding DRM file descriptors. ``fds`` maps fd number to content."""
    kernel.write(f"/proc/{pid}/comm", f"process{pid}")
    for fd, content in fds.items():
        kernel.symlink(f"/proc/{pid}/fd/{fd}", "/dev/dri/renderD128")
        kernel.write(f"/proc/{pid}/fdinfo/{fd}", content)


@pytest.fixture
def box(kernel):
    add_card(kernel)
    kernel.patch(gpu_module)
    return kernel


# --------------------------------------------------------------- discovery


def test_an_intel_card_is_found(box):
    collector = GpuCollector()
    assert collector.available is True
    assert collector.cards[0]["driver"] == "i915"
    assert collector.cards[0]["pdev"] == PDEV


def test_the_model_name_comes_from_the_pci_id(box):
    assert GpuCollector().cards[0]["name"] == "Intel Arc A380"


def test_an_unknown_pci_id_falls_back_to_the_id_itself(kernel):
    add_card(kernel, device_id="0x1234")
    kernel.patch(gpu_module)
    assert GpuCollector().cards[0]["name"] == "Intel GPU 0x1234"


def test_a_card_from_another_driver_is_ignored(kernel):
    add_card(kernel, driver="amdgpu")
    kernel.patch(gpu_module)
    collector = GpuCollector()
    assert collector.cards == []
    assert collector.available is False
    assert collector.error == "no supported DRM card found"


def test_no_card_at_all(kernel):
    kernel.patch(gpu_module)
    assert GpuCollector().sample(now=0.0) == {"cards": [], "available": False}


# --------------------------------------------------------------- fdinfo


def test_one_client_on_several_descriptors_is_counted_once(box):
    """The correctness detail this collector turns on.

    A process can hold the same DRM client on several file descriptors.
    Counting per-descriptor would multiply busy time by the number of
    duplicates -- here, reporting 100% for a GPU that is half busy.
    """
    add_client(box, 1234, {
        3: fdinfo(42, engines=[("render", 1_000_000_000)]),
        4: fdinfo(42, engines=[("render", 1_000_000_000)]),
    })
    collector = GpuCollector()
    collector.sample(now=0.0)

    add_client(box, 1234, {
        3: fdinfo(42, engines=[("render", 1_500_000_000)]),
        4: fdinfo(42, engines=[("render", 1_500_000_000)]),
    })
    card = collector.sample(now=1.0)["cards"][0]
    assert card["busy"] == pytest.approx(50.0)


def test_distinct_clients_are_summed(box):
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 0)])})
    add_client(box, 5678, {3: fdinfo(43, engines=[("render", 0)])})
    collector = GpuCollector()
    collector.sample(now=0.0)

    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 300_000_000)])})
    add_client(box, 5678, {3: fdinfo(43, engines=[("render", 200_000_000)])})
    card = collector.sample(now=1.0)["cards"][0]
    assert card["busy"] == pytest.approx(50.0)
    assert card["clients"] == 2


def test_the_headline_figure_is_the_busiest_engine_not_the_sum(box):
    """Engines run in parallel, so summing them could exceed 100%."""
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 0), ("video", 0)])})
    collector = GpuCollector()
    collector.sample(now=0.0)

    add_client(box, 1234, {
        3: fdinfo(42, engines=[("render", 300_000_000), ("video", 500_000_000)])
    })
    card = collector.sample(now=1.0)["cards"][0]
    assert card["engines"] == {"Render/3D": pytest.approx(30.0), "Video": pytest.approx(50.0)}
    assert card["busy"] == pytest.approx(50.0)


def test_busy_time_is_normalised_by_engine_capacity(box):
    """An engine class with two instances can bank 2 ns of busy time per ns."""
    add_client(box, 1234, {
        3: fdinfo(42, engines=[("video", 0)], capacities=[("video", 2)])
    })
    collector = GpuCollector()
    collector.sample(now=0.0)

    add_client(box, 1234, {
        3: fdinfo(42, engines=[("video", 1_000_000_000)], capacities=[("video", 2)])
    })
    card = collector.sample(now=1.0)["cards"][0]
    assert card["engines"]["Video"] == pytest.approx(50.0)


def test_utilisation_is_capped_at_a_hundred_percent(box):
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 0)])})
    collector = GpuCollector()
    collector.sample(now=0.0)
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 5_000_000_000)])})
    assert collector.sample(now=1.0)["cards"][0]["busy"] == pytest.approx(100.0)


def test_the_first_tick_has_no_utilisation(box):
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 1_000_000_000)])})
    assert GpuCollector().sample(now=0.0)["cards"][0]["busy"] is None


def test_a_client_on_another_card_is_ignored(box):
    add_client(box, 1234, {3: fdinfo(42, engines=[("render", 0)], pdev="0000:09:00.0")})
    collector = GpuCollector()
    collector.sample(now=0.0)
    add_client(box, 1234, {
        3: fdinfo(42, engines=[("render", 900_000_000)], pdev="0000:09:00.0")
    })
    card = collector.sample(now=1.0)["cards"][0]
    assert card["clients"] == 0
    assert card["busy"] is None


def test_descriptors_that_are_not_drm_are_skipped(box):
    box.write("/proc/1234/comm", "process1234")
    box.symlink("/proc/1234/fd/3", "/home/dnsk/some.log")
    box.write("/proc/1234/fdinfo/3", "pos:\t0\n")
    assert GpuCollector().sample(now=0.0)["cards"][0]["clients"] == 0


# ------------------------------------------------------------------ memory


def test_resident_local_memory_is_summed_as_vram(box):
    add_client(box, 1234, {3: fdinfo(42, vram_kib=524288)})
    card = GpuCollector().sample(now=0.0)["cards"][0]
    assert card["vram_used"] == 524288 * 1024


def test_vram_is_attributed_per_process(box):
    add_client(box, 1234, {3: fdinfo(42, vram_kib=524288)})
    add_client(box, 5678, {3: fdinfo(43, vram_kib=262144)})
    card = GpuCollector().sample(now=0.0)["cards"][0]
    by_pid = {row["pid"]: row for row in card["processes"]}
    assert by_pid[1234]["vram"] == 524288 * 1024
    assert by_pid[5678]["vram"] == 262144 * 1024
    assert by_pid[1234]["name"] == "process1234"


def test_the_aperture_is_reported_as_an_aperture_not_as_capacity(box):
    """The BAR is a resizable window -- 8 GiB here for a 6 GB card.

    Reporting it as VRAM total would be wrong, so no such field exists.
    """
    card = GpuCollector().sample(now=0.0)["cards"][0]
    assert card["lmem_aperture"] == 8 * 1024**3
    assert "vram_total" not in card


def test_a_malformed_resource_file_yields_no_aperture(kernel):
    add_card(kernel)
    kernel.write(f"{DEVICE}/resource", "nonsense\n")
    kernel.patch(gpu_module)
    assert GpuCollector().sample(now=0.0)["cards"][0]["lmem_aperture"] is None


# ------------------------------------------------------------------ clocks


def test_a_gated_clock_falls_back_to_the_requested_one(box):
    """gt_act_freq_mhz reads 0 whenever the sample lands during RC6.

    That happens constantly even on a busy card, so reporting it literally
    would show "0 MHz" for a GPU doing real work.
    """
    box.write("/sys/class/drm/card0/gt_act_freq_mhz", "0")
    card = GpuCollector().sample(now=0.0)["cards"][0]
    assert card["freq_gated"] is True
    assert card["freq_actual_mhz"] == 0
    assert card["freq_mhz"] == 1550


def test_a_live_clock_is_used_as_is(box):
    card = GpuCollector().sample(now=0.0)["cards"][0]
    assert card["freq_gated"] is False
    assert card["freq_mhz"] == 1550
    assert card["freq_max_mhz"] == 2400
    assert card["freq_min_mhz"] == 300


def test_the_newer_multi_tile_clock_layout_is_found(kernel):
    """Newer i915 nests these under gt/gt0/rps_* instead of flat on the card."""
    add_card(kernel)
    for name in ("gt_act_freq_mhz", "gt_cur_freq_mhz", "gt_RP0_freq_mhz", "gt_RPn_freq_mhz"):
        (kernel.root / "sys/class/drm/card0" / name).unlink()
    kernel.write("/sys/class/drm/card0/gt/gt0/rps_act_freq_mhz", "1200")
    kernel.write("/sys/class/drm/card0/gt/gt0/rps_cur_freq_mhz", "1300")
    kernel.patch(gpu_module)
    assert GpuCollector().sample(now=0.0)["cards"][0]["freq_mhz"] == 1200


# ------------------------------------------------------------------- hwmon


def test_power_is_derived_from_the_energy_counter(box):
    """energy1_input is cumulative microjoules; watts is its delta over time."""
    box.write(f"{DEVICE}/hwmon/hwmon5/energy1_input", "0")
    box.write(f"{DEVICE}/hwmon/hwmon5/temp1_input", "55000")
    box.write(f"{DEVICE}/hwmon/hwmon5/fan1_input", "1400")
    box.write(f"{DEVICE}/hwmon/hwmon5/power1_max", "35000000")
    collector = GpuCollector()
    collector.sample(now=0.0)

    box.write(f"{DEVICE}/hwmon/hwmon5/energy1_input", "20000000")
    card = collector.sample(now=1.0)["cards"][0]
    assert card["power_w"] == pytest.approx(20.0)
    assert card["temp"] == pytest.approx(55.0)
    assert card["fan_rpm"] == 1400
    assert card["power_limit_w"] == pytest.approx(35.0)


def test_a_card_without_hwmon_reports_no_thermals(box):
    card = GpuCollector().sample(now=0.0)["cards"][0]
    assert card["temp"] is None
    assert card["power_w"] is None
    assert card["fan_rpm"] is None
