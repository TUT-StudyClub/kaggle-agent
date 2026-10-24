import json
import re
from decimal import Decimal
from zoneinfo import ZoneInfo

from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.actions import ActionService
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.application.services.availability_reply import availability_reply
from modules.interaction.application.services.completion import JobCompletion
from modules.interaction.application.services.meetings import meeting_preview
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.approval import is_approval_reply
from modules.interaction.domain.models import (
    Job,
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
    ResponseRequest,
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
        followup = job.message.addressing == "followup" and not job.plan_json
        activity_started = False
        try:
            if not self.settings.allows(job.message.source):
                raise AgentError("forbidden_source", "Slackの許可範囲外です。")
            if followup:
                if not self.jobs.accepts_followup(job.message):
                    self._ignore(job, "tracking_stopped")
                    return True
                thread = self._thread_at_message(job)
                context = self.jobs.context(job.message.source)
                stored = json.loads(context)
                decision = self.planner.check_response(
                    ResponseRequest(
                        instruction=job.message.text,
                        requester_user_id=job.message.source.user_id,
                        thread=thread,
                        context=json.dumps(
                            {
                                key: stored[key]
                                for key in ("dialogue", "pending_clarification")
                                if key in stored
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                if not self.jobs.accepts_followup(job.message):
                    self._ignore(job, "tracking_stopped")
                    return True
                if not decision.should_respond:
                    self._ignore(job, "not_addressed")
                    return True
                approval = re.fullmatch(r"承認\s+([a-f0-9]{32})", job.message.text.strip())
                if approval:
                    approved = self.jobs.approve(
                        approval[1], job.message.source, self.clock.timestamp()
                    )
                    if not approved:
                        self.completion.finish(
                            job,
                            {},
                            "承認できません。対象・利用者・スレッド・有効期限を確認してください。",
                        )
                    else:
                        self.jobs.finish_silently(job.transition(JobStatus.SUCCEEDED), "approved")
                    return True
                if is_approval_reply(job.message.text):
                    if self.jobs.approve_pending(job.message.source, self.clock.timestamp()):
                        self.jobs.finish_silently(job.transition(JobStatus.SUCCEEDED), "approved")
                        return True
                    if not stored.get("pending_clarification"):
                        self.completion.finish(
                            job,
                            {},
                            "承認・返答できる確認がないか、期限が切れているよ。"
                            "Botの確認メッセージと同じスレッドで返信してね。",
                        )
                        return True
            activity_started = True
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
            rejected_plans: set[str] = set()
            if not followup:
                thread = self._thread_at_message(job)
                context = self.jobs.context(job.message.source)
            while True:
                if followup and not self.jobs.accepts_followup(job.message):
                    self._ignore(job, "tracking_stopped")
                    return True
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
                            "kaggle_may_write": (
                                self.settings.kaggle_owner_user_id is not None
                                and job.message.source.user_id == self.settings.kaggle_owner_user_id
                            ),
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
                if followup and not self.jobs.accepts_followup(job.message):
                    self._ignore(job, "tracking_stopped")
                    return True
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
                    # 過去の会話や古い操作案の対応で進めず、この依頼のDB対応を先に取得する。
                    observations.append(self.actions.execute(job, Plan(action=ListCalendars())))
                    continue
                if plan.action.kind in WRITE_KINDS:
                    self.activity.update(job.message.source, Activity.PREPARING)
                    try:
                        plan = self.actions.prepare(job, plan)
                    except AgentError as error:
                        if self._recover_input(job, plan, error, observations, rejected_plans):
                            continue
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
                try:
                    result = self.actions.execute(job, plan)
                except AgentError as error:
                    if self._recover_input(job, plan, error, observations, rejected_plans):
                        continue
                    raise
                if isinstance(plan.action, FindFree):
                    # 日付・時刻・判定はPythonの計算結果から直接返す。AIに再編集させない。
                    self.completion.finish(job, result, availability_reply(result))
                    return True
                observations.append(result)
        except AgentError as error:
            if followup and not activity_started:
                self._ignore(job, "response_check_failed", error.code)
                return True
            failed = job.transition(JobStatus.FAILED, error_code=error.code)
            self.jobs.notify(
                failed,
                error.message
                if error.code == "kaggle_input_stalled"
                else f"ジョブ {job.id}: {error.message} [{error.code}]",
                outcome=error.code,
            )
        except Exception:
            if followup and not activity_started:
                self._ignore(job, "response_check_failed", "internal_error")
                return True
            # SDK例外・ValidationError等をそのままSlackやログへ流さない。
            failed = job.transition(JobStatus.FAILED, error_code="internal_error")
            self.jobs.notify(
                failed, f"ジョブ {job.id}: 内部エラーで停止しました。", outcome="internal_error"
            )
        finally:
            if activity_started:
                self.activity.update(job.message.source, Activity.IDLE)
        return True

    def _recover_input(
        self,
        job: Job,
        plan: Plan,
        error: AgentError,
        observations: list[dict],
        rejected_plans: set[str],
    ) -> bool:
        feedback = self.actions.input_feedback(job, plan, error)
        if feedback is None:
            return False
        signature = plan.model_dump_json()
        if signature in rejected_plans:
            raise AgentError(
                "kaggle_input_stalled", "Kaggleの取得条件を修正できず、処理を止めたよ。"
            )
        rejected_plans.add(signature)
        observations.append(feedback)
        return True

    def _ignore(self, job: Job, outcome: str, error_code: str | None = None) -> None:
        self.jobs.finish_silently(job.transition(JobStatus.IGNORED, error_code=error_code), outcome)

    def _thread_at_message(self, job: Job) -> str:
        thread = self.messenger.thread(job.message.source)
        if job.message.message_ts is None:
            return thread
        messages = json.loads(thread)
        # ワーカーの待機中に投稿された未来の発言を、今回の宛先判定に使わない。
        return json.dumps(
            [item for item in messages if Decimal(item["ts"]) <= Decimal(job.message.message_ts)],
            ensure_ascii=False,
        )
