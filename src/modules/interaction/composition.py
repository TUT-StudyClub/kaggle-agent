from pathlib import Path

from slack_sdk import WebClient

from modules.kaggle.public import KaggleService
from modules.knowledge.public import KnowledgeService
from modules.planning.public import Planner, SkillBook
from modules.scheduling.public import SchedulingService
from shared.application.ports.clock import Clock
from shared.infrastructure.clock import SystemClock

from .application.dto.reminders import DailyScheduleReminder
from .application.facade import InteractionFacade
from .application.policies.access import AgentPolicy
from .application.ports.announcements import AnnouncementSender
from .application.ports.messenger import Messenger
from .application.services.actions import ActionService
from .application.services.activity import ActivityReporter
from .application.services.completion import JobCompletion
from .application.services.jobs import JobManagement
from .application.services.meetings import MeetingService
from .application.services.tools import (
    KaggleTool,
    KnowledgeTool,
    MeetingTool,
    SchedulingTool,
    ToolRegistry,
)
from .application.use_cases.deliver_notifications import DeliverNotificationsUseCase
from .application.use_cases.process_job import ProcessJobUseCase
from .application.use_cases.receive_message import ReceiveMessageUseCase
from .application.use_cases.receive_reaction import ReceiveReactionUseCase
from .application.use_cases.reconcile_job import ReconcileJobUseCase
from .application.use_cases.remind_schedule import RemindScheduleUseCase
from .domain.meetings import MeetingRepository
from .domain.repositories import JobRepository
from .infrastructure.messaging.slack import SlackMessenger
from .infrastructure.messaging.slack_announcements import SlackAnnouncementSender
from .infrastructure.messaging.slack_reminders import SlackReminderSender
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
    kaggle: KaggleService | None = None,
    reminders: RemindScheduleUseCase | None = None,
    announcements: AnnouncementSender | None = None,
    meeting_receipts: MeetingRepository | None = None,
) -> InteractionFacade:
    clock = clock or SystemClock()
    meetings = MeetingService(scheduling, announcements, meeting_receipts)
    tools = tools or ToolRegistry(
        (
            KnowledgeTool(knowledge),
            SchedulingTool(scheduling),
            MeetingTool(meetings),
            KaggleTool(kaggle),
        ),
        skillbook,
    )
    activity = ActivityReporter(messenger)
    actions = ActionService(
        policy, jobs, messenger, knowledge, scheduling, tools, activity, meetings
    )
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
        reminders,
        reactions=ReceiveReactionUseCase(policy, jobs, clock),
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
    daily_schedule_reminder: DailyScheduleReminder | None = None,
    bot_user_id: str | None = None,
    kaggle: KaggleService | None = None,
) -> InteractionFacade:
    clock = clock or SystemClock()
    jobs = SQLiteJobRepository(database)
    messenger = SlackMessenger(client, policy, jobs, bot_user_id=bot_user_id)
    reminders = None
    if daily_schedule_reminder is not None:
        reminders = RemindScheduleUseCase(
            daily_schedule_reminder,
            policy.team_id,
            scheduling,
            jobs,
            SlackReminderSender(client, policy.channel_ids),
            clock,
        )
    return build_agent(
        policy,
        jobs,
        planner,
        messenger,
        knowledge,
        scheduling,
        skillbook,
        clock=clock,
        reminders=reminders,
        announcements=SlackAnnouncementSender(client, policy.channel_ids, bot_user_id),
        meeting_receipts=jobs,
        kaggle=kaggle,
    )


def build_job_management(database: Path) -> JobManagement:
    return JobManagement(SQLiteJobRepository(database))
