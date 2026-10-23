from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.domain.approval import is_approval_reaction
from modules.interaction.domain.models import ApprovalReaction, Source
from modules.interaction.domain.repositories import JobRepository
from shared.application.ports.clock import Clock


class ReceiveReactionUseCase:
    def __init__(self, policy: AgentPolicy, jobs: JobRepository, clock: Clock):
        self.policy = policy
        self.jobs = jobs
        self.clock = clock

    def execute(self, reaction: ApprovalReaction) -> bool:
        source = Source(
            team_id=reaction.team_id,
            channel_id=reaction.channel_id,
            user_id=reaction.user_id,
            thread_ts=reaction.message_ts,
        )
        if not self.policy.allows(source) or not is_approval_reaction(reaction.reaction):
            return False
        if self.jobs.approve_reaction(reaction, self.clock.timestamp()):
            return True
        # 確認質問への同意は新しい会話ジョブとして扱い、操作の承認状態は付与しない。
        return self.jobs.answer_clarification_reaction(reaction)
