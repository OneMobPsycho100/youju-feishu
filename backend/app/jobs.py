"""SQLite 持久化任务队列：事务内入队，API 的单个后台线程顺序执行。"""
import logging
import threading
import time
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .config import settings
from .db import SessionLocal, begin_write
from .models import AICall, Job, SyncConfig, now
from .sync_config import load_sync_config
from .feishu_config import feishu_ready
from .ai_status import queue_ai_audit, settle_job_ai_audit, update_job_ai_audit

logger = logging.getLogger(__name__)
COMPENSATION_AUDIT_PREFIX = "compensation:audit:"


def enqueue(db, kind: str, payload: dict, dedupe_key: str | None = None) -> Job:
    """在调用方短写事务内入队；唯一幂等键保证重复事件不会产生多份任务。"""
    begin_write(db)
    if dedupe_key:
        previous = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if previous:
            return previous
    job = Job(kind=kind, payload=payload, dedupe_key=dedupe_key)
    try:
        with db.begin_nested():
            db.add(job)
            db.flush()
    except IntegrityError:
        if not dedupe_key:
            raise
        job = db.scalar(select(Job).where(Job.dedupe_key == dedupe_key))
        if job is None:
            raise
        return job
    queue_ai_audit(db, job)
    return job


def process_job(job_id: str) -> None:
    """通过条件 UPDATE 领取任务；领取事务提交后才执行网络、OCR 等耗时工作。"""
    from . import services
    from .imports import commit_import
    from .integrations import DomainError
    from .ai_logs import flush_call_snapshots
    from .reimbursement_metadata import backfill_applicant_name, backfill_metadata
    from .archive_metadata import backfill_archive
    from .reference_pdfs import JOB_KIND as REFERENCE_PDF_JOB, recognize_reference_pdfs

    with SessionLocal() as db:
        begin_write(db)
        claimed = db.execute(update(Job).where(Job.id == job_id, Job.status == "PENDING",
            Job.available_at <= now()).values(status="RUNNING", attempts=Job.attempts + 1, updated_at=now()))
        if claimed.rowcount == 1:
            update_job_ai_audit(db, [job_id], "RUNNING")
        db.commit()
        if claimed.rowcount != 1:
            return
        job = db.get(Job, job_id)
        kind, payload = job.kind, dict(job.payload)
        db.rollback()
        # 归档补审沿用父任务 ID，只有实际进入预审才绑定进度。
        db.info["current_job_id"] = job_id
        try:
            # 兼容发布前已排队的补偿任务，领取后仍须检查结论是否有效。
            if kind == "AUDIT" and (job.dedupe_key or "").startswith(COMPENSATION_AUDIT_PREFIX):
                payload["only_if_needed"] = True
            operations = {"AUDIT": services.run_audit, "ARCHIVE": services.archive_approval,
                          "ACTION": services.execute_action, "EVENT": services.handle_event,
                          "SYNC": services.sync_all, "IMPORT": commit_import, "REFRESH": services.refresh_original,
                          "REIMBURSEMENT_METADATA": backfill_metadata, "APPROVAL_APPLICANT_NAME": backfill_applicant_name,
                          "ARCHIVE_METADATA": backfill_archive, REFERENCE_PDF_JOB: recognize_reference_pdfs}
            # 只限制调度器创建的自动任务；手动补偿任务没有 sync: 幂等前缀。
            if kind == "SYNC" and (job.dedupe_key or "").startswith("sync:") and (
                    settings().app_mode != "live" or not load_sync_config(db).enabled):
                result = {"skipped": True, "reason": "自动增量补偿同步已关闭或当前为演示模式"}
            else:
                result = operations[kind](db, **payload)
            begin_write(db)
            job = db.get(Job, job_id)
            job.status, job.error, job.result = "SUCCEEDED", "", result or {}
            settle_job_ai_audit(db, job_id)
            db.commit()
        except Exception as exc:
            db.rollback()
            begin_write(db)
            job = db.get(Job, job_id)
            retryable = not isinstance(exc, DomainError) and job.attempts < 5
            job.status = "PENDING" if retryable else "FAILED"
            job.error = str(exc)[:800] if isinstance(exc, DomainError) else "外部服务或存储暂时不可用，请检查服务日志后重试"
            job.available_at = now() + timedelta(seconds=min(10 * 2 ** job.attempts, 300))
            update_job_ai_audit(db, [job_id], "QUEUED" if retryable else "FAILED")
            if kind == "ACTION" and not retryable:
                from .models import Approval, ApprovalAction
                record = db.get(ApprovalAction, payload.get("action_id"))
                # 尚未发出的操作可释放；UNKNOWN/EXECUTING 保留唯一操作键，重试先向飞书对账。
                if record and record.status == "REQUESTED":
                    record.status, record.task_operation_key = "FAILED", None
                    record.error = "回写准备失败，请重新审核后处理"
                    approval = db.get(Approval, record.approval_id)
                    if approval:
                        services.requeue_requested_audit(db, approval)
            db.commit()
            logger.error("job_id=%s kind=%s error_type=%s", job_id, kind, type(exc).__name__)
        finally:
            db.info.pop("current_job_id", None)
            flush_call_snapshots(db)


def pending_ids(*, archive_metadata=False) -> list[str]:
    """回收超过二十分钟的执行租约，再读取已提交、到执行时间的任务。"""
    from .reference_pdfs import JOB_KIND as REFERENCE_PDF_JOB

    with SessionLocal() as db:
        begin_write(db)
        cutoff = now() - timedelta(minutes=20)
        for target, attempts in (("PENDING", Job.attempts < 5), ("FAILED", Job.attempts >= 5)):
            recovered = list(db.scalars(update(Job).where(Job.status == "RUNNING", Job.updated_at < cutoff, attempts)
                .values(status=target, updated_at=now()).returning(Job.id)))
            update_job_ai_audit(db, recovered, "QUEUED" if target == "PENDING" else "FAILED")
        # 强制终止进程不会进入调用 finally；保留日志正文，只结束失去运行进程的调用。
        db.execute(update(AICall).where(AICall.status == "RUNNING", AICall.finished_at.is_(None),
            AICall.started_at < cutoff).values(status="FAILED", stage="运行异常中断", finished_at=now(),
                error_type="RunnerInterrupted", error_message="调用长时间未结束，运行进程可能已退出，请检查后台任务"))
        db.commit()
        statement = select(Job.id).where(Job.status == "PENDING", Job.available_at <= now())
        archive_kinds = ("ARCHIVE_METADATA", REFERENCE_PDF_JOB)
        statement = statement.where(Job.kind.in_(archive_kinds) if archive_metadata else Job.kind.not_in(archive_kinds))
        if settings().app_mode == "live" and not feishu_ready(db):
            statement = statement.where(Job.kind.not_in([
                "SYNC", "EVENT", "ACTION", "REFRESH", "REIMBURSEMENT_METADATA", "APPROVAL_APPLICANT_NAME", "ARCHIVE_METADATA",
            ]))
        return list(db.scalars(statement.order_by(Job.created_at).limit(20)))


def schedule_sync() -> None:
    """到期后原子入队并推进持久化计时，避免重启、配置变更或慢任务造成重复堆积。"""
    with SessionLocal() as db:
        config = load_sync_config(db)
        if config.next_run_at is None or config.next_run_at > now() or _auto_sync_in_flight(db):
            return
        # 读快照不能直接升级写事务：取得写锁后重新校验开关、到期时间与在途任务。
        begin_write(db)
        config = load_sync_config(db)
        if config.next_run_at is None or config.next_run_at > now() or _auto_sync_in_flight(db):
            return
        scheduled_at = now()
        enqueue(db, "SYNC", {}, f"sync:{config.version}:{scheduled_at.isoformat()}")
        db.get(SyncConfig, 1).schedule_anchor_at = scheduled_at
        db.commit()


def _auto_sync_in_flight(db) -> bool:
    """重试等待同样视为在途，自动任务未结束前不继续产生新的补偿任务。"""
    return db.scalar(select(Job.id).where(Job.kind == "SYNC", Job.dedupe_key.like("sync:%"),
        Job.status.in_(["PENDING", "RUNNING"])).limit(1)) is not None


def start_local_runner() -> tuple[threading.Event, threading.Thread]:
    """每个部署只运行一个 API 进程；线程退出由就绪检查报告，不静默丢失后台处理能力。"""
    stopped = threading.Event()

    def loop():
        from .reimbursement_metadata import schedule_metadata
        last_metadata = 0
        while not stopped.wait(1):
            try:
                # 统计元数据和姓名补齐使用独立间隔，不受自动审批同步开关影响。
                if (int(now().timestamp()) - last_metadata >= settings().sync_interval_seconds
                        and (settings().app_mode != "live" or feishu_ready())):
                    with SessionLocal() as db:
                        schedule_metadata(db, enqueue)
                    last_metadata = int(now().timestamp())
                schedule_sync()
                for job_id in pending_ids():
                    if stopped.is_set():
                        break
                    process_job(job_id)
            except Exception as exc:
                logger.error("local_dispatch error_type=%s", type(exc).__name__)

    thread = threading.Thread(target=loop, name="finance-jobs", daemon=True)
    thread.start()
    return stopped, thread


def start_archive_metadata_runner() -> tuple[threading.Event, threading.Thread]:
    """附件补齐可慢于审批同步，使用独立队列消费同一持久化任务表。"""
    from .archive_metadata import schedule_archive_metadata

    stopped = threading.Event()

    def loop():
        last_scheduled = float("-inf")
        while not stopped.wait(2):
            try:
                if time.monotonic() - last_scheduled >= settings().sync_interval_seconds:
                    if settings().app_mode != "live" or feishu_ready():
                        with SessionLocal() as db:
                            schedule_archive_metadata(db, enqueue)
                    last_scheduled = time.monotonic()
                for job_id in pending_ids(archive_metadata=True):
                    if stopped.is_set():
                        break
                    process_job(job_id)
            except Exception as exc:
                logger.error("archive_metadata_dispatch error_type=%s", type(exc).__name__)

    thread = threading.Thread(target=loop, name="archive-metadata", daemon=True)
    thread.start()
    return stopped, thread
