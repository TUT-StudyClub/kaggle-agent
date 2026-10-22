from abc import ABC, abstractmethod
from typing import Any

from modules.knowledge.public import KnowledgeService
from modules.planning.public import Action, SkillBook
from modules.scheduling.public import SchedulingService
from shared.errors import AgentError


class PythonTool(ABC):
    """AIが返した型付き操作案だけを受けるPythonツール。任意コードは受け取らない。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def actions(self) -> frozenset[str]: ...

    @abstractmethod
    def execute(self, action: Action, source_url: str, key: str) -> dict[str, Any]: ...

    @abstractmethod
    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None: ...


class KnowledgeTool(PythonTool):
    def __init__(self, service: KnowledgeService):
        self.service = service

    @property
    def name(self) -> str:
        return "knowledge"

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(
            {
                "notion.search",
                "notion.read",
                "notion.create",
                "notion.append",
                "notion.update_block",
            }
        )

    def execute(self, action: Action, source_url: str, key: str) -> dict[str, Any]:
        return self.service.execute(action, source_url, key)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        return self.service.reconcile(action, key)


class SchedulingTool(PythonTool):
    def __init__(self, service: SchedulingService):
        self.service = service

    @property
    def name(self) -> str:
        return "scheduling"

    @property
    def actions(self) -> frozenset[str]:
        return frozenset(
            {
                "calendar.list",
                "calendar.events",
                "calendar.free",
                "calendar.create",
                "calendar.update",
            }
        )

    def execute(self, action: Action, source_url: str, key: str) -> dict[str, Any]:
        return self.service.execute(action, key)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        return self.service.reconcile(action, key)


class ToolRegistry:
    def __init__(self, tools: tuple[PythonTool, ...], skillbook: SkillBook):
        self.skillbook = skillbook
        self.tools: dict[str, PythonTool] = {}
        registered: dict[str, PythonTool] = {}
        for tool in tools:
            if tool.name in registered:
                raise ValueError(f"重複したPythonツール名: {tool.name}")
            registered[tool.name] = tool
            for kind in tool.actions:
                if kind in self.tools:
                    raise ValueError(f"重複したPythonツール: {kind}")
                self.tools[kind] = tool
        for name, permission in skillbook.policy.tools.items():
            if name not in registered or not permission.actions <= registered[name].actions:
                raise ValueError(f"未登録のPythonツール・操作を指定した設定: {name}")

    def execute(self, action: Action, source_url: str, key: str) -> dict[str, Any]:
        if not self.skillbook.permits(action.kind) or action.kind not in self.tools:
            raise AgentError("forbidden_tool", "このPythonツールの操作は設定で許可されていません。")
        return self.tools[action.kind].execute(action, source_url, key)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        if not self.skillbook.permits(action.kind) or action.kind not in self.tools:
            raise AgentError("forbidden_tool", "照合対象のツールが無効です。")
        return self.tools[action.kind].reconcile(action, key)
