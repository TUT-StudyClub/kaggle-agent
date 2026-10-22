import time
from datetime import UTC, datetime

from shared.application.ports.clock import Clock


class SystemClock(Clock):
    def now(self) -> datetime:
        return datetime.now(UTC)

    def timestamp(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()
