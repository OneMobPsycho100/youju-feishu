"""首次接入自动识别：按业务含义与控件类型填入唯一结果，不把明细金额当作申请总额。"""
import re
import unicodedata

from . import schemas as s
from . import template_schemas as t
from .config import settings
from .integrations import FeishuGateway
from .models import ApprovalTemplate, User
from .company_subjects import COMPANY_FIELD_NAMES, COMPANY_FIELD_TYPES
from .pre_review import matches_pre_review_node, task_open_id
from .template_transfer import FIELD_LABELS, read_definition, recommendations


# 同义词只用于首次接入；跨公司迁移仍使用源描述的自定义 ID、类型、名称及明细路径。
FIELD_NAMES = {
    "amount": ("申请总额", "申请金额", "报销总额", "报销总金额", "报销金额", "费用总额", "费用汇总", "费用合计", "付款金额", "付款总额", "总金额", "总额", "合计金额", "金额"),
    "company_tax_id": ("主体税号", "公司税号", "购买方税号", "纳税人识别号", "统一社会信用代码", "税号"),
    "expense_type": ("费用类型", "费用类别", "报销类型", "报销类别", "支出类别", "费用分类"),
    "description": ("申请事由", "报销事由", "付款事由", "事由", "报销说明", "申请原因", "付款说明", "付款用途", "报销原因"),
    "company": COMPANY_FIELD_NAMES,
    "department": ("申请部门", "所属部门", "部门"),
    "project": ("所属项目", "项目名称", "项目"),
    "payment_method": ("付款方式", "支付方式", "结算方式"),
    "expected_payment_date": ("期望付款日期", "预计付款日期", "计划付款日期", "付款日期", "支付日期"),
    "payee_account": ("收款账户", "收款账号", "收款信息", "银行账户"),
    "related_approvals": ("关联审批", "关联申请", "相关审批"),
    "remarks": ("备注", "补充说明", "其他说明"),
}
FIELD_TYPES = {
    "amount": {"amount", "number", "formula"},
    "company_tax_id": {"input", "textarea"},
    "expense_type": {"radiov2", "radio", "checkboxv2", "input", "textarea"},
    "description": {"input", "textarea"},
    "company": COMPANY_FIELD_TYPES,
    "department": {"department", "input", "radiov2"},
    "project": {"input", "textarea", "radiov2"},
    "payment_method": {"radiov2", "radio", "input"},
    "expected_payment_date": {"date", "input"},
    "payee_account": {"account", "input", "textarea", "radiov2"},
    "related_approvals": {"connect"},
    "remarks": {"input", "textarea"},
}
CUSTOM_NAMES = {
    "amount": ("amount", "total", "total_amount", "grand_total", "reimbursement_amount", "payment_amount"),
    "company_tax_id": ("company_tax_id", "buyer_tax_id", "tax_id"),
    "expense_type": ("expense_type", "expense_category"),
    "description": ("description", "reason", "purpose"),
    "company": ("company", "payment_company", "reimbursement_company", "claim_company"),
    "department": ("department",), "project": ("project",),
    "payment_method": ("payment_method",), "expected_payment_date": ("expected_payment_date",),
    "payee_account": ("payee_account",), "related_approvals": ("related_approvals",), "remarks": ("remarks",),
}


def normalized_name(value: str) -> str:
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).lower())


def infer_business_type(name: str) -> str:
    return "PAYMENT" if any(word in normalized_name(name) for word in ("付款", "支付申请", "payment")) else "REIMBURSEMENT"


def infer_fields(definition: t.ApprovalDefinition, business_type: str) -> tuple[s.FieldMapping, list[str]]:
    """只推荐最高分的唯一结果；同分歧义留空，申请金额只从表单顶层识别。"""
    mapping = s.FieldMapping()
    notices = []
    for key, names in FIELD_NAMES.items():
        if business_type != "PAYMENT" and key not in {"amount", "company_tax_id", "company", "expense_type", "description"}:
            continue
        candidates = []
        aliases = {normalized_name(name) for name in names}
        custom_names = {normalized_name(name) for name in CUSTOM_NAMES[key]}
        for field in definition.fields:
            if field.type.lower() not in FIELD_TYPES[key]:
                continue
            if key == "amount" and field.path:
                # 一个明细可有多行，运行时读取单个明细值不能代表申请总额。
                continue
            name, custom = normalized_name(field.name), normalized_name(field.custom_id)
            score = 0
            if custom and custom in custom_names:
                score = 140
            elif name in aliases:
                score = 100 if key != "amount" or name != "金额" else 70
            elif any(alias in name for alias in aliases if len(alias) >= 3):
                score = 80
            if score:
                score += 20 if not field.path else 0
                candidates.append((score, field))
        if candidates:
            top = max(score for score, _ in candidates)
            winners = [field for score, field in candidates if score == top]
            if len(winners) == 1:
                setattr(mapping, key, winners[0].id)
            else:
                notices.append(FIELD_LABELS[key] + "存在多个同样合适的控件，请按名称选择")
    # 初次接入默认读取全部附件和补充附件，不要求逐个选择，也不静默缩小未来附件范围。
    mapping.attachment_ids = []
    return mapping, notices


def infer_node(definition: t.ApprovalDefinition, business_type: str) -> str:
    candidates = []
    for node in definition.nodes:
        name, custom = normalized_name(node.name), normalized_name(node.custom_id)
        if "人工" in name or name in {"发起", "开始", "结束", "start", "end"}:
            continue
        score = 0
        if custom in {"aireview", "aipreaudit", "invoicereview", "paymentreview", "aiaudit"}:
            score = 140
        elif ("ai" in name or "智能" in name or "机器人" in name) and ("预审" in name or "审核" in name):
            score = 120
        elif "预审" in name:
            score = 100
        if score:
            if (business_type == "PAYMENT" and "付款" in name) or (business_type == "REIMBURSEMENT" and ("发票" in name or "报销" in name)):
                score += 10
            candidates.append((score, node.id))
    if not candidates:
        return ""
    top = max(score for score, _ in candidates)
    winners = [node for score, node in candidates if score == top]
    return winners[0] if len(winners) == 1 else ""


def pending_reviewer(definition: t.ApprovalDefinition, node_ids: list[str], sample_instance: str) -> tuple[str, str]:
    """仅从此模板已选节点的真实 PENDING 待办识别唯一共用审批人。"""
    if not node_ids or settings().app_mode != "live":
        return "", ""
    selected = set(node_ids)
    aliases = selected.copy()
    for node in definition.nodes:
        if node.id in selected or node.custom_id in selected:
            aliases.update((node.id, node.custom_id))
    aliases.discard("")
    remote = FeishuGateway()
    try:
        instances = [sample_instance.strip()] if sample_instance.strip() else remote.setup_instance_candidates(definition.code)
        matches = set()
        for instance_code in instances:
            snapshot = remote.get_instance(instance_code)
            if snapshot.get("approval_code") != definition.code or snapshot.get("status") != "PENDING":
                continue
            for task in snapshot.get("task_list") or []:
                if task.get("status") != "PENDING" or not matches_pre_review_node(task, aliases):
                    continue
                member = task_open_id(task)
                if re.fullmatch(r"ou_[A-Za-z0-9]+", member):
                    matches.add(member)
        if len(matches) == 1:
            return next(iter(matches)), ""
        if len(matches) > 1:
            return "", "预审节点有多名实际审批人，请按姓名选择专用成员"
    except Exception:
        # 实例查询可能尚未授权；定义读取仍然可用，不把 SDK 原文带入界面。
        return "", "暂未取得预审待办的审批人，可按姓名选择，或提供一张停在预审节点的审批单后重新识别"
    return "", ""


def inspect_template(db, data: t.TemplateInspectInput) -> t.TemplateInspectOut:
    definition = read_definition(db, data.reference)
    if data.source is not None:
        mapping, node_ids, unresolved = recommendations(data.source, definition)
        return t.TemplateInspectOut(definition=definition, field_mapping=mapping, node_ids=node_ids,
                                    node_id=node_ids[0] if len(node_ids) == 1 else "",
                                    unresolved_fields=unresolved, business_type=data.source.business_type)
    row = db.get(ApprovalTemplate, definition.code)
    business = row.business_type if row else (data.business_type or infer_business_type(definition.name))
    mapping, notices = infer_fields(definition, business)
    suggested_node = infer_node(definition, business)
    node_ids = [suggested_node] if suggested_node else []
    reviewer, reviewer_source = "", "NONE"
    if row:
        known_fields = {field.id for field in definition.fields}
        for key, value in s.FieldMapping.model_validate(row.field_mapping).model_dump().items():
            if isinstance(value, list):
                # 既有附件选择不可因重读被放宽成全部；失效的旧 ID 留给管理员明确调整。
                mapping.attachment_ids = value
            elif value and value in known_fields:
                setattr(mapping, key, value)
        node_ids = row.node_ids
        reviewer, reviewer_source = row.reviewer_open_id, "SAVED"
    if data.node_ids is not None:
        node_ids = list(dict.fromkeys(data.node_ids))
    known_nodes = {key for node in definition.nodes for key in (node.id, node.custom_id) if key}
    missing_nodes = [node_id for node_id in node_ids if node_id not in known_nodes]
    if missing_nodes:
        notices.append("部分已选节点未在当前定义中找到，请核对 ID：" + "、".join(missing_nodes))
    if not reviewer or data.sample_instance.strip():
        candidate, notice = pending_reviewer(definition, node_ids, data.sample_instance)
        if candidate:
            reviewer = candidate
            reviewer_source = "PENDING_TASK"
        elif data.sample_instance.strip() and not notice:
            notice = "样本没有停留在所选预审节点的待办，请确认模板和审批当前所在节点"
        if notice:
            notices.append(notice)
    user = db.get(User, reviewer) if reviewer else None
    if user is not None and not user.enabled:
        reviewer, reviewer_source = "", "NONE"
        notices.append("原审批人已停用，请重新选择专用审批成员")
    unresolved = [key for key, value in (("amount", mapping.amount), ("node_ids", node_ids), ("reviewer_open_id", reviewer)) if not value]
    return t.TemplateInspectOut(
        definition=definition, field_mapping=mapping, node_ids=node_ids, node_id=node_ids[0] if len(node_ids) == 1 else "",
        unresolved_fields=unresolved,
        business_type=business, reviewer_open_id=reviewer, reviewer_name=user.name if user and reviewer else "",
        reviewer_source=reviewer_source, existing_template=s.TemplateOut.model_validate(row) if row else None, notices=notices)
