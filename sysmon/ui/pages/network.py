"""Network page: throughput, per-interface detail, byte counters, bandwidth maths."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import GLib, Gtk  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import GraphCard, KeyValueList, Meter, StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import (  # noqa: E402
    fmt_bits,
    fmt_bytes,
    fmt_count,
    fmt_duration,
    fmt_pct,
    fmt_rate,
)

# Size units offered by the transfer calculator, in bytes.
_SIZE_UNITS = [
    ("MB (10^6)", 1_000_000),
    ("MiB (2^20)", 1024**2),
    ("GB (10^9)", 1_000_000_000),
    ("GiB (2^30)", 1024**3),
    ("TB (10^12)", 1_000_000_000_000),
    ("TiB (2^40)", 1024**4),
]

_RATE_UNITS = [
    ("Mbit/s", 1_000_000 / 8),
    ("MiB/s", 1024**2),
    ("Gbit/s", 1_000_000_000 / 8),
    ("MB/s", 1_000_000),
]


class InterfaceCard(Gtk.Box):
    """One physical or virtual interface, with live rates and link utilisation."""

    def __init__(self, name: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")
        self.name = name

        header = row(spacing=8)
        self.title = Gtk.Label(label=name, xalign=0)
        self.title.add_css_class("heading")
        header.append(self.title)
        self.badge = Gtk.Label(label="")
        self.badge.add_css_class("caption")
        self.badge.add_css_class("dim-label")
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

        self.details = KeyValueList()
        self.details.remove_css_class("card")
        self.details.remove_css_class("sysmon-card")
        self.append(self.details)

    def update(self, data: dict[str, Any]) -> None:
        state = data.get("state", "unknown")
        self.title.set_text(data["name"])
        link = f" · {data['link_mbit']} Mbit/s" if data.get("link_mbit") else ""
        self.badge.set_text(f"{data.get('kind', '')} · {state}{link}")

        self.rates.set_text(
            f"↓ {fmt_rate(data.get('rx_bps'))}   ↑ {fmt_rate(data.get('tx_bps'))}"
        )

        link_mbit = data.get("link_mbit")
        if link_mbit:
            # Utilisation is against the negotiated link rate, and only the
            # busier direction is meaningful on a full-duplex link.
            rx_util = data.get("rx_utilisation") or 0.0
            tx_util = data.get("tx_utilisation") or 0.0
            self.meter.set_fraction(max(rx_util, tx_util), 100.0)
            self.meter.set_visible(True)
            self.util_label.set_visible(True)
            self.util_label.set_text(
                f"Link utilisation {max(rx_util, tx_util):.1f}% of {link_mbit} Mbit/s "
                f"(down {rx_util:.1f}%, up {tx_util:.1f}%)"
            )
        else:
            self.meter.set_visible(False)
            self.util_label.set_visible(False)

        addresses = ", ".join(
            f"{addr['address']}" for addr in data.get("addresses") or []
        )
        self.details.set("Addresses", addresses or "none assigned")
        self.details.set("MAC", data.get("mac") or "--")
        self.details.set("MTU", str(data.get("mtu") or "--"))
        if data.get("duplex"):
            self.details.set("Duplex", data["duplex"])

        wireless = data.get("wireless")
        if wireless:
            self.details.set(
                "Signal",
                f"{wireless['signal_dbm']:.0f} dBm · quality {wireless['quality']:.0f}"
                f" · noise {wireless['noise_dbm']:.0f} dBm",
            )

        self.details.set(
            "Received",
            f"{fmt_bytes(data.get('rx_bytes'))} · {fmt_count(data.get('rx_packets'))} packets",
        )
        self.details.set(
            "Sent",
            f"{fmt_bytes(data.get('tx_bytes'))} · {fmt_count(data.get('tx_packets'))} packets",
        )
        self.details.set(
            "Session",
            f"↓ {fmt_bytes(data.get('session_rx'))} · ↑ {fmt_bytes(data.get('session_tx'))}",
        )
        self.details.set(
            "Peak rate",
            f"↓ {fmt_rate(data.get('peak_rx_bps'))} · ↑ {fmt_rate(data.get('peak_tx_bps'))}",
        )
        errors = (data.get("rx_errors") or 0) + (data.get("tx_errors") or 0)
        drops = (data.get("rx_dropped") or 0) + (data.get("tx_dropped") or 0)
        self.details.set("Errors / drops", f"{errors} / {drops}")


class BandwidthCalculator(Gtk.Box):
    """Transfer-time estimator and data-volume projection.

    Both directions of the question people actually ask: 'how long will this
    take at the rate I'm getting' and 'how much data does this rate add up to'.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")

        heading = Gtk.Label(label="Bandwidth calculator", xalign=0)
        heading.add_css_class("heading")
        self.append(heading)

        caption = Gtk.Label(
            label="Estimate transfer time, or project a sustained rate into volume over time.",
            xalign=0,
        )
        caption.add_css_class("caption")
        caption.add_css_class("dim-label")
        caption.set_wrap(True)
        self.append(caption)

        # --- transfer size ----------------------------------------------------
        size_row = row(spacing=8)
        size_row.append(Gtk.Label(label="Transfer", xalign=0))
        self.size_entry = Gtk.Entry()
        self.size_entry.set_text("10")
        self.size_entry.set_max_width_chars(9)
        self.size_entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self.size_entry.connect("changed", self._recalculate)
        size_row.append(self.size_entry)

        self.size_unit = Gtk.DropDown.new_from_strings([label for label, _ in _SIZE_UNITS])
        self.size_unit.set_selected(3)  # GiB
        self.size_unit.connect("notify::selected", self._recalculate)
        size_row.append(self.size_unit)
        self.append(size_row)

        # --- rate -------------------------------------------------------------
        rate_row = row(spacing=8)
        rate_row.append(Gtk.Label(label="at", xalign=0))
        self.rate_source = Gtk.DropDown.new_from_strings(
            ["Current download", "Current upload", "Session average", "Custom rate"]
        )
        self.rate_source.connect("notify::selected", self._on_source_changed)
        rate_row.append(self.rate_source)

        self.rate_entry = Gtk.Entry()
        self.rate_entry.set_text("100")
        self.rate_entry.set_max_width_chars(8)
        self.rate_entry.set_input_purpose(Gtk.InputPurpose.NUMBER)
        self.rate_entry.set_visible(False)
        self.rate_entry.connect("changed", self._recalculate)
        rate_row.append(self.rate_entry)

        self.rate_unit = Gtk.DropDown.new_from_strings([label for label, _ in _RATE_UNITS])
        self.rate_unit.set_visible(False)
        self.rate_unit.connect("notify::selected", self._recalculate)
        rate_row.append(self.rate_unit)
        self.append(rate_row)

        self.result = Gtk.Label(label="--", xalign=0)
        self.result.add_css_class("title-3")
        self.result.add_css_class("numeric")
        self.append(self.result)

        self.result_detail = Gtk.Label(label="", xalign=0)
        self.result_detail.add_css_class("caption")
        self.result_detail.add_css_class("dim-label")
        self.result_detail.set_wrap(True)
        self.append(self.result_detail)

        separator = Gtk.Separator()
        self.append(separator)

        self.projection = KeyValueList()
        self.projection.remove_css_class("card")
        self.projection.remove_css_class("sysmon-card")
        self.append(self.projection)

        self._rates = {"rx": 0.0, "tx": 0.0, "avg": 0.0}

    def _on_source_changed(self, *_args) -> None:
        custom = self.rate_source.get_selected() == 3
        self.rate_entry.set_visible(custom)
        self.rate_unit.set_visible(custom)
        self._recalculate()

    @staticmethod
    def _parse(entry: Gtk.Entry) -> float | None:
        text = entry.get_text().strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value > 0 else None

    def _selected_rate(self) -> tuple[float, str]:
        """Resolve the chosen rate to bytes/second plus a human description."""
        index = self.rate_source.get_selected()
        if index == 0:
            return self._rates["rx"], "current download rate"
        if index == 1:
            return self._rates["tx"], "current upload rate"
        if index == 2:
            return self._rates["avg"], "session average (down + up)"
        value = self._parse(self.rate_entry) or 0.0
        _, factor = _RATE_UNITS[self.rate_unit.get_selected()]
        return value * factor, "custom rate"

    def set_live_rates(self, rx: float, tx: float, average: float) -> None:
        self._rates = {"rx": rx, "tx": tx, "avg": average}
        # Only the live sources move on their own; recomputing on every tick
        # while the user is typing a custom rate would fight the entry.
        if self.rate_source.get_selected() != 3:
            self._recalculate()

    def _recalculate(self, *_args) -> None:
        size_value = self._parse(self.size_entry)
        _, size_factor = _SIZE_UNITS[self.size_unit.get_selected()]
        rate, description = self._selected_rate()

        if size_value is None:
            self.result.set_text("--")
            self.result_detail.set_text("Enter a transfer size.")
        elif rate <= 0:
            self.result.set_text("--")
            self.result_detail.set_text(
                f"No throughput on the {description} right now — pick a custom rate to model one."
            )
        else:
            total_bytes = size_value * size_factor
            seconds = total_bytes / rate
            self.result.set_text(fmt_duration(seconds))
            self.result_detail.set_text(
                f"{fmt_bytes(total_bytes)} at {fmt_rate(rate)} ({fmt_bits(rate)}) — {description}"
            )

        # Volume projection is about the rate alone, so it stands even when the
        # size field is empty.
        if rate > 0:
            self.projection.set("Per minute", fmt_bytes(rate * 60))
            self.projection.set("Per hour", fmt_bytes(rate * 3600))
            self.projection.set("Per day", fmt_bytes(rate * 86400))
            self.projection.set("Per 30 days", fmt_bytes(rate * 86400 * 30))
            self.projection.set("Time to move 1 TiB", fmt_duration(1024**4 / rate))
        else:
            for key in ("Per minute", "Per hour", "Per day", "Per 30 days", "Time to move 1 TiB"):
                self.projection.set(key, "--")


class NetworkPage(Page):
    title = "Network"
    icons = ("network-wired-symbolic", "network-transmit-receive-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)

        tiles = row(homogeneous=True)
        self.tile_down = StatTile("Download", slot=0)
        self.tile_up = StatTile("Upload", slot=1)
        self.tile_session = StatTile("Session transferred", slot=2, with_sparkline=False)
        self.tile_total = StatTile("Since boot", slot=3, with_sparkline=False)
        for tile in (self.tile_down, self.tile_up, self.tile_session, self.tile_total):
            tiles.append(tile)
        self.box.append(tiles)

        self.graph = GraphCard(
            "Throughput",
            fmt_rate,
            height=160,
            subtitle="All interfaces except loopback",
        )
        self.box.append(self.graph)

        counter_row = row(spacing=10)
        self.reset_button = Gtk.Button(label="Reset session counters")
        self.reset_button.add_css_class("pill")
        self.reset_button.connect("clicked", self._on_reset)
        counter_row.append(self.reset_button)
        self.session_label = Gtk.Label(label="", xalign=0)
        self.session_label.add_css_class("caption")
        self.session_label.add_css_class("dim-label")
        self.session_label.set_valign(Gtk.Align.CENTER)
        counter_row.append(self.session_label)
        self.box.append(counter_row)

        self.calculator = BandwidthCalculator()
        self.box.append(self.calculator)

        self.interfaces_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.box.append(self.interfaces_box)
        self._cards: dict[str, InterfaceCard] = {}

        # --- connections ------------------------------------------------------
        connections = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        connections.add_css_class("card")
        connections.add_css_class("sysmon-card")
        header = row(spacing=8)
        heading = Gtk.Label(label="Active connections", xalign=0)
        heading.add_css_class("heading")
        header.append(heading)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        header.append(spacer)
        self.conn_count = Gtk.Label(label="")
        self.conn_count.add_css_class("caption")
        self.conn_count.add_css_class("dim-label")
        self.conn_count.set_valign(Gtk.Align.CENTER)
        header.append(self.conn_count)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.add_css_class("flat")
        refresh.set_tooltip_text("Refresh connection list")
        refresh.connect("clicked", lambda *_: self._refresh_connections())
        header.append(refresh)
        connections.append(header)

        self.conn_grid = Gtk.Grid(column_spacing=14, row_spacing=2)
        connections.append(self.conn_grid)
        self.box.append(connections)

        self._bound = False
        self._conn_tick = 0

    # --------------------------------------------------------------- actions

    def _on_reset(self, _button) -> None:
        self.hub.network.reset_session()

    def _refresh_connections(self) -> None:
        rows = self.hub.network.connections()
        established = sum(1 for item in rows if item["status"] == "ESTABLISHED")
        self.conn_count.set_text(f"{len(rows)} sockets · {established} established")

        while (child := self.conn_grid.get_first_child()) is not None:
            self.conn_grid.remove(child)

        headers = ("Proto", "Local", "Remote", "State", "Process")
        for column_index, text in enumerate(headers):
            label = Gtk.Label(label=text, xalign=0)
            label.add_css_class("caption")
            label.add_css_class("dim-label")
            self.conn_grid.attach(label, column_index, 0, 1, 1)

        # Cap the rendered rows: a busy machine can hold thousands of sockets
        # and building that many widgets would stall the frame.
        for row_index, item in enumerate(rows[:80], start=1):
            values = (
                f"{item['proto']}/{item['family'][-1]}",
                item["local"],
                item["remote"] or "—",
                item["status"],
                f"{item['process']} ({item['pid']})" if item["pid"] else "—",
            )
            for column_index, text in enumerate(values):
                label = Gtk.Label(label=text, xalign=0)
                label.add_css_class("caption")
                label.add_css_class("numeric")
                label.set_ellipsize(3)  # END
                label.set_max_width_chars(30)
                if column_index == 3 and item["status"] == "ESTABLISHED":
                    label.remove_css_class("numeric")
                self.conn_grid.attach(label, column_index, row_index, 1, 1)

        if len(rows) > 80:
            note = Gtk.Label(label=f"… and {len(rows) - 80} more", xalign=0)
            note.add_css_class("caption")
            note.add_css_class("dim-label")
            self.conn_grid.attach(note, 0, 81, 5, 1)

    # ---------------------------------------------------------------- updates

    def _bind(self, history: History) -> None:
        self.graph.set_series(
            [
                ("Download", history.series("net.rx"), 0),
                ("Upload", history.series("net.tx"), 1),
            ]
        )
        self.tile_down.bind(history.series("net.rx"))
        self.tile_up.bind(history.series("net.tx"))
        self._bound = True

    def set_active(self, active: bool) -> None:
        super().set_active(active)
        if active:
            GLib.idle_add(self._refresh_connections)

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        if not self._bound:
            self._bind(history)

        network = snapshot.get("network") or {}
        totals = network.get("totals") or {}
        session = network.get("session") or {}

        rx = totals.get("rx_bps") or 0.0
        tx = totals.get("tx_bps") or 0.0
        self.tile_down.set_value(fmt_rate(rx), fmt_bits(rx))
        self.tile_up.set_value(fmt_rate(tx), fmt_bits(tx))
        self.tile_session.set_value(
            fmt_bytes(session.get("total")),
            f"↓ {fmt_bytes(session.get('rx'))} · ↑ {fmt_bytes(session.get('tx'))}",
        )
        self.tile_total.set_value(
            fmt_bytes((totals.get("rx_bytes") or 0) + (totals.get("tx_bytes") or 0)),
            f"↓ {fmt_bytes(totals.get('rx_bytes'))} · ↑ {fmt_bytes(totals.get('tx_bytes'))}",
        )

        elapsed = session.get("elapsed") or 1.0
        self.session_label.set_text(
            f"Counting for {fmt_duration(elapsed)} · average "
            f"↓ {fmt_rate(session.get('avg_rx_bps'))} ↑ {fmt_rate(session.get('avg_tx_bps'))}"
        )

        average = ((session.get("avg_rx_bps") or 0.0) + (session.get("avg_tx_bps") or 0.0))
        self.calculator.set_live_rates(rx, tx, average)

        # Rebuild interface cards only when the set of interfaces changes.
        interfaces = network.get("interfaces") or []
        names = [item["name"] for item in interfaces]
        if names != list(self._cards):
            while (child := self.interfaces_box.get_first_child()) is not None:
                self.interfaces_box.remove(child)
            self._cards = {}
            for item in interfaces:
                card = InterfaceCard(item["name"])
                self._cards[item["name"]] = card
                self.interfaces_box.append(card)
        for item in interfaces:
            self._cards[item["name"]].update(item)

        self.graph.refresh_legend()
        self.graph.graph.queue_draw()

        # Sockets change far more slowly than counters; refresh every 5 ticks.
        self._conn_tick += 1
        if self.is_active and self._conn_tick % 5 == 0:
            self._refresh_connections()
