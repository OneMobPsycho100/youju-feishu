"""报销统计共用服务：按审批整单去重，日期、明细与导出共用一套条件。"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, localcontext
import io

from fastapi import HTTPException
from sqlalchemy import case, func, literal, select

from .config import settings
from .models import Approval, Company, User, now
from .reimbursement_schemas import GroupBy

SHANGHAI_OFFSET = timedelta(hours=8)
EXPORT_LIMIT = 50000
STATUS_LABELS = {"": "全部状态", "APPROVED": "已通过", "PENDING": "审批中", "REJECTED": "已拒绝",
                 "CANCELED": "已撤回", "REVERTED": "已撤销", "DELETED": "已删除"}


@dataclass(frozen=True)
class StatisticsFilters:
    """已校验的公共条件；空 status 与缺省 APPROVED 必须保持区分。"""
    company_id: str
    start_date: date
    end_date: date
    applicant_ids: tuple[str, ...] = ()
    expense_types: tuple[str, ...] = ()
    status: str = "APPROVED"

    def scope(self):
        return {**self.__dict__, "applicant_ids": list(self.applicant_ids), "expense_types": list(self.expense_types)}


def validate_company(db, company_id: str) -> None:
    if company_id and not db.get(Company, company_id):
        raise HTTPException(404, "公司主体不存在，请重新选择")


def applicant_key():
    """显示名不能作为已识别人员的身份，同名 open_id 分开；旧记录使用独立命名空间。"""
    return case((func.coalesce(Approval.applicant_id, "") != "", Approval.applicant_id),
                else_=literal("legacy:") + Approval.applicant)


def expense_key():
    return case((func.trim(Approval.expense_type) != "", func.trim(Approval.expense_type)), else_="其他费用")


def company_conditions(company_id: str):
    if not company_id:
        return []
    # JSON 数组精确匹配，不能把主体 ID 的子串当作归属；不展开 JOIN，避免一单多主体重复计数。
    subjects = func.json_each(Approval.company_ids).table_valued("value").alias("statistics_subject")
    return [select(1).select_from(subjects).where(subjects.c.value == company_id).correlate(Approval).exists()]


def conditions(filters: StatisticsFilters, *, dates=True):
    result = [Approval.business_type == "REIMBURSEMENT", *company_conditions(filters.company_id)]
    if filters.status:
        result.append(Approval.status == filters.status)
    if filters.applicant_ids:
        result.append(applicant_key().in_(filters.applicant_ids))
    if filters.expense_types:
        result.append(expense_key().in_(filters.expense_types))
    if dates:
        # 起点含、终点不含次日零时，精确覆盖北京时间结束日期，不依赖宿主机时区。
        start = datetime.combine(filters.start_date, time.min) - SHANGHAI_OFFSET
        end = datetime.combine(filters.end_date + timedelta(days=1), time.min) - SHANGHAI_OFFSET
        result.extend((Approval.submitted_at >= start, Approval.submitted_at < end))
    return result


def local_names(db):
    return dict(db.execute(select(User.id, User.name)).all())


def person_name(identity: str, original: str, names: dict) -> str:
    return names.get(identity) or original or identity or "未知申请人"


def options(db, company_id: str):
    validate_company(db, company_id)
    names, applicants, categories = local_names(db), {}, set()
    # 多次申请的旧显示名可能不同，按采集顺序取最新显示名，身份仍使用稳定 key。
    rows = db.execute(select(applicant_key().label("key"), Approval.applicant,
        Approval.applicant_id, expense_key().label("expense_type")).where(Approval.business_type == "REIMBURSEMENT", *company_conditions(company_id))
        .order_by(Approval.created_at, Approval.id).execution_options(yield_per=1000))
    for row in rows:
        applicants[row.key] = {"value": row.key, "label": person_name(row.applicant_id, row.applicant, names),
                               "identity_pending": not bool(row.applicant_id)}
        categories.add(row.expense_type)
    return {"applicants": sorted(applicants.values(), key=lambda item: (item["label"], item["value"])),
            "expense_types": sorted(categories)}


def summary(db, filters: StatisticsFilters, group_by: GroupBy):
    validate_company(db, filters.company_id)
    granularity = "day" if (filters.end_date - filters.start_date).days + 1 <= 90 else "month"
    names, people, categories, groups, trend = local_names(db), set(), set(), {}, {}
    zero = Decimal("0.00")
    total_amount, count, pending_identity_count = zero, 0, 0
    rows = db.execute(select(Approval.amount, Approval.submitted_at, Approval.applicant, Approval.applicant_id,
        applicant_key().label("person_key"), expense_key().label("category")).where(*conditions(filters))
        .order_by(Approval.created_at, Approval.id).execution_options(yield_per=1000))
    # SQLite 整数 SUM 对多笔大额可能溢出；逐批投影必要字段，以高精度 Decimal 汇总，不载入表单/附件。
    with localcontext() as context:
        context.prec = 50
        for row in rows:
            total_amount += row.amount
            count += 1
            people.add(row.person_key)
            categories.add(row.category)
            pending = not bool(row.applicant_id)
            pending_identity_count += int(pending)
            key = row.person_key if group_by == "applicant" else row.category
            name = person_name(row.applicant_id, row.applicant, names) if group_by == "applicant" else row.category
            item = groups.setdefault(key, {"key": key, "name": name, "amount": zero, "count": 0,
                                           "identity_pending": pending if group_by == "applicant" else False})
            item["name"], item["amount"], item["count"] = name, item["amount"] + row.amount, item["count"] + 1
            day = (row.submitted_at + SHANGHAI_OFFSET).date()
            point_key = day.isoformat() if granularity == "day" else day.strftime("%Y-%m")
            point = trend.setdefault(point_key, {"date": point_key, "amount": zero, "count": 0})
            point["amount"], point["count"] = point["amount"] + row.amount, point["count"] + 1
        for item in groups.values():
            item["percentage"] = (item["amount"] * 100 / total_amount).quantize(Decimal("0.01")) if total_amount else zero
    cursor, points = filters.start_date, []
    while cursor <= filters.end_date:
        key = cursor.isoformat() if granularity == "day" else cursor.strftime("%Y-%m")
        points.append(trend.get(key, {"date": key, "amount": zero, "count": 0}))
        if granularity == "day":
            cursor += timedelta(days=1)
        else:
            cursor = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
    missing = db.scalar(select(func.count()).select_from(Approval).where(
        *conditions(filters, dates=False), Approval.submitted_at.is_(None)))
    return {"scope": filters.scope(), "timezone": "Asia/Shanghai", "group_by": group_by,
        "totals": {"amount": total_amount, "count": count, "applicant_count": len(people), "expense_type_count": len(categories)},
        "groups": sorted(groups.values(), key=lambda item: (-item["amount"], item["name"], item["key"])),
        "granularity": granularity, "trend": points, "missing_submitted_count": missing,
        "pending_identity_count": pending_identity_count, "demo_time_simulated": settings().app_mode == "demo"}


def detail_statement(filters: StatisticsFilters, group_by: GroupBy | None = None, group_key: str | None = None):
    statement = select(Approval.id, Approval.serial_number, Approval.title, Approval.applicant,
        applicant_key().label("applicant_key"), Approval.applicant_id, Approval.department,
        expense_key().label("expense_type"), Approval.amount, Approval.submitted_at, Approval.status).where(*conditions(filters))
    if group_key is not None:
        if group_by is None:
            raise HTTPException(422, "查看分组明细时必须指定分组维度")
        statement = statement.where((applicant_key() if group_by == "applicant" else expense_key()) == group_key)
    return statement.order_by(Approval.submitted_at.desc(), Approval.id)


def detail_dump(row, names):
    return {"id": row.id, "serial_number": row.serial_number, "title": row.title,
        "applicant_id": row.applicant_key, "applicant": person_name(row.applicant_id, row.applicant, names),
        "identity_pending": not bool(row.applicant_id), "department": row.department,
        "expense_type": row.expense_type, "amount": row.amount, "submitted_at": row.submitted_at, "status": row.status}


def details(db, filters: StatisticsFilters, group_by, group_key, page, page_size):
    validate_company(db, filters.company_id)
    statement = detail_statement(filters, group_by, group_key)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    names = local_names(db)
    rows = db.execute(statement.offset((page - 1) * page_size).limit(page_size))
    return {"items": [detail_dump(row, names) for row in rows], "total": total, "page": page, "page_size": page_size}


def export_workbook(db, filters: StatisticsFilters, group_by: GroupBy):
    """同一读事务生成四张表；导出不继承页面分组下钻或分页，超限直接报错。"""
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font, PatternFill

    validate_company(db, filters.company_id)
    count = db.scalar(select(func.count()).select_from(Approval).where(*conditions(filters)))
    if count > EXPORT_LIMIT:
        raise HTTPException(422, "符合条件的审批超过50,000条，请缩小时间范围或增加筛选条件后导出")
    result, names = summary(db, filters, group_by), local_names(db)
    workbook = Workbook(write_only=True)

    def sheet(title, headers):
        output = workbook.create_sheet(title)
        output.freeze_panes = "A2"
        cells = []
        for index, label in enumerate(headers, 1):
            from openpyxl.utils import get_column_letter
            output.column_dimensions[get_column_letter(index)].width = 26
            cell = WriteOnlyCell(output, value=label)
            cell.font, cell.fill = Font(color="FFFFFF", bold=True), PatternFill("solid", fgColor="1665F5")
            cells.append(cell)
        output.append(cells)
        return output

    def append(output, values):
        cells = []
        for value in values:
            cell = WriteOnlyCell(output, value=value)
            if isinstance(value, str):
                # 显式文本可保留超长编号和两位小数，也避免用户字段被 Excel 当作公式执行。
                cell.data_type, cell.number_format = "s", "@"
            cells.append(cell)
        output.append(cells)

    company = db.get(Company, filters.company_id) if filters.company_id else None
    available = options(db, filters.company_id)
    labels = {item["value"]: item["label"] + ("（身份待补齐）" if item["identity_pending"] else "") for item in available["applicants"]}
    explanation = sheet("统计说明", ["项目", "说明"])
    for values in [
        ("公司主体", company.name if company else "全部公司主体"),
        ("时间范围", f"{filters.start_date} 至 {filters.end_date}（北京时间，包含起止日期全天）"),
        ("时间口径", "飞书申请提交时间；不是工作台采集时间"),
        ("报销人", "、".join(labels.get(key, key) for key in filters.applicant_ids) or "全部报销人"),
        ("报销类别", "、".join(filters.expense_types) or "全部类别"),
        ("审批状态", STATUS_LABELS[filters.status]),
        ("分组维度", "报销人" if group_by == "applicant" else "报销类别"),
        ("数据口径", "系统已同步审批；每单计一次申请总额，跨主体不拆分；历史导入发票不单独计入"),
        ("申请金额合计（元）", f"{result['totals']['amount']:.2f}"),
        ("申请单数", count),
        ("提交时间待补齐单数", result["missing_submitted_count"]),
        ("身份待补齐单数", result["pending_identity_count"]),
        ("生成时间", (now() + SHANGHAI_OFFSET).isoformat(timespec="seconds") + " 北京时间"),
        ("金额格式", "金额为保留两位小数的十进制文本，避免 Excel 大额精度损失"),
        ("演示时间", "演示单提交时间为模拟数据" if result["demo_time_simulated"] else "真实飞书提交时间"),
    ]:
        append(explanation, values)
    grouped = sheet("分组汇总", ["分组", "分组标识", "金额（元）", "单数", "金额占比（%）", "身份状态"])
    for row in result["groups"]:
        append(grouped, [row["name"], row["key"], f"{row['amount']:.2f}", row["count"], f"{row['percentage']:.2f}",
                         "身份待补齐" if row["identity_pending"] else ""])
    trend = sheet("趋势", ["日期" if result["granularity"] == "day" else "月份", "金额（元）", "单数"])
    for row in result["trend"]:
        append(trend, [row["date"], f"{row['amount']:.2f}", row["count"]])
    detail = sheet("审批明细", ["审批编号", "事由", "报销人", "报销人身份", "身份状态", "部门", "报销类别", "金额（元）", "提交时间（北京时间）", "审批状态"])
    for row in db.execute(detail_statement(filters).execution_options(yield_per=1000)):
        item = detail_dump(row, names)
        append(detail, [item["serial_number"], item["title"], item["applicant"], item["applicant_id"],
            "身份待补齐" if item["identity_pending"] else "", item["department"], item["expense_type"], f"{item['amount']:.2f}",
            (item["submitted_at"] + SHANGHAI_OFFSET).isoformat(sep=" ", timespec="seconds"), STATUS_LABELS.get(item["status"], item["status"])])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()
