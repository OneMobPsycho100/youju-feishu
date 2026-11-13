"""付款统计公开契约。金额以元的十进制字符串传输，日期按北京时间解释。"""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .schemas import Schema

PaymentGroupBy = Literal["project", "department", "payment_method"]
PaymentDetailScope = Literal["period", "cumulative", "missing_approved_at"]


class PaymentStatisticsScope(Schema):
    company_id: str = Field(description="付款表单匹配的主体 ID，空字符串表示全部主体")
    start_date: date = Field(description="北京时间区间起始日，包含当天，仅用于区间统计")
    end_date: date = Field(description="北京时间区间与累计的共同截止日，包含当天")
    project_keys: list[str] = Field(description="项目选项返回的分组键，多个键取并集，空列表表示全部项目")


class PaymentProjectOption(Schema):
    key: str = Field(description="付款主体和项目组成的分组键，筛选时原样传回")
    name: str = Field(description="审批表单中的项目名称，空值显示为未填写项目")
    company_name: str = Field(description="付款主体显示名，未匹配时展示原申报主体及匹配状态")


class PaymentStatisticsOptions(Schema):
    projects: list[PaymentProjectOption] = Field(description="主体范围内已同步付款申请的项目选项，含审批中及历史项目")


class PaymentStatisticsTotals(Schema):
    period_amount: Decimal = Field(description="所选起止日内的已支付金额，元，保留两位小数")
    cumulative_amount: Decimal = Field(description="相同主体和项目截至结束日的已支付金额，元，从已同步历史累计")
    count: int = Field(description="所选起止日内最终通过的付款审批单数")
    project_count: int = Field(description="所选起止日内涉及的已命名项目数，同名跨主体分别计数")


class PaymentStatisticsGroup(Schema):
    key: str = Field(description="所选维度的分组键，下钻时原样传回，空字符串为未填写部门或付款方式")
    name: str = Field(description="分组显示名称")
    company_name: str | None = Field(description="项目所属的付款主体显示名，仅项目分组提供")
    amount: Decimal = Field(description="该分组的区间已支付金额，元，保留两位小数")
    count: int = Field(description="该分组的区间已支付审批单数")
    percentage: Decimal = Field(description="占所选区间总金额的百分比，保留两位小数，总金额为零时返回零")


class PaymentStatisticsTrend(Schema):
    date: str = Field(description="北京时间的自然日 YYYY-MM-DD 或月份 YYYY-MM，首尾月份按区间裁剪")
    amount: Decimal = Field(description="该日或月的区间已支付金额，元，保留两位小数")
    count: int = Field(description="该日或月的区间已支付审批单数")


class PaymentStatisticsGroups(Schema):
    project: list[PaymentStatisticsGroup] = Field(description="按付款主体及项目汇总，按金额降序")
    department: list[PaymentStatisticsGroup] = Field(description="按申请部门汇总，按金额降序")
    payment_method: list[PaymentStatisticsGroup] = Field(description="按付款方式汇总，按金额降序")


class PaymentStatisticsOut(Schema):
    scope: PaymentStatisticsScope = Field(description="实际采用的查询条件")
    timezone: Literal["Asia/Shanghai"] = Field(description="日期统计时区")
    totals: PaymentStatisticsTotals = Field(description="当前主体和项目筛选下的区间与累计指标")
    groups: PaymentStatisticsGroups = Field(description="项目、申请部门和付款方式的完整区间汇总")
    project_ranking: list[PaymentStatisticsGroup] = Field(description="区间内按金额降序排列的前十个项目分组，包含未填写项目，跨主体分别排名")
    granularity: Literal["day", "month"] = Field(description="区间含起止日在内不超过 90 天按日，否则按月")
    trend: list[PaymentStatisticsTrend] = Field(description="区间内按最终通过日期排列的趋势，空周期补零")
    missing_approved_count: int = Field(description="符合主体和项目筛选但缺最终通过时间的已通过单数，未计入日期指标")


class PaymentStatisticsDetail(Schema):
    id: str = Field(description="飞书审批实例编号，用于打开原审批详情")
    serial_number: str = Field(description="飞书审批流水号")
    title: str = Field(description="付款事由或审批标题")
    applicant: str = Field(description="付款申请人显示名称")
    department: str = Field(description="申请部门显示名称")
    project: str = Field(description="审批表单中所属项目的显示名称")
    company_name: str = Field(description="付款主体显示名")
    payment_method: str = Field(description="审批表单中付款方式的显示名称")
    amount: Decimal = Field(description="付款申请金额，元，保留两位小数")
    approved_at: datetime | None = Field(description="飞书最终通过时间，UTC。待补齐明细中的时间为空")
