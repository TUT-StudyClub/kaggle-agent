import hashlib
import json
import time

from pydantic import ValidationError
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.domain.models import ReplyDelivery, Source
from modules.interaction.domain.repositories import DeliveryRepository
from shared.errors import AgentError


class SlackMessenger(Messenger):
    def __init__(
        self,
        client: WebClient,
        policy: AgentPolicy,
        deliveries: DeliveryRepository | None = None,
        *,
        bot_user_id: str | None = None,
    ):
        self.client = client
        self.policy = policy
        self.deliveries = deliveries
        self.bot_user_id = bot_user_id

    def status(self, source: Source, activity: Activity) -> None:
        self._authorize(source)
        try:
            self.client.assistant_threads_setStatus(
                channel_id=source.channel_id, thread_ts=source.thread_ts, status=activity.value
            )
        except SlackClientError, OSError:
            raise AgentError("slack_status", "Slackの処理状況を更新できません。") from None

    def _authorize(self, source: Source) -> None:
        if (
            source.team_id != self.policy.team_id
            or source.user_id not in self.policy.user_ids
            or source.channel_id not in self.policy.channel_ids
        ):
            raise AgentError("forbidden_source", "Slackの許可範囲外です。")

    def _read(self, method: str, **kwargs):
        for attempt in range(3):
            try:
                return getattr(self.client, method)(**kwargs)
            except SlackApiError as error:
                if error.response.status_code == 429 and attempt < 2:
                    delay = int(error.response.headers.get("Retry-After", "1"))
                    if delay <= 30:
                        time.sleep(max(0, delay))
                        continue
                raise AgentError(
                    "slack_read", "Slackの履歴取得権限・レート制限を確認してください。"
                ) from None
            except SlackClientError, OSError:
                raise AgentError("slack_network", "Slackへ接続できません。") from None
        raise AssertionError("unreachable")

    def _post(self, source: Source, text: str):
        # 通常の回答は標準MarkdownをSlack側で変換する。12,000文字を超える
        # 操作内容などは従来のmrkdwnで送り、本文や承認コマンドを切り捨てない。
        if len(text) <= 12000:
            return self.client.chat_postMessage(
                channel=source.channel_id,
                thread_ts=source.thread_ts,
                markdown_text=text,
                unfurl_links=False,
                unfurl_media=False,
            )
        return self.client.chat_postMessage(
            channel=source.channel_id,
            thread_ts=source.thread_ts,
            text=text,
            mrkdwn=True,
            unfurl_links=False,
            unfurl_media=False,
        )

    def send(self, source: Source, text: str) -> None:
        self._authorize(source)
        try:
            self._post(source, text)
        except SlackClientError, OSError:
            raise AgentError("slack_send", "Slackへの送信に失敗しました。") from None

    def _stop(self, source: Source, message_ts: str) -> None:
        try:
            self.client.chat_stopStream(channel=source.channel_id, ts=message_ts)
        except SlackApiError as error:
            if error.response.get("error") not in {
                "message_not_in_streaming_state",
                "not_in_streaming_state",
            }:
                raise

    def deliver(self, notification_id: str, source: Source, text: str) -> None:
        self._authorize(source)
        store = self.deliveries
        if store is not None and notification_id.endswith("/awaiting_approval"):
            self._deliver_approval(notification_id, source, text, store)
            return
        if store is None or not notification_id.endswith("/succeeded") or len(text) > 12000:
            self.send(source, text)
            return
        digest = hashlib.sha256(text.encode()).hexdigest()
        delivery = store.get_delivery(notification_id)
        if delivery is not None:
            if delivery.source != source or delivery.text_digest != digest:
                raise AgentError("delivery_mismatch", "返信の送信先・内容が一致しません。")
            if delivery.state == "done":
                return
            if delivery.message_ts is None:
                # 開始リクエストの結果が不明なら、新規メッセージを自動作成しない。
                raise AgentError(
                    "slack_delivery_uncertain", "Slackの返信開始結果を確認してください。"
                )
            try:
                self._stop(source, delivery.message_ts)
                # 追記が届いたか不明な場合も、同じ返信を検証済みの全文で置き換える。
                self.client.chat_update(
                    channel=source.channel_id, ts=delivery.message_ts, markdown_text=text
                )
            except SlackClientError, OSError:
                raise AgentError("slack_stream_repair", "Slackの返信を復旧できません。") from None
            store.save_delivery(delivery.model_copy(update={"state": "done"}))
            return
        delivery = ReplyDelivery(id=notification_id, source=source, text_digest=digest)
        store.save_delivery(delivery)
        try:
            try:
                started = self.client.chat_startStream(
                    channel=source.channel_id,
                    thread_ts=source.thread_ts,
                    recipient_team_id=source.team_id,
                    recipient_user_id=source.user_id,
                    markdown_text=text[:512],
                )
            except SlackApiError as error:
                code = error.response.get("error")
                if code in {"internal_error", "fatal_error"}:
                    raise
                if code in {
                    "missing_scope",
                    "method_not_supported_for_channel_type",
                    "not_allowed_token_type",
                    "unknown_method",
                    "feature_not_enabled",
                }:
                    started = self._post(source, text)
                    state = "done"
                else:
                    # Slackが明示的に拒否した場合だけ、開始を再試行できる状態へ戻す。
                    store.forget_delivery(notification_id)
                    raise
            else:
                state = "open"
            message_ts = started.get("ts")
            if not isinstance(message_ts, str):
                raise AgentError("slack_delivery_uncertain", "Slackの返信IDを確認できません。")
            try:
                delivery = ReplyDelivery.model_validate(
                    {
                        **delivery.model_dump(),
                        "state": state,
                        "message_ts": message_ts,
                        "mode": "message" if state == "done" else "stream",
                    }
                )
            except ValidationError:
                raise AgentError("slack_delivery_uncertain", "Slackの返信IDが不正です。") from None
            store.save_delivery(delivery)
            if state == "done":
                return
            for offset in range(512, len(text), 512):
                self.client.chat_appendStream(
                    channel=source.channel_id,
                    ts=message_ts,
                    markdown_text=text[offset : offset + 512],
                )
            self._stop(source, message_ts)
            store.save_delivery(delivery.model_copy(update={"state": "done"}))
        except SlackClientError, OSError:
            # TSが得られたストリームは次回に同じ返信を修復する。別投稿にフォールバックしない。
            raise AgentError("slack_stream", "Slackへの返信送信が中断しました。") from None

    def _deliver_approval(
        self,
        notice_id: str,
        source: Source,
        text: str,
        store: DeliveryRepository,
    ) -> None:
        digest = hashlib.sha256(text.encode()).hexdigest()
        existing = store.get_delivery(notice_id)
        if existing is not None:
            if existing.source != source or existing.text_digest != digest:
                raise AgentError("delivery_mismatch", "承認案の送信先・内容が一致しません。")
            if existing.state == "done":
                return
            raise AgentError("slack_delivery_uncertain", "承認案の送信結果を確認してください。")
        delivery = ReplyDelivery(id=notice_id, source=source, text_digest=digest, mode="message")
        store.save_delivery(delivery)
        try:
            try:
                response = self._post(source, text)
            except SlackApiError as error:
                code = error.response.get("error")
                if code not in {"internal_error", "fatal_error", "request_timeout"} and (
                    getattr(error.response, "status_code", 500) < 500
                ):
                    store.forget_delivery(notice_id)
                raise
            ts = response.get("ts")
            delivery = ReplyDelivery.model_validate(
                {
                    **delivery.model_dump(),
                    "state": "done",
                    "message_ts": ts,
                }
            )
            if delivery.message_ts is None:
                raise AgentError(
                    "slack_delivery_uncertain", "承認案のメッセージIDを確認できません。"
                )
            store.save_delivery(delivery)
        except ValidationError:
            raise AgentError(
                "slack_delivery_uncertain", "承認案のメッセージIDが不正です。"
            ) from None
        except SlackClientError, OSError:
            raise AgentError("slack_approval_delivery", "承認案の送信が中断しました。") from None

    def thread(self, source: Source) -> str:
        self._authorize(source)
        messages = []
        cursor = None
        for _ in range(10):
            kwargs = {"channel": source.channel_id, "ts": source.thread_ts, "limit": 100}
            if cursor:
                kwargs["cursor"] = cursor
            raw = self._read("conversations_replies", **kwargs)
            for item in raw.get("messages", []):
                # 認証済みの自分のBotだけを追加する。他のBotや非許可ユーザーは除外。
                own_reply = (
                    self.bot_user_id is not None
                    and item.get("user") == self.bot_user_id
                    and item.get("subtype") in {None, "bot_message"}
                )
                allowed_user = (
                    item.get("user") in self.policy.user_ids
                    and not item.get("bot_id")
                    and not item.get("subtype")
                )
                if own_reply or allowed_user:
                    messages.append(
                        {
                            "role": "assistant" if own_reply else "user",
                            "user": item["user"],
                            "ts": item["ts"],
                            "text": item.get("text", ""),
                        }
                    )
            cursor = raw.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                text = json.dumps(messages, ensure_ascii=False)
                # Bot回答を含めると履歴が増えるため、直近の会話を優先する。
                while len(text) > 24000 and len(messages) > 1:
                    messages.pop(0)
                    text = json.dumps(messages, ensure_ascii=False)
                if len(text) > 24000:
                    messages[0]["text"] = messages[0]["text"][-12000:]
                    messages[0]["truncated"] = True
                    text = json.dumps(messages, ensure_ascii=False)
                return text
        raise AgentError("thread_too_large", "スレッドの取得上限に達しました。")

    def permalink(self, source: Source) -> str:
        self._authorize(source)
        return self._read(
            "chat_getPermalink", channel=source.channel_id, message_ts=source.thread_ts
        )["permalink"]
