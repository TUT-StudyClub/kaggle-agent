from typing import Literal

from pydantic import Field

from shared.domain.value_object import ValueObject


class ApprovalRequest(ValueObject):
    instruction: str
    requester_user_id: str
    addressing: Literal["direct", "mention", "followup"]
    thread: str


class ApprovalDecision(ValueObject):
    approves: bool = Field(strict=True)
