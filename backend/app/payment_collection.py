"""付款仅采集：放行和材料读取分别入队，读取结果不产生审批结论。"""
import copy
import hashlib
import json
from datetime import date
from decimal import Decimal

from sqlalchemy import select, update

from .ai_status import payment_collection_enabled, reset_ai_audit
from .attachment_archives import archive_members, archive_state, has_unread_archive
from .db import begin_write
from .errors import StaleSnapshotError
from .integrations import DomainError
from .invoice_recognition import PDF_TEXT_VERSION, recognition_cache_key, recognition_cache_valid, recognition_passed
from .audit_policy import PAYMENT_PDF_POLICY_VERSION
from .jobs import enqueue
from .model_config import load_model_config
from .models import Approval, ApprovalAction, ApprovalTemplate, Attachment, Invoice, InvoiceClaim, Job
from .ocr_config import load_ocr_config
from .parser import normalize, parse_with_fallback
from .pre_review import has_completed_pre_review_task, has_pending_pre_review_task
from .payment_materials import MATERIAL_VERSION, read_payment_material
from .storage import resolve


JOB_KIND = "PAYMENT_COLLECT"
ACTION_SOURCE = "PAYMENT_AUTO_APPROVE"
TERMINAL_REJECTIONS = {"REJECTED", "CANCELED", "DELETED", "REVERTED"}


def collection_node_scope(template: ApprovalTemplate) -> str:
    """采集资格绑定模板、节点集合及专用身份；不加入材料指纹，避免重复识别已有原件。"""
    value = [template.code, sorted(set(template.node_ids)), template.reviewer_open_id]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def collection_node_reached(db, approval: Approval, template: ApprovalTemplate, *,
                            tasks: list[dict] | None = None) -> bool:
    """确认采集起点。tasks 只能传入本轮飞书权威快照，旧任务 ID 本身不能证明到过节点。

    已获资格的采集可以跨后续节点继续；节点或身份配置变化后，原凭据不再有效。
    最终通过允许归档补采，拒绝、撤回等终态不再启动或继续自动读取。
    """
    if approval.status == "APPROVED":
        return True
    if approval.status != "PENDING":
        return False
    previous = db.get(Job, approval.payment_collection_task_id) if approval.payment_collection_task_id else None
    if previous and previous.kind == JOB_KIND and previous.payload.get("pre_review_scope") == collection_node_scope(template):
        return True
    if approval.task_status == "AMBIGUOUS":
        return False
    return has_pending_pre_review_task(approval, template) or (tasks is not None
        and has_completed_pre_review_task(tasks, template.node_ids, template.reviewer_open_id))


def collection_fingerprint(db, approval: Approval) -> str:
    """只绑定稳定业务内容和识别配置；临时下载地址不使采集结果失效。"""
    from .services import audit_form_snapshot

    attachments = list(db.scalars(select(Attachment).where(
        Attachment.approval_id == approval.id, Attachment.active.is_(True)).order_by(Attachment.id)))
    config, ocr = load_model_config(db), load_ocr_config(db)
    # 附件控件中的临时链接由原件哈希替代核验，普通表单内容仍须精确比较。
    content = {"form": audit_form_snapshot(approval.raw_form), "attachments": approval.attachment_version,
        "issues": approval.payment_details.get("material_issues", []),
        "material_version": [MATERIAL_VERSION, PAYMENT_PDF_POLICY_VERSION, PDF_TEXT_VERSION],
        "recognition": recognition_cache_key("", config, ocr_version=ocr.recognition_version),
        "confirmations": [(a.id, a.sha256, (a.parsed_data or {}).get("confirmed_at"),
            ((a.parsed_data or {}).get("material") or {}).get("confirmation")) for a in attachments]}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def enqueue_payment_work(db, approval: Approval, *, tasks: list[dict] | None = None) -> None:
    """调用方持有写事务；到专用节点才采集。tasks 用于刚同步后恢复已完成节点的资格。"""
    from .services import active_approval_action, request_action

    template = db.get(ApprovalTemplate, approval.template_code)
    if not payment_collection_enabled(template) or not template.active:
        return
    db.flush()
    if has_pending_pre_review_task(approval, template) and not active_approval_action(db, approval.id):
        operation_key = f"{approval.id}:{approval.task_id}"
        previous = db.scalar(select(ApprovalAction.id).where(ApprovalAction.task_operation_key == operation_key))
        if not previous:
            request_key = hashlib.sha256(f"{approval.id}:{approval.task_id}:{approval.version}".encode()).hexdigest()
            record = request_action(db, approval, "APPROVE", "同意", approval.version,
                f"payment-pass:{request_key}", "system-agent", "付款采集服务",
                execution_source=ACTION_SOURCE)
            enqueue(db, "ACTION", {"action_id": record.id}, f"action:{record.id}")
    if not collection_node_reached(db, approval, template, tasks=tasks):
        return
    node_scope = collection_node_scope(template)
    fingerprint = collection_fingerprint(db, approval)
    job = enqueue(db, JOB_KIND, {"instance_code": approval.id, "fingerprint": fingerprint, "pre_review_scope": node_scope},
        f"payment-collect:{approval.id}:{fingerprint}")
    # 同内容仍复用原任务和缓存。只有本轮已确认节点资格，才给旧任务补充或更新凭据。
    job.payload = {**job.payload, "pre_review_scope": node_scope}
    if job.status == "SUCCEEDED" and (job.result or {}).get("skipped"):
        # 切换策略导致旧轮次跳过时，同内容重新开启仍应采集，而不是永远复用跳过状态。
        job.status, job.result, job.error, job.attempts = "PENDING", {}, "", 0
    approval.payment_collection_task_id = job.id
    approval.payment_collection_fingerprint = fingerprint


def reset_payment_work(db, template: ApprovalTemplate, *, reset_nodes: bool = False) -> None:
    """切换策略时只取消未发出的操作；已发出请求继续持锁对账，刷新后再选择新策略。"""
    from .services import log
    from .manual_review import full_review

    ids = select(Approval.id).where(Approval.template_code == template.code, Approval.status == "PENDING")
    db.execute(update(ApprovalAction).where(ApprovalAction.approval_id.in_(ids),
        ApprovalAction.status == "REQUESTED").values(status="FAILED", task_operation_key=None,
            error="付款采集策略或节点配置已变化，未发送的旧操作已停止"))
    db.execute(update(Job).where(Job.kind == "AUDIT", Job.status == "PENDING",
        Job.payload["instance_code"].as_string().in_(ids)).values(
            status="SUCCEEDED", error="", result={"skipped": "payment_strategy_changed"}))
    for approval in db.scalars(select(Approval).where(Approval.id.in_(ids))):
        approval.version += 1
        if not full_review(db, approval):
            approval.decision, approval.audit_attachment_version = "PENDING", ""
        approval.audit_requested = False
        reset_ai_audit(approval)
        # 解除旧采集任务归属；晚到结果只能保留调用日志，不能覆盖业务原件。
        approval.payment_collection_task_id = None
        if reset_nodes:
            approval.task_id = approval.node_id = approval.assignee = ""
            approval.task_status = "MISSING"
        if template.active:
            enqueue(db, "REFRESH", {"instance_code": approval.id}, f"payment-refresh:{approval.id}:{approval.version}")
    log(db, "付款采集服务", "刷新付款处理策略", template.name,
        {"payment_auto_approve_enabled": template.payment_auto_approve_enabled})


def restore_payment_work(db) -> None:
    """启动时消除升级前的排队审核，并刷新存量待办；重复启动不重复提交同意。"""
    templates = list(db.scalars(select(ApprovalTemplate).where(ApprovalTemplate.business_type == "PAYMENT",
        ApprovalTemplate.payment_auto_approve_enabled.is_(True))))
    for template in templates:
        ids = select(Approval.id).where(Approval.template_code == template.code)
        db.execute(update(ApprovalAction).where(ApprovalAction.approval_id.in_(ids),
            ApprovalAction.execution_source == "STANDARD", ApprovalAction.status == "REQUESTED").values(
                status="FAILED", task_operation_key=None, error="付款已改为仅采集，旧审核操作已停止"))
        db.execute(update(Job).where(Job.kind == "AUDIT", Job.status == "PENDING",
            Job.payload["instance_code"].as_string().in_(ids)).values(
                status="SUCCEEDED", error="", result={"skipped": "payment_collection_enabled"}))
        for approval in db.scalars(select(Approval).where(Approval.template_code == template.code,
                Approval.status.in_(["PENDING", "APPROVED"]))):
            reset_ai_audit(approval)
            approval.audit_requested = False
            if template.active:
                # 已完成的 REFRESH 不能阻止下一次启动读取最新权威待办。
                existing = db.scalar(select(Job.id).where(Job.kind == "REFRESH", Job.status.in_(["PENDING", "RUNNING"]),
                    Job.payload["instance_code"].as_string() == approval.id))
                if not existing:
                    enqueue(db, "REFRESH", {"instance_code": approval.id})


def collect_payment(db, instance_code: str, fingerprint: str, *, pre_review_scope: str | None = None) -> dict:
    """读取前和落库前校验资格、内容及任务归属，不修改回写版本。

    pre_review_scope 是到达专用节点后保存的配置范围凭据；旧任务缺省时先同步飞书确认，
    不能仅因升级前已经排队就调用模型。合法采集不要求节点持续为 PENDING。
    """
    from .services import canonical_invoice, log, reserve_invoice, sync_instance

    job_id = db.info.get("current_job_id")
    if pre_review_scope is None:
        # 先排除已被替换的旧任务，避免它通过同步重新安排过期工作。
        approval = db.get(Approval, instance_code)
        if not approval or not job_id or approval.payment_collection_task_id != job_id:
            db.rollback()
            return {"skipped": "stale_payment_collection"}
        db.rollback()
        sync_instance(db, instance_code)
        job = db.get(Job, job_id)
        pre_review_scope = job.payload.get("pre_review_scope") if job else None
        db.rollback()
        if not pre_review_scope:
            return {"skipped": "pre_review_not_reached"}

    def current():
        approval = db.get(Approval, instance_code)
        template = db.get(ApprovalTemplate, approval.template_code) if approval else None
        job = db.get(Job, job_id) if job_id else None
        return bool(approval and template and template.active and payment_collection_enabled(template)
            and approval.status in {"PENDING", "APPROVED"}
            and pre_review_scope == collection_node_scope(template)
            and job and job.payload.get("pre_review_scope") == pre_review_scope
            and approval.payment_collection_task_id == job_id
            and approval.payment_collection_fingerprint == fingerprint
            and collection_fingerprint(db, approval) == fingerprint)

    def before_request():
        db.rollback()
        db.expire_all()
        valid = current()
        db.rollback()
        if not valid:
            raise StaleSnapshotError("付款采集内容或策略已变化，停止旧材料读取")

    if not current():
        db.rollback()
        return {"skipped": "stale_payment_collection"}
    approval = db.get(Approval, instance_code)
    template_code = approval.template_code
    config, ocr = load_model_config(db), load_ocr_config(db)
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == instance_code,
        Attachment.active.is_(True))))
    issues = [{"code": "DOWNLOAD", "filename": i["filename"], "detail": i["reason"]}
              for i in approval.payment_details.get("material_issues", [])]
    members = archive_members(attachments)
    db.rollback()
    # rollback 会过期 ORM，先恢复属性再脱离会话，防止解析结果被短读校验回滚。
    for attachment in attachments:
        _ = attachment.parsed_data, attachment.storage_key, attachment.sha256, attachment.filename, attachment.invoice_id
    db.expunge_all()
    db.rollback()
    parsed, invoices = {}, {}
    try:
        for attachment in attachments:
            before_request()
            state = archive_state(attachment)
            member = members.get(attachment.id)
            if state or (member and archive_state(member[0]).get("mode") == "REFERENCE"):
                if state and state.get("status") != "READY":
                    issues.append({"code": "ARCHIVE", "filename": attachment.filename,
                        "detail": state.get("error") or "压缩包尚未完整展开"})
                continue
            try:
                material = read_payment_material(resolve(attachment.storage_key), config, sha256=attachment.sha256,
                    approval_id=instance_code, template_code=template_code, parent_db=db,
                    previous=attachment.parsed_data, invoice_linked=bool(attachment.invoice_id), before_request=before_request, ocr_config=ocr)
                result = {**copy.deepcopy(attachment.parsed_data or {}), **material}
                if material.get("material_type") != "INVOICE":
                    result["error"] = ""
                reading = material.get("material") or {}
                if not reading.get("complete"):
                    issues.append({"code": "MATERIAL", "filename": attachment.filename,
                        "detail": reading.get("error") or reading.get("summary") or "材料未完整读取"})
                if material.get("material_type") == "INVOICE":
                    if not result.get("document_policy_version") and not recognition_cache_valid(result, attachment.sha256, config, ocr_version=ocr.recognition_version):
                        result = {**parse_with_fallback(resolve(attachment.storage_key), config, ocr_config=ocr,
                            approval_id=instance_code, template_code=template_code, parent_db=db, sha256=attachment.sha256,
                            previous=attachment.parsed_data, before_request=before_request), **material}
                    result["parse_status"] = "FAILED" if result.get("error") else "PARSED"
                    if recognition_passed(result) and not result.get("error"):
                        invoices[attachment.id] = normalize(result.get("fields") or {})
                    else:
                        issues.append({"code": "RECOGNITION", "filename": attachment.filename,
                            "detail": result.get("error") or (result.get("recognition") or {}).get("reason") or "发票字段尚未可靠识别"})
                parsed[attachment.id] = result
            except StaleSnapshotError:
                raise
            except Exception as exc:
                message = str(exc)[:300] if isinstance(exc, (ValueError, DomainError)) else "材料读取失败，可重试采集"
                parsed[attachment.id] = {**copy.deepcopy(attachment.parsed_data or {}), "parse_status": "FAILED", "error": message}
                issues.append({"code": "PARSE", "filename": attachment.filename, "detail": message})
    except StaleSnapshotError:
        db.rollback()
        return {"skipped": "stale_payment_collection"}

    begin_write(db)
    if not current():
        db.commit()
        return {"skipped": "stale_payment_collection"}
    approval = db.get(Approval, instance_code)
    active = {a.id: a for a in db.scalars(select(Attachment).where(Attachment.approval_id == instance_code,
        Attachment.active.is_(True)))}
    company_ids = {approval.payment_details["company_id"]} if approval.payment_details.get("company_id") else set()
    observations = {}
    for attachment_id, fields in invoices.items():
        observations.setdefault(fields["invoice_key"], []).append((attachment_id, fields))
    for attachment_id, result in parsed.items():
        active[attachment_id].parsed_data = result
    for entries in observations.values():
        fields = entries[0][1]
        if any(item != fields for _, item in entries):
            issues.append({"code": "FIELD_CONFLICT", "filename": active[entries[0][0]].filename,
                "detail": "同票多份原件字段冲突，未覆盖统一台账"})
            continue
        invoice = canonical_invoice(db, fields)
        if (invoice.buyer_tax_id != fields["buyer_tax_id"] or invoice.issue_date != date.fromisoformat(fields["issue_date"])
                or invoice.amount != Decimal(fields["amount"])):
            issues.append({"code": "CANONICAL", "filename": active[entries[0][0]].filename,
                "detail": "票面与历史台账不一致，保留原记录"})
            continue
        for attachment_id, _ in entries:
            active[attachment_id].invoice_id = invoice.id
        if invoice.company_id:
            company_ids.add(invoice.company_id)
        if approval.status not in TERMINAL_REJECTIONS and not reserve_invoice(db, invoice, approval):
            issues.append({"code": "DUPLICATE", "filename": active[entries[0][0]].filename,
                "detail": "发票已被其他单据占用或冻结，保留原占用，仅记录付款材料关联"})
    # 未读原包或识别失败时保留旧关联；通过后移除的原件继续冻结，不能释放已使用票据。
    linked = {a.invoice_id for a in active.values() if a.invoice_id}
    if not has_unread_archive(list(active.values())):
        for claim in db.scalars(select(InvoiceClaim).where(InvoiceClaim.approval_id == instance_code)):
            if claim.active_key and claim.invoice_id not in linked:
                claim.status = "FROZEN" if approval.approved_once else "RELEASED"
                if claim.status == "RELEASED":
                    claim.active_key = None
    if linked:
        company_ids.update(value for value in db.scalars(select(Invoice.company_id).where(Invoice.id.in_(linked))) if value)
    approval.company_ids = sorted(company_ids)
    log(db, "付款采集服务", "完成付款材料采集", approval.serial_number,
        {"task_id": job_id, "issues": len(issues), "invoice_count": len(observations)})
    if approval.status == "APPROVED":
        # 原件已经在同步阶段持久化，采集后归档直接使用当前快照，避免再次下载并触发新采集。
        enqueue(db, "ARCHIVE", {"instance_code": instance_code, "already_synced": True},
            f"payment-archive:{instance_code}:{job_id}")
    db.commit()
    return {"approval_id": instance_code, "issues": issues, "invoice_count": len(observations)}
