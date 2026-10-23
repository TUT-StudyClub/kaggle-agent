import hashlib
import re
from uuid import NAMESPACE_URL, uuid5

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.ports.announcements import AnnouncementSender
from shared.errors import AgentError, UncertainWrite


class SlackAnnouncementSender(AnnouncementSender):
    def __init__(self, client: WebClient, channel_ids: frozenset[str], bot_user_id: str | None):
        self.client = client
        self.channel_ids = channel_ids
        self.bot_user_id = bot_user_id

    def _authorize(self, channel_id: str) -> None:
        if channel_id not in self.channel_ids or not re.fullmatch(r"[CG][A-Z0-9]+", channel_id):
            raise AgentError("forbidden_channel", "許可された周知先チャンネルを選択してください。")

    def _read(self, method: str, **kwargs):
        try:
            return getattr(self.client, method)(**kwargs)
        except SlackClientError, OSError:
            raise AgentError(
                "announcement_read", "Slack周知先の読取権限・接続・Botの参加を確認してください。"
            ) from None

    def channels(self) -> list[dict[str, str]]:
        result = []
        for channel_id in sorted(self.channel_ids):
            if not re.fullmatch(r"[CG][A-Z0-9]+", channel_id):
                continue
            channel = self._read("conversations_info", channel=channel_id).get("channel", {})
            if channel.get("id") != channel_id:
                raise AgentError("announcement_channel", "周知先チャンネルを確認できません。")
            if channel.get("is_archived") or not channel.get("is_member"):
                continue
            name = channel.get("name")
            if not isinstance(name, str) or not name:
                raise AgentError("announcement_channel", "周知先の名前を確認できません。")
            result.append({"id": channel_id, "name": name})
        return result

    def _marker(self, key: str) -> str:
        return hashlib.sha256(f"meeting/{key}".encode()).hexdigest()

    def send(self, channel_id: str, text: str, key: str) -> str:
        self._authorize(channel_id)
        try:
            response = self.client.chat_postMessage(
                channel=channel_id,
                text=text,
                mrkdwn=True,
                parse="none",
                link_names=False,
                unfurl_links=False,
                unfurl_media=False,
                client_msg_id=str(uuid5(NAMESPACE_URL, f"kaggle-agent/meeting/{key}")),
                metadata={
                    "event_type": "kaggle_agent_meeting",
                    "event_payload": {"key": self._marker(key)},
                },
            )
        except SlackApiError as error:
            if error.response.status_code >= 500 or error.response.get("error") in {
                "internal_error",
                "fatal_error",
                "request_timeout",
            }:
                raise UncertainWrite() from None
            raise AgentError("announcement_rejected", "Slackが周知を拒否しました。") from None
        except SlackClientError, OSError:
            raise UncertainWrite() from None
        ts = response.get("ts")
        if not isinstance(ts, str) or not re.fullmatch(r"\d+\.\d+", ts):
            raise UncertainWrite()
        return ts

    def find(self, channel_id: str, text: str, key: str) -> str | None:
        self._authorize(channel_id)
        if self.bot_user_id is None:
            raise AgentError("announcement_identity", "周知したBotのIDを確認できません。")
        cursor = None
        for _ in range(10):
            kwargs = {"channel": channel_id, "limit": 100, "include_all_metadata": True}
            if cursor:
                kwargs["cursor"] = cursor
            page = self._read("conversations_history", **kwargs)
            for message in page.get("messages", []):
                metadata = message.get("metadata", {})
                if (
                    message.get("user") == self.bot_user_id
                    and metadata.get("event_type") == "kaggle_agent_meeting"
                    and metadata.get("event_payload", {}).get("key") == self._marker(key)
                    and message.get("text") == text
                ):
                    ts = message.get("ts")
                    if isinstance(ts, str) and re.fullmatch(r"\d+\.\d+", ts):
                        return ts
            following = page.get("response_metadata", {}).get("next_cursor")
            if not following:
                if page.get("has_more"):
                    break
                return None
            if following == cursor:
                break
            cursor = following
        raise AgentError("announcement_incomplete", "Slack周知の履歴を最後まで照合できません。")
