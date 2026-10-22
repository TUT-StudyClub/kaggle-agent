import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from modules.interaction.domain.models import (
    IncomingMessage,
    Job,
    JobStatus,
    ReplyDelivery,
    Source,
)
from modules.interaction.domain.repositories import DeliveryRepository, JobRepository
from shared.errors import AgentError


class SQLiteJobRepository(JobRepository, DeliveryRepository):
    """各操作は短い独立トランザクション。ワーカーはプロセス内で1本に限定。"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connection() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
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
                CREATE TABLE IF NOT EXISTS ai_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
            """)
            if version < 2:
                # v1のジョブ・通知を保持したまま、返信の復旧用テーブルだけを追加する。
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS reply_deliveries (
                        id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL
                    );
                    PRAGMA user_version=2;
                """)
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
        event_key = hashlib.sha256(
            f"{message.source.team_id}/{message.event_id}".encode(),
        ).hexdigest()
        job = Job(id=uuid4().hex, message=message, updated_at=time.time())
        with self.connection() as db:
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
                    AND other.status IN ('running','awaiting_approval')
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

    def recover(self) -> int:
        count = 0
        with self.connection() as db:
            for (payload,) in db.execute(
                "SELECT payload FROM jobs WHERE status IN ('running','awaiting_approval')",
            ).fetchall():
                job = Job.model_validate_json(payload)
                if job.status == JobStatus.AWAITING_APPROVAL:
                    if (job.approval_expires_at or 0) >= time.time():
                        continue
                    job = job.transition(JobStatus.FAILED, error_code="approval_expired")
                elif job.write_started:
                    job = job.transition(JobStatus.FAILED, error_code="uncertain_write")
                else:
                    job = job.model_copy(update={"status": JobStatus.QUEUED})
                self._save(db, job)
                if job.status == JobStatus.FAILED:
                    self._notify(
                        db,
                        job,
                        f"ジョブ {job.id}: {job.error_code}。再実行前に結果を照合してください。",
                    )
                count += 1
        return count

    def tracks(self, source: Source) -> bool:
        with self.connection() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE conversation_key=? LIMIT 1",
                    (source.conversation_key,),
                ).fetchone()
                is not None
            )

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
            count = db.execute("DELETE FROM jobs WHERE updated_at<?", (before,)).rowcount
            db.execute("DELETE FROM conversations WHERE updated_at<?", (before,))
            db.execute("DELETE FROM audit_logs WHERE created_at<?", (before,))
            db.execute("DELETE FROM notifications WHERE created_at<?", (before,))
            db.execute("DELETE FROM reply_deliveries WHERE updated_at<?", (before,))
        return count

    def consume_ai_budget(self, limit: int) -> bool:
        day = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO ai_usage VALUES (?,0)", (day,))
            return (
                db.execute(
                    "UPDATE ai_usage SET count=count+1 WHERE day=? AND count<?",
                    (day, limit),
                ).rowcount
                == 1
            )

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
