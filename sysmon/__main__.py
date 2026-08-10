"""Entry point: ``python3 -m sysmon``."""

from __future__ import annotations

import argparse
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sysmon",
        description="A native GTK4 system monitor reading /proc, /sys and DRM fdinfo.",
    )
    parser.add_argument(
        "--page",
        default="Overview",
        help="page to open on startup (Overview, CPU, Memory, GPU, Disks, Network, Processes, Sensors)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="sampling interval in seconds (default: 1.0)",
    )
    parser.add_argument(
        "--history",
        type=int,
        default=300,
        help="samples of history to keep per series (default: 300)",
    )
    parser.add_argument(
        "--hidden",
        action="store_true",
        help="start in the tray without opening the window (used at login)",
    )
    return parser.parse_args(argv[1:])


def main() -> int:
    # Parsed here purely so --help works and bad input is rejected early; the
    # application also reads argv in do_command_line, which is what makes
    # --page work when an instance is already running.
    options = parse_args(sys.argv)

    from gi.repository import GLib

    from .ui.app import APP_ID, SysmonApplication

    # Under Wayland the compositor identifies the window by prgname, which
    # otherwise ends up as "__main__.py" for a `python3 -m` launch. Plasma
    # matches a window to its .desktop file by that name, so leaving it wrong
    # costs the taskbar icon and window grouping.
    GLib.set_prgname(APP_ID)

    application = SysmonApplication(
        interval=options.interval,
        capacity=options.history,
        start_page=options.page,
        start_hidden=options.hidden,
    )
    return application.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
