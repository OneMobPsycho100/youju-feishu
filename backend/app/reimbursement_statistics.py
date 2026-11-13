"""报销金额与预算消耗共用查询。先投影每单唯一事实，再筛选、聚合、下钻与导出。"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
import io

from fastapi import HTTPException
from sqlalchemy import case, func, literal, or_, select

from .config import settings
from .models import Approval, Budget, BudgetUsage, Company, User, now
from .reimbursement_schemas import GroupBy

SHANGHAI_OFFSET = timedelta(hours=8)
EXPORT_LIMIT = 50000
ZERO = Decimal("0.00")
ACTIVE_STATES = ("USED", "RESERVED", "FROZEN")
STATE_FIELDS = {"USED": "used_amount", "RESERVED": "reserved_amount", "FROZEN": "frozen_amount"}
STATE_LABELS = {"": "全部占用", "USED": "已使用", "RESERVED": "在途占用", "FROZEN": "冻结占用"}
STATUS_LABELS = {"": "全部状态", "APPROVED": "已通过", "PENDING": "审批中", "REJECTED": "已拒绝",
                 "CANCELED": "已撤回", "REVERTED": "已撤销", "DELETED": "已删除"}
DIMENSION_LABELS = {"department": "部门", "applicant": "报销人", "expense_type": "报销类别"}


@dataclass(frozen=True)
class StatisticsFilters:
    """所有入口使用相同条件；追加字段保留旧构造调用和默认申请金额语义。"""
    company_id: str
    start_date: date
    end_date: date
    applicant_ids: tuple[str, ...] = ()
    expense_types: tuple[str, ...] = ()
    status: str = "APPROVED"
    measure: str = "application"
    department: str = ""
    usage_state: str = ""
    budget_coverage: str = "all"

    def scope(self):
        return {**self.__dict__, "applicant_ids": list(self.applicant_ids), "expense_types": list(self.expense_types)}


def validate_company(db, company_id: str) -> None:
    if company_id and not db.get(Company, company_id):
        raise HTTPException(404, "公司主体不存在，请重新选择")


def applicant_key():
    """已识别人员按稳定身份分组；历史姓名仅作为带命名空间的临时身份。"""
    return case((func.coalesce(Approval.applicant_id, "") != "", Approval.applicant_id),
                else_=literal("legacy:") + Approval.applicant)


def expense_key():
    return named_key(Approval.expense_type, "其他费用")


def named_key(column, fallback):
    return case((func.trim(func.coalesce(column, "")) != "", func.trim(column)), else_=fallback)


def company_conditions(company_id: str):
    if not company_id:
        return []
    # 申请金额口径保留原主体归属：EXISTS 避免一单多主体 JOIN 扩大单数。
    subjects = func.json_each(Approval.company_ids).table_valued("value").alias("statistics_subject")
    return [select(1).select_from(subjects).where(subjects.c.value == company_id).correlate(Approval).exists()]


def local_names(db):
    return dict(db.execute(select(User.id, User.name)).all())


def person_name(identity: str, original: str, names: dict) -> str:
    return names.get(identity) or original or identity or "未知申请人"


def dataset(filters: StatisticsFilters, *, unresolved_company=False):
    """把两种口径投影成相同列集合；每个审批在投影中只有一行。

    申请金额的主体、时间和状态继续取原审批。消耗使用已确认用量的金额、部门和提交时间，
    预算覆盖只做 EXISTS 判断，允许匹配已归档预算，绝不 JOIN 多个预算后累计金额。
    """
    if filters.measure == "application":
        statement = select(Approval.id, Approval.serial_number, Approval.title, Approval.applicant, Approval.applicant_id,
            applicant_key().label("person_key"), Approval.department, named_key(Approval.department, "未填写部门").label("department_key"),
            expense_key().label("expense_type"), Approval.amount, Approval.submitted_at, Approval.status,
            literal("").label("usage_state"), literal(None).label("budget_matched"), literal(False).label("attribution_pending"), Approval.created_at)
        return statement.where(Approval.business_type == "REIMBURSEMENT", *company_conditions(filters.company_id)).subquery()
    day = func.date(BudgetUsage.submitted_at, "+8 hours")
    matched = select(1).select_from(Budget).where(Budget.company_id == BudgetUsage.company_id,
        Budget.department == BudgetUsage.department, Budget.start_date <= day, Budget.end_date >= day,
        or_(Budget.expense_type == "", Budget.expense_type == BudgetUsage.expense_type)).correlate(BudgetUsage).exists()
    statement = select(Approval.id, Approval.serial_number, Approval.title, Approval.applicant, Approval.applicant_id,
        applicant_key().label("person_key"), BudgetUsage.department, named_key(BudgetUsage.department, "未填写部门").label("department_key"),
        named_key(BudgetUsage.expense_type, "其他费用").label("expense_type"), BudgetUsage.amount, BudgetUsage.submitted_at, Approval.status,
        BudgetUsage.state.label("usage_state"), matched.label("budget_matched"), BudgetUsage.attribution_pending, Approval.created_at)
    statement = statement.join(BudgetUsage, BudgetUsage.approval_id == Approval.id).where(Approval.business_type == "REIMBURSEMENT")
    if filters.company_id:
        condition = BudgetUsage.company_id == filters.company_id
        if unresolved_company:
            condition = or_(condition, BudgetUsage.company_id.is_(None))
        statement = statement.where(condition)
    return statement.subquery()


def conditions(filters: StatisticsFilters, source=None, *, dates=True, ready=True):
    """汇总、明细和导出共用一套条件，不把申请状态传入消耗计算。"""
    source = dataset(filters) if source is None else source
    columns, result = source.c, []
    if filters.measure == "application":
        if filters.status:
            result.append(columns.status == filters.status)
    else:
        result.append(columns.usage_state.in_(ACTIVE_STATES))
        if filters.usage_state:
            result.append(columns.usage_state == filters.usage_state)
        if ready:
            result.append(columns.attribution_pending.is_(False))
        if filters.budget_coverage != "all":
            result.append(columns.budget_matched.is_(filters.budget_coverage == "matched"))
    if filters.department:
        result.append(columns.department_key == filters.department)
    if filters.applicant_ids:
        result.append(columns.person_key.in_(filters.applicant_ids))
    if filters.expense_types:
        result.append(columns.expense_type.in_(filters.expense_types))
    if dates:
        start = datetime.combine(filters.start_date, time.min) - SHANGHAI_OFFSET
        end = datetime.combine(filters.end_date + timedelta(days=1), time.min) - SHANGHAI_OFFSET
        result.extend((columns.submitted_at >= start, columns.submitted_at < end))
    return result


def options(db, company_id: str, measure="application", department=""):
    validate_company(db, company_id)
    filters = StatisticsFilters(company_id, date(2000, 1, 1), date(2000, 1, 1), measure=measure)
    source = dataset(filters)
    statement = select(source.c.person_key, source.c.applicant, source.c.applicant_id, source.c.expense_type, source.c.department_key)
    if measure == "consumption":
        # 已释放费用仍保留人员和部门选项，便于确认其当前消耗为零，而不是把筛选扩大为全部。
        statement = statement.where(source.c.attribution_pending.is_(False))
    names, applicants, categories, departments = local_names(db), {}, set(), set()
    for row in db.execute(statement.order_by(source.c.created_at, source.c.id).execution_options(yield_per=1000)):
        departments.add(row.department_key)
        if department and row.department_key != department:
            continue
        applicants[row.person_key] = {"value": row.person_key, "label": person_name(row.applicant_id, row.applicant, names),
                                      "identity_pending": not bool(row.applicant_id)}
        categories.add(row.expense_type)
    return {"applicants": sorted(applicants.values(), key=lambda item: (item["label"], item["value"])),
            "expense_types": sorted(categories), "departments": sorted(departments)}


def group_column(source, group_by):
    return {"applicant": source.c.person_key, "department": source.c.department_key, "expense_type": source.c.expense_type}[group_by]


def empty_point(key):
    return {"date": key, "amount": ZERO, "count": 0, "used_amount": ZERO, "reserved_amount": ZERO, "frozen_amount": ZERO}


def summary(db, filters: StatisticsFilters, group_by: GroupBy):
    validate_company(db, filters.company_id)
    granularity = "day" if (filters.end_date - filters.start_date).days + 1 <= 90 else "month"
    source, names = dataset(filters), local_names(db)
    buckets = {key: {} for key in DIMENSION_LABELS}
    trend, people, categories = {}, set(), set()
    totals = {"amount": ZERO, "count": 0, "applicant_count": 0, "expense_type_count": 0,
              "used_amount": ZERO, "reserved_amount": ZERO, "frozen_amount": ZERO, "unmatched_amount": ZERO, "unmatched_count": 0}
    pending_identity_count = 0
    # 高精度逐批投影汇总，避免 SQLite 整数 SUM 溢出，也避免读取原表单和附件。
    with localcontext() as context:
        context.prec = 50
        statement = select(source).where(*conditions(filters, source)).order_by(source.c.created_at, source.c.id).execution_options(yield_per=1000)
        for row in db.execute(statement):
            totals["amount"] += row.amount
            totals["count"] += 1
            people.add(row.person_key); categories.add(row.expense_type)
            pending = not bool(row.applicant_id)
            pending_identity_count += int(pending)
            for dimension, key, name in [("department", row.department_key, row.department_key),
                ("applicant", row.person_key, person_name(row.applicant_id, row.applicant, names)),
                ("expense_type", row.expense_type, row.expense_type)]:
                item = buckets[dimension].setdefault(key, {"key": key, "name": name, "amount": ZERO, "count": 0,
                    "identity_pending": pending if dimension == "applicant" else False})
                item["name"], item["amount"], item["count"] = name, item["amount"] + row.amount, item["count"] + 1
            day = (row.submitted_at + SHANGHAI_OFFSET).date()
            key = day.isoformat() if granularity == "day" else day.strftime("%Y-%m")
            point = trend.setdefault(key, empty_point(key))
            point["amount"] += row.amount; point["count"] += 1
            field = STATE_FIELDS.get(row.usage_state)
            if field:
                totals[field] += row.amount; point[field] += row.amount
                if not row.budget_matched:
                    totals["unmatched_amount"] += row.amount; totals["unmatched_count"] += 1
        for groups in buckets.values():
            for item in groups.values():
                item["percentage"] = (item["amount"] * 100 / totals["amount"]).quantize(Decimal("0.01")) if totals["amount"] else ZERO
    cursor, points = filters.start_date, []
    while cursor <= filters.end_date:
        key = cursor.isoformat() if granularity == "day" else cursor.strftime("%Y-%m")
        points.append(trend.get(key, empty_point(key)))
        cursor = cursor + timedelta(days=1) if granularity == "day" else date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    missing = db.scalar(select(func.count()).select_from(source).where(*conditions(filters, source, dates=False), source.c.submitted_at.is_(None)))
    unresolved, unresolved_amount, remaining = 0, ZERO, 0
    if filters.measure == "consumption":
        unresolved_source = dataset(filters, unresolved_company=True)
        start = datetime.combine(filters.start_date, time.min) - SHANGHAI_OFFSET
        end = datetime.combine(filters.end_date + timedelta(days=1), time.min) - SHANGHAI_OFFSET
        # 未知日期无法判定周期；已知日期按同一窗口提示，未知主体也纳入选定主体的待确认提示。
        statement = select(unresolved_source.c.amount).where(*conditions(filters, unresolved_source, dates=False, ready=False),
            or_(unresolved_source.c.attribution_pending.is_(True), unresolved_source.c.submitted_at.is_(None)),
            or_(unresolved_source.c.submitted_at.is_(None), (unresolved_source.c.submitted_at >= start) & (unresolved_source.c.submitted_at < end)))
        with localcontext() as context:
            context.prec = 50
            for amount in db.scalars(statement.execution_options(yield_per=1000)):
                unresolved += 1; unresolved_amount += amount
        remaining = db.scalar(select(func.count()).select_from(Approval).where(Approval.business_type == "REIMBURSEMENT",
            ~select(BudgetUsage.approval_id).where(BudgetUsage.approval_id == Approval.id).exists()))
    totals["applicant_count"], totals["expense_type_count"] = len(people), len(categories)
    groups = {dimension: sorted(items.values(), key=lambda item: (-item["amount"], item["name"], item["key"])) for dimension, items in buckets.items()}
    return {"scope": filters.scope(), "timezone": "Asia/Shanghai", "group_by": group_by, "totals": totals,
        "groups": groups[group_by], "department_groups": groups["department"], "applicant_groups": groups["applicant"], "expense_type_groups": groups["expense_type"],
        "granularity": granularity, "trend": points, "missing_submitted_count": missing, "pending_identity_count": pending_identity_count,
        "missing_attribution_count": unresolved, "missing_attribution_amount": unresolved_amount, "backfill_remaining": remaining,
        "demo_time_simulated": settings().app_mode == "demo"}


def detail_statement(filters: StatisticsFilters, group_by: GroupBy | None = None, group_key: str | None = None):
    source = dataset(filters)
    statement = select(source).where(*conditions(filters, source))
    if group_key is not None:
        if group_by is None:
            raise HTTPException(422, "查看分组明细时必须指定分组维度")
        statement = statement.where(group_column(source, group_by) == group_key)
    return statement.order_by(source.c.submitted_at.desc(), source.c.id)


def detail_dump(row, names):
    return {"id": row.id, "serial_number": row.serial_number, "title": row.title,
        "applicant_id": row.person_key, "applicant": person_name(row.applicant_id, row.applicant, names),
        "identity_pending": not bool(row.applicant_id), "department": row.department, "expense_type": row.expense_type,
        "amount": row.amount, "submitted_at": row.submitted_at, "status": row.status, "usage_state": row.usage_state, "budget_matched": row.budget_matched}


def details(db, filters: StatisticsFilters, group_by, group_key, page, page_size):
    validate_company(db, filters.company_id)
    statement = detail_statement(filters, group_by, group_key)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    names = local_names(db)
    return {"items": [detail_dump(row, names) for row in db.execute(statement.offset((page - 1) * page_size).limit(page_size))],
            "total": total, "page": page, "page_size": page_size}


def export_workbook(db, filters: StatisticsFilters, group_by: GroupBy):
    """同一读快照导出当前口径的完整报表，不继承页面下钻和分页；所有用户文本显式保存为文本。"""
    from openpyxl import Workbook
    from .statistics_workbook import append_row as append, create_sheet

    validate_company(db, filters.company_id)
    statement = detail_statement(filters)
    count = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    if count > EXPORT_LIMIT:
        raise HTTPException(422, "符合条件的审批超过50,000条，请缩小时间范围或增加筛选条件后导出")
    result, names = summary(db, filters, group_by), local_names(db)
    consumption = filters.measure == "consumption"
    workbook = Workbook(write_only=True)

    def sheet(title, headers):
        return create_sheet(workbook, title, headers)

    subject = db.get(Company, filters.company_id) if filters.company_id else None
    available = options(db, filters.company_id, filters.measure, filters.department)
    labels = {item["value"]: item["label"] for item in available["applicants"]}
    explanation = sheet("统计说明", ["项目", "说明"])
    for values in [("统计口径", "预算消耗" if consumption else "报销金额"), ("公司主体", subject.name if subject else "全部公司主体"),
        ("时间范围", f"{filters.start_date} 至 {filters.end_date}（北京时间，包含起止日期全天）"),
        ("时间口径", "申请提交时间；消耗使用预算用量快照中的提交时间，不表示历史余额"),
        ("申请部门", filters.department or "全部部门"), ("报销人", "、".join(labels.get(key, key) for key in filters.applicant_ids) or "全部报销人"),
        ("报销类别", "、".join(filters.expense_types) or "全部类别"),
        ("占用状态" if consumption else "审批状态", STATE_LABELS[filters.usage_state] if consumption else STATUS_LABELS[filters.status]),
        ("预算覆盖", {"all": "全部已归属报销", "matched": "已匹配预算", "unmatched": "未匹配预算"}[filters.budget_coverage] if consumption else "不适用"),
        ("分组维度", DIMENSION_LABELS[group_by]),
        ("数据口径", "每单唯一用量；已用、在途、冻结构成消耗，释放不计入；预算重叠不重复，包含未匹配预算报销" if consumption else "系统已同步审批；每单计一次申请总额，跨主体不拆分；历史导入发票不单独计入"),
        ("金额合计（元）", f"{result['totals']['amount']:.2f}"), ("申请单数", count),
        ("提交时间待补齐单数", result["missing_submitted_count"]), ("归属或时间待确认单数", result["missing_attribution_count"]),
        ("全局预算用量待建账单数", result["backfill_remaining"]), ("身份待补齐单数", result["pending_identity_count"]),
        ("生成时间", (now() + SHANGHAI_OFFSET).isoformat(timespec="seconds") + " 北京时间"),
        ("金额格式", "金额为两位小数十进制文本，避免大额精度损失"),
        ("演示时间", "演示单提交时间为模拟数据" if result["demo_time_simulated"] else "真实飞书提交时间")]:
        append(explanation, values)
    if consumption:
        for key, label in [("used_amount", "已使用"), ("reserved_amount", "在途占用"), ("frozen_amount", "冻结占用"), ("unmatched_amount", "未匹配预算金额")]:
            append(explanation, [label + "（元）", f"{result['totals'][key]:.2f}"])

    def export_groups(title, groups):
        grouped = sheet(title, ["分组", "分组标识", "金额（元）", "单数", "金额占比（%）", "身份状态"])
        for row in groups:
            append(grouped, [row["name"], row["key"], f"{row['amount']:.2f}", row["count"], f"{row['percentage']:.2f}", "身份待补齐" if row["identity_pending"] else ""])
    export_groups("分组汇总", result["groups"])
    export_groups("类别汇总", result["expense_type_groups"])
    trend = sheet("趋势", ["日期" if result["granularity"] == "day" else "月份", "金额（元）", "单数"] + (["已使用（元）", "在途（元）", "冻结（元）"] if consumption else []))
    for row in result["trend"]:
        append(trend, [row["date"], f"{row['amount']:.2f}", row["count"]] + ([f"{row[key]:.2f}" for key in STATE_FIELDS.values()] if consumption else []))
    detail = sheet("审批明细", ["审批编号", "事由", "报销人", "报销人身份", "身份状态", "部门", "报销类别", "金额（元）", "提交时间（北京时间）", "审批状态"] + (["预算占用状态", "预算覆盖"] if consumption else []))
    for row in db.execute(statement.execution_options(yield_per=1000)):
        item = detail_dump(row, names)
        append(detail, [item["serial_number"], item["title"], item["applicant"], item["applicant_id"], "身份待补齐" if item["identity_pending"] else "",
            item["department"], item["expense_type"], f"{item['amount']:.2f}", (item["submitted_at"] + SHANGHAI_OFFSET).isoformat(sep=" ", timespec="seconds"),
            STATUS_LABELS.get(item["status"], item["status"])] + ([STATE_LABELS[item["usage_state"]], "已匹配" if item["budget_matched"] else "未匹配"] if consumption else []))
    buffer = io.BytesIO(); workbook.save(buffer)
    return buffer.getvalue()
