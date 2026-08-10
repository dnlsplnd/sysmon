"""RateTracker and the collector error contract.

Every rate the application shows is produced here, so the edge cases -- first
sighting, a counter that goes backwards, a zero-length interval -- are the ones
that decide whether a graph shows a spike that never happened.
"""

from __future__ import annotations

from sysmon.collectors.base import Collector, RateTracker


# ----------------------------------------------------------------- rates


def test_first_sample_has_no_rate():
    """Nothing to subtract from yet, so the caller must get None, not zero."""
    tracker = RateTracker()
    assert tracker.update("rx", 1000, now=10.0) is None


def test_rate_is_the_delta_over_elapsed_time():
    tracker = RateTracker()
    tracker.update("rx", 1000, now=10.0)
    assert tracker.update("rx", 3000, now=12.0) == 1000.0


def test_a_counter_going_backwards_yields_zero_not_a_spike():
    """A device that vanished and came back, or a 32-bit wrap."""
    tracker = RateTracker()
    tracker.update("rx", 5000, now=10.0)
    assert tracker.update("rx", 10, now=11.0) == 0.0


def test_a_zero_length_interval_has_no_rate():
    tracker = RateTracker()
    tracker.update("rx", 1000, now=10.0)
    assert tracker.update("rx", 2000, now=10.0) is None


def test_time_going_backwards_has_no_rate():
    tracker = RateTracker()
    tracker.update("rx", 1000, now=10.0)
    assert tracker.update("rx", 2000, now=9.0) is None


def test_keys_are_independent():
    tracker = RateTracker()
    tracker.update(("eth0", "rx"), 100, now=0.0)
    tracker.update(("eth0", "tx"), 500, now=0.0)
    assert tracker.update(("eth0", "rx"), 200, now=1.0) == 100.0
    assert tracker.update(("eth0", "tx"), 900, now=1.0) == 400.0


def test_delta_returns_the_raw_increment():
    tracker = RateTracker()
    tracker.delta("busy", 1000, now=0.0)
    # Two seconds elapsed, but delta must not divide by it.
    assert tracker.delta("busy", 1500, now=2.0) == 500


def test_delta_also_refuses_to_go_backwards():
    tracker = RateTracker()
    tracker.delta("busy", 1000, now=0.0)
    assert tracker.delta("busy", 10, now=1.0) == 0.0


def test_forget_drops_one_key():
    tracker = RateTracker()
    tracker.update("rx", 100, now=0.0)
    tracker.forget("rx")
    assert tracker.update("rx", 200, now=1.0) is None


def test_retain_bounds_the_bookkeeping():
    """Processes and devices come and go; the dict must not grow forever."""
    tracker = RateTracker()
    for pid in range(5):
        tracker.update((pid, "r"), 100, now=0.0)
    tracker.retain({(1, "r"), (3, "r")})
    assert tracker.update((1, "r"), 200, now=1.0) == 100.0
    assert tracker.update((4, "r"), 200, now=1.0) is None


# ------------------------------------------------------- collector contract


def test_safe_sample_turns_a_crash_into_a_payload():
    """A collector that raises must not take the application down with it."""

    class Broken(Collector):
        def sample(self, now):
            raise ValueError("sensor is on fire")

    collector = Broken()
    result = collector.safe_sample(now=1.0)
    assert result == {"error": "ValueError: sensor is on fire"}
    assert collector.error == "ValueError: sensor is on fire"


def test_safe_sample_clears_a_previous_error_on_recovery():
    class Flaky(Collector):
        def __init__(self):
            super().__init__()
            self.fail = True

        def sample(self, now):
            if self.fail:
                raise OSError("gone")
            return {"ok": True}

    collector = Flaky()
    collector.safe_sample(now=1.0)
    assert collector.error is not None

    collector.fail = False
    assert collector.safe_sample(now=2.0) == {"ok": True}
    assert collector.error is None
