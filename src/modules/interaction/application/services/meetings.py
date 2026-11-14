import hashlib
import html
from typing import cast
from zoneinfo import ZoneInfo

from modules.interaction.application.ports.announcements import AnnouncementSender
from modules.interaction.application.ports.retro_documents import RetroDocuments
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
        retro_documents: RetroDocuments | None = None,
    ):
        self.scheduling = scheduling
        self.sender = sender
        self.receipts = receipts
        self.retro_documents = retro_documents

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
        retro_values = {}
        event = action.event
        if action.retro_planning:
            if self.retro_documents is None:
                raise AgentError("retro_configuration", "レトプラのNotion設定を確認してね。")
            members = self.scheduling.members(action.event.calendar_alias)
            names = [m["name"] for m in members]
            guest_ids = list(event.guest_user_ids)
            guest_ids.extend(
                m["slack_user_id"] for m in members if m["slack_user_id"] not in guest_ids
            )
            event = event.model_copy(update={"guest_user_ids": guest_ids})
            retro_values = {
                "retro_user_ids": self.retro_documents.members(members),
                "retro_user_names": tuple(names),
                "retro_slack_user_ids": tuple(m["slack_user_id"] for m in members),
            }
        event = cast(CreateEvent, self.scheduling.prepare(event))
        return action.model_copy(
            update={
                "event": event,
                **retro_values,
                "announcement": action.announcement.model_copy(
                    update={"channel_name": channel["name"]}
                ),
            }
        )

    def _digest(self, action: CreateMeeting) -> str:
        return hashlib.sha256(action.model_dump_json().encode()).hexdigest()

    def _result(self, receipt: MeetingReceipt) -> dict[str, object]:
        return {
            "event": receipt.event,
            "announcement": {"message_ts": receipt.message_ts},
            "document_url": receipt.document_url,
        }

    def _linked_event(self, action: CreateMeeting, url: str | None) -> CreateEvent:
        if not action.retro_planning or not url:
            return action.event
        description = action.event.draft.description or ""
        description = (
            description + "\n\n" if description else ""
        ) + f"レトプラのドキュメント: {url}"
        return action.event.model_copy(
            update={"draft": action.event.draft.model_copy(update={"description": description})}
        )

    def _announcement(self, action: CreateMeeting, url: str | None) -> str:
        users = (
            list(action.retro_slack_user_ids)
            if action.retro_planning
            else action.event.guest_user_ids
        )
        text = announcement_text(action.announcement, users)
        if not url:
            return text
        day = action.event.draft.period.start.astimezone(ZoneInfo("Asia/Tokyo"))
        label = html.escape(f"[Retro & Planning] {day:%Y/%m/%d}", quote=False)
        return text + f"\nレトプラのドキュメント: <{html.escape(url, quote=False)}|{label}>"

    def execute(self, action: CreateMeeting, key: str) -> dict[str, object]:
        if self.receipts is None or self.sender is None:
            raise AgentError("announcement_configuration", "Slack周知の接続を設定してください。")
        digest = self._digest(action)
        receipt = MeetingReceipt(
            key=key,
            plan_digest=digest,
            phase="creating_document" if action.retro_planning else "creating_event",
        )
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
        if action.retro_planning:
            if self.retro_documents is None:
                raise AgentError("retro_configuration", "レトプラのNotion設定を確認してね。")
            day = action.event.draft.period.start.astimezone(ZoneInfo("Asia/Tokyo")).date()
            page_id, url = self.retro_documents.create(day, action.retro_user_ids)
            receipt = receipt.model_copy(
                update={"document_id": page_id, "document_url": url, "phase": "document_created"}
            )
            self.receipts.save_meeting(receipt)
            try:
                ready = self.retro_documents.ready(page_id, day, action.retro_user_ids)
            except AgentError:
                ready = False
            if not ready:
                raise AgentError(
                    "retro_document_incomplete",
                    "Notionページを作成したけどテンプレートの反映を確認できなかったよ。"
                    f"予定とSlackは未作成なので、ページを確認してね: {url}",
                )
        event_action = self._linked_event(action, receipt.document_url)
        try:
            result = self.scheduling.execute(event_action, key)
        except AgentError as error:
            if receipt.document_url:
                raise AgentError(
                    "retro_calendar_incomplete",
                    "ドキュメントは作成済みで、予定の作成結果は未完了か確認できないよ。"
                    f"重複作成せずに確認してね: {receipt.document_url}",
                ) from error
            raise
        receipt = receipt.model_copy(update={"event": result["event"], "phase": "event_created"})
        self.receipts.save_meeting(receipt)
        receipt = receipt.model_copy(update={"phase": "posting"})
        self.receipts.save_meeting(receipt)
        try:
            ts = self.sender.send(
                action.announcement.channel_id,
                self._announcement(action, receipt.document_url),
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
        if action.retro_planning and not receipt.document_url:
            return None
        event_result = self.scheduling.reconcile(
            self._linked_event(action, receipt.document_url), key
        )
        if event_result is None:
            return None
        ts = self.sender.find(
            action.announcement.channel_id,
            self._announcement(action, receipt.document_url),
            key,
        )
        if ts is None and action.retro_planning and receipt.document_url:
            old_text = announcement_text(action.announcement, list(action.retro_slack_user_ids))
            old_text += (
                f"\nレトプラのドキュメント: {html.escape(receipt.document_url, quote=False)}"
            )
            ts = self.sender.find(action.announcement.channel_id, old_text, key)
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
    mention_names = action.retro_user_names if action.retro_planning else event.guest_names
    mentions = (
        "@channel"
        if action.announcement.mention_channel
        else ", ".join(html.escape(name, quote=False) for name in mention_names) or "なし"
    )
    retro = (
        f"Notion: テンプレートから [Retro & Planning] {start:%Y/%m/%d} を作成\n"
        f"NotionのUser: {', '.join(html.escape(n, quote=False) for n in action.retro_user_names)}\n"
        "作成したドキュメントのURLを予定の説明とSlack周知文に追加\n"
        if action.retro_planning
        else ""
    )
    return (
        f"予定作成・招待メール送信・Slack周知をまとめて承認する？\n\n"
        f"タイトル: {html.escape(event.draft.title, quote=False)}\n"
        f"日時: {start:%Y/%m/%d %H:%M}〜{end:%Y/%m/%d %H:%M}（日本時間）\n"
        f"カレンダー: {html.escape(event.calendar_alias, quote=False)}\n"
        f"イベントラベル: {label_text}\n招待先:\n{guests}\n"
        f"Google Meet: {'作成する' if event.draft.create_google_meet else '指定なし'}\n"
        f"予定の説明: {html.escape(event.draft.description or 'なし', quote=False)}\n"
        f"{retro}"
        f"周知先: #{action.announcement.channel_name} ({action.announcement.channel_id})\n"
        f"@channel: {'使用する' if action.announcement.mention_channel else '使用しない'}\n"
        f"メンション: {mentions}\n"
        # プレビューではメンションを発火させず、送信する文章を表示する。
        f"周知文:\n```\n{('@channel' + chr(10)) if action.announcement.mention_channel else ''}"
        f"{html.escape(action.announcement.text.replace('`', '｀'), quote=False)}\n```\n"
        "予定作成が確認できた後に周知するよ。"
    )
