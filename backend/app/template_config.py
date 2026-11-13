"""模板配置统一保存入口，普通编辑和迁移导入共用待办失效与审计逻辑。"""
from sqlalchemy import select

from . import schemas as s
from .audit_policy import configurable, default_rule_entries, explicit_rules
from .db import begin_write
from .ai_status import payment_collection_enabled
from .integrations import DomainError
from .models import Approval, ApprovalTemplate
from .services import invalidate_pending, log, settle_disabled_ai_work


def set_template_ai_approval(db, code: str, enabled: bool, actor: str) -> ApprovalTemplate | None:
    """单独保存模板开关，避免卡片上的操作覆盖其他管理员刚保存的映射或规则。"""
    begin_write(db)
    row = db.get(ApprovalTemplate, code)
    if not row or row.ai_approval_enabled == enabled:
        return row
    if payment_collection_enabled(row):
        raise DomainError("付款仅采集模式已开启，请先关闭仅采集开关再调整 AI 审批")
    row.ai_approval_enabled, row.mode = enabled, "OBSERVE"
    db.flush()
    if not enabled:
        settle_disabled_ai_work(db, code)
    invalidate_pending(db, code, ai_approval_changed=True)
    log(db, actor, "开启 AI 审批" if enabled else "关闭 AI 审批", row.name,
        {"template_code": code, "ai_approval_enabled": enabled})
    return row


def set_template_payment_auto_approval(db, code: str, enabled: bool, actor: str) -> ApprovalTemplate | None:
    """只修改付款策略，不覆盖 AI、规则或模式；同状态重复保存不重复安排刷新。"""
    begin_write(db)
    row = db.get(ApprovalTemplate, code)
    if not row:
        return None
    if row.business_type != "PAYMENT":
        raise DomainError("仅付款模板支持仅采集自动同意")
    if row.payment_auto_approve_enabled == enabled:
        return row
    row.payment_auto_approve_enabled = enabled
    db.flush()
    from .payment_collection import reset_payment_work
    reset_payment_work(db, row)
    log(db, actor, "开启付款仅采集自动同意" if enabled else "关闭付款仅采集自动同意", row.name,
        {"template_code": code, "payment_auto_approve_enabled": enabled})
    return row


def apply_template_config(db, data: s.TemplateInput, actor: str, *, importing: bool = False) -> ApprovalTemplate:
    """在调用方事务中保存；不提交，允许多个导入条目一起成功或一起回滚。

    普通编辑保留旧客户端的字段合并语义；导入采用配置包内的完整规则，
    避免目标模板的旧规则混入源配置，并强制重新验证接入能力。
    """
    begin_write(db)
    row = db.get(ApprovalTemplate, data.code)
    business_type = data.business_type if not row or "business_type" in data.model_fields_set else row.business_type
    payment_enabled = (data.payment_auto_approve_enabled if "payment_auto_approve_enabled" in data.model_fields_set
        else row.payment_auto_approve_enabled if row else business_type == "PAYMENT")
    if importing:
        payment_enabled = business_type == "PAYMENT"
    if business_type != "PAYMENT" and payment_enabled:
        raise DomainError("仅付款模板支持仅采集自动同意")
    was_collecting = payment_collection_enabled(row)
    collecting = business_type == "PAYMENT" and payment_enabled
    required = {"definition_read", "details_read", "files_downloaded", "node_matched", "task_write"}
    if not importing and not collecting and data.mode == "AUTO" and (not row or not row.ai_approval_enabled):
        raise DomainError("请先开启此模板的 AI 审批，再开启自动处理")
    if not importing and not collecting and data.mode == "AUTO" and (
        not row or not row.subscribed or not all(row.capabilities.get(key) for key in required)
    ):
        raise DomainError("请先完成模板读取、附件下载、节点身份核对和一次人工回写，再开启自动模式")
    is_new = row is None
    if is_new:
        row = ApprovalTemplate(code=data.code)
        db.add(row)
    elif row.business_type != data.business_type and "business_type" in data.model_fields_set:
        if db.scalar(select(Approval.id).where(Approval.template_code == data.code).limit(1)):
            raise DomainError("已有审批实例的模板不能改换业务类型，请新增模板")
    old_node_ids = row.node_ids or []
    node_ids = list(dict.fromkeys(data.node_ids or [data.node_id]))
    if data.node_ids is None and len(old_node_ids) > 1:
        if data.node_id != row.node_id:
            raise DomainError("此模板已配置多个专用预审节点，请通过新版配置修改节点范围")
        node_ids = old_node_ids
    node_scope_changed = set(node_ids) != set(old_node_ids)
    if not node_scope_changed:
        # 节点集合相同则沿用原顺序，避免仅调整顺序时清空联调能力或更换兼容首项。
        node_ids = old_node_ids
    task_scope_changed = node_scope_changed or row.reviewer_open_id != data.reviewer_open_id
    values = data.model_dump(mode="json", exclude={"code", "node_id", "node_ids"})
    values.update(node_ids=node_ids, node_id=node_ids[0])
    values["payment_auto_approve_enabled"] = payment_enabled
    if collecting and not is_new and data.mode != row.mode and not importing:
        raise DomainError("付款仅采集模式下保留原自动处理配置，请关闭仅采集开关后再调整")
    policy_fields = {"policy_schema_version", "builtin_rules", "custom_rules", "entries"}
    if not importing:
        # 普通编辑兼容旧客户端；导入已明确携带完整结构，不能隐式升级规则版本。
        if is_new:
            if "policy_schema_version" not in data.rules.model_fields_set:
                values["rules"].update(policy_schema_version=3, entries=default_rule_entries(data.business_type))
        elif "rules" not in data.model_fields_set:
            values["rules"] = dict(row.rules or {})
        else:
            old_rules = row.rules or {}
            preserve_new_policy = (
                configurable(old_rules) and not configurable(values["rules"])
            ) or (explicit_rules(old_rules) and not explicit_rules(values["rules"]))
            for field in policy_fields:
                if field not in data.rules.model_fields_set or preserve_new_policy:
                    if field in old_rules:
                        values["rules"][field] = old_rules[field]
            values["rules"] = s.AuditRules.model_validate(values["rules"]).model_dump(mode="json")
        if not is_new and "business_type" not in data.model_fields_set:
            values.pop("business_type")
        if not is_new and explicit_rules(values["rules"]) and not explicit_rules(row.rules):
            previous_rules = row.rules or {}
            duplicate = (previous_rules.get("builtin_rules") or {}).get("DUPLICATE") or {
                "enabled": True, "on_failure": "REJECT" if configurable(previous_rules) else "REVIEW"}
            values["rules"]["builtin_rules"]["DUPLICATE"] = dict(duplicate)
        if not is_new and "agent_prompt" not in data.model_fields_set:
            values.pop("agent_prompt")
    if "agent_prompt" in values:
        values["agent_prompt"] = values["agent_prompt"].strip()
    mapping_changed = row.field_mapping != values["field_mapping"]
    activated = row.active is False and values["active"]
    if importing:
        values.update(mode="OBSERVE", ai_approval_enabled=False, active=False, subscribed=False, capabilities={}, sync_cursor=None)
    if task_scope_changed or mapping_changed:
        values.update(capabilities={}, mode="OBSERVE")
    changed = importing or is_new or any(getattr(row, key) != value for key, value in values.items())
    for key, value in values.items():
        setattr(row, key, value)
    db.flush()
    if changed:
        if importing:
            settle_disabled_ai_work(db, data.code)
        if business_type == "PAYMENT" and (was_collecting != collecting or collecting):
            from .payment_collection import reset_payment_work
            reset_payment_work(db, row, reset_nodes=task_scope_changed)
        else:
            invalidate_pending(db, data.code, reset_pre_review_task=task_scope_changed,
                               refresh_source=mapping_changed or activated, ai_approval_changed=importing)
        log(db, actor, "导入审批模板配置" if importing else "更新审批接入配置", row.name, {
            "mode": row.mode, "rule_version": row.rules.get("version", "1.0"),
            "policy_schema_version": row.rules.get("policy_schema_version", 1),
            "prompt_source": "node" if row.agent_prompt else "global"})
    return row
