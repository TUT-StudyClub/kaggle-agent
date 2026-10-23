from abc import ABC, abstractmethod


class ReminderSender(ABC):
    @abstractmethod
    def send(self, channel_id: str, text: str) -> str:
        """トップレベルの通知を送り、SlackのメッセージTSを返す。"""
        ...
