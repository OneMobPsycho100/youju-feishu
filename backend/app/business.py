"""页面与 Agent 共用的业务入口；commit=False 允许确认提案与任务入队在同一事务提交。"""
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
import copy

from fastapi import HTTPException
from sqlalchemy import String, and_, cast, func, or_, select

from . import schemas as s
from .config import settings
from .db import begin_write
from .feishu_form import reimbursement_form
from .integrations import DomainError
from .invoice_display import invoice_display
from .jobs import enqueue
from .ai_status import can_run_ai_approval, queue_ai_audit
from .models import ActivityLog, Approval, ApprovalAction, ApprovalTemplate, ArchiveRecord, Attachment, AuditRun, Company, Invoice, InvoiceClaim, Job, User, now
from .parser import normalize
from .services import audit_conclusion, audit_config_matches, feishu_link, log, request_action
from .audit_policy import apply_evidence_review, reviewable
from .audit_messages import rejection_comment
from .company_subjects import declared_company_names
from .manual_review import full_review, save_full_review
from .reference_pdfs import enqueue_reference_pdfs, reference_approval, reference_pdf_summary, reference_pdfs

def page_result(db, statement, serializer, page, page_size):
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    items = db.scalars(statement.offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [serializer(item) for item in items], "total": total, "page": page, "page_size": page_size}


def audit_dump(row):
    return s.AuditOut.model_validate(row).model_dump(mode="json") if row else None


def _local_applicant_name():
    """按审批发起人的 open_id 读取本地姓名，供展示与搜索共用。"""
    # 旧单缺少身份字段时，原申请人值可能仍保存着 open_id。
    identity = func.coalesce(func.nullif(Approval.applicant_id, ""), Approval.applicant)
    name = func.trim(User.name)
    return (
        select(name)
        .where(
            User.id == identity,
            func.substr(User.id, 1, 3) == "ou_",
            name.regexp_match(r"\S"),
            name != User.id,
        )
        .correlate(Approval)
        .scalar_subquery()
    )


def approval_rows_dump(db, rows: list[Approval], *, attachments=None) -> list[dict]:
    """批量读取申请人姓名和主体展示信息，票面主体使用当前有效附件。"""
    if not rows:
        return []
    by_id = {row.id: row for row in rows}
    applicant_names = dict(db.execute(
        select(Approval.id, _local_applicant_name()).where(Approval.id.in_(list(by_id)))
    ).all())
    company_ids = {company_id for row in rows for company_id in row.company_ids}
    companies = {company.id: company for company in db.scalars(select(Company).where(Company.id.in_(company_ids)))} if company_ids else {}
    template_codes = {row.template_code for row in rows}
    templates = {row.code: row for row in db.execute(select(
        ApprovalTemplate.code, ApprovalTemplate.field_mapping, ApprovalTemplate.ai_approval_enabled).where(
            ApprovalTemplate.code.in_(template_codes)))}
    unmatched = {row.id: [] for row in rows}
    if attachments is None:
        attachments = db.scalars(select(Attachment).where(
            Attachment.approval_id.in_(list(by_id)), Attachment.active.is_(True)).order_by(Attachment.created_at)).all()
    for attachment in attachments:
        if not attachment.active or attachment.approval_id not in by_id:
            continue
        fields = (attachment.parsed_data or {}).get("fields") or {}
        name = str(fields.get("buyer_name") or "").strip()
        tax_id = str(fields.get("buyer_tax_id") or "").strip().upper()
        row = by_id[attachment.approval_id]
        matched = any(companies[company_id].tax_id == tax_id for company_id in row.company_ids if company_id in companies)
        if name and not matched and name not in unmatched[row.id]:
            unmatched[row.id].append(name)
    audits = {audit.id: audit for audit in db.scalars(select(AuditRun).where(
        AuditRun.id.in_([row.last_audit_id for row in rows if row.last_audit_id])))}
    files_by_approval = defaultdict(list)
    for attachment in attachments:
        files_by_approval[attachment.approval_id].append(attachment)
    # 申报名称和未匹配的票面名称作为独立展示字段，不能据此自动确认主体或放行审批。
    return [{**s.ApprovalOut.model_validate(row).model_dump(mode="json"),
             "ai_approval_enabled": templates[row.template_code].ai_approval_enabled,
             "manual_review_valid": bool(full_review(db, row, audit=audits.get(row.last_audit_id),
                                                      attachments=files_by_approval[row.id])),
             "applicant": applicant_names.get(row.id) or row.applicant,
             "declared_company_names": declared_company_names(row.raw_form, templates[row.template_code].field_mapping),
             "unmatched_company_names": unmatched[row.id]} for row in rows]


def overview(db, company_id: str = ""):
    approvals = list(db.scalars(select(Approval).order_by(Approval.created_at.desc())))
    if company_id:
        approvals = [a for a in approvals if company_id in a.company_ids]
    pending = [a for a in approvals if a.status == "PENDING" and a.task_status == "PENDING"]
    ids = [a.id for a in approvals]
    invoices = list(db.scalars(select(Invoice).where(Invoice.company_id == company_id))) if company_id else list(db.scalars(select(Invoice)))
    records = list(db.scalars(select(ArchiveRecord).where(ArchiveRecord.approval_id.in_(ids))))
    recent = now() + timedelta(hours=8) - timedelta(days=29)
    trend = []
    for offset in range(30):
        day = (recent + timedelta(days=offset)).date()
        daily = [a for a in approvals if (a.created_at + timedelta(hours=8)).date() == day]
        trend.append({"date": day.isoformat(), "amount": str(sum((a.amount for a in daily), Decimal("0"))), "count": len(daily)})
    risks = {}
    ai_templates = set(db.scalars(select(ApprovalTemplate.code).where(ApprovalTemplate.ai_approval_enabled.is_(True))))
    for item in pending:
        if item.template_code not in ai_templates:
            continue
        audit = db.get(AuditRun, item.last_audit_id) if item.last_audit_id else None
        for entry in audit.evidence if audit else []:
            if not entry["passed"] and entry["severity"] in {"REVIEW", "BLOCK"}:
                risks[entry["title"]] = risks.get(entry["title"], 0) + 1
    failed_jobs = db.scalar(select(func.count()).select_from(Job).where(Job.status == "FAILED"))
    queue = db.scalar(select(func.count()).select_from(Job).where(Job.status.in_(["PENDING", "RUNNING"])))
    return {"approval_amount": str(sum((a.amount for a in approvals), Decimal("0"))), "approval_count": len(approvals),
        "pending_count": len(pending), "review_count": sum(a.decision in {"REVIEW", "BLOCK"} for a in pending),
        "pass_count": sum(a.decision == "PASS" for a in pending), "block_count": sum(a.decision == "BLOCK" for a in pending),
        "invoice_count": len(invoices), "archived_count": sum(r.status == "ARCHIVED" for r in records),
        "quarantined_count": sum(r.status == "QUARANTINED" for r in records), "failed_jobs": failed_jobs, "queue_count": queue,
        "trend": trend, "risks": [{"name": key, "count": value} for key, value in sorted(risks.items(), key=lambda x: -x[1])],
        "recent_approvals": approval_rows_dump(db, pending[:6]),
        "activity": [{"id": x.id, "actor": x.actor, "action": x.action, "target": x.target, "created_at": x.created_at.isoformat()+"Z"}
                     for x in db.scalars(select(ActivityLog).order_by(ActivityLog.created_at.desc()).limit(8))]}


def list_approvals(db, q="", decision="", status="", company_id="", page=1, page_size=20, business_type=""):
    statement = select(Approval).order_by(Approval.created_at.desc())
    if q:
        statement = statement.where(or_(
            Approval.title.contains(q),
            Approval.serial_number.contains(q),
            Approval.applicant.contains(q),
            Approval.applicant_id.contains(q, autoescape=True),
            _local_applicant_name().contains(q),
        ))
    if decision:
        statement = statement.where(Approval.decision == decision)
    if status:
        statement = statement.where(Approval.status == status)
    if business_type:
        statement = statement.where(Approval.business_type == business_type)
    if company_id:
        statement = statement.where(cast(Approval.company_ids, String).contains(company_id))
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = db.scalars(statement.offset((page-1)*page_size).limit(page_size)).all()
    return {"items": approval_rows_dump(db, rows), "total": total, "page": page, "page_size": page_size}


def _invoice_out(invoice: Invoice, attachments: list[Attachment], expense_type: str | None) -> s.InvoiceOut:
    return s.InvoiceOut.model_validate(invoice).model_copy(
        update={"display_details": invoice_display(invoice, attachments, expense_type)})


def _sum_invoice_amounts(values: list[Decimal | None]) -> Decimal | None:
    known = [value for value in values if value is not None]
    return sum(known, Decimal(0)) if len(known) == len(values) else None


def approval_detail(db, approval_id: str, *, include_feishu_form: bool = False):
    """共用审批详情；原表单仅由工作台的详情和复核接口显式读取。"""
    approval = db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404, "审批单不存在")
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id)
                                 .order_by(Attachment.created_at, Attachment.id)))
    invoice_attachments = defaultdict(list)
    for attachment in attachments:
        if attachment.active and attachment.invoice_id:
            invoice_attachments[attachment.invoice_id].append(attachment)
    invoice_rows = {invoice.id: invoice for invoice in db.scalars(select(Invoice).where(
        Invoice.id.in_(list(invoice_attachments))))} if invoice_attachments else {}
    reimbursement = approval.business_type == "REIMBURSEMENT"
    expense_type = approval.expense_type if reimbursement else None
    invoices = [_invoice_out(invoice_rows[key], files, expense_type)
                for key, files in invoice_attachments.items() if key in invoice_rows]
    invoice_totals = s.InvoiceTotalsOut(
        amount=sum((invoice.amount for invoice in invoices), Decimal(0)),
        tax_amount=_sum_invoice_amounts([invoice.display_details.tax_amount for invoice in invoices]),
        untaxed_amount=_sum_invoice_amounts([invoice.display_details.untaxed_amount for invoice in invoices]),
    ) if reimbursement and invoices else None
    audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
    unresolved = [item for item in audit.evidence if not item["passed"] and item["severity"] in {"REVIEW", "BLOCK"}] if audit else []
    summary = approval_rows_dump(db, [approval], attachments=attachments)[0]
    result = {**summary, "attachments": attachments,
        "invoices": invoices, "invoice_totals": invoice_totals,
        "reference_pdf_summary": reference_pdf_summary(reference_pdfs(attachments)) if reference_approval(approval) else None,
        "claims": list(db.scalars(select(InvoiceClaim).where(InvoiceClaim.approval_id == approval.id))),
        "audit": audit, "manual_review_valid": bool(full_review(db, approval, audit=audit, attachments=attachments)),
        "suggested_reject_comment": rejection_comment(unresolved, approval.business_type) if summary["ai_approval_enabled"] else "",
        "actions": list(db.scalars(select(ApprovalAction).where(ApprovalAction.approval_id == approval.id).order_by(ApprovalAction.created_at.desc()))),
        "reviews": [row.model_analysis["manual_review"] for row in db.scalars(select(AuditRun).where(
            AuditRun.approval_id == approval.id,
            AuditRun.model_analysis["manual_review"]["audit_id"].as_string() == AuditRun.id).order_by(AuditRun.created_at.desc()))],
        "feishu_url": feishu_link(approval.id)}
    if include_feishu_form and reimbursement and approval.raw_form:
        template = db.get(ApprovalTemplate, approval.template_code)
        result["feishu_form"] = reimbursement_form(approval.raw_form, (template.field_mapping or {}) if template else {})
    return result


def audit_approval(db, approval_id: str, user, *, commit=True):
    begin_write(db)
    approval = db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404, "审批单不存在")
    if reference_approval(approval):
        attachments = list(db.scalars(select(Attachment).where(
            Attachment.approval_id == approval.id, Attachment.active.is_(True))))
        if job := enqueue_reference_pdfs(db, approval.id, reference_pdfs(attachments)):
            log(db, user.name, "发起包内 PDF 补充识别", approval_id)
            if commit:
                db.commit()
            return job
    if not can_run_ai_approval(db.get(ApprovalTemplate, approval.template_code)):
        raise DomainError("模板 AI 审批未开启，请由财务整单人工复核")
    job = enqueue(db, "AUDIT", {"instance_code": approval_id})
    log(db, user.name, "发起重新审核", approval_id)
    if commit:
        db.commit()
    return job


def approval_action(db, approval_id: str, data: s.ActionInput, user, *, commit=True):
    begin_write(db)
    approval = db.scalar(select(Approval).where(Approval.id == approval_id))
    if not approval:
        raise HTTPException(404, "审批单不存在")
    record = request_action(db, approval, data.action, data.comment, data.expected_version, data.idempotency_key, user.id, user.name)
    job = enqueue(db, "ACTION", {"action_id": record.id}, f"action:{record.id}")
    if commit:
        db.commit()
    return job


def review_approval(db, approval_id: str, data: s.ManualReviewInput, user):
    """整单复核优先于全部审核规则；旧客户端的逐项确认继续维持原有行为。"""
    begin_write(db)
    approval = db.get(Approval, approval_id)
    if not approval:
        raise HTTPException(404, "审批单不存在")
    if approval.version != data.expected_version or (data.audit_id is not None and approval.last_audit_id != data.audit_id):
        raise DomainError("审批或审核结论已更新，请刷新后重新复核")
    template = db.get(ApprovalTemplate, approval.template_code)
    if (not template or not template.active or approval.status != "PENDING" or approval.task_status != "PENDING"
            or not approval.task_id or approval.assignee != template.reviewer_open_id):
        raise DomainError("当前不是可复核的专用预审待办")
    if db.scalar(select(ApprovalAction.id).where(ApprovalAction.approval_id == approval.id,
            ApprovalAction.status.in_(["REQUESTED", "EXECUTING", "UNKNOWN"]))):
        raise DomainError("审批正在回写或核对结果，请等待完成后再复核")
    if data.scope == "ALL":
        save_full_review(db, approval, user.id, user.name, data.comment)
        db.commit()
        return approval_detail(db, approval.id, include_feishu_form=True)
    if not template.ai_approval_enabled:
        raise DomainError("模板 AI 审批已关闭，请选择整单人工复核")
    audit = db.get(AuditRun, data.audit_id) if data.audit_id else None
    if (not audit or approval.audit_requested or audit.attachment_version != approval.attachment_version
            or approval.audit_attachment_version != approval.attachment_version or not audit_config_matches(db, approval, template)):
        raise DomainError("附件、提示词或审核规则已变化，请先重新审核")
    if not audit.model_analysis.get("review_fingerprint"):
        raise DomainError("此审核生成于旧版本，请先重新审核后再复核")
    allowed = {item["code"] for item in audit.evidence if reviewable(item)}
    selected = list(dict.fromkeys(data.reviewed_codes if data.reviewed_codes is not None else ["SEMANTIC"]))
    if not selected or not set(selected).issubset(allowed):
        raise DomainError("请选择可人工确认的文字标准或费用语义项；明确拒绝及系统阻断不能覆盖")
    evidence = copy.deepcopy(audit.evidence)
    apply_evidence_review(evidence, selected)
    reviewed = AuditRun(approval_id=approval.id, rule_version=audit.rule_version,
        attachment_version=audit.attachment_version, conclusion=audit_conclusion(evidence),
        evidence=evidence, model_analysis=copy.deepcopy(audit.model_analysis))
    db.add(reviewed)
    db.flush()
    previous_review = audit.model_analysis.get("manual_review") or {}
    confirmed = previous_review.get("confirmed_codes") or previous_review.get("reviewed_codes", [])
    review = {"audit_id": reviewed.id, "source_audit_id": audit.id, "actor_id": user.id, "actor_name": user.name,
              "comment": data.comment, "reviewed_codes": selected,
              "confirmed_codes": sorted(set(confirmed) | set(selected)), "created_at": now().isoformat()+"Z",
              "review_fingerprint": audit.model_analysis["review_fingerprint"]}
    reviewed.model_analysis = {**reviewed.model_analysis, "manual_review": review}
    approval.decision, approval.last_audit_id = reviewed.conclusion, reviewed.id
    approval.version += 1
    log(db, user.name, "人工复核审核标准", approval.serial_number, review)
    # 复核只保存本次处理依据；财务随后点击同意，继续沿用版本、配置和原子占用校验。
    db.commit()
    return approval_detail(db, approval.id, include_feishu_form=True)


def correct_attachment(db, approval_id: str, attachment_id: str, data: s.CorrectionInput, user, *, commit=True):
    """保存当前原件的人工核对记录；非法字段返回业务提示，不保存或调度重审。"""
    begin_write(db)
    approval = db.scalar(select(Approval).where(Approval.id == approval_id))
    attachment = db.get(Attachment, attachment_id)
    if not approval or not attachment or attachment.approval_id != approval_id or not attachment.active:
        raise HTTPException(404, "当前附件不存在")
    from .attachment_archives import archive_readonly
    if archive_readonly(db, attachment):
        raise DomainError("请在正常解包的成员原件上核对票面")
    if approval.status != "PENDING" or approval.version != data.expected_version:
        raise DomainError("审批已更新或结束，请刷新后核对")
    if db.scalar(select(ApprovalAction).where(ApprovalAction.approval_id == approval_id, ApprovalAction.status.in_(["REQUESTED", "EXECUTING", "UNKNOWN"]))):
        raise DomainError("审批正在回写中，暂时不能修改票面字段")
    # 人工输入可能通过基础长度校验但不满足票种规则，须转换成可展示的业务错误。
    try:
        fields = normalize(data.model_dump(mode="json", exclude={"comment", "expected_version"}))
    except ValueError as exc:
        raise DomainError(str(exc)) from exc
    attachment.parsed_data = {**attachment.parsed_data, "fields": fields, "error": "", "confirmed": True,
        "confirmed_by": user.name, "confirmed_at": now().isoformat()+"Z", "confirmation_comment": data.comment}
    template = db.get(ApprovalTemplate, approval.template_code)
    if not template.ai_approval_enabled and attachment.invoice_id:
        invoice = db.get(Invoice, attachment.invoice_id)
        if invoice.invoice_key != fields["invoice_key"]:
            # 人工更正票号后不能让原件继续指向旧票据；同步按剩余原件核对旧占用。
            attachment.invoice_id = None
    approval.decision, approval.audit_attachment_version = "PENDING", ""
    approval.version += 1
    log(db, user.name, "确认票面字段", approval.serial_number, {"attachment_id": attachment_id, "comment": data.comment, "fields": fields})
    if template.ai_approval_enabled:
        job = enqueue(db, "AUDIT", {"instance_code": approval_id, "only_if_needed": True})
    else:
        job = enqueue(db, "REFRESH", {"instance_code": approval_id})
    if commit:
        db.commit()
    return job


def archive(db, approval_id: str, user, *, commit=True):
    begin_write(db)
    approval = db.get(Approval, approval_id)
    if not approval or approval.status != "APPROVED":
        raise DomainError("只有最终审批通过的单据可以归档")
    job = enqueue(db, "ARCHIVE", {"instance_code": approval_id})
    if commit:
        db.commit()
    return job


def invoices(db, q="", company_id="", page=1, page_size=20):
    statement = select(Invoice).order_by(Invoice.created_at.desc())
    if q:
        statement = statement.where(or_(Invoice.number.contains(q), Invoice.buyer_name.contains(q), Invoice.seller_name.contains(q)))
    if company_id:
        statement = statement.where(Invoice.company_id == company_id)
    return page_result(db, statement, lambda row: invoice_entry(db, row), page, page_size)


def invoice_entry(db, row):
    """台账与选中发票查询共用占用及原件投影，不能只返回票面而漏掉重复来源。"""
    claim, expense_type = db.execute(select(InvoiceClaim, Approval.expense_type).select_from(InvoiceClaim).outerjoin(Approval, and_(
        Approval.id == InvoiceClaim.approval_id, Approval.business_type == "REIMBURSEMENT"))
        .where(InvoiceClaim.active_key == row.id)).one_or_none() or (None, None)
    attachments = list(db.scalars(select(Attachment).where(Attachment.invoice_id == row.id, Attachment.active.is_(True))
                                 .order_by(Attachment.created_at, Attachment.id)))
    return {**_invoice_out(row, attachments, expense_type).model_dump(mode="json"),
            "claim": s.ClaimOut.model_validate(claim).model_dump(mode="json") if claim else None,
            "attachment_id": attachments[0].id if attachments else None}


def archives(db, company_id="", month="", status="", q="", page=1, page_size=20, *, approval_month: str = ""):
    """检索归档；month 沿用开票月份，approval_month 按北京时间的申请提交月份筛选。"""
    statement = select(ArchiveRecord).order_by(ArchiveRecord.created_at.desc())
    if company_id:
        statement = statement.where(ArchiveRecord.company_id == company_id)
    if month:
        try:
            date.fromisoformat(month+"-01")
        except ValueError:
            raise DomainError("月份格式应为 YYYY-MM")
        statement = statement.where(ArchiveRecord.archive_path.contains(month.replace("-", "/")))
    if approval_month:
        try:
            if len(approval_month) != 7:
                raise ValueError
            local_start = datetime.fromisoformat(approval_month + "-01")
            local_end = (local_start.replace(year=local_start.year + 1, month=1) if local_start.month == 12
                         else local_start.replace(month=local_start.month + 1))
            # 数据库存 UTC 无时区时间；左闭右开的范围精确覆盖北京时间自然月，包含跨年月份。
            start, end = local_start - timedelta(hours=8), local_end - timedelta(hours=8)
        except (ValueError, OverflowError):
            raise DomainError("审批月份应为有效的 YYYY-MM")
        # 使用审批 ID 子查询，在分页和计数之前筛选；提交时间缺失的记录不会命中，不回退到采集时间。
        approval_ids = select(Approval.id).where(Approval.submitted_at >= start, Approval.submitted_at < end)
        statement = statement.where(ArchiveRecord.approval_id.in_(approval_ids))
    if status:
        statement = statement.where(ArchiveRecord.status == status)
    if q:
        statement = statement.where(ArchiveRecord.archive_path.contains(q))
    return page_result(db, statement, lambda row: s.ArchiveOut.model_validate(row).model_dump(mode="json"), page, page_size)


def retry_job(db, job_id: str, user, *, commit=True):
    begin_write(db)
    job = db.scalar(select(Job).where(Job.id == job_id))
    if not job or job.status != "FAILED":
        raise DomainError("只有失败任务可以重试")
    if job.kind == "AUDIT":
        approval = db.get(Approval, job.payload.get("instance_code"))
        if approval and not can_run_ai_approval(db.get(ApprovalTemplate, approval.template_code)):
            raise DomainError("模板 AI 审批未开启，请由财务整单人工复核")
    if job.kind == "ACTION":
        action = db.get(ApprovalAction, job.payload["action_id"])
        if action.status == "FAILED":
            raise DomainError("该操作因业务条件变化已停止，请刷新审批后重新提交")
    job.status, job.attempts, job.error, job.available_at = "PENDING", 0, "", now()
    queue_ai_audit(db, job)
    log(db, user.name, "重试后台任务", job.id)
    if commit:
        db.commit()
    return job


def manual_sync(db, user, *, commit=True):
    begin_write(db)
    if settings().app_mode == "demo":
        raise DomainError("演示模式使用本地模拟审批，无需访问飞书进行补偿同步")
    job = enqueue(db, "SYNC", {})
    if commit:
        db.commit()
    return job
