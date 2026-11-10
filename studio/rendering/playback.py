"""Defaults and clock for Studio's animated previews."""

from time import monotonic


DEFAULT_ANIMATION_SPEED = 0.50


class PreviewClock:
    """Accumulate preview time without rescaling time already played."""

    def __init__(self, speed=None):
        self._speed = speed
        self.start()

    def start(self):
        self._last = monotonic()
        self._elapsed = 0.0

    def elapsed(self) -> float:
        now = monotonic()
        rate = self._speed() if self._speed is not None else DEFAULT_ANIMATION_SPEED
        self._elapsed += (now - self._last) * 1000.0 * rate
        self._last = now
        return self._elapsed
