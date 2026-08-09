"""GPU page: per-engine utilisation, VRAM, clocks, power and per-process attribution."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import Gauge, GraphCard, KeyValueList, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_bytes, fmt_hz, fmt_pct, fmt_temp, fmt_watts  # noqa: E402

# Engine keys as they appear in DRM fdinfo, with the palette slot each gets.
# Slots are fixed per engine so a quiet engine dropping out never repaints the
# others.
_ENGINE_SLOTS = {
    "render": (0, "Render / 3D"),
    "video": (1, "Video decode"),
    "compute": (2, "Compute"),
    "copy": (3, "Blitter"),
    "video-enhance": (4, "Video enhance"),
}


class GpuPage(Page):
    title = "GPU"
    icons = ("video-display-symbolic", "monitor-symbolic", "display-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        self.empty = Gtk.Label(label="", xalign=0)
        self.empty.add_css_class("dim-label")
        self.empty.set_visible(False)
        self.box.append(self.empty)

        top = row(homogeneous=False)
        gauge_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        gauge_card.add_css_class("card")
        gauge_card.add_css_class("sysmon-card")
        heading = Gtk.Label(label="Busiest engine", xalign=0.5)
        heading.add_css_class("heading")
        gauge_card.append(heading)
        self.gauge = Gauge(size=150, slot=0)
        self.gauge.set_halign(Gtk.Align.CENTER)
        gauge_card.append(self.gauge)
        self.gauge_detail = Gtk.Label(label="", xalign=0.5)
        self.gauge_detail.add_css_class("caption")
        self.gauge_detail.add_css_class("dim-label")
        gauge_card.append(self.gauge_detail)
        top.append(gauge_card)

        tiles = Gtk.Grid(column_spacing=14, row_spacing=14, column_homogeneous=True)
        tiles.set_hexpand(True)
        self.tile_vram = StatTile("Video memory in use", slot=2)
        self.tile_clock = StatTile("Clock", slot=1)
        self.tile_power = StatTile("Power draw", slot=3)
        self.tile_temp = StatTile("Temperature", slot=4)
        tiles.attach(self.tile_vram, 0, 0, 1, 1)
        tiles.attach(self.tile_clock, 1, 0, 1, 1)
        tiles.attach(self.tile_power, 0, 1, 1, 1)
        tiles.attach(self.tile_temp, 1, 1, 1, 1)
        top.append(tiles)
        self.box.append(top)

        self.engine_graph = GraphCard(
            "Engine utilisation",
            lambda v: fmt_pct(v),
            fixed_max=100.0,
            height=160,
            subtitle="Busy time per engine class, from DRM fdinfo — the source intel_gpu_top reads",
        )
        self.box.append(self.engine_graph)

        graphs = row(homogeneous=True)
        self.clock_graph = GraphCard("Clock", fmt_hz)
        self.power_graph = GraphCard("Power & temperature", fmt_watts)
        graphs.append(self.clock_graph)
        graphs.append(self.power_graph)
        self.box.append(graphs)

        details = row(homogeneous=True)
        self.info = KeyValueList("Adapter")
        self.clients = KeyValueList("Top GPU clients")
        details.append(self.info)
        details.append(self.clients)
        self.box.append(details)

        self._bound_card: str | None = None
        self._process_names: dict[int, str] = {}

    def _bind(self, history: History, card_key: str, engines: list[str]) -> None:
        entries = []
        for engine in engines:
            slot, label = _ENGINE_SLOTS.get(engine, (5, engine))
            entries.append((label, history.series(f"gpu.{card_key}.engine.{engine}"), slot))
        self.engine_graph.set_series(entries)
        self.clock_graph.set_series([("Clock", history.series(f"gpu.{card_key}.freq"), 1)])
        self.power_graph.set_series([("Power", history.series(f"gpu.{card_key}.power"), 3)])
        self.tile_vram.bind(history.series(f"gpu.{card_key}.vram"))
        self.tile_clock.bind(history.series(f"gpu.{card_key}.freq"))
        self.tile_power.bind(history.series(f"gpu.{card_key}.power"))
        self.tile_temp.bind(history.series(f"gpu.{card_key}.temp"))
        self._bound_card = card_key

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        gpu = snapshot.get("gpu") or {}
        cards = gpu.get("cards") or []
        if not cards:
            self.empty.set_visible(True)
            self.empty.set_text(
                gpu.get("error")
                or "No Intel i915/xe GPU found. Other vendors are not read by this page."
            )
            return
        self.empty.set_visible(False)

        card = cards[0]
        key = card["card"]

        # Only plot engines the driver actually reports for this card.
        engines = [
            name
            for name in ("render", "video", "compute", "copy", "video-enhance")
            if name in (card.get("engine_raw") or {})
        ]
        if self._bound_card != key:
            self._bind(history, key, engines)

        busy = card.get("busy")
        self.gauge.set_value(busy, caption=fmt_hz(card.get("freq_mhz")))
        active = [
            f"{_ENGINE_SLOTS.get(name, (0, name))[1]} {value:.0f}%"
            for name, value in sorted(
                (card.get("engine_raw") or {}).items(),
                key=lambda item: -(item[1] or 0),
            )
            if value and value >= 1.0
        ]
        self.gauge_detail.set_text(" · ".join(active) if active else "all engines idle")

        vram = card.get("vram_used")
        self.tile_vram.set_value(
            fmt_bytes(vram),
            f"{card.get('clients', 0)} clients · {fmt_bytes(card.get('system_used'))} system memory",
        )
        self.tile_clock.set_value(
            fmt_hz(card.get("freq_mhz")),
            "power-gated at sample · showing requested clock"
            if card.get("freq_gated")
            else f"requested {fmt_hz(card.get('freq_requested_mhz'))} · max {fmt_hz(card.get('freq_max_mhz'))}",
        )
        power = card.get("power_w")
        limit = card.get("power_limit_w")
        self.tile_power.set_value(
            fmt_watts(power),
            f"limit {fmt_watts(limit)}" if limit else "",
        )
        self.tile_temp.set_value(
            fmt_temp(card.get("temp")),
            f"fan {card['fan_rpm']} RPM" if card.get("fan_rpm") else "",
        )

        self.info.set("Adapter", card.get("name", "--"))
        self.info.set("Driver", f"{card.get('driver')} · {card.get('pdev')}")
        self.info.set("Clock range", f"{fmt_hz(card.get('freq_min_mhz'))} – {fmt_hz(card.get('freq_max_mhz'))}")
        self.info.set("Video memory in use", fmt_bytes(vram))
        # The BAR is the CPU-visible window onto video memory, not the card's
        # capacity -- saying "total" here would be wrong.
        aperture = card.get("lmem_aperture")
        if aperture:
            self.info.set("Local memory aperture", f"{fmt_bytes(aperture)} (resizable BAR window)")
        self.info.set("System memory mapped", fmt_bytes(card.get("system_used")))
        if limit:
            self.info.set("Power limit", fmt_watts(limit))

        # --- per-process attribution -----------------------------------------
        processes = sorted(
            card.get("processes") or [],
            key=lambda item: -(sum(item["engines"].values()) if item["engines"] else 0),
        )
        self.clients.clear()
        shown = 0
        for entry in processes:
            if shown >= 8:
                break
            vram_text = fmt_bytes(entry["vram"]) if entry["vram"] else "—"
            self.clients.set(f"{entry['name']} ({entry['pid']})", f"{vram_text} VRAM")
            shown += 1
        if shown == 0:
            self.clients.set("Clients", "none")

        for graph in (self.engine_graph, self.clock_graph, self.power_graph):
            graph.refresh_legend()
            graph.graph.queue_draw()
