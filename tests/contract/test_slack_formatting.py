from unittest.mock import Mock

import pytest
from slack_sdk.errors import SlackApiError, SlackClientError

from modules.interaction.application.services.meetings import announcement_text
from modules.interaction.infrastructure.messaging.formatting import message_payload
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.interaction.infrastructure.messaging.slack_announcements import SlackAnnouncementSender
from modules.interaction.infrastructure.messaging.slack_reminders import SlackReminderSender
from modules.planning.public import MeetingAnnouncement
from shared.errors import AgentError
from tests.conftest import Harness


def elements(payload):
    return payload["blocks"][0]["elements"]


@pytest.mark.parametrize("marker", ["-", "+", "*", "•", "1.", "3)"])
def test_list_markers_become_native_slack_lists(marker):
    text = f"予定\n\n{marker} **打ち合わせ**\n{marker} `資料を確認`"
    payload = message_payload(text)
    assert payload["text"] == text  # 通知・スクリーンリーダー用の全文。
    native = elements(payload)[1]
    assert native["type"] == "rich_text_list"
    assert native["style"] == ("ordered" if marker[0].isdigit() else "bullet")
    assert len(native["elements"]) == 2
    assert native["elements"][0]["elements"] == [
        {"type": "text", "text": "打ち合わせ", "style": {"bold": True}}
    ]
    assert native["elements"][1]["elements"][0]["style"] == {"code": True}


def test_nested_lists_keep_indentation_numbering_and_continuation_lines():
    payload = message_payload("3. 親\n   続き\n   - 子\n     1. 孫\n4. 次の親")
    lists = elements(payload)
    assert [item["indent"] for item in lists] == [0, 1, 2, 0]
    assert [item["style"] for item in lists] == ["ordered", "bullet", "ordered", "ordered"]
    assert lists[0]["offset"] == 2 and lists[3]["offset"] == 3
    assert "".join(part["text"] for part in lists[0]["elements"][0]["elements"]) == "親\n続き"


def test_paragraph_and_code_after_nested_list_keep_their_original_order():
    payload = message_payload("- 親\n  - 子\n\n  補足\n\n  ```\n  • code\n  ```\n- 次")
    rendered = elements(payload)
    assert [part["type"] for part in rendered] == [
        "rich_text_list",
        "rich_text_list",
        "rich_text_section",
        "rich_text_preformatted",
        "rich_text_list",
    ]
    assert rendered[2]["elements"][0]["text"] == "補足"
    assert rendered[3]["elements"][0]["text"] == "• code\n"


def test_mentions_links_and_escaped_source_text_survive_conversion():
    text = (
        "• 予定 <https://example.com/?a=1&amp;b=2|予定を開く>\n"
        "  担当者: <@U1> <!channel> <#C1|team>\n"
        "• &lt;@U2&gt; &lt;!here&gt; &amp; `<@U3>`\n"
        "• [**参照**](https://example.com)"
    )
    native = elements(message_payload(text, slack_markup=True))[0]["elements"]
    first = native[0]["elements"]
    assert {"type": "link", "url": "https://example.com/?a=1&b=2", "text": "予定を開く"} in first
    assert {"type": "user", "user_id": "U1"} in first
    assert {"type": "broadcast", "range": "channel"} in first
    assert {"type": "channel", "channel_id": "C1"} in first
    second = native[1]["elements"]
    assert all(part["type"] == "text" for part in second)
    assert second[-1] == {"type": "text", "text": "<@U3>", "style": {"code": True}}
    assert native[2]["elements"] == [{"type": "link", "url": "https://example.com", "text": "参照"}]


@pytest.mark.parametrize(
    "code",
    ["```text\n• サンプル\n- 引数\n```", "~~~\n• サンプル\n~~~", "    • コード", "`\n• コード\n`"],
)
def test_code_examples_are_not_lists_and_their_content_is_not_modified(code):
    assert "blocks" not in message_payload(code)
    payload = message_payload("- 実際の項目\n\n説明\n\n" + code)
    assert elements(payload)[0]["type"] == "rich_text_list"
    rendered = elements(payload)[2]
    assert "•" in "".join(part["text"] for part in rendered["elements"])
    assert all(item["type"] != "rich_text_list" for item in elements(payload)[1:])


def test_long_list_message_keeps_every_item_and_approval_command():
    text = "予定\n\n" + "• あいうえお\n" * 2000 + "\n`承認 job-id`"
    assert len(text) > 12000
    payload = message_payload(text)
    assert payload["text"] == text
    assert len(elements(payload)[1]["elements"]) == 2000
    assert elements(payload)[2]["elements"][0]["text"] == "承認 job-id"


@pytest.mark.parametrize("notice", [None, "job/failed", "job/awaiting_approval", "job/succeeded"])
def test_every_reply_path_uses_native_lists(harness: Harness, notice):
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    client.chat_startStream.return_value = {"ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    text = "確認\n\n- 候補1\n- 候補2"
    if notice is None:
        messenger.send(harness.source, text)
    else:
        messenger.deliver(notice, harness.source, text)
    call = client.chat_update if notice == "job/succeeded" else client.chat_postMessage
    assert elements(call.call_args.kwargs)[1]["type"] == "rich_text_list"
    if notice in {"job/awaiting_approval", "job/succeeded"}:
        messenger.deliver(notice, harness.source, text)
        call.assert_called_once()


def test_interrupted_native_list_update_repairs_same_stream(harness: Harness):
    client = Mock()
    client.chat_startStream.return_value = {"ts": "2000.000001"}
    client.chat_update.side_effect = [SlackClientError("response lost"), {"ok": True}]
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    text = "- 確認済みの項目\n- 次の項目"
    with pytest.raises(AgentError, match="中断"):
        messenger.deliver("job/succeeded", harness.source, text)
    delivery = harness.jobs.get_delivery("job/succeeded")
    assert delivery and delivery.state == "open"
    messenger.deliver("job/succeeded", harness.source, text)
    client.chat_startStream.assert_called_once()
    client.chat_postMessage.assert_not_called()
    assert client.chat_update.call_count == 2
    assert client.chat_update.call_args_list[0] == client.chat_update.call_args_list[1]
    assert elements(client.chat_update.call_args.kwargs)[0]["type"] == "rich_text_list"
    delivery = harness.jobs.get_delivery("job/succeeded")
    assert delivery and delivery.state == "done"


def test_unavailable_stream_falls_back_to_a_native_list_once(harness: Harness):
    client = Mock()
    client.chat_startStream.side_effect = SlackApiError("unsupported", {"error": "missing_scope"})
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    messenger = SlackMessenger(client, harness.policy, harness.jobs)
    messenger.deliver("job/succeeded", harness.source, "• 項目")
    messenger.deliver("job/succeeded", harness.source, "• 項目")
    client.chat_postMessage.assert_called_once()
    assert elements(client.chat_postMessage.call_args.kwargs)[0]["type"] == "rich_text_list"


def test_reminders_have_native_lists_and_real_recipient_mentions():
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    SlackReminderSender(client, frozenset({"C1"})).send(
        "C1", "今日のタスク\n• 調査\n  担当者: <@U1>"
    )
    payload = client.chat_postMessage.call_args.kwargs
    native = elements(payload)[1]
    assert native["type"] == "rich_text_list"
    assert {"type": "user", "user_id": "U1"} in native["elements"][0]["elements"]
    assert payload["parse"] == "none" and payload["link_names"] is False


def test_announcements_only_notify_approved_channel_mention():
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    draft = MeetingAnnouncement(channel_id="C1", text="- 議題 <!here> <@U2>", mention_channel=True)
    text = announcement_text(draft)
    SlackAnnouncementSender(client, frozenset({"C1"}), "UBOT").send("C1", text, "meeting")
    payload = client.chat_postMessage.call_args.kwargs
    assert payload["text"] == text
    assert elements(payload)[0]["elements"] == [{"type": "broadcast", "range": "channel"}]
    native = elements(payload)[1]
    assert native["type"] == "rich_text_list"
    assert all(part["type"] == "text" for part in native["elements"][0]["elements"])
