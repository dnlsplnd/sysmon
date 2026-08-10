# sysmon

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

Requires `python3-gobject`, GTK 4, libadwaita, `pycairo` and `psutil` — all
present on a stock Fedora KDE/GNOME install. Running the tests additionally
needs `pytest`; nothing else does.

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

Anything that needs root — DIMM part numbers via `dmidecode`, SMART health via
`smartctl` — is behind an explicit button that goes through `pkexec`, never a
prompt at startup. CPU package power via RAPL is root-only on current kernels
and is simply absent rather than faked.

## Cost

Sampling all seven collectors costs about **35 ms per tick** on this box (Ryzen
1700X, ~480 processes), down from 183 ms in the first working version. A monitor
that perturbs what it measures is not much use, so:

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
the `io_ticks` delta, and the 65261 °C threshold being rejected as a sentinel.

The suite is checked by mutation: breaking de-duplication, reversing the
frequency preference, computing used memory from free, accepting implausible
thresholds, changing the sector size, and removing the guard on a counter going
backwards are each caught by a test that names the behaviour.

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
```

The threading contract is narrow on purpose: the worker thread only touches
collectors, and never GTK or the history buffers. Snapshots cross to the main
loop via `GLib.idle_add`, and history is written there, so widgets read the
buffers without locking.

Chart colours follow a palette validated for colour-vision deficiency in both
light and dark modes; series are assigned fixed slots so filtering one out never
repaints the others. Per-core load and similar magnitude encodings use a
single-hue ramp rather than categorical colours.
