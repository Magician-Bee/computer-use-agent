"""Deterministic, bounded pointer motion; this module never accesses a desktop."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable, Iterable


@dataclass(frozen=True)
class MotionPolicy:
    profile: str = "smooth"
    min_duration: float = 0.25
    max_duration: float = 1.2
    pixels_per_second: float = 1000.0
    frequency: int = 60
    settle_seconds: float = 0.055
    double_click_gap: float = 0.12
    key_interval: float = 0.035

    def __post_init__(self):
        if self.profile != "smooth":
            raise ValueError("Only the smooth continuous-motion profile is supported.")
        numbers = (self.min_duration, self.max_duration, self.pixels_per_second,
                   self.settle_seconds, self.double_click_gap, self.key_interval)
        if not all(math.isfinite(n) for n in numbers):
            raise ValueError("Motion timings must be finite.")
        if not 0.1 <= self.min_duration <= self.max_duration <= 3 or not 100 <= self.pixels_per_second <= 4000:
            raise ValueError("Motion duration or speed is outside supported limits.")
        if not isinstance(self.frequency, int) or not 20 <= self.frequency <= 120:
            raise ValueError("Motion frequency must be between 20 and 120 Hz.")
        if not 0 <= self.settle_seconds <= 0.5 or not 0.05 <= self.double_click_gap <= 0.3 or not 0.01 <= self.key_interval <= 0.15:
            raise ValueError("Input pacing is outside supported limits.")

    def duration(self, distance: float, requested: float | None = None) -> float:
        if requested is not None and requested > 0:
            return min(max(requested, self.min_duration), 3.0)
        return min(self.max_duration, max(self.min_duration, distance / self.pixels_per_second + self.min_duration))


@dataclass(frozen=True)
class MotionPoint:
    seconds: float
    x: int
    y: int


def trajectory(start: tuple[int, int], end: tuple[int, int], policy: MotionPolicy,
               requested_duration: float | None = None) -> list[MotionPoint]:
    """Minimum-jerk interpolation: zero endpoint velocity and acceleration.

    Straight paths stay inside the endpoint rectangle, avoiding arbitrary curved
    detours over unrelated controls. No random jitter or target overshoot.
    """
    if not all(math.isfinite(v) for v in (*start, *end)):
        raise ValueError("Pointer coordinates must be finite.")
    distance = math.dist(start, end)
    if distance == 0:
        return [MotionPoint(0.0, *map(round, end))]
    duration = policy.duration(distance, requested_duration)
    count = max(2, math.ceil(duration * policy.frequency))
    result = []
    for step in range(count + 1):
        progress = step / count
        eased = progress ** 3 * (10 + progress * (-15 + 6 * progress))
        result.append(MotionPoint(duration * progress,
            round(start[0] + (end[0] - start[0]) * eased),
            round(start[1] + (end[1] - start[1]) * eased)))
    return result


def play_trajectory(points: Iterable[MotionPoint], *, emit: Callable[[int, int], None],
                    check: Callable[[], None], wait: Callable[[float], None],
                    clock: Callable[[], float] = time.monotonic) -> None:
    """Send every scheduled real pointer sample with an interruptible deadline."""
    began = clock()
    for point in points:
        check()
        remaining = began + point.seconds - clock()
        while remaining > 0:
            wait(min(remaining, 0.025))
            check()
            remaining = began + point.seconds - clock()
        check()
        emit(point.x, point.y)
    check()
