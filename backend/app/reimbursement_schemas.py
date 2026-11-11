"""报销统计公开契约；金额为元的十进制字符串，日期按北京时间解释。"""
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import Field

from .schemas import Schema

GroupBy = Literal["expense_type", "applicant"]
ApprovalStatus = Literal["", "APPROVED", "PENDING", "REJECTED", "CANCELED", "REVERTED", "DELETED"]


class StatisticsScope(Schema):
    company_id: str = Field(description="公司主体；空字符串表示全部主体，跨主体单据不拆分金额")
    start_date: date = Field(description="北京时间开始日期，包含当天")
    end_date: date = Field(description="北京时间结束日期，包含当天")
    applicant_ids: list[str] = Field(description="报销人身份/旧记录分组键，多选，空列表表示全部")
    expense_types: list[str] = Field(description="报销类别，多选，空列表表示全部")
    status: str = Field(description="飞书最终实例状态，默认 APPROVED；空字符串表示全部，不按预审结论筛选")


class ApplicantOption(Schema):
    value: str = Field(description="筛选和分组使用的稳定身份；未补齐的旧记录使用 legacy:姓名")
    label: str = Field(description="优先本地姓名，其次原申请人信息或身份编号")
    identity_pending: bool = Field(description="是否尚缺飞书人员身份，同名旧记录暂按原申请人字段归组")


class StatisticsOptions(Schema):
    applicants: list[ApplicantOption] = Field(description="当前公司范围内的报销人；不受日期和状态筛选影响")
    expense_types: list[str] = Field(description="当前公司范围内的全部报销类别")


class StatisticsTotals(Schema):
    amount: Decimal = Field(description="筛选范围内报销申请金额合计，单位元，保留两位小数")
    count: int = Field(description="报销申请单数；每张审批只统计一次")
    applicant_count: int = Field(description="不同报销人身份/历史分组键数量")
    expense_type_count: int = Field(description="不同报销类别数量")


class StatisticsGroup(Schema):
    key: str = Field(description="分组标识，查看明细时原样传回，不能用显示名称代替")
    name: str = Field(description="类别名称或报销人显示名称")
    amount: Decimal = Field(description="当前分组申请总额，元")
    count: int = Field(description="当前分组审批单数")
    percentage: Decimal = Field(description="分组金额占当前筛选总额的百分数，0 到 100，总额为零时为零")
    identity_pending: bool = Field(False, description="按报销人分组时是否尚缺身份")


class StatisticsTrend(Schema):
    date: str = Field(description="北京时间日期 YYYY-MM-DD 或月份 YYYY-MM，空周期补零")
    amount: Decimal = Field(description="周期内申请金额，元")
    count: int = Field(description="周期内申请单数")


class StatisticsOut(Schema):
    scope: StatisticsScope = Field(description="本次实际使用的筛选条件")
    timezone: Literal["Asia/Shanghai"] = Field("Asia/Shanghai", description="统计日期和导出日期的时区")
    group_by: GroupBy = Field(description="expense_type 按类别；applicant 按报销人")
    totals: StatisticsTotals = Field(description="当前筛选下的总指标")
    groups: list[StatisticsGroup] = Field(description="全部分组，金额降序；图表仅展示前十项，表格分页展示全部")
    granularity: Literal["day", "month"] = Field(description="不超过90天按日，超过90天按月")
    trend: list[StatisticsTrend] = Field(description="补零后的完整时间趋势")
    missing_submitted_count: int = Field(description="符合其他筛选条件、因缺少提交时间未纳入日期统计的单数")
    pending_identity_count: int = Field(description="已纳入统计但人员身份尚待补齐的单数")
    demo_time_simulated: bool = Field(description="是否为演示模式；演示单提交时间为明确模拟数据")


class StatisticsDetail(Schema):
    id: str = Field(description="审批实例 ID，复用现有审批详情读取")
    serial_number: str = Field(description="审批编号，按文本保留")
    title: str = Field(description="报销事由")
    applicant_id: str = Field(description="报销人筛选身份/历史分组键")
    applicant: str = Field(description="报销人显示名称")
    identity_pending: bool = Field(description="报销人身份是否尚待补齐")
    department: str = Field(description="申请部门")
    expense_type: str = Field(description="报销类别")
    amount: Decimal = Field(description="报销总额，元")
    submitted_at: datetime = Field(description="申请提交时间，UTC，展示时转换为北京时间")
    status: str = Field(description="飞书审批实例状态")
