"""System tray presence, spoken as StatusNotifierItem over D-Bus.

There is no tray API in GTK4, and the usual shortcut -- libappindicator --
links against GTK 3, which cannot share a process with GTK 4. The protocol
itself is small, though, so this talks to the watcher directly through Gio and
keeps the process on a single GTK major.

Two interfaces are needed. ``org.kde.StatusNotifierItem`` is the icon; the
context menu is a second object speaking ``com.canonical.dbusmenu``, because
the SNI spec carries only a path to a menu and not the menu itself.
"""

from __future__ import annotations

import os
from typing import Callable

from gi.repository import Gio, GLib

WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

_ITEM_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="WindowId" type="i" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconThemePath" type="s" access="read"/>
    <property name="AttentionIconName" type="s" access="read"/>
    <property name="OverlayIconName" type="s" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
    <signal name="NewTitle"/>
    <signal name="NewIcon"/>
    <signal name="NewAttentionIcon"/>
    <signal name="NewOverlayIcon"/>
    <signal name="NewToolTip"/>
    <signal name="NewStatus">
      <arg name="status" type="s"/>
    </signal>
  </interface>
</node>
"""

_MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="GetProperty">
      <arg name="id" type="i" direction="in"/>
      <arg name="name" type="s" direction="in"/>
      <arg name="value" type="v" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <signal name="ItemsPropertiesUpdated">
      <arg name="updatedProps" type="a(ia{sv})"/>
      <arg name="removedProps" type="a(ias)"/>
    </signal>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""

# Menu item ids. 0 is the root the spec reserves for the menu itself.
_ID_SHOW = 1
_ID_SEPARATOR = 2
_ID_QUIT = 3


class TrayIcon:
    """A tray icon backed by the StatusNotifier protocol.

    ``available`` reports whether a watcher accepted the registration. The
    caller must check it before making the window close to the tray -- with no
    tray to restore from, that would leave no way to get the window back.
    """

    def __init__(
        self,
        icon_name: str,
        title: str,
        on_activate: Callable[[], None],
        on_show: Callable[[], None],
        on_quit: Callable[[], None],
        icon_theme_path: str = "",
    ) -> None:
        self.icon_name = icon_name
        self.icon_theme_path = icon_theme_path
        self.title = title
        self._on_activate = on_activate
        self._on_show = on_show
        self._on_quit = on_quit

        self.available = False
        self._tooltip = ""
        self._revision = 1
        self._connection: Gio.DBusConnection | None = None
        self._name_id = 0
        self._item_reg = 0
        self._menu_reg = 0

        self._bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    # ------------------------------------------------------------------ set-up

    def start(self) -> bool:
        """Publish the icon. Returns True once a watcher has accepted it."""
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return False

        # No watcher means nothing is drawing a tray -- bail out before
        # claiming a bus name we would only have to drop again.
        if not self._watcher_present():
            return False

        try:
            item_info = Gio.DBusNodeInfo.new_for_xml(_ITEM_XML).interfaces[0]
            menu_info = Gio.DBusNodeInfo.new_for_xml(_MENU_XML).interfaces[0]
            self._item_reg = self._connection.register_object(
                ITEM_PATH, item_info, self._item_method, self._item_property, None
            )
            self._menu_reg = self._connection.register_object(
                MENU_PATH, menu_info, self._menu_method, self._menu_property, None
            )
        except GLib.Error:
            return False

        self._name_id = Gio.bus_own_name_on_connection(
            self._connection,
            self._bus_name,
            Gio.BusNameOwnerFlags.NONE,
            self._on_name_acquired,
            None,
        )
        return True

    def _watcher_present(self) -> bool:
        assert self._connection is not None
        try:
            reply = self._connection.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "NameHasOwner",
                GLib.Variant("(s)", (WATCHER_NAME,)),
                GLib.VariantType.new("(b)"),
                Gio.DBusCallFlags.NONE,
                2000,
                None,
            )
        except GLib.Error:
            return False
        return bool(reply.unpack()[0])

    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        """Register with the watcher only once the name is really ours.

        Registering earlier is a race: the watcher resolves the name we hand it
        immediately, and would find no owner.
        """
        try:
            connection.call_sync(
                WATCHER_NAME,
                WATCHER_PATH,
                WATCHER_NAME,
                "RegisterStatusNotifierItem",
                GLib.Variant("(s)", (name,)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
            self.available = True
        except GLib.Error:
            self.available = False

    def stop(self) -> None:
        if self._connection is None:
            return
        for registration in (self._item_reg, self._menu_reg):
            if registration:
                self._connection.unregister_object(registration)
        if self._name_id:
            Gio.bus_unown_name(self._name_id)
        self._item_reg = self._menu_reg = self._name_id = 0
        self.available = False

    # --------------------------------------------------------------- item side

    def set_tooltip(self, text: str) -> None:
        """Replace the hover text and tell the host to re-read it."""
        if text == self._tooltip or self._connection is None:
            return
        self._tooltip = text
        try:
            self._connection.emit_signal(
                None, ITEM_PATH, "org.kde.StatusNotifierItem", "NewToolTip", None
            )
        except GLib.Error:
            pass

    def _item_property(
        self, _connection, _sender, _path, _interface, name: str
    ) -> GLib.Variant | None:
        if name == "Category":
            return GLib.Variant("s", "SystemServices")
        if name == "Id":
            return GLib.Variant("s", "sysmon")
        if name == "Title":
            return GLib.Variant("s", self.title)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "WindowId":
            return GLib.Variant("i", 0)
        if name == "IconName":
            return GLib.Variant("s", self.icon_name)
        if name == "IconThemePath":
            # The host resolves IconName against its own icon theme, not ours,
            # so an icon that is only bundled with the package -- a checkout
            # that was never installed -- would be a blank in the panel. This
            # is the spec's way to point the host at it.
            return GLib.Variant("s", self.icon_theme_path)
        if name in ("AttentionIconName", "OverlayIconName"):
            return GLib.Variant("s", "")
        if name == "ToolTip":
            # (icon name, pixmaps, title, body) -- Plasma renders title bold
            # above body, so the live numbers go in the body.
            return GLib.Variant(
                "(sa(iiay)ss)", (self.icon_name, [], self.title, self._tooltip)
            )
        if name == "ItemIsMenu":
            # False, so a left click is delivered as Activate rather than
            # being swallowed to open the menu.
            return GLib.Variant("b", False)
        if name == "Menu":
            return GLib.Variant("o", MENU_PATH)
        return None

    def _item_method(
        self, _connection, _sender, _path, _interface, method, _params, invocation
    ) -> None:
        if method == "Activate":
            self._on_activate()
        elif method == "SecondaryActivate":
            self._on_show()
        # ContextMenu and Scroll need no action: the host opens the menu from
        # the Menu property itself, and there is nothing to scroll.
        invocation.return_value(None)

    # --------------------------------------------------------------- menu side

    def _menu_items(self) -> list[tuple[int, dict[str, GLib.Variant]]]:
        return [
            (
                _ID_SHOW,
                {
                    "label": GLib.Variant("s", "Show System Monitor"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True),
                },
            ),
            (
                _ID_SEPARATOR,
                {
                    "type": GLib.Variant("s", "separator"),
                    "enabled": GLib.Variant("b", False),
                    "visible": GLib.Variant("b", True),
                },
            ),
            (
                _ID_QUIT,
                {
                    "label": GLib.Variant("s", "Quit"),
                    "enabled": GLib.Variant("b", True),
                    "visible": GLib.Variant("b", True),
                },
            ),
        ]

    def _menu_property(
        self, _connection, _sender, _path, _interface, name: str
    ) -> GLib.Variant | None:
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _menu_method(
        self, _connection, _sender, _path, _interface, method, params, invocation
    ) -> None:
        if method == "GetLayout":
            children = [
                GLib.Variant("(ia{sv}av)", (item_id, props, []))
                for item_id, props in self._menu_items()
            ]
            invocation.return_value(
                GLib.Variant(
                    "(u(ia{sv}av))",
                    (self._revision, (0, {"children-display": GLib.Variant("s", "submenu")}, children)),
                )
            )
        elif method == "GetGroupProperties":
            wanted = set(params.unpack()[0])
            invocation.return_value(
                GLib.Variant(
                    "(a(ia{sv}))",
                    ([
                        (item_id, props)
                        for item_id, props in self._menu_items()
                        if not wanted or item_id in wanted
                    ],),
                )
            )
        elif method == "GetProperty":
            item_id, name = params.unpack()[0], params.unpack()[1]
            for candidate, props in self._menu_items():
                if candidate == item_id and name in props:
                    invocation.return_value(GLib.Variant("(v)", (props[name],)))
                    return
            invocation.return_value(GLib.Variant("(v)", (GLib.Variant("s", ""),)))
        elif method == "Event":
            item_id, event_id = params.unpack()[0], params.unpack()[1]
            if event_id == "clicked":
                if item_id == _ID_SHOW:
                    self._on_show()
                elif item_id == _ID_QUIT:
                    self._on_quit()
            invocation.return_value(None)
        elif method == "AboutToShow":
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:
            invocation.return_value(None)
