"""Processes page: a sortable, filterable table with per-process CPU, memory and I/O."""

from __future__ import annotations

from typing import Any, Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, Pango  # noqa: E402

from .base import Page, row  # noqa: E402
from ..widgets import StatTile  # noqa: E402
from ...history import History  # noqa: E402
from ...util import fmt_bytes, fmt_rate  # noqa: E402

# How many rows are rendered. The table is sorted before it is trimmed, so the
# interesting end is always on screen; rendering all ~500 processes would cost
# far more than it tells anyone.
_VISIBLE_ROWS = 120


class Column:
    """One table column: how to size it, what to show, and what to sort on."""

    __slots__ = ("title", "attr", "width", "numeric", "expand", "format")

    def __init__(
        self,
        title: str,
        attr: str,
        width: int,
        format: Callable[[dict[str, Any]], str],
        numeric: bool = True,
        expand: bool = False,
    ) -> None:
        self.title = title
        self.attr = attr
        self.width = width
        self.format = format
        self.numeric = numeric
        self.expand = expand


COLUMNS = [
    Column("PID", "pid", 70, lambda r: str(r["pid"])),
    Column("Name", "name", 180, lambda r: r["name"], numeric=False, expand=True),
    Column("User", "user", 110, lambda r: r["user"], numeric=False),
    Column("CPU", "cpu", 80, lambda r: f"{r['cpu']:.1f}%"),
    Column("Memory", "rss", 100, lambda r: fmt_bytes(r["rss"])),
    Column("Mem %", "mem_percent", 76, lambda r: f"{r['mem_percent']:.1f}%"),
    Column("Threads", "threads", 78, lambda r: str(r["threads"])),
    Column("Disk I/O", "io_bps", 104, lambda r: fmt_rate(r["io_bps"]) if r["io_bps"] else "—"),
    Column("Status", "status", 92, lambda r: r["status"], numeric=False),
]


class ProcessRow(Gtk.Box):
    """A reusable row of labels.

    Rows are created once and refilled in place. Nothing is added to or removed
    from the table on a tick -- re-sorting is just writing different text into
    the same widgets, which keeps a one-second refresh cheap and, unlike a
    model-driven list, makes the rendered order exactly the order we computed.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.pid: int | None = None
        self.labels: list[Gtk.Label] = []
        for column in COLUMNS:
            label = Gtk.Label(xalign=1.0 if column.numeric else 0.0)
            label.add_css_class("caption")
            label.add_css_class("sysmon-cell")
            if column.numeric:
                label.add_css_class("numeric")
            label.set_ellipsize(Pango.EllipsizeMode.END)
            # Width comes from the size request and the padding lives inside it
            # (via CSS), so header cells and body cells occupy identical space.
            label.set_size_request(column.width, -1)
            label.set_hexpand(column.expand)
            self.labels.append(label)
            self.append(label)

    def fill(self, data: dict[str, Any]) -> None:
        self.pid = data["pid"]
        for label, column in zip(self.labels, COLUMNS):
            label.set_text(column.format(data))


class ProcessesPage(Page):
    title = "Processes"
    icons = ("view-list-symbolic", "format-justify-fill-symbolic")

    def __init__(self, hub) -> None:
        super().__init__(hub)
        # The table scrolls itself, so the page must not scroll as well.
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)

        tiles = row(homogeneous=True)
        self.tile_total = StatTile("Processes", slot=0, with_sparkline=False)
        self.tile_running = StatTile("Runnable", slot=1, with_sparkline=False)
        self.tile_threads = StatTile("Threads", slot=2, with_sparkline=False)
        self.tile_cpu = StatTile("CPU by processes", slot=3, with_sparkline=False)
        for tile in (self.tile_total, self.tile_running, self.tile_threads, self.tile_cpu):
            tiles.append(tile)
        self.box.append(tiles)

        controls = row(spacing=8)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Filter by name, user or PID")
        self.search.set_hexpand(True)
        self.search.connect("search-changed", self._on_search)
        controls.append(self.search)

        self.kill_button = Gtk.Button(label="Terminate")
        self.kill_button.add_css_class("destructive-action")
        self.kill_button.set_sensitive(False)
        self.kill_button.connect("clicked", lambda *_: self._terminate(force=False))
        controls.append(self.kill_button)

        self.force_button = Gtk.Button(label="Kill")
        self.force_button.add_css_class("destructive-action")
        self.force_button.set_sensitive(False)
        self.force_button.connect("clicked", lambda *_: self._terminate(force=True))
        controls.append(self.force_button)
        self.box.append(controls)

        # --- table ------------------------------------------------------------
        table = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        table.add_css_class("card")

        self._sort_attr = "cpu"
        self._sort_desc = True
        self._header_buttons: dict[str, Gtk.Button] = {}

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        header.add_css_class("sysmon-table-header")
        for column in COLUMNS:
            button = Gtk.Button()
            button.add_css_class("flat")
            button.set_size_request(column.width, -1)
            button.set_hexpand(column.expand)
            label = Gtk.Label(label=column.title, xalign=1.0 if column.numeric else 0.0)
            label.add_css_class("caption-heading")
            label.set_ellipsize(Pango.EllipsizeMode.END)
            button.set_child(label)
            button.connect("clicked", self._on_header_clicked, column.attr)
            self._header_buttons[column.attr] = button
            header.append(button)
        table.append(header)
        table.append(Gtk.Separator())

        self.rows_box = Gtk.ListBox()
        self.rows_box.add_css_class("sysmon-rows")
        self.rows_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.rows_box.connect("row-selected", self._on_row_selected)

        self._rows: list[ProcessRow] = []
        for _ in range(_VISIBLE_ROWS):
            process_row = ProcessRow()
            list_row = Gtk.ListBoxRow()
            list_row.set_child(process_row)
            list_row.set_visible(False)
            self.rows_box.append(list_row)
            self._rows.append(process_row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        scroll.set_child(self.rows_box)
        table.append(scroll)

        self.footer = Gtk.Label(label="", xalign=0)
        self.footer.add_css_class("caption")
        self.footer.add_css_class("dim-label")
        self.footer.set_margin_start(8)
        self.footer.set_margin_top(4)
        self.footer.set_margin_bottom(6)
        table.append(self.footer)

        self.box.append(table)
        self.box.set_vexpand(True)

        self.toast_parent: Adw.ToastOverlay | None = None
        self._query = ""
        self._selected_pid: int | None = None
        self._latest: list[dict[str, Any]] = []
        self._update_header_labels()

    # ---------------------------------------------------------------- sorting

    def _on_header_clicked(self, _button, attr: str) -> None:
        if self._sort_attr == attr:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_attr = attr
            # Numbers are most useful largest-first; names read A-Z.
            column = next(c for c in COLUMNS if c.attr == attr)
            self._sort_desc = column.numeric
        self._update_header_labels()
        self._render()

    def _update_header_labels(self) -> None:
        arrow = "▾" if self._sort_desc else "▴"
        for column in COLUMNS:
            button = self._header_buttons[column.attr]
            active = column.attr == self._sort_attr
            button.get_child().set_text(
                f"{column.title} {arrow}" if active else column.title
            )

    # -------------------------------------------------------------- filtering

    def _on_search(self, entry: Gtk.SearchEntry) -> None:
        self._query = entry.get_text().strip().lower()
        self._render()

    def _match(self, data: dict[str, Any]) -> bool:
        if not self._query:
            return True
        return (
            self._query in data["name"].lower()
            or self._query in data["user"].lower()
            or self._query in str(data["pid"])
        )

    # --------------------------------------------------------------- actions

    def _on_row_selected(self, _box, list_row) -> None:
        if list_row is None:
            self._selected_pid = None
        else:
            self._selected_pid = list_row.get_child().pid
        enabled = self._selected_pid is not None
        self.kill_button.set_sensitive(enabled)
        self.force_button.set_sensitive(enabled)

    def _terminate(self, force: bool) -> None:
        pid = self._selected_pid
        if pid is None:
            return
        name = next((r["name"] for r in self._latest if r["pid"] == pid), str(pid))

        dialog = Adw.AlertDialog(
            heading=f"{'Kill' if force else 'Terminate'} {name}?",
            body=(
                f"PID {pid} will be sent SIGKILL and cannot save its work."
                if force
                else f"PID {pid} will be sent SIGTERM, giving it a chance to shut down cleanly."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("confirm", "Kill" if force else "Terminate")
        dialog.set_response_appearance("confirm", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog, response):
            if response != "confirm":
                return
            error = self.hub.processes.terminate(pid, force=force)
            self._toast(
                f"Could not signal {name} ({pid}): {error}"
                if error
                else f"Signalled {name} ({pid})"
            )

        dialog.connect("response", on_response)
        dialog.present(self)

    def _toast(self, message: str) -> None:
        if self.toast_parent is not None:
            self.toast_parent.add_toast(Adw.Toast(title=message))

    # ---------------------------------------------------------------- updates

    def set_active(self, active: bool) -> None:
        # Building the full process table is the most expensive thing the app
        # does, so it only runs while this page is on screen.
        self.hub.processes.detailed = active
        super().set_active(active)

    def _render(self) -> None:
        """Sort, trim and write the current sample into the fixed row widgets."""
        rows = [data for data in self._latest if self._match(data)]
        rows.sort(key=lambda data: data[self._sort_attr], reverse=self._sort_desc)

        shown = rows[:_VISIBLE_ROWS]
        selected_row = None

        for index, process_row in enumerate(self._rows):
            list_row = process_row.get_parent()
            if index < len(shown):
                process_row.fill(shown[index])
                list_row.set_visible(True)
                if shown[index]["pid"] == self._selected_pid:
                    selected_row = list_row
            else:
                process_row.pid = None
                list_row.set_visible(False)

        # Keep the highlight on the same process as it moves up and down the
        # table, rather than on whatever now occupies that position.
        if selected_row is not None:
            if self.rows_box.get_selected_row() is not selected_row:
                self.rows_box.select_row(selected_row)
        elif self._selected_pid is not None:
            self.rows_box.unselect_all()

        total = len(rows)
        if total > _VISIBLE_ROWS:
            self.footer.set_text(
                f"Showing the top {_VISIBLE_ROWS} of {total} processes by "
                f"{next(c.title for c in COLUMNS if c.attr == self._sort_attr).lower()}"
            )
        else:
            self.footer.set_text(f"{total} processes")

    def update(self, snapshot: dict[str, Any], history: History) -> None:
        payload = snapshot.get("processes") or {}
        processes = payload.get("processes") or []
        counts = payload.get("counts") or {}

        if not payload.get("detailed"):
            # The first snapshot after this page opens is still the cheap one.
            self.tile_total.set_value(str(counts.get("total", 0)), "gathering details…")
            return

        self.tile_total.set_value(
            str(counts.get("total", 0)),
            f"{counts.get('sleeping', 0)} sleeping · {counts.get('zombie', 0)} zombie",
        )
        self.tile_running.set_value(str(counts.get("running", 0)))
        self.tile_threads.set_value(str(sum(item["threads"] for item in processes)))

        total_cpu = sum(item["cpu"] for item in processes)
        cores = payload.get("core_count") or 1
        self.tile_cpu.set_value(
            f"{total_cpu / cores:.1f}%",
            "first sample — settling" if not payload.get("primed") else f"across {cores} threads",
        )

        # Normalise the one field that can be None so sorting never trips.
        for item in processes:
            item["io_bps"] = item.get("io_bps") or 0.0

        self._latest = processes
        self._render()
