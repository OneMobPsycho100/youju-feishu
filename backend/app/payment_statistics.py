"""付款统计按已同步审批实例查询。汇总、下钻和导出共用主体、项目与日期口径。"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
from io import BytesIO

from fastapi import HTTPException
from sqlalchemy import case, func, literal, select

from .models import Approval, Company
from .payment_statistics_schemas import PaymentDetailScope, PaymentGroupBy
from .reimbursement_statistics import SHANGHAI_OFFSET, ZERO, local_names, person_name, validate_company


GROUP_LABELS = {"project": "项目", "department": "申请部门", "payment_method": "付款方式"}
MISSING_PROJECT = "未填写项目"
MISSING_DEPARTMENT = "未填写部门"
MISSING_METHOD = "未填写付款方式"


@dataclass(frozen=True)
class PaymentFilters:
    """同一主体及项目条件用于区间、截至结束日累计和缺时间的待补齐明细。"""
    company_id: str
    start_date: date
    end_date: date
    project_keys: tuple[str, ...]

    def utc_bounds(self) -> tuple[datetime, datetime]:
        """将包含首尾日的北京时间范围转成数据库的 UTC 左闭右开区间。"""
        start = datetime.combine(self.start_date, time.min) - SHANGHAI_OFFSET
        end = datetime.combine(self.end_date + timedelta(days=1), time.min) - SHANGHAI_OFFSET
        return start, end


def _trim(column):
    # 同步已规范化新表单，查询时也清理历史值的常见首尾空白，包括全角空格。
    return func.trim(func.coalesce(column, ""), " \t\r\n\v\f\u00a0\u3000")


def _source(company_id: str, project_keys: tuple[str, ...] = (), *, approved_only=False):
    """每个付款实例投影一行。使用表单的付款主体，附件关联主体不会扩大汇总范围。"""
    mapped_company = Approval.payment_details["company_id"].as_string()
    declared_company = _trim(Approval.payment_details["company_name"].as_string())
    project = _trim(Approval.payment_details["project"].as_string())
    department = _trim(Approval.department)
    payment_method = _trim(Approval.payment_details["payment_method"].as_string())
    # JSON 数组保留主体类型和完整字段边界，避免同名跨主体或字段中的分隔符造成键碰撞。
    matched_company = Company.id.is_not(None)
    project_key = func.json_array(
        case((matched_company, "mapped"), else_="declared"),
        case((matched_company, Company.id), else_=declared_company),
        project,
    )
    company_name = func.coalesce(Company.name, case(
        (declared_company != "", declared_company + literal("（未匹配）")), else_="主体未识别"))
    statement = select(
        Approval.id, Approval.serial_number, Approval.title, Approval.applicant, Approval.applicant_id,
        Approval.amount, Approval.approved_at, company_name.label("company_name"), project.label("project"),
        project_key.label("project_key"), department.label("department"), payment_method.label("payment_method"),
    ).select_from(Approval).outerjoin(Company, Company.id == mapped_company).where(Approval.business_type == "PAYMENT")
    if company_id:
        statement = statement.where(mapped_company == company_id)
    if project_keys:
        statement = statement.where(project_key.in_(project_keys))
    if approved_only:
        statement = statement.where(Approval.status == "APPROVED")
    return statement.subquery()


def _project_options(db, company_id: str, project_keys: tuple[str, ...] = ()):
    source = _source(company_id, project_keys)
    rows = db.execute(select(source.c.project_key, source.c.project, source.c.company_name).distinct())
    return sorted([
        {"key": row.project_key, "name": row.project or MISSING_PROJECT, "company_name": row.company_name}
        for row in rows
    ], key=lambda item: (item["name"], item["company_name"], item["key"]))


def options(db, company_id: str):
    validate_company(db, company_id)
    return {"projects": _project_options(db, company_id)}


def _add_group(groups, key: str, name: str, company_name: str | None, amount: Decimal):
    group = groups.setdefault(key, {"key": key, "name": name, "company_name": company_name, "amount": ZERO, "count": 0})
    group["amount"] += amount
    group["count"] += 1


def _sorted_groups(groups, total: Decimal):
    for group in groups.values():
        group["percentage"] = (group["amount"] * 100 / total).quantize(Decimal("0.01")) if total else ZERO
    return sorted(groups.values(), key=lambda group: (-group["amount"], group["name"], group["company_name"] or "", group["key"]))


def _empty_point(key: str):
    return {"date": key, "amount": ZERO, "count": 0}


def _trend_points(trend, filters: PaymentFilters, daily: bool):
    cursor = filters.start_date if daily else filters.start_date.replace(day=1)
    while cursor <= filters.end_date:
        key = cursor.isoformat() if daily else cursor.isoformat()[:7]
        yield trend.get(key, _empty_point(key))
        cursor = cursor + timedelta(days=1) if daily else date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)


def summary(db, filters: PaymentFilters):
    validate_company(db, filters.company_id)
    start, end = filters.utc_bounds()
    daily = (filters.end_date - filters.start_date).days < 90
    source = _source(filters.company_id, filters.project_keys, approved_only=True)
    columns = source.c
    buckets = {dimension: {} for dimension in GROUP_LABELS}
    trend = {}
    named_projects = set()
    period_amount = cumulative_amount = ZERO
    count = 0

    # SQLite 整数分由 Money 转为 Decimal。按批读取并累加，避免 SQLite SUM 整数溢出。
    # 截止日以前的全部有效记录构成累计，只有起始日以后的记录用于区间分组和趋势。
    statement = select(columns.approved_at, columns.amount, columns.project, columns.project_key,
        columns.company_name, columns.department, columns.payment_method).where(columns.approved_at < end)
    with localcontext() as context:
        context.prec = 50
        for row in db.execute(statement.execution_options(yield_per=1000)):
            cumulative_amount += row.amount
            if row.approved_at < start:
                continue
            period_amount += row.amount
            count += 1
            if row.project:
                named_projects.add(row.project_key)
            _add_group(buckets["project"], row.project_key, row.project or MISSING_PROJECT, row.company_name, row.amount)
            _add_group(buckets["department"], row.department, row.department or MISSING_DEPARTMENT, None, row.amount)
            _add_group(buckets["payment_method"], row.payment_method, row.payment_method or MISSING_METHOD, None, row.amount)
            day = (row.approved_at + SHANGHAI_OFFSET).date().isoformat()
            key = day if daily else day[:7]
            point = trend.setdefault(key, _empty_point(key))
            point["amount"] += row.amount
            point["count"] += 1
        groups = {dimension: _sorted_groups(items, period_amount) for dimension, items in buckets.items()}

    missing_count = db.scalar(select(func.count()).select_from(source).where(columns.approved_at.is_(None)))
    return {
        "scope": {"company_id": filters.company_id, "start_date": filters.start_date, "end_date": filters.end_date,
                  "project_keys": list(filters.project_keys)},
        "timezone": "Asia/Shanghai",
        "totals": {"period_amount": period_amount, "cumulative_amount": cumulative_amount,
                   "count": count, "project_count": len(named_projects)},
        "groups": groups,
        "project_ranking": groups["project"][:10],
        "granularity": "day" if daily else "month",
        "trend": list(_trend_points(trend, filters, daily)),
        "missing_approved_count": missing_count,
    }


def _detail_statement(filters: PaymentFilters, scope: PaymentDetailScope = "period",
                      group_by: PaymentGroupBy | None = None, group_key: str | None = None):
    if (group_by is None) != (group_key is None):
        raise HTTPException(422, "查看分组明细时须同时指定分组维度和分组键")
    source = _source(filters.company_id, filters.project_keys, approved_only=True)
    columns = source.c
    start, end = filters.utc_bounds()
    statement = select(source)
    if scope == "missing_approved_at":
        statement = statement.where(columns.approved_at.is_(None))
    else:
        statement = statement.where(columns.approved_at < end)
        if scope == "period":
            statement = statement.where(columns.approved_at >= start)
    if group_by is not None:
        group_column = {"project": columns.project_key, "department": columns.department, "payment_method": columns.payment_method}[group_by]
        statement = statement.where(group_column == group_key)
    return statement.order_by(columns.approved_at.desc(), columns.id)


def _detail(row, names):
    return {
        "id": row.id, "serial_number": row.serial_number, "title": row.title,
        "applicant": person_name(row.applicant_id, row.applicant, names),
        "department": row.department or MISSING_DEPARTMENT, "project": row.project or MISSING_PROJECT,
        "company_name": row.company_name, "payment_method": row.payment_method or MISSING_METHOD,
        "amount": row.amount, "approved_at": row.approved_at,
    }


def details(db, filters: PaymentFilters, scope: PaymentDetailScope, group_by: PaymentGroupBy | None,
            group_key: str | None, page: int, page_size: int):
    validate_company(db, filters.company_id)
    statement = _detail_statement(filters, scope, group_by, group_key)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    names = local_names(db)
    rows = db.execute(statement.offset((page - 1) * page_size).limit(page_size))
    return {"items": [_detail(row, names) for row in rows], "total": total, "page": page, "page_size": page_size}


def export_workbook(db, filters: PaymentFilters, group_by: PaymentGroupBy) -> bytes:
    """在同一数据库读快照中导出指标、分组、趋势和完整区间明细。"""
    from openpyxl import Workbook
    from openpyxl.xml.constants import MAX_ROW

    from .statistics_workbook import append_row, create_sheet

    report = summary(db, filters)
    workbook = Workbook(write_only=True)
    subject = db.get(Company, filters.company_id) if filters.company_id else None
    project_labels = "全部项目"
    if filters.project_keys:
        labels = {item["key"]: f"{item['name']} · {item['company_name']}"
                  for item in _project_options(db, filters.company_id, filters.project_keys)}
        project_labels = "、".join(labels.get(key, key) for key in filters.project_keys)
    explanation = create_sheet(workbook, "统计说明", ["项目", "说明"])
    for row in [
        ("统计口径", "以已同步且当前最终通过的付款申请作为已支付，每个审批实例计一次"),
        ("付款主体", subject.name if subject else "全部主体"),
        ("项目", project_labels),
        ("所选期间", f"{filters.start_date} 至 {filters.end_date}（包含起止日全天）"),
        ("时间口径", "飞书最终通过时间，使用北京时间"),
        ("累计口径", "相同主体和项目条件下，已同步历史截至结束日的累计金额"),
        ("分组维度", GROUP_LABELS[group_by]),
    ]:
        append_row(explanation, row)

    totals = report["totals"]
    metrics = create_sheet(workbook, "付款指标", ["指标", "值"])
    for row in [
        ("所选期间已支付金额（元）", f"{totals['period_amount']:.2f}"),
        ("截至结束日累计已支付金额（元）", f"{totals['cumulative_amount']:.2f}"),
        ("所选期间已支付单数", totals["count"]),
        ("涉及的已命名项目数", totals["project_count"]),
        ("通过时间待补齐单数", report["missing_approved_count"]),
    ]:
        append_row(metrics, row)

    subject_header = ["付款主体"] if group_by == "project" else []
    groups = create_sheet(workbook, "分组汇总", [GROUP_LABELS[group_by], *subject_header, "已支付金额（元）", "单数", "区间占比（%）"])
    for group in report["groups"][group_by]:
        subject_value = [group["company_name"]] if group_by == "project" else []
        append_row(groups, [group["name"], *subject_value, f"{group['amount']:.2f}", group["count"], f"{group['percentage']:.2f}"])

    trend = create_sheet(workbook, "趋势", ["日期" if report["granularity"] == "day" else "月份", "已支付金额（元）", "单数"])
    for point in report["trend"]:
        append_row(trend, [point["date"], f"{point['amount']:.2f}", point["count"]])

    headers = ["审批编号", "付款事由", "付款主体", "项目", "申请人", "申请部门", "付款方式", "已支付金额（元）", "最终通过时间（北京时间）"]
    sheet = create_sheet(workbook, "付款明细", headers)
    names = local_names(db)
    for index, row in enumerate(db.execute(_detail_statement(filters).execution_options(yield_per=1000))):
        # 为超出单张 Excel 工作表容量的区间保留后续明细。
        if index and index % (MAX_ROW - 1) == 0:
            sheet = create_sheet(workbook, f"付款明细 {index // (MAX_ROW - 1) + 1}", headers)
        item = _detail(row, names)
        append_row(sheet, [item["serial_number"], item["title"], item["company_name"], item["project"], item["applicant"],
            item["department"], item["payment_method"], f"{item['amount']:.2f}",
            (row.approved_at + SHANGHAI_OFFSET).isoformat(sep=" ", timespec="seconds")])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
