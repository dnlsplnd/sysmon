"""The time-series graph: a Cairo plot with grid, auto-scale, and a hover crosshair."""

from __future__ import annotations

import math
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("PangoCairo", "1.0")

from gi.repository import Gtk, Pango, PangoCairo  # noqa: E402

from ..theme import THEME  # noqa: E402
from ...history import Series  # noqa: E402

# Plot padding. The left gutter holds y-axis tick labels, and has to fit the
# widest one a formatter can produce ("1023.9 KiB/s") without clipping.
_PAD_LEFT = 72
_PAD_RIGHT = 8
_PAD_TOP = 10
_PAD_BOTTOM = 18

_LINE_WIDTH = 2.0
_AREA_ALPHA = 0.16
_GRID_ROWS = 4


def nice_ceiling(value: float) -> float:
    """Round up to the next 1/2/5 x 10^n, so axis labels land on readable numbers."""
    if value <= 0:
        return 1.0
    exponent = math.floor(math.log10(value))
    magnitude = 10.0**exponent
    for step in (1.0, 2.0, 5.0, 10.0):
        if value <= step * magnitude * 1.0001:
            return step * magnitude
    return 10.0 * magnitude


class GraphArea(Gtk.DrawingArea):
    """Draws one or more :class:`Series` against a shared time axis.

    A ``None`` in a series is a genuine gap -- the line breaks rather than
    dropping to zero, because a missing sample is not a measurement of nothing.
    """

    def __init__(
        self,
        formatter: Callable[[float | None], str],
        fixed_max: float | None = None,
        height: int = 120,
        show_axis: bool = True,
    ) -> None:
        super().__init__()
        self.formatter = formatter
        self.fixed_max = fixed_max
        self.show_axis = show_axis
        self._entries: list[tuple[str, Series, int]] = []
        self._scale = 1.0
        self._hover_x: float | None = None

        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_draw_func(self._draw)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        motion.connect("leave", self._on_leave)
        self.add_controller(motion)

    # ---------------------------------------------------------------- config

    def set_series(self, entries: list[tuple[str, Series, int]]) -> None:
        """Replace the plotted series. Each entry is (label, series, palette slot)."""
        self._entries = entries
        self.queue_draw()

    # ----------------------------------------------------------------- hover

    def _on_motion(self, _controller, x: float, _y: float) -> None:
        self._hover_x = x
        self.queue_draw()

    def _on_leave(self, _controller) -> None:
        self._hover_x = None
        self.queue_draw()

    # ------------------------------------------------------------- geometry

    def _span(self) -> int:
        """Number of sample slots the x-axis covers.

        This tracks how much history has actually been collected rather than
        the buffer's full capacity, so a freshly-started graph fills the width
        instead of cramming a few points against the right edge. Once the
        buffer is full the two are equal and the plot scrolls normally.
        """
        longest = max((len(entry[1]) for entry in self._entries), default=0)
        return max(2, longest)

    def _compute_scale(self) -> float:
        if self.fixed_max is not None:
            return self.fixed_max
        peak = 0.0
        for _, series, _ in self._entries:
            peak = max(peak, series.maximum(0.0))
        target = nice_ceiling(peak * 1.15) if peak > 0 else 1.0
        # Grow immediately so a spike is never clipped, but shrink only when the
        # data has dropped well clear of the current ceiling -- otherwise the
        # axis flickers between two scales on every tick.
        if target > self._scale or target < self._scale * 0.5:
            self._scale = target
        return self._scale

    # ------------------------------------------------------------------ draw

    @staticmethod
    def _layout(context, text: str, size: int = 9):
        layout = PangoCairo.create_layout(context)
        description = Pango.FontDescription()
        description.set_family("system-ui, sans-serif")
        description.set_size(size * Pango.SCALE)
        layout.set_font_description(description)
        layout.set_text(text, -1)
        return layout

    def _draw(self, _area, context, width: int, height: int) -> None:
        if not self._entries:
            return

        pad_left = _PAD_LEFT if self.show_axis else 4
        pad_bottom = _PAD_BOTTOM if self.show_axis else 4
        plot_w = max(1.0, width - pad_left - _PAD_RIGHT)
        plot_h = max(1.0, height - _PAD_TOP - pad_bottom)
        scale = self._compute_scale()
        span = self._span()

        def x_at(index: int, offset: int) -> float:
            return pad_left + plot_w * ((offset + index) / max(1, span - 1))

        def y_at(value: float) -> float:
            return _PAD_TOP + plot_h * (1.0 - max(0.0, min(1.0, value / scale)))

        # --- grid ------------------------------------------------------------
        context.set_line_width(1.0)
        context.set_source_rgb(*THEME.chrome("grid"))
        for row in range(_GRID_ROWS + 1):
            y = _PAD_TOP + plot_h * row / _GRID_ROWS
            context.move_to(pad_left, round(y) + 0.5)
            context.line_to(pad_left + plot_w, round(y) + 0.5)
        context.stroke()

        if self.show_axis:
            context.set_source_rgb(*THEME.chrome("muted"))
            for row in range(_GRID_ROWS + 1):
                value = scale * (1.0 - row / _GRID_ROWS)
                y = _PAD_TOP + plot_h * row / _GRID_ROWS
                layout = self._layout(context, self.formatter(value), 8)
                _, extent = layout.get_pixel_extents()
                context.move_to(pad_left - 6 - extent.width, y - extent.height / 2)
                PangoCairo.show_layout(context, layout)

        # --- series ----------------------------------------------------------
        single = len(self._entries) == 1
        for label, series, slot in self._entries:
            values = list(series.values)
            # Shorter series (created later) right-align against the newest tick.
            offset = span - len(values)
            colour = THEME.series(slot)

            # Split into runs of consecutive real samples so gaps stay gaps.
            runs: list[list[tuple[float, float]]] = []
            current: list[tuple[float, float]] = []
            for index, value in enumerate(values):
                if value is None:
                    if current:
                        runs.append(current)
                        current = []
                    continue
                current.append((x_at(index, offset), y_at(float(value))))
            if current:
                runs.append(current)

            if single:
                baseline = _PAD_TOP + plot_h
                context.set_source_rgba(*colour, _AREA_ALPHA)
                for run in runs:
                    if len(run) < 2:
                        continue
                    context.move_to(run[0][0], baseline)
                    for point in run:
                        context.line_to(*point)
                    context.line_to(run[-1][0], baseline)
                    context.close_path()
                    context.fill()

            context.set_source_rgb(*colour)
            context.set_line_width(_LINE_WIDTH)
            context.set_line_join(1)  # round
            context.set_line_cap(1)
            for run in runs:
                if len(run) == 1:
                    context.arc(run[0][0], run[0][1], _LINE_WIDTH / 2, 0, 2 * math.pi)
                    context.fill()
                    continue
                context.move_to(*run[0])
                for point in run[1:]:
                    context.line_to(*point)
                context.stroke()

        # --- baseline --------------------------------------------------------
        context.set_line_width(1.0)
        context.set_source_rgb(*THEME.chrome("axis"))
        context.move_to(pad_left, round(_PAD_TOP + plot_h) + 0.5)
        context.line_to(pad_left + plot_w, round(_PAD_TOP + plot_h) + 0.5)
        context.stroke()

        # --- hover crosshair -------------------------------------------------
        if self._hover_x is not None and pad_left <= self._hover_x <= pad_left + plot_w:
            self._draw_hover(context, pad_left, plot_w, plot_h, span, scale, width)

    def _draw_hover(self, context, pad_left, plot_w, plot_h, span, scale, width) -> None:
        fraction = (self._hover_x - pad_left) / plot_w
        slot = int(round(fraction * (span - 1)))

        readings: list[tuple[str, float, int]] = []
        for label, series, palette_slot in self._entries:
            values = list(series.values)
            index = slot - (span - len(values))
            if 0 <= index < len(values) and values[index] is not None:
                readings.append((label, float(values[index]), palette_slot))
        if not readings:
            return

        x = pad_left + plot_w * (slot / max(1, span - 1))

        context.set_line_width(1.0)
        context.set_source_rgba(*THEME.chrome("muted"), 0.7)
        context.move_to(round(x) + 0.5, _PAD_TOP)
        context.line_to(round(x) + 0.5, _PAD_TOP + plot_h)
        context.stroke()

        for _, value, palette_slot in readings:
            y = _PAD_TOP + plot_h * (1.0 - max(0.0, min(1.0, value / scale)))
            # A surface-coloured ring keeps overlapping markers separable.
            context.set_source_rgb(*THEME.chrome("surface"))
            context.arc(x, y, 4.5, 0, 2 * math.pi)
            context.fill()
            context.set_source_rgb(*THEME.series(palette_slot))
            context.arc(x, y, 3.0, 0, 2 * math.pi)
            context.fill()

        lines = [f"{label}  {self.formatter(value)}" for label, value, _ in readings]
        layouts = [self._layout(context, line, 8) for line in lines]
        box_w = max(layout.get_pixel_extents()[1].width for layout in layouts) + 14
        line_h = layouts[0].get_pixel_extents()[1].height + 3
        box_h = line_h * len(layouts) + 8

        # Flip the tooltip to the other side of the cursor near the right edge.
        box_x = x + 10 if x + 10 + box_w < width else x - 10 - box_w
        box_y = _PAD_TOP + 4

        context.set_source_rgba(*THEME.chrome("surface"), 0.97)
        context.rectangle(box_x, box_y, box_w, box_h)
        context.fill()
        context.set_line_width(1.0)
        context.set_source_rgba(*THEME.chrome("axis"), 0.9)
        context.rectangle(box_x + 0.5, box_y + 0.5, box_w - 1, box_h - 1)
        context.stroke()

        for index, (layout, (_, _, palette_slot)) in enumerate(zip(layouts, readings)):
            y = box_y + 4 + index * line_h
            context.set_source_rgb(*THEME.series(palette_slot))
            context.rectangle(box_x + 5, y + 4, 4, 4)
            context.fill()
            # Text stays in ink; the swatch beside it carries series identity.
            context.set_source_rgb(*THEME.chrome("primary"))
            context.move_to(box_x + 13, y)
            PangoCairo.show_layout(context, layout)


class Sparkline(Gtk.DrawingArea):
    """A compact, axis-free trend line for stat tiles."""

    def __init__(self, slot: int = 0, height: int = 32, width: int = 90) -> None:
        super().__init__()
        self.slot = slot
        self._series: Series | None = None
        self._fixed_max: float | None = None
        self.set_content_height(height)
        self.set_content_width(width)
        self.set_draw_func(self._draw)

    def set_series(self, series: Series, fixed_max: float | None = None) -> None:
        self._series = series
        self._fixed_max = fixed_max
        self.queue_draw()

    def _draw(self, _area, context, width: int, height: int) -> None:
        series = self._series
        if series is None or not len(series):
            return

        values = list(series.values)
        # Match the main graphs: span what has been collected, not the capacity.
        span = max(2, len(values))
        offset = span - len(values)
        scale = self._fixed_max if self._fixed_max is not None else nice_ceiling(series.maximum(0.0) * 1.15)
        if not scale:
            scale = 1.0

        pad = 2.0
        plot_h = max(1.0, height - pad * 2)

        def point(index: int, value: float) -> tuple[float, float]:
            x = width * ((offset + index) / max(1, span - 1))
            y = pad + plot_h * (1.0 - max(0.0, min(1.0, value / scale)))
            return x, y

        runs: list[list[tuple[float, float]]] = []
        current: list[tuple[float, float]] = []
        for index, value in enumerate(values):
            if value is None:
                if current:
                    runs.append(current)
                    current = []
                continue
            current.append(point(index, float(value)))
        if current:
            runs.append(current)

        colour = THEME.series(self.slot)
        context.set_source_rgba(*colour, _AREA_ALPHA)
        for run in runs:
            if len(run) < 2:
                continue
            context.move_to(run[0][0], height)
            for item in run:
                context.line_to(*item)
            context.line_to(run[-1][0], height)
            context.close_path()
            context.fill()

        context.set_source_rgb(*colour)
        context.set_line_width(1.5)
        context.set_line_join(1)
        context.set_line_cap(1)
        for run in runs:
            if len(run) < 2:
                continue
            context.move_to(*run[0])
            for item in run[1:]:
                context.line_to(*item)
            context.stroke()
