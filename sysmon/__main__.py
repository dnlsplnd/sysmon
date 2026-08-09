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
    return parser.parse_args(argv[1:])


def main() -> int:
    # Parsed here purely so --help works and bad input is rejected early; the
    # application also reads argv in do_command_line, which is what makes
    # --page work when an instance is already running.
    options = parse_args(sys.argv)

    from .ui.app import SysmonApplication

    application = SysmonApplication(
        interval=options.interval,
        capacity=options.history,
        start_page=options.page,
    )
    return application.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
