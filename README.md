# sysmon

A native GTK4/libadwaita system monitor. Everything is read straight from
`/proc`, `/sys` and DRM `fdinfo`; graphs are drawn with Cairo.

```
python3 -m sysmon                  # or ./sysmon.sh
python3 -m sysmon --page GPU       # open on a specific page
python3 -m sysmon --interval 0.5   # sample twice a second
python3 -m sysmon --history 600    # keep 10 minutes of history at 1 s
```

Only one instance runs at a time. Launching again raises the existing window,
and `--page` switches it rather than being ignored.

Requires `python3-gobject`, GTK 4, libadwaita, `pycairo` and `psutil` — all
present on a stock Fedora KDE/GNOME install.

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
one the installed theme actually has is used. A symbolic CPU icon is bundled
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

## Layout

```
sysmon/
  collectors/     one module per subsystem; sample() returns a plain dict
  ui/
    widgets/      Cairo chart widgets (graph, sparkline, gauge, meter, heatmap)
    pages/        one module per page
    theme.py      colour roles, light and dark
  hub.py          sampling thread; history is written on the GTK main thread
  history.py      fixed-length series behind every graph
```

The threading contract is narrow on purpose: the worker thread only touches
collectors, and never GTK or the history buffers. Snapshots cross to the main
loop via `GLib.idle_add`, and history is written there, so widgets read the
buffers without locking.

Chart colours follow a palette validated for colour-vision deficiency in both
light and dark modes; series are assigned fixed slots so filtering one out never
repaints the others. Per-core load and similar magnitude encodings use a
single-hue ramp rather than categorical colours.
