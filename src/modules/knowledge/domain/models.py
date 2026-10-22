from typing import Literal

from pydantic import Field

from shared.domain.value_object import ValueObject


class Page(ValueObject):
    kind: Literal["page", "data_source"] = "page"
    id: str
    title: str
    url: str
    text: str = ""
    revision: str = ""


class Note(ValueObject):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    source_url: str
