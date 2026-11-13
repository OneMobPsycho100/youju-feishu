"""付款统计只读接口。汇总、明细和报表共用筛选默认值及校验。"""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from . import payment_statistics as statistics, payment_statistics_schemas as s
from .auth import current_user
from .db import get_db
from .models import now
from .schemas import PageOut


router = APIRouter(prefix="/api/v1/payment-statistics", tags=["付款统计"], dependencies=[Depends(current_user)])


def parse_filters(
    company_id: str = Query("", max_length=32, description="付款表单匹配的主体 ID，空字符串表示全部主体"),
    start_date: date | None = Query(None, description="北京时间起始日 YYYY-MM-DD，包含当天，默认为结束日所在月的 1 日"),
    end_date: date | None = Query(None, description="北京时间截止日 YYYY-MM-DD，包含当天，默认为今天"),
    project_keys: Annotated[list[str] | None, Query(max_length=1000, description="选项接口返回的项目分组键，重复传参为多选，省略则为全部项目")] = None,
) -> statistics.PaymentFilters:
    end = end_date or (now() + statistics.SHANGHAI_OFFSET).date()
    start = start_date or end.replace(day=1)
    if start > end:
        raise HTTPException(422, "开始日期不能晚于结束日期")
    if start.year < 2 or end.year >= 9999:
        raise HTTPException(422, "日期须在0002年至9998年之间")
    if project_keys and any(not key for key in project_keys):
        raise HTTPException(422, "项目分组键不能为空")
    return statistics.PaymentFilters(company_id, start, end, tuple(dict.fromkeys(project_keys or [])))


@router.get("", response_model=s.PaymentStatisticsOut, summary="查询付款区间与累计指标、项目排行、趋势和分组汇总")
def summary(filters=Depends(parse_filters), db=Depends(get_db)):
    return statistics.summary(db, filters)


@router.get("/options", response_model=s.PaymentStatisticsOptions, summary="查询指定主体内的可选项目")
def options(company_id: str = Query("", max_length=32, description="付款主体 ID，空字符串表示全部，与日期和审批状态无关"),
            db=Depends(get_db)):
    return statistics.options(db, company_id)


@router.get("/details", response_model=PageOut[s.PaymentStatisticsDetail], summary="分页查询付款区间、累计或通过时间待补齐明细")
def details(filters=Depends(parse_filters),
            scope: s.PaymentDetailScope = Query("period", description="区间使用首尾日，累计只使用截止日，待补齐按主体和项目查缺少通过时间的审批"),
            group_by: s.PaymentGroupBy | None = Query(None, description="分组维度，与分组键一起提供，省略时查询整个范围"),
            group_key: str | None = Query(None, description="汇总返回的分组键，原样传入，包括表示未填写的空字符串"),
            page: int = Query(1, ge=1, description="页码"),
            page_size: int = Query(20, ge=1, le=100, description="每页单数"), db=Depends(get_db)):
    return statistics.details(db, filters, scope, group_by, group_key, page, page_size)


@router.get("/export", summary="导出当前付款筛选的口径、指标、分组、趋势和完整区间明细")
def export(filters=Depends(parse_filters),
           group_by: s.PaymentGroupBy = Query("project", description="分组汇总使用的维度，默认按项目"), db=Depends(get_db)):
    content = statistics.export_workbook(db, filters, group_by)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={
        "Content-Disposition": f'attachment; filename="payment-statistics-{filters.start_date}-{filters.end_date}.xlsx"',
    })
