import hashlib
import html
from typing import cast
from zoneinfo import ZoneInfo

from modules.interaction.application.ports.announcements import AnnouncementSender
from modules.interaction.domain.meetings import MeetingReceipt, MeetingRepository
from modules.planning.public import CreateMeeting, MeetingAnnouncement
from modules.scheduling.public import CreateEvent, SchedulingService
from shared.errors import AgentError, UncertainWrite


def announcement_text(
    announcement: MeetingAnnouncement, guest_user_ids: list[str] | None = None
) -> str:
    # 本文内のSlack制御構文を無効化し、承認対象のフラグだけを全体メンションへ変換する。
    text = html.escape(announcement.text, quote=False)
    mentions = (
        "<!channel>"
        if announcement.mention_channel
        else " ".join(f"<@{user}>" for user in (guest_user_ids or []))
    )
    return (mentions + "\n" if mentions else "") + text


class MeetingService:
    def __init__(
        self,
        scheduling: SchedulingService,
        sender: AnnouncementSender | None,
        receipts: MeetingRepository | None,
    ):
        self.scheduling = scheduling
        self.sender = sender
        self.receipts = receipts

    def channels(self) -> list[dict[str, str]]:
        if self.sender is None:
            raise AgentError("announcement_configuration", "Slack周知の接続を設定してください。")
        return self.sender.channels()

    def prepare(self, action: CreateMeeting) -> CreateMeeting:
        channel = next(
            (c for c in self.channels() if c["id"] == action.announcement.channel_id), None
        )
        if channel is None:
            raise AgentError("forbidden_channel", "許可された周知先チャンネルを選択してください。")
        event = cast(CreateEvent, self.scheduling.prepare(action.event))
        return action.model_copy(
            update={
                "event": event,
                "announcement": action.announcement.model_copy(
                    update={"channel_name": channel["name"]}
                ),
            }
        )

    def _digest(self, action: CreateMeeting) -> str:
        return hashlib.sha256(action.model_dump_json().encode()).hexdigest()

    def _result(self, receipt: MeetingReceipt) -> dict[str, object]:
        return {"event": receipt.event, "announcement": {"message_ts": receipt.message_ts}}

    def execute(self, action: CreateMeeting, key: str) -> dict[str, object]:
        if self.receipts is None or self.sender is None:
            raise AgentError("announcement_configuration", "Slack周知の接続を設定してください。")
        digest = self._digest(action)
        receipt = MeetingReceipt(key=key, plan_digest=digest)
        if not self.receipts.reserve_meeting(receipt):
            previous = self.receipts.meeting(key)
            if previous is None or previous.plan_digest != digest:
                raise AgentError(
                    "meeting_mismatch", "承認された操作と保存済みの操作が一致しません。"
                )
            if previous.phase == "complete":
                return self._result(previous)
            # API送信と保存の間で止まった可能性がある。再送せず読取りで照合する。
            result = self.reconcile(action, key)
            if result is not None:
                return result
            raise UncertainWrite()
        # Slackの参加状況・チャンネル名の変更も、Calendarに書く前に検証する。
        prepared = self.prepare(action)
        if prepared != action:
            raise AgentError(
                "meeting_changed", "承認後に操作内容が変わりました。再度依頼してください。"
            )
        result = self.scheduling.execute(action.event, key)
        receipt = receipt.model_copy(update={"event": result["event"], "phase": "event_created"})
        self.receipts.save_meeting(receipt)
        receipt = receipt.model_copy(update={"phase": "posting"})
        self.receipts.save_meeting(receipt)
        try:
            ts = self.sender.send(
                action.announcement.channel_id,
                announcement_text(action.announcement, action.event.guest_user_ids),
                key,
            )
        except Exception:
            raise AgentError(
                "meeting_announcement_incomplete",
                "予定は作成済みで、招待メール送信の要求も完了したよ。"
                "Slack周知は未完了か結果不明。予定を作り直さず、このジョブの結果を照合してね。",
            ) from None
        receipt = receipt.model_copy(update={"phase": "complete", "message_ts": ts})
        self.receipts.save_meeting(receipt)
        return self._result(receipt)

    def reconcile(self, action: CreateMeeting, key: str) -> dict[str, object] | None:
        if self.receipts is None or self.sender is None:
            return None
        receipt = self.receipts.meeting(key)
        if receipt is None or receipt.plan_digest != self._digest(action):
            return None
        event_result = self.scheduling.reconcile(action.event, key)
        if event_result is None:
            return None
        ts = self.sender.find(
            action.announcement.channel_id,
            announcement_text(action.announcement, action.event.guest_user_ids),
            key,
        )
        if ts is None:
            return None
        receipt = receipt.model_copy(
            update={
                "event": event_result["event"],
                "message_ts": ts,
                "phase": "complete",
            }
        )
        self.receipts.save_meeting(receipt)
        return self._result(receipt)


def meeting_preview(action: CreateMeeting) -> str:
    event = action.event
    period = event.draft.period
    start = period.start.astimezone(ZoneInfo("Asia/Tokyo"))
    end = period.end.astimezone(ZoneInfo("Asia/Tokyo"))
    label = event.draft.label
    label_text = (
        f"{html.escape(label.name or '名前なし', quote=False)} ({label.background_color})"
        if label is not None
        else "指定なし"
    )
    guests = (
        "\n".join(
            f"• {html.escape(name, quote=False)} ({user}): {html.escape(email, quote=False)}"
            for user, name, email in zip(
                event.guest_user_ids, event.guest_names, event.draft.guest_emails, strict=True
            )
        )
        or "なし"
    )
    mentions = (
        "@channel"
        if action.announcement.mention_channel
        else ", ".join(html.escape(name, quote=False) for name in event.guest_names) or "なし"
    )
    return (
        f"予定作成・招待メール送信・Slack周知をまとめて承認する？\n\n"
        f"タイトル: {html.escape(event.draft.title, quote=False)}\n"
        f"日時: {start:%Y/%m/%d %H:%M}〜{end:%Y/%m/%d %H:%M}（日本時間）\n"
        f"カレンダー: {html.escape(event.calendar_alias, quote=False)}\n"
        f"イベントラベル: {label_text}\n招待先:\n{guests}\n"
        f"Google Meet: {'作成する' if event.draft.create_google_meet else '指定なし'}\n"
        f"周知先: #{action.announcement.channel_name} ({action.announcement.channel_id})\n"
        f"@channel: {'使用する' if action.announcement.mention_channel else '使用しない'}\n"
        f"メンション: {mentions}\n"
        # プレビューではメンションを発火させず、送信する文章を表示する。
        f"周知文:\n```\n{('@channel' + chr(10)) if action.announcement.mention_channel else ''}"
        f"{html.escape(action.announcement.text.replace('`', '｀'), quote=False)}\n```\n"
        "予定作成が確認できた後に周知するよ。"
    )
