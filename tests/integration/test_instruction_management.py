import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest

from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.models import ApprovalReaction, JobStatus
from modules.interaction.infrastructure.instruction_tool import InstructionTool
from modules.interaction.infrastructure.messaging.slack import SlackMessenger
from modules.planning.infrastructure.skills import load_skillbook
from modules.planning.public import Plan, UpdateInstructions
from shared.errors import AgentError


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def test_manager_alone_approves_and_change_is_rechecked(harness, tmp_path: Path):
    rules = tmp_path / "rules"
    skills = tmp_path / "skills"
    rules.mkdir()
    (skills / "instruction-management").mkdir(parents=True)
    (rules / "common.md").write_text("共通")
    (rules / "response.md").write_text("応答")
    (rules / "instruction-management.md").write_text("管理")
    (skills / "instruction-management" / "SKILL.md").write_text(
        "---\nname: instruction-management\ndescription: 管理\n---\n\n管理する。"
    )
    target = rules / "common.md"
    policy_path = tmp_path / "tools.toml"
    policy_path.write_text(
        '[tools.instructions]\nenabled = true\nskill = "instruction-management"\n'
        'actions = ["instructions.read", "instructions.update"]\n'
    )
    book = load_skillbook(skills, policy_path, rules_root=rules)
    tool = InstructionTool(tmp_path)
    registry = ToolRegistry((tool,), book)
    policy = harness.policy.model_copy(
        update={"user_ids": frozenset({"U1", "U2"}), "kaggle_owner_user_id": "U2"}
    )
    harness.configure(policy=policy, tools=registry)
    harness.planner.steps = [
        Plan(
            action=UpdateInstructions(
                path="rules/common.md", previous_sha256=digest("共通"), content="変更"
            )
        )
    ]
    job_id = harness.submit()
    harness.agent.run_once()
    job = harness.jobs.get(job_id)
    assert job.status == JobStatus.AWAITING_APPROVAL
    assert target.read_text() == "共通"
    notice_id, source, text = harness.jobs.pending_notifications()[0]
    assert "<@U2>" in text
    client = Mock()
    client.chat_postMessage.return_value = {"ts": "2000.000001"}
    SlackMessenger(client, policy, harness.jobs).deliver(notice_id, source, text)
    reaction = ApprovalReaction(
        team_id="T1", channel_id="C1", message_ts="2000.000001", user_id="U1", reaction="thumbsup"
    )
    assert not harness.agent.receive_reaction(reaction)
    assert harness.agent.receive_reaction(reaction.model_copy(update={"user_id": "U2"}))
    harness.agent.run_once()
    assert harness.jobs.get(job_id).status == JobStatus.SUCCEEDED
    assert target.read_text() == "変更"
    stale = UpdateInstructions(
        path="rules/common.md", previous_sha256=digest("共通"), content="上書き"
    )
    with pytest.raises(AgentError, match="内容が変わった"):
        tool.prepare(stale, harness.source)
    assert target.read_text() == "変更"


def test_rejects_paths_outside_instruction_files(tmp_path: Path, harness):
    tool = InstructionTool(tmp_path)
    with pytest.raises(AgentError):
        tool.prepare(
            UpdateInstructions(path="../.env", previous_sha256="missing", content="x"),
            harness.source,
        )


def test_new_standalone_skill_is_loaded_and_bad_format_is_rejected(tmp_path: Path, harness):
    rules = tmp_path / "rules"
    skills = tmp_path / "skills"
    rules.mkdir()
    skills.mkdir()
    (rules / "common.md").write_text("共通")
    (rules / "response.md").write_text("応答")
    policy = tmp_path / "tools.toml"
    policy.write_text(
        '[tools.disabled]\nenabled = false\nskill = "disabled"\nactions = ["unused"]\n'
    )
    tool = InstructionTool(tmp_path)
    action = UpdateInstructions(
        path="skills/new-skill/SKILL.md",
        previous_sha256="missing",
        content="---\nname: new-skill\ndescription: 新規\n---\n\n新しい手順。",
    )
    tool.execute(action, "", "job", source=harness.source)
    book = load_skillbook(skills, policy, rules_root=rules)
    assert "新しい手順。" in book.prompt()
    invalid = action.model_copy(update={"path": "skills/other/SKILL.md"})
    with pytest.raises(AgentError):
        tool.prepare(invalid, harness.source)
