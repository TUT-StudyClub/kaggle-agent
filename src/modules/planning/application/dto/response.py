from pydantic import Field

from shared.domain.value_object import ValueObject


class ResponseRequest(ValueObject):
    instruction: str
    requester_user_id: str
    context: str
    thread: str


class ResponseDecision(ValueObject):
    should_respond: bool = Field(strict=True)
