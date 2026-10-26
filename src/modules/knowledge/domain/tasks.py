from abc import ABC, abstractmethod
from datetime import date

from pydantic import Field

from shared.domain.value_object import ValueObject


class TaskPerson(ValueObject):
    id: str
    name: str = ""
    email: str = ""


class BoardTask(ValueObject):
    id: str
    title: str
    url: str
    start: str
    assignees: tuple[TaskPerson, ...] = ()


class TaskMember(ValueObject):
    name: str = Field(min_length=1)
    slack_user_id: str = Field(pattern=r"^[UW][A-Z0-9]+$")
    emails: frozenset[str] = frozenset()


class TaskNotification(ValueObject):
    id: str
    title: str
    url: str
    start: str
    slack_user_ids: frozenset[str]


class TaskBoardRepository(ABC):
    @abstractmethod
    def tasks_on(self, day: date) -> list[BoardTask]: ...

    @abstractmethod
    def members(self) -> list[TaskMember]: ...
