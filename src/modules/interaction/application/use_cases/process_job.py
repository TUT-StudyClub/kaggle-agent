import json
from decimal import Decimal

from pydantic import BaseModel

from modules.interaction.application.policies.access import AgentPolicy
from modules.interaction.application.ports.messenger import Activity, Messenger
from modules.interaction.application.services.actions import ActionService
from modules.interaction.application.services.activity import ActivityReporter
from modules.interaction.application.services.availability_reply import availability_reply
from modules.interaction.application.services.completion import JobCompletion
from modules.interaction.application.services.meetings import meeting_preview
from modules.interaction.application.services.tools import ToolRegistry
from modules.interaction.domain.approval import approval_job_id
from modules.interaction.domain.models import (
    Job,
    JobStatus,
)
from modules.interaction.domain.repositories import JobRepository
from modules.planning.public import (
    WRITE_KINDS,
    Action,
    ApprovalRequest,
    Clarify,
    CreateMeeting,
    Plan,
    Planner,
    PlanningRequest,
    Reply,
    UpdateInstructions,
)
from modules.scheduling.public import (
    FindFree,
    ListCalendars,
    SchedulingService,
)
from shared.application.ports.clock import Clock
from shared.domain.timezone import JST
from shared.errors import AgentError


class _Run(BaseModel):
    """1ジョブの処理中に変わる状態。失敗時は最新のジョブ状態から遷移させる。"""

    job: Job
    followup: bool
    working: bool = False


def _approved_reply(action: Action, result: dict[str, object]) -> str:
    if not isinstance(action, CreateMeeting):
        return "実行しました。\n" + json.dumps(result, ensure_ascii=False, indent=2)
    return (
        f"「{action.event.draft.title}」を作成して、"
        + ("招待メール送信を要求したよ。" if action.event.guest_user_ids else "")
        + f"#{action.announcement.channel_name}への周知も完了したよ。"
        + (f"\nドキュメント: {result['document_url']}" if result.get("document_url") else "")
    )


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
        run = _Run(job=job, followup=job.message.addressing == "followup" and not job.plan_json)
        try:
            self._process(run)
        except AgentError as error:
            failed = run.job.transition(JobStatus.FAILED, error_code=error.code)
            self.jobs.notify(
                failed,
                error.message
                if error.code == "kaggle_input_stalled"
                else f"ジョブ {failed.id}: {error.message} [{error.code}]",
                outcome=error.code,
            )
        except Exception:
            # SDK例外・ValidationError等をそのままSlackやログへ流さない。
            failed = run.job.transition(JobStatus.FAILED, error_code="internal_error")
            self.jobs.notify(
                failed, f"ジョブ {failed.id}: 内部エラーで停止しました。", outcome="internal_error"
            )
        finally:
            if run.working:
                self.activity.update(run.job.message.source, Activity.IDLE)
        return True

    def _process(self, run: _Run) -> None:
        job = run.job
        source = job.message.source
        if not self.settings.allows(source):
            raise AgentError("forbidden_source", "Slackの許可範囲外です。")
        if (
            not job.plan_json
            and self.jobs.pending_approval_count(source, self.clock.timestamp())
            and self._answer_pending_approval(run)
        ):
            return
        conversation = None
        if run.followup:
            if self._stale(run):
                return
            conversation = self._conversation(job)
            if self._approve_by_id(job):
                return
        run.working = True
        self.activity.update(source, Activity.READING)
        if job.plan_json:
            self._execute_approved(run)
            return
        if conversation is None:
            conversation = self._conversation(job)
        self._plan_and_act(run, *conversation)

    def _stale(self, run: _Run) -> bool:
        """!stop 後に届いた追従発言は、処理のどの段階でも応答せずに終える。"""
        if not run.followup or self.jobs.accepts_followup(run.job.message):
            return False
        self.jobs.finish_silently(run.job.transition(JobStatus.IGNORED), "tracking_stopped")
        return True

    def _answer_pending_approval(self, run: _Run) -> bool:
        """承認待ちの案がある会話での発言を承認として扱えたら True。通常の依頼なら False。"""
        job = run.job
        if self._stale(run):
            return True
        try:
            decision = self.planner.check_approval(
                ApprovalRequest(
                    instruction=job.message.text,
                    requester_user_id=job.message.source.user_id,
                    addressing=job.message.addressing,
                    thread=self._thread_at_message(job),
                )
            )
        except AgentError:
            if not self._stale(run):
                self.completion.finish(
                    job,
                    {},
                    "承認の意思を確認できなかったよ。操作は実行していないので、"
                    "もう一度伝えるか、承認案にリアクションしてね。",
                )
            return True
        if self._stale(run):
            return True
        if not decision.approves:
            return False
        if self.jobs.approve_pending(
            job.message.source, self.clock.timestamp(), job.message.message_ts
        ):
            self.jobs.finish_silently(job.transition(JobStatus.SUCCEEDED), "approved")
        else:
            self.completion.finish(
                job, {}, "承認する案を特定できないよ。対象の承認案にリアクションしてね。"
            )
        return True

    def _approve_by_id(self, job: Job) -> bool:
        """「承認 <ジョブID>」形式の追従発言を処理したら True。"""
        approval_id = approval_job_id(job.message.text)
        if approval_id is None:
            return False
        if self.jobs.approve(approval_id, job.message.source, self.clock.timestamp()):
            self.jobs.finish_silently(job.transition(JobStatus.SUCCEEDED), "approved")
        else:
            self.completion.finish(
                job, {}, "承認できません。対象・利用者・スレッド・有効期限を確認してください。"
            )
        return True

    def _execute_approved(self, run: _Run) -> None:
        job = run.job
        if job.approval_expires_at is not None and job.approval_expires_at < self.clock.timestamp():
            raise AgentError(
                "approval_expired",
                "承認の有効期限が切れたため、操作は実行していないよ。再度依頼してね。",
            )
        plan = Plan.model_validate_json(job.plan_json or "")
        run.job = job = job.start_write()
        self.jobs.save(job)
        result = self.actions.execute(job, plan)
        self.completion.finish(job, result, _approved_reply(plan.action, result))

    def _plan_and_act(self, run: _Run, thread: str, context: str) -> None:
        job = run.job
        source = job.message.source
        observations: list[dict] = []
        rejected_plans: set[str] = set()
        while True:
            if self._stale(run):
                return
            self.activity.update(source, Activity.THINKING)
            plan = self.planner.plan(
                PlanningRequest(
                    instruction=job.message.text,
                    now=self.clock.now().astimezone(JST).isoformat(),
                    context=context,
                    thread=thread,
                    observations=observations,
                    resources=self._resources(job),
                )
            )
            if self._stale(run):
                return
            action = plan.action
            if not self.tools.skillbook.permits(action.kind):
                raise AgentError("forbidden_tool", "この操作はツール設定で許可されていません。")
            match action:
                case Reply(text=text) | Clarify(question=text):
                    last = observations[-1] if observations else json.loads(context)
                    self.completion.finish(
                        job, last, text, action.question if isinstance(action, Clarify) else None
                    )
                    return
            if self._needs_member_directory(action, observations):
                # 過去の会話や古い操作案の対応で進めず、この依頼のDB対応を先に取得する。
                observations.append(self.actions.execute(job, Plan(action=ListCalendars())))
                continue
            if action.kind in WRITE_KINDS:
                self.activity.update(source, Activity.PREPARING)
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
                    return
                self._request_approval(run, plan)
                return
            try:
                result = self.actions.execute(job, plan)
            except AgentError as error:
                if self._recover_input(job, plan, error, observations, rejected_plans):
                    continue
                raise
            if isinstance(action, FindFree):
                # 日付・時刻・判定はPythonの計算結果から直接返す。AIに再編集させない。
                self.completion.finish(job, result, availability_reply(result))
                return
            observations.append(result)

    def _resources(self, job: Job) -> dict[str, object]:
        settings = self.settings
        user_id = job.message.source.user_id
        return {
            "requester_slack_user_id": user_id,
            "kaggle_may_write": (
                settings.kaggle_owner_user_id is not None
                and user_id == settings.kaggle_owner_user_id
            ),
            "notion_pages": sorted(settings.notion_pages),
            "notion_data_sources": sorted(settings.allowed_notion_data_sources),
            "notion_competition_data_source_id": settings.notion_competition_data_source_id,
            "notion_parents": settings.notion_parents,
            "calendars": {
                alias: {
                    "writable": calendar.writable,
                    "label_users": calendar.label_users,
                    "member_directory": calendar.member_data_source_id is not None,
                    "creation_label": calendar.creation_label.model_dump(mode="json")
                    if calendar.creation_label is not None
                    else None,
                }
                for alias, calendar in settings.calendars.items()
            },
        }

    def _needs_member_directory(self, action: Action, observations: list[dict]) -> bool:
        return (
            self.scheduling.uses_member_directory
            and (action.kind.startswith("calendar.") or isinstance(action, CreateMeeting))
            and action.kind != "calendar.list"
            and not any("calendars" in observation for observation in observations)
        )

    def _request_approval(self, run: _Run, plan: Plan) -> None:
        action = plan.action
        owner_approves = isinstance(action, UpdateInstructions)
        run.job = run.job.transition(
            JobStatus.AWAITING_APPROVAL,
            plan_json=plan.model_dump_json(),
            approval_expires_at=None,
            approval_user_id=self.settings.kaggle_owner_user_id if owner_approves else None,
        )
        if isinstance(action, CreateMeeting):
            description = meeting_preview(action)
        else:
            preview = json.dumps(
                action.model_dump(mode="json"), ensure_ascii=False, indent=2
            ).replace("`", "\\u0060")
            description = f"以下の操作を承認しますか？\n\n```json\n{preview}\n```"
        if owner_approves:
            description = (
                f"<@{self.settings.kaggle_owner_user_id}> 変更案の承認をお願いします。"
                "承認する場合はこのメッセージに 👍 を付けてね。\n\n" + description
            )
        self.jobs.notify(run.job, description, outcome="awaiting_approval")

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

    def _conversation(self, job: Job) -> tuple[str, str]:
        return self._thread_at_message(job), self.jobs.context(job.message.source)

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
