"""Fixed-length history buffers backing the time-series graphs."""

from __future__ import annotations

from collections import deque
from typing import Iterable


class Series:
    """A single named channel of history -- one line on a graph.

    Values may be ``None`` for ticks where the metric was unavailable (the very
    first sample of a rate, a sensor that dropped out). Graph widgets treat a
    ``None`` as a gap rather than plotting it as zero, which would invent a
    dip that never happened.
    """

    __slots__ = ("name", "values", "capacity")

    def __init__(self, name: str, capacity: int) -> None:
        self.name = name
        self.capacity = capacity
        self.values: deque[float | None] = deque(maxlen=capacity)

    def push(self, value: float | None) -> None:
        self.values.append(value)

    def latest(self) -> float | None:
        return self.values[-1] if self.values else None

    def finite(self) -> list[float]:
        return [value for value in self.values if value is not None]

    def maximum(self, default: float = 0.0) -> float:
        finite = self.finite()
        return max(finite) if finite else default

    def average(self) -> float | None:
        finite = self.finite()
        return sum(finite) / len(finite) if finite else None

    def __len__(self) -> int:
        return len(self.values)


class History:
    """Named series sharing one capacity, so every graph scrolls in lockstep."""

    def __init__(self, capacity: int = 300) -> None:
        self.capacity = capacity
        self._series: dict[str, Series] = {}
        # Number of frames recorded so far. Every series is expected to hold
        # exactly this many samples, which is what keeps them aligned on the
        # shared time axis.
        self._frame = 0

    def begin_frame(self) -> None:
        """Open a new sample frame. Call once per tick, before any pushes."""
        self._frame += 1

    def series(self, name: str) -> Series:
        existing = self._series.get(name)
        if existing is None:
            existing = Series(name, self.capacity)
            # Back-fill so a series created mid-run lines up on the time axis
            # with the ones recording since startup. The count comes from the
            # frame number, not from how long other series happen to be:
            # measuring against siblings would give every series created later
            # in the same frame one extra gap than the one before it, and the
            # error compounds on every frame.
            missing = min(self._frame - 1, self.capacity)
            for _ in range(max(0, missing)):
                existing.push(None)
            self._series[name] = existing
        return existing

    def push(self, name: str, value: float | None) -> None:
        self.series(name).push(value)

    def push_many(self, values: dict[str, float | None]) -> None:
        for name, value in values.items():
            self.push(name, value)

    def get(self, *names: str) -> list[Series]:
        return [self.series(name) for name in names]

    def names(self) -> Iterable[str]:
        return self._series.keys()

    def drop(self, name: str) -> None:
        self._series.pop(name, None)

    def prune(self, keep: set[str]) -> None:
        for name in set(self._series) - keep:
            del self._series[name]
