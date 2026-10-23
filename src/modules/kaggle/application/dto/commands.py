from typing import Literal

from pydantic import Field

from shared.domain.value_object import ValueObject


class KaggleHelp(ValueObject):
    kind: Literal["kaggle.help"] = "kaggle.help"
    command: str = Field(default="", max_length=100)


class ReadKaggle(ValueObject):
    kind: Literal["kaggle.read"] = "kaggle.read"
    command: str = Field(min_length=1, max_length=100)
    arguments: list[str] = Field(default_factory=list, max_length=64)


class WriteKaggle(ValueObject):
    kind: Literal["kaggle.write"] = "kaggle.write"
    command: str = Field(min_length=1, max_length=100)
    arguments: list[str] = Field(default_factory=list, max_length=64)
