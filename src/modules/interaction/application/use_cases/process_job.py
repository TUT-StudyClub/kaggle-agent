import json
from zoneinfo import ZoneInfo

from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.actions import ActionService
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.application.services.availability_reply import availability_reply
from modules.interaction.application.services.completion import JobCompletion
from modules.interaction.application.services.meetings import meeting_preview
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.models import (
    JobStatus,
)
from modules.interaction.domain.repositories import JobRepository
from modules.planning.public import (
    WRITE_KINDS,
    Clarify,
    CreateMeeting,
    Plan,
    Planner,
    PlanningRequest,
    Reply,
)
from modules.scheduling.public import (
    FindFree,
    ListCalendars,
    SchedulingService,
)
from shared.application.ports.clock import Clock
from shared.errors import AgentError


class ProcessJobUseCase:
    def __init__(
        self,
        settings: AgentPolicy,
        jobs: JobRepository,
        planner: Planner,
        messenger: Messenger,
        scheduling: SchedulingService,
        tools: ToolRegistry,
        actions: ActionService,
        completion: JobCompletion,
        activity: ActivityReporter,
        clock: Clock,
    ):
        self.settings = settings
        self.jobs = jobs
        self.planner = planner
        self.messenger = messenger
        self.scheduling = scheduling
        self.tools = tools
        self.actions = actions
        self.completion = completion
        self.activity = activity
        self.clock = clock

    def execute(self) -> bool:
        job = self.jobs.claim()
        if not job:
            return False
        try:
            if not self.settings.allows(job.message.source):
                raise AgentError("forbidden_source", "Slackの許可範囲外です。")
            self.activity.update(job.message.source, Activity.READING)
            if job.plan_json:
                if (job.approval_expires_at or 0) < self.clock.timestamp():
                    raise AgentError(
                        "approval_expired",
                        "承認の有効期限が切れたため、操作は実行していないよ。再度依頼してね。",
                    )
                plan = Plan.model_validate_json(job.plan_json)
                job = job.start_write()
                self.jobs.save(job)
                result = self.actions.execute(job, plan)
                text = (
                    f"「{plan.action.event.draft.title}」を作成して、"
                    + ("招待メール送信を要求したよ。" if plan.action.event.guest_user_ids else "")
                    + f"#{plan.action.announcement.channel_name}への周知も完了したよ。"
                    if isinstance(plan.action, CreateMeeting)
                    else "実行しました。\n" + json.dumps(result, ensure_ascii=False, indent=2)
                )
                self.completion.finish(
                    job,
                    result,
                    text,
                )
                return True
            observations = []
            thread = self.messenger.thread(job.message.source)
            context = self.jobs.context(job.message.source)
            for _ in range(self.settings.max_read_steps + 1):
                self.activity.update(job.message.source, Activity.THINKING)
                plan = self.planner.plan(
                    PlanningRequest(
                        instruction=job.message.text,
                        now=self.clock.now().astimezone(ZoneInfo("Asia/Tokyo")).isoformat(),
                        context=context,
                        thread=thread,
                        observations=observations,
                        resources={
                            "requester_slack_user_id": job.message.source.user_id,
                            "notion_pages": sorted(self.settings.notion_pages),
                            "notion_data_sources": sorted(
                                self.settings.allowed_notion_data_sources
                            ),
                            "notion_competition_data_source_id": (
                                self.settings.notion_competition_data_source_id
                            ),
                            "notion_parents": self.settings.notion_parents,
                            "calendars": {
                                k: {
                                    "writable": v.writable,
                                    "label_users": v.label_users,
                                    "member_directory": v.member_data_source_id is not None,
                                    "creation_label": v.creation_label.model_dump(mode="json")
                                    if v.creation_label is not None
                                    else None,
                                }
                                for k, v in self.settings.calendars.items()
                            },
                        },
                    )
                )
                if not self.tools.skillbook.permits(plan.action.kind):
                    raise AgentError("forbidden_tool", "この操作はツール設定で許可されていません。")
                match plan.action:
                    case Reply(text=text) | Clarify(question=text):
                        last = observations[-1] if observations else json.loads(context)
                        self.completion.finish(
                            job,
                            last,
                            text,
                            plan.action.question if isinstance(plan.action, Clarify) else None,
                        )
                        return True
                if (
                    self.scheduling.uses_member_directory
                    and (
                        plan.action.kind.startswith("calendar.")
                        or isinstance(plan.action, CreateMeeting)
                    )
                    and plan.action.kind != "calendar.list"
                    and not any("calendars" in observation for observation in observations)
                ):
                    if len(observations) >= self.settings.max_read_steps:
                        break
                    # 過去の会話や古い操作案の対応で進めず、この依頼のDB対応を先に取得する。
                    observations.append(self.actions.execute(job, Plan(action=ListCalendars())))
                    continue
                if plan.action.kind in WRITE_KINDS:
                    self.activity.update(job.message.source, Activity.PREPARING)
                    try:
                        plan = self.actions.prepare(job, plan)
                    except AgentError as error:
                        if error.code not in {
                            "invitation_missing_email",
                            "invitation_ambiguous_email",
                        }:
                            raise
                        self.completion.finish(job, {}, error.message, error.message)
                        return True
                    job = job.transition(
                        JobStatus.AWAITING_APPROVAL,
                        plan_json=plan.model_dump_json(),
                        approval_expires_at=self.clock.timestamp()
                        + self.settings.approval_minutes * 60,
                    )
                    preview = json.dumps(
                        plan.action.model_dump(mode="json"), ensure_ascii=False, indent=2
                    ).replace("`", "\\u0060")
                    description = (
                        meeting_preview(plan.action)
                        if isinstance(plan.action, CreateMeeting)
                        else f"以下の操作を承認しますか？\n\n```json\n{preview}\n```"
                    )
                    self.jobs.notify(
                        job,
                        description,
                        outcome="awaiting_approval",
                    )
                    return True
                if len(observations) >= self.settings.max_read_steps:
                    break
                result = self.actions.execute(job, plan)
                if isinstance(plan.action, FindFree):
                    # 日付・時刻・判定はPythonの計算結果から直接返す。AIに再編集させない。
                    self.completion.finish(job, result, availability_reply(result))
                    return True
                observations.append(result)
            raise AgentError("step_limit", "参照回数の上限に達しました。依頼を絞ってください。")
        except AgentError as error:
            failed = job.transition(JobStatus.FAILED, error_code=error.code)
            self.jobs.notify(
                failed, f"ジョブ {job.id}: {error.message} [{error.code}]", outcome=error.code
            )
        except Exception:
            # SDK例外・ValidationError等をそのままSlackやログへ流さない。
            failed = job.transition(JobStatus.FAILED, error_code="internal_error")
            self.jobs.notify(
                failed, f"ジョブ {job.id}: 内部エラーで停止しました。", outcome="internal_error"
            )
        finally:
            self.activity.update(job.message.source, Activity.IDLE)
        return True
