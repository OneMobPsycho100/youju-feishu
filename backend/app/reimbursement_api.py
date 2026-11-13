"""报销统计只读 HTTP 接口，查询和导出使用相同的条件解析及业务服务。"""
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from . import reimbursement_schemas as s, reimbursement_statistics as statistics
from .auth import current_user
from .db import get_db
from .models import now
from .schemas import PageOut

router = APIRouter(prefix="/api/v1/reimbursement-statistics", tags=["报销统计"], dependencies=[Depends(current_user)])


def parse_filters(
    company_id: str = Query("", max_length=32, description="公司主体，空值表示全部；跨主体审批不拆分金额"),
    start_date: date | None = Query(None, description="北京时间起始日 YYYY-MM-DD，默认本月1日，包含当天"),
    end_date: date | None = Query(None, description="北京时间结束日 YYYY-MM-DD，默认今天，包含当天"),
    applicant_ids: Annotated[list[str] | None, Query(description="报销人身份，多选时重复传参；空列表为全部")] = None,
    expense_types: Annotated[list[str] | None, Query(description="类别，多选时重复传参；空列表为全部")] = None,
    status: s.ApprovalStatus = Query("APPROVED", description="最终审批状态，默认已通过；显式空字符串表示全部"),
    measure: s.Measure = Query("application", description="报销申请金额或预算消耗；默认保持原报销金额口径"),
    department: str = Query("", max_length=100, description="申请部门名称，空值表示全部；与人员条件取交集"),
    usage_state: s.UsageState = Query("", description="预算消耗状态；空值包括已使用、在途、冻结。申请金额口径不使用该条件"),
    budget_coverage: s.Coverage = Query("all", description="预算覆盖；消耗默认包括全部已归属报销，支持查看未匹配预算的明细"),
) -> statistics.StatisticsFilters:
    """所有入口共用默认值与范围校验，不能用 truthy 回退把显式全部变成已通过。"""
    today = (now() + timedelta(hours=8)).date()
    start, end = start_date or today.replace(day=1), end_date or today
    if start > end:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    if start.year < 2 or end.year >= 9999:
        raise HTTPException(422, "日期须在0002年至9998年之间")
    for values, maximum, label in ((applicant_ids, 220, "报销人"), (expense_types, 80, "报销类别")):
        if values and (len(values) > 1000 or any(not item or len(item) > maximum for item in values)):
            raise HTTPException(422, label + "筛选值格式不正确")
    return statistics.StatisticsFilters(company_id, start, end, tuple(dict.fromkeys(applicant_ids or [])),
        tuple(dict.fromkeys(expense_types or [])), status if measure == "application" else "", measure, department.strip(),
        usage_state if measure == "consumption" else "", budget_coverage if measure == "consumption" else "all")


@router.get("", response_model=s.StatisticsOut, summary="按提交时间、报销人和类别查询报销汇总及趋势")
def summary(filters=Depends(parse_filters), group_by: s.GroupBy = Query("expense_type", description="按类别、报销人或部门分组"), db=Depends(get_db)):
    return statistics.summary(db, filters, group_by)


@router.get("/options", response_model=s.StatisticsOptions, summary="查询公司范围内全部报销人和类别选项")
def options(company_id: str = Query("", max_length=32, description="公司主体，空值表示全部"),
            measure: s.Measure = Query("application", description="统计口径，默认报销金额"),
            department: str = Query("", max_length=100, description="人员和类别选项所在部门，空值不限"), db=Depends(get_db)):
    return statistics.options(db, company_id, measure, department.strip())


@router.get("/details", response_model=PageOut[s.StatisticsDetail], summary="分页读取当前筛选及指定分组的审批明细")
def details(filters=Depends(parse_filters), group_by: s.GroupBy | None = Query(None, description="下钻分组维度"),
            group_key: str | None = Query(None, max_length=220, description="汇总接口返回的分组 key，原样传入"),
            page: int = Query(1, ge=1, description="页码"), page_size: int = Query(20, ge=1, le=100, description="每页记录数"), db=Depends(get_db)):
    return statistics.details(db, filters, group_by, group_key, page, page_size)


@router.get("/export", summary="导出当前筛选的统计说明、分组汇总、趋势及全部审批明细")
def export(filters=Depends(parse_filters), group_by: s.GroupBy = Query("expense_type", description="导出的分组维度"), db=Depends(get_db)):
    content = statistics.export_workbook(db, filters, group_by)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="reimbursement-statistics-{filters.start_date}-{filters.end_date}.xlsx"'})
