import tomllib
from pathlib import Path

import yaml

from modules.planning.domain.skills import Skill, SkillBook, SkillMetadata, ToolPolicy


def read_skill(path: Path, *, rules: str) -> Skill:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"YAML frontmatterが必要です: {path}")
    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        raise ValueError(f"YAML frontmatterの終端がありません: {path}")
    try:
        raw = yaml.safe_load("".join(lines[1:end]))
    except yaml.YAMLError:
        raise ValueError(f"YAML frontmatterを読み取れません: {path}") from None
    metadata = SkillMetadata.model_validate(raw)
    if metadata.name != path.parent.name:
        raise ValueError(f"スキル名とディレクトリ名が一致しません: {path}")
    return Skill(
        **metadata.model_dump(), rules=rules, instructions="".join(lines[end + 1 :]).strip()
    )


def load_skillbook(
    skills_root: Path | None = None,
    policy_path: Path | None = None,
    *,
    rules_root: Path | None = None,
) -> SkillBook:
    resources = Path(__file__).resolve().parents[1] / "resources"
    bundled_policy = resources / "tools.toml"
    # ソースではリポジトリ直下を読み、wheelでは同梱した同じファイルを読む。
    project = Path(__file__).resolve().parents[4]
    skills_root = skills_root or (
        resources / "skills" if bundled_policy.exists() else project / "skills"
    )
    policy_path = policy_path or (
        bundled_policy if bundled_policy.exists() else project / "config" / "tools.toml"
    )
    rules_root = rules_root or (resources if bundled_policy.exists() else project) / "rules"
    with policy_path.open("rb") as stream:
        policy = ToolPolicy.model_validate(tomllib.load(stream))

    def rule(name: str) -> str:
        return (rules_root / f"{name}.md").read_text(encoding="utf-8").strip()

    tool_skills = {tool.skill for tool in policy.tools.values() if tool.enabled}
    # ツールを持たない独立スキルも読み込む。
    configured_skills = {tool.skill for tool in policy.tools.values()}
    independent_skills = {
        path.parent.name
        for path in skills_root.glob("*/SKILL.md")
        if path.parent.name not in configured_skills
    }
    skills = tuple(
        read_skill(
            skills_root / name / "SKILL.md",
            # ツールに紐づくスキルはルール必須。独立スキルはルールがなければ共通ルールに従う。
            rules=rule(name)
            if (rules_root / f"{name}.md").exists() or name in tool_skills
            else "共通ルールに従います。",
        )
        for name in sorted(tool_skills | independent_skills)
    )
    return SkillBook(
        rules=rule("common"),
        response_rules=rule("response"),
        skills=skills,
        policy=policy,
        research_rules=rule("research") if policy.web_search != "disabled" else "",
    )
