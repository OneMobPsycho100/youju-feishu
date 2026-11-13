"""Agent 的公开契约与工具输入；金额和票号沿用业务接口，不接受执行代码。"""
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .schemas import JobOut, Schema


class AgentContext(BaseModel):
    """发送时冻结的页面上下文；这是查询默认值，不是授权来源。"""
    model_config = ConfigDict(extra="forbid")
    route: str = Field("overview", max_length=150, description="发送时所在的工作模块")
    company_id: str = Field("", max_length=32, description="公司筛选；空值表示全部主体")
    approval_id: str = Field("", max_length=64, description="当前打开的审批 ID")
    selection_type: Literal["", "approvals", "invoices", "archives", "archived_approvals", "budgets"] = Field("",
        description="选中记录类型；archived_approvals 的 ID 是审批实例，archives 是旧原件归档记录")
    selected_ids: list[str] = Field(default_factory=list, max_length=20, description="用户选中的记录，最多二十条")
    filters: dict[str, str] = Field(default_factory=dict, description="页面当前搜索、状态、月份及业务类型；仅接受已知筛选字段")

    @field_validator("selected_ids")
    @classmethod
    def valid_ids(cls, value):
        if any(not item or len(item) > 100 for item in value):
            raise ValueError("记录 ID 不合法")
        return list(dict.fromkeys(value))

    @field_validator("filters")
    @classmethod
    def valid_filters(cls, value):
        allowed = {"q", "decision", "status", "month", "approval_month", "business_type",
                   "month_basis", "invoice_month", "scope", "archive_status", "department", "expense_type",
                   "period_type", "period_scope", "budget_status", "archived", "start_date", "end_date"}
        if set(value) - allowed or any(len(item) > 200 for item in value.values()):
            raise ValueError("筛选字段不合法")
        return value


class ConversationInput(BaseModel):
    title: str = Field("新对话", min_length=1, max_length=100, description="会话标题；首次发送时可自动取首句")

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value.strip()


class AgentMessageInput(BaseModel):
    text: str = Field("", max_length=8000, description="本轮用户指令，最多八千字；仅附件发送时使用默认分析指令")
    request_id: str = Field(min_length=8, max_length=100, description="消息幂等标识；网络重试必须复用")
    context: AgentContext = Field(default_factory=AgentContext, description="本轮页面上下文快照")
    attachment_ids: list[str] = Field(default_factory=list, max_length=5, description="当前私人会话中已解析的附件 ID，最多五个；重发复用原 ID")

    @model_validator(mode="after")
    def normalize_message(self):
        self.text = self.text.strip()
        if len(set(self.attachment_ids)) != len(self.attachment_ids) or any(len(item) != 32 for item in self.attachment_ids):
            raise ValueError("附件标识重复或格式不正确")
        if not self.text and not self.attachment_ids:
            raise ValueError("消息不能为空")
        if not self.text:
            self.text = "请分析附件；如为发票，请检查是否已被使用并说明依据"
        return self


class AgentAttachmentOut(Schema):
    """公开附件投影不含磁盘路径和正文；正文只通过会话限定的读取工具分页返回。"""
    id: str = Field(description="私人附件 ID")
    conversation_id: str = Field(description="所属私人会话")
    upload_id: str = Field(description="客户端上传幂等标识，用于断线后合并同一附件")
    filename: str = Field(description="规范化后的原件名称")
    mime: str = Field(description="根据文件签名识别的 MIME")
    size: int = Field(description="原件字节数")
    status: Literal["QUEUED", "PROCESSING", "READY", "FAILED"] = Field(description="排队、解析中、就绪或失败")
    error: str = Field(description="可公开的解析失败原因或不完整提示")
    submitted: bool = Field(description="已发送附件随会话保留，未发送附件可移除")
    created_at: datetime = Field(description="上传时间，UTC")
    updated_at: datetime = Field(description="最近状态变化，UTC")


class ConfirmInput(BaseModel):
    original_checked: bool = Field(False, description="票面纠正时须明确确认已核对原件；其他操作忽略")


class AgentMessageOut(Schema):
    id: str = Field(description="消息 ID，也是流式增量的稳定目标")
    conversation_id: str = Field(description="所属私人会话")
    run_id: str | None = Field(description="关联运行；系统消息可以为空")
    role: Literal["user", "assistant", "tool", "system"] = Field(description="消息类型")
    content: str = Field(description="脱敏后的消息正文；前端禁止执行其中的 HTML")
    data: dict[str, Any] = Field(description="上下文、工具结果或阶段状态")
    created_at: datetime = Field(description="创建时间，UTC")


class AgentRunOut(Schema):
    id: str = Field(description="本轮运行 ID")
    conversation_id: str = Field(description="所属会话")
    user_message_id: str = Field(description="触发本轮的用户消息")
    status: Literal["RUNNING", "COMPLETED", "FAILED", "CANCELED", "INTERRUPTED"] = Field(description="运行状态")
    stage: str = Field(description="当前可见阶段，不代表模型隐藏推理")
    model_name: str = Field(description="本轮使用的模型")
    config_version: int = Field(description="启动时模型配置版本")
    error: str = Field(description="可公开的中文错误原因")
    usage: dict[str, Any] = Field(description="供应商明确返回的 Token 用量")
    started_at: datetime = Field(description="启动时间，UTC")
    finished_at: datetime | None = Field(description="结束时间，UTC")


class ConversationOut(Schema):
    id: str = Field(description="本人会话 ID")
    title: str = Field(description="会话标题")
    event_seq: int = Field(description="本快照已包含的最大事件游标")
    created_at: datetime = Field(description="创建时间，UTC")
    updated_at: datetime = Field(description="最近更新时间，UTC")


class ProposalOut(Schema):
    id: str = Field(description="不可变操作提案 ID，确认接口不接受新的业务参数")
    run_id: str = Field(description="生成提案的运行")
    kind: str = Field(description="业务操作类型")
    target_id: str = Field(description="审批或任务的真实 ID")
    status: str = Field(description="PENDING / SUBMITTED / CANCELED / EXPIRED / INVALID")
    display: dict[str, Any] = Field(description="服务端生成的操作对象、影响、依据和字段差异")
    error: str = Field(description="失效或执行错误说明")
    job: JobOut | None = Field(description="已提交任务及其真实状态；提交不等于完成")
    operation_status: str = Field(description="审批回写的真实状态，UNKNOWN 表示正在对账")
    expires_at: datetime = Field(description="确认有效期，UTC")
    created_at: datetime = Field(description="生成时间，UTC")


class ConversationDetail(ConversationOut):
    messages: list[AgentMessageOut] = Field(description="按时间排列的消息")
    runs: list[AgentRunOut] = Field(description="本会话的运行及终止状态")
    proposals: list[ProposalOut] = Field(description="本会话提案与最新业务任务状态")
    attachments: list[AgentAttachmentOut] = Field(default_factory=list, description="本会话可访问的附件，含未发送草稿")
    attachment_usage: dict[str, int] = Field(default_factory=dict, description="本会话附件识别的供应商用量；不与对话生成重复计数")


class CapabilitiesOut(BaseModel):
    available: bool = Field(description="模型启用且连接配置完整；实际工具支持在调用时验证")
    reason: str = Field(description="不可用原因或就绪提示")
    model_name: str = Field(description="模型名称，不含连接密钥")
    can_write: bool = Field(description="当前用户是否可以确认日常业务操作")
    can_sync: bool = Field(description="当前用户是否可发起真实飞书补偿同步")
    actions: list[str] = Field(description="可提出的写操作类型")
    max_selected: int = Field(20, description="单次候选记录数量上限")
    active_conversation_id: str = Field("", description="本人正在运行的会话，空值表示没有运行")
    active_run_id: str = Field("", description="本人正在运行的轮次 ID")
    max_attachments: int = Field(5, description="每轮及未发送草稿的附件上限")
    max_attachment_bytes: int = Field(20 * 1024 * 1024, description="单附件字节上限")
    attachment_extensions: list[str] = Field(default_factory=list, description="支持的附件扩展名，包含点号")


class ReadAttachmentInput(BaseModel):
    """只接受当前会话已发送的附件 ID，不能传入文件路径或 URL。"""
    model_config = ConfigDict(extra="forbid")
    attachment_id: str = Field(min_length=32, max_length=32, description="本会话已发送的附件 ID")
    offset: int = Field(0, ge=0, description="正文字符游标；继续读取使用上次返回的 next_offset")
    limit: int = Field(12000, ge=100, le=20000, description="本次最多返回的正文字符数，保留原件定位")


class CheckInvoiceUsageInput(BaseModel):
    """只查询原件识别及全企业业务记录，不接受模型填写或猜测票号。"""
    model_config = ConfigDict(extra="forbid")
    attachment_id: str = Field(min_length=32, max_length=32, description="本会话已发送的发票附件 ID")


class UsageSummaryOut(BaseModel):
    """只返回聚合数据；全局统计不包含其他账号的会话标识或正文。"""
    plugin: str = Field(description="实际启用的 Harness 计量插件")
    timezone: str = Field(description="按日聚合的时区，固定为北京时间")
    days: int = Field(description="统计最近的日历天数")
    scope: Literal["mine", "all"] = Field(description="本人业务对话或管理员全局模型调用")
    calls: int = Field(description="所选范围的运行与调用记录数量")
    reported: int = Field(description="至少返回一份供应商用量的记录数")
    missing: int = Field(description="没有或未完整返回用量的记录数")
    totals: dict[str, int] = Field(description="已报告用量之和；完整输入包含缓存读写，推理不另加到输出")
    coverage: dict[str, int] = Field(description="各个统计字段有数据的记录数，避免把缺失字段当成零")
    cache_hit_rate: float | None = Field(description="有完整输入及缓存读取的相同样本中的命中率，缺失时为空")
    daily: list[dict[str, Any]] = Field(description="按北京时间日期的输入、输出、总量和调用数")
    models: list[dict[str, Any]] = Field(description="按模型汇总的输入、输出、总量和调用数")
    sources: list[dict[str, Any]] = Field(description="业务对话、审批审核和连接测试的分项汇总")


class QueryInput(BaseModel):
    """白名单数据检索；不接受 SQL、接口地址或任意查询表达式。"""
    model_config = ConfigDict(extra="forbid")
    entity: Literal["overview", "companies", "approvals", "approval", "invoices", "archives", "archived_approvals", "jobs", "job", "budgets", "budget", "budget_usages"] = Field(
        description="查询对象；budgets 预算台账，budget 单项预算和贡献，budget_usages 预算报销明细；archived_approvals 整单归档")
    target_id: str = Field("", max_length=100, description="单条审批、任务或预算的真实ID；预算明细传预算ID，审批和预算可省略以引用当前打开记录")
    company_id: str | None = Field(None, max_length=32, description="省略继承本轮公司；明确空字符串查询全部主体")
    q: str = Field("", max_length=200, description="事由、编号、申请人或票号关键词")
    decision: Literal["", "PENDING", "PASS", "REVIEW", "BLOCK", "COLLECT"] = Field("", description="预审结论筛选；COLLECT 查询付款仅采集，其他结论不含仅采集付款")
    status: str = Field("", max_length=30, description="审批、归档或后台任务状态")
    business_type: Literal["", "REIMBURSEMENT", "PAYMENT"] = Field("", description="审批业务类型；空值全部，REIMBURSEMENT 报销，PAYMENT 付款")
    month: str = Field("", max_length=7, description="归档开票月份 YYYY-MM")
    approval_month: str = Field("", max_length=7, description="归档审批月份 YYYY-MM，空值不限；旧原件按申请提交月")
    month_basis: Literal["approved", "submitted"] = Field("approved", description="整单归档按最终通过月或提交月，均为北京时间")
    invoice_month: str = Field("", max_length=7, description="整单归档的开票月份 YYYY-MM")
    scope: Literal["approved", "revoked"] = Field("approved", description="整单归档查询最终通过或撤销留痕")
    archive_status: Literal["", "ARCHIVED", "PARTIAL", "FAILED", "NONE", "QUARANTINED", "REVOKED"] = Field("",
        description="整单归档状态；空值不限")
    page: int = Field(1, ge=1, le=10000, description="页码；从一开始")
    page_size: int = Field(10, ge=1, le=20, description="返回条数，最多二十")
    ids: list[str] = Field(default_factory=list, max_length=20, description="按真实 ID 查询页面选中的审批、发票或归档")
    department: str = Field("", max_length=100, description="预算的表单申请部门名称，精确匹配")
    expense_type: str = Field("", max_length=80, description="预算费用类别，留空不限")
    period_type: Literal["", "MONTH", "QUARTER", "YEAR", "CUSTOM"] = Field("", description="预算周期类型，留空不限")
    period_scope: Literal["current", "all"] = Field("current", description="预算默认覆盖今天；历史和未来预算使用all")
    budget_status: Literal["", "NORMAL", "WARNING", "EXHAUSTED", "EXCEEDED"] = Field("", description="预算使用状态，EXCEEDED为超预算")
    archived: Literal["active", "archived", "all"] = Field("active", description="预算归档范围")
    start_date: date | None = Field(None, description="预算相交周期筛选起始日期，北京时间")
    end_date: date | None = Field(None, description="预算相交周期筛选结束日期，北京时间")
    usage_state: Literal["", "RESERVED", "USED", "FROZEN", "RELEASED"] = Field("", description="预算明细占用状态")
    applicant_id: str = Field("", max_length=220, description="预算人员贡献返回的稳定身份，用于明细下钻")

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def empty_budget_date(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def valid_budget_dates(self):
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("预算筛选开始日期不能晚于结束日期")
        return self


class CorrectionDraft(BaseModel):
    """人工核对用的完整票面草稿；审批版本由服务端读取，不由模型指定。"""
    model_config = ConfigDict(extra="forbid")
    number: str = Field(min_length=1, max_length=40, description="发票号码，字符串保留前导零")
    code: str = Field("", max_length=20, description="传统发票代码，数电票为空")
    buyer_name: str = Field(min_length=1, max_length=150, description="购买方名称")
    buyer_tax_id: str = Field(min_length=15, max_length=20, description="购买方税号")
    seller_name: str = Field("", max_length=150, description="销售方名称")
    issue_date: date = Field(description="开票日期")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, description="价税合计，单位元")


class ProposalInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["AUDIT", "ARCHIVE", "APPROVE", "REJECT", "CORRECT", "SYNC", "RETRY"] = Field(description="仅提出待确认的业务方案，不直接执行")
    target_id: str = Field("", max_length=100, description="真实审批或失败任务 ID；同步时为空")
    comment: str = Field("", max_length=600, description="同意、拒绝或票面纠正的处理依据，至少二字")
    attachment_id: str = Field("", max_length=32, description="票面纠正对应的当前附件 ID")
    correction: CorrectionDraft | None = Field(None, description="纠正票面时必填完整字段，必须由用户核对原件")


class DownloadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["invoice_export", "attachment", "archive_zip", "approval_archive_zip"] = Field(
        description="台账导出、原件下载、旧归档原件打包或按审批 ID 整单打包")
    company_id: str | None = Field(None, max_length=32, description="台账导出主体，省略继承本轮；导出该主体全部台账")
    ids: list[str] = Field(default_factory=list, max_length=20, description="真实附件或归档记录 ID，最多二十条")
