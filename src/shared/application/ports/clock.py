from abc import ABC, abstractmethod
from datetime import datetime


class Clock(ABC):
    @abstractmethod
    def now(self) -> datetime: ...

    def timestamp(self) -> float:
        return self.now().timestamp()

    @abstractmethod
    def monotonic(self) -> float: ...
