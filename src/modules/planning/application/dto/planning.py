from typing import Annotated, Literal

from pydantic import Field

from modules.kaggle.public import KaggleHelp, ReadKaggle, WriteKaggle
from modules.knowledge.public import (
    AppendPage as AppendPage,
)
from modules.knowledge.public import (
    CreatePage as CreatePage,
)
from modules.knowledge.public import (
    ReadPage as ReadPage,
)
from modules.knowledge.public import (
    SearchPages as SearchPages,
)
from modules.knowledge.public import (
    UpdateBlock as UpdateBlock,
)
from modules.scheduling.public import (
    CreateEvent as CreateEvent,
)
from modules.scheduling.public import (
    FindFree as FindFree,
)
from modules.scheduling.public import (
    ListCalendars as ListCalendars,
)
from modules.scheduling.public import (
    ListEvents as ListEvents,
)
from modules.scheduling.public import (
    UpdateEvent as UpdateEvent,
)
from shared.domain.value_object import ValueObject

from .meetings import CreateMeeting, ListAnnouncementChannels


class Reply(ValueObject):
    kind: Literal["reply"] = "reply"
    text: str = Field(min_length=1, max_length=6000)


class Clarify(ValueObject):
    kind: Literal["clarify"] = "clarify"
    question: str = Field(min_length=1, max_length=2000)


type Action = Annotated[
    Reply
    | Clarify
    | SearchPages
    | ReadPage
    | CreatePage
    | AppendPage
    | UpdateBlock
    | ListCalendars
    | ListEvents
    | FindFree
    | CreateEvent
    | UpdateEvent
    | ListAnnouncementChannels
    | CreateMeeting
    | KaggleHelp
    | ReadKaggle
    | WriteKaggle,
    Field(discriminator="kind"),
]


class Plan(ValueObject):
    action: Action


WRITE_KINDS = frozenset(
    {
        "notion.create",
        "notion.append",
        "notion.update_block",
        "calendar.create",
        "calendar.update",
        "meeting.create",
        "kaggle.write",
    }
)


class PlanningRequest(ValueObject):
    instruction: str
    now: str
    context: str
    resources: dict[str, object]
    thread: str
    observations: list[dict[str, object]]
