import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.ports.reminders import ReminderSender
from shared.errors import AgentError, UncertainWrite


class SlackReminderSender(ReminderSender):
    def __init__(self, client: WebClient, channel_ids: frozenset[str]):
        self.client = client
        self.channel_ids = channel_ids

    def send(self, channel_id: str, text: str) -> str:
        if channel_id not in self.channel_ids:
            raise AgentError("forbidden_channel", "定期通知の送信先が許可範囲外です。")
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
