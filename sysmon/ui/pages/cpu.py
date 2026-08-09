"""CPU page: aggregate load, time breakdown, per-core heatmap, frequency, thermals."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import CoreHeatmap, GraphCard, KeyValueList, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_count, fmt_hz, fmt_pct, fmt_temp  # noqa: E402


class CpuPage(Page):
    title = "CPU"
    # Themed names first; the bundled icon covers themes (Breeze) that have no
    # symbolic CPU icon at all, and only a colour one that would look out of
    # place next to the rest of the sidebar.
    icons = ("cpu-symbolic", "processor-symbolic", "sysmon-cpu-symbolic", "computer-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        tiles = row(homogeneous=True)
        self.tile_usage = StatTile("Utilisation", slot=0)
        self.tile_freq = StatTile("Frequency", slot=1)
        self.tile_temp = StatTile("Die temperature", slot=3)
        self.tile_load = StatTile("Load average", slot=2, with_sparkline=False)
        for tile in (self.tile_usage, self.tile_freq, self.tile_temp, self.tile_load):
            tiles.append(tile)
        self.box.append(tiles)

        self.usage_graph = GraphCard(
            "CPU time breakdown",
            lambda v: fmt_pct(v),
            fixed_max=100.0,
            height=150,
            subtitle="Share of each interval spent in user code, the kernel, and waiting on I/O",
        )
        self.box.append(self.usage_graph)

        heat_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        heat_card.add_css_class("card")
        heat_card.add_css_class("sysmon-card")
        heading = Gtk.Label(label="Per-thread load", xalign=0)
        heading.add_css_class("heading")
        heat_card.append(heading)
        self.heat_caption = Gtk.Label(label="", xalign=0)
        self.heat_caption.add_css_class("caption")
        self.heat_caption.add_css_class("dim-label")
        heat_card.append(self.heat_caption)
        self.heatmap = CoreHeatmap(height=104)
        heat_card.append(self.heatmap)
        self.box.append(heat_card)

        graphs = row(homogeneous=True)
        self.freq_graph = GraphCard("Frequency", fmt_hz)
        self.temp_graph = GraphCard("Temperature", fmt_temp)
        graphs.append(self.freq_graph)
        graphs.append(self.temp_graph)
        self.box.append(graphs)

        details = row(homogeneous=True)
        self.info = KeyValueList("Processor")
        self.activity = KeyValueList("Kernel activity")
        details.append(self.info)
        details.append(self.activity)
        self.box.append(details)

        self._bound = False

    def _bind(self, history: History) -> None:
        self.usage_graph.set_series(
            [
                ("Total", history.series("cpu.usage"), 0),
                ("User", history.series("cpu.user"), 1),
                ("System", history.series("cpu.system"), 2),
                ("I/O wait", history.series("cpu.iowait"), 3),
            ]
        )
        self.freq_graph.set_series([("Frequency", history.series("cpu.freq"), 1)])
        self.temp_graph.set_series([("Die", history.series("cpu.temp"), 3)])
        self.tile_usage.bind(history.series("cpu.usage"), fixed_max=100.0)
        self.tile_freq.bind(history.series("cpu.freq"))
        self.tile_temp.bind(history.series("cpu.temp"))
        self._bound = True

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        if not self._bound:
            self._bind(history)

        cpu = snapshot.get("cpu") or {}
        topology = cpu.get("topology") or {}

        self.tile_usage.set_value(fmt_pct(cpu.get("usage"), 1))
        self.tile_freq.set_value(
            fmt_hz(cpu.get("freq_mhz")),
            f"peak {fmt_hz(cpu.get('freq_max_mhz'))}",
        )
        self.tile_temp.set_value(fmt_temp(cpu.get("temp")))

        load = cpu.get("loadavg") or []
        if load:
            threads = topology.get("threads") or 1
            # Load is only interpretable against the thread count: 16.0 on a
            # 16-thread box is saturated, on a 4-thread box it is 4x oversubscribed.
            self.tile_load.set_value(
                f"{load[0]:.2f}",
                f"{100.0 * load[0] / threads:.0f}% of {threads} threads",
            )

        per_core = cpu.get("per_core") or []
        self.heatmap.set_values(
            per_core,
            cpu.get("freq_per_core") or [],
            topology.get("threads_per_core", 1),
        )
        busy = [value for value in per_core if value is not None]
        if busy:
            self.heat_caption.set_text(
                f"{len(per_core)} logical CPUs · busiest {max(busy):.0f}% · "
                f"idlest {min(busy):.0f}% · spread {max(busy) - min(busy):.0f} points"
            )

        self.info.set("Model", cpu.get("model", "--"))
        self.info.set(
            "Topology",
            f"{topology.get('cores', '?')} cores / {topology.get('threads', '?')} threads"
            f" ({topology.get('threads_per_core', 1)} per core)",
        )
        self.info.set("Governor", cpu.get("governor") or "--")
        self.info.set("Peak frequency", fmt_hz(cpu.get("freq_max_mhz")))
        # Name the file the number came from: on a fixed-multiplier overclock,
        # scaling_cur_freq and the measured frequency disagree badly, and the
        # user needs to know which one they are looking at.
        source = cpu.get("freq_source") or "unavailable"
        measured = source in ("cpuinfo_avg_freq", "cpuinfo_cur_freq")
        self.info.set(
            "Frequency source",
            f"{source} ({'measured' if measured else 'governor request'})",
        )

        temps = cpu.get("temps") or {}
        for label, value in temps.items():
            self.info.set(f"Sensor · {label}", fmt_temp(value))

        self.activity.set("Context switches", f"{fmt_count(cpu.get('ctxt_per_s'))}/s")
        self.activity.set("Interrupts", f"{fmt_count(cpu.get('intr_per_s'))}/s")
        self.activity.set("Forks", f"{fmt_count(cpu.get('forks_per_s'))}/s")
        self.activity.set("Runnable", str(cpu.get("procs_running") or "--"))
        self.activity.set("Blocked on I/O", str(cpu.get("procs_blocked") or "--"))
        if load:
            self.activity.set("Load 1 / 5 / 15", f"{load[0]:.2f} · {load[1]:.2f} · {load[2]:.2f}")

        for card in (self.usage_graph, self.freq_graph, self.temp_graph):
            card.refresh_legend()
            card.graph.queue_draw()
