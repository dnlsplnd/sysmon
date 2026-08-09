"""Sensors page: every hwmon rail, grouped by chip, with per-sensor history."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..theme import THEME  # noqa: E402
from ..widgets import GraphCard, Meter, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_temp  # noqa: E402


def _format(reading: dict[str, Any]) -> str:
    value, unit = reading["value"], reading["unit"]
    if unit == "°C":
        return f"{value:.1f} °C"
    if unit == "RPM":
        return f"{value:.0f} RPM"
    if unit == "V":
        return f"{value:.3f} V"
    if unit == "A":
        return f"{value:.2f} A"
    if unit == "W":
        return f"{value:.1f} W"
    return f"{value:.2f} {unit}"


class SensorRow(Gtk.Box):
    """One rail: label, live value, and a bar scaled to its critical point."""

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)

        header = row(spacing=8)
        self.label = Gtk.Label(label=label, xalign=0)
        self.label.add_css_class("caption")
        header.append(self.label)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.value = Gtk.Label(label="--", xalign=1)
        self.value.add_css_class("caption-heading")
        self.value.add_css_class("numeric")
        header.append(self.value)
        self.append(header)

        self.meter = Meter(height=8)
        self.append(self.meter)

    def update(self, reading: dict[str, Any]) -> None:
        self.value.set_text(_format(reading))

        kind = reading["kind"]
        if kind == "temperature":
            # Scale against the chip's own critical point when it publishes one,
            # otherwise a 100 C reference, which suits every rail on this class
            # of hardware.
            ceiling = reading.get("crit") or reading.get("max") or 100.0
            self.meter.set_fraction(
                reading["value"], ceiling, status=THEME.temp_status(reading["value"])
            )
            self.meter.set_visible(True)
        elif kind == "fan":
            self.meter.set_fraction(reading["value"], max(1.0, reading.get("max") or 3000.0))
            self.meter.set_visible(True)
        else:
            # Voltages and currents have no meaningful zero-to-max span here,
            # so a bar would imply a scale that does not exist.
            self.meter.set_visible(False)


class SensorsPage(Page):
    title = "Sensors"
    icons = ("temperature-symbolic", "temperature-normal-symbolic", "sensors-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        tiles = row(homogeneous=True)
        self.tile_hottest = StatTile("Hottest sensor", slot=3)
        self.tile_cpu = StatTile("CPU die", slot=0, with_sparkline=False)
        self.tile_gpu = StatTile("GPU", slot=1, with_sparkline=False)
        self.tile_disk = StatTile("Storage", slot=2, with_sparkline=False)
        for tile in (self.tile_hottest, self.tile_cpu, self.tile_gpu, self.tile_disk):
            tiles.append(tile)
        self.box.append(tiles)

        self.graph = GraphCard(
            "Temperatures",
            fmt_temp,
            height=170,
            subtitle="Every temperature rail the kernel exposes, on one axis",
        )
        self.box.append(self.graph)

        self.chips_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.box.append(self.chips_box)

        self._chip_cards: dict[str, dict[str, SensorRow]] = {}
        self._bound_keys: list[str] = []

    def _bind(self, snapshot: dict[str, Any], history: History) -> None:
        """Plot up to eight temperature rails -- the categorical palette's limit.

        Past eight the remainder is dropped from the chart rather than given a
        ninth invented hue; the per-chip lists below still show every rail.
        """
        entries = []
        keys = []
        for chip in (snapshot.get("sensors") or {}).get("chips") or []:
            for reading in chip["values"]:
                if reading["kind"] != "temperature":
                    continue
                keys.append(reading["key"])
                if len(entries) < 8:
                    # Drop the chip prefix when the rail's own label already
                    # identifies it, so the legend stays narrow.
                    rail = reading["label"]
                    short = chip["label"].split(" (")[0]
                    label = rail if rail.lower().startswith(short.lower()) else f"{short} {rail}"
                    entries.append((label, history.series(f"sensor.{reading['key']}"), len(entries)))
        if keys != self._bound_keys:
            self.graph.set_series(entries)
            self._bound_keys = keys
            extra = len(keys) - len(entries)
            self.graph.set_subtitle(
                "Every temperature rail the kernel exposes, on one axis"
                if extra <= 0
                else f"Showing the first {len(entries)} of {len(keys)} temperature rails"
            )

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        sensors = snapshot.get("sensors") or {}
        chips = sensors.get("chips") or []
        self._bind(snapshot, history)

        hottest = sensors.get("hottest")
        if hottest:
            self.tile_hottest.set_value(fmt_temp(hottest["value"]), hottest["label"])
            self.tile_hottest.bind(
                history.series("cpu.temp") if "CPU" in hottest["label"] else history.series("cpu.temp")
            )

        cpu = snapshot.get("cpu") or {}
        self.tile_cpu.set_value(fmt_temp(cpu.get("temp")), "Tdie")

        gpu_cards = (snapshot.get("gpu") or {}).get("cards") or []
        if gpu_cards:
            card = gpu_cards[0]
            fan = f"fan {card['fan_rpm']} RPM" if card.get("fan_rpm") else ""
            self.tile_gpu.set_value(fmt_temp(card.get("temp")), fan)

        devices = (snapshot.get("disk") or {}).get("devices") or []
        with_temp = [device for device in devices if device.get("temp") is not None]
        if with_temp:
            hottest_disk = max(with_temp, key=lambda device: device["temp"])
            self.tile_disk.set_value(fmt_temp(hottest_disk["temp"]), hottest_disk["name"])
        else:
            self.tile_disk.set_value("--", "no drive sensors")

        # Rebuild chip cards only when the sensor topology changes.
        signature = {chip["id"]: [item["key"] for item in chip["values"]] for chip in chips}
        if list(signature) != list(self._chip_cards):
            while (child := self.chips_box.get_first_child()) is not None:
                self.chips_box.remove(child)
            self._chip_cards = {}
            for chip in chips:
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
                card.add_css_class("card")
                card.add_css_class("sysmon-card")

                heading = Gtk.Label(label=f"{chip['label']}", xalign=0)
                heading.add_css_class("heading")
                card.append(heading)
                subtitle = Gtk.Label(label=f"hwmon chip “{chip['name']}”", xalign=0)
                subtitle.add_css_class("caption")
                subtitle.add_css_class("dim-label")
                card.append(subtitle)

                rows: dict[str, SensorRow] = {}
                for reading in chip["values"]:
                    sensor_row = SensorRow(reading["label"])
                    rows[reading["key"]] = sensor_row
                    card.append(sensor_row)
                self._chip_cards[chip["id"]] = rows
                self.chips_box.append(card)

        for chip in chips:
            rows = self._chip_cards.get(chip["id"], {})
            for reading in chip["values"]:
                sensor_row = rows.get(reading["key"])
                if sensor_row is not None:
                    sensor_row.update(reading)

        self.graph.refresh_legend()
        self.graph.graph.queue_draw()
