import threading
import time
from typing import Callable, Optional


class RuntimeWatchdog:
    """Decide when a running job must stop: cancellation, wall clock, or silence.

    A job is never bounded by default; a wall-clock deadline only exists when
    the caller supplies one explicitly. Silence is the liveness signal: reader
    threads call ``record_activity`` for every chunk the child emits and the
    owning loop calls ``verdict`` between polls. Only the owning loop touches
    the clock, which keeps the policy deterministic under test.
    """

    CANCELED = "canceled"
    TIMEOUT = "timeout"
    IDLE = "idle"
    MIN_POLL_INTERVAL_SECONDS = 0.05

    def __init__(
        self,
        timeout_seconds: Optional[float],
        idle_timeout_seconds: Optional[float],
        cancel_check: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self._clock = clock or time.monotonic
        self._cancel_check = cancel_check
        self.timeout_seconds = timeout_seconds
        self.idle_timeout_seconds = idle_timeout_seconds
        self._activity = threading.Event()
        now = self._clock()
        self._now = now
        self._deadline = now + timeout_seconds if timeout_seconds else None
        self._idle_deadline = now + idle_timeout_seconds if idle_timeout_seconds else None

    def record_activity(self) -> None:
        self._activity.set()

    def verdict(self) -> Optional[str]:
        if self._cancel_check is not None:
            try:
                if bool(self._cancel_check()):
                    return self.CANCELED
            except Exception:
                pass
        now = self._clock()
        self._now = now
        if self._idle_deadline is not None and self._activity.is_set():
            self._activity.clear()
            self._idle_deadline = now + self.idle_timeout_seconds
        if self._deadline is not None and now >= self._deadline:
            return self.TIMEOUT
        if self._idle_deadline is not None and now >= self._idle_deadline:
            return self.IDLE
        return None

    def poll_interval(self, maximum: float) -> float:
        deadlines = [value for value in (self._deadline, self._idle_deadline) if value is not None]
        if not deadlines:
            return maximum
        return max(self.MIN_POLL_INTERVAL_SECONDS, min(maximum, min(deadlines) - self._now))

    def describe(self, verdict: str, subject: str) -> str:
        if verdict == self.TIMEOUT:
            return f"{subject} timed out after {self.timeout_seconds:g} seconds"
        if verdict == self.IDLE:
            return f"{subject} produced no output for {self.idle_timeout_seconds:g} seconds"
        return f"{subject} canceled"
