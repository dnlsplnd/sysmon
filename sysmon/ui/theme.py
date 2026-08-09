"""Colour tokens for the Cairo widgets, following the validated data-viz palette.

Everything is expressed as (r, g, b) floats in 0..1 because that is what Cairo
wants. Both modes are *selected* -- the dark column is the same eight hues
re-stepped for the dark surface, not an automatic inversion.
"""

from __future__ import annotations

from gi.repository import Adw


def _rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    return (
        int(value[0:2], 16) / 255.0,
        int(value[2:4], 16) / 255.0,
        int(value[4:6], 16) / 255.0,
    )


# Categorical slots, in fixed order. Never cycled: past slot 8 a chart folds the
# remainder into "Other" rather than inventing a ninth hue.
_CATEGORICAL_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
_CATEGORICAL_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"]

# Single-hue blue ramp, light -> dark, for magnitude encoding (the core heatmap).
_SEQUENTIAL = [
    "#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5",
    "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b",
]

# Status roles are fixed across modes and never reused as a series colour.
_STATUS = {
    "good": "#0ca30c",
    "warning": "#fab219",
    "serious": "#ec835a",
    "critical": "#d03b3b",
}

_CHROME = {
    "light": {
        "surface": "#fcfcfb",
        "plane": "#f9f9f7",
        "primary": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "grid": "#e1e0d9",
        "axis": "#c3c2b7",
    },
    "dark": {
        "surface": "#1a1a19",
        "plane": "#0d0d0d",
        "primary": "#ffffff",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "grid": "#2c2c2a",
        "axis": "#383835",
    },
}


class Theme:
    """Resolves colour roles against the current light/dark appearance."""

    def __init__(self) -> None:
        self._manager = Adw.StyleManager.get_default()

    @property
    def dark(self) -> bool:
        return self._manager.get_dark()

    @property
    def mode(self) -> str:
        return "dark" if self.dark else "light"

    # ---------------------------------------------------------------- roles

    def chrome(self, role: str) -> tuple[float, float, float]:
        return _rgb(_CHROME[self.mode][role])

    def series(self, index: int) -> tuple[float, float, float]:
        """Categorical slot by position. Callers pass a stable index per entity,
        so a filtered-out series never repaints the survivors."""
        palette = _CATEGORICAL_DARK if self.dark else _CATEGORICAL_LIGHT
        return _rgb(palette[index % len(palette)])

    def series_hex(self, index: int) -> str:
        palette = _CATEGORICAL_DARK if self.dark else _CATEGORICAL_LIGHT
        return palette[index % len(palette)]

    def status(self, role: str) -> tuple[float, float, float]:
        return _rgb(_STATUS[role])

    def sequential(self, fraction: float) -> tuple[float, float, float]:
        """Sample the blue ramp at 0..1. Lightest step means 'near zero'."""
        fraction = max(0.0, min(1.0, fraction))
        ramp = _SEQUENTIAL if not self.dark else _SEQUENTIAL[2:]
        index = int(round(fraction * (len(ramp) - 1)))
        return _rgb(ramp[index])

    def load_status(self, percent: float | None) -> str:
        """Map a 0-100 utilisation onto a status role.

        Thresholds are deliberately generous: a monitor that shouts 'critical'
        at 70% teaches the user to ignore it.
        """
        if percent is None:
            return "good"
        if percent >= 95:
            return "critical"
        if percent >= 85:
            return "serious"
        if percent >= 70:
            return "warning"
        return "good"

    def temp_status(self, celsius: float | None) -> str:
        if celsius is None:
            return "good"
        if celsius >= 90:
            return "critical"
        if celsius >= 80:
            return "serious"
        if celsius >= 70:
            return "warning"
        return "good"


THEME = Theme()
