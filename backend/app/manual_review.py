"""整单人工结论的公共边界：绑定业务内容，不绑定 AI、审核规则或处理进度。"""
import hashlib
import json
import copy

from sqlalchemy import select

from .attachment_archives import archive_members, archive_state
from .models import Approval, Attachment, AuditRun, now


def content_fingerprint(approval: Approval, attachments: list[Attachment]) -> str:
    """业务内容及人工票面修订的指纹；下载链接、自动提取结果和配置不参与比较。"""
    # 延迟引用以复用附件链接归一化逻辑，避免与审批服务的模块导入形成循环。
    from .services import audit_form_snapshot

    files = []
    members = archive_members(attachments)
    for attachment in attachments:
        if not attachment.active:
            continue
        parsed = attachment.parsed_data or {}
        if archive_state(attachment):
            parsed = parsed.get("previous_invoice_parse") or {}
        confirmation = (parsed.get("material") or {}).get("confirmation") or {}
        # 同一原包自动展开时，原件指纹继续代表其全部字节。只额外纳入成员上的人工修订。
        if attachment.id in members and not parsed.get("confirmed") and not confirmation:
            continue
        files.append({"source_key": attachment.source_key, "sha256": attachment.sha256,
                      "confirmed_fields": parsed.get("fields") if parsed.get("confirmed") else None,
                      "confirmed_material_type": confirmation.get("material_type")})
    material = {"version": 1, "business_type": approval.business_type, "title": approval.title,
                "amount": str(approval.amount), "expense_type": approval.expense_type,
                "applicant": approval.applicant_id or approval.applicant, "department": approval.department,
                "form": audit_form_snapshot(approval.raw_form), "attachment_version": approval.attachment_version,
                "files": sorted(files, key=lambda item: (item["source_key"], item["sha256"]))}
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def full_review(db, approval: Approval, *, audit: AuditRun | None = None,
                attachments: list[Attachment] | None = None) -> dict | None:
    """仅返回当前业务内容仍匹配的整单复核；逐项旧记录不能自动获得整单权限。"""
    audit = audit or (db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None)
    if not audit or audit.approval_id != approval.id:
        return None
    review = (audit.model_analysis or {}).get("manual_review") or {}
    if review.get("scope") != "ALL" or not review.get("content_fingerprint"):
        return None
    if attachments is None:
        attachments = list(db.scalars(select(Attachment).where(
            Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    return review if review["content_fingerprint"] == content_fingerprint(approval, attachments) else None


def apply_full_review(evidence: list[dict]) -> None:
    """整单人工确认覆盖全部规则级别，保留 result、原始原因及关联审批以供追溯。"""
    for item in evidence:
        if not item["passed"] and item["severity"] in {"REVIEW", "BLOCK"}:
            item.update(passed=True, severity="INFO", reviewed=True, reviewable=False,
                        auto_reject_eligible=False)


def conflict_snapshot(db, approval: Approval) -> list[dict]:
    """保存人工放行时的票据关联和冲突占用；不改变其他单据的唯一占用。"""
    from .models import InvoiceClaim

    invoice_ids = set(db.scalars(select(Attachment.invoice_id).where(
        Attachment.approval_id == approval.id, Attachment.active.is_(True), Attachment.invoice_id.is_not(None))))
    if not invoice_ids:
        return []
    return [{"invoice_id": claim.invoice_id, "claim_id": claim.id, "approval_id": claim.approval_id,
             "status": claim.status, "source_label": claim.source_label}
            for claim in db.scalars(select(InvoiceClaim).where(
                InvoiceClaim.invoice_id.in_(invoice_ids), InvoiceClaim.active_key.is_not(None)))
            if claim.approval_id != approval.id or claim.status == "FROZEN"]


def save_full_review(db, approval: Approval, actor_id: str, actor_name: str, comment: str,
                     *, promoted_review: dict | None = None) -> AuditRun:
    """在调用方的写事务中保存整单复核；升级旧记录时保留原作者、时间和意见并独立留痕。"""
    from .services import log

    audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
    attachments = list(db.scalars(select(Attachment).where(
        Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    fingerprint = content_fingerprint(approval, attachments)
    evidence = copy.deepcopy(audit.evidence) if audit else []
    codes = sorted({item["code"] for item in evidence
                    if item.get("reviewed") or not item["passed"] and item["severity"] in {"REVIEW", "BLOCK"}})
    apply_full_review(evidence)
    analysis = copy.deepcopy(audit.model_analysis) if audit else {
        "status": "SKIPPED", "summary": "财务已整单复核，本次未调用 AI"}
    reviewed = AuditRun(approval_id=approval.id, rule_version=audit.rule_version if audit else "manual-v1",
                        attachment_version=approval.attachment_version, conclusion="PASS",
                        evidence=evidence, model_analysis=analysis)
    db.add(reviewed)
    db.flush()
    review = {"audit_id": reviewed.id, "source_audit_id": audit.id if audit else "",
              "actor_id": actor_id, "actor_name": actor_name, "comment": comment,
              "reviewed_codes": codes, "confirmed_codes": codes, "scope": "ALL",
              "created_at": now().isoformat()+"Z", "review_fingerprint": fingerprint,
              "content_fingerprint": fingerprint, "conflicting_claims": conflict_snapshot(db, approval)}
    if promoted_review:
        for key in ("actor_id", "actor_name", "comment", "created_at"):
            review[key] = promoted_review[key]
        review.update(promoted_from=promoted_review["audit_id"], promoted_at=now().isoformat()+"Z")
    reviewed.model_analysis = {**analysis, "manual_review": review}
    approval.decision, approval.last_audit_id = "PASS", reviewed.id
    approval.audit_attachment_version, approval.audit_requested = approval.attachment_version, False
    # 脱离已在执行的 AI 任务，使其迟到的失败或结果只能落日志，不能改写人工结论的进度。
    approval.ai_task_id = None
    if approval.ai_status in {"RUNNING", "QUEUED"}:
        approval.ai_status = "NOT_STARTED"
    approval.version += 1
    log(db, actor_name, "升级整单人工复核" if promoted_review else "整单人工复核", approval.serial_number, review)
    return reviewed
