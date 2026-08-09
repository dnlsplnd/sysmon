"""Collector base class and the delta/rate bookkeeping they all share."""

from __future__ import annotations

from typing import Any


class RateTracker:
    """Turns monotonic counters into per-second rates.

    Kernel counters (bytes, sectors, jiffies, nanoseconds) are cumulative, so a
    rate needs the previous value and the previous timestamp. Keys are arbitrary
    hashables, which lets one tracker serve every interface or disk a collector
    sees. A counter that goes backwards -- a device that vanished and came back,
    or a 32-bit wrap -- yields 0.0 for that tick rather than a nonsense spike.
    """

    def __init__(self) -> None:
        self._previous: dict[Any, tuple[float, float]] = {}

    def update(self, key: Any, value: float, now: float) -> float | None:
        """Feed a counter reading. Returns units/second, or None on the first sample."""
        previous = self._previous.get(key)
        self._previous[key] = (value, now)
        if previous is None:
            return None
        prev_value, prev_time = previous
        elapsed = now - prev_time
        if elapsed <= 0:
            return None
        if value < prev_value:
            return 0.0
        return (value - prev_value) / elapsed

    def delta(self, key: Any, value: float, now: float) -> float | None:
        """Like :meth:`update` but returns the raw increment, not a rate."""
        previous = self._previous.get(key)
        self._previous[key] = (value, now)
        if previous is None:
            return None
        prev_value, _ = previous
        if value < prev_value:
            return 0.0
        return value - prev_value

    def forget(self, key: Any) -> None:
        self._previous.pop(key, None)

    def retain(self, keys: set[Any]) -> None:
        """Drop bookkeeping for keys that no longer exist, so the dict can't grow forever."""
        for stale in set(self._previous) - keys:
            del self._previous[stale]


class Collector:
    """One subsystem's sampler.

    ``sample()`` runs on the background thread and must never touch GTK. It
    returns a plain dict that the hub hands to the UI. Collectors are expected
    to degrade rather than raise: a missing sensor becomes ``None``, not an
    exception, because a monitor that dies when one file is unreadable is worse
    than one that shows a dash.
    """

    name = "collector"

    def __init__(self) -> None:
        self.available = True
        self.error: str | None = None

    def sample(self, now: float) -> dict[str, Any]:
        raise NotImplementedError

    def safe_sample(self, now: float) -> dict[str, Any]:
        """Run :meth:`sample`, converting an unexpected failure into an error payload."""
        try:
            data = self.sample(now)
            self.error = None
            return data
        except Exception as exc:  # noqa: BLE001 - a collector must not kill the app
            self.error = f"{type(exc).__name__}: {exc}"
            return {"error": self.error}
