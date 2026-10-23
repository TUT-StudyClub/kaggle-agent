from dataclasses import dataclass
from pathlib import Path

import pytest

from modules.interaction.application.ports.announcements import AnnouncementSender
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.composition import build_agent
from modules.interaction.domain.models import IncomingMessage, Source
from modules.interaction.infrastructure.persistence.sqlite import SQLiteJobRepository
from modules.interaction.public import AgentPolicy
from modules.interaction.public import InteractionFacade as Agent
from modules.knowledge.composition import compose_knowledge
from modules.knowledge.domain.models import Note, Page
from modules.knowledge.public import KnowledgeRepository
from modules.planning.infrastructure.skills import load_skillbook
from modules.planning.public import (
    Plan,
    Planner,
    PlanningRequest,
    ResponseDecision,
    ResponseRequest,
    SkillBook,
)
from modules.scheduling.composition import compose_scheduling
from modules.scheduling.domain.models import Calendar, Event, EventDraft, TimeRange
from modules.scheduling.domain.repositories import MemberDirectory
from modules.scheduling.public import CalendarConfig, CalendarRepository
from shared.application.ports.clock import Clock
from shared.errors import AgentError, UncertainWrite


class ScriptedPlanner(Planner):
    def __init__(self):
        self.steps: list[Plan | AgentError] = []
        self.requests: list[PlanningRequest] = []
        self.response_requests: list[ResponseRequest] = []
        self.response_steps: list[bool | AgentError] = []

    def check_response(self, request: ResponseRequest) -> ResponseDecision:
        self.response_requests.append(request)
        step = self.response_steps.pop(0)
        if isinstance(step, AgentError):
            raise step
        return ResponseDecision(should_respond=step)

    def plan(self, request: PlanningRequest) -> Plan:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, AgentError):
            raise step
        return step


class FakeMessenger(Messenger):
    def __init__(self):
        self.sent: list[str] = []
        self.thread_reads = 0
        self.unavailable = False
        self.activities: list[Activity] = []
        self.thread_text = "[]"

    def status(self, source: Source, activity: Activity) -> None:
        self.activities.append(activity)

    def send(self, source: Source, text: str) -> None:
        if self.unavailable:
            raise AgentError("slack_send", "offline")
        self.sent.append(text)

    def thread(self, source: Source) -> str:
        self.thread_reads += 1
        return self.thread_text

    def permalink(self, source: Source) -> str:
        return "https://example.slack.com/archives/C1/p1000000001"


class FakeKnowledge(KnowledgeRepository):
    def __init__(self):
        self.writes = 0
        self.pages = {
            "page1": Page(
                id="page1",
                title="テスト",
                url="https://notion.so/page1",
                text="機微な原文",
                revision="v1",
            )
        }
        self.receipts: dict[str, str] = {}
        self.uncertain = False

    def search(self, query: str) -> list[Page]:
        return list(self.pages.values())

    def read(self, page_id: str) -> Page:
        return self.pages[page_id]

    def create(self, parent_alias: str, note: Note, key: str) -> Page:
        self.writes += 1
        page = Page(
            id=f"page{self.writes + 1}",
            title=note.title,
            url=f"https://notion.so/page{self.writes + 1}",
        )
        self.pages[page.id] = page
        self.receipts[key] = page.id
        if self.uncertain:
            raise UncertainWrite()
        return page

    def append(self, page_id: str, note: Note, key: str) -> Page:
        self.writes += 1
        self.receipts[key] = page_id
        return self.pages[page_id]

    def update_block(self, page_id: str, block_id: str, before: str, after: str, key: str) -> Page:
        self.writes += 1
        return self.pages[page_id]

    def find_created(self, parent_alias: str, key: str) -> Page | None:
        page_id = self.receipts.get(key)
        return self.pages.get(page_id) if page_id else None

    def find_marker(self, page_id: str, key: str) -> bool:
        return self.receipts.get(key) == page_id

    def block_matches(self, page_id: str, block_id: str, text: str) -> bool:
        return False


class FakeCalendar(CalendarRepository):
    def __init__(self):
        self.writes = 0
        self.role = "owner"
        self.editable = True
        self.etag = "v1"
        self.stored: dict[str, Event] = {}

    def calendars(self) -> list[Calendar]:
        return [
            Calendar(
                alias="personal",
                id="test@example.com",
                name="個人",
                role=self.role,
                owned=self.role == "owner",
            )
        ]

    def events(self, alias: str, period: TimeRange) -> list[Event]:
        return list(self.stored.values())

    def busy(self, aliases: list[str], period: TimeRange) -> list[TimeRange]:
        return []

    def event(self, alias: str, event_id: str) -> Event:
        if event_id in self.stored:
            return self.stored[event_id].model_copy(
                update={"etag": self.etag, "editable": self.editable}
            )
        return Event(
            id=event_id,
            calendar_alias=alias,
            title="面談",
            start="2026-10-23T18:00:00+09:00",
            end="2026-10-23T18:30:00+09:00",
            url="https://calendar.google.com/event/test",
            etag=self.etag,
            editable=self.editable,
        )

    def create(self, alias: str, draft: EventDraft, key: str) -> Event:
        self.writes += 1
        event = Event(
            id=key,
            calendar_alias=alias,
            title=draft.title,
            start=draft.period.start.isoformat(),
            end=draft.period.end.isoformat(),
            url="https://calendar.google.com/event/test",
            etag=self.etag,
            editable=True,
            guest_emails=frozenset(draft.guest_emails),
            attendees_complete=True,
            label_id=draft.label.id if draft.label else None,
            label=draft.label,
        )
        self.stored[key] = event
        return event

    def update(self, alias: str, event: Event, draft: EventDraft, key: str) -> Event:
        return self.create(alias, draft, key)

    def find_applied(self, alias: str, event_id: str | None, key: str) -> Event | None:
        return self.stored.get(key)


@dataclass
class Harness:
    agent: Agent
    jobs: SQLiteJobRepository
    planner: ScriptedPlanner
    messenger: FakeMessenger
    knowledge: FakeKnowledge
    calendar: FakeCalendar
    source: Source

    policy: AgentPolicy
    skillbook: SkillBook
    label_users: dict[str, dict[str, str]] | None = None
    member_directory: MemberDirectory | None = None
    clock: Clock | None = None
    announcements: AnnouncementSender | None = None

    def configure(
        self,
        *,
        policy: AgentPolicy | None = None,
        label_users: dict[str, dict[str, str]] | None = None,
        member_directory: MemberDirectory | None = None,
        tools: ToolRegistry | None = None,
        clock: Clock | None = None,
        announcements: AnnouncementSender | None = None,
    ) -> None:
        if policy is not None:
            self.policy = policy
        if label_users is not None:
            self.label_users = label_users
        if member_directory is not None:
            self.member_directory = member_directory
        if clock is not None:
            self.clock = clock
        if announcements is not None:
            self.announcements = announcements
        knowledge = compose_knowledge(self.knowledge)
        scheduling = compose_scheduling(
            self.calendar,
            {"personal"},
            self.label_users,
            self.member_directory,
            creation_labels={
                alias: config.creation_label
                for alias, config in self.policy.calendars.items()
                if config.creation_label is not None
            },
        )
        self.agent = build_agent(
            self.policy,
            self.jobs,
            self.planner,
            self.messenger,
            knowledge,
            scheduling,
            self.skillbook,
            tools=tools,
            clock=self.clock,
            announcements=self.announcements,
            meeting_receipts=self.jobs,
        )

    def submit(self, text: str = "依頼", event_id: str = "event1") -> str:
        job_id = self.agent.receive(
            IncomingMessage(event_id=event_id, source=self.source, text=text)
        )
        assert job_id is not None
        return job_id

    def approve(self, job_id: str) -> None:
        self.agent.receive(
            IncomingMessage(
                event_id="approval-" + job_id, source=self.source, text="承認 " + job_id
            )
        )


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    policy = AgentPolicy(
        team_id="T1",
        user_ids=frozenset({"U1"}),
        channel_ids=frozenset({"C1"}),
        notion_parents={"notes": "parent1"},
        calendars={
            "personal": CalendarConfig(id="test@example.com", writable=True),
        },
    )
    jobs = SQLiteJobRepository(tmp_path / "agent.sqlite3")
    planner, messenger = ScriptedPlanner(), FakeMessenger()
    knowledge, calendar = FakeKnowledge(), FakeCalendar()
    book = load_skillbook()
    agent = build_agent(
        policy,
        jobs,
        planner,
        messenger,
        compose_knowledge(knowledge),
        compose_scheduling(calendar, {"personal"}),
        book,
    )
    return Harness(
        agent,
        jobs,
        planner,
        messenger,
        knowledge,
        calendar,
        Source(team_id="T1", channel_id="C1", thread_ts="1000.000001", user_id="U1"),
        policy,
        book,
    )
