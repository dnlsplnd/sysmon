"""Network collector: per-interface rates, link utilisation, session totals."""

from __future__ import annotations

import socket
from collections import namedtuple
from types import SimpleNamespace

import pytest

from sysmon.collectors import network as network_module
from sysmon.collectors.network import NetworkCollector

Addr = namedtuple("Addr", "family address netmask")

# The hub samples with time.time(), so these stand in for epoch seconds.
T0 = 1_700_000_000.0
T1 = T0 + 1.0

_WIRELESS = """\
Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
 wlan0: 0000   70.  -40.  -256        0      0      0      0     36        0
"""


def add_interface(kernel, name, rx=0, tx=0, rx_packets=0, tx_packets=0, **files):
    kernel.write_many(
        f"/sys/class/net/{name}/statistics",
        {
            "rx_bytes": str(rx),
            "tx_bytes": str(tx),
            "rx_packets": str(rx_packets),
            "tx_packets": str(tx_packets),
            "rx_errors": "0",
            "tx_errors": "0",
            "rx_dropped": "0",
            "tx_dropped": "0",
        },
    )
    kernel.write_many(f"/sys/class/net/{name}", files)


@pytest.fixture
def box(kernel, monkeypatch):
    add_interface(
        kernel, "enp5s0", rx=1_000_000, tx=500_000, rx_packets=1000, tx_packets=800,
        speed="1000", operstate="up", address="aa:bb:cc:dd:ee:ff", mtu="1500",
        duplex="full", type="1",
    )
    add_interface(kernel, "lo", rx=500_000, tx=500_000, operstate="up", type="772", mtu="65536")
    monkeypatch.setattr(
        network_module,
        "psutil",
        SimpleNamespace(
            net_if_addrs=lambda: {
                "enp5s0": [
                    Addr(socket.AF_INET, "192.168.1.50", "255.255.255.0"),
                    Addr(socket.AF_INET6, "fe80::1%enp5s0", "ffff:ffff:ffff:ffff::"),
                    Addr(socket.AF_PACKET, "aa:bb:cc:dd:ee:ff", None),
                ]
            }
        ),
    )
    kernel.patch(network_module)
    return kernel


def _iface(sample, name):
    for interface in sample["interfaces"]:
        if interface["name"] == name:
            return interface
    raise AssertionError(f"no interface {name}")


# ------------------------------------------------------------------- rates


def test_the_first_tick_has_no_rates(box):
    interface = _iface(NetworkCollector().sample(now=T0), "enp5s0")
    assert interface["rx_bps"] is None
    assert interface["tx_bps"] is None


def test_rates_come_from_the_counter_delta(box):
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "2250000")
    box.write("/sys/class/net/enp5s0/statistics/tx_bytes", "700000")
    interface = _iface(collector.sample(now=T1), "enp5s0")
    assert interface["rx_bps"] == pytest.approx(1_250_000.0)
    assert interface["tx_bps"] == pytest.approx(200_000.0)


def test_packet_rates_are_tracked_too(box):
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_packets", "3000")
    interface = _iface(collector.sample(now=T1), "enp5s0")
    assert interface["rx_pps"] == pytest.approx(2000.0)


def test_peaks_are_remembered_across_ticks(box):
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "2250000")
    collector.sample(now=T1)
    # A quiet second afterwards must not erase the peak.
    interface = _iface(collector.sample(now=T1 + 1.0), "enp5s0")
    assert interface["rx_bps"] == pytest.approx(0.0)
    assert interface["peak_rx_bps"] == pytest.approx(1_250_000.0)


# ------------------------------------------------------------- utilisation


def test_utilisation_is_measured_against_the_link_rate(box):
    """1.25 MB/s is 10 Mbit/s, which is 1% of a gigabit link."""
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "2250000")
    interface = _iface(collector.sample(now=T1), "enp5s0")
    assert interface["link_mbit"] == 1000
    assert interface["rx_utilisation"] == pytest.approx(1.0)


def test_a_link_with_no_known_rate_has_no_utilisation(box):
    """The kernel reports -1 for a down or virtual link."""
    box.write("/sys/class/net/enp5s0/speed", "-1")
    interface = _iface(NetworkCollector().sample(now=T0), "enp5s0")
    assert interface["link_mbit"] is None
    assert interface["rx_utilisation"] is None


def test_a_missing_speed_file_has_no_utilisation(box):
    interface = _iface(NetworkCollector().sample(now=T0), "lo")
    assert interface["link_mbit"] is None
    assert interface["tx_utilisation"] is None


# -------------------------------------------------------------------- kind


def test_an_ethernet_interface(box):
    assert _iface(NetworkCollector().sample(now=T0), "enp5s0")["kind"] == "Ethernet"


def test_loopback(box):
    assert _iface(NetworkCollector().sample(now=T0), "lo")["kind"] == "Loopback"


def test_wireless_is_detected_from_the_wireless_directory(box):
    add_interface(box, "wlan0", operstate="up", type="1")
    box.write("/sys/class/net/wlan0/wireless/link", "70")
    assert _iface(NetworkCollector().sample(now=T0), "wlan0")["kind"] == "Wi-Fi"


def test_a_tunnel(box):
    add_interface(box, "tun0", operstate="unknown", tun_flags="0x1001")
    assert _iface(NetworkCollector().sample(now=T0), "tun0")["kind"] == "VPN/TUN"


def test_a_bridge(box):
    add_interface(box, "virbr0", operstate="down", type="1")
    box.write("/sys/class/net/virbr0/bridge/stp_state", "0")
    assert _iface(NetworkCollector().sample(now=T0), "virbr0")["kind"] == "Bridge"


def test_anything_else(box):
    add_interface(box, "sit0", operstate="down", type="776")
    assert _iface(NetworkCollector().sample(now=T0), "sit0")["kind"] == "Other"


def test_bonding_masters_is_not_an_interface(box):
    box.write("/sys/class/net/bonding_masters", "")
    names = [i["name"] for i in NetworkCollector().sample(now=T0)["interfaces"]]
    assert "bonding_masters" not in names


# ---------------------------------------------------------------- wireless


def test_signal_quality_is_parsed_from_proc_net_wireless(box):
    add_interface(box, "wlan0", operstate="up")
    box.write("/sys/class/net/wlan0/wireless/link", "70")
    box.write("/proc/net/wireless", _WIRELESS)
    wireless = _iface(NetworkCollector().sample(now=T0), "wlan0")["wireless"]
    # The columns carry a trailing '.' from the file's fixed-width layout.
    assert wireless == {"quality": 70.0, "signal_dbm": -40.0, "noise_dbm": -256.0}


def test_a_wired_interface_has_no_wireless_block(box):
    assert _iface(NetworkCollector().sample(now=T0), "enp5s0")["wireless"] is None


# --------------------------------------------------------------- addresses


def test_addresses_exclude_the_link_layer_entry(box):
    """The MAC is already read from sysfs; repeating it as an address is noise."""
    addresses = _iface(NetworkCollector().sample(now=T0), "enp5s0")["addresses"]
    assert addresses == [
        {"family": "IPv4", "address": "192.168.1.50", "netmask": "255.255.255.0"},
        {"family": "IPv6", "address": "fe80::1", "netmask": "ffff:ffff:ffff:ffff::"},
    ]


def test_the_mac_and_mtu_come_from_sysfs(box):
    interface = _iface(NetworkCollector().sample(now=T0), "enp5s0")
    assert interface["mac"] == "aa:bb:cc:dd:ee:ff"
    assert interface["mtu"] == 1500
    assert interface["duplex"] == "full"
    assert interface["up"] is True


# ---------------------------------------------------------------- totals


def test_totals_exclude_loopback(box):
    """Loopback traffic is not traffic leaving the machine."""
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "2250000")
    box.write("/sys/class/net/lo/statistics/rx_bytes", "9500000")
    totals = collector.sample(now=T1)["totals"]
    assert totals["rx_bps"] == pytest.approx(1_250_000.0)
    assert totals["rx_bytes"] == 2_250_000


def test_session_counters_are_relative_to_the_first_sample(box):
    collector = NetworkCollector()
    collector._session_start = T0
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "3000000")
    session = collector.sample(now=T1)["session"]
    assert session["rx"] == 2_000_000
    assert session["elapsed"] == pytest.approx(1.0)
    assert session["avg_rx_bps"] == pytest.approx(2_000_000.0)


def test_resetting_the_session_rebaselines_it(box):
    collector = NetworkCollector()
    collector.sample(now=T0)
    box.write("/sys/class/net/enp5s0/statistics/rx_bytes", "3000000")
    collector.sample(now=T1)

    collector.reset_session()
    assert collector.sample(now=T1 + 1.0)["session"]["rx"] == 0


def test_interfaces_are_sorted_with_loopback_last(box):
    add_interface(box, "docker0", operstate="down", type="1")
    names = [i["name"] for i in NetworkCollector().sample(now=T0)["interfaces"]]
    assert names == ["enp5s0", "docker0", "lo"]


# ------------------------------------------------------------- connections


def test_connections_are_annotated_with_the_owning_process(box, monkeypatch):
    Conn = namedtuple("Conn", "fd family type laddr raddr status pid")
    Addr2 = namedtuple("Addr2", "ip port")

    monkeypatch.setattr(
        network_module.psutil,
        "net_connections",
        lambda kind: [
            Conn(1, socket.AF_INET, socket.SOCK_STREAM,
                 Addr2("192.168.1.50", 44321), Addr2("140.82.121.4", 443), "ESTABLISHED", 4242),
            Conn(2, socket.AF_INET6, socket.SOCK_DGRAM, Addr2("::", 5353), None, "NONE", None),
        ],
        raising=False,
    )
    monkeypatch.setattr(
        network_module.psutil, "Process", lambda pid: SimpleNamespace(name=lambda: "firefox"),
        raising=False,
    )

    rows = NetworkCollector().connections()
    assert rows[0]["status"] == "ESTABLISHED"
    assert rows[0]["proto"] == "TCP"
    assert rows[0]["family"] == "IPv4"
    assert rows[0]["local"] == "192.168.1.50:44321"
    assert rows[0]["remote"] == "140.82.121.4:443"
    assert rows[0]["process"] == "firefox"
    # The UDP listener has no peer and no owner we could name.
    assert rows[1]["proto"] == "UDP"
    assert rows[1]["remote"] == ""
    assert rows[1]["process"] == ""
