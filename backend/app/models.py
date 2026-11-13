"""业务模型。审批状态、审核结论、发票占用和归档状态互相独立。"""
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base
from .money import Money


def uid() -> str:
    return uuid4().hex


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Timestamped:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class User(Base, Timestamped):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="本应用内的飞书 open_id")
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(16), default="viewer", comment="admin / finance / viewer")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class FeishuConfig(Base, Timestamped):
    """单企业接入配置；密钥和初始化草稿不以明文存储。"""
    __tablename__ = "feishu_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False, comment="首次初始化后不再读取旧飞书环境变量")
    status: Mapped[str] = mapped_column(String(24), default="UNCONFIGURED", comment="UNCONFIGURED / ACTIVE")
    app_id: Mapped[str] = mapped_column(String(100), default="")
    app_secret_encrypted: Mapped[str] = mapped_column(Text, default="")
    tenant_key: Mapped[str] = mapped_column(String(100), default="")
    public_url: Mapped[str] = mapped_column(String(2048), default="")
    event_transport: Mapped[str] = mapped_column(String(16), default="websocket")
    encrypt_key_encrypted: Mapped[str] = mapped_column(Text, default="")
    verification_token_encrypted: Mapped[str] = mapped_column(Text, default="")
    initial_admin_ids: Mapped[list] = mapped_column(JSON, default=list, comment="仅兼容旧部署的首次管理员绑定，之后由工作台用户角色管理")
    imported_from_env: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=0, comment="配置乐观锁")
    connection_version: Mapped[int] = mapped_column(Integer, default=0, comment="凭据或事件方式改变时递增")
    credential_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receiver_status: Mapped[str] = mapped_column(String(24), default="WAITING")
    receiver_applied_version: Mapped[int] = mapped_column(Integer, default=0)
    receiver_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    receiver_error: Mapped[str] = mapped_column(String(500), default="")
    last_event_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SetupAccess(Base, Timestamped):
    """初始化/恢复授权摘要与短时草稿；原始安装码只交给部署负责人。"""
    __tablename__ = "setup_access"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code_hash: Mapped[str] = mapped_column(String(64), default="")
    generation: Mapped[str] = mapped_column(String(64), default="")
    purpose: Mapped[str] = mapped_column(String(16), default="INIT")
    code_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    draft_encrypted: Mapped[str] = mapped_column(Text, default="")
    draft_owner: Mapped[str] = mapped_column(String(64), default="")
    draft_version: Mapped[int] = mapped_column(Integer, default=0)
    draft_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Company(Base, Timestamped):
    __tablename__ = "companies"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    tax_id: Mapped[str] = mapped_column(String(32), unique=True, comment="购买方税号，主体归类主键")
    aliases: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class InvoiceDeductionConfig(Base):
    """每个购买方主体独立维护抵扣规则，整组配置使用同一个乐观锁版本。"""
    __tablename__ = "invoice_deduction_configs"
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", comment="配置乐观锁版本")
    rules: Mapped[list[dict]] = mapped_column(JSON, default=list, server_default="[]", comment="票种、费用类型和抵扣方式")


class ModelConfig(Base, Timestamped):
    """全局单例。managed=False 时沿用部署环境，保存后整份配置由数据库管理。"""
    __tablename__ = "model_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否已在工作台保存配置
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否启用模型语义审核
    base_url: Mapped[str] = mapped_column(String(2048), default="", comment="兼容流式 Chat Completions 的 API 根地址")
    model_name: Mapped[str] = mapped_column(String(200), default="", comment="供应商模型 ID")
    api_key_encrypted: Mapped[str] = mapped_column(Text, default="", comment="Fernet 加密的 API Key")
    default_prompt: Mapped[str] = mapped_column(Text, default="", comment="全局业务审核要求")
    version: Mapped[int] = mapped_column(Integer, default=0)  # 乐观锁，避免管理员互相覆盖
    connection_version: Mapped[int] = mapped_column(Integer, default=0)  # 仅连接、密钥和启停变化时递增


class InvoiceOCRConfig(Base, Timestamped):
    """腾讯云发票识别配置单例，两项凭据一起加密及更新。"""
    __tablename__ = "invoice_ocr_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    credentials_encrypted: Mapped[str] = mapped_column(Text, default="", comment="Fernet 加密的 SecretId 和 SecretKey")
    version: Mapped[int] = mapped_column(Integer, default=0, comment="配置乐观锁版本")


class SyncConfig(Base, Timestamped):
    """自动增量补偿同步的全局单例；调度计时持久化，重启不会重置间隔。"""
    __tablename__ = "sync_config"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    managed: Mapped[bool] = mapped_column(Boolean, default=False, comment="首次在工作台保存后使用数据库配置")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, comment="仅控制自动补偿同步，手动同步仍然可用")
    interval_seconds: Mapped[int] = mapped_column(Integer, default=300, comment="自动同步间隔，单位秒，范围 1 至 86400")
    version: Mapped[int] = mapped_column(Integer, default=0, comment="配置乐观锁；调度推进不增加版本")
    schedule_anchor_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
        comment="上次自动入队或配置变更的计时起点，UTC；空值表示首次启动尽快调度")


class ApprovalTemplate(Base, Timestamped):
    __tablename__ = "approval_templates"
    code: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    business_type: Mapped[str] = mapped_column(String(24), default="REIMBURSEMENT", comment="REIMBURSEMENT 报销 / PAYMENT 付款申请")
    node_id: Mapped[str] = mapped_column(String(100), comment="首个专用预审节点 ID，兼容旧接口")
    node_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]",
        comment="专用预审节点 ID 列表，所有节点共用模板的专用审批人")
    reviewer_open_id: Mapped[str] = mapped_column(String(100))
    field_mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    agent_prompt: Mapped[str] = mapped_column(Text, default="", comment="节点业务审核要求；空值继承全局默认")
    ai_approval_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0",
        comment="是否允许此模板运行 AI 预审和按规则自动回写；默认由财务整单人工预审")
    payment_auto_approve_enabled: Mapped[bool] = mapped_column(Boolean, server_default="0",
        default=lambda context: context.get_current_parameters().get("business_type") == "PAYMENT",
        comment="付款仅采集并自动同意专用节点；优先于 AI 开关和审核模式，报销固定关闭")
    mode: Mapped[str] = mapped_column(String(16), default="OBSERVE")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    subscribed: Mapped[bool] = mapped_column(Boolean, default=False)
    capabilities: Mapped[dict] = mapped_column(JSON, default=dict, comment="实际联调积累的读取、下载与回写能力")
    sync_cursor: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Approval(Base, Timestamped):
    __tablename__ = "approvals"
    id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="飞书 instance_code")
    template_code: Mapped[str] = mapped_column(ForeignKey("approval_templates.code"))
    business_type: Mapped[str] = mapped_column(String(24), default="REIMBURSEMENT", index=True, comment="采集时固化业务类型，历史统计不随模板配置变化")
    payment_details: Mapped[dict] = mapped_column(JSON, default=dict, comment="规范化付款字段、主体关联及未取得附件的原因")
    payment_collection_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True,
        comment="当前付款材料采集任务；进度沿用 Job，不修改审批回写版本")
    payment_collection_fingerprint: Mapped[str] = mapped_column(String(64), default="", server_default="",
        comment="付款采集内容与识别配置指纹；不含待办状态或付款审核规则")
    serial_number: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(200))
    applicant: Mapped[str] = mapped_column(String(100), comment="申请人姓名；待通讯录补齐时可保存飞书身份作为展示值")
    applicant_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True,
        comment="申请人 open_id；仅有 user_id 时带 user_id: 前缀，空值表示历史身份待补齐")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True,
        comment="飞书 start_time 对应的申请提交时间，UTC；不得用采集时间替代")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True,
        comment="飞书最终通过时间，UTC；历史缺失时留空")
    archive_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, server_default="{}",
        comment="打印用审批轨迹及全单附件索引；补充原件不参与预审")
    department: Mapped[str] = mapped_column(String(100), default="")
    amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"), comment="整数分存储，业务层使用元 Decimal")
    expense_type: Mapped[str] = mapped_column(String(80), default="其他费用")
    company_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    decision: Mapped[str] = mapped_column(String(16), default="PENDING", index=True)
    ai_status: Mapped[str] = mapped_column(String(16), default="NOT_STARTED", server_default="NOT_STARTED",
        comment="整轮预审进度：NOT_STARTED / QUEUED / RUNNING / COMPLETED / FAILED，与审核结论及飞书状态独立")
    ai_task_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True,
        comment="当前预审所属后台任务 ID；直接调用时为独立执行标识，防止旧任务覆盖新一轮进度")
    ai_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True,
        comment="最新预审请求时间，UTC；显式重试沿用旧任务 ID 时也更新，仅用于进度归属判断")
    archive_status: Mapped[str] = mapped_column(String(24), default="NONE")
    task_id: Mapped[str] = mapped_column(String(100), default="")
    node_id: Mapped[str] = mapped_column(String(100), default="")
    assignee: Mapped[str] = mapped_column(String(100), default="")
    task_status: Mapped[str] = mapped_column(String(24), default="PENDING")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="乐观并发控制，前端操作须回传")
    attachment_version: Mapped[str] = mapped_column(String(64), default="")
    audit_attachment_version: Mapped[str] = mapped_column(String(64), default="")
    audit_requested: Mapped[bool] = mapped_column(Boolean, default=False, comment="配置变更后待重审，回写对账结束后补发")
    last_audit_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    approved_once: Mapped[bool] = mapped_column(Boolean, default=False)
    last_synced_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True, comment="最近一次权威状态读取，用于存量轮询补偿")
    raw_form: Mapped[list] = mapped_column(JSON, default=list)
    external_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, comment="仅演示模式用于模拟飞书权威状态")


class Budget(Base, Timestamped):
    """独立周期的部门预算；空费用类别表示覆盖该部门全部类别。"""
    __tablename__ = "budgets"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(150))
    company_id: Mapped[str] = mapped_column(ForeignKey("companies.id"), index=True)
    department: Mapped[str] = mapped_column(String(100), comment="审批表单业务部门名称，去除首尾空白后精确匹配")
    expense_type: Mapped[str] = mapped_column(String(80), default="", comment="空字符串覆盖全部费用类别")
    period_type: Mapped[str] = mapped_column(String(16), comment="MONTH / QUARTER / YEAR / CUSTOM")
    start_date: Mapped[date] = mapped_column(Date, index=True)
    end_date: Mapped[date] = mapped_column(Date, index=True)
    amount: Mapped[Decimal] = mapped_column(Money(), comment="当前预算额度，整数分存储")
    warning_threshold: Mapped[int] = mapped_column(Integer, default=80, comment="占用预警百分比，1 至 100")
    notes: Mapped[str] = mapped_column(Text, default="")
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    version: Mapped[int] = mapped_column(Integer, default=1, comment="预算维护乐观锁，与审批审核版本独立")
    __table_args__ = (UniqueConstraint("company_id", "department", "expense_type", "start_date", "end_date",
                                     name="uq_budget_scope_period"),)


class BudgetUsage(Base, Timestamped):
    """每张报销唯一的预算用量事实；多个周期共享事实，禁止逐事件累加金额。"""
    __tablename__ = "budget_usages"
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), primary_key=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True, index=True)
    department: Mapped[str] = mapped_column(String(100), default="")
    expense_type: Mapped[str] = mapped_column(String(80), default="其他费用")
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    amount: Mapped[Decimal] = mapped_column(Money(), default=Decimal("0"))
    state: Mapped[str] = mapped_column(String(16), default="RESERVED", index=True,
        comment="RESERVED 在途 / USED 已用 / FROZEN 冻结 / RELEASED 已释放")
    attribution_pending: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    attribution_reason: Mapped[str] = mapped_column(String(500), default="")
    attribution_source: Mapped[str] = mapped_column(String(16), default="AUTO", comment="AUTO 自动 / MANUAL 人工 / REVIEW 来源变化待确认")
    source_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    approved_snapshot: Mapped[dict] = mapped_column(JSON, default=dict, comment="最后有效通过快照；历史缺失时保存带 _historical 标记的首次建账依据")
    freeze_fingerprint: Mapped[str] = mapped_column(String(64), default="", comment="冻结依据指纹，不包含同步次数和审批审核版本")
    released_fingerprint: Mapped[str] = mapped_column(String(64), default="", comment="已人工释放的冻结指纹；重复同步不恢复相同占用")
    historical_review: Mapped[bool] = mapped_column(Boolean, default=False, comment="历史撤销单缺少通过快照，当前金额待财务核对")
    version: Mapped[int] = mapped_column(Integer, default=1, comment="归属确认与预算释放使用的独立乐观锁")
    __table_args__ = (Index("ix_budget_usages_scope", "company_id", "department", "submitted_at"),)


class BudgetOperation(Base):
    """预算领域操作留痕；幂等标识同时绑定操作者、目标和完整请求参数。"""
    __tablename__ = "budget_operations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    budget_id: Mapped[str | None] = mapped_column(ForeignKey("budgets.id"), nullable=True, index=True)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32))
    actor_id: Mapped[str] = mapped_column(String(64))
    actor_name: Mapped[str] = mapped_column(String(100))
    reason: Mapped[str] = mapped_column(String(600), default="")
    before: Mapped[dict] = mapped_column(JSON, default=dict)
    after: Mapped[dict] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    request_fingerprint: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class Invoice(Base, Timestamped):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    invoice_key: Mapped[str] = mapped_column(String(80), unique=True, comment="全企业唯一，不能包含公司主体")
    number: Mapped[str] = mapped_column(String(40), index=True)
    code: Mapped[str] = mapped_column(String(20), default="")
    kind: Mapped[str] = mapped_column(String(24))
    buyer_name: Mapped[str] = mapped_column(String(150), default="")
    buyer_tax_id: Mapped[str] = mapped_column(String(32), index=True)
    seller_name: Mapped[str] = mapped_column(String(150), default="")
    issue_date: Mapped[date] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(Money(), comment="整数分存储，业务层使用元 Decimal")
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    verification: Mapped[str] = mapped_column(String(24), default="UNVERIFIED")
    historical: Mapped[bool] = mapped_column(Boolean, default=False)


class Attachment(Base, Timestamped):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    source_key: Mapped[str] = mapped_column(String(200))
    filename: Mapped[str] = mapped_column(String(240))
    storage_key: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    size: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    parsed_data: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("approval_id", "source_key", "sha256", name="uq_attachment_version"),)


class InvoiceClaim(Base, Timestamped):
    __tablename__ = "invoice_claims"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("approvals.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(24), default="RESERVED")
    active_key: Mapped[str | None] = mapped_column(String(32), unique=True, nullable=True,
        comment="占用时等于 invoice_id，释放时为 NULL；唯一约束是跨主体防重复的最终防线")
    source_label: Mapped[str] = mapped_column(String(150), default="")
    __table_args__ = (UniqueConstraint("invoice_id", "approval_id", name="uq_invoice_approval_claim"),)


class AuditRun(Base):
    __tablename__ = "audit_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    rule_version: Mapped[str] = mapped_column(String(30), default="1.0")
    attachment_version: Mapped[str] = mapped_column(String(64))
    conclusion: Mapped[str] = mapped_column(String(16))
    evidence: Mapped[list] = mapped_column(JSON)
    model_analysis: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AICall(Base):
    """一次真实调用或跳过记录；与业务审核分开保存，日志不含密钥及供应商请求头。"""
    __tablename__ = "ai_calls"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    source: Mapped[str] = mapped_column(String(24), index=True, comment="AUDIT / CONNECTION_TEST / INVOICE_EXTRACTION")
    approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True, comment="关联审批；连接测试为空")
    template_code: Mapped[str] = mapped_column(String(100), default="")
    agent_conversation_id: Mapped[str | None] = mapped_column(ForeignKey("agent_conversations.id"), nullable=True, index=True,
        comment="私人附件识别所属会话；日志正文仅会话属主可读，管理员也不例外")
    actor: Mapped[str] = mapped_column(String(100), default="审批 Agent")
    engine: Mapped[str] = mapped_column(String(80))
    model_name: Mapped[str] = mapped_column(String(200))
    config_version: Mapped[int] = mapped_column(Integer)
    session_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True, comment="RUNNING / COMPLETED / FAILED / SKIPPED")
    stage: Mapped[str] = mapped_column(String(120), default="准备审核数据")
    prompt: Mapped[str] = mapped_column(Text, default="", comment="实际系统提示词，包含固定约束；已脱敏")
    input_json: Mapped[str] = mapped_column(Text, default="", comment="结构化输入 JSON 文本；已脱敏")
    reasoning: Mapped[str] = mapped_column(Text, default="", comment="仅保存供应商明确返回的思考文本")
    raw_output: Mapped[str] = mapped_column(Text, default="", comment="JSON 解析前的最终输出，失败时也保留已收到内容")
    parsed_result: Mapped[dict] = mapped_column(JSON, default=dict)
    usage: Mapped[dict] = mapped_column(JSON, default=dict, comment="供应商返回的 Token 用量；缺失不估算")
    events: Mapped[list] = mapped_column(JSON, default=list, comment="白名单阶段事件，不保存完整 SDK 通知或鉴权信息")
    finish_reason: Mapped[str] = mapped_column(String(100), default="")
    error_type: Mapped[str] = mapped_column(String(100), default="")
    error_message: Mapped[str] = mapped_column(String(500), default="")
    has_reasoning: Mapped[bool] = mapped_column(Boolean, default=False)
    has_output: Mapped[bool] = mapped_column(Boolean, default=False)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, comment="日志超出长度上限时明确标记截断")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)


class ApprovalAction(Base, Timestamped):
    __tablename__ = "approval_actions"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    task_operation_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    source_audit_id: Mapped[str | None] = mapped_column(ForeignKey("audit_runs.id"), nullable=True,
        comment="自动操作或整单人工同意的来源审核；执行时核对结论仍有效")
    execution_source: Mapped[str] = mapped_column(String(32), default="STANDARD", server_default="STANDARD",
        comment="STANDARD 原审核回写 / PAYMENT_AUTO_APPROVE 付款仅采集自动同意；后者不依赖审核结论")
    task_id: Mapped[str] = mapped_column(String(100))
    actor_id: Mapped[str] = mapped_column(String(100))
    actor_name: Mapped[str] = mapped_column(String(100))
    feishu_identity: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(String(600))
    expected_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="REQUESTED")
    error: Mapped[str] = mapped_column(Text, default="")


class ArchiveRecord(Base, Timestamped):
    __tablename__ = "archive_records"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    approval_id: Mapped[str] = mapped_column(ForeignKey("approvals.id"), index=True)
    attachment_id: Mapped[str] = mapped_column(ForeignKey("attachments.id"), unique=True)
    invoice_id: Mapped[str | None] = mapped_column(ForeignKey("invoices.id"), nullable=True)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    archive_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(24), default="ARCHIVED")
    manifest: Mapped[dict] = mapped_column(JSON, default=dict)


class EventInbox(Base, Timestamped):
    __tablename__ = "event_inbox"
    id: Mapped[str] = mapped_column(String(100), primary_key=True, comment="uuid 或 event_id")
    event_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="PENDING")


class Job(Base, Timestamped):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True, nullable=True)
    kind: Mapped[str] = mapped_column(String(24), index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(24), default="PENDING", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ImportBatch(Base, Timestamped):
    __tablename__ = "import_batches"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    filename: Mapped[str] = mapped_column(String(240))
    actor_id: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(24), default="UPLOADED")
    columns: Mapped[list] = mapped_column(JSON)
    rows: Mapped[list] = mapped_column(JSON)
    mapping: Mapped[dict] = mapped_column(JSON, default=dict)
    preview: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    imported: Mapped[int] = mapped_column(Integer, default=0)


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now, index=True)


class AgentConversation(Base, Timestamped):
    """私人业务会话；属主由登录身份指定，客户端不能指定其他用户。"""
    __tablename__ = "agent_conversations"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="新对话")
    event_seq: Mapped[int] = mapped_column(Integer, default=0, comment="会话内单调递增的 SSE 游标")
    harness_initialized: Mapped[bool] = mapped_column(Boolean, default=False)


class AgentAttachment(Base, Timestamped):
    """私人会话的独立原件；submitted 后允许历史追问，删除标记仅用于未发送草稿。"""
    __tablename__ = "agent_attachments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    upload_id: Mapped[str] = mapped_column(String(100), comment="客户端上传幂等标识，网络重试复用")
    filename: Mapped[str] = mapped_column(String(240))
    storage_key: Mapped[str] = mapped_column(Text, comment="独立会话原件路径，不与审批原件共享删除生命周期")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime: Mapped[str] = mapped_column(String(100))
    size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True, comment="QUEUED / PROCESSING / READY / FAILED")
    error: Mapped[str] = mapped_column(String(600), default="")
    segments: Mapped[list] = mapped_column(JSON, default=list, comment="保留页码、段落或单元格定位的只读正文")
    recognition: Mapped[dict] = mapped_column(JSON, default=dict, comment="票面独立来源与身份校验；不能作为审批放行依据")
    submitted: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已绑定过用户消息；绑定后不能删除")
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("conversation_id", "upload_id", name="uq_agent_attachment_upload"),)


class AgentMessage(Base):
    __tablename__ = "agent_messages"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    run_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), comment="user / assistant / tool / system")
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict] = mapped_column(JSON, default=dict, comment="上下文、工具结果和步骤状态；不含鉴权数据")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    active_owner: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True,
        comment="运行时等于属主 ID；终止后置空，保证每人最多一个运行")
    request_id: Mapped[str] = mapped_column(String(100), comment="客户端消息幂等标识")
    user_message_id: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(24), default="RUNNING", index=True)
    stage: Mapped[str] = mapped_column(String(150), default="准备会话")
    model_name: Mapped[str] = mapped_column(String(200))
    config_version: Mapped[int] = mapped_column(Integer)
    error: Mapped[str] = mapped_column(String(600), default="")
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (UniqueConstraint("conversation_id", "request_id", name="uq_agent_message_request"),)


class AgentEvent(Base):
    __tablename__ = "agent_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (UniqueConstraint("conversation_id", "seq", name="uq_agent_event_seq"),)


class AgentProposal(Base):
    """模型只能提出不可变方案；只有浏览器属主确认接口能够执行。"""
    __tablename__ = "agent_proposals"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("agent_conversations.id"), index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))
    target_id: Mapped[str] = mapped_column(String(100), default="")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict)
    checks: Mapped[dict] = mapped_column(JSON, default=dict, comment="确认时复核的版本、原件及配置快照")
    display: Mapped[dict] = mapped_column(JSON, default=dict, comment="由服务端生成的目标、影响与字段差异")
    status: Mapped[str] = mapped_column(String(24), default="PENDING")
    job_id: Mapped[str | None] = mapped_column(ForeignKey("jobs.id"), nullable=True)
    error: Mapped[str] = mapped_column(String(600), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
