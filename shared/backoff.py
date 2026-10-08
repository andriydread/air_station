"""Retry backoff: 30 s, 60 s, 120 s, 240 s, then every 300 s.

Used for devices that may be unplugged or stuck (a sensor, the I2C bus, the e-paper).
"""

BACKOFF_START = 30.0
BACKOFF_MAX = 300.0


class ReinitBackoff:
    def __init__(self, start: float = BACKOFF_START, maximum: float = BACKOFF_MAX):
        self.start = start
        self.maximum = maximum
        self.delay = start
        self.next_try = 0.0
        self.failures = 0

    def due(self, now: float) -> bool:
        return now >= self.next_try

    def failed(self, now: float) -> float:
        """Record a failure and return the delay before the next attempt."""
        self.failures += 1
        self.next_try = now + self.delay
        current = self.delay
        self.delay = min(self.delay * 2, self.maximum)
        return current

    def reset(self) -> None:
        self.delay = self.start
        self.next_try = 0.0
        self.failures = 0
