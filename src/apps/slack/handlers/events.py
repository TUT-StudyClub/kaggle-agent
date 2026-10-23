import re

from pydantic import ValidationError
from slack_bolt import App

from modules.interaction.public import ApprovalReaction, IncomingMessage, InteractionFacade, Source


def register_listeners(app: App, agent: InteractionFacade, bot_user_id: str) -> None:
    def receive(body: dict, event: dict) -> None:
        if (
            event.get("bot_id")
            or event.get("subtype")
            or event.get("user") == bot_user_id
            or not event.get("text")
        ):
            return
        try:
            source = Source(
                team_id=body.get("team_id", ""),
                channel_id=event.get("channel", ""),
                thread_ts=event.get("thread_ts", event.get("ts", "")),
                user_id=event.get("user", ""),
            )
            text = re.sub(rf"<@{re.escape(bot_user_id)}>", "", event["text"]).strip()
            mentioned = f"<@{bot_user_id}>" in event["text"]
            addressing = (
                "mention"
                if mentioned
                else "direct"
                if event.get("channel_type") == "im"
                else "followup"
            )
            if event.get("type") == "message" and event.get("channel_type") != "im":
                if not mentioned and text != "!stop" and not agent.tracks(source):
                    return
            # app_mentionとmessage.channelsで同じ投稿を受けても一度だけ登録する。
            agent.receive(
                IncomingMessage(
                    event_id=f"{source.channel_id}/{event['ts']}",
                    source=source,
                    text=text,
                    addressing=addressing,
                    message_ts=event["ts"],
                )
            )
        except ValidationError, KeyError:
            return

    app.event("app_mention")(receive)
    app.event("message")(receive)

    def receive_reaction(body: dict, event: dict) -> None:
        item = event.get("item", {})
        if item.get("type") != "message" or event.get("item_user") != bot_user_id:
            return
        if event.get("user") == bot_user_id:
            return
        try:
            agent.receive_reaction(
                ApprovalReaction(
                    team_id=body.get("team_id", ""),
                    channel_id=item.get("channel", ""),
                    message_ts=item.get("ts", ""),
                    user_id=event.get("user", ""),
                    reaction=event.get("reaction", ""),
                )
            )
        except ValidationError:
            return

    app.event("reaction_added")(receive_reaction)
