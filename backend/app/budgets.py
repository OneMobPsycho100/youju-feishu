"""部门预算共用服务：页面、导出和助手读取相同用量事实，预算提醒不参与审批资格判定。"""
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
import io
import json

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError

from . import budget_schemas as s
from .company_subjects import _form_fields, read_company_declaration
from .db import begin_write
from .models import Approval, ApprovalTemplate, Budget, BudgetOperation, BudgetUsage, Company, Job, User, now
from .payment_service import decoded

OFFSET = timedelta(hours=8)
ZERO = Decimal("0.00")
ACTIVE_STATES = ("USED", "RESERVED", "FROZEN")
EXPORT_LIMIT = 50000
USAGE_FIELDS = ("company_id", "department", "expense_type", "submitted_at", "amount", "state",
                "attribution_pending", "attribution_reason", "attribution_source", "source_fingerprint",
                "approved_snapshot", "freeze_fingerprint", "released_fingerprint", "historical_review")
BUDGET_FIELDS = ("id", "name", "company_id", "department", "expense_type", "period_type", "start_date",
                 "end_date", "amount", "warning_threshold", "notes", "archived", "version", "created_at", "updated_at")


def json_safe(value):
    """金额只能转十进制文本，不能经 JSON 编码器隐式转为浮点数。"""
    return json.loads(json.dumps(value, ensure_ascii=False, default=str, sort_keys=True))


def fingerprint(value):
    return sha256(json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def today():
    return (now() + OFFSET).date()


def company(db, company_id, *, active=False):
    row = db.get(Company, company_id)
    if not row or active and not row.active:
        raise HTTPException(422, "公司主体不存在或已停用，请重新选择")
    return row


def get_budget(db, budget_id):
    row = db.get(Budget, budget_id)
    if row is None:
        raise HTTPException(404, "预算不存在")
    return row


def department_from_form(form, mapping):
    """只读表单业务部门，不回退申请人的组织部门；多选或仅有部门编号时交人工确认。"""
    fields = list(_form_fields(form))
    field_id = mapping.get("department", "")
    candidates = [item for item in fields if item.get("id") == field_id] if field_id else [
        item for item in fields if str(item.get("name", "")).strip() in {"申请部门", "所属部门", "部门"}]

    def names(value):
        value = decoded(value)
        if isinstance(value, list):
            return [name for item in value for name in names(item)] or [""]
        if isinstance(value, dict):
            for key in ("name", "text", "label", "department_name", "value"):
                if value.get(key):
                    return names(value[key])
            return [""]
        text = value.strip() if isinstance(value, str) else ""
        return [text] if text and not text.startswith("od-") else [""]

    parts = [label for item in candidates for label in names(item.get("value"))]
    labels = sorted({label for label in parts if label})
    source = {"labels": labels, "mapping": field_id,
              "fields": [{"id": item.get("id"), "value": decoded(item.get("value"))}
                         for item in sorted(candidates, key=lambda item: str(item.get("id", "")))]}
    if len({item.get("id") for item in candidates}) > 1 or len(labels) > 1:
        return "", "表单部门存在多个选择，请确认整单预算部门", source
    if not labels or any(not label for label in parts) or len(labels[0]) > 100:
        return "", "表单未提供可识别的申请部门，请配置部门映射或人工确认", source
    return labels[0], "", source


def automatic_attribution(db, approval, companies=None):
    template = db.get(ApprovalTemplate, approval.template_code)
    mapping = (template.field_mapping or {}) if template else {}
    declaration = read_company_declaration(approval.raw_form or [], mapping)
    department, department_error, department_source = department_from_form(approval.raw_form or [], mapping)
    companies = companies if companies is not None else list(db.scalars(select(Company)))
    errors, matches = list(declaration.issues), set()
    if declaration.present:
        for name in declaration.names:
            candidates = {item.id for item in companies if name.strip() in {item.name.strip(), *(str(alias).strip() for alias in item.aliases)}}
            if len(candidates) != 1:
                errors.append("表单申报主体名称无法唯一匹配公司")
            matches.update(candidates)
        for tax_id in declaration.tax_ids:
            candidates = {item.id for item in companies if item.tax_id == tax_id}
            if len(candidates) != 1:
                errors.append("表单申报税号无法匹配公司")
            matches.update(candidates)
    else:
        matches = {value for value in approval.company_ids or [] if any(item.id == value for item in companies)}
    if len(matches) != 1:
        errors.append("主体缺失或涉及多个公司，请确认整单预算主体")
    if department_error:
        errors.append(department_error)
    source = {"names": declaration.names, "tax_ids": declaration.tax_ids, "issues": declaration.issues,
              "present": declaration.present, "departments": department_source,
              "invoice_companies": sorted(matches) if not declaration.present else []}
    return {"company_id": next(iter(matches)) if len(matches) == 1 and not declaration.issues else None,
            "department": department, "attribution_pending": bool(errors),
            "attribution_reason": "；".join(dict.fromkeys(errors))[:500], "source_fingerprint": fingerprint(source)}


def usage_snapshot(row):
    return json_safe({"approval_id": row.approval_id, "version": row.version,
                      **{key: getattr(row, key) for key in USAGE_FIELDS}})


def refresh_usage(db, approval, *, companies=None):
    """调用方必须持有短写事务；与权威审批快照一起提交，不触发网络、审核或审批回写。

    通过后的冻结沿用通过快照。释放绑定业务指纹，重复事件和审核版本递增都不能恢复同一冻结。
    重新通过会撤销旧释放依据；金额、提交日期和归属来源变化会产生新的核对依据。
    """
    if approval.business_type != "REIMBURSEMENT":
        return None
    row = db.get(BudgetUsage, approval.id)
    if row is None:
        row = BudgetUsage(approval_id=approval.id)
        db.add(row)
        db.flush()
    before = json_safe({key: getattr(row, key) for key in USAGE_FIELDS})
    attribution = automatic_attribution(db, approval, companies)
    changed_source = bool(row.source_fingerprint and row.source_fingerprint != attribution["source_fingerprint"])
    source = row.attribution_source
    if source == "MANUAL" and not changed_source:
        attribution.update(company_id=row.company_id, department=row.department, attribution_pending=False, attribution_reason="")
    elif source == "REVIEW" or source == "MANUAL" and changed_source:
        source = "REVIEW"
        attribution.update(company_id=row.company_id, department=row.department, attribution_pending=True,
                           attribution_reason="报销归属来源已变化，请重新确认整单预算归属")
    else:
        source = "AUTO"
    current = {**attribution, "expense_type": (approval.expense_type or "其他费用").strip() or "其他费用",
               "submitted_at": approval.submitted_at, "amount": approval.amount, "attribution_source": source}
    if not approval.submitted_at:
        current["attribution_pending"] = True
        current["attribution_reason"] = (current["attribution_reason"] + "；申请提交时间待补齐").strip("；")[:500]
    ever_approved = approval.approved_once or bool(row.approved_snapshot) or approval.status == "REVERTED"
    if approval.status == "APPROVED":
        row.state, row.historical_review = "USED", False
        # 新表单暂时无法归属时仍保留最后一次完整通过快照，之后撤销不能丢失原冻结依据。
        if not current["attribution_pending"] or not row.approved_snapshot or row.approved_snapshot.get("attribution_pending"):
            row.approved_snapshot = json_safe(current)
        row.freeze_fingerprint = row.released_fingerprint = ""
        effective = current
    elif approval.status == "PENDING":
        row.state = "RESERVED"
        row.freeze_fingerprint = row.released_fingerprint = ""
        effective = current
    elif ever_approved:
        if not row.approved_snapshot:
            # 历史原单缺少通过快照时固定首次建账依据，并明确标记，不能随之后的表单变化移动冻结金额。
            row.approved_snapshot = json_safe({**current, "_historical": True})
        effective = dict(row.approved_snapshot)
        row.historical_review = bool(effective.get("_historical")) or bool(changed_source)
        if isinstance(effective.get("submitted_at"), str):
            effective["submitted_at"] = datetime.fromisoformat(effective["submitted_at"])
        effective["amount"] = Decimal(str(effective["amount"]))
        # 元数据补齐允许填入原快照缺失的提交时间；不挪动原有通过日期、部门或金额。
        if not effective.get("submitted_at") and approval.submitted_at:
            effective["submitted_at"] = approval.submitted_at
            effective["attribution_reason"] = str(effective.get("attribution_reason", "")).replace("申请提交时间待补齐", "").strip("；")
        if effective.get("company_id") and effective.get("department") and effective.get("submitted_at") and not effective.get("attribution_reason"):
            effective["attribution_pending"] = False
        effective["source_fingerprint"] = attribution["source_fingerprint"]
        if source == "REVIEW":
            row.historical_review = True
            effective["attribution_source"] = "REVIEW"
        row.freeze_fingerprint = fingerprint({"frozen": effective, "current": current,
                                               "approved_at": approval.approved_at})
        row.state = "RELEASED" if row.released_fingerprint == row.freeze_fingerprint else "FROZEN"
        if row.state == "RELEASED":
            row.historical_review = False
    else:
        row.state = "RELEASED"
        effective = current
    for key in ("company_id", "department", "expense_type", "submitted_at", "amount", "attribution_pending",
                "attribution_reason", "attribution_source", "source_fingerprint"):
        setattr(row, key, effective[key])
    after = json_safe({key: getattr(row, key) for key in USAGE_FIELDS})
    if before != after:
        row.version += 1
    return row


def backfill_usage(db, instance_code):
    begin_write(db)
    approval = db.get(Approval, instance_code)
    if approval:
        refresh_usage(db, approval)
    db.commit()
    return {"approval_id": instance_code, "completed": bool(approval)}


def schedule_backfill(db, enqueue):
    """本地历史建账独立于飞书连接状态；已失败任务由现有任务中心重试。"""
    begin_write(db)
    exists = select(BudgetUsage.approval_id).where(BudgetUsage.approval_id == Approval.id).exists()
    scheduled = select(Job.id).where(Job.dedupe_key == "budget-usage:" + Approval.id).exists()
    ids = list(db.scalars(select(Approval.id).where(Approval.business_type == "REIMBURSEMENT", ~exists, ~scheduled)
                         .order_by(Approval.created_at, Approval.id).limit(20)))
    for approval_id in ids:
        enqueue(db, "BUDGET_USAGE_BACKFILL", {"instance_code": approval_id}, "budget-usage:" + approval_id)
    db.commit()


def scope_conditions(budget, *, ready=True):
    start = datetime.combine(budget.start_date, time.min) - OFFSET
    end = datetime.combine(budget.end_date + timedelta(days=1), time.min) - OFFSET
    values = [BudgetUsage.company_id == budget.company_id, BudgetUsage.department == budget.department,
              BudgetUsage.submitted_at >= start, BudgetUsage.submitted_at < end]
    if budget.expense_type:
        values.append(BudgetUsage.expense_type == budget.expense_type)
    if ready:
        values.append(BudgetUsage.attribution_pending.is_(False))
    return values


def completeness(db, company_id=""):
    filters = [BudgetUsage.state.in_(ACTIVE_STATES)]
    if company_id:
        filters.append(or_(BudgetUsage.company_id == company_id, BudgetUsage.company_id.is_(None)))
    rows = db.execute(select(BudgetUsage.amount, BudgetUsage.attribution_pending, BudgetUsage.historical_review).where(*filters))
    pending, amount, historical = 0, ZERO, 0
    with localcontext() as ctx:
        ctx.prec = 50
        for value, unresolved, review in rows:
            if unresolved:
                pending, amount = pending + 1, amount + value
            historical += int(review)
    remaining = db.scalar(select(func.count()).select_from(Approval).where(Approval.business_type == "REIMBURSEMENT",
        ~select(BudgetUsage.approval_id).where(BudgetUsage.approval_id == Approval.id).exists()))
    return {"pending_count": pending, "pending_amount": amount, "backfill_remaining": remaining,
            "historical_review_count": historical}


def metrics(db, budget):
    amounts = {key: ZERO for key in ACTIVE_STATES}
    count = 0
    rows = db.execute(select(BudgetUsage.state, BudgetUsage.amount).where(*scope_conditions(budget), BudgetUsage.state.in_(ACTIVE_STATES)).execution_options(yield_per=1000))
    with localcontext() as ctx:
        ctx.prec = 50
        for state, amount in rows:
            amounts[state] += amount
            count += 1
        occupied = sum(amounts.values(), ZERO)
        available = budget.amount - occupied
        percentage = (occupied * 100 / budget.amount).quantize(Decimal("0.01"))
    status = "EXCEEDED" if available < 0 else "EXHAUSTED" if available == 0 else "WARNING" if occupied * 100 >= budget.amount * budget.warning_threshold else "NORMAL"
    return {"used_amount": amounts["USED"], "reserved_amount": amounts["RESERVED"], "frozen_amount": amounts["FROZEN"],
            "occupied_amount": occupied, "available_amount": available, "exceeded_amount": max(-available, ZERO),
            "usage_percentage": percentage, "budget_status": status, "usage_count": count}


def budget_dump(db, budget, names=None):
    name = names.get(budget.company_id, "") if names is not None else company(db, budget.company_id).name
    return {**{key: getattr(budget, key) for key in BUDGET_FIELDS}, "company_name": name, **metrics(db, budget)}


def list_budgets(db, *, company_id="", department="", expense_type="", period_type="", period_scope="current",
                 budget_status="", archived="active", start_date=None, end_date=None, q="", page=1, page_size=20):
    if company_id:
        company(db, company_id)
    statement = select(Budget)
    for column, value in ((Budget.company_id, company_id), (Budget.department, department),
                          (Budget.expense_type, expense_type), (Budget.period_type, period_type)):
        if value:
            statement = statement.where(column == value)
    if archived != "all":
        statement = statement.where(Budget.archived.is_(archived == "archived"))
    if period_scope == "current":
        statement = statement.where(Budget.start_date <= today(), Budget.end_date >= today())
    if start_date:
        statement = statement.where(Budget.end_date >= start_date)
    if end_date:
        statement = statement.where(Budget.start_date <= end_date)
    if q:
        statement = statement.where(Budget.name.contains(q, autoescape=True))
    names = dict(db.execute(select(Company.id, Company.name)).all())
    # 各预算范围可能重叠，因此只汇总条目数，绝不把不同预算的额度或用量相加。
    items = [budget_dump(db, row, names) for row in db.scalars(statement.order_by(Budget.start_date.desc(), Budget.created_at.desc(), Budget.id))]
    if budget_status:
        items = [item for item in items if item["budget_status"] == budget_status]
    start = (page - 1) * page_size
    return {"items": items[start:start + page_size], "total": len(items), "page": page, "page_size": page_size,
            "warning_count": sum(item["budget_status"] in {"WARNING", "EXHAUSTED"} for item in items),
            "exceeded_count": sum(item["budget_status"] == "EXCEEDED" for item in items), "completeness": completeness(db, company_id)}


def options(db, company_id=""):
    if company_id:
        company(db, company_id)
    budgets = select(Budget.department, Budget.expense_type)
    usages = select(BudgetUsage.department, BudgetUsage.expense_type)
    if company_id:
        budgets, usages = budgets.where(Budget.company_id == company_id), usages.where(BudgetUsage.company_id == company_id)
    rows = [*db.execute(budgets).all(), *db.execute(usages).all()]
    return {"departments": sorted({row.department for row in rows if row.department}),
            "expense_types": sorted({row.expense_type for row in rows if row.expense_type})}


def usage_dump(db, row, approval, names=None, companies=None):
    names = names if names is not None else dict(db.execute(select(User.id, User.name)).all())
    companies = companies if companies is not None else dict(db.execute(select(Company.id, Company.name)).all())
    identity = approval.applicant_id or "legacy:" + approval.applicant
    return {"approval_id": row.approval_id, "serial_number": approval.serial_number, "title": approval.title,
            "applicant_id": identity, "applicant": names.get(identity) or approval.applicant,
            "company_name": companies.get(row.company_id, ""), "version": row.version,
            **{key: getattr(row, key) for key in ("company_id", "department", "expense_type", "submitted_at", "amount", "state",
                "attribution_pending", "attribution_reason", "attribution_source", "historical_review")}}


def usage_statement(db, *, budget_id="", company_id="", state="", pending_only=False, applicant_id="", expense_type="", department=""):
    statement = select(BudgetUsage, Approval).join(Approval, Approval.id == BudgetUsage.approval_id)
    if budget_id:
        statement = statement.where(*scope_conditions(get_budget(db, budget_id)))
    elif company_id:
        company(db, company_id)
        statement = statement.where(or_(BudgetUsage.company_id == company_id, BudgetUsage.company_id.is_(None)))
    if state:
        statement = statement.where(BudgetUsage.state == state)
    if pending_only:
        statement = statement.where(BudgetUsage.attribution_pending.is_(True), BudgetUsage.state.in_(ACTIVE_STATES))
    if applicant_id:
        from .reimbursement_statistics import applicant_key
        statement = statement.where(applicant_key() == applicant_id)
    if expense_type:
        statement = statement.where(BudgetUsage.expense_type == expense_type)
    if department:
        statement = statement.where(BudgetUsage.department == department)
    return statement.order_by(BudgetUsage.submitted_at.desc(), BudgetUsage.approval_id)


def usage_details(db, *, page=1, page_size=20, **filters):
    statement = usage_statement(db, **filters)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    names, companies = dict(db.execute(select(User.id, User.name)).all()), dict(db.execute(select(Company.id, Company.name)).all())
    items = [usage_dump(db, row, approval, names, companies) for row, approval in db.execute(statement.offset((page - 1) * page_size).limit(page_size))]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def budget_detail(db, budget_id):
    budget = get_budget(db, budget_id)
    names = dict(db.execute(select(User.id, User.name)).all())
    people, categories, points = {}, {}, {}
    daily = (budget.end_date - budget.start_date).days < 90
    statement = usage_statement(db, budget_id=budget_id).where(BudgetUsage.state.in_(ACTIVE_STATES))
    with localcontext() as ctx:
        ctx.prec = 50
        for row, approval in db.execute(statement.execution_options(yield_per=1000)):
            key = approval.applicant_id or "legacy:" + approval.applicant
            person = people.setdefault(key, {"key": key, "name": names.get(key) or approval.applicant, "amount": ZERO, "count": 0})
            category = categories.setdefault(row.expense_type, {"key": row.expense_type, "name": row.expense_type, "amount": ZERO, "count": 0})
            for group in (person, category):
                group["amount"] += row.amount
                group["count"] += 1
            day = (row.submitted_at + OFFSET).date()
            point_key = day.isoformat() if daily else day.strftime("%Y-%m")
            points[point_key] = points.get(point_key, ZERO) + row.amount
    cursor, trend = budget.start_date, []
    while cursor <= budget.end_date:
        key = cursor.isoformat() if daily else cursor.strftime("%Y-%m")
        trend.append({"date": key, "amount": points.get(key, ZERO)})
        cursor = cursor + timedelta(days=1) if daily else date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    sort_groups = lambda groups: sorted(groups.values(), key=lambda item: (-item["amount"], item["key"]))
    return {**budget_dump(db, budget), "completeness": completeness(db, budget.company_id),
            "applicants": sort_groups(people), "expense_types": sort_groups(categories), "trend": trend}


def matching_budget_ids(db, snapshot):
    if not snapshot.get("company_id") or not snapshot.get("department") or not snapshot.get("submitted_at"):
        return []
    submitted = snapshot["submitted_at"]
    if isinstance(submitted, str):
        submitted = datetime.fromisoformat(submitted)
    day = (submitted + OFFSET).date()
    return list(db.scalars(select(Budget.id).where(Budget.company_id == snapshot["company_id"],
        Budget.department == snapshot["department"], Budget.start_date <= day, Budget.end_date >= day,
        or_(Budget.expense_type == "", Budget.expense_type == snapshot.get("expense_type", "其他费用")))))


def record_operation(db, user, action, before, after, *, budget_id=None, approval_id=None, reason="", request_id=None, request_fingerprint=""):
    from .services import log
    before, after = json_safe(before), json_safe(after)
    if approval_id:
        before["budget_ids"], after["budget_ids"] = matching_budget_ids(db, before), matching_budget_ids(db, after)
    row = BudgetOperation(budget_id=budget_id, approval_id=approval_id, action=action, actor_id=user.id, actor_name=user.name,
        reason=reason, before=before, after=after, request_id=request_id, request_fingerprint=request_fingerprint)
    db.add(row)
    log(db, user.name, "预算操作：" + {"CREATE": "创建", "EDIT": "维护", "ADJUST": "调整额度", "ATTRIBUTION": "确认报销归属", "RELEASE": "释放冻结"}[action],
        budget_id or approval_id, {"reason": reason, "before": before, "after": after})
    return row


def check_version(row, version):
    if row.version != version:
        raise HTTPException(409, "预算或报销用量已变化，请刷新后重新核对")


def idempotent_operation(db, data, user, action, target):
    digest = fingerprint({"actor": user.id, "action": action, "target": target, "input": data.model_dump(mode="json")})
    previous = db.scalar(select(BudgetOperation).where(BudgetOperation.request_id == data.request_id))
    if previous and previous.request_fingerprint != digest:
        raise HTTPException(409, "操作幂等标识已用于其他请求，请刷新后重试")
    return previous, digest


def create_budget(db, data, user):
    begin_write(db)
    company(db, data.company_id, active=True)
    row = Budget(**data.model_dump())
    db.add(row)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "相同主体、部门、类别和起止日期的预算已存在，请编辑或恢复原预算")
    record_operation(db, user, "CREATE", {}, {key: getattr(row, key) for key in BUDGET_FIELDS}, budget_id=row.id)
    db.commit()
    return budget_dump(db, row)


def edit_budget(db, budget_id, data, user):
    begin_write(db)
    row = get_budget(db, budget_id)
    check_version(row, data.expected_version)
    before = {key: getattr(row, key) for key in BUDGET_FIELDS}
    for key, value in data.model_dump(exclude={"expected_version"}, exclude_none=True).items():
        setattr(row, key, value)
    row.version += 1
    db.flush()
    record_operation(db, user, "EDIT", before, {key: getattr(row, key) for key in BUDGET_FIELDS}, budget_id=row.id)
    db.commit()
    return budget_dump(db, row)


def adjust_budget(db, budget_id, data, user):
    begin_write(db)
    previous, digest = idempotent_operation(db, data, user, "ADJUST", budget_id)
    row = get_budget(db, budget_id)
    if previous:
        return budget_dump(db, row)
    check_version(row, data.expected_version)
    before = {key: getattr(row, key) for key in BUDGET_FIELDS}
    row.amount, row.version = data.amount, row.version + 1
    db.flush()
    record_operation(db, user, "ADJUST", before, {key: getattr(row, key) for key in BUDGET_FIELDS}, budget_id=row.id,
                     reason=data.reason, request_id=data.request_id, request_fingerprint=digest)
    db.commit()
    return budget_dump(db, row)


def get_usage(db, approval_id):
    approval, usage = db.get(Approval, approval_id), db.get(BudgetUsage, approval_id)
    if not approval or approval.business_type != "REIMBURSEMENT":
        raise HTTPException(404, "报销审批不存在")
    if usage is None:
        raise HTTPException(409, "报销预算尚未完成历史建账，请稍后刷新")
    return approval, usage


def confirm_attribution(db, approval_id, data, user):
    begin_write(db)
    previous, digest = idempotent_operation(db, data, user, "ATTRIBUTION", approval_id)
    approval, row = get_usage(db, approval_id)
    if previous:
        return usage_dump(db, row, approval)
    check_version(row, data.expected_version)
    company(db, data.company_id, active=True)
    before, version = usage_snapshot(row), row.version
    row.company_id, row.department, row.attribution_source = data.company_id, data.department, "MANUAL"
    row.source_fingerprint = automatic_attribution(db, approval)["source_fingerprint"]
    if row.approved_snapshot and row.state in {"FROZEN", "RELEASED"}:
        row.approved_snapshot = {**row.approved_snapshot, "company_id": data.company_id, "department": data.department,
            "attribution_source": "MANUAL", "attribution_pending": not bool(approval.submitted_at),
            "attribution_reason": "" if approval.submitted_at else "申请提交时间待补齐",
            "source_fingerprint": row.source_fingerprint}
    refresh_usage(db, approval)
    row.version = max(row.version, version + 1)
    record_operation(db, user, "ATTRIBUTION", before, usage_snapshot(row), approval_id=approval_id,
        reason=data.reason, request_id=data.request_id, request_fingerprint=digest)
    db.commit()
    return usage_dump(db, row, approval)


def release_frozen(db, approval_id, data, user):
    begin_write(db)
    previous, digest = idempotent_operation(db, data, user, "RELEASE", approval_id)
    approval, row = get_usage(db, approval_id)
    if previous:
        return usage_dump(db, row, approval)
    check_version(row, data.expected_version)
    if row.state != "FROZEN":
        raise HTTPException(409, "当前报销没有可释放的冻结预算，请刷新后核对")
    before = usage_snapshot(row)
    row.state, row.released_fingerprint = "RELEASED", row.freeze_fingerprint
    row.historical_review = False
    row.version += 1
    record_operation(db, user, "RELEASE", before, usage_snapshot(row), approval_id=approval_id,
        reason=data.reason, request_id=data.request_id, request_fingerprint=digest)
    db.commit()
    return usage_dump(db, row, approval)


def operation_statement(db, budget_id):
    get_budget(db, budget_id)
    before = func.json_each(BudgetOperation.before, "$.budget_ids").table_valued("value").alias("before_budgets")
    after = func.json_each(BudgetOperation.after, "$.budget_ids").table_valued("value").alias("after_budgets")
    current_ids = select(BudgetUsage.approval_id).where(*scope_conditions(get_budget(db, budget_id), ready=False))
    return select(BudgetOperation).where(or_(BudgetOperation.budget_id == budget_id,
        BudgetOperation.approval_id.in_(current_ids),
        select(1).select_from(before).where(before.c.value == budget_id).correlate(BudgetOperation).exists(),
        select(1).select_from(after).where(after.c.value == budget_id).correlate(BudgetOperation).exists()))


def operations(db, budget_id, page=1, page_size=20):
    statement = operation_statement(db, budget_id)
    total = db.scalar(select(func.count()).select_from(statement.subquery()))
    items = list(db.scalars(statement.order_by(BudgetOperation.created_at.desc(), BudgetOperation.id).offset((page - 1) * page_size).limit(page_size)))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


def approval_context(db, approval):
    if approval.business_type != "REIMBURSEMENT":
        return None
    row = db.get(BudgetUsage, approval.id)
    if row is None:
        return {"usage": None, "budgets": [], "message": "预算用量正在后台建账，请稍后刷新"}
    ids = matching_budget_ids(db, usage_snapshot(row)) if not row.attribution_pending else []
    budgets = [budget_dump(db, budget) for budget in db.scalars(select(Budget).where(Budget.id.in_(ids), Budget.archived.is_(False)).order_by(Budget.start_date, Budget.id))] if ids else []
    if row.attribution_pending:
        message = row.attribution_reason
    elif budgets:
        message = "预算仅提醒；本单预算占用已释放" if row.state == "RELEASED" else "预算仅提醒；本单已计入对应占用"
    else:
        message = "未配置预算"
    return {"usage": usage_dump(db, row, approval), "budgets": budgets, "message": message}


def export_workbook(db, budget_id):
    """同一读快照导出预算与全部明细；金额、编号和用户输入显式保存为文本。"""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    statement = usage_statement(db, budget_id=budget_id)
    count = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    if count > EXPORT_LIMIT:
        raise HTTPException(422, "当前预算的报销明细超过50,000条，请使用更小范围的预算导出")
    budget = budget_dump(db, get_budget(db, budget_id))
    complete = completeness(db, budget["company_id"])
    workbook = Workbook(write_only=True)

    def append(sheet, values, header=False):
        cells = []
        for value in values:
            cell = WriteOnlyCell(sheet, value=str(value) if value is not None else "")
            cell.data_type, cell.number_format = "s", "@"
            if header:
                cell.font, cell.fill = Font(color="FFFFFF", bold=True), PatternFill("solid", fgColor="1665F5")
            cells.append(cell)
        sheet.append(cells)

    def sheet(name, headers):
        result = workbook.create_sheet(name)
        result.freeze_panes = "A2"
        for index in range(1, len(headers) + 1):
            result.column_dimensions[get_column_letter(index)].width = 26
        append(result, headers, True)
        return result

    explanation = sheet("预算说明", ["项目", "说明"])
    for values in [("预算名称", budget["name"]), ("公司主体", budget["company_name"]), ("部门", budget["department"]),
                   ("费用类别", budget["expense_type"] or "全部费用"), ("周期", f"{budget['start_date']} 至 {budget['end_date']}"),
                   ("时间口径", "飞书申请提交时间，北京时间，包含起止日期全天"),
                   ("金额口径", "最终通过计已用，审批中预占，通过后撤销冻结；不代表付款或入账"),
                   ("重叠预算", "各预算独立核算，不可跨预算叠加金额"),
                   ("主体范围待归属单数", complete["pending_count"]), ("全局历史待建账单数", complete["backfill_remaining"]),
                   ("生成时间", (now() + OFFSET).isoformat(timespec="seconds") + " 北京时间")]:
        append(explanation, values)
    summary = sheet("金额汇总", ["指标", "金额（元）"])
    for key, label in [("amount", "预算额度"), ("used_amount", "已使用"), ("reserved_amount", "在途占用"),
                       ("frozen_amount", "冻结占用"), ("available_amount", "可用余额"), ("exceeded_amount", "超出金额")]:
        append(summary, [label, f"{budget[key]:.2f}"])
    entries = sheet("报销明细", ["审批编号", "事由", "报销人", "人员身份", "公司主体", "部门", "类别", "金额（元）", "占用状态", "提交时间（北京时间）", "历史金额待核对"])
    names, companies = dict(db.execute(select(User.id, User.name)).all()), dict(db.execute(select(Company.id, Company.name)).all())
    labels = {"USED": "已使用", "RESERVED": "在途", "FROZEN": "冻结", "RELEASED": "已释放"}
    for row, approval in db.execute(statement):
        item = usage_dump(db, row, approval, names, companies)
        append(entries, [item["serial_number"], item["title"], item["applicant"], item["applicant_id"], item["company_name"],
            row.department, row.expense_type, f"{row.amount:.2f}", labels[row.state],
            (row.submitted_at + OFFSET).isoformat(timespec="seconds") if row.submitted_at else "", "是" if row.historical_review else "否"])
    history = sheet("操作记录", ["操作", "操作人", "核查依据", "操作前", "操作后", "时间（北京时间）"])
    for row in db.scalars(operation_statement(db, budget_id).order_by(BudgetOperation.created_at, BudgetOperation.id)):
        append(history, [row.action, row.actor_name, row.reason, json.dumps(row.before, ensure_ascii=False),
            json.dumps(row.after, ensure_ascii=False), (row.created_at + OFFSET).isoformat(timespec="seconds")])
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def seed_demo_budgets(db):
    """仅完善演示表单与缺失的预算样例；重复启动保留财务已调整的额度、归属和释放记录。"""
    from .config import settings
    if settings().app_mode != "demo":
        return
    begin_write(db)
    template = db.get(ApprovalTemplate, "DEMO-EXPENSE")
    if not template:
        db.rollback()
        return
    mapping = dict(template.field_mapping or {})
    mapping.setdefault("department", "department")
    template.field_mapping = mapping
    department_id = mapping["department"]
    for approval in db.scalars(select(Approval).where(Approval.template_code == template.code)):
        snapshot = dict(approval.external_snapshot or {})
        form = list(approval.raw_form or snapshot.get("form") or [])
        if approval.department and not any(field.get("id") == department_id for field in form):
            form.append({"id": department_id, "name": "申请部门", "type": "input", "value": approval.department})
        approval.raw_form = form
        snapshot["form"] = form
        approval.external_snapshot = snapshot
    for identity, title, department, status, amount in [
        ("demo-budget-frozen", "演示：通过后撤销待核对预算", "行政管理部", "REVERTED", "1600.00"),
        ("demo-budget-unassigned", "演示：申请部门缺失待归属", "", "PENDING", "500.00"),
    ]:
        if db.get(Approval, identity):
            continue
        submitted = now() - timedelta(days=2)
        form = [{"id": "amount", "value": amount}, {"id": "reason", "value": title},
                {"id": "expense", "value": "办公费"}, {"id": "buyer", "value": "91310000DEMO00001X"}]
        if department:
            form.append({"id": department_id, "name": "申请部门", "value": department})
        snapshot = {"approval_code": template.code, "instance_code": identity, "serial_number": identity,
            "approval_name": title, "applicant_name": "陈予宁", "open_id": "demo-finance", "status": status,
            "reverted": status == "REVERTED", "start_time": int((submitted - datetime(1970, 1, 1)).total_seconds() * 1000),
            "form": form, "task_list": [], "demo_files": []}
        db.add(Approval(id=identity, template_code=template.code, serial_number=identity, title=title,
            applicant="陈予宁", applicant_id="demo-finance", department=department, amount=Decimal(amount),
            expense_type="办公费", status=status, approved_once=status == "REVERTED", submitted_at=submitted,
            raw_form=form, external_snapshot=snapshot))
    db.flush()
    companies = list(db.scalars(select(Company)))
    for approval in db.scalars(select(Approval).where(Approval.template_code == template.code)):
        refresh_usage(db, approval, companies=companies)
    year, month = today().year, today().month
    start, end = date(year, month, 1), date(year, month, monthrange(year, month)[1])
    specs = [
        ("demo-budget-normal", "财务部门月度预算", "demo-cloud", "财务管理部", "", "MONTH", start, end, "3000.00"),
        ("demo-budget-warning", "商务差旅年度预算", "demo-cloud", "商务拓展部", "差旅费", "YEAR", date(year, 1, 1), date(year, 12, 31), "9000.00"),
        ("demo-budget-exceeded", "设计软件月度预算", "demo-cloud", "产品设计部", "软件服务费", "MONTH", start, end, "1000.00"),
        ("demo-budget-freeze", "行政办公月度预算", "demo-cloud", "行政管理部", "办公费", "MONTH", start, end, "5000.00"),
    ]
    user = db.get(User, "demo-admin")
    for identity, name, company_id, department, category, period, first, last, amount in specs:
        if db.get(Budget, identity):
            continue
        # 演示初始化与正式创建共用范围唯一约束，不覆盖用户已创建的同范围预算。
        duplicate = db.scalar(select(Budget.id).where(Budget.company_id == company_id, Budget.department == department,
            Budget.expense_type == category, Budget.start_date == first, Budget.end_date == last))
        if duplicate:
            continue
        row = Budget(id=identity, name=name, company_id=company_id, department=department, expense_type=category,
            period_type=period, start_date=first, end_date=last, amount=Decimal(amount), notes="仅用于预算管理功能演示")
        db.add(row)
        db.flush()
        if user:
            record_operation(db, user, "CREATE", {}, {key: getattr(row, key) for key in BUDGET_FIELDS}, budget_id=identity)
    db.commit()
