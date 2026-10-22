from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.domain.models import Source
from shared.errors import AgentError


class ActivityReporter:
    def __init__(self, messenger: Messenger):
        self.messenger = messenger

    def update(self, source: Source, activity: Activity) -> None:
        try:
            self.messenger.status(source, activity)
        except AgentError:
            # 表示の失敗で業務処理を中断しない。
            pass
