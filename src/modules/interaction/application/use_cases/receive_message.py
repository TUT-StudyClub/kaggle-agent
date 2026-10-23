import re

from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Messenger
from modules.interaction.domain.approval import is_approval_reply
from modules.interaction.domain.models import (
    IncomingMessage,
)
from modules.interaction.domain.repositories import JobRepository
from shared.application.ports.clock import Clock


class ReceiveMessageUseCase:
    def __init__(
        self, settings: AgentPolicy, jobs: JobRepository, messenger: Messenger, clock: Clock
    ):
        self.settings = settings
        self.jobs = jobs
        self.messenger = messenger
        self.clock = clock

    def execute(self, message: IncomingMessage) -> str | None:
        source = message.source
        if not self.settings.allows(source):
            return None
        approval = re.fullmatch(r"承認\s+([a-f0-9]{32})", message.text.strip())
        if approval:
            if not self.jobs.approve(approval[1], source, self.clock.timestamp()):
                self.messenger.send(
                    source, "承認できません。対象・利用者・スレッド・有効期限を確認してください。"
                )
            return None
        if is_approval_reply(message.text):
            if self.jobs.approve_pending(source, self.clock.timestamp()):
                return None
            job = self.jobs.answer_clarification(message)
            if job is not None:
                return job.id
            self.messenger.send(
                source,
                "承認・返答できる確認がないか、期限が切れているよ。"
                "Botの確認メッセージと同じスレッドで返信してね。",
            )
            return None
        job = self.jobs.enqueue(message)
        if job:
            return job.id
        return None
