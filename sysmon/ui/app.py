"""The application shell: window, sidebar navigation and the update fan-out."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .pages import PAGE_CLASSES  # noqa: E402
from .tray import TrayIcon  # noqa: E402
from ..hub import Hub  # noqa: E402
from ..util import fmt_duration, fmt_pct, fmt_rate  # noqa: E402

APP_ID = "dev.dnsk.Sysmon"

# Sampling rates offered in the menu, in seconds.
_INTERVALS = [("0.5 s", 0.5), ("1 s", 1.0), ("2 s", 2.0), ("5 s", 5.0)]

_CSS = """
.sysmon-card { padding: 14px; }
.sysmon-tile { padding: 12px 14px; }
.numeric { font-feature-settings: "tnum" 1; }
.sysmon-statusbar { padding: 4px 12px; font-size: 0.85em; }
/* Header cells and body cells must share the same horizontal padding, or the
   column titles drift out of line with the values beneath them. */
.sysmon-table-header button { padding: 4px 6px; min-height: 0; border-radius: 0; }
.sysmon-cell { padding: 3px 6px; }
.sysmon-rows > row { padding: 0; min-height: 0; }
.sysmon-rows > row:nth-child(even) { background: alpha(currentColor, 0.035); }
"""


def register_bundled_icons() -> None:
    """Add this package's icons to the theme search path.

    Icons live under ``ui/icons/hicolor/...``; hicolor is always last in the
    inheritance chain, so bundled names resolve everywhere without overriding
    anything the user's theme provides. Used where no theme offers a symbolic
    icon we need -- Breeze's only CPU icon is a full-colour device icon, which
    looks wrong beside a column of monochrome ones.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return
    icons = Path(__file__).parent / "icons"
    if icons.is_dir():
        Gtk.IconTheme.get_for_display(display).add_search_path(str(icons))


def resolve_icon(candidates: tuple[str, ...], fallback: str) -> str:
    """Return the first candidate the installed icon theme actually has.

    GTK renders a missing icon as a blank or a broken-image glyph rather than
    falling back on its own, and the standard names differ between Adwaita and
    Breeze, so the choice has to be made against the live theme.
    """
    display = Gdk.Display.get_default()
    if display is None:
        return fallback
    theme = Gtk.IconTheme.get_for_display(display)
    for name in candidates:
        if theme.has_icon(name):
            return name
    # Last resort: a generic icon that every theme ships, so a row is never
    # left with an empty slot where an icon should be.
    return fallback if theme.has_icon(fallback) else "application-x-executable"


class SysmonWindow(Adw.ApplicationWindow):
    """One window, one sidebar, one page visible at a time."""

    def __init__(self, application: Adw.Application, hub: Hub, start_page: str = "Overview") -> None:
        super().__init__(application=application, title="System Monitor")
        self.hub = hub
        self.set_default_size(1280, 860)

        self.toasts = Adw.ToastOverlay()

        self.split = Adw.NavigationSplitView()
        self.split.set_min_sidebar_width(190)
        self.split.set_max_sidebar_width(230)

        # --- sidebar ----------------------------------------------------------
        self.sidebar_list = Gtk.ListBox()
        self.sidebar_list.add_css_class("navigation-sidebar")
        self.sidebar_list.connect("row-selected", self._on_row_selected)

        sidebar_scroll = Gtk.ScrolledWindow()
        sidebar_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sidebar_scroll.set_vexpand(True)
        sidebar_scroll.set_child(self.sidebar_list)

        sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar_box.append(sidebar_scroll)

        sidebar_toolbar = Adw.ToolbarView()
        sidebar_header = Adw.HeaderBar()
        sidebar_header.set_show_end_title_buttons(False)
        sidebar_toolbar.add_top_bar(sidebar_header)
        sidebar_toolbar.set_content(sidebar_box)
        self.split.set_sidebar(
            Adw.NavigationPage(child=sidebar_toolbar, title="System Monitor")
        )

        # --- content ----------------------------------------------------------
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)

        self.pages = []
        for index, page_class in enumerate(PAGE_CLASSES):
            page = page_class(hub)
            self.pages.append(page)
            self.stack.add_named(page, page_class.title)

            row = Adw.ActionRow(title=page_class.title)
            icon_name = resolve_icon(
                getattr(page_class, "icons", ()) or (page_class.icon,),
                page_class.icon,
            )
            row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
            row.set_activatable(True)
            setattr(row, "_page_index", index)
            self.sidebar_list.append(row)

            if hasattr(page, "toast_parent"):
                page.toast_parent = self.toasts

        content_toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title="Overview", subtitle="")
        self.header.set_title_widget(self.title_widget)
        self.header.pack_end(self._build_menu())
        content_toolbar.add_top_bar(self.header)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        content_box.append(self.stack)
        self.stack.set_vexpand(True)

        self.statusbar = Gtk.Label(label="", xalign=0)
        self.statusbar.add_css_class("dim-label")
        self.statusbar.add_css_class("sysmon-statusbar")
        self.statusbar.add_css_class("numeric")
        content_box.append(Gtk.Separator())
        content_box.append(self.statusbar)

        content_toolbar.set_content(content_box)
        self.split.set_content(Adw.NavigationPage(child=content_toolbar, title="Overview"))

        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        start_index = next(
            (
                index
                for index, page in enumerate(self.pages)
                if page.title.lower() == start_page.lower()
            ),
            0,
        )
        self.sidebar_list.select_row(self.sidebar_list.get_row_at_index(start_index))
        hub.subscribe(self._on_snapshot)

        self._install_shortcuts()

    # ------------------------------------------------------------------- menu

    def _build_menu(self) -> Gtk.MenuButton:
        menu = Gio.Menu()

        rates = Gio.Menu()
        for label, seconds in _INTERVALS:
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value(
                "win.interval", GLib.Variant.new_double(seconds)
            )
            rates.append_item(item)
        menu.append_section("Sampling interval", rates)

        other = Gio.Menu()
        other.append("Reset network session counters", "win.reset-network")
        menu.append_section(None, other)

        button = Gtk.MenuButton(icon_name="open-menu-symbolic")
        button.set_menu_model(menu)

        interval_action = Gio.SimpleAction.new_stateful(
            "interval",
            GLib.VariantType.new("d"),
            GLib.Variant.new_double(self.hub.interval),
        )
        interval_action.connect("activate", self._on_interval)
        self.add_action(interval_action)

        reset_action = Gio.SimpleAction.new("reset-network", None)
        reset_action.connect(
            "activate",
            lambda *_: (
                self.hub.network.reset_session(),
                self.toasts.add_toast(Adw.Toast(title="Network session counters reset")),
            ),
        )
        self.add_action(reset_action)
        return button

    def _on_interval(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        seconds = value.get_double()
        self.hub.set_interval(seconds)
        action.set_state(value)
        self.toasts.add_toast(Adw.Toast(title=f"Sampling every {seconds:g} s"))

    def _install_shortcuts(self) -> None:
        """Alt+1..8 jumps straight to a page."""
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.GLOBAL)
        for index in range(len(self.pages)):
            shortcut = Gtk.Shortcut.new(
                Gtk.ShortcutTrigger.parse_string(f"<Alt>{index + 1}"),
                Gtk.CallbackAction.new(
                    lambda _widget, _args, position=index: self._select(position)
                ),
            )
            controller.add_shortcut(shortcut)
        self.add_controller(controller)

    def _select(self, index: int) -> bool:
        row = self.sidebar_list.get_row_at_index(index)
        if row is not None:
            self.sidebar_list.select_row(row)
        return True

    def select_page(self, title: str) -> None:
        """Switch to a page by name, ignoring case. Unknown names are ignored."""
        for index, page in enumerate(self.pages):
            if page.title.lower() == title.lower():
                self._select(index)
                return

    # -------------------------------------------------------------- navigation

    def _on_row_selected(self, _listbox, row) -> None:
        if row is None:
            return
        index = getattr(row, "_page_index", 0)
        page = self.pages[index]
        self.stack.set_visible_child(page)
        self.title_widget.set_title(page.title)
        for position, other in enumerate(self.pages):
            other.set_active(position == index)

    # ------------------------------------------------------------ update loop

    def _on_snapshot(self, snapshot: dict[str, Any]) -> None:
        # Sitting in the tray, nothing on screen is worth redrawing; the
        # history buffers keep filling either way, so the graphs are complete
        # the moment the window comes back.
        if not self.get_visible():
            return

        visible = self.stack.get_visible_child()
        for page in self.pages:
            # Only the visible page is redrawn; the others catch up from the
            # shared history the moment they are selected.
            if page is visible:
                page.update(snapshot, self.hub.history)

        self._update_status(snapshot)

    def _update_status(self, snapshot: dict[str, Any]) -> None:
        cpu = snapshot.get("cpu") or {}
        memory = snapshot.get("memory") or {}
        totals = (snapshot.get("network") or {}).get("totals") or {}
        meta = snapshot.get("meta") or {}

        parts = [
            f"CPU {fmt_pct(cpu.get('usage'))}",
            f"RAM {fmt_pct(memory.get('percent'))}",
            f"↓ {fmt_rate(totals.get('rx_bps'))}",
            f"↑ {fmt_rate(totals.get('tx_bps'))}",
            f"up {fmt_duration(cpu.get('uptime'))}",
            f"sampled in {meta.get('sample_ms', 0):.0f} ms every {meta.get('interval', 1):g} s",
        ]
        errors = [
            name
            for name in ("cpu", "memory", "disk", "network", "gpu", "sensors", "processes")
            if (snapshot.get(name) or {}).get("error")
        ]
        if errors:
            parts.append(f"collector errors: {', '.join(errors)}")
        self.statusbar.set_text("   ·   ".join(parts))


class SysmonApplication(Adw.Application):
    def __init__(
        self,
        interval: float = 1.0,
        capacity: int = 300,
        start_page: str = "Overview",
    ) -> None:
        # HANDLES_COMMAND_LINE so a second launch reaches the running instance
        # instead of being dropped. Without it GApplication forwards a bare
        # "activate" and any --page on that command line is silently lost.
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_COMMAND_LINE,
        )
        self.hub = Hub(interval=interval, capacity=capacity)
        self.start_page = start_page
        self.window: SysmonWindow | None = None
        self.tray: TrayIcon | None = None
        self._tooltip_due = 0.0

    def do_command_line(self, command_line: Gio.ApplicationCommandLine) -> int:
        """Handle argv, whether we are the first instance or a later one."""
        argv = command_line.get_arguments()
        page = None
        for index, argument in enumerate(argv):
            if argument == "--page" and index + 1 < len(argv):
                page = argv[index + 1]
            elif argument.startswith("--page="):
                page = argument.split("=", 1)[1]
        if page:
            self.start_page = page

        self.activate()

        # A second launch should bring the running window forward and switch it
        # to the requested page rather than doing nothing.
        if self.window is not None and page:
            self.window.select_page(page)
        return 0

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        register_bundled_icons()
        provider = Gtk.CssProvider()
        provider.load_from_string(_CSS)
        Gtk.StyleContext.add_provider_for_display(
            Gio.Application.get_default() and self._display(), provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

    @staticmethod
    def _display():
        from gi.repository import Gdk

        return Gdk.Display.get_default()

    def do_activate(self) -> None:
        if self.window is None:
            self.window = SysmonWindow(self, self.hub, start_page=self.start_page)
            self.hub.start()
            # Redraw every graph when the light/dark preference flips, so the
            # selected dark steps take effect immediately.
            Adw.StyleManager.get_default().connect(
                "notify::dark", lambda *_: self._redraw()
            )
            self._start_tray()
        self.window.present()

    def _redraw(self) -> None:
        if self.window is not None:
            self.window.queue_draw()

    # ------------------------------------------------------------------- tray

    def _start_tray(self) -> None:
        """Publish the tray icon, and close to it if a tray took the icon."""
        tray = TrayIcon(
            icon_name=resolve_icon(
                ("utilities-system-monitor", "org.kde.plasma-systemmonitor"),
                "utilities-system-monitor",
            ),
            title="System Monitor",
            on_activate=self._toggle_window,
            on_show=self._show_window,
            on_quit=self._quit_from_tray,
        )
        if not tray.start():
            return
        self.tray = tray
        # Only intercept the close button once there is somewhere to close to.
        # The handler re-checks, because the watcher accepts the registration
        # asynchronously and may yet refuse it.
        if self.window is not None:
            self.window.connect("close-request", self._on_close_request)
        self.hub.subscribe(self._on_tray_snapshot)

    def _on_close_request(self, window: SysmonWindow) -> bool:
        """Hide to the tray instead of quitting. True stops the destroy."""
        if self.tray is None or not self.tray.available:
            return False
        window.set_visible(False)
        return True

    def _toggle_window(self) -> None:
        if self.window is None:
            return
        if self.window.get_visible():
            self.window.set_visible(False)
        else:
            self._show_window()

    def _show_window(self) -> None:
        if self.window is not None:
            self.window.set_visible(True)
            self.window.present()

    def _quit_from_tray(self) -> None:
        if self.tray is not None:
            self.tray.stop()
        self.quit()

    def _on_tray_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Keep the hover text current, but well below the sampling rate.

        Every update is a signal plus the host reading the property back, so
        this is deliberately much slower than the graphs.
        """
        if self.tray is None or not self.tray.available:
            return
        now = GLib.get_monotonic_time() / 1e6
        if now < self._tooltip_due:
            return
        self._tooltip_due = now + 5.0

        cpu = (snapshot.get("cpu") or {}).get("usage")
        memory = (snapshot.get("memory") or {}).get("percent")
        parts = [f"CPU {fmt_pct(cpu)}", f"RAM {fmt_pct(memory)}"]

        # "busy" is per card, and is already the busiest engine on it rather
        # than a sum, so the busiest card is the one figure worth showing.
        cards = (snapshot.get("gpu") or {}).get("cards") or []
        busy = [card["busy"] for card in cards if card.get("busy") is not None]
        if busy:
            parts.append(f"GPU {fmt_pct(max(busy))}")
        self.tray.set_tooltip("   ·   ".join(parts))

    def do_shutdown(self) -> None:
        if self.tray is not None:
            self.tray.stop()
        self.hub.stop()
        Adw.Application.do_shutdown(self)


def main(argv: list[str] | None = None) -> int:
    import sys

    application = SysmonApplication()
    return application.run(argv if argv is not None else sys.argv)
