from typing import Literal

from pydantic import Field

from shared.domain.value_object import ValueObject


class SearchPages(ValueObject):
    kind: Literal["notion.search"] = "notion.search"
    query: str = Field(min_length=1, max_length=200)


class ReadPage(ValueObject):
    kind: Literal["notion.read"] = "notion.read"
    page_id: str = Field(min_length=1)


class CreatePage(ValueObject):
    kind: Literal["notion.create"] = "notion.create"
    parent_alias: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    record_thread: bool = False


class AppendPage(ValueObject):
    kind: Literal["notion.append"] = "notion.append"
    page_id: str
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=12000)
    record_thread: bool = False


class UpdateBlock(ValueObject):
    kind: Literal["notion.update_block"] = "notion.update_block"
    page_id: str
    block_id: str
    before: str = Field(max_length=2000)
    after: str = Field(min_length=1, max_length=2000)
