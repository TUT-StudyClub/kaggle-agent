from unittest.mock import create_autospec

import pytest
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

from modules.interaction.infrastructure.messaging.slack_reminders import SlackReminderSender
from shared.errors import AgentError


def test_complete_channel_membership_excludes_bots_apps_and_deactivated_accounts():
    client = create_autospec(WebClient, instance=True)
    client.conversations_members.side_effect = [
        {"members": ["U1", "UBOT", "UAPP"], "response_metadata": {"next_cursor": "next"}},
        {"members": ["U1", "U2", "UDELETED", "USLACKBOT"]},
    ]

    def profile(*, user):
        return {
            "user": {
                "id": user,
                "is_bot": user in {"UBOT", "USLACKBOT"},
                "is_app_user": user == "UAPP",
                "deleted": user == "UDELETED",
            }
        }

    client.users_info.side_effect = profile
    actual = SlackReminderSender(client, frozenset({"C1"})).human_members("C1")
    assert actual == {"U1", "U2"}
    assert client.conversations_members.call_args_list[1].kwargs["cursor"] == "next"
    assert client.users_info.call_count == 5
    client.chat_postMessage.assert_not_called()


@pytest.mark.parametrize("kind", ["cycle", "limit", "invalid_user", "missing_bot_flag"])
def test_incomplete_membership_is_never_used_to_decide_channel_mention(kind):
    client = create_autospec(WebClient, instance=True)
    if kind in {"cycle", "limit"}:
        count = 0

        def page(**kwargs):
            nonlocal count
            count += 1
            return {
                "members": ["U1"],
                "response_metadata": {
                    "next_cursor": "same" if kind == "cycle" else str(count),
                },
            }

        client.conversations_members.side_effect = page
    else:
        client.conversations_members.return_value = {"members": ["U1"]}
        client.users_info.return_value = (
            {"user": {"id": "UOTHER", "is_bot": False}}
            if kind == "invalid_user"
            else {"user": {"id": "U1"}}
        )
    with pytest.raises(AgentError) as caught:
        SlackReminderSender(client, frozenset({"C1"})).human_members("C1")
    assert caught.value.code == "slack_members_incomplete"
    client.chat_postMessage.assert_not_called()


def test_missing_users_read_scope_is_actionable_and_does_not_reveal_response():
    client = create_autospec(WebClient, instance=True)
    client.conversations_members.return_value = {"members": ["U1"]}
    response = SlackResponse(
        client=client,
        http_verb="GET",
        api_url="https://slack.test/",
        req_args={},
        data={"ok": False, "error": "missing_scope"},
        headers={},
        status_code=200,
    )
    client.users_info.side_effect = SlackApiError("private-error", response)
    with pytest.raises(AgentError) as caught:
        SlackReminderSender(client, frozenset({"C1"})).human_members("C1")
    assert caught.value.code == "slack_members_permission"
    assert "users:read" in caught.value.message
    assert "private-error" not in str(caught.value)


def test_members_outside_allowlist_are_not_read():
    client = create_autospec(WebClient, instance=True)
    with pytest.raises(AgentError):
        SlackReminderSender(client, frozenset({"C1"})).human_members("COTHER")
    client.conversations_members.assert_not_called()
