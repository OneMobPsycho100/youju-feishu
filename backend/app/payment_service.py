"""付款表单规范化与人工材料确认；复用审批版本、权限及任务机制。"""
import copy
import json

from fastapi import HTTPException
from sqlalchemy import select

from .attachment_archives import archive_readonly
from .db import begin_write
from .integrations import DomainError
from .jobs import enqueue
from .models import Approval, ApprovalAction, ApprovalTemplate, Attachment, Company, now


def decoded(value):
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def display(value) -> str:
    """只取控件显示文本，不把复杂对象的 Python repr 作为公司名称。"""
    value = decoded(value)
    if isinstance(value, dict):
        return next((display(value[key]) for key in ("name", "text", "label", "value", "department_name") if value.get(key)), "")
    if isinstance(value, list):
        return "、".join(filter(None, (display(item) for item in value)))
    return str(value or "").strip()


def payment_fields(db, form: list, mapping: dict, issues: list) -> tuple[dict, str]:
    from .services import field_values

    def value(key, default=""):
        values = field_values(form, mapping.get(key, ""))
        return decoded(values[0]) if values else default

    name = display(value("company"))
    matches = [company for company in db.scalars(select(Company).where(Company.active.is_(True)))
               if name and name in {company.name, *company.aliases}]
    return {"company_name": name, "company_id": matches[0].id if len(matches) == 1 else None,
            "project": display(value("project")), "payment_method": display(value("payment_method")),
            "expected_payment_date": display(value("expected_payment_date")),
            "payee_account": value("payee_account", {}), "related_approvals": value("related_approvals", []),
            "remarks": display(value("remarks")), "material_issues": issues}, display(value("department"))


def confirm_material(db, approval_id, attachment_id, data, user):
    """保存人工分类，按模板开关选择重审或仅同步原件；已关联的发票保留占用。"""
    from .services import log

    begin_write(db)
    approval, attachment = db.get(Approval, approval_id), db.get(Attachment, attachment_id)
    if not approval or not attachment or attachment.approval_id != approval_id or not attachment.active:
        raise HTTPException(404, "付款申请或当前附件不存在")
    if archive_readonly(db, attachment):
        raise DomainError("请在正常解包的成员原件上确认材料类别")
    template = db.get(ApprovalTemplate, approval.template_code)
    if approval.business_type != "PAYMENT" or approval.version != data.expected_version:
        raise DomainError("审批内容已变化或不是付款申请，请刷新后重试")
    if (not template.active or approval.status != "PENDING" or approval.task_status != "PENDING"
            or approval.assignee != template.reviewer_open_id):
        raise DomainError("当前不是可处理的专用预审待办")
    if db.scalar(select(ApprovalAction.id).where(ApprovalAction.approval_id == approval_id,
            ApprovalAction.status.in_(["REQUESTED", "EXECUTING", "UNKNOWN"]))):
        raise DomainError("审批正在回写或对账，请等待处理完成")
    if attachment.invoice_id and data.material_type != "INVOICE":
        raise DomainError("此原件已关联发票台账，不能通过修改材料类别解除占用")
    parsed = copy.deepcopy(attachment.parsed_data or {})
    material = parsed.setdefault("material", {})
    confirmation = {"material_type": data.material_type, "actor_id": user.id, "actor_name": user.name,
                    "comment": data.comment.strip(), "created_at": now().isoformat() + "Z", "sha256": attachment.sha256}
    if material.pop("page_scope", None):
        material.update(segments=[], summary="", error="")
    material.update(confirmation=confirmation, complete=False)
    parsed["material_type"] = data.material_type
    attachment.parsed_data = parsed
    approval.version += 1
    approval.decision, approval.audit_attachment_version = "PENDING", ""
    approval.audit_requested = template.ai_approval_enabled
    log(db, user.name, "确认付款材料类别", approval.serial_number, {"attachment_id": attachment_id, **confirmation})
    if template.ai_approval_enabled:
        job = enqueue(db, "AUDIT", {"instance_code": approval_id, "only_if_needed": True})
    else:
        job = enqueue(db, "REFRESH", {"instance_code": approval_id})
    db.commit()
    return job
