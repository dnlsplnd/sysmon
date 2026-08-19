# sysmon

[![tests](https://github.com/dnlsplnd/sysmon/actions/workflows/tests.yml/badge.svg)](https://github.com/dnlsplnd/sysmon/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A native GTK4/libadwaita system monitor. Everything is read straight from
`/proc`, `/sys` and DRM `fdinfo`; graphs are drawn with Cairo.

```
sysmon                  # installed; ./sysmon.sh runs a checkout in place
sysmon --page GPU       # open on a specific page
sysmon --interval 0.5   # sample twice a second
sysmon --history 600    # keep 10 minutes of history at 1 s
sysmon --hidden         # start in the tray, without a window
```

Only one instance runs at a time. Launching again raises the existing window,
and `--page` switches it rather than being ignored.

## Dependencies

All of these are distribution packages. There is nothing to `pip install`, no
virtualenv and no build step — the collectors read the kernel directly, so
`psutil` is the only third-party Python library involved at all.

| Needs | Why | Minimum |
|---|---|---|
| Python | | 3.11 |
| PyGObject | the GTK bindings | 3.42 |
| GTK | the toolkit | **4.12**, for `CssProvider.load_from_string` |
| libadwaita | window, dialogs and styling | **1.5**, for `Adw.AlertDialog` |
| pycairo | every graph is drawn through it | 1.20 |
| psutil | filesystems, sockets and the process table | 5.9 |

The two versions in bold are hard floors. libadwaita 1.5 is from spring 2024,
so a distribution older than that will install the packages below and still
fail on startup.

**Fedora**

```
sudo dnf install python3-gobject gtk4 libadwaita python3-cairo python3-psutil
```

**Debian / Ubuntu**

```
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 python3-psutil
```

**Arch**

```
sudo pacman -S python-gobject gtk4 libadwaita python-cairo python-psutil
```

On anything else, four things have to be present: the GTK 4 and libadwaita
libraries, their GObject-introspection typelibs, PyGObject, and pycairo. Some
distributions ship the typelibs separately from the libraries — that is what
`gir1.2-adw-1` is above, and openSUSE calls the same thing
`typelib-1_0-Adw-1` — which is the usual reason an install that looks complete
still cannot start.

Then check what you actually have:

```
python3 - <<'EOF'
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
import cairo, psutil, sys
print("python    ", sys.version.split()[0])
print("GTK       ", f"{Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}")
print("libadwaita", f"{Adw.get_major_version()}.{Adw.get_minor_version()}.{Adw.get_micro_version()}")
print("pycairo   ", cairo.version)
print("psutil    ", psutil.__version__)
EOF
```

Anything missing fails by name. `ValueError: Namespace Adw not available` is
the typelib rather than the library itself; `ModuleNotFoundError: No module
named 'gi'` is PyGObject.

The test suite additionally needs `pytest`, and nothing else does — it reads a
fake `/proc` and never imports the UI, so `pip install pytest psutil` is enough
to run it on a machine with no GTK at all. That is exactly what CI does.

## Installing

```
make install                    # into ~/.local, no root needed
make install PREFIX=/usr/local  # anywhere else; DESTDIR is honoured
make uninstall
```

This puts the package under `$PREFIX/lib/sysmon`, a `sysmon` launcher on the
path, the desktop entry, and the application icon into `hicolor`. Nothing in
the repository names an absolute path: the launcher's `PYTHONPATH` and the
desktop entry's `Exec` are substituted into the templates in `packaging/` at
install time, which is the only reason there is a build step at all.

The package goes outside `site-packages` deliberately. It is an application
rather than a library, and the launcher putting one directory on `PYTHONPATH`
is easier to reason about — and to remove — than a scattered install.

Right-clicking the launcher entry jumps straight to Processes, GPU or Sensors,
because `--page` reaches an instance that is already running.

```
make install-autostart          # start in the tray at login
make uninstall-autostart
```

The autostart entry passes `--hidden`, which goes to the tray without opening a
window, so history is already filled in the first time it is opened. If no tray
accepts the icon it presents the window after all, rather than starting
somewhere it could never be reached.

## System tray

While running, the monitor sits in the tray. Closing the window hides it there
instead of quitting — sampling continues, so the graphs are already filled in
when it comes back. A left click on the icon toggles the window, and the menu
offers *Show System Monitor* and *Quit*. Hovering shows current CPU, RAM and
GPU load, refreshed well below the sampling rate because each update costs a
signal plus a property read.

GTK 4 has no tray API, and libappindicator links against GTK 3, which cannot
share a process with GTK 4. The `StatusNotifierItem` protocol underneath it is
small, so `ui/tray.py` speaks it directly over Gio — the icon on
`org.kde.StatusNotifierItem`, and its menu as a second object on
`com.canonical.dbusmenu`, since the spec carries only a path to the menu.

The window closes to the tray **only** once a watcher has accepted the
registration. On a desktop with no tray the close button keeps quitting, rather
than hiding the window somewhere it could never be recovered from.

The host resolves the icon name against *its* icon theme, not ours, so a
checkout that was never installed would show a blank in the panel — the icon
exists only inside the package at that point. `IconThemePath` is the spec's
answer to exactly this, and is set to the bundled icon directory, so the tray
icon is right whether or not `make install` has been run.

## Pages

| Page | What it shows |
|---|---|
| **Overview** | Four gauges (CPU, memory, GPU, busiest disk), headline stats, and the four main graphs |
| **CPU** | Aggregate load, user/system/iowait split, per-thread heatmap, frequency, die temperature, context switches, interrupts, forks |
| **Memory** | Composition bar (apps / cache / buffers / free), full `meminfo` breakdown, swap, zram compression ratio, physical module info |
| **GPU** | Per-engine utilisation, VRAM in use, clocks, power draw, temperature, fan, and per-process VRAM attribution |
| **Disks** | Per-device throughput, IOPS, queue utilisation, service latency, temperature, SMART health, filesystem usage |
| **Network** | Per-interface rates and link utilisation, byte counters (session and since boot), wireless signal, socket list, bandwidth calculator |
| **Processes** | Sortable, filterable table with CPU, memory, threads and disk I/O; terminate/kill with confirmation |
| **Sensors** | Every hwmon rail on the machine — temperatures, fans, voltages, currents — grouped by chip |

`Alt+1`…`Alt+8` jump between pages. The sampling interval is in the menu.

Sidebar icon names are not portable between icon themes — there is no
`processor-symbolic` or `temperature-symbolic` on Breeze, and no
`monitor-symbolic` on Adwaita — and GTK draws a missing icon as a blank rather
than falling back. Each page therefore lists the names it knows and the first
one the installed theme actually has is used. The application's own icon is
bundled the same way, so it is correct in the window and the tray before it is
installed anywhere. A symbolic CPU icon is bundled
under `ui/icons/`, because Breeze ships only a full-colour one that looks wrong
beside a column of monochrome icons.

## How the numbers are obtained

Some of these are worth knowing, because the obvious source is the wrong one.

**CPU frequency** comes from `cpuinfo_avg_freq` where the kernel exposes it.
That file is derived from the APERF/MPERF counters, so it reports the frequency
the cores *actually averaged* over the interval. `scaling_cur_freq` reports the
P-state the governor last selected, which on this box is quantised to the three
steps its overclocked ladder offers (2.2 / 3.0 / 3.7 GHz) — so at partial load
the two can differ by a lot. The CPU page names the file it used, so the number
is never ambiguous.

**GPU utilisation and VRAM** come from DRM `fdinfo`, the same source
`intel_gpu_top` reads, so no root and no perf events are needed. Clients are
de-duplicated by `drm-client-id`: a process can hold one DRM client on several
descriptors, and counting per-descriptor would multiply both busy time and VRAM
by the number of duplicates. Busy time is normalised by each engine class's
capacity, and the headline figure is the busiest engine rather than the sum,
since engines run in parallel.

**GPU clock** falls back to the requested clock when `gt_act_freq_mhz` reads 0.
That file reads 0 whenever the sample lands while the GPU is power-gated (RC6),
which happens constantly even on a busy card; reporting it literally would show
"0 MHz" on a GPU doing real work. The page flags when it has fallen back.

**VRAM total is not reported.** The card's PCI BAR is a resizable aperture (8 GiB
here for a 6 GB card), not its capacity, so the page shows measured VRAM in use
and labels the aperture as what it is.

**Disk utilisation** is the delta of `io_ticks` — milliseconds during which the
queue was non-empty — over the wall-clock interval. Throughput uses the fixed
512-byte sector unit that `/proc/diskstats` always reports in, regardless of the
device's real block size.

**Memory "used"** is total minus available, matching `free(1)`, so reclaimable
page cache is not counted as used.

**Sensor thresholds** are ignored when they are not physical: this NVMe reports
`temp2_max` as 65261 °C to mean "no limit", and scaling a bar against that makes
every reading look like nothing.

**Sensor rails are reported literally**, including the zeroes. A power-gated
device answers 0 for rails it cannot currently measure — the Intel GPU's `in0`
voltage reads 0.000 V in RC6, the same gating that makes `gt_act_freq_mhz` read
0. The GPU page substitutes the requested clock there because a running GPU
cannot be at 0 Hz, but the Sensors page does not guess: it shows every hwmon rail
the kernel exposes, generically, and a voltage genuinely can be zero.

Anything that needs root — DIMM part numbers via `dmidecode`, SMART health via
`smartctl` — is behind an explicit button that goes through `pkexec`, never a
prompt at startup. CPU package power via RAPL is root-only on current kernels
and is simply absent rather than faked.

## Cost

A monitor that perturbs what it measures is not much use, so the sampling budget
is watched. Measured over 60 ticks at the default one-second interval on this box
(Ryzen 1700X, 16 threads, ~415 processes), idle and again against sixteen busy
loops. Idle was run twice; a range appears where the two disagreed:

> Measured 2026-08-10 — sysmon 1.0.0 at commit `c700358`, Linux 7.1.7 (Fedora
> 44), Python 3.14.6. Every figure here is specific to this hardware and kernel,
> and to the rails and DRM clients this box happens to have; treat them as the
> shape of the cost rather than as numbers to expect elsewhere. `make bench`
> reproduces them and prints its own provenance header; re-take them whenever a
> collector changes what it reads.

| Collector | Idle median | Idle p95 | Loaded median | Loaded p95 |
|---|---|---|---|---|
| Network | 4.6 ms | 4.9 ms | 9.1 ms | 12 ms |
| GPU | 4.4 ms | 23 ms | 6.5 ms | 38 ms |
| CPU | 1.4 ms | 1.5 ms | 1.0 ms | 1.8 ms |
| Disks | 1.1 ms | 14 ms | 0.9 ms | 13 ms |
| Processes *(page closed)* | 0.7 ms | 0.8 ms | 0.7 ms | 3.5 ms |
| Sensors | 0.3 ms | 26–42 ms | 0.2 ms | 45 ms |
| Memory | 0.3 ms | 0.3 ms | 0.2 ms | 0.2 ms |
| **whole tick** | **14 ms** | **62–73 ms** | **22 ms** | **93 ms** |

The whole tick is dearer than the sum of the medians because the collectors that
defer work do not defer it to the *same* tick, so most ticks carry somebody's
periodic spike.

Both columns are worth keeping, because a system monitor is most often looked at
precisely when the machine is busy. Contention lands almost entirely on the two
collectors that make the most kernel calls — the GPU collector's `/proc` walk for
DRM clients, and the network collector's per-interface sysfs reads. CPU, disks
and memory do not move at all: they cost a fixed handful of reads regardless of
what else is running.

The gap between median and p95 is design rather than noise. Several collectors
defer expensive work to every fourth or fifth tick, so most ticks skip it and the
occasional one pays for all of them at once. The single number this section used
to quote averaged that away, and said nothing about which page was open.

Sensors is the row worth reading twice: a one-millisecond median against a
hundred-millisecond tail. Two unrelated things cause that, and only one of them
is what the throttle was built for.

The first is the NVMe rails. Reading a drive's composite temperature is a command
round-trip to its controller and costs ~12 ms whether the last read was one
second ago or five, so throttling those three rails to every fifth tick is a
straight win — 7 ms per second of wall clock instead of 36.

Which rails get that treatment is decided twice over, because one decision is not
enough.

At startup each rail is timed, as the median of three probes rather than a single
reading. A single sample is at the mercy of whatever the machine was doing in
that instant, and a monitor started on a busy box used to throttle a
microsecond-cheap rail for the life of the process. The median makes the choice
identical run to run, idle or loaded, for about 27 ms more startup — nothing
beside building the window.

That still only measures what a rail *typically* costs, which is the wrong
question for one that is cheap almost always and dreadful occasionally: this
box's Wi-Fi temperature (`mt7921`) reads in 1 ms and, every few seconds, in
90–110 ms. It passes the startup check honestly and would then dominate the tail.
So the cost of every unthrottled read is watched as well, and a rail that blows
the threshold three times within twenty reads joins the throttled set. The Wi-Fi
rail gets there about eleven seconds in, which took the sensors 95th percentile
from ~90 ms to the 26–42 above, and its median contribution from 1.7 ms to
0.3 ms. Promotion is one way only: a rail that has shown it can stall has not
stopped being able to.

Opening the Processes page changes the picture entirely:

| | Idle | Loaded |
|---|---|---|
| the table alone | 79 ms | 196 ms |
| whole tick | 89 ms | 217 ms |

Six times the idle cheap path, and the one figure here large enough to be felt.
That is the whole reason the table is built only while that page is on screen.

The first working version cost 183 ms per tick unconditionally. What bought the
difference:

- The full process table is only built while the Processes page is on screen;
  otherwise a process count is one `readdir`.
- `io_counters` is fetched as part of the batch rather than per process, so
  psutil absorbs the `AccessDenied` that every foreign process raises.
- The `/proc` walk for DRM clients re-checks known GPU processes each tick and
  rediscovers the full table periodically.
- Rails that are slow to read — an NVMe composite temperature is a command
  round-trip to the drive controller, not a memory read — are timed at startup
  and polled less often.

## Tests

```
make test
```

The collectors parse kernel text formats, which is exactly the code that breaks
silently: a column that moves, a unit that changes, a file that starts reporting
`0`. So the suite gives each collector a fake `/proc` and `/sys` built per test
and reads that instead of the machine it runs on — which means it asserts real
numbers rather than "did not crash", and gives the same result on a box with no
Intel GPU, no zram and no `k10temp`.

Redirection works because every collector reaches the kernel through
`util.read_text`, `read_int` and `listdir`. Those are imported *by name*, so
`tests/conftest.py` rebinds them inside each collector's own namespace; the two
that also call `os` directly get a proxy with the same rerouting. Nothing under
test can reach the real kernel by accident.

What is pinned is mostly the reasoning in *How the numbers are obtained* above,
since that is where a plausible-looking wrong answer is easiest to produce —
`cpuinfo_avg_freq` beating `scaling_cur_freq` when both exist, DRM clients
de-duplicated by `drm-client-id`, the busiest engine rather than the sum, busy
time normalised by engine capacity, a gated clock falling back rather than
reporting 0 MHz, memory used as total minus *available*, disk utilisation from
the `io_ticks` delta, `/proc/stat` counters divided by the elapsed time rather
than reported per tick, the 65261 °C threshold being rejected as a sentinel, and
both halves of the sensor throttling decision — classification by the median of
several probes, and promotion of a rail that keeps blowing the threshold.

The suite is checked by mutation. Breaking de-duplication, reversing the
frequency preference, computing used memory from free, accepting implausible
thresholds, changing the sector size, removing the guard on a counter going
backwards, timing a rail once instead of three times, disabling promotion, and
letting the promotion window grow without bound are each caught by a test that
names the behaviour.

CI runs it on every push, across three Python versions, with nothing installed
but `pytest` and `psutil` — the UI is never imported, so no GTK is involved. It
then runs `make check` against the runner's own kernel, which has no `k10temp`,
no Intel GPU and virtualised disks: the one place the *degrade rather than
raise* contract is exercised against hardware nothing like this box.

## Layout

```
sysmon/
  collectors/     one module per subsystem; sample() returns a plain dict
  ui/
    widgets/      Cairo chart widgets (graph, sparkline, gauge, meter, heatmap)
    pages/        one module per page
    icons/        bundled icons, laid out as a real theme directory
    theme.py      colour roles, light and dark
  hub.py          sampling thread; history is written on the GTK main thread
  history.py      fixed-length series behind every graph
packaging/        launcher, desktop and autostart templates; Makefile fills them in
tests/            one module per collector; conftest.py builds the fake kernel
tools/bench.py    the Cost section above, reproducible: `make bench`
```

The threading contract is narrow on purpose: the worker thread only touches
collectors, and never GTK or the history buffers. Snapshots cross to the main
loop via `GLib.idle_add`, and history is written there, so widgets read the
buffers without locking.

Chart colours follow a palette validated for colour-vision deficiency in both
light and dark modes; series are assigned fixed slots so filtering one out never
repaints the others. Per-core load and similar magnitude encodings use a
single-hue ramp rather than categorical colours.

## License

MIT — see [LICENSE](LICENSE). GTK 4, libadwaita and PyGObject are LGPL and
`psutil` is BSD, so none of the dependencies constrain what you do with this.
