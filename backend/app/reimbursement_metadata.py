"""审批统计与姓名补齐；任务仅更新元数据，不触发审批业务流程。"""
from datetime import datetime, timedelta, timezone
from hashlib import sha256

from sqlalchemy import and_, func, or_, select, update

from .config import settings
from .db import begin_write
from .integrations import DomainError, FeishuGateway, gateway
from .models import Approval, Job, User


_UNKNOWN_APPLICANT = "未知申请人"


def feishu_time(value) -> datetime | None:
    """飞书正整数毫秒时间戳，转换为数据库使用的无时区 UTC。"""
    try:
        if not isinstance(value, bool) and isinstance(value, (str, int)) and int(value) > 0:
            return datetime(1970, 1, 1) + timedelta(milliseconds=int(value))
    except (ValueError, OverflowError):
        pass
    return None


def snapshot_metadata(snapshot: dict) -> dict:
    """提取可用于补齐历史申请时间和申请人身份的权威字段。"""
    result = {}
    if submitted := feishu_time(snapshot.get("start_time")):
        result["submitted_at"] = submitted
    open_id, user_id = str(snapshot.get("open_id") or "").strip(), str(snapshot.get("user_id") or "").strip()
    identity = open_id or ("user_id:" + user_id if user_id else "")
    if identity and len(identity) <= 120:
        result["applicant_id"] = identity
    return result


def apply_snapshot_metadata(approval: Approval, snapshot: dict) -> None:
    """普通审批同步共用此入口；只有统计元数据变化时不递增审批审核版本。"""
    for key, value in snapshot_metadata(snapshot).items():
        setattr(approval, key, value)


def ensure_demo_metadata(db) -> None:
    """仅由演示初始化调用；旧演示单用原采集时间明确模拟提交时间，身份按姓名稳定生成。"""
    begin_write(db)
    for approval in db.scalars(select(Approval).where(Approval.template_code == "DEMO-EXPENSE")):
        snapshot = dict(approval.external_snapshot or {})
        if not snapshot:
            continue
        original = snapshot_metadata(snapshot)
        submitted = original.get("submitted_at") or approval.submitted_at or approval.created_at
        identity = original.get("applicant_id") or approval.applicant_id or (
            "demo-applicant-" + sha256(approval.applicant.encode()).hexdigest()[:24])
        original_snapshot = dict(snapshot)
        if "submitted_at" not in original or "applicant_id" not in original:
            snapshot.update(start_time=int(submitted.replace(tzinfo=timezone.utc).timestamp() * 1000),
                            open_id=identity, statistics_time_simulated=True)
        if snapshot.get("status") == "APPROVED" and not feishu_time(snapshot.get("end_time")):
            # 仅为历史演示单构造通过时间；真实审批只读取飞书的终态时间。
            finished = min(max(approval.updated_at, submitted), submitted + timedelta(hours=1))
            snapshot["end_time"] = int(finished.replace(tzinfo=timezone.utc).timestamp() * 1000)
            snapshot["archive_time_simulated"] = True
        if snapshot != original_snapshot:
            db.execute(update(Approval).where(Approval.id == approval.id).values(external_snapshot=snapshot,
                submitted_at=submitted, applicant_id=identity, updated_at=Approval.updated_at))
        elif approval.submitted_at is None or approval.applicant_id is None:
            db.execute(update(Approval).where(Approval.id == approval.id).values(**original, updated_at=Approval.updated_at))
    db.commit()


def backfill_metadata(db, instance_code: str) -> dict:
    """单据独立任务：先读取远端，释放读事务，再在短写事务中仅补充缺失字段。"""
    approval = db.get(Approval, instance_code)
    if not approval:
        return {"skipped": True}
    if approval.submitted_at is not None and approval.applicant_id:
        return {"approval_id": instance_code, "already_complete": True}
    db.rollback()
    snapshot = gateway(db).get_instance(instance_code)
    metadata = snapshot_metadata(snapshot)
    begin_write(db)
    approval = db.get(Approval, instance_code)
    if not approval:
        db.rollback()
        return {"skipped": True}
    missing = {key: value for key, value in metadata.items() if not getattr(approval, key)}
    if missing:
        # 保留原更新时间和审核版本；元数据补齐不会令进行中的审批操作过期。
        db.execute(update(Approval).where(Approval.id == instance_code).values(**missing, updated_at=Approval.updated_at))
    complete = (approval.submitted_at is not None or "submitted_at" in missing) and bool(approval.applicant_id or missing.get("applicant_id"))
    db.commit()
    if not complete:
        raise DomainError("飞书实例未返回有效提交时间或报销人身份；已保留取得的字段，请在后台任务中重试")
    return {"approval_id": instance_code, "completed": True}


def _needs_applicant_name():
    """只查已有飞书身份、展示值仍是身份标识或占位符的记录。"""
    identity = Approval.applicant_id
    name = func.trim(Approval.applicant)
    is_user_id = func.substr(identity, 1, 8) == "user_id:"
    return and_(
        or_(and_(func.substr(identity, 1, 3) == "ou_", func.length(identity) > 3),
            and_(is_user_id, func.length(identity) > 8)),
        or_(name.in_(("", _UNKNOWN_APPLICANT)), name == identity,
            func.substr(name, 1, 3).in_(("ou_", "on_")), func.substr(name, 1, 8) == "user_id:",
            and_(is_user_id, name == func.substr(identity, 9))),
    )


def _display_name(value: str | None, identity: str) -> str:
    name = (value or "").strip()
    if name in (_UNKNOWN_APPLICANT, identity.removeprefix("user_id:")) or name.startswith(("ou_", "on_", "user_id:")):
        return ""
    return name


def backfill_applicant_name(db, instance_code: str) -> dict:
    """按申请人稳定身份补齐展示姓名，独立于单据版本和审核结论。"""
    target = select(Approval.applicant_id).where(Approval.id == instance_code, _needs_applicant_name())
    identity = db.scalar(target)
    if not identity:
        return {"approval_id": instance_code, "skipped": True}
    user = db.get(User, identity)
    name = _display_name(user.name if user else None, identity)
    db.rollback()
    if not name:
        names = FeishuGateway().archive_people([identity], strict=True)
        name = _display_name(names.get(identity), identity)

    # 通讯录调用期间可能已同步新身份或取得姓名，持写锁再次确认目标。
    begin_write(db)
    current = db.scalar(target)
    if not current:
        db.rollback()
        return {"approval_id": instance_code, "skipped": True}
    if current != identity:
        raise RuntimeError("读取姓名期间申请人身份已更新，任务将重试")
    if not name:
        raise DomainError("飞书未返回申请人姓名，请核对通讯录数据范围和用户基本信息权限后重试")
    db.execute(update(Approval).where(Approval.id == instance_code).values(
        applicant=name[:100], updated_at=Approval.updated_at))
    db.commit()
    return {"approval_id": instance_code, "completed": True}


def schedule_metadata(db, enqueue) -> None:
    """统计字段和姓名各自限量入队，已调度的失败任务可在任务中心重试。"""
    begin_write(db)
    key = "reimbursement-metadata:" + Approval.id
    scheduled = select(Job.id).where(Job.dedupe_key == key).exists()
    rows = db.scalars(select(Approval.id).where(
        or_(Approval.submitted_at.is_(None), Approval.applicant_id.is_(None), Approval.applicant_id == ""),
        ~scheduled).order_by(Approval.created_at, Approval.id).limit(20)).all()
    for instance_code in rows:
        enqueue(db, "REIMBURSEMENT_METADATA", {"instance_code": instance_code}, f"reimbursement-metadata:{instance_code}")
    if settings().app_mode == "live":
        key = "approval-applicant-name:" + Approval.id
        scheduled = select(Job.id).where(Job.dedupe_key == key).exists()
        rows = db.scalars(select(Approval.id).where(_needs_applicant_name(), ~scheduled)
                          .order_by(Approval.created_at, Approval.id).limit(20)).all()
        for instance_code in rows:
            enqueue(db, "APPROVAL_APPLICANT_NAME", {"instance_code": instance_code}, f"approval-applicant-name:{instance_code}")
    db.commit()
