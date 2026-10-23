from abc import ABC, abstractmethod
from typing import Any

from modules.interaction.domain.models import Source
from modules.kaggle.public import KaggleService
from modules.knowledge.public import KnowledgeService
from modules.planning.public import Action, CreateMeeting, ListAnnouncementChannels, SkillBook
from modules.scheduling.public import SchedulingService
from shared.errors import AgentError

from .meetings import MeetingService


class PythonTool(ABC):
    """AIが返した型付き操作案だけを受けるPythonツール。任意コードは受け取らない。"""

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def actions(self) -> frozenset[str]: ...

    @abstractmethod
    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]: ...

    def prepare(self, action: Action, source: Source) -> None:
        """外部操作を伴わない事前検証。追加検証が必要なツールで上書きする。"""
        return None

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

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]:
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

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]:
        return self.service.execute(action, key)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        return self.service.reconcile(action, key)


class MeetingTool(PythonTool):
    def __init__(self, service: MeetingService):
        self.service = service

    @property
    def name(self) -> str:
        return "meetings"

    @property
    def actions(self) -> frozenset[str]:
        return frozenset({"slack.channels", "meeting.create"})

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]:
        if isinstance(action, ListAnnouncementChannels):
            return {"announcement_channels": self.service.channels()}
        if isinstance(action, CreateMeeting):
            return self.service.execute(action, key)
        raise AgentError("unsupported_action", "未対応の会議操作です。")

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        return self.service.reconcile(action, key) if isinstance(action, CreateMeeting) else None


class KaggleTool(PythonTool):
    def __init__(self, service: KaggleService | None):
        self.service = service

    @property
    def name(self) -> str:
        return "kaggle"

    @property
    def actions(self) -> frozenset[str]:
        return frozenset({"kaggle.help", "kaggle.read", "kaggle.write"})

    def _service(self) -> KaggleService:
        if self.service is None:
            raise AgentError("kaggle_configuration", "Kaggleツールが接続されていないよ。")
        return self.service

    def prepare(self, action: Action, source: Source) -> None:
        self._service().prepare(action, source.user_id)

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]:
        return self._service().execute(action, source.user_id if source else None, key)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        # Kaggle CLIには全操作共通の冪等キーがない。未確定の変更は再実行しない。
        return None


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

    def prepare(self, action: Action, source: Source) -> None:
        if not self.skillbook.permits(action.kind) or action.kind not in self.tools:
            raise AgentError("forbidden_tool", "このPythonツールの操作は設定で許可されていません。")
        self.tools[action.kind].prepare(action, source)

    def execute(
        self, action: Action, source_url: str, key: str, *, source: Source | None = None
    ) -> dict[str, Any]:
        if not self.skillbook.permits(action.kind) or action.kind not in self.tools:
            raise AgentError("forbidden_tool", "このPythonツールの操作は設定で許可されていません。")
        if isinstance(action, CreateMeeting) and not self.skillbook.permits("calendar.create"):
            raise AgentError("forbidden_tool", "予定作成がツール設定で許可されていません。")
        return self.tools[action.kind].execute(action, source_url, key, source=source)

    def reconcile(self, action: Action, key: str) -> dict[str, Any] | None:
        if not self.skillbook.permits(action.kind) or action.kind not in self.tools:
            raise AgentError("forbidden_tool", "照合対象のツールが無効です。")
        return self.tools[action.kind].reconcile(action, key)
