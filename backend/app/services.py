"""审批领域服务。所有入口共用相同的规则、原子占用和飞书写入边界。"""
import copy
import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from .config import settings
from .db import begin_write
from .integrations import DomainError, FeishuGateway, gateway
from .agent_runtime import model_analyze
from .ai_logs import AICallRecorder
from .ai_status import (ACTIVE_AI_STATUSES, can_run_ai_approval, can_run_automatic_audit, complete_ai_audit, payment_collection_enabled,
                        invalidate_ai_audit, reset_ai_audit, settle_ai_audit, start_ai_audit, update_job_ai_audit)
from .errors import StaleSnapshotError
from .model_config import audit_config_snapshot, current_audit_fingerprint, load_model_config
from .ocr_config import load_ocr_config
from .jobs import COMPENSATION_AUDIT_PREFIX, enqueue
from .models import (ActivityLog, Approval, ApprovalAction, ApprovalTemplate, ArchiveRecord, Attachment,
                     AuditRun, Company, EventInbox, Invoice, InvoiceClaim, Job, User, now, uid)
from .parser import normalize, parse_with_fallback
from .invoice_recognition import normalized_candidate, recognition_cache_valid, recognition_passed
from .audit_policy import (apply_evidence_review, builtin_policy, configurable, enabled_custom_rules,
                           explicit_rules, has_approval_conditions, no_conditions_evidence, rejection_items, rule_evidence)
from .audit_messages import concise, duplicate_message, evidence_message, invoice_label, model_message, rejection_comment
from .company_subjects import compare_declared_company, read_company_declaration
from .storage import resolve, safe_name, save_original
from .attachment_archives import (ExpansionBudget, apply_expansion, archive_cache, archive_members,
                                  archive_state, archive_versions, attachment_key, has_unread_archive, upsert_original)
from .attachment_policy import invoice_attachment, skip_invoice_parsing
from .file_types import ARCHIVE_MIMES
from .manual_review import apply_full_review, conflict_snapshot, content_fingerprint, full_review
from .pre_review import matches_pre_review_node, select_pre_review_task, task_open_id
from .reference_pdfs import enqueue_reference_pdfs, reference_approval, reference_pdfs


PARTIAL_CONTRACT_EVIDENCE = "MATERIAL_PARTIAL"
ACTIVE_ACTION_STATUSES = ("REQUESTED", "EXECUTING", "UNKNOWN")


def log(db, actor: str, action: str, target: str, detail: dict | None = None):
    db.add(ActivityLog(actor=actor, action=action, target=target, detail=detail or {}))


def clear_disabled_ai_state(approval: Approval, *, reviewed: bool) -> bool:
    """清除待办的旧 AI 资格，返回是否需要使非人工结论的版本失效。"""
    stale = not reviewed and (
        approval.decision != "PENDING" or bool(approval.audit_attachment_version)
        or approval.audit_requested or approval.ai_status != "NOT_STARTED" or approval.ai_task_id is not None
    )
    if not reviewed:
        approval.decision, approval.audit_attachment_version = "PENDING", ""
    approval.audit_requested = False
    reset_ai_audit(approval)
    return stale


def settle_disabled_ai_work(db, template_code: str | None = None) -> None:
    """在关停事务中作废排队任务。已进入回写的操作保留锁，由原任务先向飞书对账。"""
    approvals = select(Approval.id).join(ApprovalTemplate).where(ApprovalTemplate.ai_approval_enabled.is_(False))
    if template_code:
        approvals = approvals.where(Approval.template_code == template_code)
    db.execute(update(Job).where(Job.kind == "AUDIT", Job.status == "PENDING",
        Job.payload["instance_code"].as_string().in_(approvals)).values(
            status="SUCCEEDED", error="", result={"skipped": "ai_approval_disabled"}))
    db.execute(update(ApprovalAction).where(ApprovalAction.actor_id == "system-agent",
        ApprovalAction.execution_source == "STANDARD", ApprovalAction.status == "REQUESTED", ApprovalAction.approval_id.in_(approvals)).values(
            status="FAILED", error="模板 AI 审批已关闭，已停止自动回写", task_operation_key=None))
    # 归档也可能已有一轮预审在途。仅递增在途终态单据的版本，避免关停再开启时接收旧结果。
    db.execute(update(Approval).where(Approval.id.in_(approvals), Approval.status != "PENDING",
        Approval.ai_status.in_(ACTIVE_AI_STATUSES)).values(
            ai_status="NOT_STARTED", ai_task_id=None, ai_requested_at=None, version=Approval.version + 1))


def invalidate_pending(db, template_code: str | None = None, *, inherited_only=False, reset_pre_review_task=False,
                       refresh_source=False, ai_approval_changed=False) -> int:
    """在配置事务中使旧结论失效并提交重审发件箱；回写中的审批保留延迟重审标记。"""
    statement = select(Approval, ApprovalTemplate).join(ApprovalTemplate).where(Approval.status == "PENDING")
    if template_code:
        statement = statement.where(Approval.template_code == template_code)
    else:
        statement = statement.where(ApprovalTemplate.ai_approval_enabled.is_(True))
    if inherited_only:
        statement = statement.where(func.trim(ApprovalTemplate.agent_prompt) == "")
    count = 0
    refreshed_payment_templates = set()
    for approval, template in db.execute(statement.order_by(Approval.id)):
        if payment_collection_enabled(template):
            # 材料采集不依赖规则或模型语义配置；节点、字段和启用范围变化仍须重新同步。
            if (reset_pre_review_task or refresh_source) and template.code not in refreshed_payment_templates:
                from .payment_collection import reset_payment_work
                reset_payment_work(db, template, reset_nodes=reset_pre_review_task)
                refreshed_payment_templates.add(template.code)
            continue
        reviewed = full_review(db, approval)
        if reviewed and template.ai_approval_enabled and not reset_pre_review_task:
            # 配置变化只影响自动审核；已经确认的业务内容继续由人工结论负责。
            continue
        if reset_pre_review_task:
            # 节点或身份变更后，配置提交与旧待办失效必须原子完成，包含已整单复核的审批。
            approval.task_id = approval.node_id = approval.assignee = ""
            approval.task_status = "MISSING"
        if not template.ai_approval_enabled:
            stale = clear_disabled_ai_state(approval, reviewed=bool(reviewed))
            if not (stale or reset_pre_review_task or refresh_source or (ai_approval_changed and not reviewed)):
                continue
            if template.active and (reset_pre_review_task or refresh_source):
                enqueue(db, "REFRESH", {"instance_code": approval.id})
        elif not reviewed:
            approval.decision, approval.audit_attachment_version = "PENDING", ""
            # 开启开关时只补做当前专用节点的待办，其余单据由后续同步按原条件处理。
            approval.audit_requested = not ai_approval_changed or (
                approval.task_status == "PENDING" and bool(approval.task_id) and approval.assignee == template.reviewer_open_id)
            invalidate_ai_audit(approval)
        approval.version += 1
        if can_run_ai_approval(template):
            if reviewed or reset_pre_review_task or refresh_source:
                # 节点范围已失效或源字段改变时先重新同步，不能用被清空的待办直接排队审核。
                enqueue(db, "REFRESH", {"instance_code": approval.id})
            elif approval.audit_requested:
                enqueue_automatic_audit(db, approval)
        count += 1
    return count


def requeue_requested_audit(db, approval: Approval) -> None:
    """回写结束或重启恢复时补发重审。未知回写必须先对账，不提前释放它的操作锁。"""
    template = db.get(ApprovalTemplate, approval.template_code)
    if payment_collection_enabled(template):
        from .payment_collection import enqueue_payment_work
        approval.audit_requested = False
        enqueue_payment_work(db, approval)
        return
    if not approval.audit_requested:
        return
    if full_review(db, approval):
        approval.audit_requested = False
        return
    if approval.status != "PENDING" or not template or not template.ai_approval_enabled:
        approval.audit_requested = False
        return
    if not template.active:
        return
    if not can_run_automatic_audit(approval, template):
        # 保留待重审意图，进入专用节点后由同步补发；不能在重启或回写收尾时提前审核。
        if approval.ai_task_id:
            settle_ai_audit(approval, approval.ai_task_id, waiting_for_node=True)
        return
    db.flush()
    if not active_approval_action(db, approval.id):
        # 不沿用已经“跳过”的 AUDIT 的幂等键，否则会取到 SUCCEEDED 的旧任务而永远不重算。
        enqueue(db, "AUDIT", {"instance_code": approval.id, "only_if_needed": True})


def audit_config_matches(db, approval: Approval, template: ApprovalTemplate) -> bool:
    """自动结论须匹配配置；有效整单人工结论只绑定业务内容。"""
    if full_review(db, approval):
        return True
    if not template.ai_approval_enabled:
        return False
    audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
    if audit and (audit.model_analysis.get("manual_review") or {}).get("scope") == "ALL":
        return False
    expected = current_audit_fingerprint(db, template)
    return bool(audit and audit.model_analysis.get("config_fingerprint") == expected)


def audit_result_is_current(db, approval: Approval, template: ApprovalTemplate) -> bool:
    """判断最近一轮审核可否复用。同步检测表单和附件变化，配置用生效指纹比较。"""
    if full_review(db, approval):
        return True
    if (approval.audit_requested or approval.decision not in {"PASS", "REVIEW", "BLOCK"}
            or approval.audit_attachment_version != approval.attachment_version or not approval.last_audit_id):
        return False
    audit = db.get(AuditRun, approval.last_audit_id)
    return bool(audit and audit.approval_id == approval.id and audit.attachment_version == approval.attachment_version
                and audit_config_matches(db, approval, template))


def active_approval_action(db, approval_id: str) -> str | None:
    """返回仍持有回写锁的操作，UNKNOWN 必须继续向飞书对账。"""
    return db.scalar(select(ApprovalAction.id).where(
        ApprovalAction.approval_id == approval_id, ApprovalAction.status.in_(ACTIVE_ACTION_STATUSES)).limit(1))


def enqueue_automatic_audit(db, approval: Approval) -> None:
    """在写事务内按同单据同版本合并回调与补偿任务，保留回写期间的重审请求。"""
    template = db.get(ApprovalTemplate, approval.template_code)
    if payment_collection_enabled(template):
        from .payment_collection import enqueue_payment_work
        enqueue_payment_work(db, approval)
        return
    if (not can_run_automatic_audit(approval, template)
            or audit_result_is_current(db, approval, template)):
        return
    if active_approval_action(db, approval.id):
        approval.audit_requested = True
        return
    enqueue(db, "AUDIT", {"instance_code": approval.id, "only_if_needed": True},
            f"{COMPENSATION_AUDIT_PREFIX}{approval.id}:{approval.version}")


def _check_partial_contract_action(db, approval: Approval, actor_id: str) -> None:
    """限页合同须经整单复核，附件同步和类别重审期间也不能跳过。"""
    if approval.business_type != "PAYMENT":
        return
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
    partial = any((item.parsed_data or {}).get("material", {}).get("page_scope") for item in attachments)
    pending_recheck = (audit and audit.approval_id == approval.id and audit.attachment_version == approval.attachment_version
                       and any(item["code"] == PARTIAL_CONTRACT_EVIDENCE for item in audit.evidence))
    if (partial or pending_recheck) and (actor_id == "system-agent" or not full_review(db, approval, audit=audit, attachments=attachments)):
        raise DomainError("合同仅自动读取了部分页面，请由财务核对完整原件并完成整单复核后处理")


def semantic_review_fingerprints(approval: Approval, invoices: list[dict], config_fingerprint: str, *,
                                 payment_materials: list[dict] | None, legacy_review: bool) -> list[str]:
    """首项绑定申请人身份，其余项兼容按旧展示值记录的逐项复核。"""
    material = {"description": approval.title, "expense_type": approval.expense_type, "amount": str(approval.amount),
                "department": approval.department, "form": audit_form_snapshot(approval.raw_form),
                "attachment_version": approval.attachment_version, "config_fingerprint": config_fingerprint,
                "invoices": sorted(invoices, key=lambda item: item["invoice_key"])}
    identity = approval.applicant_id or approval.applicant
    applicants = [identity]
    if legacy_review:
        applicants.extend((approval.applicant, identity.removeprefix("user_id:"), "未知申请人"))
    fingerprints = [hashlib.sha256(json.dumps({**material, "applicant": applicant}, sort_keys=True,
        ensure_ascii=False).encode()).hexdigest() for applicant in dict.fromkeys(applicants)]
    if payment_materials is None:
        return fingerprints
    # 付款复核仍需比对原材料及确认状态。历史指纹和新指纹使用同一份材料快照。
    material_identity = [{"id": item["attachment_id"], "type": item["material_type"],
                          "complete": item.get("complete", False), "confirmation": item.get("confirmation")}
                         for item in payment_materials]
    payment_snapshot = json.dumps({"materials": material_identity, "payment": approval.payment_details},
                                  ensure_ascii=False, sort_keys=True)
    return [hashlib.sha256((value + payment_snapshot).encode()).hexdigest() for value in fingerprints]


def audit_conclusion(evidence: list[dict]) -> str:
    """确定性规则与人工确认共用同一结论计算，任何未解决的阻断项都优先阻断。"""
    failed = [item for item in evidence if not item["passed"] and item["severity"] in {"REVIEW", "BLOCK"}]
    return "BLOCK" if any(item["severity"] == "BLOCK" for item in failed) else "REVIEW" if failed else "PASS"


def field_values(form: list, target_id: str) -> list:
    values = []
    for item in form:
        if not isinstance(item, dict):
            continue
        if item.get("id") == target_id:
            values.append(item.get("value"))
        value = item.get("value")
        if isinstance(value, list):
            for child in value:
                values.extend(field_values(child if isinstance(child, list) else [child], target_id))
    return values


def approval_department(snapshot: dict, form: list, mapping: dict) -> str:
    """从已提交表单取部门显示名；优先使用映射，多个候选有歧义时保留实例信息。"""
    from .payment_service import display

    field_id = mapping.get("department", "")
    if field_id:
        text = "、".join(filter(None, (display(value) for value in field_values(form, field_id))))
        if text:
            return text[:100]
    # 报销模板未配置部门映射时，也读取原生部门控件携带的名称，不把 open_id 当作名称。
    fields = [item for item in form if isinstance(item, dict) and str(item.get("type", "")).lower() == "department"]
    named = [item for item in fields if str(item.get("name", "")).strip() in {"申请部门", "所属部门", "部门"}]
    candidates = named or fields
    if len(candidates) == 1:
        text = display(candidates[0].get("value"))
        if text:
            return text[:100]
    return str(snapshot.get("department_name") or snapshot.get("department_id") or "")[:100]


def audit_form_snapshot(form: list) -> list:
    """返回用于版本比较的表单副本；附件链接由原件内容哈希核验，不参与业务变更判断。"""
    def visit(value, attachment_context=False):
        if isinstance(value, list):
            return [visit(item, attachment_context) for item in value]
        if isinstance(value, dict):
            kind = str(value.get("type", "")).lower()
            context = attachment_context or kind in {"attachment", "attachmentv2", "image", "imagev2"}
            return {key: visit(child, context) for key, child in value.items()}
        if isinstance(value, str) and attachment_context:
            # 飞书附件控件既可能直接返回对象/数组，也可能把它们编码在 value 字符串里。
            # 只屏蔽附件控件中的下载地址；普通文本中的网址、金额、事由等仍须精确比较。
            if value.startswith(("https://", "http://")):
                return "<attachment-content-checked-by-sha256>"
            try:
                decoded = json.loads(value)
            except ValueError:
                return value
            if isinstance(decoded, (dict, list)) or (isinstance(decoded, str) and decoded.startswith(("https://", "http://"))):
                return visit(decoded, True)
        return value

    return visit(form)


def synced_audit_inputs(approval: Approval) -> tuple:
    """同步时比较参与审核的稳定业务数据，排除飞书流转状态与附件临时链接。"""
    return (approval.amount, approval.title, approval.expense_type, approval.department,
            approval.applicant_id or approval.applicant, audit_form_snapshot(approval.raw_form),
            approval.attachment_version, (approval.payment_details or {}).get("company_id"))


def attachment_descriptors(snapshot: dict, allowed_ids: list[str]) -> list[dict]:
    """读取配置范围内的飞书原件地址，附件名称与稳定来源键分别处理。"""
    # 表单展示依赖模板配置，后者也复用审批服务。推迟导入避免模块初始化互相等待。
    from .feishu_form import attachment_name, attachment_names, attachment_values

    if settings().app_mode == "demo" and "demo_files" in snapshot:
        return snapshot["demo_files"]
    form = snapshot.get("form", "[]")
    if isinstance(form, str):
        form = json.loads(form)
    result = []

    def visit(value, parent_id="", attachment_context=False, suggested_name=""):
        if isinstance(value, list):
            for child in value:
                visit(child, parent_id, attachment_context)
        elif isinstance(value, dict):
            field_id = str(value.get("id", parent_id))
            kind = str(value.get("type", "")).lower()
            is_attachment = kind in {"attachment", "attachmentv2", "image", "imagev2"}
            context = attachment_context or is_attachment
            url = value.get("url") or value.get("file_url") or value.get("download_url")
            if isinstance(url, str) and url and (context or "name" in value or "file_name" in value or attachment_name(value)):
                if not allowed_ids or parent_id in allowed_ids or field_id in allowed_ids:
                    # 保留旧来源键的生成规则，修正后缀时可复用同一附件版本。
                    legacy_name = value.get("name") or value.get("file_name") or f"{len(result)+1}.pdf"
                    filename = attachment_name(value) or suggested_name or f"附件-{len(result)+1}"
                    result.append({"key": str(value.get("token") or value.get("file_token") or f"{parent_id}:{legacy_name}"),
                                   "filename": filename, "url": url})
                return
            for key, child in value.items():
                if context and key == "value":
                    if isinstance(child, str) and child.strip().startswith('"'):
                        try:
                            child = json.loads(child)
                        except ValueError:
                            pass
                    files = attachment_values(child)
                    for item, name in zip(files, attachment_names(value.get("ext"), len(files))):
                        visit(item, field_id, context, name)
                elif isinstance(child, (dict, list)) and not (is_attachment and key == "ext"):
                    visit(child, field_id, context)
        elif isinstance(value, str) and attachment_context and value.startswith("https://"):
            if not allowed_ids or parent_id in allowed_ids:
                result.append({"key": f"{parent_id}:{len(result)}",
                               "filename": suggested_name or f"附件-{len(result)+1}", "url": value})

    visit(form)
    # 评论和动态中的补充附件也要进入最终集合，不能只处理首次提交的表单。
    allowed_ids = []
    visit(snapshot.get("comment_list", []), "comment", True)
    visit(snapshot.get("timeline", []), "timeline", True)
    unique = {f"{item['key']}:{item['url'].split('?')[0]}": item for item in result}
    if len(unique) > 100:
        raise DomainError("单据附件超过 100 个，请人工拆分处理")
    return list(unique.values())


def update_capability(template, key: str, value=True):
    template.capabilities = {**(template.capabilities or {}), key: value}


def sync_instance(db, instance_code: str, template_code: str | None = None) -> Approval:
    """先下载完整原件，再校验读取期间的版本；同步状态与附件集合在一个短事务中提交。"""
    db.rollback()
    remote = gateway(db)
    snapshot = remote.get_instance(instance_code)
    code = snapshot.get("approval_code") or template_code
    template = db.get(ApprovalTemplate, code)
    if not template or not template.active:
        raise DomainError("审批模板尚未配置或已停用")
    approval = db.get(Approval, instance_code)
    expected_version = approval.version if approval else None
    template_snapshot = (tuple(template.node_ids), template.reviewer_open_id, copy.deepcopy(template.field_mapping), template.business_type)
    form = snapshot.get("form", [])
    if isinstance(form, str):
        form = json.loads(form)
    mapping = copy.deepcopy(template.field_mapping or {})
    values = lambda key: field_values(form, mapping.get(key, ""))
    money = values("amount")
    try:
        amount = Decimal(str(money[0]).replace(",", "")) if money else Decimal("0")
        if not amount.is_finite() or amount < 0 or amount >= Decimal("10000000000000000"):
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise DomainError("报销金额字段缺失或不是有效金额，请检查模板映射")
    descriptors = attachment_descriptors(snapshot, mapping.get("attachment_ids", []))
    cached = archive_cache(list(db.scalars(select(Attachment).where(Attachment.approval_id == instance_code)))) if approval else {}
    db.rollback()
    # 保存不可变原件不需要写锁；下载失败时不会提交半套附件集合。
    downloaded, material_issues = [], []
    for descriptor in descriptors:
        try:
            downloaded.append((descriptor, save_original(remote.download(descriptor), descriptor["filename"])))
        except Exception as exc:
            if template_snapshot[3] != "PAYMENT":
                raise
            material_issues.append({"key": str(descriptor.get("key", "")), "filename": descriptor["filename"],
                "reason": str(exc)[:300] if isinstance(exc, (DomainError, ValueError)) else "附件下载失败，请稍后重试或到飞书核对原件"})
    budget = ExpansionBudget()
    expansions = {}
    for descriptor, saved in downloaded:
        identity = (attachment_key(descriptor, saved), saved["sha256"])
        if saved["mime"] in ARCHIVE_MIMES and identity not in expansions:
            expansions[identity] = budget.prepare(saved, cached.get(identity))
    begin_write(db)
    template = db.get(ApprovalTemplate, code)
    approval = db.get(Approval, instance_code)
    if (not template or not template.active
            or (tuple(template.node_ids), template.reviewer_open_id, template.field_mapping, template.business_type) != template_snapshot
            or (approval.version if approval else None) != expected_version
            or (settings().app_mode == "demo" and approval and approval.external_snapshot != snapshot)):
        raise StaleSnapshotError("下载期间审批或模板已更新，请重新同步")
    if not approval:
        approval = Approval(id=instance_code, template_code=code,
            business_type=template.business_type,
            serial_number=snapshot.get("serial_number") or instance_code, title=template.name,
            applicant=snapshot.get("applicant_name") or snapshot.get("open_id") or snapshot.get("user_id") or "未知申请人")
        db.add(approval)
        db.flush()
    old_inputs = synced_audit_inputs(approval)
    old_workflow = (approval.status, approval.task_id, approval.task_status, approval.node_id, approval.assignee)
    old_pending_task = approval.task_id if approval.status == approval.task_status == "PENDING" else ""
    approval.title = str((values("description") or [snapshot.get("approval_name", template.name)])[0])[:200]
    approval.amount = amount.quantize(Decimal("0.01"))
    approval.department = approval_department(snapshot, form, mapping)
    approval.expense_type = str((values("expense_type") or ["其他费用"])[0])[:80]
    from .reimbursement_metadata import apply_snapshot_metadata
    apply_snapshot_metadata(approval, snapshot)
    approval.raw_form = form
    if approval.business_type == "PAYMENT":
        from .payment_service import payment_fields
        approval.payment_details, payment_department = payment_fields(db, form, mapping, material_issues)
        if payment_department:
            approval.department = payment_department[:100]
    task, approval.task_status = select_pre_review_task(
        snapshot.get("task_list") or [], template.node_ids, template.reviewer_open_id)
    approval.task_id, approval.node_id = str(task.get("id") or ""), str(task.get("node_id") or "")
    approval.assignee = task_open_id(task)
    # 飞书的已通过原单可能仍返回 APPROVED，通过 reverted 独立标志表达“已被撤销”。
    approval.status = "REVERTED" if snapshot.get("reverted") is True else snapshot.get("status", "PENDING")
    approval.approved_once = approval.approved_once or approval.status in {"APPROVED", "REVERTED"}
    approval.last_synced_at = now()
    existing = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id)))
    old_archives = archive_versions(existing)
    old_audit_archives = archive_versions(existing, for_audit=True)
    originals = {(item.source_key, item.sha256): item for item in existing}
    previously_active = {item.id: item for item in existing if item.active}
    for attachment in existing:
        attachment.active = False
    fingerprint = []
    for issue in material_issues:
        fingerprint.append("unavailable:" + issue["key"])
        # 下载失败时保留已取得原件及旧占用，不能把失败误当成申请人删除了附件。
        for attachment in previously_active.values():
            if attachment.source_key == issue["key"][:200]:
                attachment.active = True
                fingerprint.append(f"{attachment.source_key}:{attachment.sha256}")
                for member in archive_state(attachment).get("members", []):
                    if child := previously_active.get(member["attachment_id"]):
                        child.active = True
    for descriptor, saved in downloaded:
        key = attachment_key(descriptor, saved)
        attachment = upsert_original(db, originals, approval.id, key, saved)
        if expansion := expansions.get((key, saved["sha256"])):
            apply_expansion(db, originals, attachment, expansion)
        fingerprint.append(f"{key}:{saved['sha256']}")
    existing = list(originals.values())
    # 业务原件版本继续使用飞书提交的内容哈希。展开清单单独使过期审核和打印失效。
    approval.attachment_version = hashlib.sha256("|".join(sorted(fingerprint)).encode()).hexdigest()
    inputs_changed = old_inputs != synced_audit_inputs(approval)
    audit_archives_changed = old_audit_archives != archive_versions(existing, for_audit=True)
    workflow = (approval.status, approval.task_id, approval.task_status, approval.node_id, approval.assignee)
    if inputs_changed or old_workflow != workflow or old_archives != archive_versions(existing):
        approval.version += 1
    # 专用待办首次出现或重新进入时需要单独评估，离开待办及同一待办的后续事件只同步状态。
    pending_task = approval.task_id if approval.status == approval.task_status == "PENDING" else ""
    entered_pre_review = can_run_ai_approval(template) and pending_task and pending_task != old_pending_task
    if (inputs_changed or audit_archives_changed or entered_pre_review) and not full_review(db, approval, attachments=existing):
        approval.decision, approval.audit_attachment_version = "PENDING", ""
        if can_run_ai_approval(template) and approval.status == "PENDING":
            approval.audit_requested = True
        invalidate_ai_audit(approval)
    update_capability(template, "details_read")
    if approval.status == "PENDING" and approval.task_status == "PENDING":
        update_capability(template, "node_matched")
    if descriptors and not material_issues:
        update_capability(template, "files_downloaded")
    # 实例最终状态是权威数据；乱序回调不会把终态恢复为旧状态。
    claims = db.scalars(select(InvoiceClaim).where(InvoiceClaim.approval_id == approval.id)).all()
    if not template.ai_approval_enabled and approval.status in {"PENDING", "APPROVED"} and not has_unread_archive(existing):
        # 人工模式没有后续预审负责清理占用。以当前原件关联为准，下载失败保留的原件也算在内。
        linked = {attachment.invoice_id for attachment in existing if attachment.active and attachment.invoice_id}
        for claim in claims:
            if claim.active_key and claim.invoice_id not in linked:
                claim.status = "FROZEN" if approval.approved_once else "RELEASED"
                if claim.status == "RELEASED":
                    claim.active_key = None
    if approval.status in {"REJECTED", "CANCELED", "DELETED", "REVERTED"}:
        for claim in claims:
            if claim.active_key:
                claim.status = "FROZEN" if approval.approved_once or claim.status == "USED" else "RELEASED"
                if claim.status == "RELEASED":
                    claim.active_key = None
        if approval.approved_once:
            for record in db.scalars(select(ArchiveRecord).where(ArchiveRecord.approval_id == approval.id)):
                record.status = "REVOKED"
            approval.archive_status = "REVOKED"
    elif approval.status == "APPROVED":
        for claim in claims:
            if claim.active_key and claim.status == "RESERVED":
                claim.status = "USED"
    db.flush()
    reverted_origin = snapshot.get("reverted_instance_code")
    if reverted_origin and reverted_origin != approval.id and approval.status == "APPROVED":
        # 撤销申请与原单可产生不同事件，收到撤销申请通过时主动对账原单。
        enqueue(db, "REFRESH", {"instance_code": reverted_origin}, f"reverted-origin:{approval.id}:{reverted_origin}")
    from .archive_metadata import observe_archive
    observe_archive(db, approval, snapshot, enqueue)
    if reference_approval(approval):
        enqueue_reference_pdfs(db, approval.id, reference_pdfs(existing), automatic=True)
    from .budgets import refresh_usage
    refresh_usage(db, approval)
    if payment_collection_enabled(template):
        from .payment_collection import enqueue_payment_work
        enqueue_payment_work(db, approval, tasks=snapshot.get("task_list") or [])
    db.commit()
    return approval


def canonical_invoice(db, fields: dict, historical=False) -> Invoice:
    invoice = db.scalar(select(Invoice).where(Invoice.invoice_key == fields["invoice_key"]))
    if invoice:
        return invoice
    company = db.scalar(select(Company).where(Company.tax_id == fields["buyer_tax_id"], Company.active.is_(True)))
    invoice = Invoice(invoice_key=fields["invoice_key"], number=fields["number"], code=fields.get("code", ""),
        kind=fields["kind"], buyer_name=fields["buyer_name"], buyer_tax_id=fields["buyer_tax_id"],
        seller_name=fields.get("seller_name", ""), issue_date=date.fromisoformat(fields["issue_date"]),
        amount=Decimal(fields["amount"]), company_id=company.id if company else None, historical=historical)
    try:
        with db.begin_nested():
            db.add(invoice)
            db.flush()
    except IntegrityError:
        invoice = db.scalar(select(Invoice).where(Invoice.invoice_key == fields["invoice_key"]))
    return invoice


def reserve_invoice(db, invoice: Invoice, approval: Approval) -> InvoiceClaim | None:
    """原子占用而非先查后写；数据库唯一键决定并发竞争的唯一胜者。"""
    owned = db.scalar(select(InvoiceClaim).where(InvoiceClaim.invoice_id == invoice.id,
                                                InvoiceClaim.approval_id == approval.id))
    if owned and owned.active_key:
        return None if owned.status == "FROZEN" else owned
    claim = owned or InvoiceClaim(invoice_id=invoice.id, approval_id=approval.id, source_label=approval.serial_number)
    try:
        with db.begin_nested():
            claim.active_key, claim.status = invoice.id, "USED" if approval.status == "APPROVED" else "RESERVED"
            db.add(claim)
            db.flush()
        return claim
    except IntegrityError:
        return None


def require_current_ai_snapshot(db, instance_code: str, expected_version: int, *,
                                pre_review_task_id: str | None = None) -> None:
    """外部请求前复核开关和版本；自动审核另绑定原专用待办，人工和归档不传待办限制。"""
    db.rollback()
    current = db.execute(select(Approval, ApprovalTemplate).join(ApprovalTemplate)
        .where(Approval.id == instance_code)).one_or_none()
    valid = bool(current and current[0].version == expected_version and can_run_ai_approval(current[1]))
    node_valid = pre_review_task_id is None or bool(current
        and can_run_automatic_audit(current[0], current[1]) and current[0].task_id == pre_review_task_id)
    db.rollback()
    if not node_valid:
        raise StaleSnapshotError("已离开本轮专用预审待办，停止自动审核")
    if not valid:
        raise StaleSnapshotError("AI 审批已关闭或审批版本已变化，已停止本轮处理")


def run_audit(db, instance_code: str, *, cached_only=False, suppress_actions=False,
              only_if_needed: bool = False, archive_synced: bool = False) -> dict:
    """执行预审。only_if_needed 用于自动待办，archive_synced 用于已同步的终态补审。

    两个入口均可复用有效结论。手动重审走默认分支，存量刷新可仅使用本地缓存并禁止自动回写。
    """
    job_id = db.info.get("current_job_id")
    ai_task_id = job_id or uid()
    begin_write(db)
    approval = db.get(Approval, instance_code)
    if approval:
        if not can_run_ai_approval(db.get(ApprovalTemplate, approval.template_code)):
            db.commit()
            return {"approval_id": instance_code, "skipped": "ai_approval_disabled"}
        if not (only_if_needed or archive_synced):
            start_ai_audit(db, approval, ai_task_id)
    db.commit()
    try:
        return _run_audit(db, instance_code, ai_task_id, cached_only=cached_only, suppress_actions=suppress_actions,
                          only_if_needed=only_if_needed, archive_synced=archive_synced)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, StaleSnapshotError):
            begin_write(db)
            approval = db.get(Approval, instance_code)
            template = db.get(ApprovalTemplate, approval.template_code) if approval else None
            enabled = can_run_ai_approval(template)
            waiting_for_node = only_if_needed and not archive_synced and not can_run_automatic_audit(approval, template)
            if waiting_for_node and approval:
                settle_ai_audit(approval, ai_task_id, waiting_for_node=True)
            db.commit()
            if not enabled:
                return {"approval_id": instance_code, "skipped": "ai_approval_disabled"}
            if waiting_for_node:
                return {"approval_id": instance_code, "skipped": "pre_review_not_pending"}
        # 后台失败由队列统一决定等待重试还是最终失败，直接调用则没有队列代为收尾。
        if not job_id:
            begin_write(db)
            update_job_ai_audit(db, [ai_task_id], "FAILED")
            db.commit()
        raise


def _run_audit(db, instance_code: str, ai_task_id: str, *, cached_only: bool, suppress_actions: bool,
               only_if_needed: bool, archive_synced: bool) -> dict:
    """快照解析与模型调用不持锁；结论、票据建档和占用在版本复核后原子提交。"""
    if not cached_only and not archive_synced:
        sync_instance(db, instance_code)
    begin_write(db)
    approval = db.get(Approval, instance_code)
    template = db.get(ApprovalTemplate, approval.template_code)
    if not can_run_ai_approval(template):
        db.commit()
        return {"approval_id": instance_code, "skipped": "ai_approval_disabled"}
    if only_if_needed and not archive_synced and not can_run_automatic_audit(approval, template):
        settle_ai_audit(approval, ai_task_id, waiting_for_node=True)
        db.commit()
        return {"approval_id": instance_code, "skipped": "pre_review_not_pending"}
    if only_if_needed or archive_synced:
        # 入队后可能已有别的任务完成审核，也可能已离开原审批阶段。用刚同步的快照重新判断。
        expected_status = "APPROVED" if archive_synced else "PENDING"
        if approval.status != expected_status:
            settle_ai_audit(approval, ai_task_id)
            db.commit()
            return {"approval_id": instance_code, "skipped": "status_changed"}
        if audit_result_is_current(db, approval, template):
            settle_ai_audit(approval, ai_task_id)
            db.commit()
            return {"approval_id": instance_code, "skipped": "audit_current"}
    if approval.status == "PENDING" and active_approval_action(db, approval.id):
        # 等待回写对账不使结论失效。确有变化时留下请求，交给回写任务结束后补发。
        if not audit_result_is_current(db, approval, template):
            approval.audit_requested = True
        settle_ai_audit(approval, ai_task_id)
        db.commit()
        return {"approval_id": approval.id, "skipped": "operation_in_progress"}
    start_ai_audit(db, approval, ai_task_id)
    model_config = load_model_config(db)
    ocr_config = load_ocr_config(db)
    previous_audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
    previous_review = copy.deepcopy((previous_audit.model_analysis if previous_audit else {}).get("manual_review"))
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    human_review = full_review(db, approval, audit=previous_audit, attachments=attachments)
    local_only = cached_only or bool(human_review)
    expected_version, expected_attachments = approval.version, approval.attachment_version
    pre_review_task_id = approval.task_id if only_if_needed and not archive_synced else None
    config_snapshot = audit_config_snapshot(model_config, template, ocr_version=ocr_config.recognition_version)
    expected_config = config_snapshot["config_fingerprint"]
    # 提交预审进度并脱离 ORM，解析与模型调用期间不持有事务。
    db.commit()
    db.expunge_all()

    def check_current():
        require_current_ai_snapshot(db, instance_code, expected_version, pre_review_task_id=pre_review_task_id)

    evidence, observations, company_ids = [], {}, set()
    if human_review:
        company_ids.update(approval.company_ids)
    retained_invoice_ids = set()
    payment = approval.business_type == "PAYMENT"
    declaration = read_company_declaration(approval.raw_form, template.field_mapping, include_names=not payment)
    payment_materials, invoice_attachment_count = [], 0
    has_partial_contract = False
    member_sources = archive_members(attachments)
    reference_pdf_ids = {item.id for item in reference_pdfs(attachments)}
    unread_archive = has_unread_archive(attachments)
    if payment and approval.payment_details.get("company_id"):
        company_ids.add(approval.payment_details["company_id"])

    def check(code, title, passed, detail, severity="REVIEW", related=None, **options):
        if explicit_rules(template.rules):
            from .audit_policy import BUSINESS_DEFAULT_ACTIONS
            if code in BUSINESS_DEFAULT_ACTIONS and not any(rule.get("kind") == "BUILTIN" and rule.get("builtin_code") == code for rule in template.rules.get("entries", [])):
                return
        entry = rule_evidence(template.rules, code, title, passed, detail, severity, related, **options)
        if not entry["passed"]:
            entry["user_message"] = evidence_message(entry)
            if payment and code != "DUPLICATE":
                entry["user_message"] = entry["user_message"].replace("报销", "付款申请")
        evidence.append(entry)

    if payment:
        from .payment_materials import read_payment_material
        for issue in approval.payment_details.get("material_issues", []):
            check("MATERIAL_DOWNLOAD", "付款附件尚未取得", False, issue["filename"] + "：" + issue["reason"])
    for attachment in attachments:
        if not local_only:
            check_current()
        state = archive_state(attachment)
        member = member_sources.get(attachment.id)
        parent_state = archive_state(member[0]) if member else {}
        if state or parent_state.get("mode") == "REFERENCE" or parent_state.get("status") == "FAILED":
            if attachment.invoice_id:
                retained_invoice_ids.add(attachment.invoice_id)
            if state.get("mode") == "AUDIT":
                if state["status"] == "FAILED":
                    check("ARCHIVE_READ", "压缩包待核对", False,
                          f"{attachment.filename}：{state['error']}", result="UNKNOWN")
                if attachment.invoice_id:
                    check("MATERIAL_RECLASSIFICATION", "已入台账的压缩包待核对", False,
                          attachment.filename + " 已关联发票台账，请核对原件", result="UNKNOWN")
            continue
        material_result = None
        if not payment and not invoice_attachment(attachment):
            # Excel 等普通材料不属于发票，不制造识别失败，也不影响发票金额的完整性。
            if attachment.invoice_id:
                retained_invoice_ids.add(attachment.invoice_id)
            skip_invoice_parsing(attachment)
            continue
        if payment:
            material_result = copy.deepcopy(attachment.parsed_data or {}) if local_only else read_payment_material(resolve(attachment.storage_key), model_config, sha256=attachment.sha256,
                approval_id=approval.id, template_code=template.code, parent_db=db, previous=attachment.parsed_data,
                invoice_linked=bool(attachment.invoice_id), before_request=check_current, ocr_config=ocr_config)
            material_result.setdefault("material_type", "UNKNOWN")
            material = material_result.setdefault("material", {})
            payment_materials.append({"attachment_id": attachment.id, "filename": attachment.filename,
                                     "material_type": material_result["material_type"], **material})
            if material_result["material_type"] != "INVOICE":
                if attachment.invoice_id:
                    # 已确认过的发票不能通过改分类解除跨单占用。
                    retained_invoice_ids.add(attachment.invoice_id)
                    check("MATERIAL_RECLASSIFICATION", "已入台账发票类别变更", False, attachment.filename + " 已关联发票台账，请核对原件")
                attachment.parsed_data = {**material_result, "error": ""}
                if material_result["material_type"] == "CONTRACT" and material.get("page_scope"):
                    has_partial_contract = True
                    check(PARTIAL_CONTRACT_EVIDENCE, "合同限页读取需整单复核", False,
                          f"{attachment.filename}，{material['summary']}", result="UNKNOWN")
                elif not material.get("complete"):
                    check("MATERIAL_READ", "付款材料读取待确认", False, attachment.filename + "：" + (material.get("error") or material.get("summary", "")))
                    evidence[-1]["reviewable"] = material_result["material_type"] != "UNKNOWN"
                continue
        if material_result and material_result.get("document_policy_version"):
            attachment.parsed_data = copy.deepcopy(material_result)
        elif not local_only and not recognition_cache_valid(attachment.parsed_data or {}, attachment.sha256, model_config,
                                       ocr_version=ocr_config.recognition_version):
            attachment.parsed_data = parse_with_fallback(resolve(attachment.storage_key), model_config,
                ocr_config=ocr_config, approval_id=approval.id, template_code=template.code, parent_db=db, sha256=attachment.sha256,
                previous={**(attachment.parsed_data or {}), **(material_result or {})}, before_request=check_current)
        if material_result:
            attachment.parsed_data = {**material_result, **attachment.parsed_data, "material": material_result.get("material", {})}
        result = attachment.parsed_data or {}
        if result.get("parse_status") == "SKIPPED":
            if attachment.invoice_id:
                retained_invoice_ids.add(attachment.invoice_id)
                check("MATERIAL_RECLASSIFICATION", "已入台账发票类别变更", False,
                      attachment.filename + " 已关联发票台账，请核对原件", result="UNKNOWN")
            continue
        invoice_attachment_count += 1
        attachment.parsed_data = {**result, "parse_status": "FAILED" if result.get("error") else "PARSED"}
        previous_invoice_id = attachment.invoice_id
        # 原件仍在，只是本次识别暂时失败时，保留已有占用，防止识别重试意外释放票据。
        if previous_invoice_id and not recognition_passed(result):
            retained_invoice_ids.add(previous_invoice_id)
        attachment.invoice_id = previous_invoice_id if previous_invoice_id in retained_invoice_ids else None
        recognition = result.get("recognition") or {}
        check("RECOGNITION", "票面识别一致性", recognition_passed(result),
              f"{attachment.filename}：" + ("已由财务核对确认" if result.get("confirmed") else
                  recognition.get("reason", "尚未完成当前识别流程，需要核对票面")))
        if result.get("error"):
            check("PARSE", "附件解析", False, f"{attachment.filename}：{result['error']}")
            continue
        try:
            fields = normalize(result.get("fields") or {})
        except ValueError as exc:
            check("IDENTITY", "票据身份完整性", False, str(exc))
            continue
        key = fields["invoice_key"]
        amount_conflict = key in observations and observations[key]["fields"]["amount"] != fields["amount"]
        if key in observations and observations[key]["fields"] != fields:
            check("FIELD_CONFLICT", "同票多附件一致性", False, f"发票 {fields['number']} 的不同格式附件存在票面信息冲突")
            observations[key]["conflict"] = True
        observations.setdefault(key, {"fields": fields, "reliable": False, "verified": False,
                                     "all_verified": True, "conflict": False, "amount_conflict": False,
                                     "amount_known": True, "attachments": []})
        amount_agreed = any(item.get("field") == "amount" and item.get("status") == "AGREED"
                            and normalized_candidate("amount", item.get("accepted")) == fields["amount"]
                            for item in recognition.get("comparisons", []))
        observations[key]["amount_known"] &= recognition_passed(result) or amount_agreed
        observations[key]["amount_conflict"] |= amount_conflict
        observations[key]["reliable"] |= bool(result.get("reliable") or result.get("confirmed"))
        observations[key]["verified"] |= recognition_passed(result)
        observations[key]["all_verified"] &= recognition_passed(result)
        observations[key]["attachments"].append(attachment)
    if not payment:
        if not invoice_attachment_count and reference_pdf_ids and reference_approval(approval) and not unread_archive:
            check("ATTACHMENTS", "发票原件完整性", True,
                  f"当前有 {len(reference_pdf_ids)} 份包内 PDF，请查看历史附件补充识别结果", result="SKIPPED")
        else:
            check("ATTACHMENTS", "发票原件完整性", invoice_attachment_count > 0 and not unread_archive,
                  f"已获取 {invoice_attachment_count} 个发票或待确认附件，另有 {len(attachments)-invoice_attachment_count} 个其他附件",
                  result="UNKNOWN" if unread_archive else None)
    prompt = config_snapshot["prompt"]
    if has_partial_contract:
        prompt += ("\n带 page_scope 的合同只提供第 1 页至 read_pages 页正文。"
                   "依据已读页及其他已提供材料核对并引用原页码。需要未读页面才能判断的条件必须返回 UNKNOWN。")
    if not local_only:
        check_current()
        trace = AICallRecorder(model_config, source="AUDIT", prompt=prompt,
            approval_id=approval.id, template_code=template.code, parent_db=db)
    if local_only:
        # 已人工整单确认时只刷新本地证据，不再请求 OCR、材料模型或语义模型。
        analysis = copy.deepcopy(previous_audit.model_analysis) if previous_audit else {
            "status": "SKIPPED", "summary": "本次仅刷新本地核对依据，未调用 AI"}
    elif payment:
        from .agent_runtime import run_model_analysis
        from .payment_materials import payment_model_payload
        custom_rules = enabled_custom_rules(template.rules)
        characters = sum(len(segment["text"]) for material in payment_materials for segment in material.get("segments", []))
        if characters > 120000:
            analysis = {"status": "FAILED", "summary": "付款材料正文总量超过 12 万字符，需拆分或人工核对；未截断内容进行审核"}
            trace.set_input({"material_count": len(payment_materials), "character_count": characters})
            trace.finish(analysis, error_type="MaterialLimit")
            check("MATERIAL_LIMIT", "付款材料总量超限", False, analysis["summary"])
            evidence[-1]["reviewable"] = True
        elif not custom_rules:
            analysis = {"status": "SKIPPED", "summary": "没有启用的文字条件，未调用付款语义审核"}
            trace.finish(analysis)
        else:
            analysis = run_model_analysis(model_config, prompt,
                payment_model_payload(approval, payment_materials, custom_rules), trace=trace, before_request=check_current)
    else:
        analysis = model_analyze(model_config, template, approval, [x["fields"] for x in observations.values()], trace=trace,
            declared_tax_ids=list(declaration.tax_ids), declared_company_names=list(declaration.names), before_request=check_current)
    if not local_only:
        analysis.update(config_snapshot)
    legacy_review = bool(previous_review and previous_review.get("scope") != "ALL")
    review_fingerprints = semantic_review_fingerprints(approval, [x["fields"] for x in observations.values()],
        expected_config, payment_materials=payment_materials if payment else None, legacy_review=legacy_review)
    analysis["review_fingerprint"] = review_fingerprints[0]
    parsed_attachments = attachments
    begin_write(db)
    approval = db.get(Approval, instance_code)
    template = db.get(ApprovalTemplate, approval.template_code)
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    active_operation = approval.status == "PENDING" and active_approval_action(db, approval.id)
    if not can_run_ai_approval(template):
        # 关停事务已清除任务归属，已发送的模型请求可保留日志，结果和附件解析均不入业务库。
        db.commit()
        return {"approval_id": instance_code, "skipped": "ai_approval_disabled"}
    if pre_review_task_id is not None and (
            not can_run_automatic_audit(approval, template) or approval.task_id != pre_review_task_id):
        # 即使外部调用已经完成，也不能把旧节点的识别、占用或审核结论写入新阶段。
        settle_ai_audit(approval, ai_task_id, waiting_for_node=True)
        if can_run_automatic_audit(approval, template):
            approval.audit_requested = True
            requeue_requested_audit(db, approval)
        db.commit()
        return {"approval_id": instance_code, "skipped": "pre_review_not_pending"}
    if full_review(db, approval) and not human_review:
        # AI 执行期间已经完成整单复核，旧结果不能覆盖人工判断或再安排一次 AI 审批。
        approval.audit_requested = False
        settle_ai_audit(approval, ai_task_id)
        db.commit()
        return {"approval_id": approval.id, "skipped": "manual_review_priority"}
    stale = (approval.version != expected_version or approval.attachment_version != expected_attachments
        or not template.active
        or (not human_review and current_audit_fingerprint(db, template) != expected_config)
        or {a.id for a in attachments} != {a.id for a in parsed_attachments})
    if stale or active_operation:
        # 已完成的 AI 调用仍保留；旧结论不能覆盖新字段、新配置或正在执行的审批操作。
        if approval.status == "PENDING" and not audit_result_is_current(db, approval, template):
            # 若已有更新的有效结论，过期任务无需再补发。否则按回写锁决定何时重审。
            approval.audit_requested = True
            requeue_requested_audit(db, approval)
        settle_ai_audit(approval, ai_task_id)
        db.commit()
        return {"approval_id": approval.id, "skipped": "stale_snapshot" if stale else "operation_in_progress"}
    current_attachments = {a.id: a for a in attachments}
    for parsed in parsed_attachments:
        if parsed.id in reference_pdf_ids:
            # 普通预审与补充识别可并行，跳过只读成员，避免旧快照覆盖最新票面。
            continue
        current_attachments[parsed.id].parsed_data = copy.deepcopy(parsed.parsed_data)
        current_attachments[parsed.id].invoice_id = parsed.invoice_id
    for item in observations.values():
        item["attachments"] = [current_attachments[a.id] for a in item["attachments"]]
    # 原子占用和规则结论一次提交，期间没有网络、文件解析或模型请求。
    current_invoice_ids = set(retained_invoice_ids)
    subject_companies = list(db.scalars(select(Company)))
    for key in sorted(observations):
        item = observations[key]
        fields = item["fields"]
        invoice = canonical_invoice(db, fields)
        # OCR 首次建档可能产生待确认字段。同一审批独占、尚未通过且已核对时，
        # 允许纠正本单的临时票面；历史台账及其他单据使用过的票面保持不可覆盖。
        previous_claims = db.scalars(select(InvoiceClaim).where(InvoiceClaim.invoice_id == invoice.id)).all()
        # 旧单拒绝并释放后，错误的临时建档不应永久污染同票后续申请。
        # 只允许修正本单临时占用或从未通过且已拒绝的旧单；已用、冻结和历史台账不覆盖。
        def editable_claim(claim):
            if claim.approval_id == approval.id:
                return claim.status in {"RESERVED", "RELEASED"}
            prior = db.get(Approval, claim.approval_id) if claim.approval_id else None
            return bool(claim.status == "RELEASED" and not claim.active_key and prior
                        and prior.status == "REJECTED" and not prior.approved_once)

        can_correct_own = (item["verified"] and not item["conflict"] and not invoice.historical and not approval.approved_once
                           and all(editable_claim(c) for c in previous_claims))
        if can_correct_own:
            before = {"buyer_name": invoice.buyer_name, "buyer_tax_id": invoice.buyer_tax_id,
                      "seller_name": invoice.seller_name, "issue_date": invoice.issue_date.isoformat(), "amount": str(invoice.amount)}
            invoice.buyer_name, invoice.buyer_tax_id = fields["buyer_name"], fields["buyer_tax_id"]
            invoice.seller_name, invoice.issue_date = fields.get("seller_name", ""), date.fromisoformat(fields["issue_date"])
            invoice.amount = Decimal(fields["amount"])
            after = {key: fields.get(key, "") for key in before}
            if before != after:
                log(db, "审批 Agent", "纠正未使用的临时票面", approval.serial_number,
                    {"invoice_id": invoice.id, "before": before, "after": after})
        current_invoice_ids.add(invoice.id)
        for attachment in item["attachments"]:
            attachment.invoice_id = invoice.id
        same_canonical = (invoice.buyer_tax_id == fields["buyer_tax_id"] and invoice.amount == Decimal(fields["amount"])
                          and invoice.issue_date.isoformat() == fields["issue_date"])
        safe = item["all_verified"] and not item["conflict"] and same_canonical
        label = invoice_label(fields["number"])
        company = db.scalar(select(Company).where(Company.tax_id == fields["buyer_tax_id"], Company.active.is_(True)))
        check("COMPANY", "购买方主体匹配", bool(company), f"{fields['buyer_name']} · {fields['buyer_tax_id']}" +
              ("，已匹配主体" if company else "，未匹配已配置主体"), reliable=safe,
              user_message=f"{label}的购买方为《{concise(fields['buyer_name'])}》（税号：{fields['buyer_tax_id']}），"
                           "未匹配到可报销的公司。请核对发票抬头和税号；如确认无误，请联系财务。")
        if company:
            if not payment:
                company_ids.add(company.id)
            if invoice.buyer_tax_id == fields["buyer_tax_id"]:
                invoice.company_id = company.id
            check("BUYER_NAME", "购买方名称核对", normalized_candidate("buyer_name", fields["buyer_name"]) in
                  [normalized_candidate("buyer_name", name) for name in [company.name, *company.aliases]],
                  f"票面购买方：{fields['buyer_name']}；票面税号对应主体：{company.name}", reliable=safe,
                  user_message=f"{label}的抬头为《{concise(fields['buyer_name'])}》，与税号对应的公司《{concise(company.name)}》不一致。"
                               "请核对并更换正确抬头的发票；如属于曾用名，请联系财务确认。")
        elif configurable(template.rules):
            check("BUYER_NAME", "购买方名称核对", True, "未匹配公司主体，名称核对不适用", result="SKIPPED")
        claim_result, claim_detail, declared_label = compare_declared_company(
            declaration, subject_companies, fields["buyer_name"], fields["buyer_tax_id"])
        check("CLAIM_COMPANY", "报销主体与票据一致性", claim_result in {"PASS", "SKIPPED"},
              claim_detail, reliable=safe, result=claim_result,
              user_message=f"{label}的购买方为《{concise(fields['buyer_name'], 60)}》，但本次选择的报销公司为《{concise(declared_label, 150)}》。"
                           "请更正报销公司，或更换对应公司的发票后重新提交。")
        differences = []
        for label, actual, recorded in (("税号", fields["buyer_tax_id"], invoice.buyer_tax_id),
                                         ("日期", fields["issue_date"], invoice.issue_date.isoformat()),
                                         ("金额", Decimal(fields["amount"]), invoice.amount)):
            if actual != recorded:
                differences.append(f"{label}：本次票面 {actual}，台账 {recorded}")
        check("CANONICAL", "历史票面信息一致性", same_canonical,
              f"发票 {fields['number']} 与统一票据库的税号、日期、金额核对" + ("；" + "；".join(differences) if differences else "，一致"))
        check("RELIABILITY", "关键字段可靠性", item["reliable"],
              f"发票 {fields['number']}：" + ("字段来自本地解析、发票 OCR 或人工确认" if item["reliable"] else
                  "字段来自文字、OCR 或模型；来源提示不影响通过，识别一致性单独核验"), "INFO")
        today = datetime.now(timezone(timedelta(hours=8))).date()
        check("DATE", "开票日期检查", date.fromisoformat(fields["issue_date"]) <= today,
              f"发票 {fields['number']}，开票日期 {fields['issue_date']}", reliable=safe,
              user_message=f"{label}的开票日期为 {fields['issue_date']}，晚于今天（{today.isoformat()}）。"
                           "请核对是否上传了错误票据或日期识别有误，确认后重新提交。")
        if approval.status not in {"REJECTED", "CANCELED", "DELETED", "REVERTED"}:
            claimed = reserve_invoice(db, invoice, approval)
            other = db.scalar(select(InvoiceClaim).where(InvoiceClaim.active_key == invoice.id)) if not claimed else None
            related = db.get(Approval, other.approval_id) if other and other.approval_id else None
            applicant = db.get(User, related.applicant_id or related.applicant) if related else None
            check("DUPLICATE", "跨主体重复报销检查", bool(claimed),
                  f"发票 {fields['number']} " + ("已由本单原子占用" if claimed else f"已被 {other.source_label if other else '冻结记录'} 占用"),
                  "BLOCK", other.approval_id if other else None, reliable=safe,
                  user_message=duplicate_message(fields["number"], other, related, applicant.name if applicant else "", business_type=approval.business_type))
    # 仅在未通过审批中释放已移除附件的占用。通过后发生变化须冻结核查。
    for claim in db.scalars(select(InvoiceClaim).where(InvoiceClaim.approval_id == approval.id)):
        if not unread_archive and claim.invoice_id not in current_invoice_ids and claim.active_key:
            if approval.approved_once:
                claim.status = "FROZEN"
                check("REMOVED_AFTER_APPROVAL", "通过后附件变更", False, "已通过审批移除了原发票，占用已冻结，需财务核查", "BLOCK")
            else:
                claim.status, claim.active_key = "RELEASED", None
    total = sum((Decimal(item["fields"]["amount"]) for item in observations.values()), Decimal("0"))
    tolerance = Decimal(str((template.rules or {}).get("amount_tolerance", "0.01")))
    # 业务证据完整性与金额来源分别判断。主体或历史台账差异不能把相同金额降为 UNKNOWN。
    complete = not unread_archive and bool(observations) and sum(len(item["attachments"]) for item in observations.values()) == invoice_attachment_count
    amount_complete = complete and all(item["amount_known"] and not item["amount_conflict"] for item in observations.values())
    complete = complete and not any(not item["passed"] and item["code"] in
        {"ATTACHMENTS", "RECOGNITION", "PARSE", "IDENTITY", "FIELD_CONFLICT", "CANONICAL"} for item in evidence)
    detail = (f"报销金额 ¥{approval.amount:,.2f}；去重后发票合计 ¥{total:,.2f}；"
              f"差额 ¥{abs(total-approval.amount):,.2f}；容差 ¥{tolerance:.2f}")
    if configurable(template.rules):
        check("AMOUNT_VALID", "报销金额有效性", approval.amount > 0, "报销金额必须大于零")
        amount_policy = builtin_policy(template.rules, "AMOUNT")
        enough = abs(total - approval.amount) <= tolerance if amount_policy.get("parameters", {}).get("comparison") == "EXACT" else total + tolerance >= approval.amount
        check("AMOUNT", "发票金额足额核对", enough and amount_complete, detail, reliable=amount_complete,
              result=None if amount_complete else "UNKNOWN",
              user_message=f"本次申请报销 {approval.amount:,.2f} 元，但发票合计只有 {total:,.2f} 元，还差 {max(approval.amount-total, Decimal('0')):,.2f} 元。"
                           "请补充足额发票，或将报销金额调整为实际可报销金额后重新提交。")
    else:
        check("AMOUNT", "报销金额与票据合计", amount_complete and approval.amount > 0
              and abs(total - approval.amount) <= tolerance, detail,
              result=None if amount_complete else "UNKNOWN")
    # 通用语义风险只处理通用费用疑点，文字标准分别映射策略，避免仅提示被重复升级。
    semantic_failed = analysis.get("risk_level") in {"medium", "high"}
    semantic_required = (builtin_policy(template.rules, "SEMANTIC").get("parameters", {}).get("require_completed", True)
                         if explicit_rules(template.rules) else template.rules.get("require_semantic", False))
    if configurable(template.rules):
        if semantic_failed:
            state = "FAIL"
        elif analysis["status"] == "COMPLETED":
            state = "PASS"
        else:
            state = "UNKNOWN" if semantic_required else "SKIPPED"
        check("SEMANTIC", "费用语义核对", state in {"PASS", "SKIPPED"}, analysis["summary"], result=state,
              reliable=complete and bool(analysis.get("findings")),
              user_message=model_message("费用用途", "；".join(analysis.get("findings") or [analysis["summary"]])))
    elif semantic_failed or (semantic_required and analysis["status"] != "COMPLETED"):
        check("SEMANTIC", "费用语义核对", False, analysis["summary"])
    results = {item["id"]: item for item in analysis.get("rule_results", [])}
    material_complete = (not unread_archive and bool(payment_materials) and all(item.get("complete") for item in payment_materials)
                         and not approval.payment_details.get("material_issues"))
    for rule in enabled_custom_rules(template.rules):
        item = results.get(rule["id"], {}) if analysis["status"] == "COMPLETED" else {}
        state = item.get("result", "UNKNOWN")
        check(f"CUSTOM:{rule['id']}", rule["name"], state == "PASS",
              item.get("reason") or analysis.get("summary", "模型未提供有效判定"),
              result=state, custom=rule, reliable=material_complete if payment else complete,
              user_message=model_message(rule["name"], item.get("reason", ""), custom=True))
    if explicit_rules(template.rules) and not has_approval_conditions(template.rules):
        evidence.append(no_conditions_evidence())
    analysis["rules_snapshot"] = copy.deepcopy(template.rules)
    # 整单人工判断仅绑定业务内容；逐项旧复核仍沿用原兼容边界。
    if human_review:
        apply_full_review(evidence)
        analysis["manual_review"] = previous_review
    elif legacy_review and previous_review.get("review_fingerprint") in review_fingerprints:
        apply_evidence_review(evidence, previous_review.get("confirmed_codes") or previous_review.get("reviewed_codes", ["SEMANTIC"]))
        analysis["manual_review"] = {**previous_review, "review_fingerprint": analysis["review_fingerprint"]}
    else:
        analysis.pop("manual_review", None)
    conclusion = audit_conclusion(evidence)
    policy_hash = hashlib.sha256(json.dumps(template.rules, sort_keys=True).encode()).hexdigest()[:8]
    audit = AuditRun(approval_id=approval.id, attachment_version=approval.attachment_version,
                     rule_version=f"{template.rules.get('version', '1.0')[:20]}-{policy_hash}", conclusion=conclusion,
                     evidence=evidence, model_analysis=analysis)
    db.add(audit)
    db.flush()
    approval.decision, approval.last_audit_id = conclusion, audit.id
    approval.audit_attachment_version = approval.attachment_version
    approval.audit_requested = False
    complete_ai_audit(approval, ai_task_id)
    approval.company_ids = sorted(company_ids)
    from .budgets import refresh_usage
    refresh_usage(db, approval)
    approval.version += 1
    log(db, "审批 Agent", "完成付款预审" if payment else "完成发票预审", approval.serial_number, {"conclusion": conclusion, "audit_id": audit.id})
    if (not suppress_actions and not human_review and not has_partial_contract and not unread_archive and template.mode == "AUTO"
            and (not explicit_rules(template.rules) or has_approval_conditions(template.rules))
            and approval.status == "PENDING" and approval.task_status == "PENDING"
            and approval.assignee == template.reviewer_open_id):
        rejected = rejection_items(evidence)
        kind = None
        if rejected:
            kind = "REJECT"
        elif conclusion == "PASS" and not analysis.get("manual_review"):
            kind = "APPROVE"
        if kind:
            comment = "全部规则通过，执行低风险自动同意"
            if rejected:
                comment = rejection_comment(rejected, approval.business_type)
            action = request_action(db, approval, kind, comment, approval.version,
                f"auto:{audit.id}:{kind}", "system-agent", "审批 Agent", source_audit_id=audit.id)
            enqueue(db, "ACTION", {"action_id": action.id}, f"action:{action.id}")
    db.commit()
    return {"approval_id": approval.id, "decision": conclusion, "invoice_count": len(observations)}


def request_action(db, approval, action: str, comment: str, expected_version: int,
                   idempotency_key: str, actor_id: str, actor_name: str, *, source_audit_id: str | None = None,
                   execution_source: str = "STANDARD") -> ApprovalAction:
    """统一创建回写；仅系统付款采集操作可绕过审核，身份、版本和唯一待办锁始终保留。"""
    previous = db.scalar(select(ApprovalAction).where(ApprovalAction.idempotency_key == idempotency_key))
    if previous:
        if (previous.approval_id, previous.action, previous.comment, previous.execution_source) != (approval.id, action, comment, execution_source):
            raise DomainError("同一幂等标识不能用于不同操作")
        return previous
    if approval.version != expected_version:
        raise DomainError("审批内容已更新，请刷新详情后重试")
    template = db.get(ApprovalTemplate, approval.template_code)
    if not template.active:
        raise DomainError("审批模板已停用，请先由管理员确认接入配置")
    collecting = payment_collection_enabled(template)
    passthrough = execution_source == "PAYMENT_AUTO_APPROVE"
    if execution_source not in {"STANDARD", "PAYMENT_AUTO_APPROVE"}:
        raise DomainError("未知的审批操作来源")
    if collecting != passthrough or (passthrough and (actor_id != "system-agent" or action != "APPROVE" or source_audit_id)):
        raise DomainError("付款仅采集模式只允许系统自动同意专用节点，请刷新当前处理策略")
    if actor_id == "system-agent" and not passthrough and not can_run_ai_approval(template):
        raise DomainError("模板 AI 审批已关闭，请由财务人工处理")
    if (approval.status != "PENDING" or approval.task_status != "PENDING" or not approval.task_id
            or approval.assignee != template.reviewer_open_id):
        raise DomainError("当前不是可操作的专用预审待办")
    if not passthrough:
        _check_partial_contract_action(db, approval, actor_id)
    if not passthrough and action == "APPROVE" and not template.ai_approval_enabled and not full_review(db, approval):
        raise DomainError("模板 AI 审批已关闭，请先完成整单人工复核")
    if not passthrough and action == "APPROVE" and (approval.decision != "PASS" or approval.audit_attachment_version != approval.attachment_version):
        raise DomainError("请先完成规则审核或整单人工复核后再同意")
    if not passthrough and action == "APPROVE" and not audit_config_matches(db, approval, template):
        raise DomainError("模型、节点提示词或审核规则已变化，请重新审核后再同意")
    if actor_id == "system-agent" and not passthrough:
        if full_review(db, approval):
            raise DomainError("当前已有整单人工复核，请由财务手动处理预审节点")
        if explicit_rules(template.rules) and not has_approval_conditions(template.rules):
            raise DomainError("当前没有启用的同意条件，请由财务人工处理")
        audit = db.get(AuditRun, source_audit_id) if source_audit_id else None
        if (template.mode != "AUTO" or not audit or audit.id != approval.last_audit_id
                or audit.approval_id != approval.id or approval.audit_requested
                or audit.attachment_version != approval.attachment_version or not audit_config_matches(db, approval, template)):
            raise DomainError("自动操作的审核快照已失效，请重新审核")
        if action == "REJECT" and (not configurable(template.rules) or not rejection_items(audit.evidence)):
            raise DomainError("当前没有证据可靠且明确命中的拒绝标准")
    operation_key = f"{approval.id}:{approval.task_id}"
    # 一个实例可流转到另一个配置节点，旧待办正在回写或对账时须等待其结果。
    existing = db.scalar(select(ApprovalAction.id).where(
        (ApprovalAction.task_operation_key == operation_key)
        | ((ApprovalAction.approval_id == approval.id)
           & ApprovalAction.status.in_(["REQUESTED", "EXECUTING", "UNKNOWN"]))
    ).limit(1))
    if existing:
        raise DomainError("该审批已有处理操作，请等待结果，回写结果未知时禁止重复提交")
    if action == "APPROVE" and actor_id != "system-agent" and full_review(db, approval):
        source_audit_id = approval.last_audit_id
    record = ApprovalAction(idempotency_key=idempotency_key, task_operation_key=operation_key, source_audit_id=source_audit_id,
        execution_source=execution_source,
        approval_id=approval.id, task_id=approval.task_id, actor_id=actor_id, actor_name=actor_name,
        feishu_identity=template.reviewer_open_id, action=action, comment=comment, expected_version=expected_version)
    db.add(record)
    db.flush()
    return record


def execute_action(db, action_id: str) -> dict:
    record = db.get(ApprovalAction, action_id)
    if not record:
        raise DomainError("处理操作不存在")
    if record.status == "FAILED":
        raise DomainError(record.error or "该操作已停止，请刷新审批后重新提交")
    if record.status == "SUCCEEDED":
        # 回写成功已经提交、但进程在同步及补发前退出时，重试仍须完成延迟重审。
        approval = db.get(Approval, record.approval_id)
        if approval and approval.audit_requested:
            approval = sync_instance(db, approval.id)
            begin_write(db)
            requeue_requested_audit(db, approval)
            db.commit()
        return {"action_id": record.id, "status": record.status}
    approval = db.get(Approval, record.approval_id)
    if db.get(ApprovalTemplate, approval.template_code).active:
        approval = sync_instance(db, approval.id)
    snapshot = gateway(db).get_instance(approval.id)
    begin_write(db)
    record = db.get(ApprovalAction, action_id)
    approval = db.get(Approval, record.approval_id)
    tasks = snapshot.get("task_list") or []
    original_task = next((task for task in tasks if str(task.get("id")) == record.task_id), {})
    desired = "APPROVED" if record.action == "APPROVE" else "REJECTED"
    reconciling = record.status in {"UNKNOWN", "EXECUTING"}
    if reconciling and not original_task.get("status"):
        # 流转后可能已出现其他节点的待办，原任务状态缺失时继续持有操作锁等待对账。
        raise RuntimeError("审批待办结果尚未确认")
    if reconciling and original_task.get("status") == desired:
        record.status = "SUCCEEDED"
    else:
        template = db.get(ApprovalTemplate, approval.template_code)
        try:
            if record.status == "FAILED":
                raise DomainError(record.error or "该操作已停止，请刷新审批后重新提交")
            auto = record.actor_id == "system-agent"
            passthrough = record.execution_source == "PAYMENT_AUTO_APPROVE"
            collecting = payment_collection_enabled(template)
            auto_allowed = (collecting if passthrough else can_run_ai_approval(template) and template.mode == "AUTO")
            if (reconciling and original_task.get("status") == "PENDING"
                    and (collecting != passthrough or (auto and not auto_allowed)
                         or approval.version != record.expected_version)):
                # 飞书仍显示待办时，已发出的请求可能尚未可见。只继续对账，禁止重发或释放操作锁。
                raise RuntimeError("旧自动回写仍在等待飞书确认")
            if not template.active or (auto and not auto_allowed):
                raise DomainError("模板已停用或 AI 自动处理已关闭")
            if collecting != passthrough or (passthrough and (not auto or record.action != "APPROVE" or record.source_audit_id)):
                raise DomainError("付款处理策略已变化，已停止旧操作")
            if record.actor_id != "system-agent":
                from .models import User
                actor = db.get(User, record.actor_id)
                if not actor or not actor.enabled or actor.role not in {"admin", "finance"}:
                    raise DomainError("操作者权限已变更，已停止待办回写")
            current_task, current_status = select_pre_review_task(tasks, template.node_ids, template.reviewer_open_id)
            if (snapshot.get("status") != "PENDING" or snapshot.get("reverted") is True
                    or current_status != "PENDING"
                    or str(current_task.get("id") or "") != record.task_id
                    or approval.version != record.expected_version or approval.status != "PENDING"
                    or approval.task_id != record.task_id or approval.task_status != "PENDING"
                    or approval.assignee != record.feishu_identity or template.reviewer_open_id != record.feishu_identity):
                if (auto and approval.status == "PENDING"
                        and not audit_result_is_current(db, approval, template)):
                    approval.audit_requested = True
                raise DomainError("飞书待办或附件版本已变化，已停止回写，请重新审核")
            if not passthrough:
                _check_partial_contract_action(db, approval, record.actor_id)
            if auto and not passthrough:
                if full_review(db, approval):
                    raise DomainError("已有有效整单人工复核，已停止自动操作，请由财务手动处理")
                # 自动拒绝与自动同意使用同样的有效快照边界，不能只保护同意路径。
                audit = db.get(AuditRun, record.source_audit_id) if record.source_audit_id else None
                if (not audit or audit.id != approval.last_audit_id or audit.approval_id != approval.id
                        or approval.audit_requested or audit.attachment_version != approval.attachment_version
                        or approval.audit_attachment_version != approval.attachment_version
                        or not audit_config_matches(db, approval, template)):
                    approval.decision, approval.audit_requested = "PENDING", True
                    raise DomainError("自动操作的审核配置或来源结论已变化，已停止回写并安排重新审核")
                if record.action == "REJECT" and (not configurable(template.rules) or not rejection_items(audit.evidence)):
                    raise DomainError("拒绝标准已失效，已停止自动拒绝")
            if record.action == "APPROVE" and not passthrough:
                manual = full_review(db, approval) if record.actor_id != "system-agent" else None
                if manual and record.source_audit_id != approval.last_audit_id:
                    raise DomainError("人工复核来源已变化，请刷新后重新提交同意")
                if not audit_config_matches(db, approval, template):
                    approval.decision, approval.audit_requested = "PENDING", True
                    raise DomainError("模型、节点提示词或审核规则已变化，已停止回写并安排重新审核")
                claims = db.scalars(select(InvoiceClaim).where(InvoiceClaim.approval_id == approval.id)).all()
                active_invoice_ids = set(db.scalars(select(Attachment.invoice_id).where(Attachment.approval_id == approval.id, Attachment.active.is_(True), Attachment.invoice_id.is_not(None))))
                owned = {c.invoice_id for c in claims if c.active_key and c.status in {"RESERVED", "USED"}}
                # 付款可以只附合同、报价等材料，无发票时不要求创建发票占用。
                # 只要存在发票，两类单据都必须逐票持有有效占用；报销仍要求至少一张发票。
                missing_required_invoice = approval.business_type != "PAYMENT" and not active_invoice_ids
                if (approval.decision != "PASS" or approval.attachment_version != approval.audit_attachment_version
                        or (not manual and (missing_required_invoice or active_invoice_ids != owned))):
                    raise DomainError("审核结论或发票占用已失效，已停止同意，请重新审核")
                if manual:
                    log(db, record.actor_name, "按整单人工复核提交同意", approval.serial_number,
                        {"action_id": record.id, "review_audit_id": manual["audit_id"],
                         "conflicting_claims": conflict_snapshot(db, approval),
                         "unowned_invoice_ids": sorted(active_invoice_ids - owned)})
            record.status = "EXECUTING"
            db.commit()
            try:
                remote = gateway(db)
                operation = (approval.template_code, approval.id, record.task_id,
                             record.feishu_identity, record.action, record.comment)
                if passthrough:
                    # 发送前最后检查策略、节点范围和版本；采集进度不参与版本计算。
                    db.rollback()
                    db.expire_all()
                    latest = db.get(Approval, operation[1])
                    latest_template = db.get(ApprovalTemplate, latest.template_code)
                    latest_record = db.get(ApprovalAction, action_id)
                    if (not latest_template.active or not payment_collection_enabled(latest_template)
                            or latest.version != latest_record.expected_version or latest.status != "PENDING"
                            or latest.task_id != latest_record.task_id or latest.task_status != "PENDING"
                            or latest.assignee != latest_template.reviewer_open_id
                            or latest_record.feishu_identity != latest_template.reviewer_open_id):
                        raise DomainError("付款仅采集策略或待办已变化，已停止回写")
                    db.rollback()
                elif auto:
                    try:
                        require_current_ai_snapshot(db, approval.id, record.expected_version)
                    except StaleSnapshotError as exc:
                        raise DomainError(str(exc)) from exc
                remote.operate(*operation)
                # SDK 接受请求后仍读取权威待办状态；超时或尚未可见都保留 UNKNOWN，
                # 后续先对账再重试，不能把本地入队或请求成功直接展示为已拒绝。
                try:
                    confirmed = remote.get_instance(approval.id)
                except DomainError as exc:
                    # 查询失败不能作为写入失败的依据，继续按原任务对账。
                    raise RuntimeError("审批待办结果尚未确认") from exc
                confirmed_task = next((task for task in (confirmed.get("task_list") or [])
                                       if str(task.get("id")) == record.task_id), None)
                if not confirmed_task or confirmed_task.get("status") != desired:
                    raise RuntimeError("审批待办结果尚未确认")
            except DomainError:
                raise
            except Exception:
                # 不能把网络超时当成写入失败。保留唯一操作锁，下一次先查询远端。
                begin_write(db)
                record = db.get(ApprovalAction, action_id)
                record.status = "UNKNOWN"
                record.error = "飞书回写结果未知，后台将先核对待办状态"
                db.commit()
                raise
            begin_write(db)
            record = db.get(ApprovalAction, action_id)
            approval = db.get(Approval, record.approval_id)
            record.status = "SUCCEEDED"
        except DomainError as exc:
            begin_write(db)
            record = db.get(ApprovalAction, action_id)
            approval = db.get(Approval, record.approval_id)
            record.status, record.error, record.task_operation_key = "FAILED", str(exc), None
            requeue_requested_audit(db, approval)
            db.commit()
            raise
    record.error = ""
    template = db.get(ApprovalTemplate, approval.template_code)
    if (template.active and matches_pre_review_node(original_task, template.node_ids)
            and task_open_id(original_task) == record.feishu_identity == template.reviewer_open_id):
        update_capability(template, "task_write")
    log(db, record.actor_name, "同意预审" if record.action == "APPROVE" else "拒绝预审", approval.serial_number,
        {"comment": record.comment, "feishu_identity": record.feishu_identity, "action_id": record.id,
         "source_audit_id": record.source_audit_id, "execution_source": record.execution_source})
    db.commit()
    if template.active:
        approval = sync_instance(db, approval.id)
        begin_write(db)
        requeue_requested_audit(db, approval)
        db.commit()
    return {"action_id": record.id, "status": record.status}


def archive_approval(db, instance_code: str, *, already_synced: bool = False) -> dict:
    """同步最终原件并更新归档清单；飞书回调可直接使用本轮已提交的同步快照。"""
    approval = db.get(Approval, instance_code) if already_synced else sync_instance(db, instance_code)
    if approval.status != "APPROVED":
        raise DomainError("只有最终审批通过的单据可以进入正常归档")
    # 归档沿用同一套审核有效性判定。已有结论时复用原件解析结果和占用信息。
    pre_reviewed = False
    if can_run_ai_approval(db.get(ApprovalTemplate, approval.template_code)):
        result = run_audit(db, instance_code, archive_synced=True)
        skipped = result.get("skipped")
        if skipped and skipped not in {"ai_approval_disabled", "audit_current"}:
            raise StaleSnapshotError("归档审核快照已变化，等待重新审核后重试")
        pre_reviewed = skipped != "ai_approval_disabled"
    begin_write(db)
    approval = db.get(Approval, instance_code)
    if approval.status != "APPROVED":
        raise DomainError("审批状态已变化，已停止归档")
    template = db.get(ApprovalTemplate, approval.template_code)
    ai_enabled = pre_reviewed and can_run_ai_approval(template)
    if ai_enabled and not audit_result_is_current(db, approval, template):
        raise StaleSnapshotError("归档审核快照已变化，等待重新审核后重试")
    attachments = list(db.scalars(select(Attachment).where(Attachment.approval_id == approval.id, Attachment.active.is_(True))))
    manual = full_review(db, approval, attachments=attachments)
    member_sources = archive_members(attachments)
    records = {row.attachment_id: row for row in db.scalars(
        select(ArchiveRecord).where(ArchiveRecord.approval_id == approval.id))}
    # 普通附件随审批主体归档；多主体无法唯一确定时仍保留在待确认主体目录。
    company_candidates = list(db.scalars(select(Company).where(Company.id.in_(approval.company_ids), Company.active.is_(True))))
    if approval.business_type == "PAYMENT" and approval.payment_details.get("company_id"):
        candidate = db.get(Company, approval.payment_details["company_id"])
        company_candidates = [candidate] if candidate and candidate.active else []
    attachment_company = company_candidates[0] if len(company_candidates) == 1 else None
    quarantined = 0
    for attachment in attachments:
        parsed = attachment.parsed_data or {}
        state = archive_state(attachment)
        member = member_sources.get(attachment.id)
        parent_state = archive_state(member[0]) if member else {}
        record = records.get(attachment.id)
        if state.get("mode") == "REFERENCE" or parent_state.get("mode") == "REFERENCE" or parent_state.get("status") == "FAILED":
            # 历史补齐只增加可查阅资料。原归档分类与既有业务凭据继续用于统计。
            quarantined += bool(record and record.status == "QUARANTINED")
            continue
        ordinary = approval.business_type != "PAYMENT" and parsed.get("parse_status") == "SKIPPED"
        invoice = db.get(Invoice, attachment.invoice_id) if attachment.invoice_id else None
        confirmed_fields = (parsed.get("fields") or {}) if not ai_enabled and parsed.get("confirmed") else {}
        material = parsed.get("material") or {}
        kind = parsed.get("material_type") or ("INVOICE" if ai_enabled or invoice or confirmed_fields else "UNKNOWN")
        company = db.get(Company, invoice.company_id) if invoice and invoice.company_id else None
        claim = db.scalar(select(InvoiceClaim).where(InvoiceClaim.invoice_id == invoice.id, InvoiceClaim.active_key == invoice.id)) if invoice else None
        normal = bool(invoice and company and company.active and claim and claim.approval_id == approval.id and claim.status == "USED")
        payment = approval.business_type == "PAYMENT"
        if payment and not invoice:
            company = db.get(Company, approval.payment_details.get("company_id")) if approval.payment_details.get("company_id") else None
            # 审批通过后归档的是完整原件。已按策略读取的合同与读取失败的材料分开判断。
            readable = (material.get("complete") or (kind == "CONTRACT" and material.get("page_scope"))
                        or (not ai_enabled and material.get("confirmation")))
            normal = bool(company and company.active and readable and kind not in {"UNKNOWN", "INVOICE"})
        elif confirmed_fields:
            tax_id = confirmed_fields.get("buyer_tax_id", "")
            company = db.scalar(select(Company).where(Company.tax_id == tax_id, Company.active.is_(True))) if tax_id else None
            normal = bool(normal and invoice.buyer_tax_id == tax_id and company)
        exception = bool(manual and not ordinary and not normal)
        if state:
            # 原包状态由展开完整性决定，成员的人工核对只作用于对应成员。
            company, normal = attachment_company, bool(attachment_company and state["status"] == "READY")
            exception = bool(manual and not normal)
        elif ordinary:
            company, normal = attachment_company, bool(attachment_company)
        elif manual and not normal and (ai_enabled or invoice or confirmed_fields or (payment and kind != "UNKNOWN")):
            # 人工例外保留票据原归属；归档清单标识异常，不伪造独占或税务核验结果。
            company = company if company and company.active else attachment_company
            normal = bool(company)
        prefix = company.tax_id if normal else "待确认主体"
        year_month = invoice.issue_date.strftime("%Y/%m") if invoice else now().strftime("%Y/%m")
        if payment:
            year_month = (approval.submitted_at or approval.created_at).strftime("%Y/%m")
        path = f"{prefix}/{year_month}/{safe_name(approval.expense_type)}/{safe_name(approval.serial_number)}/{attachment.filename}"
        if ordinary:
            path = f"{prefix}/{year_month}/{safe_name(approval.expense_type)}/{safe_name(approval.serial_number)}/普通附件/{attachment.filename}"
        if payment:
            path = "付款申请/" + path
        if not record:
            record = ArchiveRecord(approval_id=approval.id, attachment_id=attachment.id, archive_path=path)
            db.add(record)
        record.invoice_id, record.company_id = attachment.invoice_id, company.id if normal else None
        record.archive_path, record.status = path, "ARCHIVED" if normal else "QUARANTINED"
        record.manifest = {"filename": attachment.filename, "sha256": attachment.sha256, "size": attachment.size,
                           "approval_code": approval.template_code, "instance_code": approval.id,
                           "serial_number": approval.serial_number,
                           "invoice_number": confirmed_fields.get("number") or (invoice.number if invoice else None),
                           "buyer_tax_id": confirmed_fields.get("buyer_tax_id") or (invoice.buyer_tax_id if invoice else None),
                           "verification": "UNVERIFIED",
                           "audit_id": None if payment_collection_enabled(template) else approval.last_audit_id,
                           "payment_collection_only": payment_collection_enabled(template),
                           "payment_collection_task_id": approval.payment_collection_task_id if payment_collection_enabled(template) else None,
                           "attachment_version": approval.attachment_version, "business_type": approval.business_type,
                           "material_type": kind,
                           "invoice_parse_skipped": ordinary, "manual_exception": exception,
                           "manual_review_audit_id": manual["audit_id"] if manual else None,
                           "conflicting_claims": conflict_snapshot(db, approval) if exception else []}
        if member:
            record.manifest = {**record.manifest, "archive_source": {
                "attachment_id": member[0].id, "filename": member[0].filename, "path": member[1]}}
        quarantined += not normal
    active_ids = {attachment.id for attachment in attachments}
    for old_record in db.scalars(select(ArchiveRecord).where(ArchiveRecord.approval_id == approval.id)):
        if old_record.attachment_id not in active_ids and old_record.status != "REVOKED":
            old_record.status = "SUPERSEDED"
    unavailable = bool(approval.payment_details.get("material_issues")) if approval.business_type == "PAYMENT" else False
    approval.archive_status = "PARTIAL" if quarantined or unavailable else "ARCHIVED" if attachments else "NONE" if approval.business_type == "PAYMENT" else "FAILED"
    log(db, "归档服务", "完成审批附件归档", approval.serial_number,
        {"files": len(attachments), "quarantined": quarantined})
    db.commit()
    return {"files": len(attachments), "quarantined": quarantined}


def handle_event(db, event_id: str) -> dict:
    inbox = db.get(EventInbox, event_id)
    event = inbox.payload.get("event", {})
    instance_code = event.get("instance_code")
    template = db.get(ApprovalTemplate, event.get("approval_code", ""))
    if not template or not template.active or not instance_code:
        begin_write(db)
        inbox = db.get(EventInbox, event_id)
        inbox.status = "IGNORED"
        db.commit()
        return {"ignored": True}
    approval = sync_instance(db, instance_code, template.code)
    if approval.status == "APPROVED":
        result = archive_approval(db, instance_code, already_synced=True)
    else:
        begin_write(db)
        approval = db.get(Approval, instance_code)
        enqueue_automatic_audit(db, approval)
        result = {"approval_id": approval.id, "status": approval.status}
    begin_write(db)
    inbox = db.get(EventInbox, event_id)
    inbox.status = "PROCESSED"
    db.commit()
    return result


def sync_all(db) -> dict:
    remote, count = FeishuGateway(), 0
    template_codes = list(db.scalars(select(ApprovalTemplate.code).where(ApprovalTemplate.active.is_(True))))
    for template_code in template_codes:
        template = db.get(ApprovalTemplate, template_code)
        start = (template.sync_cursor or template.created_at) - timedelta(minutes=5)
        end = min(now(), start + timedelta(days=1))
        db.rollback()
        codes = remote.list_instances(template_code, int(start.replace(tzinfo=timezone.utc).timestamp()*1000),
                                      int(end.replace(tzinfo=timezone.utc).timestamp()*1000))
        # 发起时间筛选无法覆盖旧审批的新终态；按上次读取时间补偿存量实例。
        for statuses in (("PENDING",), ("APPROVED", "REVERTED")):
            codes.extend(db.scalars(select(Approval.id).where(Approval.template_code == template_code,
                Approval.status.in_(statuses)).order_by(Approval.last_synced_at).limit(100)).all())
        db.rollback()
        for code in dict.fromkeys(codes):
            approval = sync_instance(db, code, template_code)
            begin_write(db)
            if approval.status == "APPROVED":
                enqueue(db, "ARCHIVE", {"instance_code": code}, f"compensation:archive:{code}:{approval.attachment_version}")
            else:
                enqueue_automatic_audit(db, approval)
            # 每个实例独立提交，不能把前一个实例的待投递任务带入下一次下载。
            db.commit()
            count += 1
        begin_write(db)
        template = db.get(ApprovalTemplate, template_code)
        template.sync_cursor = end
        db.commit()
    return {"synced": count}


def refresh_original(db, instance_code: str) -> dict:
    approval = sync_instance(db, instance_code)
    begin_write(db)
    enqueue_automatic_audit(db, approval)
    db.commit()
    return {"approval_id": approval.id, "status": approval.status}


def feishu_link(instance_code: str) -> str:
    path = quote(f"pc/pages/in-process/index?instanceId={instance_code}", safe="")
    return f"https://applink.feishu.cn/client/mini_program/open?appId=cli_9cb844403dbb9108&path={path}"
