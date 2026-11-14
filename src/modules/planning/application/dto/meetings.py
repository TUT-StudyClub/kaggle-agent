from typing import Literal

from pydantic import Field

from modules.scheduling.public import CreateEvent
from shared.domain.value_object import ValueObject


class ListAnnouncementChannels(ValueObject):
    kind: Literal["slack.channels"] = "slack.channels"


class MeetingAnnouncement(ValueObject):
    channel_id: str = Field(pattern=r"^[CG][A-Z0-9]+$")
    text: str = Field(min_length=1, max_length=3000)
    mention_channel: bool = False
    channel_name: str = ""


class CreateMeeting(ValueObject):
    kind: Literal["meeting.create"] = "meeting.create"
    event: CreateEvent
    announcement: MeetingAnnouncement
    retro_planning: bool = False
    retro_user_ids: tuple[str, ...] = ()
    retro_user_names: tuple[str, ...] = ()
    retro_slack_user_ids: tuple[str, ...] = ()
