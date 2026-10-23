from typing import Literal

from pydantic import Field, model_validator

from shared.domain.value_object import ValueObject


class SkillMetadata(ValueObject):
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1)


class Skill(SkillMetadata):
    rules: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


class ToolPermission(ValueObject):
    enabled: bool
    skill: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    actions: frozenset[str] = Field(min_length=1)


class ToolPolicy(ValueObject):
    web_search: Literal["disabled", "live"] = "disabled"
    tools: dict[str, ToolPermission] = Field(min_length=1)

    @property
    def enabled_actions(self) -> frozenset[str]:
        return frozenset(
            action for tool in self.tools.values() if tool.enabled for action in tool.actions
        )


class SkillBook(ValueObject):
    rules: str = Field(min_length=1)
    response_rules: str = Field(min_length=1)
    skills: tuple[Skill, ...]
    policy: ToolPolicy
    research_rules: str = ""

    @model_validator(mode="after")
    def require_research_rules(self) -> SkillBook:
        if self.policy.web_search != "disabled" and not self.research_rules.strip():
            raise ValueError("Web検索を有効にする場合は調査ルールが必要です。")
        return self

    def permits(self, action_kind: str) -> bool:
        return action_kind in {"reply", "clarify"} or action_kind in self.policy.enabled_actions

    def prompt(self) -> str:
        sections = ["# 共通ルール\n" + self.rules]
        if self.policy.web_search != "disabled":
            sections.append("# 公開Web調査（組込みWeb検索が利用可能）\n" + self.research_rules)
        for skill in self.skills:
            operations = "\n".join(
                f"- {name}: {', '.join(sorted(tool.actions))}"
                for name, tool in sorted(self.policy.tools.items())
                if tool.enabled and tool.skill == skill.name
            )
            sections.append(
                f"# スキル: {skill.name}\n{skill.description}\n\n"
                f"## 利用できるPythonツールの操作\n{operations}\n\n"
                f"## 適用ルール\n{skill.rules}\n\n## 手順書\n{skill.instructions}"
            )
        return "\n\n".join(sections)
