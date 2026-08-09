"""Composite containers: graph cards, stat tiles and key/value detail lists."""

from __future__ import annotations

import math
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk, Pango  # noqa: E402

from ..theme import THEME  # noqa: E402
from ...history import Series  # noqa: E402
from .graph import GraphArea, Sparkline  # noqa: E402


class Swatch(Gtk.DrawingArea):
    """A small colour chip that carries series identity beside ink-coloured text."""

    def __init__(self, slot: int, size: int = 9) -> None:
        super().__init__()
        self.slot = slot
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_valign(Gtk.Align.CENTER)
        self.set_draw_func(self._draw)

    def _draw(self, _area, context, width: int, height: int) -> None:
        context.set_source_rgb(*THEME.series(self.slot))
        radius = min(width, height) / 2
        context.arc(width / 2, height / 2, radius, 0, 2 * math.pi)
        context.fill()


class LegendItem(Gtk.Box):
    """Swatch + name + live value -- the direct label the palette's contrast
    warning requires, so identity never rests on colour alone."""

    def __init__(self, label: str, slot: int) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        self.append(Swatch(slot))
        self._name = Gtk.Label(label=label)
        self._name.add_css_class("dim-label")
        self._name.add_css_class("caption")
        # Long series names (sensor rails, interface names) must not be able to
        # widen the card without bound.
        self._name.set_ellipsize(Pango.EllipsizeMode.END)
        self._name.set_max_width_chars(22)
        self._name.set_tooltip_text(label)
        self.append(self._name)
        self._value = Gtk.Label(label="--")
        self._value.add_css_class("caption-heading")
        self._value.add_css_class("numeric")
        self.append(self._value)

    def set_value(self, text: str) -> None:
        self._value.set_text(text)

    def set_name(self, text: str) -> None:
        self._name.set_text(text)


class GraphCard(Gtk.Box):
    """A titled card wrapping a :class:`GraphArea` with a direct-labelled legend."""

    def __init__(
        self,
        title: str,
        formatter: Callable[[float | None], str],
        fixed_max: float | None = None,
        height: int = 130,
        subtitle: str | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")
        self.formatter = formatter

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("heading")
        title_box.append(title_label)
        self._subtitle = Gtk.Label(label=subtitle or "", xalign=0)
        self._subtitle.add_css_class("caption")
        self._subtitle.add_css_class("dim-label")
        self._subtitle.set_visible(bool(subtitle))
        title_box.append(self._subtitle)
        header.append(title_box)

        self.append(header)

        # A FlowBox rather than a Box: with eight temperature rails on one
        # chart, a non-wrapping legend demands more width than the display has
        # and drags the whole window off screen. It gets its own row instead of
        # sharing the header, because squeezed beside the title GTK measures it
        # at zero width and warns on every frame.
        self._legend_box = Gtk.FlowBox()
        self._legend_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._legend_box.set_max_children_per_line(8)
        self._legend_box.set_min_children_per_line(1)
        self._legend_box.set_row_spacing(2)
        self._legend_box.set_column_spacing(14)
        self._legend_box.set_halign(Gtk.Align.START)
        self._legend_box.set_visible(False)
        self.append(self._legend_box)

        self.graph = GraphArea(formatter=formatter, fixed_max=fixed_max, height=height)
        self.append(self.graph)

        self._legend: dict[str, LegendItem] = {}
        self._entries: list[tuple[str, Series, int]] = []

    def set_subtitle(self, text: str) -> None:
        self._subtitle.set_text(text)
        self._subtitle.set_visible(bool(text))

    def set_series(self, entries: list[tuple[str, Series, int]]) -> None:
        """Bind series to the plot and rebuild the legend when the set changes."""
        signature = [(label, slot) for label, _, slot in entries]
        current = [(label, item_slot) for label, item_slot in self._legend_signature()]
        if signature != current:
            while (child := self._legend_box.get_first_child()) is not None:
                self._legend_box.remove(child)
            self._legend.clear()
            # A single series needs no legend box -- the card title names it.
            if len(entries) > 1:
                for label, _, slot in entries:
                    item = LegendItem(label, slot)
                    self._legend[label] = item
                    self._legend_box.append(item)
            self._legend_box.set_visible(len(entries) > 1)
        self._entries = entries
        self.graph.set_series(entries)
        self.refresh_legend()

    def _legend_signature(self) -> list[tuple[str, int]]:
        return [(label, slot) for label, _, slot in self._entries]

    def refresh_legend(self) -> None:
        for label, series, _ in self._entries:
            item = self._legend.get(label)
            if item is not None:
                item.set_value(self.formatter(series.latest()))


class StatTile(Gtk.Box):
    """A hero number with a caption and an optional sparkline.

    Used where a chart would be overkill -- a single current reading whose trend
    is a supporting detail, not the point.
    """

    def __init__(
        self,
        title: str,
        slot: int = 0,
        with_sparkline: bool = True,
        subtitle: str = "",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.add_css_class("card")
        self.add_css_class("sysmon-tile")
        self.set_hexpand(True)

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.add_css_class("caption")
        title_label.add_css_class("dim-label")
        self.append(title_label)

        self._value = Gtk.Label(label="--", xalign=0)
        self._value.add_css_class("title-2")
        self._value.add_css_class("numeric")
        self.append(self._value)

        self._subtitle = Gtk.Label(label=subtitle, xalign=0)
        self._subtitle.add_css_class("caption")
        self._subtitle.add_css_class("dim-label")
        self._subtitle.set_visible(bool(subtitle))
        self.append(self._subtitle)

        self.sparkline: Sparkline | None = None
        if with_sparkline:
            self.sparkline = Sparkline(slot=slot, height=26)
            self.sparkline.set_hexpand(True)
            self.append(self.sparkline)

    def set_value(self, text: str, subtitle: str | None = None) -> None:
        self._value.set_text(text)
        if subtitle is not None:
            self._subtitle.set_text(subtitle)
            self._subtitle.set_visible(bool(subtitle))

    def bind(self, series: Series, fixed_max: float | None = None) -> None:
        if self.sparkline is not None:
            self.sparkline.set_series(series, fixed_max)


class KeyValueList(Gtk.Box):
    """A compact two-column detail list built from rows of plain labels."""

    def __init__(self, title: str | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add_css_class("card")
        self.add_css_class("sysmon-card")

        if title:
            label = Gtk.Label(label=title, xalign=0)
            label.add_css_class("heading")
            self.append(label)

        self._grid = Gtk.Grid(column_spacing=16, row_spacing=3)
        self._grid.set_hexpand(True)
        self.append(self._grid)
        self._rows: dict[str, Gtk.Label] = {}
        self._order: list[str] = []

    def set(self, key: str, value: str) -> None:
        existing = self._rows.get(key)
        if existing is not None:
            existing.set_text(value)
            existing.set_tooltip_text(value)
            return

        row = len(self._order)
        key_label = Gtk.Label(label=key, xalign=0)
        key_label.add_css_class("caption")
        key_label.add_css_class("dim-label")
        value_label = Gtk.Label(label=value, xalign=0)
        value_label.add_css_class("caption")
        value_label.add_css_class("numeric")
        value_label.set_selectable(True)
        value_label.set_hexpand(True)
        # Ellipsise rather than wrap. A wrapping label inside a grid inside a
        # homogeneous box makes GTK's height-for-width measurement circular
        # (it warns "needs at least N"), and single-line rows keep these dense
        # detail tables scannable. The tooltip carries anything that is cut.
        value_label.set_ellipsize(Pango.EllipsizeMode.END)
        value_label.set_max_width_chars(44)
        value_label.set_tooltip_text(value)

        self._grid.attach(key_label, 0, row, 1, 1)
        self._grid.attach(value_label, 1, row, 1, 1)
        self._rows[key] = value_label
        self._order.append(key)

    def clear(self) -> None:
        while (child := self._grid.get_first_child()) is not None:
            self._grid.remove(child)
        self._rows.clear()
        self._order.clear()


def section_title(text: str) -> Gtk.Label:
    label = Gtk.Label(label=text, xalign=0)
    label.add_css_class("title-4")
    label.set_margin_top(4)
    return label
