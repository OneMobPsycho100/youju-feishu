"""预算 HTTP 边界；所有写操作由服务端角色、独立版本及幂等标识共同约束。"""
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query, Response

from . import budget_schemas as s, budgets
from .auth import current_user, finance_user
from .db import get_db
from .schemas import PageOut

router = APIRouter(prefix="/api/v1", tags=["预算管理"], dependencies=[Depends(current_user)])


def parse_filters(
    company_id: str = Query("", max_length=32, description="公司主体；空字符串查询全部主体"),
    department: str = Query("", max_length=100, description="表单申请部门，精确匹配"),
    expense_type: str = Query("", max_length=80, description="费用类别，留空查询全部预算类别范围"),
    period_type: s.PeriodType | Literal[""] = Query("", description="预算周期类型，留空不限"),
    period_scope: Literal["current", "all"] = Query("current", description="默认只查覆盖北京时间今天的预算；all 不限制"),
    budget_status: s.BudgetStatus | Literal[""] = Query("", description="正常、预警、额度用尽、超预算；留空不限"),
    archived: Literal["active", "archived", "all"] = Query("active", description="未归档、已归档或全部"),
    start_date: date | None = Query(None, description="筛选与该日期以后相交的预算周期，包含当天"),
    end_date: date | None = Query(None, description="筛选与该日期以前相交的预算周期，包含当天"),
    q: str = Query("", max_length=150, description="预算名称关键词"),
    page: int = Query(1, ge=1, description="页码，从1开始"),
    page_size: int = Query(20, ge=1, le=100, description="每页记录数，最多100"),
):
    if start_date and end_date and start_date > end_date:
        from fastapi import HTTPException
        raise HTTPException(422, "筛选开始日期不能晚于结束日期")
    return dict(company_id=company_id, department=department, expense_type=expense_type, period_type=period_type,
                period_scope=period_scope, budget_status=budget_status, archived=archived,
                start_date=start_date, end_date=end_date, q=q, page=page, page_size=page_size)


@router.get("/budgets", response_model=s.BudgetList, summary="查询独立预算及使用进度，不叠加重叠预算金额")
def list_budgets(filters=Depends(parse_filters), db=Depends(get_db)):
    return budgets.list_budgets(db, **filters)


@router.get("/budgets/options", response_model=s.BudgetOptions, summary="读取主体范围内的预算部门和费用类别")
def options(company_id: str = Query("", max_length=32, description="公司主体，空值表示全部"), db=Depends(get_db)):
    return budgets.options(db, company_id)


@router.get("/budgets/usages", response_model=PageOut[s.BudgetUsageOut], summary="查询待归属或冻结等整单预算用量")
def usages(company_id: str = Query("", max_length=32, description="主体；包含无法确定主体的待归属记录"),
           pending_only: bool = Query(False, description="仅查归属或提交时间待补齐的占用"),
           state: s.UsageState | Literal[""] = Query("", description="用量状态；留空表示全部"),
           department: str = Query("", max_length=100, description="申请部门，精确匹配"),
           page: int = Query(1, ge=1, description="页码"), page_size: int = Query(20, ge=1, le=100, description="每页记录数"), db=Depends(get_db)):
    return budgets.usage_details(db, company_id=company_id, pending_only=pending_only, state=state, department=department, page=page, page_size=page_size)


@router.post("/budgets", response_model=s.BudgetOut, summary="创建预算，自动统计所选周期内已有报销")
def create_budget(data: s.BudgetCreate, user=Depends(finance_user), db=Depends(get_db)):
    return budgets.create_budget(db, data, user)


@router.get("/budgets/{budget_id}", response_model=s.BudgetDetail, summary="读取预算指标、人员类别贡献及提交日期分布")
def detail(budget_id: str, db=Depends(get_db)):
    return budgets.budget_detail(db, budget_id)


@router.patch("/budgets/{budget_id}", response_model=s.BudgetOut, summary="维护名称、备注、预警阈值或归档状态，范围和周期固定")
def edit_budget(budget_id: str, data: s.BudgetEdit, user=Depends(finance_user), db=Depends(get_db)):
    return budgets.edit_budget(db, budget_id, data, user)


@router.post("/budgets/{budget_id}/adjustments", response_model=s.BudgetOut, summary="幂等调整预算额度并保留调整依据")
def adjust_budget(budget_id: str, data: s.BudgetAdjustment, user=Depends(finance_user), db=Depends(get_db)):
    return budgets.adjust_budget(db, budget_id, data, user)


@router.get("/budgets/{budget_id}/details", response_model=PageOut[s.BudgetUsageOut], summary="按状态、报销人或类别下钻预算报销明细")
def details(budget_id: str, state: s.UsageState | Literal[""] = Query("", description="用量状态，留空包括已释放记录"),
            applicant_id: str = Query("", max_length=220, description="贡献汇总返回的报销人稳定身份，不能传显示名"),
            expense_type: str = Query("", max_length=80, description="贡献汇总返回的费用类别"),
            page: int = Query(1, ge=1, description="页码"), page_size: int = Query(20, ge=1, le=100, description="每页记录数"), db=Depends(get_db)):
    return budgets.usage_details(db, budget_id=budget_id, state=state, applicant_id=applicant_id, expense_type=expense_type, page=page, page_size=page_size)


@router.get("/budgets/{budget_id}/history", response_model=PageOut[s.BudgetOperationOut], summary="读取预算维护、归属确认及冻结释放操作记录")
def history(budget_id: str, page: int = Query(1, ge=1, description="页码"),
            page_size: int = Query(20, ge=1, le=100, description="每页记录数"), db=Depends(get_db)):
    return budgets.operations(db, budget_id, page, page_size)


@router.get("/budgets/{budget_id}/export", summary="导出预算说明、金额汇总、完整报销明细和操作记录")
def export(budget_id: str, db=Depends(get_db)):
    return Response(budgets.export_workbook(db, budget_id), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="budget-{budget_id}.xlsx"'})


@router.post("/approvals/{approval_id}/budget-attribution", response_model=s.BudgetUsageOut, summary="幂等确认整单预算主体和部门，不修改原审批表单")
def attribution(approval_id: str, data: s.BudgetAttribution, user=Depends(finance_user), db=Depends(get_db)):
    return budgets.confirm_attribution(db, approval_id, data, user)


@router.post("/approvals/{approval_id}/budget-release", response_model=s.BudgetUsageOut, summary="核对后整单释放所有匹配预算的冻结额度，并保留依据")
def release(approval_id: str, data: s.BudgetMutation, user=Depends(finance_user), db=Depends(get_db)):
    return budgets.release_frozen(db, approval_id, data, user)
