from abc import ABC, abstractmethod


class ReminderSender(ABC):
    @abstractmethod
    def human_members(self, channel_id: str) -> frozenset[str]:
        """通知先の有効な人間のメンバーを全件取得する。Bot・無効化済みは除く。"""
        ...

    @abstractmethod
    def send(self, channel_id: str, text: str) -> str:
        """トップレベルの通知を送り、SlackのメッセージTSを返す。"""
        ...
