"""只读业务检索与待确认方案。此模块没有对模型开放的业务执行函数。"""
import json
from datetime import timedelta
from urllib.parse import urlencode

from fastapi import HTTPException
from sqlalchemy import select

from . import agent_schemas as a, business, schemas as s
from .agent_store import clean, emit, owned, proposal_dump
from .config import settings
from .db import begin_write
from .integrations import DomainError
from .model_config import current_audit_fingerprint
from .models import (AgentProposal, Approval, ApprovalAction, ApprovalTemplate, ArchiveRecord,
                     AgentRun, Attachment, Company, Invoice, Job, User, now, uid)
from .services import log

ACTION_NAMES = {"AUDIT": "重新预审", "ARCHIVE": "归档原件", "APPROVE": "同意预审", "REJECT": "拒绝预审",
                "CORRECT": "纠正票面并重审", "SYNC": "补偿同步", "RETRY": "重试失败任务"}
PRE_REVIEW_PROPOSALS = {"AUDIT", "APPROVE", "REJECT", "CORRECT"}


def actor(db, owner_id, *, write=False, admin=False):
    """身份始终从运行绑定的用户读取；每次调用复核实时角色，不相信模型参数。"""
    user = db.get(User, owner_id, populate_existing=True)
    if not user or not user.enabled:
        raise HTTPException(401, "账号已停用或不存在")
    if admin and user.role != "admin" or write and user.role not in {"admin", "finance"}:
        raise HTTPException(403, "当前账号没有执行此操作的权限")
    return user


def company_scope(db, requested, context):
    company_id = context.get("company_id", "") if requested is None else requested
    if company_id and not db.get(Company, company_id):
        raise DomainError("公司主体不存在，请重新选择")
    return company_id


def read_business(db, data: a.QueryInput, context: dict):
    route = context.get("route", "").split("/")[0]
    approval_archives = data.entity == "archived_approvals" and route == "archives"
    requested_ids = "ids" in data.model_fields_set
    if route == data.entity or approval_archives:
        inherited = {key: value for key, value in context.get("filters", {}).items()
                     if key not in data.model_fields_set}
        if route == "archives" and data.entity == "archives" and context.get("selection_type") == "archived_approvals":
            # 旧原件查询的月份口径独立保留，整单归档走同一个新分页入口。
            inherited.pop("approval_month", None)
        data = a.QueryInput.model_validate({**data.model_dump(), **inherited})
    company_id = company_scope(db, data.company_id, context)
    if data.entity == "overview":
        result = business.overview(db, company_id)
        # 助手不需要读取全局操作者活动；金额及结论来自同一汇总入口。
        result.pop("activity", None)
        return {"entity": data.entity, "scope": company_id, **result}
    if data.entity == "companies":
        return {"entity": "companies", "items": [s.CompanyOut.model_validate(row).model_dump(mode="json")
            for row in db.scalars(select(Company).order_by(Company.created_at).limit(20))]}
    if data.entity == "approval":
        target = data.target_id or context.get("approval_id", "")
        if not target:
            raise DomainError("请先指定审批编号，或打开一张审批单")
        result = s.ApprovalDetail.model_validate(business.approval_detail(db, target)).model_dump(
            mode="json", exclude={"feishu_form"})
        result["url"] = "#/approvals/" + target
        return {"entity": "approval", **clean(result)}
    if data.entity == "archived_approvals":
        from .archive_center import list_archive_approvals
        from .archive_schemas import ArchivedApproval

        ids = data.ids or (context.get("selected_ids") if approval_archives and
                           context.get("selection_type") == data.entity and not requested_ids else [])
        result = list_archive_approvals(db, data.page, data.page_size, company_id=company_id, month=data.approval_month,
            month_basis=data.month_basis, invoice_month=data.invoice_month, scope=data.scope, archive_status=data.archive_status,
            q=data.q, business_type=data.business_type, ids=ids or None)
        result["items"] = [ArchivedApproval.model_validate(item).model_dump(mode="json") for item in result["items"]]
        return {"entity": data.entity, "scope": company_id, **result}
    if data.entity in {"approvals", "invoices", "archives"}:
        if data.ids:
            model, schema = {"approvals": (Approval, s.ApprovalOut), "invoices": (Invoice, s.InvoiceOut),
                             "archives": (ArchiveRecord, s.ArchiveOut)}[data.entity]
            rows = list(db.scalars(select(model).where(model.id.in_(data.ids)).limit(20)))
            if company_id:
                rows = [row for row in rows if company_id in row.company_ids] if model is Approval else [row for row in rows if row.company_id == company_id]
            if model is Approval and data.business_type:
                rows = [row for row in rows if row.business_type == data.business_type]
            result = {"items": [business.invoice_entry(db, row) if model is Invoice else schema.model_validate(row).model_dump(mode="json") for row in rows], "total": len(rows)}
        elif data.entity == "approvals":
            result = business.list_approvals(db, data.q, data.decision, data.status, company_id, data.page, data.page_size, data.business_type)
        elif data.entity == "invoices":
            result = business.invoices(db, data.q, company_id, data.page, data.page_size)
        else:
            result = business.archives(db, company_id, data.month, data.status, data.q, data.page, data.page_size,
                                       approval_month=data.approval_month)
        return {"entity": data.entity, "scope": company_id, **result}
    if data.entity == "job":
        job = db.get(Job, data.target_id)
        if not job:
            raise DomainError("后台任务不存在")
        result = s.JobOut.model_validate(job).model_dump(mode="json")
        if job.kind == "ACTION":
            operation = db.get(ApprovalAction, job.payload.get("action_id"))
            result["operation_status"] = operation.status if operation else ""
        return {"entity": "job", **result}
    statement = select(Job).order_by(Job.created_at.desc())
    if data.status:
        statement = statement.where(Job.status == data.status)
    return {"entity": "jobs", **business.page_result(db, statement,
        lambda row: s.JobOut.model_validate(row).model_dump(mode="json"), data.page, data.page_size)}


def check_retry(db, job):
    if not job or job.status != "FAILED":
        raise DomainError("只有失败任务可以重试")
    if job.kind == "AUDIT":
        approval = db.get(Approval, job.payload.get("instance_code"))
        if approval and not db.get(ApprovalTemplate, approval.template_code).ai_approval_enabled:
            raise DomainError("此模板由财务人工预审，请打开审批详情处理")
    if job.kind == "ACTION":
        action = db.get(ApprovalAction, job.payload.get("action_id"))
        if not action or action.status == "FAILED":
            raise DomainError("该审批操作已因业务条件变化停止，请重新审核后提出新方案")


def create_proposal(db, data: a.ProposalInput, handle):
    begin_write(db)
    if handle.stopped.is_set() or db.get(AgentRun, handle.run_id).status != "RUNNING":
        raise DomainError("产生方案的运行已经停止")
    user = actor(db, handle.owner_id, write=True, admin=data.kind == "SYNC")
    if db.scalar(select(AgentProposal.id).where(AgentProposal.run_id == handle.run_id).offset(19).limit(1)):
        raise DomainError("单轮最多提出二十项方案，请分批处理")
    target = data.target_id or (handle.context.get("approval_id", "") if data.kind not in {"SYNC", "RETRY"} else "")
    checks, parameters = {}, {"comment": data.comment, "attachment_id": data.attachment_id}
    display = {"action": ACTION_NAMES[data.kind], "title": "", "description": "", "comment": data.comment}
    if data.kind == "SYNC":
        if settings().app_mode != "live":
            raise DomainError("演示模式使用本地模拟审批，无需访问飞书进行补偿同步")
        display.update(title="飞书审批增量补偿同步", description="同步可能产生新的预审任务，并按现有模板自动化策略继续处理。")
    elif data.kind == "RETRY":
        job = db.get(Job, target)
        check_retry(db, job)
        checks = {"attempts": job.attempts, "updated_at": job.updated_at.isoformat()}
        if job.kind == "SYNC" and user.role != "admin":
            raise HTTPException(403, "补偿同步任务仅管理员可重试")
        display.update(title=f"{job.kind} · {job.id}", description="复用原任务；审批回写结果未知时先对账，重审可能触发现有自动策略。")
    else:
        approval = db.get(Approval, target)
        if not approval:
            raise DomainError("审批单不存在，请先查询真实记录")
        template = db.get(ApprovalTemplate, approval.template_code)
        if data.kind in PRE_REVIEW_PROPOSALS and not template.ai_approval_enabled:
            raise DomainError("此模板由财务人工预审，请打开审批详情处理")
        checks = {"version": approval.version, "attachment_version": approval.attachment_version,
            "config_fingerprint": current_audit_fingerprint(db, template)}
        display.update(title=approval.title, serial_number=approval.serial_number, amount=str(approval.amount),
            approval_id=approval.id, expected_version=approval.version, url="#/approvals/"+approval.id,
            description="操作沿用当前审批规则，不改变后续审批、付款或入账状态。")
        if data.kind == "ARCHIVE" and approval.status != "APPROVED":
            raise DomainError("只有最终审批通过的单据可以归档")
        if data.kind in {"APPROVE", "REJECT"}:
            if len(data.comment.strip()) < 2:
                raise DomainError("请补充至少二字的处理意见")
            if not template.active or approval.status != "PENDING" or approval.task_status != "PENDING" or not approval.task_id:
                raise DomainError("当前不是可处理的预审待办")
            if data.kind == "APPROVE" and approval.decision != "PASS":
                raise DomainError("存在未解决的审核项，不能提出同意预审方案；请先核对并重新审核")
        if data.kind == "CORRECT":
            attachment = db.get(Attachment, data.attachment_id)
            if not attachment or attachment.approval_id != target or not attachment.active:
                raise DomainError("请指定该审批的当前附件")
            if not data.correction or len(data.comment.strip()) < 2:
                raise DomainError("请补齐完整票面字段和人工核对依据")
            if approval.status != "PENDING":
                raise DomainError("审批已经结束，不能更改票面")
            correction = s.CorrectionInput(**data.correction.model_dump(), comment=data.comment, expected_version=approval.version)
            parameters["correction"] = correction.model_dump(mode="json")
            display.update(before=attachment.parsed_data.get("fields", {}),
                after=data.correction.model_dump(mode="json"), attachment_id=attachment.id,
                preview_mime=attachment.mime,
                preview_url=f"/api/v1/attachments/{attachment.id}/download?preview=true",
                description="核对原件后确认全部票面字段，保存人工确认记录并重新预审。")
        if data.kind in {"AUDIT", "CORRECT"} and template.mode == "AUTO":
            display["warning"] = "此模板已开启自动模式，重审后可能按规则自动同意或拒绝专用预审节点。"
    # 同一目标的新方案使旧方案失效，避免调整意见后误点旧卡片。
    for previous in db.scalars(select(AgentProposal).where(AgentProposal.conversation_id == handle.conversation_id,
            AgentProposal.target_id == target, AgentProposal.status == "PENDING")):
        previous.status = "CANCELED"
        previous.error = "已被针对同一对象的新方案替代"
        emit(db, handle.conversation_id, "action.updated", {"proposal": clean(proposal_dump(db, previous))})
    proposal = AgentProposal(id=uid(), conversation_id=handle.conversation_id, run_id=handle.run_id,
        kind=data.kind, target_id=target, parameters=parameters, checks=checks, display=display,
        status="PENDING", expires_at=now()+timedelta(minutes=10), created_at=now())
    db.add(proposal)
    db.flush()
    result = clean(proposal_dump(db, proposal))
    emit(db, handle.conversation_id, "action.updated", {"proposal": result})
    db.commit()
    return {"entity": "proposal", "message": "只生成了方案，尚未执行。请用户点击卡片确认。", "proposal": result}


def confirm_proposal(db, proposal_id, user, original_checked=False):
    """唯一执行业务入口：属主、实时权限、快照和任务入队在同一写事务中复核。"""
    begin_write(db)
    proposal = db.get(AgentProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "操作方案不存在")
    owned(db, proposal.conversation_id, user)
    actor(db, user.id, write=True, admin=proposal.kind == "SYNC")
    if proposal.status == "SUBMITTED":
        return clean(proposal_dump(db, proposal))
    if proposal.status != "PENDING":
        raise DomainError("该方案已取消或失效，请重新生成")
    try:
        if proposal.expires_at <= now():
            raise DomainError("确认卡已过期，请让助手重新查询并生成方案")
        parameters = proposal.parameters
        if proposal.kind in {"AUDIT", "ARCHIVE", "CORRECT", "APPROVE", "REJECT"}:
            approval = db.get(Approval, proposal.target_id)
            if not approval or approval.version != proposal.checks["version"] or approval.attachment_version != proposal.checks["attachment_version"]:
                raise DomainError("审批或附件已经变化，请重新查询并生成方案")
            template = db.get(ApprovalTemplate, approval.template_code)
            if proposal.kind in PRE_REVIEW_PROPOSALS and not template.ai_approval_enabled:
                raise DomainError("此模板由财务人工预审，请打开审批详情处理")
            from .manual_review import full_review
            manual_approve = proposal.kind == "APPROVE" and full_review(db, approval)
            if not manual_approve and current_audit_fingerprint(db, template) != proposal.checks["config_fingerprint"]:
                raise DomainError("审核配置已经变化，请重新查询并生成方案")
        if proposal.kind == "CORRECT" and not original_checked:
            # 未勾选只返回提示，保留方案，允许用户核对后再次确认。
            raise HTTPException(422, "请先预览原件并勾选已核对票面字段")
        if proposal.kind == "AUDIT":
            job = business.audit_approval(db, proposal.target_id, user, commit=False)
        elif proposal.kind == "ARCHIVE":
            job = business.archive(db, proposal.target_id, user, commit=False)
        elif proposal.kind in {"APPROVE", "REJECT"}:
            data = s.ActionInput(action=proposal.kind, comment=parameters["comment"],
                expected_version=proposal.checks["version"], idempotency_key="agent:"+proposal.id)
            job = business.approval_action(db, proposal.target_id, data, user, commit=False)
        elif proposal.kind == "CORRECT":
            job = business.correct_attachment(db, proposal.target_id, parameters["attachment_id"],
                s.CorrectionInput.model_validate(parameters["correction"]), user, commit=False)
        elif proposal.kind == "SYNC":
            job = business.manual_sync(db, user, commit=False)
        elif proposal.kind == "RETRY":
            previous = db.get(Job, proposal.target_id)
            check_retry(db, previous)
            if previous.attempts != proposal.checks["attempts"] or previous.updated_at.isoformat() != proposal.checks["updated_at"]:
                raise DomainError("任务状态已变化，请重新生成方案")
            if previous.kind == "SYNC" and user.role != "admin":
                raise HTTPException(403, "补偿同步任务仅管理员可重试")
            job = business.retry_job(db, proposal.target_id, user, commit=False)
        else:
            raise DomainError("未开放的业务操作")
        db.flush()
        proposal.status, proposal.job_id = "SUBMITTED", job.id
        log(db, user.name, "确认 Agent 业务方案", proposal.display.get("title", proposal.target_id),
            {"conversation_id": proposal.conversation_id, "run_id": proposal.run_id, "proposal_id": proposal.id, "job_id": job.id})
        result = clean(proposal_dump(db, proposal))
        emit(db, proposal.conversation_id, "action.updated", {"proposal": result})
        db.commit()
        return result
    except DomainError as exc:
        # 业务入口可能已写入部分对象；先回滚全部业务变化，再单独标记失效。
        db.rollback()
        begin_write(db)
        proposal = db.get(AgentProposal, proposal_id)
        proposal.status, proposal.error = "INVALID", str(exc)
        emit(db, proposal.conversation_id, "action.updated", {"proposal": clean(proposal_dump(db, proposal))})
        db.commit()
        raise


def download_links(db, data: a.DownloadInput, context):
    if data.kind == "invoice_export":
        company_id = company_scope(db, data.company_id, context)
        company = db.get(Company, company_id) if company_id else None
        label = company.name if company else "全部公司主体"
        return {"entity": "downloads", "links": [{"label": f"导出{label}的完整发票台账（不含搜索筛选）",
            "url": "/api/v1/invoices/export?"+urlencode({"company_id": company_id})}]}
    if data.kind == "approval_archive_zip":
        from .archive_center import ordered_approvals
        from .archive_print import approval_ids

        ids = data.ids or (context.get("selected_ids", []) if context.get("selection_type") == "archived_approvals" else [])
        ids = approval_ids(ids)
        ordered_approvals(db, ids)
        return {"entity": "downloads", "links": [{"label": f"打包下载 {len(ids)} 张审批的原件和清单",
            "url": "/api/v1/archives/approval-download?" + urlencode({"ids": ",".join(ids)})}]}
    if not data.ids:
        raise DomainError("请先指定要下载的真实原件或归档记录")
    if data.kind == "archive_zip":
        rows = list(db.scalars(select(ArchiveRecord).where(ArchiveRecord.id.in_(data.ids))))
        if len(rows) != len(set(data.ids)):
            raise DomainError("部分归档记录不存在")
        return {"entity": "downloads", "links": [{"label": f"下载 {len(rows)} 个归档原件与清单",
            "url": "/api/v1/archives/download?"+urlencode({"ids": ",".join(data.ids)})}]}
    rows = list(db.scalars(select(Attachment).where(Attachment.id.in_(data.ids))))
    if len(rows) != len(set(data.ids)):
        raise DomainError("部分附件不存在")
    return {"entity": "downloads", "links": [{"label": row.filename,
        "url": f"/api/v1/attachments/{row.id}/download", "preview_url": f"/api/v1/attachments/{row.id}/download?preview=true"}
        for row in rows]}
