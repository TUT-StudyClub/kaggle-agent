import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from modules.planning.infrastructure.codex_cli import CodexCLIPlanner
from modules.planning.infrastructure.skills import load_skillbook
from modules.planning.public import ResponseRequest
from shared.errors import AgentError


@pytest.mark.parametrize(
    "output,event,expected",
    [
        ('{"should_respond": true}', None, True),
        ('{"should_respond": false}', None, False),
        ('{"should_respond": "false"}', None, None),
        ('{"action": {"kind": "reply", "text": "回答"}}', None, None),
        ('{"should_respond": true}', "web_search", None),
        ('{"should_respond": true}', "mcp_tool_call", None),
        ('{"should_respond": true}', "command_execution", None),
    ],
)
def test_classifier_uses_separate_schema_rules_and_disables_tools(
    monkeypatch, output, event, expected
):
    book = load_skillbook()
    planner = CodexCLIPlanner(book)
    monkeypatch.setattr(planner, "check_auth", lambda: None)
    process = MagicMock()
    process.__enter__.return_value = process
    process.returncode = 0
    popen = Mock(return_value=process)
    monkeypatch.setattr("modules.planning.infrastructure.codex_cli.subprocess.Popen", popen)

    def respond(input_text, timeout):
        command = popen.call_args.args[0]
        assert 'web_search="disabled"' in command
        assert command[command.index("code_mode_host") - 1] == "--disable"
        rules = next(value for value in command if value.startswith("developer_instructions="))
        assert json.loads(rules.split("=", 1)[1]) == book.response_rules
        schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
        assert set(schema["properties"]) == {"should_respond"}
        payload = json.loads(input_text.split("\n", 1)[1])
        assert payload["context"] == {"dialogue": []} and payload["thread"] == []
        Path(command[command.index("--output-last-message") + 1]).write_text(output)
        return (
            json.dumps({"type": "item.completed", "item": {"type": event}}) if event else "",
            "",
        )

    process.communicate.side_effect = respond
    request = ResponseRequest(
        instruction="それって明日でも大丈夫？",
        requester_user_id="U1",
        context='{"dialogue": []}',
        thread="[]",
    )
    if expected is None:
        with pytest.raises(AgentError) as caught:
            planner.check_response(request)
        assert caught.value.code == "ai_invalid_output"
    else:
        assert planner.check_response(request).should_respond is expected
    assert planner.last_web_searches == []
