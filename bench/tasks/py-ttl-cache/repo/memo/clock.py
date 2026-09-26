"""Clock sources. A clock is any zero-argument callable returning seconds."""
import time


def monotonic_clock():
    return time.monotonic()


class FakeClock:
    """Manually driven clock for tests."""

    def __init__(self, start=0.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("time only moves forward")
        self.now += seconds
        return self.now
