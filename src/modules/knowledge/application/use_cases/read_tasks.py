from datetime import date

from shared.errors import AgentError

from ...domain.tasks import TaskBoardRepository, TaskMember, TaskNotification, TaskPerson


def normalized_name(value: str) -> str:
    return "".join(value.split()).casefold()


def resolve_person(person: TaskPerson, members: list[TaskMember]) -> str:
    email = person.email.strip().casefold()
    name = normalized_name(person.name)
    by_email = {m.slack_user_id for m in members if email and email in m.emails}
    by_name = {m.slack_user_id for m in members if name and name == normalized_name(m.name)}
    # メールと氏名が別人を指すときも、片方を優先して送信しない。
    candidates = by_email | by_name
    if len(candidates) != 1:
        raise AgentError(
            "task_member_ambiguous" if candidates else "task_member_missing",
            "タスク担当者とMember DBの氏名・メール・Slack User IDの対応を確認してください。",
        )
    return next(iter(candidates))


class TaskBoardService:
    def __init__(self, repository: TaskBoardRepository):
        self.repository = repository

    def tasks_on(self, day: date) -> list[TaskNotification]:
        tasks = self.repository.tasks_on(day)
        members = self.repository.members() if any(t.assignees for t in tasks) else []
        if len({m.slack_user_id for m in members}) != len(members):
            raise AgentError("task_member_duplicate", "Member DBのSlack User IDが重複しています。")
        return [
            TaskNotification(
                id=task.id,
                title=task.title,
                url=task.url,
                start=task.start,
                slack_user_ids=frozenset(resolve_person(p, members) for p in task.assignees),
            )
            for task in tasks
        ]
