"""Overview: the at-a-glance page -- four gauges and the four headline graphs."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import Gauge, GraphCard, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import (  # noqa: E402
    fmt_bytes,
    fmt_duration,
    fmt_hz,
    fmt_pct,
    fmt_rate,
    fmt_temp,
    fmt_watts,
)


class GaugeCard(Gtk.Box):
    """A gauge with a title above and a free-text detail line below."""

    def __init__(self, title: str, slot: int) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")
        self.set_hexpand(True)

        label = Gtk.Label(label=title, xalign=0.5)
        label.add_css_class("heading")
        self.append(label)

        self.gauge = Gauge(size=132, slot=slot)
        self.gauge.set_halign(Gtk.Align.CENTER)
        self.append(self.gauge)

        self.detail = Gtk.Label(label="", xalign=0.5)
        self.detail.add_css_class("caption")
        self.detail.add_css_class("dim-label")
        self.detail.set_wrap(True)
        self.detail.set_justify(Gtk.Justification.CENTER)
        self.append(self.detail)


class OverviewPage(Page):
    title = "Overview"
    icons = ("view-grid-symbolic", "view-dual-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        gauges = row(homogeneous=True)
        self.cpu_gauge = GaugeCard("CPU", 0)
        self.mem_gauge = GaugeCard("Memory", 2)
        self.gpu_gauge = GaugeCard("GPU", 3)
        self.disk_gauge = GaugeCard("Disk busiest", 1)
        for card in (self.cpu_gauge, self.mem_gauge, self.gpu_gauge, self.disk_gauge):
            gauges.append(card)
        self.box.append(gauges)

        tiles = row(homogeneous=True)
        self.tile_net = StatTile("Network total", slot=0)
        self.tile_temp = StatTile("Hottest sensor", slot=1)
        self.tile_load = StatTile("Load average", slot=2, with_sparkline=False)
        self.tile_uptime = StatTile("Uptime", slot=3, with_sparkline=False)
        for tile in (self.tile_net, self.tile_temp, self.tile_load, self.tile_uptime):
            tiles.append(tile)
        self.box.append(tiles)

        graphs = row(homogeneous=True)
        self.cpu_graph = GraphCard("CPU load", lambda v: fmt_pct(v), fixed_max=100.0)
        self.mem_graph = GraphCard("Memory", lambda v: fmt_pct(v), fixed_max=100.0)
        graphs.append(self.cpu_graph)
        graphs.append(self.mem_graph)
        self.box.append(graphs)

        graphs2 = row(homogeneous=True)
        self.net_graph = GraphCard("Network throughput", fmt_rate)
        self.disk_graph = GraphCard("Disk throughput", fmt_rate)
        graphs2.append(self.net_graph)
        graphs2.append(self.disk_graph)
        self.box.append(graphs2)

        self._bound = False

    def _bind(self, history: History) -> None:
        """Attach history series once; afterwards only values change."""
        self.cpu_graph.set_series([("CPU", history.series("cpu.usage"), 0)])
        self.mem_graph.set_series(
            [
                ("Used", history.series("mem.percent"), 2),
                ("Swap", history.series("mem.swap"), 4),
            ]
        )
        self.net_graph.set_series(
            [
                ("Down", history.series("net.rx"), 0),
                ("Up", history.series("net.tx"), 1),
            ]
        )
        self.disk_graph.set_series(
            [
                ("Read", history.series("disk.read"), 0),
                ("Write", history.series("disk.write"), 1),
            ]
        )
        self.tile_net.bind(history.series("net.rx"))
        self.tile_temp.bind(history.series("cpu.temp"))
        self._bound = True

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        if not self._bound:
            self._bind(history)

        cpu = snapshot.get("cpu") or {}
        memory = snapshot.get("memory") or {}
        network = snapshot.get("network") or {}
        disk = snapshot.get("disk") or {}
        gpu = snapshot.get("gpu") or {}
        sensors = snapshot.get("sensors") or {}

        # --- CPU -------------------------------------------------------------
        usage = cpu.get("usage")
        self.cpu_gauge.gauge.set_value(
            usage,
            caption=fmt_hz(cpu.get("freq_mhz")),
            status=None,
        )
        threads = (cpu.get("topology") or {}).get("threads", "?")
        self.cpu_gauge.detail.set_text(
            f"{threads} threads · {fmt_temp(cpu.get('temp'))}"
        )

        # --- Memory ----------------------------------------------------------
        self.mem_gauge.gauge.set_value(memory.get("percent"), caption=fmt_bytes(memory.get("used")))
        self.mem_gauge.detail.set_text(
            f"{fmt_bytes(memory.get('used'))} of {fmt_bytes(memory.get('total'))}"
        )

        # --- GPU -------------------------------------------------------------
        cards = gpu.get("cards") or []
        if cards:
            card = cards[0]
            self.gpu_gauge.gauge.set_value(card.get("busy"), caption=fmt_hz(card.get("freq_mhz")))
            details = [fmt_temp(card.get("temp"))]
            if card.get("power_w") is not None:
                details.append(fmt_watts(card.get("power_w")))
            if card.get("vram_used") is not None:
                details.append(f"{fmt_bytes(card['vram_used'])} VRAM")
            self.gpu_gauge.detail.set_text(" · ".join(details))
        else:
            self.gpu_gauge.gauge.set_value(None, caption="")
            self.gpu_gauge.detail.set_text(gpu.get("error") or "no GPU detected")

        # --- Disk ------------------------------------------------------------
        devices = disk.get("devices") or []
        busiest = None
        for device in devices:
            if device.get("utilisation") is None:
                continue
            if busiest is None or device["utilisation"] > busiest["utilisation"]:
                busiest = device
        if busiest:
            self.disk_gauge.gauge.set_value(
                busiest["utilisation"], caption=busiest["name"]
            )
            self.disk_gauge.detail.set_text(
                f"R {fmt_rate(busiest.get('read_bps'))} · W {fmt_rate(busiest.get('write_bps'))}"
            )
        else:
            self.disk_gauge.gauge.set_value(None, caption="")
            self.disk_gauge.detail.set_text("waiting for samples")

        # --- Tiles -----------------------------------------------------------
        totals = network.get("totals") or {}
        session = network.get("session") or {}
        self.tile_net.set_value(
            fmt_rate((totals.get("rx_bps") or 0) + (totals.get("tx_bps") or 0)),
            f"session {fmt_bytes(session.get('total'))}",
        )

        hottest = sensors.get("hottest")
        if hottest:
            self.tile_temp.set_value(fmt_temp(hottest["value"]), hottest["label"])
        else:
            self.tile_temp.set_value("--", "no sensors")

        load = cpu.get("loadavg") or []
        if load:
            self.tile_load.set_value(
                f"{load[0]:.2f}",
                f"{load[1]:.2f} · {load[2]:.2f} over 5/15 min",
            )

        self.tile_uptime.set_value(
            fmt_duration(cpu.get("uptime")),
            f"{(snapshot.get('processes') or {}).get('counts', {}).get('total', 0)} processes",
        )

        for card in (self.cpu_graph, self.mem_graph, self.net_graph, self.disk_graph):
            card.refresh_legend()
            card.graph.queue_draw()
