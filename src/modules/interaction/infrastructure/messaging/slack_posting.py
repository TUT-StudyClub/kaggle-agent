import re

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.infrastructure.messaging.formatting import message_payload
from shared.errors import AgentError, UncertainWrite


def post_top_level(
    client: WebClient, channel_id: str, text: str, rejected: tuple[str, str], **extra: object
) -> str:
    """チャンネルへトップレベルの通知を投稿し、メッセージTSを返す。

    Slackが明示的に拒否した場合だけ rejected のエラーにする。
    届いたか分からない失敗は、二重投稿を避けるため UncertainWrite にする。
    """
    try:
        response = client.chat_postMessage(
            channel=channel_id,
            **message_payload(text, slack_markup=True),
            parse="none",
            link_names=False,
            unfurl_links=False,
            unfurl_media=False,
            **extra,
        )
    except SlackApiError as error:
        if error.response.status_code >= 500 or error.response.get("error") in {
            "internal_error",
            "fatal_error",
            "request_timeout",
        }:
            raise UncertainWrite() from None
        raise AgentError(*rejected) from None
    except SlackClientError, OSError:
        raise UncertainWrite() from None
    ts = response.get("ts")
    if not isinstance(ts, str) or not re.fullmatch(r"\d+\.\d+", ts):
        raise UncertainWrite()
    return ts
