"""Memory page: composition breakdown, swap, zram compression, physical modules."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import GraphCard, KeyValueList, LegendItem, Meter, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_bytes, fmt_hz, fmt_pct  # noqa: E402


class MemoryPage(Page):
    title = "Memory"
    icons = (
        "memory-symbolic",
        "drive-harddisk-solidstate-symbolic",
        "media-flash-symbolic",
        "memory",
    )

    def __init__(self, hub) -> None:
        super().__init__(hub)

        tiles = row(homogeneous=True)
        self.tile_used = StatTile("In use", slot=2)
        self.tile_available = StatTile("Available", slot=0, with_sparkline=False)
        self.tile_cached = StatTile("Cache & buffers", slot=1, with_sparkline=False)
        self.tile_swap = StatTile("Swap used", slot=4)
        for tile in (self.tile_used, self.tile_available, self.tile_cached, self.tile_swap):
            tiles.append(tile)
        self.box.append(tiles)

        # --- composition meter ------------------------------------------------
        composition = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        composition.add_css_class("card")
        composition.add_css_class("sysmon-card")
        heading = Gtk.Label(label="Physical memory composition", xalign=0)
        heading.add_css_class("heading")
        composition.append(heading)

        self.meter = Meter(height=18)
        composition.append(self.meter)

        legend = row(spacing=16)
        self.legend_items = {
            name: LegendItem(name, slot)
            for name, slot in (("Applications", 2), ("Cache", 1), ("Buffers", 3), ("Free", 5))
        }
        for item in self.legend_items.values():
            legend.append(item)
        composition.append(legend)
        self.box.append(composition)

        graphs = row(homogeneous=True)
        self.usage_graph = GraphCard("Memory usage", lambda v: fmt_pct(v), fixed_max=100.0)
        self.swap_graph = GraphCard("Swap usage", lambda v: fmt_pct(v), fixed_max=100.0)
        graphs.append(self.usage_graph)
        graphs.append(self.swap_graph)
        self.box.append(graphs)

        details = row(homogeneous=True)
        self.breakdown = KeyValueList("Kernel breakdown")
        self.modules = KeyValueList("Modules & swap devices")
        details.append(self.breakdown)
        details.append(self.modules)
        self.box.append(details)

        # DMI needs root, so offer an explicit opt-in rather than silently
        # showing nothing or nagging with a password prompt at startup.
        self.dimm_button = Gtk.Button(label="Read module details (needs authentication)")
        self.dimm_button.add_css_class("pill")
        self.dimm_button.set_halign(Gtk.Align.START)
        self.dimm_button.connect("clicked", self._on_read_modules)
        self.box.append(self.dimm_button)

        self._bound = False

    def _bind(self, history: History) -> None:
        self.usage_graph.set_series(
            [
                ("Used", history.series("mem.percent"), 2),
                ("Cached", history.series("mem.cached"), 1),
            ]
        )
        # Cached is recorded in bytes while used is a percentage, and one axis
        # cannot honestly carry both -- plot cache as its own percentage.
        self.usage_graph.set_series([("Used", history.series("mem.percent"), 2)])
        self.swap_graph.set_series([("Swap", history.series("mem.swap"), 4)])
        self.tile_used.bind(history.series("mem.percent"), fixed_max=100.0)
        self.tile_swap.bind(history.series("mem.swap"), fixed_max=100.0)
        self._bound = True

    def _on_read_modules(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        button.set_label("Reading…")
        found = self.hub.memory.load_modules(interactive=True)
        button.set_visible(found)
        button.set_sensitive(True)
        button.set_label("Read module details (needs authentication)")

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        if not self._bound:
            self._bind(history)

        memory = snapshot.get("memory") or {}
        total = memory.get("total") or 1

        self.tile_used.set_value(
            fmt_bytes(memory.get("used")),
            f"{fmt_pct(memory.get('percent'), 1)} of {fmt_bytes(total)}",
        )
        self.tile_available.set_value(
            fmt_bytes(memory.get("available")),
            f"{100.0 * (memory.get('available') or 0) / total:.0f}% reclaimable or free",
        )
        self.tile_cached.set_value(
            fmt_bytes(memory.get("cached")),
            f"buffers {fmt_bytes(memory.get('buffers'))}",
        )
        swap_total = memory.get("swap_total") or 0
        self.tile_swap.set_value(
            fmt_bytes(memory.get("swap_used")),
            f"of {fmt_bytes(swap_total)}" if swap_total else "no swap configured",
        )

        # Applications = used minus what the kernel counts as reclaimable.
        cached = memory.get("cached") or 0
        buffers = memory.get("buffers") or 0
        applications = max(0, (memory.get("used") or 0))
        free = max(0, total - applications - cached - buffers)
        self.meter.set_segments(
            [
                ("Applications", applications, 2),
                ("Cache", cached, 1),
                ("Buffers", buffers, 3),
                ("Free", free, 5),
            ],
            total,
        )
        for name, value in (
            ("Applications", applications),
            ("Cache", cached),
            ("Buffers", buffers),
            ("Free", free),
        ):
            self.legend_items[name].set_value(fmt_bytes(value))

        self.breakdown.set("Total", fmt_bytes(total))
        self.breakdown.set("Used", fmt_bytes(memory.get("used")))
        self.breakdown.set("Available", fmt_bytes(memory.get("available")))
        self.breakdown.set("Free", fmt_bytes(memory.get("free")))
        self.breakdown.set("Cached", fmt_bytes(memory.get("cached")))
        self.breakdown.set("Buffers", fmt_bytes(memory.get("buffers")))
        self.breakdown.set("Anonymous", fmt_bytes(memory.get("anon")))
        self.breakdown.set("Mapped", fmt_bytes(memory.get("mapped")))
        self.breakdown.set("Shared (tmpfs)", fmt_bytes(memory.get("shmem")))
        self.breakdown.set("Slab", fmt_bytes(memory.get("slab")))
        self.breakdown.set("Page tables", fmt_bytes(memory.get("page_tables")))
        self.breakdown.set("Dirty", fmt_bytes(memory.get("dirty")))
        self.breakdown.set("Writeback", fmt_bytes(memory.get("writeback")))
        committed = memory.get("committed") or 0
        limit = memory.get("commit_limit") or 0
        self.breakdown.set(
            "Committed",
            f"{fmt_bytes(committed)} of {fmt_bytes(limit)}"
            + (f" ({100.0 * committed / limit:.0f}%)" if limit else ""),
        )

        speed = memory.get("speed_mhz")
        self.modules.set("Configured speed", fmt_hz(speed) if speed else "requires authentication")
        for module in memory.get("modules") or []:
            detail = " · ".join(
                part
                for part in (
                    module.get("size"),
                    module.get("type"),
                    fmt_hz(module["speed_mhz"]) if module.get("speed_mhz") else None,
                    (module.get("part") or "").strip() or None,
                )
                if part
            )
            self.modules.set(f"Slot {module.get('locator')}", detail)

        if memory.get("modules"):
            self.dimm_button.set_visible(False)

        for device in memory.get("zram") or []:
            ratio = device.get("ratio")
            self.modules.set(
                f"zram · {device['name']}",
                f"{fmt_bytes(device['original'])} stored in {fmt_bytes(device['compressed'])}"
                + (f" · {ratio:.1f}x" if ratio else "")
                + f" · limit {fmt_bytes(device['disksize'])}",
            )

        for card in (self.usage_graph, self.swap_graph):
            card.refresh_legend()
            card.graph.queue_draw()
