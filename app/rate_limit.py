import time
from collections import deque
from collections.abc import Callable


class SlidingWindowLimiter:
    def __init__(self, window_seconds: float, clock: Callable[[], float] = time.monotonic):
        self._window_seconds = window_seconds
        self._clock = clock
        self._hits: deque[tuple[float, str]] = deque()

    def try_acquire(self, key: str, limit: int) -> bool:
        now = self._clock()
        while self._hits and self._hits[0][0] <= now - self._window_seconds:
            self._hits.popleft()
        if sum(1 for _, hit_key in self._hits if hit_key == key) >= limit:
            return False
        self._hits.append((now, key))
        return True
