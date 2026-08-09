"""Gauges, bar meters, stacked meters and the per-core heatmap."""

from __future__ import annotations

import math

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

from ..theme import THEME  # noqa: E402

# Data ends get a 4px round cap; segments are separated by a 2px surface gap so
# adjacent fills never read as one continuous block.
_RADIUS = 4.0
_GAP = 2.0


def _text_layout(context, text: str, size: int, bold: bool = False):
    layout = PangoCairo.create_layout(context)
    description = Pango.FontDescription()
    description.set_family("system-ui, sans-serif")
    description.set_size(size * Pango.SCALE)
    if bold:
        description.set_weight(Pango.Weight.BOLD)
    layout.set_font_description(description)
    layout.set_text(text, -1)
    return layout


def _rounded_rect(context, x: float, y: float, width: float, height: float, radius: float) -> None:
    radius = min(radius, height / 2, width / 2) if width > 0 else 0
    if radius <= 0:
        context.rectangle(x, y, max(0.0, width), height)
        return
    context.new_sub_path()
    context.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    context.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    context.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    context.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    context.close_path()


class Gauge(Gtk.DrawingArea):
    """A 270-degree arc gauge with the value as a hero number in the middle."""

    def __init__(self, size: int = 132, slot: int = 0, suffix: str = "%") -> None:
        super().__init__()
        self.slot = slot
        self.suffix = suffix
        self._value: float | None = None
        self._maximum = 100.0
        self._caption = ""
        self._status: str | None = None
        self.set_content_width(size)
        self.set_content_height(size)
        self.set_draw_func(self._draw)

    def set_value(
        self,
        value: float | None,
        maximum: float = 100.0,
        caption: str = "",
        status: str | None = None,
    ) -> None:
        self._value = value
        self._maximum = maximum or 100.0
        self._caption = caption
        self._status = status
        self.queue_draw()

    def _draw(self, _area, context, width: int, height: int) -> None:
        centre_x, centre_y = width / 2, height / 2 + 6
        radius = min(width, height) / 2 - 12
        thickness = max(7.0, radius * 0.17)

        start = math.radians(135)
        sweep = math.radians(270)

        context.set_line_width(thickness)
        context.set_line_cap(1)

        context.set_source_rgb(*THEME.chrome("grid"))
        context.arc(centre_x, centre_y, radius, start, start + sweep)
        context.stroke()

        fraction = 0.0 if self._value is None else max(0.0, min(1.0, self._value / self._maximum))
        if fraction > 0:
            colour = THEME.status(self._status) if self._status else THEME.series(self.slot)
            context.set_source_rgb(*colour)
            context.arc(centre_x, centre_y, radius, start, start + sweep * fraction)
            context.stroke()

        value_text = "--" if self._value is None else f"{self._value:.0f}{self.suffix}"
        layout = _text_layout(context, value_text, 21, bold=True)
        _, extent = layout.get_pixel_extents()
        context.set_source_rgb(*THEME.chrome("primary"))
        context.move_to(centre_x - extent.width / 2, centre_y - extent.height / 2 - 6)
        PangoCairo.show_layout(context, layout)

        if self._caption:
            layout = _text_layout(context, self._caption, 9)
            _, extent = layout.get_pixel_extents()
            context.set_source_rgb(*THEME.chrome("secondary"))
            context.move_to(centre_x - extent.width / 2, centre_y + 12)
            PangoCairo.show_layout(context, layout)


class Meter(Gtk.DrawingArea):
    """A horizontal bar, optionally split into stacked segments.

    Segments are (label, value, palette slot). Values share one scale, so this
    doubles as a composition breakdown (used / cached / free) rather than a
    plain percentage bar.
    """

    def __init__(self, height: int = 14) -> None:
        super().__init__()
        self._segments: list[tuple[str, float, int]] = []
        self._total = 1.0
        self._status: str | None = None
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

    def set_segments(self, segments: list[tuple[str, float, int]], total: float) -> None:
        self._segments = segments
        self._total = total if total > 0 else 1.0
        self._status = None
        self.queue_draw()

    def set_fraction(self, value: float, total: float, status: str | None = None) -> None:
        self._segments = [("", max(0.0, value), 0)]
        self._total = total if total > 0 else 1.0
        self._status = status
        self.queue_draw()

    def _draw(self, _area, context, width: int, height: int) -> None:
        context.set_source_rgb(*THEME.chrome("grid"))
        _rounded_rect(context, 0, 0, width, height, _RADIUS)
        context.fill()

        cursor = 0.0
        for index, (_, value, slot) in enumerate(self._segments):
            span = width * (max(0.0, value) / self._total)
            if span <= 0.5:
                continue
            # Trim each segment by the gap so the surface shows between fills.
            visible = span - (_GAP if index < len(self._segments) - 1 else 0.0)
            colour = THEME.status(self._status) if self._status else THEME.series(slot)
            context.set_source_rgb(*colour)
            _rounded_rect(context, cursor, 0, max(0.0, visible), height, _RADIUS)
            context.fill()
            cursor += span


class CoreHeatmap(Gtk.DrawingArea):
    """One column per logical CPU, height and colour both encoding load.

    Colour comes from the single-hue sequential ramp: this is magnitude, not
    identity, so cores must not get categorical hues.
    """

    def __init__(self, height: int = 96) -> None:
        super().__init__()
        self._values: list[float | None] = []
        self._freqs: list[float | None] = []
        self._threads_per_core = 1
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        self._tooltip_index: int | None = None
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    def set_values(
        self,
        values: list[float | None],
        freqs: list[float | None] | None = None,
        threads_per_core: int = 1,
    ) -> None:
        self._values = values
        self._freqs = freqs or []
        self._threads_per_core = max(1, threads_per_core)
        self.queue_draw()

    def _on_motion(self, _controller, x: float, _y: float) -> None:
        if not self._values:
            return
        allocation_width = self.get_width()
        slot_width = allocation_width / max(1, len(self._values))
        index = int(x // slot_width)
        self._tooltip_index = index if 0 <= index < len(self._values) else None
        self.queue_draw()

    def _on_leave(self, _controller) -> None:
        self._tooltip_index = None
        self.queue_draw()

    def _draw(self, _area, context, width: int, height: int) -> None:
        if not self._values:
            return

        count = len(self._values)
        label_h = 14
        plot_h = height - label_h
        slot_width = width / count
        bar_width = max(2.0, slot_width - _GAP)

        for index, value in enumerate(self._values):
            x = index * slot_width
            context.set_source_rgb(*THEME.chrome("grid"))
            _rounded_rect(context, x, 0, bar_width, plot_h, _RADIUS)
            context.fill()

            if value is None:
                continue
            fraction = max(0.0, min(1.0, value / 100.0))
            bar_h = plot_h * fraction
            if bar_h < 1.0:
                continue
            context.set_source_rgb(*THEME.sequential(fraction))
            _rounded_rect(context, x, plot_h - bar_h, bar_width, bar_h, _RADIUS)
            context.fill()

        # Label every core boundary when there is room, otherwise every other
        # one, so SMT sibling pairs stay legible instead of overprinting.
        step = 1 if slot_width >= 22 else self._threads_per_core
        context.set_source_rgb(*THEME.chrome("muted"))
        for index in range(0, count, step):
            layout = _text_layout(context, str(index), 7)
            _, extent = layout.get_pixel_extents()
            if extent.width > slot_width * step:
                continue
            context.move_to(index * slot_width + (bar_width - extent.width) / 2, plot_h + 2)
            PangoCairo.show_layout(context, layout)

        index = self._tooltip_index
        if index is not None and index < len(self._values):
            value = self._values[index]
            freq = self._freqs[index] if index < len(self._freqs) else None
            parts = [f"CPU {index}", "--" if value is None else f"{value:.0f}%"]
            if freq:
                parts.append(f"{freq / 1000:.2f} GHz")
            layout = _text_layout(context, "   ".join(parts), 8)
            _, extent = layout.get_pixel_extents()
            box_w, box_h = extent.width + 12, extent.height + 8
            box_x = min(max(0.0, index * slot_width - box_w / 2), width - box_w)

            context.set_source_rgba(*THEME.chrome("surface"), 0.97)
            _rounded_rect(context, box_x, 0, box_w, box_h, 4)
            context.fill()
            context.set_line_width(1.0)
            context.set_source_rgba(*THEME.chrome("axis"), 0.9)
            _rounded_rect(context, box_x + 0.5, 0.5, box_w - 1, box_h - 1, 4)
            context.stroke()
            context.set_source_rgb(*THEME.chrome("primary"))
            context.move_to(box_x + 6, 4)
            PangoCairo.show_layout(context, layout)
