import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.ports.reminders import ReminderSender
from shared.errors import AgentError, UncertainWrite


class SlackReminderSender(ReminderSender):
    def __init__(self, client: WebClient, channel_ids: frozenset[str]):
        self.client = client
        self.channel_ids = channel_ids

    def _authorize(self, channel_id: str) -> None:
        if channel_id not in self.channel_ids:
            raise AgentError("forbidden_channel", "定期通知の送信先が許可範囲外です。")

    def _read(self, method: str, **kwargs):
        try:
            return getattr(self.client, method)(**kwargs)
        except SlackApiError as error:
            if error.response.get("error") == "missing_scope":
                raise AgentError(
                    "slack_members_permission",
                    "users:readとチャンネル読取権限を追加し、"
                    "Slack Appを再インストールしてください。",
                ) from None
            raise AgentError("slack_members_read", "通知先のメンバーを取得できません。") from None
        except SlackClientError, OSError:
            raise AgentError(
                "slack_members_network", "通知先のメンバーに接続できません。"
            ) from None

    def human_members(self, channel_id: str) -> frozenset[str]:
        self._authorize(channel_id)
        ids = set()
        cursor = None
        for _ in range(10):
            kwargs = {"channel": channel_id, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            page = self._read("conversations_members", **kwargs)
            members = page.get("members")
            if not isinstance(members, list) or any(
                not isinstance(user, str) or not re.fullmatch(r"[UW][A-Z0-9]+", user)
                for user in members
            ):
                raise AgentError("slack_members_incomplete", "チャンネルのメンバー一覧が不正です。")
            ids.update(members)
            following = page.get("response_metadata", {}).get("next_cursor")
            if not following:
                break
            if following == cursor:
                raise AgentError("slack_members_incomplete", "メンバーを最後まで取得できません。")
            cursor = following
        else:
            raise AgentError("slack_members_incomplete", "メンバーの取得上限に達しました。")
        humans = set()
        for user_id in sorted(ids):
            if user_id == "USLACKBOT":
                continue
            user = self._read("users_info", user=user_id).get("user", {})
            if user.get("id") != user_id or not isinstance(user.get("is_bot"), bool):
                raise AgentError("slack_members_incomplete", "メンバーの種別を確認できません。")
            if not user.get("deleted") and not user["is_bot"] and not user.get("is_app_user"):
                humans.add(user_id)
        return frozenset(humans)

    def send(self, channel_id: str, text: str) -> str:
        self._authorize(channel_id)
        try:
            result = self.client.chat_postMessage(
                channel=channel_id,
                text=text,
                mrkdwn=True,
                parse="none",
                link_names=False,
                unfurl_links=False,
                unfurl_media=False,
            )
        except SlackApiError as error:
            if error.response.status_code >= 500 or error.response.get("error") in {
                "internal_error",
                "fatal_error",
                "request_timeout",
            }:
                raise UncertainWrite() from None
            raise AgentError("slack_reminder_rejected", "Slackが定期通知を拒否しました。") from None
        except SlackClientError, OSError:
            raise UncertainWrite() from None
        ts = result.get("ts")
        if not isinstance(ts, str) or not re.fullmatch(r"\d+\.\d+", ts):
            raise UncertainWrite()
        return ts
