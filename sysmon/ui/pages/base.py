"""Shared page scaffolding."""

from __future__ import annotations

from typing import Any

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from ...history import History  # noqa: E402


class Page(Gtk.ScrolledWindow):
    """A scrollable, padded column of cards.

    Subclasses build their widgets in ``__init__`` and mutate them in
    :meth:`update`. Pages are never rebuilt per tick -- recreating widgets 60
    times a minute would thrash the layout and lose scroll position.
    """

    title = "Page"
    # Icon names in order of preference, resolved against whatever theme is
    # actually installed. Icon naming is not portable between Adwaita and
    # Breeze -- there is no "processor-symbolic" or "temperature-symbolic" on
    # KDE, and no "monitor-symbolic" on GNOME -- so each page offers the names
    # it knows about and the first one present wins.
    icons: tuple[str, ...] = ()
    icon = "utilities-system-monitor-symbolic"

    def __init__(self, hub) -> None:
        super().__init__()
        self.hub = hub
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)

        clamp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.box.set_margin_top(16)
        self.box.set_margin_bottom(24)
        self.box.set_margin_start(16)
        self.box.set_margin_end(16)
        clamp.append(self.box)
        self.set_child(clamp)

        self._visible = False

    def set_active(self, active: bool) -> None:
        """Track visibility so hidden pages can skip expensive redraws."""
        self._visible = active
        if active:
            snapshot = getattr(self.hub, "snapshot", None)
            if snapshot:
                self.update(snapshot, self.hub.history)

    @property
    def is_active(self) -> bool:
        return self._visible

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        raise NotImplementedError


def row(spacing: int = 14, homogeneous: bool = False) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=spacing)
    box.set_homogeneous(homogeneous)
    return box


def column(spacing: int = 14) -> Gtk.Box:
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
