"""公开接口契约。金额以十进制字符串传输，避免前端浮点运算影响账务。"""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints, field_validator, model_validator
from typing_extensions import TypedDict
from .audit_policy import BUILTIN_DEFAULT_ACTIONS, BUSINESS_DEFAULT_ACTIONS
from .budget_schemas import BudgetContext


class Schema(BaseModel):
    model_config = ConfigDict(from_attributes=True)


T = TypeVar("T")


class PageOut(Schema, Generic[T]):
    """分页结果，page 从 1 开始，默认每页 20 行。"""
    items: list[T] = Field(description="当前页记录")
    total: int = Field(description="符合筛选的记录总数")
    page: int = Field(description="当前页码")
    page_size: int = Field(description="每页记录数")


class UserOut(Schema):
    """当前工作台用户；角色以服务端配置为准。"""
    id: str = Field(description="飞书 open_id，演示用户带 demo 前缀")
    name: str = Field(description="用户显示名称")
    role: Literal["admin", "finance", "viewer"] = Field(description="管理员、财务、只读")
    enabled: bool = Field(description="账号是否启用")


class UserUpdate(Schema):
    role: Literal["admin", "finance", "viewer"] = Field(description="授权角色")
    enabled: bool = Field(True, description="是否允许登录工作台")


class CompanyInput(Schema):
    name: str = Field(min_length=2, max_length=150, description="公司法定名称")
    tax_id: str = Field(min_length=15, max_length=20, pattern=r"^[0-9A-Z]+$", description="购买方纳税人识别号")
    aliases: list[str] = Field(default_factory=list, description="曾用名及允许的名称别名")
    active: bool = Field(True, description="是否用于自动归类")


class CompanyOut(CompanyInput):
    id: str = Field(description="主体标识")


class FieldMapping(Schema):
    amount: str = Field("", description="报销总额控件 ID")
    company_tax_id: str = Field("", description="申报主体税号控件 ID，可位于明细内；仅映射实际税号，不映射公司名称或选项 ID")
    expense_type: str = Field("", description="费用类型控件 ID")
    description: str = Field("", description="报销事由控件 ID")
    attachment_ids: list[str] = Field(default_factory=list, description="票据附件控件 ID；空列表表示提取全部附件控件")
    company: str = Field("", description="报销或付款申报主体的名称控件 ID，按公司法定名称或允许的别名关联；旧报销模板未映射时读取明确命名的主体控件")
    department: str = Field("", description="申请部门控件 ID")
    project: str = Field("", description="所属项目控件 ID")
    payment_method: str = Field("", description="付款方式控件 ID")
    expected_payment_date: str = Field("", description="期望付款日期控件 ID")
    payee_account: str = Field("", description="飞书原生收款账户控件 ID")
    related_approvals: str = Field("", description="关联审批控件 ID，仅作为引用，不继承原审批同意结论")
    remarks: str = Field("", description="备注控件 ID")


class RulePolicy(Schema):
    """标准异常处理策略；系统底线不属于可配置项。"""
    enabled: bool = Field(True, description="是否参与本模板审核")
    on_failure: Literal["REJECT", "REVIEW", "WARN"] = Field("REVIEW", description="明确违规后的处理：拒绝、人工复核、仅提示")


class CustomAuditRule(RulePolicy):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$", description="稳定条目标识，修改名称或说明时保持不变")
    name: str = Field(min_length=1, max_length=100, description="财务可识别的标准名称")
    description: str = Field(min_length=1, max_length=2000, description="应满足的文字标准，模型逐条判定是否违反，不能覆盖系统权限")

    @field_validator("name", "description", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class AuditRuleEntry(CustomAuditRule):
    """版本 3 的显式业务规则；列表缺项表示删除，不隐式补回默认值。"""
    kind: Literal["BUILTIN", "CUSTOM"] = Field("CUSTOM", description="程序预置条件或模型文字条件")
    builtin_code: str = Field("", description="预置条件编码；CUSTOM 时必须为空，重复占票属于独立执行约束")
    parameters: dict[str, Any] = Field(default_factory=dict, description="预置参数；金额 comparison 为 AT_LEAST/EXACT，语义 require_completed 决定模型不可用时是否转人工")


class AuditRules(Schema):
    version: str = Field("1.0", max_length=30, description="规则版本，随配置变更留存到审核记录")
    amount_tolerance: Decimal = Field(Decimal("0.01"), ge=0, le=1, description="金额核对允许的舍入差额，单位元")
    require_semantic: bool = Field(False, description="是否要求模型完成语义核对，未配置或失败时转人工")
    policy_schema_version: Literal[1, 2, 3] = Field(1, description="策略结构版本；1/2 兼容旧配置，3 使用显式业务规则列表")
    builtin_rules: dict[str, RulePolicy] = Field(default_factory=dict, description="按稳定规则编码配置预置标准；重复占票始终拦截")
    custom_rules: list[CustomAuditRule] = Field(default_factory=list, max_length=20, description="文字标准，最多 20 条；显式空列表表示删除全部文字标准")
    entries: list[AuditRuleEntry] = Field(default_factory=list, max_length=40, description="版本 3 的完整业务规则列表；显式空列表为无业务条件，须人工确认")

    @model_validator(mode="after")
    def validate_policies(self):
        if set(self.builtin_rules) - BUILTIN_DEFAULT_ACTIONS.keys():
            raise ValueError("存在未知预置标准编码")
        duplicate = self.builtin_rules.get("DUPLICATE")
        if duplicate and (not duplicate.enabled or duplicate.on_failure == "WARN"):
            raise ValueError("重复占票不能停用或仅提示，请选择拒绝或人工处理")
        ids = [rule.id for rule in self.custom_rules]
        if len(ids) != len(set(ids)):
            raise ValueError("文字标准 ID 不能重复")
        if self.policy_schema_version == 3:
            ids = [rule.id for rule in self.entries]
            codes = [rule.builtin_code for rule in self.entries if rule.kind == "BUILTIN"]
            if len(ids) != len(set(ids)) or len(codes) != len(set(codes)):
                raise ValueError("业务规则 ID 和预置编码不能重复")
            for rule in self.entries:
                if rule.kind == "BUILTIN" and rule.builtin_code not in BUSINESS_DEFAULT_ACTIONS:
                    raise ValueError("未知预置业务规则；重复占票属于不可删除的执行约束")
                if rule.kind == "CUSTOM" and rule.builtin_code:
                    raise ValueError("文字规则不能指定程序预置编码")
                if rule.parameters.get("comparison", "AT_LEAST") not in {"AT_LEAST", "EXACT"}:
                    raise ValueError("金额比较方式无效")
                if "require_completed" in rule.parameters and not isinstance(rule.parameters["require_completed"], bool):
                    raise ValueError("语义核对完成要求必须为布尔值")
        return self


NodeId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class TemplateConfig(Schema):
    code: str = Field(min_length=1, max_length=100, description="飞书 approval_code")
    name: str = Field(min_length=1, max_length=150, description="审批模板名称")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] = Field("REIMBURSEMENT", description="报销或付款申请；已有实例后不得改换类型")
    reviewer_open_id: str = Field(min_length=1, max_length=100, description="所有专用预审节点共用的审批成员在本应用中的 open_id")
    field_mapping: FieldMapping = Field(default_factory=FieldMapping, description="按控件 ID 读取字段，避免依赖字段名称")
    rules: AuditRules = Field(default_factory=AuditRules, description="确定性审核规则")
    agent_prompt: str = Field("", max_length=20000, description="Agent 节点业务审核要求，空字符串继承全局默认；不覆盖系统输出约定")
    mode: Literal["OBSERVE", "AUTO"] = Field("OBSERVE", description="观察模式或按规则自动同意、拒绝")
    active: bool = Field(True, description="是否接收此模板的业务事件")
    payment_auto_approve_enabled: bool = Field(False, strict=True,
        description="付款仅采集并自动同意专用节点，优先于 AI 和自动模式；新付款省略时默认开启，旧模板省略时保留原值，报销不得开启")


class TemplateInput(TemplateConfig):
    node_ids: list[NodeId] | None = Field(None, min_length=1, description="专用预审节点 ID 或 custom_node_id 列表，提供时以列表为准")
    node_id: NodeId | None = Field(None, description="旧版单节点字段，仅省略 node_ids 时使用")

    @model_validator(mode="after")
    def require_pre_review_nodes(self):
        if self.node_ids is None and ("node_ids" in self.model_fields_set or not self.node_id):
            raise ValueError("请配置至少一个专用预审节点")
        return self


class TemplateAIApprovalInput(Schema):
    ai_approval_enabled: bool = Field(strict=True, description="开启后允许模板运行 AI 预审，关闭后由财务整单人工预审")


class TemplatePaymentAutoApprovalInput(Schema):
    payment_auto_approve_enabled: bool = Field(strict=True,
        description="付款模板仅采集开关；开启后包含存量专用待办，材料解析或历史审核异常不阻止自动同意")


class TemplateOut(TemplateConfig):
    ai_approval_enabled: bool = Field(description="模板保存的 AI 审批开关；付款仅采集时暂停生效，原自动同意和拒绝还需开启自动模式")
    node_ids: list[str] = Field(description="当前配置的所有专用预审节点 ID 或 custom_node_id")
    node_id: str = Field(description="首个专用预审节点 ID，兼容旧版接口")
    subscribed: bool = Field(description="是否完成原生审批定义事件订阅")
    capabilities: dict[str, bool] = Field(default_factory=dict, description="通过实际调用验证的能力")
    sync_cursor: datetime | None = Field(None, description="增量补偿同步游标，UTC")


class PaymentDetails(Schema):
    """付款表单规范值，保留账户原始结构以兼容飞书个人及对公账户。"""
    company_name: str = Field("", description="表单选择的付款主体名称")
    company_id: str | None = Field(None, description="唯一匹配的工作台公司 ID；未知或歧义时为空")
    project: str = Field("", description="所属项目")
    payment_method: str = Field("", description="付款方式显示值")
    expected_payment_date: str = Field("", description="期望付款日期的原始显示值")
    payee_account: Any = Field(default_factory=dict, description="飞书原生收款账户信息，字符串或结构化对象")
    related_approvals: Any = Field(default_factory=list, description="关联审批 ID 或飞书返回的结构化引用")
    remarks: str = Field("", description="申请备注")
    material_issues: list[dict[str, str]] = Field(default_factory=list, description="未取得的附件名称、来源键和原因，不包含临时下载地址")


class ApprovalOut(Schema):
    payment_auto_approve_enabled: bool = Field(False, description="所属付款模板当前仅采集并自动同意；审核结论仅作历史记录")
    payment_collection_status: Literal["NOT_STARTED", "QUEUED", "RUNNING", "COMPLETED", "FAILED"] = Field("NOT_STARTED",
        description="独立材料采集进度，与 AI 审核和飞书回写状态无关")
    payment_collection_task_id: str | None = Field(None, description="当前材料采集后台任务 ID，失败可通过现有任务重试接口重试")
    payment_collection_issues: list[dict[str, str]] = Field(default_factory=list,
        description="材料读取、发票识别及占用冲突记录，仅作提示，不阻止付款预审放行")
    payment_auto_approval_status: Literal["NOT_STARTED", "REQUESTED", "EXECUTING", "UNKNOWN", "SUCCEEDED", "FAILED"] = Field("NOT_STARTED",
        description="当前专用待办的仅采集自动同意回写状态；成功仅表示该节点同意，最终状态以飞书为准")
    manual_review_valid: bool = Field(False, description="当前整单人工结论是否有效，独立于 AI 进度和审核配置")
    ai_approval_enabled: bool = Field(False, description="当前生效的 AI 预审开关；付款仅采集时为 false，模板原保存值不变")
    id: str = Field(description="飞书 instance_code")
    template_code: str = Field(description="飞书 approval_code")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] = Field("REIMBURSEMENT", description="审批实例固化的业务类型")
    payment_details: PaymentDetails = Field(default_factory=PaymentDetails, description="付款表单信息；报销单返回空的默认结构")
    serial_number: str = Field(description="原审批申请编号")
    title: str = Field(description="报销事由")
    applicant: str = Field(description="申请人展示姓名，优先使用工作台登录姓名，其次使用已补齐的飞书通讯录姓名，缺失时沿用审批原值")
    applicant_id: str | None = Field(None, description="审批发起人身份，优先使用 open_id，仅有 user_id 时带 user_id: 前缀，历史未补齐时为空")
    submitted_at: datetime | None = Field(None, description="飞书申请提交时间，UTC；历史未补齐时为空，不以采集时间代替")
    department: str = Field(description="申请部门")
    amount: Decimal = Field(description="报销总额，十进制字符串")
    expense_type: str = Field(description="费用类型")
    company_ids: list[str] = Field(description="票据涉及的主体 ID")
    declared_company_names: list[str] = Field(default_factory=list, description="从主体名称映射或明确命名的主体控件读取的申报名称；与审核使用相同来源，不代表票面主体已匹配")
    unmatched_company_names: list[str] = Field(default_factory=list, description="当前附件中尚未归入 company_ids 的票面购买方名称，保留待确认状态")
    status: str = Field(description="飞书审批实例状态")
    decision: Literal["PENDING", "PASS", "REVIEW", "BLOCK"] = Field(description="待审核、通过、需复核、阻断")
    ai_status: Literal["NOT_STARTED", "QUEUED", "RUNNING", "COMPLETED", "FAILED"] = Field("NOT_STARTED",
        description="整轮预审进度：未开始、排队中、审批中、已完成、审批失败；已完成表示结论已保存，不代表审批通过")
    archive_status: str = Field(description="独立归档状态")
    task_id: str = Field(description="预审任务 ID")
    task_status: str = Field(description="当前专用预审任务状态，MISSING 表示未找到匹配待办，AMBIGUOUS 表示匹配到多个待办")
    version: int = Field(description="当前版本，人工操作必须回传")
    attachment_version: str = Field(description="当前附件集合的 SHA256 指纹")
    created_at: datetime = Field(description="采集时间，UTC")
    updated_at: datetime = Field(description="最后更新时间，UTC")


class InvoiceDisplayOut(Schema):
    """展示用票面信息及报销单类别。"""
    billing_type: str | None = Field(None, description="票面账单类型；未识别细分类型时按数电或传统发票展示")
    expense_type: str | None = Field(None, description="当前报销单的费用类型；台账取有效占用关联的报销单，无来源时为空")
    tax_amount: Decimal | None = Field(None, description="票面总税额，单位元，十进制字符串；缺失或冲突时为空")
    untaxed_amount: Decimal | None = Field(None, description="票面不含税金额合计，单位元，十进制字符串；缺失或冲突时为空")
    reimbursable_amount: Decimal | None = Field(None, description="单票价税合计，单位元，以十进制字符串展示为可报销金额")
    deductible_tax_rate: Decimal | None = Field(None, description="按主体规则确认的单一抵扣税率，十进制字符串，13 表示 13%；待确认时为空")
    deductible_tax_amount: Decimal | None = Field(None, description="按主体规则计算的可抵扣税额，单位元，十进制字符串；待确认时为空")


class InvoiceTotalsOut(Schema):
    """当前审批中已识别的发票按票去重合计。"""
    amount: Decimal = Field(description="价税合计之和，单位元，十进制字符串")
    tax_amount: Decimal | None = Field(description="总税额之和，单位元；任一张发票缺值时为空")
    untaxed_amount: Decimal | None = Field(description="不含税金额之和，单位元；任一张发票缺值时为空")
    deductible_tax_amount: Decimal | None = Field(None, description="去重后可抵扣税额之和，单位元，十进制字符串；任一张发票待确认时为空")


class InvoiceOut(Schema):
    id: str = Field(description="发票记录 ID")
    invoice_key: str = Field(description="全企业查重唯一键")
    number: str = Field(description="发票号码，保留前导零")
    code: str = Field(description="传统发票代码")
    kind: str = Field(description="E_INVOICE 数电发票 / VAT 传统发票")
    buyer_name: str = Field(description="购买方名称")
    buyer_tax_id: str = Field(description="购买方税号")
    seller_name: str = Field(description="销售方名称")
    issue_date: date = Field(description="开票日期")
    amount: Decimal = Field(description="票面价税合计")
    company_id: str | None = Field(description="识别到的企业主体")
    verification: str = Field(description="UNVERIFIED 表示未接入税务验真，不能解释为验真通过")
    historical: bool = Field(description="是否由历史台账导入")
    created_at: datetime = Field(description="建档时间，UTC")
    display_details: InvoiceDisplayOut = Field(default_factory=InvoiceDisplayOut, description="页面展示用的账单类型、费用类型和补充票面金额")


class OCRLineOut(Schema):
    """OCR 坐标以对应渲染页的像素为单位，置信度不单独决定审批资格。"""
    text: str = Field(description="历史 OCR 文本行或腾讯云识别到的票面字段")
    score: float | None = Field(None, description="历史本地 OCR 置信度，腾讯云发票接口未提供时为空")
    box: list[list[float]] = Field(default_factory=list, description="四角坐标，每点为 [x, y]，无有效坐标时为空列表")


class OCRPageOut(Schema):
    page: int = Field(description="从 1 开始的页码")
    width: int = Field(description="渲染页宽度，像素")
    height: int = Field(description="渲染页高度，像素")
    lines: list[OCRLineOut] = Field(description="本页 OCR 字段或文本行及可用坐标")
    request_id: str = Field("", description="该页腾讯云请求 ID，未取得时为空")


class RecognitionSourceOut(Schema):
    """一个来源只计一票，不能用重复识别或多页结果增加来源数量。"""
    model_config = ConfigDict(from_attributes=True, extra="allow")
    status: str = Field(description="COMPLETED / EMPTY / FAILED / SKIPPED，表示该路提取结果")
    fields: dict[str, str] = Field(default_factory=dict, description="独立提取的原始票面候选，允许不完整")
    error: str = Field("", description="该路失败原因，已脱敏")
    cached: bool = Field(False, description="是否复用同一原件与配置下成功的模型提取结果")
    pages: list[OCRPageOut] = Field(default_factory=list, description="OCR 原始页面及文本框；其他来源为空")
    clear_fields: dict[str, bool] = Field(default_factory=dict, description="多模态逐字段可辨认状态；未明确为 true 的字段不参与模型计票")


class RecognitionComparisonOut(Schema):
    field: str = Field(description="稳定的票面字段名")
    label: str = Field(description="字段中文名称")
    required: bool = Field(description="此票种是否要求该字段；数电票代码不适用")
    values: dict[str, str] = Field(description="按 TEXT / TENCENT_OCR / MODEL / XML 等来源保存的候选值，历史本地来源为 OCR")
    accepted: str = Field(description="PDF/XML 本地有效字段或独立来源一致后采用的规范值；未解决时为空")
    matched_sources: list[str] = Field(description="支持采用值的来源；PDF 本地、XML 或完整图片 OCR 可单路采用，模型补缺需通过同票身份核对")
    status: Literal["AGREED", "CONFLICT", "MISSING", "NOT_REQUIRED"] = Field(description="已一致、存在分歧、缺少一致证据或票种不适用")


class RecognitionOut(Schema):
    model_config = ConfigDict(from_attributes=True, extra="allow")
    status: Literal["LOCAL", "VERIFIED", "STRUCTURED", "OCR", "UNCERTAIN"] = Field(
        description="LOCAL 为 PDF 本地完整，VERIFIED 为模型兜底或历史交叉核对，OCR 为图片 OCR 主识别完成，STRUCTURED 为完整 XML，UNCERTAIN 为待确认；不代表税务验真")
    reason: str = Field(description="一致性结论或尚未解决字段的中文说明")
    comparisons: list[RecognitionComparisonOut] = Field(default_factory=list, description="逐字段候选对比及采用依据")


class PaymentMaterialPageScope(TypedDict):
    """限页合同实际提取的页范围，页码从原件第 1 页开始。"""
    read_pages: Annotated[int, Field(ge=1, description="已读取的连续页数，也是最后一张已读页的原件页码")]
    total_pages: Annotated[int, Field(ge=1, description="完整 PDF 原件的总页数，包含未读页面")]


class PaymentMaterialOut(TypedDict, total=False):
    """保留原有解析与人工确认字段，限页合同另返回阅读范围。"""
    __pydantic_config__ = ConfigDict(extra="allow")
    page_scope: Annotated[PaymentMaterialPageScope, Field(description="仅限页合同返回，原件仍完整且 complete 为 false")]


class ArchiveMemberOut(Schema):
    """展开的成员原件。可用现有附件接口按 ID 下载。"""

    attachment_id: str = Field(description="包内成员的附件版本 ID")
    path: str = Field(description="压缩包内的相对路径，包含成员名称")


class AttachmentArchiveOut(Schema):
    """压缩包原件的展开清单和用途。"""

    status: Literal["READY", "FAILED"] = Field(description="完整展开或需财务核对")
    mode: Literal["AUDIT", "REFERENCE"] = Field(description="参与业务审核或仅供历史查阅")
    error: str = Field(description="展开失败时的中文原因，成功时为空")
    members: list[ArchiveMemberOut] = Field(description="可取得的同版本成员，按包内目录顺序")


class DocumentClassificationOut(Schema):
    """整份附件分类证据；不暴露分类请求中的内部票面候选，不作为重复字段计票。"""
    status: Literal["COMPLETED", "UNKNOWN", "FAILED"] = Field(description="分类完成、内容不确定或分类调用失败")
    material_type: Literal["INVOICE", "CONTRACT", "QUOTE", "OTHER", "UNKNOWN"] = Field(description="原件实际内容类别")
    complete: bool = Field(description="模型已完整检查全部页面，或财务已明确确认当前原件")
    reason: str = Field(description="分类依据或未完成原因")
    source: Literal["MODEL", "MANUAL"] = Field(description="模型分类或绑定当前原件的人工确认")
    pages: list[int] = Field(default_factory=list, description="模型完整检查的页码；人工确认可为空")
    invoice_pages: list[int] = Field(default_factory=list, description="实际包含发票票面的页码")
    invoice_count: int = Field(description="不同发票数量；同票跨页只计一次")
    version: str = Field(description="分类策略版本")
    model_call_id: str = Field("", description="分类模型调用日志 ID；人工确认时为空")
    cached: bool = Field(False, description="是否复用同一原件及模型配置下的完整分类")


class ParsedInvoiceDataOut(Schema):
    """兼容旧解析记录，人工确认与原始识别证据分别保存，不改写历史意见。"""
    model_config = ConfigDict(from_attributes=True, extra="allow")
    parse_status: Literal["PARSED", "SKIPPED", "FAILED"] | None = Field(None, description="发票解析状态；SKIPPED 表示普通附件，不参与票据规则")
    skip_reason: str = Field("", description="跳过发票解析的中文说明；原件仍可下载和归档")
    fields: dict[str, str] = Field(default_factory=dict, description="用于业务核验或人工编辑的字段；自动资格由 recognition 独立判断")
    source: str = Field("", description="原始主提取路径，如 TEXT / TENCENT_OCR / XML / OFD")
    reliable: bool = Field(False, description="来源可靠性提示，不直接决定是否通过审批")
    confirmed: bool = Field(False, description="财务是否已明确核对当前附件字段")
    confirmed_by: str = Field("", description="明确核对字段的财务姓名")
    confirmed_at: str = Field("", description="人工核对时间，UTC ISO 字符串；未确认时为空")
    confirmation_comment: str = Field("", description="财务保存的人工核对说明")
    error: str = Field("", description="无法建立最低票据身份或原件被拒绝处理的原因")
    local_error: str = Field("", description="保留文字层和 OCR 的失败原因，供追溯识别路径")
    raw_text: str = Field("", description="主提取路径的原始文字，最多保留 30000 字符")
    model_call_id: str = Field("", description="独立图像识别的 AI 日志 ID；复用时指向原调用")
    pipeline_version: str = Field("", description="识别策略版本；旧记录可能为空")
    sources: dict[str, RecognitionSourceOut] = Field(default_factory=dict, description="各个独立来源的候选及失败原因")
    recognition: RecognitionOut | None = Field(None, description="本地解析或交叉核对结论；旧记录可能尚无识别结论")
    material_type: Literal["INVOICE", "CONTRACT", "QUOTE", "OTHER", "UNKNOWN"] = Field("INVOICE", description="附件内容分类；历史未分类记录兼容原默认值")
    classification: DocumentClassificationOut | None = Field(None, description="PDF、图片整份分类证据；旧记录和结构化原件可为空")
    document_policy_version: str = Field("", description="PDF、图片分类及字段采用策略版本")
    material: PaymentMaterialOut = Field(default_factory=dict, description="付款材料完整读取状态、定位文本片段、阅读范围和分类依据")
    archive: AttachmentArchiveOut | None = Field(None, description="仅压缩包原件返回，含展开状态、用途和包内文件")
    reference_pdf: Literal["INVOICE", "TRAVEL", "REVIEW"] | None = Field(
        None, description="历史包内 PDF 的补充识别类型，分别为发票、行程单和待核对")


class AttachmentOut(Schema):
    id: str = Field(description="附件版本 ID")
    invoice_id: str | None = Field(description="关联发票 ID")
    filename: str = Field(description="原件文件名；扩展名按真实文件格式校正")
    sha256: str = Field(description="原件内容哈希")
    mime: str = Field(description="依据原件内容识别的文件媒体类型")
    size: int = Field(description="文件大小，字节")
    active: bool = Field(description="是否属于当前审批的附件集合")
    parsed_data: ParsedInvoiceDataOut = Field(description="票面字段、分来源交叉识别证据及人工确认记录；model_call_id 关联独立图像识别日志")


class Evidence(Schema):
    code: str = Field(description="稳定的规则编码")
    title: str = Field(description="规则名称")
    passed: bool = Field(description="此项当前有效结论是否通过，含人工确认；原始判定保存在 result，INFO 提示为 false 也不阻止通过")
    severity: Literal["INFO", "REVIEW", "BLOCK"] = Field(description="提示、人工复核、硬性阻断")
    detail: str = Field(description="可供人工核对的依据")
    user_message: str | None = Field(None, description="面向报销人的通俗原因和处理建议，独立保留 detail 中的技术证据；历史审核可为空")
    related_approval: str | None = Field(None, description="重复占用关联审批")
    source: Literal["BUILTIN", "CUSTOM", "SYSTEM"] | None = Field(None, description="预置、文字或强制系统标准；历史记录可为空")
    result: Literal["PASS", "FAIL", "UNKNOWN", "SKIPPED"] | None = Field(None, description="原始事实判定：通过、违规、无法判断、停用或不适用；人工确认不改写此值")
    on_failure: Literal["REJECT", "REVIEW", "WARN"] | None = Field(None, description="本次配置的异常处理方式，系统项及旧记录可为空")
    auto_reject_eligible: bool = Field(False, description="明确违规且支持证据可靠，允许自动模式拒绝")
    reviewable: bool | None = Field(None, description="是否可逐条人工确认；历史记录由语义规则编码兼容判断")
    reviewed: bool = Field(False, description="本审核快照中此项是否已人工确认")


class AuditOut(Schema):
    id: str = Field(description="本次审核 ID")
    rule_version: str = Field(description="本次使用的规则版本")
    attachment_version: str = Field(description="本次审核对应的附件版本")
    conclusion: str = Field(description="PASS / REVIEW / BLOCK")
    evidence: list[Evidence] = Field(description="逐条规则结论与证据")
    model_analysis: dict[str, Any] = Field(description="模型分析结果或未调用原因")
    created_at: datetime = Field(description="审核时间，UTC")


class ManualReviewInput(Schema):
    """财务整单或逐项复核；整单结论覆盖全部业务规则，执行权限与待办身份仍需校验。"""
    scope: Literal["ALL", "ITEMS"] = Field("ITEMS", description="ALL 为整单复核，无需等待 AI；省略时兼容旧客户端逐项复核")
    audit_id: str | None = Field(None, min_length=1, max_length=32, description="打开窗口时的审核 ID；ALL 允许尚无审核记录，ITEMS 必须提供")
    expected_version: int = Field(ge=1, description="打开复核窗口时的审批版本号")
    reviewed_codes: list[str] | None = Field(None, max_length=80, description="选择确认的业务条件或无规则人工确认项；省略时兼容旧客户端")
    comment: str = Field("已核对报销用途及票据，确认可报销", min_length=2, max_length=600,
                         description="简短复核意见，可使用默认意见，无需补填报销明细")

    @field_validator("comment", mode="before")
    @classmethod
    def trim_comment(cls, value):
        return value.strip() if isinstance(value, str) else value


class MaterialConfirmationInput(Schema):
    """人工确认付款材料类别，不直接确认票面字段或改变审批结果。"""
    material_type: Literal["INVOICE", "CONTRACT", "QUOTE", "OTHER"] = Field(description="财务核对原件后的材料类别")
    expected_version: int = Field(ge=1, description="打开确认窗口时的审批版本")
    comment: str = Field(min_length=2, max_length=600, description="核对原件及确认类别的依据")


class ManualReviewOut(Schema):
    """人工复核记录与它绑定的业务快照；历史审核和模型原始意见保持可追溯。"""
    audit_id: str = Field(description="人工复核生成的审核 ID")
    scope: Literal["ALL", "ITEMS"] = Field("ITEMS", description="整单或旧版逐项复核范围")
    source_audit_id: str = Field(description="被复核的原审核 ID")
    actor_id: str = Field(description="实际复核人的工作台身份")
    actor_name: str = Field(description="实际复核人姓名")
    comment: str = Field(description="简短复核意见")
    reviewed_codes: list[str] = Field(description="本次人工确认的文字标准或费用语义规则编码")
    confirmed_codes: list[str] = Field(default_factory=list, description="本业务快照累计确认的编码，供相同内容重审沿用")
    created_at: datetime = Field(description="复核时间，UTC")
    review_fingerprint: str = Field(description="兼容旧版逐项指纹；整单复核使用独立 content_fingerprint 判断有效性")
    content_fingerprint: str = Field("", description="整单业务内容、原件及人工票面字段指纹，不包含 AI 或审核配置")
    conflicting_claims: list[dict[str, Any]] = Field(default_factory=list, description="人工放行时的重复或冻结占用，保留原占用归属")
    promoted_from: str | None = Field(None, description="存量升级前的人工复核 ID；只在明确授权升级旧记录时填写")
    promoted_at: datetime | None = Field(None, description="存量复核升级时间，UTC；原复核时间保持不变")


class ActionInput(Schema):
    action: Literal["APPROVE", "REJECT"] = Field(description="只允许操作专用预审节点")
    comment: str = Field(min_length=2, max_length=600, description="必填处理意见")
    expected_version: int = Field(ge=1, description="用户查看审批时的版本号")
    idempotency_key: str = Field(min_length=8, max_length=100, description="客户端生成的唯一操作标识，重试必须复用")


class ActionOut(Schema):
    execution_source: Literal["STANDARD", "PAYMENT_AUTO_APPROVE"] = Field("STANDARD",
        description="原审核操作或付款仅采集放行；仅采集操作没有来源审核且只能同意指定节点")
    id: str = Field(description="操作记录 ID")
    action: str = Field(description="APPROVE / REJECT")
    actor_name: str = Field(description="实际工作台操作者")
    feishu_identity: str = Field(description="飞书执行身份 open_id")
    comment: str = Field(description="处理意见")
    status: str = Field(description="REQUESTED / EXECUTING / UNKNOWN / SUCCEEDED / FAILED")
    error: str = Field(description="失败原因，敏感信息已移除")
    source_audit_id: str | None = Field(None, description="自动操作或整单人工同意绑定的审核 ID；其他旧记录可为空")
    created_at: datetime = Field(description="操作时间，UTC")


class ClaimOut(Schema):
    id: str = Field(description="占用记录 ID")
    invoice_id: str = Field(description="关联发票 ID")
    approval_id: str | None = Field(description="关联审批，历史台账可为空")
    status: str = Field(description="RESERVED / USED / FROZEN / RELEASED / HISTORICAL")
    source_label: str = Field(description="审批编号或历史来源")


class FeishuFormField(Schema):
    label: str = Field(description="飞书实例中的控件名称，旧记录缺失时使用模板映射名称")
    control_id: str = Field("", description="飞书控件 ID，用于匹配原表单字段")
    kind: Literal["text", "rows", "attachment", "account"] = Field("text", description="文本、逐行明细、附件摘要或结构化账户")
    text: str | None = Field(None, description="可直接展示的值，空值为空，附件仅含名称或数量")
    attachment_count: int = Field(0, description="该附件控件的原始文件数量")
    rows: list[list["FeishuFormField"]] = Field(default_factory=list, description="按飞书原顺序排列的明细行，每行保留其字段顺序")
    accounts: list[dict[str, str]] = Field(default_factory=list, description="仅包含供付款账户格式化器使用的户名、账号、银行、支行、地区和类型")


class ReferencePdfSummary(Schema):
    """包内 PDF 的补充识别数据，与原预审和台账合计分别展示。"""

    pdf_count: int = Field(description="当前参考包内 PDF 文件数量")
    invoice_count: int = Field(description="已识别并按票据身份去重的发票张数")
    travel_count: int = Field(description="已读取的网约车行程单数量")
    review_count: int = Field(description="已尝试识别且仍需核对的 PDF 数量")
    pending_count: int = Field(description="尚未完成本地识别的 PDF 数量")
    amount: Decimal | None = Field(description="已识别发票的价税合计，无可用票面或同号金额冲突时为空")


class ApprovalDetail(ApprovalOut):
    budget_context: BudgetContext | None = Field(None, description="报销预算归属与独立预算实时指标；付款申请为空，预算提醒不改变审批结论")
    feishu_form: list[FeishuFormField] = Field(default_factory=list, description="工作台报销详情使用的本地飞书表单快照；按原顺序排列，无可用快照或为付款时为空")
    attachments: list[AttachmentOut] = Field(description="全部附件版本，包含历史版本")
    invoices: list[InvoiceOut] = Field(description="当前附件识别到的发票")
    invoice_totals: InvoiceTotalsOut | None = Field(None, description="报销审批中已识别发票的去重合计；尚未识别发票或为付款审批时为空")
    reference_pdf_summary: ReferencePdfSummary | None = Field(None, description="已通过或已撤销单据的历史包内 PDF 补充识别结果")
    claims: list[ClaimOut] = Field(description="本审批占用记录")
    audit: AuditOut | None = Field(description="最新审核结果")
    actions: list[ActionOut] = Field(description="人工与自动操作留痕")
    reviews: list[ManualReviewOut] = Field(default_factory=list, description="人工文字标准及费用语义复核记录，按时间倒序，排除后续重审沿用的重复快照")
    suggested_reject_comment: str = Field("", description="基于当前未解决项生成的易读拒绝建议，最多 600 字；只作草稿，不自动执行")
    feishu_url: str = Field(description="飞书原审批详情链接")


class CorrectionInput(Schema):
    number: str = Field(min_length=1, max_length=40, description="核对原件后的发票号码")
    code: str = Field("", max_length=20, description="传统发票代码，数电发票留空")
    buyer_name: str = Field(min_length=1, max_length=150, description="购买方名称")
    buyer_tax_id: str = Field(min_length=15, max_length=20, description="购买方税号")
    seller_name: str = Field("", max_length=150, description="销售方名称")
    issue_date: date = Field(description="开票日期")
    amount: Decimal = Field(gt=0, max_digits=18, decimal_places=2, description="票面价税合计")
    comment: str = Field(min_length=2, max_length=600, description="人工确认依据")
    expected_version: int = Field(ge=1, description="审批版本号")


class JobOut(Schema):
    id: str = Field(description="后台任务 ID")
    kind: str = Field(description="任务类型")
    status: str = Field(description="PENDING / RUNNING / SUCCEEDED / FAILED")
    attempts: int = Field(description="执行次数")
    error: str = Field(description="最近一次失败原因")
    result: dict[str, Any] = Field(description="执行结果")
    created_at: datetime = Field(description="创建时间，UTC")
    updated_at: datetime = Field(description="更新时间，UTC")


class ArchiveOut(Schema):
    id: str = Field(description="归档记录 ID")
    approval_id: str = Field(description="来源审批实例")
    attachment_id: str = Field(description="对应的原始附件版本")
    invoice_id: str | None = Field(description="关联发票")
    company_id: str | None = Field(description="归属主体，空表示待确认")
    archive_path: str = Field(description="主体税号 / 开票年月 / 费用类型目录")
    status: str = Field(description="ARCHIVED / QUARANTINED / REVOKED")
    manifest: dict[str, Any] = Field(description="原件哈希、审批、字段和审核版本清单")
    created_at: datetime = Field(description="归档时间，UTC")


class ImportOut(Schema):
    id: str = Field(description="导入批次 ID")
    filename: str = Field(description="上传台账文件名")
    status: str = Field(description="UPLOADED / PREVIEWED / IMPORTING / COMPLETED")
    columns: list[str] = Field(description="原表格列名")
    mapping: dict[str, str] = Field(description="标准字段到原列名的映射")
    preview: list[dict[str, Any]] = Field(description="标准化行预览，最多返回前 100 行")
    errors: list[dict[str, Any]] = Field(description="行号和具体错误")
    imported: int = Field(description="成功导入行数")
    row_count: int = Field(0, description="文件中的数据总行数")
    valid_count: int = Field(0, description="通过预览校验的有效行数")


class Message(Schema):
    message: str = Field(description="中文操作结果说明")


class InvoiceEntry(InvoiceOut):
    claim: ClaimOut | None = Field(description="当前有效占用，空表示已释放或未占用")
    attachment_id: str | None = Field(description="可通过鉴权原件接口下载的附件 ID")


class ClaimReleaseInput(Schema):
    comment: str = Field(min_length=5, max_length=600, description="付款、入账状态的核查依据")
    payment_and_bookkeeping_checked: bool = Field(description="明确确认已核查付款与入账情况，必须为 true")


class ActivityOut(Schema):
    id: str = Field(description="审计记录 ID")
    actor: str = Field(description="操作者姓名或系统处理器名称")
    action: str = Field(description="执行的具体业务动作")
    target: str = Field(description="审批编号或配置对象")
    detail: dict[str, Any] = Field(default_factory=dict, description="决策版本、处理依据等结构化留痕")
    created_at: datetime = Field(description="操作时间，UTC")


class TrendPoint(Schema):
    date: str = Field(description="统计日期，YYYY-MM-DD")
    amount: Decimal = Field(description="当日采集审批的总金额")
    count: int = Field(description="当日采集审批数量")


class RiskCount(Schema):
    name: str = Field(description="未通过规则名称")
    count: int = Field(description="当前待办中的命中次数")


class OverviewOut(Schema):
    approval_amount: Decimal = Field(description="所选主体累计审批金额")
    approval_count: int = Field(description="累计审批单数")
    pending_count: int = Field(description="当前专用节点待处理审批数")
    review_count: int = Field(description="需要复核或阻断的当前待办数")
    pass_count: int = Field(description="预审通过且等待处理的审批数")
    block_count: int = Field(description="被硬性规则阻断的待办数")
    invoice_count: int = Field(description="统一票据库发票数")
    archived_count: int = Field(description="正常归档的原件数")
    quarantined_count: int = Field(description="归属或占用待确认的原件数")
    failed_jobs: int = Field(description="失败后台任务数")
    queue_count: int = Field(description="等待或正在执行的任务数")
    trend: list[TrendPoint] = Field(description="近 30 天采集趋势")
    risks: list[RiskCount] = Field(description="当前未通过规则分布")
    recent_approvals: list[ApprovalOut] = Field(description="最近待处理审批")
    activity: list[ActivityOut] = Field(description="最近业务处理动态")


class SystemOut(Schema):
    mode: str = Field(description="demo / live，业务数据彼此隔离")
    job_mode: Literal["local"] = Field(description="local：API 进程内执行数据库持久化任务")
    database: Literal["sqlite"] = Field(description="sqlite：按业务模式隔离的本地持久化数据库")
    storage: str = Field(description="持久化原件存储方式")
    model_configured: bool = Field(description="模型服务必要配置是否完整")
    model_name: str = Field(description="配置的模型名称，不返回密钥")
    model_source: Literal["environment", "database"] = Field(description="当前模型配置来源：部署环境或工作台数据库")
    agent_engine: str = Field(description="执行语义审核的 Agent 引擎及版本")
    ocr_enabled: bool = Field(description="工作台是否启用腾讯云发票 OCR")
    ocr_configured: bool = Field(description="是否已启用且可读取完整腾讯云凭据")
    ocr_state: Literal["DISABLED", "UNVERIFIED", "READY", "FAILED"] = Field(
        description="当前配置版本的业务调用状态，依次表示停用、尚未调用、最近调用成功或失败")
    ocr_last_error: str = Field(description="最近一次腾讯云业务调用的脱敏错误，或当前凭据读取错误")
    ocr_last_elapsed_ms: int | None = Field(description="最近一次发票 OCR 调用耗时，毫秒，尚未调用时为空")
    feishu_configured: bool = Field(description="飞书应用凭据是否存在")
    public_url: str = Field(description="外部工作台地址")
    verification: str = Field(description="UNVERIFIED：未接入税务验真服务")


class OCRConfigInput(Schema):
    """管理员保存腾讯云发票识别开关和一组完整凭据。"""
    version: int = Field(ge=0, description="读取时的配置版本，用于拒绝并发覆盖")
    enabled: bool = Field(description="是否启用腾讯云发票 OCR，启用需要一组可读取的凭据")
    secret_id: SecretStr = Field(SecretStr(""), max_length=200, description="腾讯云 SecretId，和 SecretKey 同时填写或留空")
    secret_key: SecretStr = Field(SecretStr(""), max_length=200, description="腾讯云 SecretKey，和 SecretId 同时填写或留空")
    clear_credentials: bool = Field(False, description="清除已保存凭据并停用腾讯云发票 OCR")


class OCRConfigOut(Schema):
    """仅公开配置状态，凭据的值由服务端持有。"""
    version: int = Field(description="保存或测试时应提交的配置版本")
    enabled: bool = Field(description="腾讯云发票 OCR 生效开关")
    credentials_configured: bool = Field(description="是否保存过完整凭据，解密错误单独展示")
    credential_error: str = Field(description="脱敏的凭据读取错误，可用时为空")


class OCRConnectionOut(Schema):
    """样票测试摘要，样票文字和凭据均由服务端处理。"""
    success: bool = Field(description="是否成功从样票取得发票字段")
    message: str = Field(description="识别成功说明或脱敏失败原因")
    elapsed_ms: int = Field(description="样票识别耗时，毫秒")
    request_id: str = Field(description="腾讯云请求 ID，未取得时为空")


class SyncConfigInput(Schema):
    """管理员保存自动同步策略；启停不会限制手动同步或飞书事件处理。"""
    version: int = Field(ge=0, description="读取时的配置版本，防止管理员并发覆盖")
    enabled: bool = Field(description="是否启用自动增量补偿同步；演示模式始终不执行真实同步")
    interval_seconds: int = Field(ge=1, le=86400, description="自动同步间隔，单位秒，整数，范围 1 至 86400")


class SyncConfigOut(SyncConfigInput):
    """生效配置与预计下次入队时间；队列繁忙时实际执行可能延后。"""
    source: Literal["environment", "database"] = Field(description="首次保存前使用部署环境，之后使用工作台配置")
    next_run_at: datetime | None = Field(description="预计下次允许自动入队时间，UTC；关闭或演示模式时为空")


class ModelConfigInput(Schema):
    """模型配置草稿。测试连接使用相同契约，但不会保存或触发业务重审。"""
    version: int = Field(ge=0, description="读取配置得到的版本号，保存时用于检查并发覆盖")
    enabled: bool = Field(description="是否启用模型语义审核；启用时必须有完整连接信息")
    base_url: str = Field("", max_length=2048, description="HTTP(S) API 根地址，包含 /v1 等前缀，不包含 /chat/completions")
    model_name: str = Field("", max_length=200, description="模型服务接受的模型 ID")
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), description="仅写入的 API Key；留空保留现有有效密钥")
    clear_api_key: bool = Field(False, description="显式清除密钥并停用模型，不回退部署环境密钥")
    default_prompt: str = Field(min_length=1, max_length=20000, description="全局默认业务审核要求，系统会追加权限和 JSON 格式约定")

    @field_validator("base_url", "model_name", "default_prompt", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("base_url")
    @classmethod
    def validate_endpoint(cls, value):
        from urllib.parse import urlsplit
        if value:
            endpoint = urlsplit(value)
            if (endpoint.scheme not in {"http", "https"} or not endpoint.hostname or endpoint.username
                    or endpoint.password or endpoint.query or endpoint.fragment):
                raise ValueError("模型地址必须是无账号、查询参数和片段的 HTTP(S) 根地址")
            _ = endpoint.port
            if endpoint.path.rstrip("/").endswith("/chat/completions"):
                raise ValueError("请填写 API 根地址，不包含 /chat/completions")
        return value.rstrip("/")

    @field_validator("api_key")
    @classmethod
    def limit_key(cls, value):
        if len(value.get_secret_value()) > 8192:
            raise ValueError("密钥长度超出限制")
        return value


class ModelConfigOut(Schema):
    payment_default_prompt: str = Field("", description="付款模板未覆盖节点提示词时使用的业务审核要求")
    """供管理员编辑的脱敏配置。响应中永远不包含 API Key 或密文。"""
    version: int = Field(description="配置版本，保存时须原样回传")
    connection_version: int = Field(description="模型连接版本，供审核追溯")
    source: Literal["environment", "database"] = Field(description="部署环境回退或工作台配置")
    enabled: bool = Field(description="是否启用模型语义审核")
    base_url: str = Field(description="当前 API 根地址")
    model_name: str = Field(description="当前模型 ID")
    api_key_configured: bool = Field(description="是否已存在密钥，仅返回状态")
    credential_error: str = Field(description="密钥无法解密时的修复提示，不含内部错误")
    configured: bool = Field(description="已启用且必要连接信息完整、密钥可用")
    default_prompt: str = Field(description="当前全局业务审核要求")
    builtin_prompt: str = Field(description="系统内置业务审核要求，用于恢复默认")
    system_constraints: str = Field(description="由系统固定追加的权限边界与 JSON 输出约定")
    engine: str = Field(description="固定的 Agent 引擎及版本")


class ModelConnectionOut(Schema):
    success: bool = Field(description="是否通过 Harness 完成调用并校验结构化输出")
    message: str = Field(description="脱敏的连接结果说明")
    latency_ms: int = Field(description="连接检查总耗时，单位毫秒")
    call_id: str | None = Field(None, description="本次 AI 调用日志 ID，可查询思考内容、原始输出与阶段事件")


class AICallSummary(Schema):
    """分页列表不携带大段提示词、输入和输出，避免历史日志拖慢页面。"""
    id: str = Field(description="AI 调用日志 ID")
    source: Literal["AUDIT", "CONNECTION_TEST", "INVOICE_EXTRACTION", "PAYMENT_MATERIAL", "AGENT_ATTACHMENT"] = Field(description="审批审核、连接测试、发票识别、付款材料读取或私人附件解析")
    approval_id: str | None = Field(description="关联审批 ID，连接测试为空")
    template_code: str = Field(description="关联审批模板编码")
    actor: str = Field(description="触发调用的操作者")
    engine: str = Field(description="调用时使用的 Agent 引擎与版本")
    model_name: str = Field(description="实际使用的模型名称")
    config_version: int = Field(description="调用时读取的模型配置版本")
    session_id: str = Field(description="Harness 独立会话 ID")
    status: Literal["RUNNING", "COMPLETED", "FAILED", "SKIPPED"] = Field(description="运行中、完成、失败或跳过")
    stage: str = Field(description="最近一次可见调用阶段")
    has_reasoning: bool = Field(description="供应商是否明确返回思考内容；不从最终答案推断")
    has_output: bool = Field(description="是否收到原始输出")
    usage: dict[str, int] = Field(description="供应商返回的 Token 用量，未提供时为空")
    finish_reason: str = Field(description="Harness 报告的完成原因")
    error_type: str = Field(description="脱敏错误类别，不含完整异常或请求正文")
    error_message: str = Field(description="可供用户排查的失败或跳过原因")
    truncated: bool = Field(description="文本或阶段事件是否达到日志长度上限")
    started_at: datetime = Field(description="调用开始时间，UTC")
    finished_at: datetime | None = Field(description="调用结束时间，UTC；运行中为空")
    duration_ms: int = Field(description="已记录的耗时，单位毫秒")


class AICallDetail(AICallSummary):
    prompt: str = Field(description="实际系统提示词，包含固定约束，已隐藏已知密钥")
    input_json: str = Field(description="本次结构化输入的 JSON 文本，已脱敏")
    reasoning: str = Field(description="仅保存模型明确返回的 reasoning/thinking 文本，未提供时为空")
    raw_output: str = Field(description="结构化解析前的最终输出；校验失败时仍保存已收到内容")
    parsed_result: dict[str, Any] = Field(description="通过业务解析得到的结果，或失败/跳过状态")
    events: list[dict[str, Any]] = Field(description="按时间排列的白名单调用阶段事件")
