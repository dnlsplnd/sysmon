# Installation.
#
# The default prefix is ~/.local, so `make install` needs no root and lands in
# directories the XDG desktop-entry and icon lookups already search. A system
# install is `sudo make install PREFIX=/usr/local`, and DESTDIR is honoured for
# staged package builds.
#
# There is no build step: the desktop entry, autostart entry and launcher are
# generated at install time purely so the prefix is substituted into them
# rather than hardcoded in the repository.

PREFIX  ?= $(HOME)/.local
DESTDIR ?=
PYTHON  ?= python3

APP_ID := dev.dnsk.Sysmon

BINDIR  := $(PREFIX)/bin
LIBDIR  := $(PREFIX)/lib/sysmon
DATADIR := $(PREFIX)/share
APPDIR  := $(DATADIR)/applications
ICONDIR := $(DATADIR)/icons/hicolor/scalable/apps

# Autostart is per-user even for a system-wide install, so it is not under
# PREFIX and is its own target rather than part of `install`.
AUTOSTARTDIR ?= $(HOME)/.config/autostart

ICON := sysmon/ui/icons/hicolor/scalable/apps/$(APP_ID).svg

.PHONY: help install uninstall install-autostart uninstall-autostart \
        update-caches run test check

help:
	@echo 'make install              install to $(PREFIX)'
	@echo 'make install PREFIX=/usr  install elsewhere (DESTDIR is honoured)'
	@echo 'make uninstall           remove it again'
	@echo 'make install-autostart   start in the tray at login'
	@echo 'make uninstall-autostart stop doing that'
	@echo 'make run                 run from this checkout, without installing'
	@echo 'make test                run the test suite'
	@echo 'make check               byte-compile and sample every collector once'

install:
	@echo '  package   -> $(DESTDIR)$(LIBDIR)/sysmon'
	@find sysmon -name '__pycache__' -prune -o -type f -print | while read -r f; do \
		install -Dm644 "$$f" "$(DESTDIR)$(LIBDIR)/$$f" || exit 1; \
	done
	@echo '  launcher  -> $(DESTDIR)$(BINDIR)/sysmon'
	@install -d "$(DESTDIR)$(BINDIR)"
	@sed -e 's|@LIBDIR@|$(LIBDIR)|g' -e 's|@PYTHON@|$(PYTHON)|g' \
		packaging/sysmon.in > "$(DESTDIR)$(BINDIR)/sysmon"
	@chmod 755 "$(DESTDIR)$(BINDIR)/sysmon"
	@echo '  desktop   -> $(DESTDIR)$(APPDIR)/$(APP_ID).desktop'
	@install -d "$(DESTDIR)$(APPDIR)"
	@sed -e 's|@BINDIR@|$(BINDIR)|g' packaging/$(APP_ID).desktop.in \
		> "$(DESTDIR)$(APPDIR)/$(APP_ID).desktop"
	@chmod 644 "$(DESTDIR)$(APPDIR)/$(APP_ID).desktop"
	@echo '  icon      -> $(DESTDIR)$(ICONDIR)/$(APP_ID).svg'
	@install -Dm644 $(ICON) "$(DESTDIR)$(ICONDIR)/$(APP_ID).svg"
	@$(MAKE) --no-print-directory update-caches
	@case ":$$PATH:" in \
		*":$(BINDIR):"*) ;; \
		*) echo; echo "note: $(BINDIR) is not on your PATH, so the 'sysmon'"; \
		   echo "      command will not be found -- the launcher entry still works."; ;; \
	esac
	@echo
	@echo 'Installed. Run "sysmon", or find "System Monitor" in the launcher.'

uninstall:
	@rm -rf "$(DESTDIR)$(LIBDIR)"
	@rm -f "$(DESTDIR)$(BINDIR)/sysmon"
	@rm -f "$(DESTDIR)$(APPDIR)/$(APP_ID).desktop"
	@rm -f "$(DESTDIR)$(ICONDIR)/$(APP_ID).svg"
	@$(MAKE) --no-print-directory update-caches
	@if [ -e "$(DESTDIR)$(AUTOSTARTDIR)/$(APP_ID).desktop" ]; then \
		echo 'note: the autostart entry is still there; "make uninstall-autostart" removes it.'; \
	fi
	@echo 'Uninstalled.'

install-autostart:
	@install -d "$(DESTDIR)$(AUTOSTARTDIR)"
	@sed -e 's|@BINDIR@|$(BINDIR)|g' packaging/$(APP_ID)-autostart.desktop.in \
		> "$(DESTDIR)$(AUTOSTARTDIR)/$(APP_ID).desktop"
	@chmod 644 "$(DESTDIR)$(AUTOSTARTDIR)/$(APP_ID).desktop"
	@echo 'Will start in the tray at the next login.'

uninstall-autostart:
	@rm -f "$(DESTDIR)$(AUTOSTARTDIR)/$(APP_ID).desktop"
	@echo 'Will no longer start at login.'

# Both caches are optional: a missing entry only costs a stale menu until the
# desktop rescans, so nothing here is allowed to fail the install.
update-caches:
	@if command -v update-desktop-database >/dev/null 2>&1; then \
		update-desktop-database -q "$(DESTDIR)$(APPDIR)" 2>/dev/null || true; \
	fi
	@if command -v gtk4-update-icon-cache >/dev/null 2>&1; then \
		gtk4-update-icon-cache -qtf "$(DESTDIR)$(DATADIR)/icons/hicolor" 2>/dev/null || true; \
	elif command -v gtk-update-icon-cache >/dev/null 2>&1; then \
		gtk-update-icon-cache -qtf "$(DESTDIR)$(DATADIR)/icons/hicolor" 2>/dev/null || true; \
	fi

run:
	@$(PYTHON) -m sysmon

# The suite reads a fake /proc and /sys, so it neither needs this machine to
# have any particular hardware nor notices what it does have.
test:
	@$(PYTHON) -m pytest

# Complements `test`: this one samples the real kernel, so it catches a format
# this box actually has that the fixtures do not cover.
check:
	@$(PYTHON) -m compileall -q sysmon
	@$(PYTHON) -c 'import time; from sysmon.collectors import *; \
		cols = [CpuCollector(), MemoryCollector(), GpuCollector(), DiskCollector(), \
		        NetworkCollector(), ProcessCollector(), SensorCollector()]; \
		[c.sample(time.monotonic()) for c in cols]; \
		print("collectors ok:", ", ".join(type(c).__name__ for c in cols))'
