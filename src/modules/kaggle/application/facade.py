from shared.errors import AgentError

from .dto.commands import KaggleHelp, ReadKaggle, WriteKaggle
from .ports.runner import KaggleRunner


class KaggleService:
    def __init__(self, runner: KaggleRunner, owner_user_id: str | None):
        self.runner = runner
        self.owner_user_id = owner_user_id

    def prepare(self, action: object, requester_user_id: str | None) -> None:
        if not requester_user_id:
            raise AgentError("kaggle_actor_required", "Kaggle操作の依頼者を確認できないよ。")
        if isinstance(action, KaggleHelp):
            return
        if not isinstance(action, (ReadKaggle, WriteKaggle)):
            raise AgentError("unsupported_action", "未対応のKaggle操作だよ。")
        if isinstance(action, WriteKaggle) and requester_user_id != self.owner_user_id:
            raise AgentError(
                "kaggle_owner_only",
                "Kaggleの提出・公開・削除・作成・実行・更新は、管理者本人だけが実行できるよ。",
            )
        invocation = self.runner.prepare(action.command, action.arguments)
        if invocation.write != isinstance(action, WriteKaggle):
            raise AgentError(
                "kaggle_action_mismatch",
                "閲覧と変更の操作区分が一致しないため、Kaggle操作を止めたよ。",
            )

    def execute(self, action: object, requester_user_id: str | None, key: str) -> dict[str, object]:
        self.prepare(action, requester_user_id)
        if isinstance(action, KaggleHelp):
            return self.runner.describe(action.command, requester_user_id == self.owner_user_id)
        assert isinstance(action, (ReadKaggle, WriteKaggle))
        invocation = self.runner.prepare(action.command, action.arguments)
        return self.runner.run(invocation, key)
