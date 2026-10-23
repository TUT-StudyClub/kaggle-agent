from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from shared.domain.value_object import ValueObject
from shared.errors import AgentError


class Source(ValueObject):
    team_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    thread_ts: str = Field(pattern=r"^\d+\.\d+$")
    user_id: str = Field(min_length=1)

    @property
    def conversation_key(self) -> str:
        return f"{self.team_id}/{self.channel_id}/{self.thread_ts}/{self.user_id}"

    @property
    def thread_key(self) -> str:
        return f"{self.team_id}/{self.channel_id}/{self.thread_ts}"


class IncomingMessage(ValueObject):
    event_id: str = Field(min_length=1)
    source: Source
    text: str = Field(max_length=12000)
    received_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    addressing: Literal["direct", "mention", "followup"] = "direct"
    message_ts: str | None = Field(default=None, pattern=r"^\d+\.\d+$")

    @model_validator(mode="after")
    def require_instruction_or_mention(self) -> IncomingMessage:
        if not self.text.strip() and self.addressing != "mention":
            raise ValueError("本文が必要です。")
        return self


class ApprovalReaction(ValueObject):
    team_id: str = Field(min_length=1)
    channel_id: str = Field(min_length=1)
    message_ts: str = Field(pattern=r"^\d+\.\d+$")
    user_id: str = Field(min_length=1)
    reaction: str = Field(min_length=1)


class ReplyDelivery(ValueObject):
    id: str
    source: Source
    text_digest: str
    mode: Literal["stream", "message"] = "stream"
    state: Literal["reserved", "open", "done"] = "reserved"
    message_ts: str | None = Field(default=None, pattern=r"^\d+\.\d+$")


class PendingClarification(ValueObject):
    requests: list[str] = Field(min_length=1, max_length=8)
    question: str = Field(min_length=1, max_length=2000)


class ConversationTurn(ValueObject):
    job_id: str
    instruction: str
    reply: str
    received_at: datetime


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    IGNORED = "ignored"


class Job(ValueObject):
    id: str
    message: IncomingMessage
    status: JobStatus = JobStatus.QUEUED
    plan_json: str | None = None
    approval_expires_at: float | None = None
    error_code: str | None = None
    result_json: str | None = None
    write_started: bool = False
    updated_at: float

    def start_write(self) -> Job:
        if self.status != JobStatus.RUNNING or not self.plan_json:
            raise AgentError("invalid_transition", "承認済みの実行中ジョブだけを書き込めます。")
        return self.model_copy(update={"write_started": True})

    def approve(self, source: Source, now: float) -> Job | None:
        if (
            self.status != JobStatus.AWAITING_APPROVAL
            or self.message.source != source
            or self.approval_expires_at is None
            or self.approval_expires_at < now
        ):
            return None
        return self.transition(JobStatus.QUEUED)

    def reconciled(self, result_json: str) -> Job:
        if self.status != JobStatus.FAILED or not self.write_started:
            raise AgentError(
                "invalid_reconciliation", "結果不明の書き込みジョブだけを照合できます。"
            )
        return self.model_copy(
            update={"status": JobStatus.SUCCEEDED, "result_json": result_json, "error_code": None}
        )

    def transition(self, target: JobStatus, **changes: object) -> Job:
        allowed = {
            JobStatus.QUEUED: {JobStatus.RUNNING, JobStatus.FAILED},
            JobStatus.RUNNING: {
                JobStatus.AWAITING_APPROVAL,
                JobStatus.SUCCEEDED,
                JobStatus.FAILED,
                JobStatus.IGNORED,
            },
            JobStatus.AWAITING_APPROVAL: {JobStatus.QUEUED, JobStatus.FAILED},
            JobStatus.SUCCEEDED: set(),
            JobStatus.FAILED: set(),
            JobStatus.IGNORED: set(),
        }
        if target not in allowed[self.status]:
            raise AgentError("invalid_transition", "ジョブ状態の遷移が不正です。")
        return self.model_copy(update={"status": target, **changes})
