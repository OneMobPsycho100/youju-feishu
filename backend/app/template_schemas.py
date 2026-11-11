"""审批模板迁移契约：仅包含配置与定义描述，不承载凭据、实例或运行状态。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schemas import AuditRules, FieldMapping, NodeId, TemplateOut


class TransferSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DefinitionField(TransferSchema):
    """控件描述仅用于选择和迁移匹配，path 为所在明细的名称路径。"""
    id: str = Field(max_length=100, description="飞书真实控件 ID，运行时映射最终保存此值")
    custom_id: str = Field("", max_length=100, description="自定义控件 ID，仅作为跨模板匹配线索")
    name: str = Field(max_length=300, description="控件名称")
    type: str = Field(max_length=100, description="飞书控件类型")
    path: list[str] = Field(default_factory=list, max_length=32, description="外层明细或控件组名称路径")


class DefinitionNode(TransferSchema):
    id: str = Field(max_length=100, description="飞书真实节点 ID")
    custom_id: str = Field("", max_length=100, description="自定义节点 ID，仅用于匹配")
    name: str = Field(max_length=300, description="节点名称")
    type: str = Field("", max_length=100, description="飞书节点类型")


class ApprovalDefinition(TransferSchema):
    code: str = Field(description="解析后的 approval_code")
    name: str = Field(description="飞书审批定义名称")
    status: str = Field("", description="飞书定义状态")
    fields: list[DefinitionField] = Field(default_factory=list, description="展开明细后的叶子控件")
    nodes: list[DefinitionNode] = Field(default_factory=list, description="供管理员核对的定义节点；飞书可能同时返回发起和结束节点，须明确选择专用预审节点")
    fingerprint: str = Field("", description="定义摘要；提交时重新读取并比对，避免沿用过期映射")
    demo: bool = Field(False, description="是否为独立演示定义")


class PortableTemplate(TransferSchema):
    source_code: str = Field(min_length=1, max_length=100, description="源模板编码，仅用于标识包内条目")
    name: str = Field(min_length=1, max_length=150, description="工作台模板名称")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] = Field(description="模板业务类型")
    field_mapping: FieldMapping = Field(description="源控件映射；导入时必须转换为目标真实 ID")
    rules: AuditRules = Field(description="完整审核规则，保留规则结构版本和稳定条目 ID")
    agent_prompt: str = Field("", max_length=20000, description="模板提示词；空值继承目标系统默认要求")
    prompt_source: Literal["INHERIT", "OVERRIDE"] = Field(description="提示词继承或覆盖状态")
    fields: list[DefinitionField] = Field(default_factory=list, max_length=3000, description="用于匹配的源控件描述")
    nodes: list[DefinitionNode] = Field(default_factory=list, description="版本 2 的源专用预审节点描述，未读取的定义仅保留源 ID")
    node: DefinitionNode | None = Field(None, description="版本 1 的单个源专用预审节点描述")
    definition_warning: str = Field("", max_length=500, description="源定义读取失败或缺失信息的说明")


class TemplateBundle(TransferSchema):
    schema_version: int = Field(strict=True, description="迁移文件格式版本，导出使用 2，导入支持 1 和 2")
    exported_at: datetime = Field(description="导出时间")
    templates: list[PortableTemplate] = Field(min_length=1, max_length=100, description="单个或批量模板配置")


class TemplateInspectInput(TransferSchema):
    reference: str = Field(min_length=1, max_length=2048, description="approval_code 或含 definitionCode 的飞书模板链接")
    source: PortableTemplate | None = Field(None, description="可选源配置，用于生成跨模板匹配建议")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] | None = Field(None, description="管理员明确指定的业务类型；未指定时按模板名称识别")
    node_ids: list[NodeId] | None = Field(None, description="当前模板的节点编辑草稿，提供时按此范围识别样本中的审批人")
    sample_instance: str = Field("", max_length=100, description="可选样本 instance_code，仅从所选预审节点的真实待办识别审批人")


class TemplateInspectOut(TransferSchema):
    definition: ApprovalDefinition = Field(description="目标审批定义")
    field_mapping: FieldMapping = Field(description="唯一匹配的目标控件建议，未匹配项留空")
    node_ids: list[str] = Field(default_factory=list, description="已配置或识别的目标专用预审节点 ID")
    node_id: str = Field("", description="只有一个节点时返回的兼容字段")
    unresolved_fields: list[str] = Field(default_factory=list, description="需要重新绑定或明确清除的字段")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] = Field("REIMBURSEMENT", description="识别后的业务类型")
    reviewer_open_id: str = Field("", description="当前应用内已配置或真实待办验证的审批人，不默认使用当前管理员")
    reviewer_name: str = Field("", description="当前工作台已知的审批人姓名；未知时留空")
    reviewer_source: Literal["SAVED", "PENDING_TASK", "NONE"] = Field("NONE", description="审批人来自当前模板配置、真实待办或尚未识别")
    existing_template: TemplateOut | None = Field(None, description="同编码模板已接入时返回现有配置，避免添加流程覆盖已有规则")
    notices: list[str] = Field(default_factory=list, description="需要处理的歧义及自动识别说明")


class TemplateExportInput(TransferSchema):
    codes: list[str] = Field(min_length=1, max_length=100, description="要导出的本地模板编码")


class TemplateBinding(TransferSchema):
    """导入草稿必须显式提交目标绑定，不能把源编码、节点或审批人作为默认目标。"""
    source_code: str = Field(min_length=1, max_length=100, description="关联包内源模板")
    selected: bool = Field(True, description="是否导入此条目")
    target_reference: str = Field("", max_length=2048, description="目标模板链接或编码")
    name: str = Field("", max_length=150, description="导入后的模板名称，留空使用源配置名称")
    node_ids: list[NodeId] | None = Field(None, description="确认后的全部目标专用预审节点 ID，必须来自当前定义")
    node_id: str = Field("", max_length=100, description="旧版单节点绑定，仅省略 node_ids 时使用")
    node_confirmed: bool = Field(False, description="管理员已确认所选目标专用预审节点")
    reviewer_open_id: str = Field("", max_length=100, description="目标应用中的审批人身份，必须重新填写或选择")
    field_mapping: FieldMapping = Field(default_factory=FieldMapping, description="目标真实控件 ID")
    cleared_fields: list[str] = Field(default_factory=list, max_length=20, description="管理员明确清除的源可选字段；attachment_ids 表示明确改读全部附件")
    attachment_scope_confirmed: bool = Field(False, description="源指定多个附件而目标减少选择时，管理员已确认新的附件范围")
    conflict_action: Literal["SKIP", "OVERWRITE"] = Field("SKIP", description="已有同编码模板时默认跳过，覆盖必须显式指定")
    definition_fingerprint: str = Field("", max_length=64, description="预览时目标定义摘要")
    expected_revision: str | None = Field(None, max_length=64, description="预览时目标配置摘要；空字符串表示当时不存在")


class TemplatePreviewInput(TransferSchema):
    bundle: TemplateBundle = Field(description="已解析的 JSON 配置包")
    bindings: list[TemplateBinding] = Field(default_factory=list, max_length=100, description="各条目的目标草稿，首次上传可留空")


class TemplatePreviewItem(TransferSchema):
    source_code: str = Field(description="源模板编码")
    binding: TemplateBinding = Field(description="重新匹配后的目标草稿")
    definition: ApprovalDefinition | None = Field(None, description="已读取的目标定义")
    conflict: bool = Field(False, description="目标编码是否已存在")
    unresolved_fields: list[str] = Field(default_factory=list, description="待处理的源映射")
    warnings: list[str] = Field(default_factory=list, description="需要管理员查看的说明")
    error: str = Field("", description="目标定义读取错误，不影响其他条目的预览")


class TemplatePreviewOut(TransferSchema):
    items: list[TemplatePreviewItem] = Field(description="逐条预览结果；此接口不会保存或订阅")


class TemplateImportOut(TransferSchema):
    templates: list[TemplateOut] = Field(description="本次原子保存的模板，全部未启用且处于观察模式")
    skipped: list[str] = Field(description="未选择或因冲突跳过的源模板编码")
