from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.models import (
    Job,
)
from modules.interaction.domain.repositories import JobRepository
from modules.knowledge.public import (
    AppendPage,
    CreatePage,
    KnowledgeService,
    UpdateBlock,
)
from modules.planning.public import (
    WRITE_KINDS,
    Plan,
)
from modules.scheduling.public import (
    CreateEvent,
    SchedulingService,
    UpdateEvent,
)
from shared.errors import AgentError


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
    ):
        self.settings = settings
        self.jobs = jobs
        self.messenger = messenger
        self.knowledge = knowledge
        self.scheduling = scheduling
        self.tools = tools
        self.activity = activity

    def execute(self, job: Job, plan: Plan) -> dict[str, object]:
        action = plan.action
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
            )
            if isinstance(action, (CreatePage, AppendPage)) and action.record_thread:
                page = result.get("page")
                if isinstance(page, dict):
                    self.jobs.map_page(job.message.source.thread_key, str(page["id"]))
            return result
        return self.tools.execute(action, "", job.id)

    def prepare(self, job: Job, plan: Plan) -> Plan:
        action = plan.action
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
