from pathlib import Path

from slack_sdk import WebClient

from modules.knowledge.public import KnowledgeService
from modules.planning.public import Planner, SkillBook
from modules.scheduling.public import SchedulingService
from shared.application.ports.clock import Clock
from shared.infrastructure.clock import SystemClock

from .application.facade import InteractionFacade
from .application.policies.access import AgentPolicy
from .application.ports.messenger import Messenger
from .application.services.actions import ActionService
from .application.services.activity import ActivityReporter
from .application.services.completion import JobCompletion
from .application.services.jobs import JobManagement
from .application.services.tools import KnowledgeTool, SchedulingTool, ToolRegistry
from .application.use_cases.deliver_notifications import DeliverNotificationsUseCase
from .application.use_cases.process_job import ProcessJobUseCase
from .application.use_cases.receive_message import ReceiveMessageUseCase
from .application.use_cases.reconcile_job import ReconcileJobUseCase
from .domain.repositories import JobRepository
from .infrastructure.messaging.slack import SlackMessenger
from .infrastructure.persistence.sqlite import SQLiteJobRepository


def build_agent(
    policy: AgentPolicy,
    jobs: JobRepository,
    planner: Planner,
    messenger: Messenger,
    knowledge: KnowledgeService,
    scheduling: SchedulingService,
    skillbook: SkillBook,
    *,
    clock: Clock | None = None,
    tools: ToolRegistry | None = None,
) -> InteractionFacade:
    clock = clock or SystemClock()
    tools = tools or ToolRegistry((KnowledgeTool(knowledge), SchedulingTool(scheduling)), skillbook)
    activity = ActivityReporter(messenger)
    actions = ActionService(policy, jobs, messenger, knowledge, scheduling, tools, activity)
    completion = JobCompletion(jobs)
    return InteractionFacade(
        ReceiveMessageUseCase(policy, jobs, messenger, clock),
        ProcessJobUseCase(
            policy,
            jobs,
            planner,
            messenger,
            scheduling,
            tools,
            actions,
            completion,
            activity,
            clock,
        ),
        ReconcileJobUseCase(jobs, tools),
        DeliverNotificationsUseCase(jobs, messenger, activity, clock),
        JobManagement(jobs),
    )


def build_interaction(
    database: Path,
    policy: AgentPolicy,
    client: WebClient,
    planner: Planner,
    knowledge: KnowledgeService,
    scheduling: SchedulingService,
    skillbook: SkillBook,
    *,
    clock: Clock | None = None,
) -> InteractionFacade:
    jobs = SQLiteJobRepository(database)
    messenger = SlackMessenger(client, policy, jobs)
    return build_agent(
        policy, jobs, planner, messenger, knowledge, scheduling, skillbook, clock=clock
    )


def build_job_management(database: Path) -> JobManagement:
    return JobManagement(SQLiteJobRepository(database))
