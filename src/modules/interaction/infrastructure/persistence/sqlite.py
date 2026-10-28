import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from modules.interaction.domain.meetings import MeetingReceipt, MeetingRepository
from modules.interaction.domain.models import (
    ApprovalReaction,
    IncomingMessage,
    Job,
    JobStatus,
    ReplyDelivery,
    Source,
)
from modules.interaction.domain.reminders import ReminderRepository, ReminderState
from modules.interaction.domain.repositories import DeliveryRepository, JobRepository
from shared.errors import AgentError


class SQLiteJobRepository(JobRepository, DeliveryRepository, ReminderRepository, MeetingRepository):
    """短い独立トランザクション。ジョブはclaim、定期通知は日別の予約で排他する。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 6:
                raise AgentError("database_version", "このバージョンではDBを開けません。")
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
                    conversation_key TEXT NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS seen_events (event_key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS conversations (
                    key TEXT PRIMARY KEY, result TEXT NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_mappings (
                    key TEXT PRIMARY KEY, page_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_logs (
                    job_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT,
                    outcome TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY, source TEXT NOT NULL, text TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
            """)
            if version < 2:
                # v1のジョブ・通知を保持したまま、返信の復旧用テーブルだけを追加する。
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS reply_deliveries (
                        id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                    );
                    PRAGMA user_version=2;
                """)
            if version < 3:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS schedule_reminders (
                        key TEXT PRIMARY KEY, state TEXT NOT NULL,
                        message_ts TEXT, updated_at REAL NOT NULL
                    );
                    PRAGMA user_version=3;
                """)
            if version < 4:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS meeting_receipts (
                        key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                    );
                    PRAGMA user_version=4;
                """)
            if version < 5:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS thread_tracking (
                        key TEXT PRIMARY KEY, stopped INTEGER NOT NULL,
                        event_ts TEXT NOT NULL, last_stop_ts TEXT
                    );
                    PRAGMA user_version=5;
                """)
            if version < 6:
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS conversation_tracking (key TEXT PRIMARY KEY);
                    PRAGMA user_version=6;
                """)
            # 旧版で作成された未期限切れの承認待ちも期限なしへ移行する。
            # すでに期限切れの案は復活させない。
            db.execute(
                "UPDATE jobs SET payload=json_set(payload,'$.approval_expires_at',NULL) "
                "WHERE status='awaiting_approval' "
                "AND json_extract(payload,'$.approval_expires_at') >= ?",
                (time.time(),),
            )
        self.path.chmod(0o600)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, message: IncomingMessage) -> Job | None:
        with self.connection() as db:
            return self._enqueue(db, message)

    def _enqueue(self, db: sqlite3.Connection, message: IncomingMessage) -> Job | None:
        event_key = hashlib.sha256(
            f"{message.source.team_id}/{message.event_id}".encode(),
        ).hexdigest()
        job = Job(id=uuid4().hex, message=message, updated_at=time.time())
        if (
            db.execute(
                "INSERT OR IGNORE INTO seen_events VALUES (?)",
                (event_key,),
            ).rowcount
            == 0
        ):
            return None
        db.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            (
                job.id,
                event_key,
                message.source.conversation_key,
                job.status,
                job.model_dump_json(),
                job.updated_at,
            ),
        )
        return job

    def claim(self) -> Job | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("""
                SELECT payload FROM jobs j WHERE status='queued'
                AND NOT EXISTS (
                    SELECT 1 FROM jobs other WHERE other.conversation_key=j.conversation_key
                    AND other.status='running'
                ) ORDER BY CASE WHEN json_extract(payload,'$.plan_json') IS NOT NULL
                    THEN 0 ELSE 1 END, updated_at LIMIT 1
            """).fetchone()
            if not row:
                return None
            job = Job.model_validate_json(row[0]).transition(JobStatus.RUNNING)
            self._save(db, job)
            return job

    def get(self, job_id: str) -> Job | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return Job.model_validate_json(row[0]) if row else None

    def _save(self, db: sqlite3.Connection, job: Job) -> None:
        updated = job.model_copy(update={"updated_at": time.time()})
        db.execute(
            "UPDATE jobs SET status=?,payload=?,updated_at=? WHERE id=?",
            (
                updated.status,
                updated.model_dump_json(),
                updated.updated_at,
                updated.id,
            ),
        )

    def save(self, job: Job) -> None:
        with self.connection() as db:
            self._save(db, job)

    def approve(self, job_id: str, source: Source, now: float) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return False
            job = Job.model_validate_json(row[0])
            approved = job.approve(source, now)
            if approved is None:
                return False
            self._save(db, approved)
        return True

    def approve_pending(self, source: Source, now: float, message_ts: str | None = None) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT payload FROM jobs WHERE conversation_key=? AND status='awaiting_approval' "
                "AND (json_extract(payload,'$.approval_expires_at') IS NULL "
                "OR json_extract(payload,'$.approval_expires_at') >= ?)",
                (source.conversation_key, now),
            ).fetchall()
            if len(rows) != 1:
                return False
            job = Job.model_validate_json(rows[0][0])
            row = db.execute(
                "SELECT payload FROM reply_deliveries WHERE id=?",
                (f"{job.id}/awaiting_approval",),
            ).fetchone()
            if row is None:
                return False
            delivery = ReplyDelivery.model_validate_json(row[0])
            if delivery.state != "done" or delivery.source != source or not delivery.message_ts:
                return False
            if message_ts is not None and Decimal(message_ts) <= Decimal(delivery.message_ts):
                return False
            approved = job.approve(source, now)
            if approved is None:
                return False
            self._save(db, approved)
        return True

    def pending_approval_count(self, source: Source, now: float) -> int:
        with self.connection() as db:
            return db.execute(
                "SELECT count(*) FROM jobs WHERE conversation_key=? AND status='awaiting_approval' "
                "AND (json_extract(payload,'$.approval_expires_at') IS NULL "
                "OR json_extract(payload,'$.approval_expires_at') >= ?)",
                (source.conversation_key, now),
            ).fetchone()[0]

    def approve_reaction(self, reaction: ApprovalReaction, now: float) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT payload FROM reply_deliveries WHERE "
                "json_extract(payload,'$.source.team_id')=? AND "
                "json_extract(payload,'$.source.channel_id')=? AND "
                "json_extract(payload,'$.source.user_id')=? AND "
                "json_extract(payload,'$.message_ts')=? AND "
                "json_extract(payload,'$.state')='done' AND id LIKE '%/awaiting_approval'",
                (reaction.team_id, reaction.channel_id, reaction.user_id, reaction.message_ts),
            ).fetchall()
            if len(rows) != 1:
                return False
            delivery = ReplyDelivery.model_validate_json(rows[0][0])
            row = db.execute(
                "SELECT payload FROM jobs WHERE id=?", (delivery.id.split("/")[0],)
            ).fetchone()
            if row is None:
                return False
            job = Job.model_validate_json(row[0])
            approved = job.approve(delivery.source, now)
            if approved is None:
                return False
            self._save(db, approved)
        return True

    def _clarification_job(self, db: sqlite3.Connection, source: Source) -> Job | None:
        if db.execute(
            "SELECT 1 FROM jobs WHERE conversation_key=? "
            "AND status IN ('queued','running','awaiting_approval') LIMIT 1",
            (source.conversation_key,),
        ).fetchone():
            return None
        row = db.execute(
            "SELECT result FROM conversations WHERE key=?",
            (source.conversation_key,),
        ).fetchone()
        context = json.loads(row[0]) if row else {}
        pending = context.get("pending_clarification")
        dialogue = context.get("dialogue", [])
        if not pending or not dialogue:
            return None
        job_id = dialogue[-1].get("job_id")
        row = db.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return None
        job = Job.model_validate_json(row[0])
        if job.status != JobStatus.SUCCEEDED or job.message.source != source or job.write_started:
            return None
        stored = json.loads(job.result_json or "{}")
        if stored.get("pending_clarification") != pending:
            return None
        row = db.execute(
            "SELECT payload FROM reply_deliveries WHERE id=?",
            (f"{job.id}/succeeded",),
        ).fetchone()
        if row is None:
            return None
        delivery = ReplyDelivery.model_validate_json(row[0])
        if delivery.source != source or delivery.state != "done" or not delivery.message_ts:
            return None
        return job

    def answer_clarification(self, message: IncomingMessage) -> Job | None:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            if self._clarification_job(db, message.source) is None:
                return None
            return self._enqueue(db, message)

    def answer_clarification_reaction(self, reaction: ApprovalReaction) -> bool:
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT payload FROM reply_deliveries WHERE "
                "json_extract(payload,'$.source.team_id')=? AND "
                "json_extract(payload,'$.source.channel_id')=? AND "
                "json_extract(payload,'$.source.user_id')=? AND "
                "json_extract(payload,'$.message_ts')=? AND "
                "json_extract(payload,'$.state')='done' AND id LIKE '%/succeeded'",
                (reaction.team_id, reaction.channel_id, reaction.user_id, reaction.message_ts),
            ).fetchall()
            if len(rows) != 1:
                return False
            delivery = ReplyDelivery.model_validate_json(rows[0][0])
            job = self._clarification_job(db, delivery.source)
            if job is None or delivery.id != f"{job.id}/succeeded":
                return False
            message = IncomingMessage(
                event_id=f"clarification-reaction/{job.id}",
                source=delivery.source,
                text=f":{reaction.reaction}:（直前の確認質問への同意リアクション）",
            )
            return self._enqueue(db, message) is not None

    def recover(self) -> int:
        count = 0
        with self.connection() as db:
            for (payload,) in db.execute(
                "SELECT payload FROM jobs WHERE status IN ('running','awaiting_approval')",
            ).fetchall():
                job = Job.model_validate_json(payload)
                if job.status == JobStatus.AWAITING_APPROVAL:
                    if job.approval_expires_at is None or job.approval_expires_at >= time.time():
                        continue
                    job = job.transition(JobStatus.FAILED, error_code="approval_expired")
                elif job.write_started:
                    job = job.transition(JobStatus.FAILED, error_code="uncertain_write")
                else:
                    job = job.model_copy(update={"status": JobStatus.QUEUED})
                self._save(db, job)
                if job.status == JobStatus.FAILED:
                    message = (
                        "承認の有効期限が切れたため、操作は実行していないよ。再度依頼してね。"
                        if job.error_code == "approval_expired"
                        else "実行結果が不明だよ。再実行前に結果を照合してね。"
                    )
                    self._notify(
                        db,
                        job,
                        f"ジョブ {job.id}: {message} [{job.error_code}]",
                    )
                count += 1
        return count

    def tracks(self, source: Source) -> bool:
        if self.tracking_stopped(source):
            return False
        prefix = source.thread_key + "/"
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM thread_tracking WHERE key=? AND stopped=0 "
                    "UNION ALL SELECT 1 FROM conversation_tracking WHERE substr(key,1,?)=? "
                    "UNION ALL SELECT 1 FROM jobs WHERE substr(conversation_key,1,?)=? LIMIT 1",
                    (source.thread_key, len(prefix), prefix, len(prefix), prefix),
                ).fetchone()
                is not None
            )

    def tracking_stopped(self, source: Source) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT stopped FROM thread_tracking WHERE key=?", (source.thread_key,)
            ).fetchone()
        return bool(row and row[0])

    @staticmethod
    def _message_time(message: IncomingMessage) -> str:
        return message.message_ts or str(message.received_at.timestamp())

    def set_tracking(self, message: IncomingMessage, *, stopped: bool) -> bool:
        event_ts = self._message_time(message)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # 再配信された古いメンションで、後から送られた !stop を取り消さない。
            row = db.execute(
                "SELECT event_ts,last_stop_ts FROM thread_tracking WHERE key=?",
                (message.source.thread_key,),
            ).fetchone()
            if row and Decimal(event_ts) <= Decimal(row[0]):
                return False
            db.execute(
                "INSERT INTO thread_tracking VALUES (?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET stopped=excluded.stopped, "
                "event_ts=excluded.event_ts,last_stop_ts=excluded.last_stop_ts",
                (
                    message.source.thread_key,
                    stopped,
                    event_ts,
                    event_ts if stopped else row[1] if row else None,
                ),
            )
            if not stopped:
                # 本文なしのメンションも、AIジョブを作らず本人の会話開始として保持する。
                db.execute(
                    "INSERT OR IGNORE INTO conversation_tracking VALUES (?)",
                    (message.source.conversation_key,),
                )
        return True

    def accepts_followup(self, message: IncomingMessage) -> bool:
        with self.connection() as db:
            row = db.execute(
                "SELECT stopped,last_stop_ts FROM thread_tracking WHERE key=?",
                (message.source.thread_key,),
            ).fetchone()
        return not row or (
            not row[0]
            and (row[1] is None or Decimal(self._message_time(message)) > Decimal(row[1]))
        )

    def finish_silently(self, job: Job, outcome: str) -> None:
        with self.connection() as db:
            self._save(db, job)
            self._audit(db, job, outcome)

    def context(self, source: Source) -> str:
        with self.connection() as db:
            row = db.execute(
                "SELECT result FROM conversations WHERE key=?",
                (source.conversation_key,),
            ).fetchone()
        return row[0] if row else "{}"

    def remember(self, source: Source, result_json: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?,?,?)",
                (
                    source.conversation_key,
                    result_json,
                    time.time(),
                ),
            )

    def mapping(self, thread_key: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT page_id FROM source_mappings WHERE key=?",
                (thread_key,),
            ).fetchone()
        return row[0] if row else None

    def unresolved_record(self, thread_key: str) -> str | None:
        with self.connection() as db:
            row = db.execute(
                """
                SELECT id FROM jobs WHERE status='failed'
                AND json_extract(payload,'$.write_started')=1
                AND json_extract(json_extract(payload,'$.plan_json'),'$.action.record_thread')=1
                AND json_extract(payload,'$.message.source.team_id') || '/' ||
                    json_extract(payload,'$.message.source.channel_id') || '/' ||
                    json_extract(payload,'$.message.source.thread_ts') = ? LIMIT 1
            """,
                (thread_key,),
            ).fetchone()
        return row[0] if row else None

    def map_page(self, thread_key: str, page_id: str) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO source_mappings VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                (
                    thread_key,
                    page_id,
                ),
            )

    def _audit(self, db: sqlite3.Connection, job: Job, outcome: str) -> None:
        kind = json.loads(job.plan_json)["action"]["kind"] if job.plan_json else None
        db.execute(
            "INSERT INTO audit_logs VALUES (?,?,?,?,?)",
            (job.id, job.message.source.user_id, kind, outcome, time.time()),
        )

    def audit(self, job: Job, outcome: str) -> None:
        with self.connection() as db:
            self._audit(db, job, outcome)

    def purge(self, before: float) -> int:
        with self.connection() as db:
            count = db.execute(
                "DELETE FROM jobs WHERE updated_at<? AND status!='awaiting_approval'", (before,)
            ).rowcount
            db.execute("DELETE FROM conversations WHERE updated_at<?", (before,))
            db.execute("DELETE FROM audit_logs WHERE created_at<?", (before,))
            db.execute(
                "DELETE FROM notifications WHERE created_at<? AND NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE status='awaiting_approval' "
                "AND notifications.id=jobs.id || '/awaiting_approval')",
                (before,),
            )
            db.execute(
                "DELETE FROM reply_deliveries WHERE updated_at<? AND NOT EXISTS "
                "(SELECT 1 FROM jobs WHERE status='awaiting_approval' "
                "AND reply_deliveries.id=jobs.id || '/awaiting_approval')",
                (before,),
            )
            db.execute("DELETE FROM schedule_reminders WHERE updated_at<?", (before,))
            db.execute("DELETE FROM meeting_receipts WHERE updated_at<?", (before,))
        return count

    def reminder_state(self, key: str) -> ReminderState | None:
        with self.connection() as db:
            row = db.execute("SELECT state FROM schedule_reminders WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def reserve_reminder(self, key: str, now: float, *, empty: bool = False) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO schedule_reminders VALUES (?,?,NULL,?)",
                    (key, "empty" if empty else "sending", now),
                ).rowcount
                == 1
            )

    def finish_reminder(self, key: str, message_ts: str, now: float) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE schedule_reminders SET state='sent',message_ts=?,updated_at=? WHERE key=?",
                (message_ts, now, key),
            )

    def release_reminder(self, key: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM schedule_reminders WHERE key=? AND state='sending'", (key,))

    def _notify(self, db: sqlite3.Connection, job: Job, text: str) -> None:
        notice_id = f"{job.id}/{job.status}"
        db.execute(
            "INSERT OR IGNORE INTO notifications VALUES (?,?,?,?)",
            (
                notice_id,
                job.message.source.model_dump_json(),
                text[:38000],
                time.time(),
            ),
        )

    def notify(self, job: Job, text: str, *, outcome: str | None = None) -> None:
        with self.connection() as db:
            self._save(db, job)
            self._notify(db, job, text)
            if outcome is not None:
                self._audit(db, job, outcome)

    def complete(
        self,
        job: Job,
        context_json: str,
        text: str,
        outcome: str,
        *,
        page_mapping: tuple[str, str] | None = None,
    ) -> None:
        with self.connection() as db:
            self._save(db, job)
            db.execute(
                "INSERT OR REPLACE INTO conversations VALUES (?,?,?)",
                (job.message.source.conversation_key, context_json, time.time()),
            )
            if page_mapping is not None:
                db.execute(
                    "INSERT INTO source_mappings VALUES (?,?) ON CONFLICT(key) DO NOTHING",
                    page_mapping,
                )
            self._notify(db, job, text)
            self._audit(db, job, outcome)

    def pending_notifications(self) -> list[tuple[str, Source, str]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT id,source,text FROM notifications ORDER BY created_at LIMIT 20",
            ).fetchall()
        return [(key, Source.model_validate_json(source), text) for key, source, text in rows]

    def notification_sent(self, notification_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM notifications WHERE id=?", (notification_id,))

    def get_delivery(self, notification_id: str) -> ReplyDelivery | None:
        with self.connection() as db:
            row = db.execute(
                "SELECT payload FROM reply_deliveries WHERE id=?", (notification_id,)
            ).fetchone()
        return ReplyDelivery.model_validate_json(row[0]) if row else None

    def save_delivery(self, delivery: ReplyDelivery) -> None:
        with self.connection() as db:
            db.execute(
                "INSERT INTO reply_deliveries VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "payload=excluded.payload,updated_at=excluded.updated_at",
                (delivery.id, delivery.model_dump_json(), time.time()),
            )

    def forget_delivery(self, notification_id: str) -> None:
        with self.connection() as db:
            db.execute("DELETE FROM reply_deliveries WHERE id=?", (notification_id,))

    def reserve_meeting(self, receipt: MeetingReceipt) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "INSERT OR IGNORE INTO meeting_receipts VALUES (?,?,?)",
                    (receipt.key, receipt.model_dump_json(), time.time()),
                ).rowcount
                == 1
            )

    def meeting(self, key: str) -> MeetingReceipt | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM meeting_receipts WHERE key=?", (key,)).fetchone()
        return MeetingReceipt.model_validate_json(row[0]) if row else None

    def save_meeting(self, receipt: MeetingReceipt) -> None:
        with self.connection() as db:
            db.execute(
                "UPDATE meeting_receipts SET payload=?,updated_at=? WHERE key=?",
                (receipt.model_dump_json(), time.time(), receipt.key),
            )
