"""Process collector: the cheap path, the detailed table, and per-process I/O."""

from __future__ import annotations

from types import SimpleNamespace

import psutil as real_psutil
import pytest

from sysmon.collectors import process as process_module
from sysmon.collectors.process import ProcessCollector


class FakeProc:
    """Stands in for a psutil.Process as returned by process_iter."""

    def __init__(self, **info):
        self._info = info

    @property
    def info(self):
        return self._info


class ExplodingProc:
    """A process that exits between being listed and being read."""

    @property
    def info(self):
        raise real_psutil.NoSuchProcess(999)


def make_proc(pid, name="bash", status="sleeping", rss=100 * 1024**2, cpu=0.0,
              threads=1, read_bytes=None, write_bytes=None, user="dnsk"):
    io = None
    if read_bytes is not None:
        io = SimpleNamespace(read_bytes=read_bytes, write_bytes=write_bytes)
    return FakeProc(
        pid=pid,
        name=name,
        username=user,
        status=status,
        memory_info=SimpleNamespace(rss=rss, vms=rss * 4),
        memory_percent=1.5,
        num_threads=threads,
        nice=0,
        create_time=1_700_000_000.0,
        cpu_percent=cpu,
        io_counters=io,
    )


def install(monkeypatch, kernel, processes):
    """Point the collector at a fixed process list."""
    holder = {"procs": processes}
    monkeypatch.setattr(
        process_module,
        "psutil",
        SimpleNamespace(
            process_iter=lambda attrs, ad_value=None: list(holder["procs"]),
            Process=real_psutil.Process,
            Error=real_psutil.Error,
            NoSuchProcess=real_psutil.NoSuchProcess,
            AccessDenied=real_psutil.AccessDenied,
        ),
    )
    kernel.patch(process_module)
    return holder


# ------------------------------------------------------------- cheap path


def test_the_table_is_off_until_a_page_asks_for_it(kernel, monkeypatch):
    """Building the full table is most of the sampling budget, so it stays off."""
    for pid in (1, 42, 1234):
        kernel.mkdir(f"/proc/{pid}")
    kernel.mkdir("/proc/self")
    kernel.write("/proc/stat", "cpu 0 0 0 0\n")
    install(monkeypatch, kernel, [])

    sample = ProcessCollector().sample(now=0.0)
    assert sample["detailed"] is False
    assert sample["processes"] == []
    # Only the numeric entries count, and only as a total.
    assert sample["counts"] == {"total": 3, "running": 0, "sleeping": 0, "stopped": 0, "zombie": 0}


def test_an_unreadable_proc_counts_zero_rather_than_raising(kernel, monkeypatch):
    (kernel.root / "proc").rmdir()
    install(monkeypatch, kernel, [])
    assert ProcessCollector().sample(now=0.0)["counts"]["total"] == 0


def test_core_count_is_reported_for_normalisation(kernel, monkeypatch):
    kernel.cpu_count = 16
    install(monkeypatch, kernel, [])
    assert ProcessCollector().sample(now=0.0)["core_count"] == 16


# ---------------------------------------------------------- detailed table


@pytest.fixture
def collector(kernel, monkeypatch):
    kernel.cpu_count = 8
    holder = install(monkeypatch, kernel, [
        make_proc(1, name="systemd", status="sleeping", user="root"),
        make_proc(1234, name="firefox", status="running", cpu=400.0, threads=120,
                  read_bytes=1_000_000, write_bytes=500_000),
        make_proc(5678, name="Xorg", status="sleeping", cpu=25.0),
    ])
    instance = ProcessCollector()
    instance.detailed = True
    instance.holder = holder
    return instance


def test_rows_carry_what_the_table_shows(collector):
    rows = {row["pid"]: row for row in collector.sample(now=0.0)["processes"]}
    assert rows[1234]["name"] == "firefox"
    assert rows[1234]["user"] == "dnsk"
    assert rows[1234]["status"] == "running"
    assert rows[1234]["threads"] == 120
    assert rows[1234]["rss"] == 100 * 1024**2
    assert rows[1234]["vms"] == 400 * 1024**2


def test_cpu_is_also_reported_normalised_against_all_cores(collector):
    """psutil reports a share of one core, so 400% is four busy cores of eight."""
    rows = {row["pid"]: row for row in collector.sample(now=0.0)["processes"]}
    assert rows[1234]["cpu"] == pytest.approx(400.0)
    assert rows[1234]["cpu_normalised"] == pytest.approx(50.0)


def test_status_counts(collector):
    counts = collector.sample(now=0.0)["counts"]
    assert counts["total"] == 3
    assert counts["running"] == 1
    assert counts["sleeping"] == 2


def test_an_unknown_status_still_counts_toward_the_total(kernel, monkeypatch):
    install(monkeypatch, kernel, [make_proc(1, status="idle")])
    instance = ProcessCollector()
    instance.detailed = True
    counts = instance.sample(now=0.0)["counts"]
    assert counts["total"] == 1
    assert counts["running"] == 0


def test_a_process_that_exits_mid_walk_is_skipped(kernel, monkeypatch):
    install(monkeypatch, kernel, [make_proc(1), ExplodingProc(), make_proc(2)])
    instance = ProcessCollector()
    instance.detailed = True
    assert [row["pid"] for row in instance.sample(now=0.0)["processes"]] == [1, 2]


def test_the_primed_flag_explains_the_first_zero_cpu_column(collector):
    """psutil's first cpu_percent for a process is always 0.0, with no baseline."""
    assert collector.sample(now=0.0)["primed"] is False
    assert collector.sample(now=1.0)["primed"] is True


# --------------------------------------------------------------------- I/O


def test_io_rates_need_a_second_sample(collector):
    rows = {row["pid"]: row for row in collector.sample(now=0.0)["processes"]}
    assert rows[1234]["read_bps"] is None
    assert rows[1234]["write_bps"] is None
    assert rows[1234]["io_bps"] == 0.0


def test_io_rates_come_from_the_counter_delta(collector):
    collector.sample(now=0.0)
    collector.holder["procs"] = [
        make_proc(1234, name="firefox", read_bytes=3_000_000, write_bytes=1_500_000),
    ]
    row = collector.sample(now=1.0)["processes"][0]
    assert row["read_bps"] == pytest.approx(2_000_000.0)
    assert row["write_bps"] == pytest.approx(1_000_000.0)
    assert row["io_bps"] == pytest.approx(3_000_000.0)


def test_a_process_without_io_counters_reports_none(collector):
    """Foreign processes raise AccessDenied, which psutil turns into None."""
    rows = {row["pid"]: row for row in collector.sample(now=0.0)["processes"]}
    assert rows[5678]["read_bps"] is None


def test_rate_history_is_dropped_when_a_process_exits(collector):
    """Processes die constantly; the bookkeeping must stay bounded."""
    collector.sample(now=0.0)
    collector.holder["procs"] = [make_proc(1, name="systemd")]
    collector.sample(now=1.0)

    # 1234 is gone, so its history should be too: a pid reused later must not
    # inherit a rate computed against the dead process's counters.
    assert collector._io.update((1234, "r"), 9_000_000, now=2.0) is None


# ----------------------------------------------------------------- actions


def test_terminate_reports_a_process_that_already_went_away(monkeypatch):
    def gone(pid):
        raise real_psutil.NoSuchProcess(pid)

    monkeypatch.setattr(process_module.psutil, "Process", gone, raising=False)
    assert ProcessCollector.terminate(999) == "process already exited"


def test_terminate_reports_a_process_we_do_not_own(monkeypatch):
    def denied(pid):
        raise real_psutil.AccessDenied(pid)

    monkeypatch.setattr(process_module.psutil, "Process", denied, raising=False)
    assert ProcessCollector.terminate(1) == "permission denied (process owned by another user)"


def test_terminate_sends_the_signal_the_caller_asked_for(monkeypatch):
    sent = []
    monkeypatch.setattr(
        process_module.psutil,
        "Process",
        lambda pid: SimpleNamespace(
            terminate=lambda: sent.append("SIGTERM"), kill=lambda: sent.append("SIGKILL")
        ),
        raising=False,
    )
    assert ProcessCollector.terminate(1234) is None
    assert ProcessCollector.terminate(1234, force=True) is None
    assert sent == ["SIGTERM", "SIGKILL"]
