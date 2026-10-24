from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.models import (
    Job,
)
from modules.interaction.domain.repositories import JobRepository
from modules.kaggle.public import KaggleHelp, ReadKaggle, WriteKaggle
from modules.knowledge.public import (
    AppendPage,
    CreatePage,
    KnowledgeService,
    UpdateBlock,
)
from modules.planning.public import (
    WRITE_KINDS,
    CreateMeeting,
    Plan,
)
from modules.scheduling.public import (
    CreateEvent,
    SchedulingService,
    UpdateEvent,
)
from shared.errors import AgentError

from .meetings import MeetingService


class ActionService:
    def __init__(
        self,
        settings: AgentPolicy,
        jobs: JobRepository,
        messenger: Messenger,
        knowledge: KnowledgeService,
        scheduling: SchedulingService,
        tools: ToolRegistry,
        activity: ActivityReporter,
        meetings: MeetingService,
    ):
        self.settings = settings
        self.jobs = jobs
        self.messenger = messenger
        self.knowledge = knowledge
        self.scheduling = scheduling
        self.tools = tools
        self.activity = activity
        self.meetings = meetings

    def execute(self, job: Job, plan: Plan) -> dict[str, object]:
        action = plan.action
        self._authorize(job, plan)
        activity = (
            Activity.WRITING
            if action.kind in WRITE_KINDS
            else Activity.NOTION
            if action.kind.startswith("notion.")
            else Activity.CALENDAR
        )
        self.activity.update(job.message.source, activity)
        if action.kind.startswith("notion."):
            result = self.tools.execute(
                action,
                self.messenger.permalink(job.message.source),
                job.id,
                source=job.message.source,
            )
            if isinstance(action, (CreatePage, AppendPage)) and action.record_thread:
                page = result.get("page")
                if isinstance(page, dict):
                    self.jobs.map_page(job.message.source.thread_key, str(page["id"]))
            return result
        return self.tools.execute(action, "", job.id, source=job.message.source)

    def prepare(self, job: Job, plan: Plan) -> Plan:
        action = plan.action
        self._authorize(job, plan)
        self.tools.prepare(action, job.message.source)
        if isinstance(action, CreateMeeting):
            if not self.tools.skillbook.permits("calendar.create"):
                raise AgentError("forbidden_tool", "予定作成がツール設定で許可されていません。")
            action = self.meetings.prepare(action)
        if isinstance(action, (CreatePage, AppendPage)) and action.record_thread:
            mapped = self.jobs.mapping(job.message.source.thread_key)
            unresolved = self.jobs.unresolved_record(job.message.source.thread_key)
            if unresolved:
                raise AgentError(
                    "unresolved_record",
                    f"前回の記録結果が未確定です。ジョブ {unresolved} を先に照合してください。",
                )
            if mapped:
                action = AppendPage(
                    page_id=mapped, title=action.title, body=action.body, record_thread=True
                )
        if isinstance(action, (CreateEvent, UpdateEvent)):
            action = self.scheduling.prepare(action)
        # Notion対象の権限検証は読取りで先行。実行時にも再検証する。
        if isinstance(action, (AppendPage, UpdateBlock)):
            self.knowledge.ensure_page_access(action.page_id)
        if isinstance(action, CreatePage):
            if action.parent_alias not in self.settings.notion_parents:
                raise AgentError("unknown_parent", "Notion登録先の別名を指定してください。")
        return Plan(action=action)

    def _authorize(self, job: Job, plan: Plan) -> None:
        if isinstance(plan.action, WriteKaggle) and (
            self.settings.kaggle_owner_user_id is None
            or job.message.source.user_id != self.settings.kaggle_owner_user_id
        ):
            raise AgentError(
                "kaggle_owner_only",
                "Kaggleの提出・公開・削除・作成・実行・更新は、管理者本人だけが実行できるよ。",
            )

    def input_feedback(self, job: Job, plan: Plan, error: AgentError) -> dict[str, object] | None:
        """実行前の入力不備だけを、再計画に使える許可済みツールのヘルプへ戻す。"""
        action = plan.action
        if not isinstance(action, (ReadKaggle, WriteKaggle)) or error.code not in {
            "kaggle_arguments",
            "kaggle_command",
            "kaggle_path",
        }:
            return None
        self._authorize(job, plan)
        help_result = self.tools.execute(
            KaggleHelp(command="" if error.code == "kaggle_command" else action.command),
            "",
            job.id,
            source=job.message.source,
        )
        return {
            "tool_error": {
                "code": error.code,
                "message": error.message,
                "command": action.command,
                "arguments": action.arguments,
                "executed": False,
            },
            "kaggle_help": help_result,
        }
