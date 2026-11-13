"""预算公开契约：金额单位为元，以十进制字符串输出；日期按北京时间解释。"""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

PeriodType = Literal["MONTH", "QUARTER", "YEAR", "CUSTOM"]
UsageState = Literal["RESERVED", "USED", "FROZEN", "RELEASED"]
BudgetStatus = Literal["NORMAL", "WARNING", "EXHAUSTED", "EXCEEDED"]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Department = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
Reason = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=600)]
Quota = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]


class BudgetSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class BudgetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Name = Field(description="预算名称")
    company_id: str = Field(min_length=1, max_length=32, description="费用承担公司主体 ID")
    department: Department = Field(description="申请表单中的部门名称，去除首尾空白后精确匹配")
    expense_type: str = Field("", max_length=80, description="单一费用类别；空字符串为该部门全部费用")
    period_type: PeriodType = Field(description="月度、季度、年度或自定义周期")
    start_date: date = Field(description="北京时间周期起始日，包含当天")
    end_date: date = Field(description="北京时间周期结束日，包含当天")
    amount: Quota = Field(description="预算额度，单位元，大于零且最多两位小数")
    warning_threshold: int = Field(80, ge=1, le=100, description="占用预警百分比，默认为 80")
    notes: str = Field("", max_length=2000, description="预算备注")

    @field_validator("expense_type")
    @classmethod
    def trim_category(cls, value):
        return value.strip()

    @model_validator(mode="after")
    def valid_period(self):
        from calendar import monthrange
        if self.start_date > self.end_date or self.start_date.year < 2 or self.end_date.year >= 9999:
            raise ValueError("预算起止日期不正确，须在0002年至9998年之间")
        start, end = self.start_date, self.end_date
        if self.period_type == "MONTH" and (start.day != 1 or end != date(start.year, start.month, monthrange(start.year, start.month)[1])):
            raise ValueError("月度预算必须覆盖完整自然月")
        if self.period_type == "QUARTER" and (start.day != 1 or start.month not in (1, 4, 7, 10)
                or end != date(start.year, start.month + 2, monthrange(start.year, start.month + 2)[1])):
            raise ValueError("季度预算必须覆盖完整自然季度")
        if self.period_type == "YEAR" and (start != date(start.year, 1, 1) or end != date(start.year, 12, 31)):
            raise ValueError("年度预算必须覆盖完整自然年")
        return self


class BudgetEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, description="打开编辑窗口时的预算版本")
    name: Name | None = Field(None, description="预算名称；省略时保持原值")
    warning_threshold: int | None = Field(None, ge=1, le=100, description="预警百分比；省略时保持原值")
    notes: str | None = Field(None, max_length=2000, description="预算备注；省略时保持原值")
    archived: bool | None = Field(None, description="是否归档；归档后历史用量仍随权威状态更新")


class BudgetMutation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1, description="预算或用量快照的独立版本，防止使用旧页面覆盖变更")
    request_id: str = Field(min_length=8, max_length=100, description="本次操作幂等标识，网络重试必须复用")
    reason: Reason = Field(description="额度调整、归属确认或冻结释放的核查依据")


class BudgetAdjustment(BudgetMutation):
    amount: Quota = Field(description="调整后的预算总额度，单位元；不是增减差额")


class BudgetAttribution(BudgetMutation):
    company_id: str = Field(min_length=1, max_length=32, description="人工确认的整单预算主体，不修改票据主体")
    department: Department = Field(description="人工确认的整单申请部门，不进行跨主体或跨部门金额分摊")


class BudgetMetrics(BudgetSchema):
    used_amount: Decimal = Field(description="最终通过的报销金额合计，单位元")
    reserved_amount: Decimal = Field(description="审批中的在途占用金额，单位元")
    frozen_amount: Decimal = Field(description="最终通过后异常终止且尚未人工释放的金额，单位元")
    occupied_amount: Decimal = Field(description="已用、在途与冻结之和，单位元")
    available_amount: Decimal = Field(description="当前额度减总占用，允许为负数，单位元")
    exceeded_amount: Decimal = Field(description="超预算金额，可用余额为负时取其绝对值，否则为零")
    usage_percentage: Decimal = Field(description="总占用除以预算额度的百分比，允许超过100")
    budget_status: BudgetStatus = Field(description="正常、预警、额度用尽或超预算")
    usage_count: int = Field(description="纳入当前预算占用的报销单数，按审批ID去重")


class BudgetOut(BudgetCreate, BudgetMetrics):
    model_config = ConfigDict(from_attributes=True)
    id: str = Field(description="预算 ID")
    company_name: str = Field(description="公司主体名称")
    archived: bool = Field(description="是否归档")
    version: int = Field(description="预算当前版本")
    created_at: datetime = Field(description="创建时间，UTC")
    updated_at: datetime = Field(description="最近维护时间，UTC")


class BudgetCompleteness(BudgetSchema):
    pending_count: int = Field(description="已建账但归属或提交时间待确认的占用单数；主体未知记录也纳入提示")
    pending_amount: Decimal = Field(description="待确认单据当前金额，暂未扣减明确预算，单位元")
    backfill_remaining: int = Field(description="还未建立预算用量快照的历史报销数量")
    historical_review_count: int = Field(description="缺少历史通过快照、冻结金额待核对的报销数量")


class BudgetList(BudgetSchema):
    items: list[BudgetOut] = Field(description="当前页独立预算；不同预算金额不得叠加")
    total: int = Field(description="符合筛选的预算数量")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页记录数")
    warning_count: int = Field(description="符合筛选的预警预算数量")
    exceeded_count: int = Field(description="符合筛选的超预算数量")
    completeness: BudgetCompleteness = Field(description="当前主体范围内的数据完整性提示")


class BudgetUsageOut(BudgetSchema):
    approval_id: str = Field(description="飞书审批实例 ID")
    serial_number: str = Field(description="审批申请编号")
    title: str = Field(description="报销事由")
    applicant_id: str = Field(description="报销人稳定身份或历史分组键，同名人员独立统计")
    applicant: str = Field(description="报销人显示名称")
    company_id: str | None = Field(description="预算主体，待归属时可能为空")
    company_name: str = Field(description="预算主体名称")
    department: str = Field(description="预算申请部门")
    expense_type: str = Field(description="预算费用类别")
    submitted_at: datetime | None = Field(description="归入预算的申请提交时间，UTC；缺失时不计入周期")
    amount: Decimal = Field(description="本单预算用量，单位元")
    state: UsageState = Field(description="在途、已使用、冻结或已释放")
    attribution_pending: bool = Field(description="主体、部门或提交时间是否仍待确认")
    attribution_reason: str = Field(description="待归属原因或数据完整性说明")
    attribution_source: str = Field(description="AUTO 自动归属 / MANUAL 人工确认 / REVIEW 来源变化待确认")
    historical_review: bool = Field(description="历史冻结金额是否需要核对")
    version: int = Field(description="预算用量独立版本，人工归属和释放时回传")


class BudgetOperationOut(BudgetSchema):
    id: str = Field(description="预算操作记录 ID")
    budget_id: str | None = Field(description="关联预算 ID；归属与冻结释放为整单操作时可为空")
    approval_id: str | None = Field(description="关联报销审批 ID")
    action: str = Field(description="CREATE / EDIT / ADJUST / ATTRIBUTION / RELEASE")
    actor_name: str = Field(description="操作人姓名")
    reason: str = Field(description="操作依据")
    before: dict = Field(description="操作前的预算或用量快照，金额为十进制字符串")
    after: dict = Field(description="操作后的预算或用量快照")
    created_at: datetime = Field(description="操作时间，UTC")


class BudgetGroup(BudgetSchema):
    key: str = Field(description="稳定分组标识，人员使用身份而非姓名")
    name: str = Field(description="报销人或费用类别名称")
    amount: Decimal = Field(description="当前分组的总占用，单位元")
    count: int = Field(description="当前分组的报销单数")


class BudgetTrend(BudgetSchema):
    date: str = Field(description="提交日期 YYYY-MM-DD 或月份 YYYY-MM；展示当前用量按提交时间的分布")
    amount: Decimal = Field(description="该日期或月份提交单据的当前占用金额，单位元")


class BudgetDetail(BudgetOut):
    completeness: BudgetCompleteness = Field(description="主体范围数据完整性提示")
    applicants: list[BudgetGroup] = Field(description="按稳定报销人身份汇总的占用贡献")
    expense_types: list[BudgetGroup] = Field(description="按费用类别汇总的占用贡献")
    trend: list[BudgetTrend] = Field(description="按提交日期分布，90天以内按日，否则按月，空日期补零")


class BudgetContext(BudgetSchema):
    usage: BudgetUsageOut | None = Field(None, description="本单预算用量；历史尚未建账时为空")
    budgets: list[BudgetOut] = Field(default_factory=list, description="匹配本单的未归档独立预算；本单已经计入各自汇总，不再重复扣减")
    message: str = Field("", description="待补齐、未配置预算或只提醒的业务说明")


class BudgetOptions(BudgetSchema):
    departments: list[str] = Field(description="当前主体的已归属部门和已配置预算部门，可输入新名称")
    expense_types: list[str] = Field(description="当前主体的费用类别，可输入新类别")
