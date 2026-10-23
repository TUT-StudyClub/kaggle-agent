from abc import ABC, abstractmethod

from modules.kaggle.domain.commands import KaggleInvocation


class KaggleRunner(ABC):
    @abstractmethod
    def describe(self, command: str, may_write: bool) -> dict[str, object]: ...

    @abstractmethod
    def prepare(self, command: str, arguments: list[str]) -> KaggleInvocation: ...

    @abstractmethod
    def run(self, invocation: KaggleInvocation, key: str) -> dict[str, object]: ...
