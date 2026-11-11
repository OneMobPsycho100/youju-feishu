"""已通过单据的包内 PDF 补充识别，只更新原件的票面展示数据。"""
from decimal import Decimal
from hashlib import sha256
import re

from sqlalchemy import select

from .attachment_archives import archive_members, archive_state
from .db import begin_write
from .errors import DomainError, StaleSnapshotError
from .invoice_recognition import recognition_cache_key, recognition_passed
from .models import Approval, Attachment, Job
from .parser import parse
from .storage import resolve


JOB_KIND = "REFERENCE_PDFS"
POLICY_VERSION = "reference-pdf-v1"


def reference_approval(approval: Approval) -> bool:
    return approval.status == "APPROVED" or approval.archive_status == "REVOKED"


def reference_pdfs(attachments: list[Attachment]) -> list[Attachment]:
    """只选当前已展开、仅供查阅的包内 PDF。参与业务审核的成员继续走原流程。"""
    members = archive_members([row for row in attachments if row.active])
    result = []
    for row in attachments:
        if row.active and row.mime == "application/pdf" and (member := members.get(row.id)):
            package = archive_state(member[0])
            if package.get("mode") == "REFERENCE" and package.get("status") == "READY":
                result.append(row)
    return result


def _cache_key(row: Attachment) -> str:
    # 本地 PDF 解析缓存只依赖原件和解析策略，不受模型或 OCR 配置影响。
    return f"{POLICY_VERSION}:{recognition_cache_key(row.sha256, None, file_type='.pdf')}"


def _result_kind(row: Attachment) -> str:
    parsed = row.parsed_data or {}
    return parsed.get("reference_pdf", "") if parsed.get("cache_key") == _cache_key(row) else ""


def reference_pdf_summary(rows: list[Attachment]) -> dict | None:
    """汇总当前包内票面，按票据身份去重。同号金额冲突时暂缓提供合计。"""
    if not rows:
        return None
    amounts: dict[str, Decimal] = {}
    travel, review, pending, conflict = 0, 0, 0, False
    for row in rows:
        kind = _result_kind(row)
        if kind == "INVOICE" and recognition_passed(row.parsed_data):
            fields = row.parsed_data["fields"]
            key, amount = fields["invoice_key"], Decimal(fields["amount"])
            conflict |= amounts.setdefault(key, amount) != amount
        elif kind == "TRAVEL":
            travel += 1
        elif kind:
            review += 1
        else:
            pending += 1
    return {"pdf_count": len(rows), "invoice_count": len(amounts), "travel_count": travel,
            "review_count": review, "pending_count": pending,
            "amount": sum(amounts.values(), Decimal(0)) if amounts and not conflict else None}


def enqueue_reference_pdfs(db, approval_id: str, rows: list[Attachment], *, automatic=False) -> Job | None:
    """同审批合并在途识别任务，自动补齐只为每组原件和解析策略入队一次。"""
    from .jobs import enqueue

    if not rows or (automatic and all(_result_kind(row) for row in rows)):
        return None
    current = db.scalar(select(Job).where(Job.kind == JOB_KIND, Job.status.in_(("PENDING", "RUNNING")),
        Job.payload["instance_code"].as_string() == approval_id).order_by(Job.created_at.desc()).limit(1))
    if current:
        return current
    dedupe = None
    if automatic:
        source = "|".join(sorted(f"{row.id}:{_cache_key(row)}" for row in rows))
        dedupe = f"reference-pdfs:{approval_id}:{sha256(source.encode()).hexdigest()}"
    return enqueue(db, JOB_KIND, {"instance_code": approval_id}, dedupe)


def _read_pdf(storage_key: str, cache_key: str) -> dict:
    path = resolve(storage_key)
    if not path.is_file():
        raise DomainError("包内 PDF 原件缺失，请补齐附件后重试")
    result = parse(path)
    if recognition_passed(result):
        kind, status = "INVOICE", "PARSED"
    else:
        text = re.sub(r"\s+", "", result.get("raw_text", ""))
        # 网约车行程单是独立的佐证。只在有明确标题且无发票标志时单列。
        is_travel = "行程单" in text[:240] and ("滴滴" in text or "网约车" in text) and "发票" not in text
        kind, status = ("TRAVEL", "SKIPPED") if is_travel else ("REVIEW", "FAILED")
        if is_travel:
            result.update(error="", material_type="OTHER", skip_reason="已读取网约车行程单，可与对应发票一起核对")
    return {**result, "reference_pdf": kind, "parse_status": status, "cache_key": cache_key}


def recognize_reference_pdfs(db, instance_code: str) -> dict:
    """事务外读票面，写入前复核原件快照。审批、审核记录、发票台账和占用均不参与写入。"""
    def load():
        approval = db.get(Approval, instance_code)
        if not approval or not reference_approval(approval):
            return []
        return reference_pdfs(list(db.scalars(select(Attachment).where(
            Attachment.approval_id == instance_code, Attachment.active.is_(True)))))

    rows = load()
    if not rows:
        return {"skipped": "no_reference_pdfs"}
    snapshot = {row.id: (row.sha256, row.storage_key) for row in rows}
    pending = [(row.id, row.storage_key, _cache_key(row)) for row in rows if not _result_kind(row)]
    if pending:
        db.rollback()
        results = {identity: _read_pdf(storage_key, key) for identity, storage_key, key in pending}
        begin_write(db)
        rows = load()
        if {row.id: (row.sha256, row.storage_key) for row in rows} != snapshot:
            raise StaleSnapshotError("包内 PDF 已变化，等待按当前原件重新识别")
        for row in rows:
            if row.id in results and not _result_kind(row):
                row.parsed_data = results[row.id]
    result = reference_pdf_summary(rows)
    db.commit()
    return {**result, "amount": str(result["amount"]) if result["amount"] is not None else None}
