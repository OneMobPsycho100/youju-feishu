"""整轮预审的独立进度。调用方持有短写事务，进度变化不递增审批业务版本。"""
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Approval, ApprovalTemplate, Job, now
from .pre_review import has_pending_pre_review_task


ACTIVE_AI_STATUSES = ("QUEUED", "RUNNING")


def payment_collection_enabled(template: ApprovalTemplate | None) -> bool:
    """判断付款策略，不含 active；停用模板仍需展示其配置，实际任务另校验接入状态。"""
    return bool(template and template.business_type == "PAYMENT" and template.payment_auto_approve_enabled)


def can_run_ai_approval(template: ApprovalTemplate | None) -> bool:
    """模板接入与 AI 开关开启且未采用付款仅采集策略时，才允许进入后台预审。"""
    return bool(template and template.active and template.ai_approval_enabled and not payment_collection_enabled(template))


def can_run_automatic_audit(approval: Approval | None, template: ApprovalTemplate | None) -> bool:
    """自动预审必须同时满足模板策略和专用待办资格；人工重审、归档补审另走各自入口。"""
    return can_run_ai_approval(template) and has_pending_pre_review_task(approval, template)


def automatic_audit_job(job: Job) -> bool:
    """兼容旧补偿任务只有幂等前缀、尚未携带 only_if_needed 的情况。"""
    from .jobs import COMPENSATION_AUDIT_PREFIX
    return job.kind == "AUDIT" and bool(
        job.payload.get("only_if_needed") or (job.dedupe_key or "").startswith(COMPENSATION_AUDIT_PREFIX))


def queue_ai_audit(db: Session, job: Job) -> None:
    """仅预审任务入队时绑定进度。回调先同步，归档需要补审时才开始显示预审进度。"""
    target = job.payload.get("instance_code") if job.kind == "AUDIT" else None
    approval = db.get(Approval, target) if target else None
    template = db.get(ApprovalTemplate, approval.template_code) if approval else None
    if approval and can_run_ai_approval(template):
        if automatic_audit_job(job) and not can_run_automatic_audit(approval, template):
            return
        from .manual_review import full_review
        if full_review(db, approval):
            return
        approval.ai_task_id, approval.ai_status = job.id, "QUEUED"
        approval.ai_requested_at = now()


def start_ai_audit(db: Session, approval: Approval, task_id: str) -> None:
    """在实际处理前记录进度；延迟执行的旧任务不能抢占更新任务的状态。"""
    from .manual_review import full_review
    if not can_run_ai_approval(db.get(ApprovalTemplate, approval.template_code)) or full_review(db, approval):
        return
    if approval.ai_task_id and approval.ai_task_id != task_id:
        candidate = db.get(Job, task_id)
        requested_at = candidate.created_at if candidate else now()
        # 显式重试复用旧 Job.id，不能只比较任务创建时间，否则旧请求会抢占这次更新的重试。
        if approval.ai_requested_at and approval.ai_requested_at >= requested_at:
            return
        approval.ai_requested_at = requested_at
    elif not approval.ai_requested_at:
        candidate = db.get(Job, task_id)
        approval.ai_requested_at = candidate.created_at if candidate else now()
    approval.ai_task_id, approval.ai_status = task_id, "RUNNING"


def complete_ai_audit(approval: Approval, task_id: str) -> None:
    """与新结论在同一事务内完成，仅更新仍属于本轮任务的进度。"""
    if approval.ai_task_id == task_id:
        approval.ai_status = "COMPLETED"


def settle_ai_audit(approval: Approval, task_id: str, *, waiting_for_node: bool = False) -> None:
    """跳过时恢复已有结论进度；等待节点的重审意图不等于已经排队，不展示虚假队列状态。"""
    if approval.ai_task_id != task_id or approval.ai_status not in ACTIVE_AI_STATUSES:
        return
    if approval.audit_requested and not waiting_for_node:
        approval.ai_status = "QUEUED"
    elif (approval.last_audit_id and approval.decision != "PENDING"
          and approval.audit_attachment_version == approval.attachment_version):
        approval.ai_status = "COMPLETED"
    else:
        approval.ai_status = "NOT_STARTED"


def settle_job_ai_audit(db: Session, job_id: str) -> None:
    """收尾跳过审核的任务，也兼容升级前已绑定进度的回调或归档任务。"""
    for approval in db.scalars(select(Approval).where(
            Approval.ai_task_id == job_id, Approval.ai_status.in_(ACTIVE_AI_STATUSES))):
        job = db.get(Job, job_id)
        template = db.get(ApprovalTemplate, approval.template_code)
        waiting = bool(job and automatic_audit_job(job) and not can_run_automatic_audit(approval, template))
        settle_ai_audit(approval, job_id, waiting_for_node=waiting)


def update_job_ai_audit(db: Session, job_ids: list[str], status: str) -> None:
    """领取、重试、失败及租约回收共用；已保存的预审结论不受后续归档失败影响。"""
    if job_ids:
        db.execute(update(Approval).where(Approval.ai_task_id.in_(job_ids),
            Approval.ai_status.in_(ACTIVE_AI_STATUSES)).values(ai_status=status))


def invalidate_ai_audit(approval: Approval) -> None:
    """业务内容或配置使结论失效时清除完成标记，在途任务仍由自身的版本复核和重试收尾。"""
    if approval.ai_status not in ACTIVE_AI_STATUSES:
        approval.ai_status = "NOT_STARTED"


def reset_ai_audit(approval: Approval) -> None:
    """关闭 AI 时解除任务归属，晚到的成功、失败和租约回收均无法再改变进度。"""
    approval.ai_status = "NOT_STARTED"
    approval.ai_task_id = approval.ai_requested_at = None
