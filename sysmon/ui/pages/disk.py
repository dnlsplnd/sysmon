"""Disk page: per-device throughput, IOPS, latency, utilisation, health, filesystems."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import GraphCard, KeyValueList, Meter, StatTile  # noqa: E402
from ..theme import THEME  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_bytes, fmt_count, fmt_pct, fmt_rate, fmt_temp  # noqa: E402


class DeviceCard(Gtk.Box):
    """One block device: live rates, queue utilisation and on-demand SMART."""

    def __init__(self, name: str, page: "DiskPage") -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")
        self.name = name
        self._page = page

        header = row(spacing=8)
        self.title = Gtk.Label(label=name, xalign=0)
        self.title.add_css_class("heading")
        header.append(self.title)
        self.badge = Gtk.Label(label="")
        self.badge.add_css_class("caption")
        self.badge.add_css_class("dim-label")
        self.badge.set_valign(Gtk.Align.CENTER)
        header.append(self.badge)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.rates = Gtk.Label(label="")
        self.rates.add_css_class("caption-heading")
        self.rates.add_css_class("numeric")
        header.append(self.rates)
        self.append(header)

        self.util_label = Gtk.Label(label="", xalign=0)
        self.util_label.add_css_class("caption")
        self.util_label.add_css_class("dim-label")
        self.append(self.util_label)
        self.meter = Meter(height=10)
        self.append(self.meter)

        self.graph = GraphCard(f"{name} throughput", fmt_rate, height=96)
        self.graph.remove_css_class("card")
        self.graph.remove_css_class("sysmon-card")
        self.append(self.graph)

        self.details = KeyValueList()
        self.details.remove_css_class("card")
        self.details.remove_css_class("sysmon-card")
        self.append(self.details)

        self.smart_button = Gtk.Button(label="Read SMART health (needs authentication)")
        self.smart_button.add_css_class("pill")
        self.smart_button.set_halign(Gtk.Align.START)
        self.smart_button.connect("clicked", self._on_smart)
        self.append(self.smart_button)

        self.smart_details = KeyValueList()
        self.smart_details.remove_css_class("card")
        self.smart_details.remove_css_class("sysmon-card")
        self.smart_details.set_visible(False)
        self.append(self.smart_details)

        self._bound = False

    def _on_smart(self, button: Gtk.Button) -> None:
        button.set_sensitive(False)
        button.set_label("Reading…")
        result = self._page.hub.disk.fetch_smart(self.name)
        button.set_sensitive(True)
        button.set_label("Refresh SMART health")

        self.smart_details.set_visible(True)
        if not result or result.get("error"):
            self.smart_details.set("SMART", (result or {}).get("error", "unavailable"))
            return

        passed = result.get("passed")
        self.smart_details.set(
            "Overall health",
            "PASSED" if passed else ("FAILED" if passed is False else "unknown"),
        )
        if result.get("firmware"):
            self.smart_details.set("Firmware", result["firmware"])
        for key, value in (result.get("attributes") or {}).items():
            if value is None:
                continue
            if key in ("Data read", "Data written"):
                self.smart_details.set(key, fmt_bytes(value))
            elif key in ("Percentage used", "Available spare"):
                self.smart_details.set(key, f"{value}%")
            else:
                self.smart_details.set(key, str(value))

    def bind(self, history: History) -> None:
        if self._bound:
            return
        self.graph.set_series(
            [
                ("Read", history.series(f"disk.{self.name}.read"), 0),
                ("Write", history.series(f"disk.{self.name}.write"), 1),
            ]
        )
        self._bound = True

    def update(self, data: dict[str, Any]) -> None:
        self.title.set_text(f"{data['name']} — {data.get('model', '')}")
        parts = [data.get("kind", ""), fmt_bytes(data.get("size"))]
        if data.get("scheduler"):
            parts.append(f"scheduler {data['scheduler']}")
        if data.get("temp") is not None:
            parts.append(fmt_temp(data["temp"]))
        self.badge.set_text(" · ".join(part for part in parts if part))

        self.rates.set_text(
            f"R {fmt_rate(data.get('read_bps'))}   W {fmt_rate(data.get('write_bps'))}"
        )

        utilisation = data.get("utilisation")
        if utilisation is not None:
            self.meter.set_fraction(
                utilisation, 100.0, status=THEME.load_status(utilisation)
            )
            self.util_label.set_text(
                f"Queue busy {utilisation:.0f}% of the interval · "
                f"{data.get('in_flight', 0)} requests in flight"
            )

        self.details.set(
            "IOPS",
            f"{fmt_count(data.get('read_iops'))} read · {fmt_count(data.get('write_iops'))} write",
        )
        # Build each half separately: a conditional expression binds looser
        # than +, so folding these into one expression silently drops the
        # write half whenever a read latency is present.
        read_latency = data.get("read_latency_ms")
        write_latency = data.get("write_latency_ms")
        read_text = f"{read_latency:.2f} ms read" if read_latency is not None else "-- read"
        write_text = f"{write_latency:.2f} ms write" if write_latency is not None else "-- write"
        self.details.set("Average latency", f"{read_text} · {write_text}")
        self.details.set(
            "Since boot",
            f"{fmt_bytes(data.get('read_total'))} read · {fmt_bytes(data.get('write_total'))} written",
        )
        self.details.set("Temperature", fmt_temp(data.get("temp")))

        self.graph.refresh_legend()
        self.graph.graph.queue_draw()


class DiskPage(Page):
    title = "Disks"
    icons = ("drive-harddisk-symbolic", "drive-multidisk-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        tiles = row(homogeneous=True)
        self.tile_read = StatTile("Read", slot=0)
        self.tile_write = StatTile("Write", slot=1)
        self.tile_busiest = StatTile("Busiest device", slot=3, with_sparkline=False)
        self.tile_capacity = StatTile("Root filesystem", slot=2, with_sparkline=False)
        for tile in (self.tile_read, self.tile_write, self.tile_busiest, self.tile_capacity):
            tiles.append(tile)
        self.box.append(tiles)

        self.graph = GraphCard(
            "Aggregate throughput",
            fmt_rate,
            height=150,
            subtitle="Summed across every physical block device",
        )
        self.box.append(self.graph)

        self.devices_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.box.append(self.devices_box)
        self._cards: dict[str, DeviceCard] = {}

        fs_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        fs_card.add_css_class("card")
        fs_card.add_css_class("sysmon-card")
        heading = Gtk.Label(label="Filesystems", xalign=0)
        heading.add_css_class("heading")
        fs_card.append(heading)
        self.fs_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        fs_card.append(self.fs_box)
        self.box.append(fs_card)
        self._fs_rows: dict[str, tuple[Gtk.Label, Meter, Gtk.Label]] = {}

        self._bound = False

    def _bind(self, history: History) -> None:
        self.graph.set_series(
            [
                ("Read", history.series("disk.read"), 0),
                ("Write", history.series("disk.write"), 1),
            ]
        )
        self.tile_read.bind(history.series("disk.read"))
        self.tile_write.bind(history.series("disk.write"))
        self._bound = True

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        if not self._bound:
            self._bind(history)

        disk = snapshot.get("disk") or {}
        devices = disk.get("devices") or []

        read_total = sum(device.get("read_bps") or 0.0 for device in devices)
        write_total = sum(device.get("write_bps") or 0.0 for device in devices)
        self.tile_read.set_value(fmt_rate(read_total))
        self.tile_write.set_value(fmt_rate(write_total))

        busiest = None
        for device in devices:
            if device.get("utilisation") is None:
                continue
            if busiest is None or device["utilisation"] > busiest["utilisation"]:
                busiest = device
        if busiest:
            self.tile_busiest.set_value(
                fmt_pct(busiest["utilisation"]),
                f"{busiest['name']} · {busiest.get('in_flight', 0)} in flight",
            )

        filesystems = disk.get("filesystems") or []
        root = next((fs for fs in filesystems if fs["mountpoint"] == "/"), None)
        if root:
            self.tile_capacity.set_value(
                fmt_pct(root["percent"]),
                f"{fmt_bytes(root['free'])} free of {fmt_bytes(root['total'])}",
            )

        # --- device cards -----------------------------------------------------
        names = [device["name"] for device in devices]
        if names != list(self._cards):
            while (child := self.devices_box.get_first_child()) is not None:
                self.devices_box.remove(child)
            self._cards = {}
            for device in devices:
                card = DeviceCard(device["name"], self)
                card.bind(history)
                self._cards[device["name"]] = card
                self.devices_box.append(card)
        for device in devices:
            self._cards[device["name"]].update(device)

        # --- filesystems ------------------------------------------------------
        # Hide the snap/flatpak squashfs mounts: they are always exactly 100%
        # full by construction, which is noise rather than a warning.
        visible = [
            fs
            for fs in filesystems
            if fs["fstype"] not in ("squashfs", "ramfs")
            and not fs["mountpoint"].startswith(("/snap", "/var/lib/snapd/snap"))
            and fs["total"] > 0
        ]
        keys = [fs["mountpoint"] for fs in visible]
        if keys != list(self._fs_rows):
            while (child := self.fs_box.get_first_child()) is not None:
                self.fs_box.remove(child)
            self._fs_rows = {}
            for fs in visible:
                container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
                header = row(spacing=8)
                title = Gtk.Label(label="", xalign=0)
                title.add_css_class("caption-heading")
                header.append(title)
                spacer = Gtk.Box()
                spacer.set_hexpand(True)
                header.append(spacer)
                detail = Gtk.Label(label="", xalign=1)
                detail.add_css_class("caption")
                detail.add_css_class("dim-label")
                detail.add_css_class("numeric")
                header.append(detail)
                container.append(header)
                meter = Meter(height=10)
                container.append(meter)
                self.fs_box.append(container)
                self._fs_rows[fs["mountpoint"]] = (title, meter, detail)

        for fs in visible:
            title, meter, detail = self._fs_rows[fs["mountpoint"]]
            title.set_text(f"{fs['mountpoint']}  ·  {fs['device']} ({fs['fstype']})")
            meter.set_fraction(fs["used"], fs["total"], status=THEME.load_status(fs["percent"]))
            detail.set_text(
                f"{fmt_bytes(fs['used'])} of {fmt_bytes(fs['total'])} · "
                f"{fmt_bytes(fs['free'])} free · {fs['percent']:.0f}%"
            )

        self.graph.refresh_legend()
        self.graph.graph.queue_draw()
