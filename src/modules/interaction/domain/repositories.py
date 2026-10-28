from abc import ABC, abstractmethod

from .models import ApprovalReaction, IncomingMessage, Job, ReplyDelivery, Source


class DeliveryRepository(ABC):
    @abstractmethod
    def get_delivery(self, notification_id: str) -> ReplyDelivery | None: ...

    @abstractmethod
    def save_delivery(self, delivery: ReplyDelivery) -> None: ...

    @abstractmethod
    def forget_delivery(self, notification_id: str) -> None: ...


class JobRepository(ABC):
    @abstractmethod
    def enqueue(self, message: IncomingMessage) -> Job | None: ...

    @abstractmethod
    def claim(self) -> Job | None: ...

    @abstractmethod
    def get(self, job_id: str) -> Job | None: ...

    @abstractmethod
    def save(self, job: Job) -> None: ...

    @abstractmethod
    def approve(self, job_id: str, source: Source, now: float) -> bool: ...

    @abstractmethod
    def approve_pending(
        self, source: Source, now: float, message_ts: str | None = None
    ) -> bool: ...

    @abstractmethod
    def pending_approval_count(self, source: Source, now: float) -> int: ...

    @abstractmethod
    def approve_reaction(self, reaction: ApprovalReaction, now: float) -> bool: ...

    @abstractmethod
    def answer_clarification(self, message: IncomingMessage) -> Job | None: ...

    @abstractmethod
    def answer_clarification_reaction(self, reaction: ApprovalReaction) -> bool: ...

    @abstractmethod
    def recover(self) -> int: ...

    @abstractmethod
    def context(self, source: Source) -> str: ...

    @abstractmethod
    def tracks(self, source: Source) -> bool: ...

    @abstractmethod
    def tracking_stopped(self, source: Source) -> bool: ...

    @abstractmethod
    def set_tracking(self, message: IncomingMessage, *, stopped: bool) -> bool: ...

    @abstractmethod
    def accepts_followup(self, message: IncomingMessage) -> bool: ...

    @abstractmethod
    def finish_silently(self, job: Job, outcome: str) -> None: ...

    @abstractmethod
    def remember(self, source: Source, result_json: str) -> None: ...

    @abstractmethod
    def mapping(self, thread_key: str) -> str | None: ...

    @abstractmethod
    def unresolved_record(self, thread_key: str) -> str | None: ...

    @abstractmethod
    def map_page(self, thread_key: str, page_id: str) -> None: ...

    @abstractmethod
    def audit(self, job: Job, outcome: str) -> None: ...

    @abstractmethod
    def purge(self, before: float) -> int: ...

    @abstractmethod
    def notify(self, job: Job, text: str, *, outcome: str | None = None) -> None: ...

    @abstractmethod
    def pending_notifications(self) -> list[tuple[str, Source, str]]: ...

    @abstractmethod
    def notification_sent(self, notification_id: str) -> None: ...

    @abstractmethod
    def complete(
        self,
        job: Job,
        context_json: str,
        text: str,
        outcome: str,
        *,
        page_mapping: tuple[str, str] | None = None,
    ) -> None:
        """状態・会話・通知予定・監査を原子的に保存する。"""
        ...
