import type { BudgetContext } from "./budgetTypes";

/** 工作台角色由后端鉴权，前端隐藏操作仅用于改善交互。 */
export interface User {
  id: string;
  name: string;
  role: "admin" | "finance" | "viewer";
  enabled: boolean;
}
/** 多主体以购买方税号识别，名称和别名用于辅助核对。 */
export interface Company {
  id: string;
  name: string;
  tax_id: string;
  aliases: string[];
  active: boolean;
}
/** 审核结论与飞书实例状态分别保存，不把预审通过解释为审批结束。 */
export type Decision = "PENDING" | "PASS" | "REVIEW" | "BLOCK";
/** 整轮预审的执行进度；已完成表示结论已保存，是否通过由 Decision 表达。 */
export type AIApprovalStatus = "NOT_STARTED" | "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED";
/** 审批固化的业务类型，付款单不计入报销统计。 */
export type BusinessType = "REIMBURSEMENT" | "PAYMENT";
export type MaterialType = "INVOICE" | "CONTRACT" | "QUOTE" | "OTHER" | "UNKNOWN";
/** 付款表单信息；账户保留飞书原始值，由详情页解析控件显示文本。 */
export interface PaymentDetails {
  company_name: string;
  company_id: string | null;
  project: string;
  payment_method: string;
  expected_payment_date: string;
  /** 兼容普通文本、JSON 字符串及含 widgetAccount* 子字段的个人／对公账户控件。 */
  payee_account: unknown;
  related_approvals: unknown;
  remarks: string;
  material_issues: { key: string; filename: string; reason: string }[];
}
export interface Approval {
  id: string;
  template_code: string;
  business_type: BusinessType;
  payment_details: PaymentDetails;
  /** 付款仅采集模式优先于原审核配置；业务规则和解析异常不阻止专用节点自动同意。 */
  payment_auto_approve_enabled: boolean;
  /** 独立材料采集进度，完成不表示已通过付款审核或最终审批。 */
  payment_collection_status: AIApprovalStatus;
  payment_collection_task_id: string | null;
  /** 材料读取、票面识别与占用异常仅作记录。 */
  payment_collection_issues: { code: string; filename: string; detail: string }[];
  /** 当前专用节点的自动同意状态，与飞书最终审批及采集进度独立。 */
  payment_auto_approval_status: "NOT_STARTED" | "REQUESTED" | "EXECUTING" | "UNKNOWN" | "SUCCEEDED" | "FAILED";
  serial_number: string;
  title: string;
  /** 申请人展示姓名，优先使用工作台登录姓名，其次使用已补齐的飞书通讯录姓名，缺失时沿用审批原值。 */
  applicant: string;
  /** 审批发起人身份，优先使用 open_id，仅有 user_id 时带 user_id: 前缀，历史未补齐时为空。 */
  applicant_id: string | null;
  /** 申请提交时间（UTC），与工作台采集时间 created_at 独立。 */
  submitted_at: string | null;
  department: string;
  amount: string;
  expense_type: string;
  /** 票面税号已匹配的配置主体 ID；预审结论独立判断。 */
  company_ids: string[];
  /** 表单申报主体名称，与审核使用相同的名称映射或控件来源，不代表票据已匹配。 */
  declared_company_names: string[];
  /** 当前附件中尚未匹配主体的票面购买方名称。 */
  unmatched_company_names: string[];
  status: string;
  decision: Decision;
  /** 有效整单人工结论优先于规则和 AI；只有业务内容或原件变化才失效。 */
  manual_review_valid?: boolean;
  /** 从排队、附件处理到结果保存的独立进度，与飞书审批状态分别展示。 */
  ai_status: AIApprovalStatus;
  /** 所属模板是否允许 AI 预审，关闭时由财务整单人工复核。 */
  ai_approval_enabled: boolean;
  archive_status: string;
  /** 匹配到的专用预审待办 ID，无匹配时为空。 */
  task_id: string;
  /** 专用预审待办状态。MISSING 为未找到匹配待办，AMBIGUOUS 为匹配到多个。 */
  task_status: string;
  /** 人工操作必须带回此版本号，防止过期页面回写。 */
  version: number;
  attachment_version: string;
  created_at: string;
  updated_at: string;
}
/** 票面金额按字符串传输，所有金额校验由后端 Decimal 完成。 */
export interface InvoiceFields {
  number?: string;
  code?: string;
  buyer_name?: string;
  buyer_tax_id?: string;
  seller_name?: string;
  issue_date?: string;
  amount?: string;
  invoice_key?: string;
  kind?: string;
}
/** 各路独立提取结果；OCR 坐标以该页渲染后的像素为单位，置信度只用于展示依据。 */
export interface RecognitionSource {
  status: string;
  fields: InvoiceFields;
  error?: string;
  elapsed_ms?: number;
  /** 同一原件与配置下复用成功的模型识别，不重复调用。 */
  cached?: boolean;
  clear_fields?: Partial<Record<keyof InvoiceFields, boolean>>;
  pages?: {
    page: number;
    width: number;
    height: number;
    /** 腾讯云返回的页级请求 ID。 */
    request_id?: string;
    lines: { text: string; score?: number | null; box: number[][] }[];
  }[];
}
/** PDF 本地优先；图片 OCR 主识别，模型仅在同票身份一致时补缺。 */
export interface RecognitionComparison {
  field: string;
  label: string;
  required: boolean;
  values: Record<string, string>;
  accepted: string;
  matched_sources: string[];
  status: "AGREED" | "CONFLICT" | "MISSING" | "NOT_REQUIRED";
}
/** 来源提示与识别资格分别保存；旧记录可能尚无识别结论。 */
/** 压缩包原件的展开结果。成员仍使用正常附件 ID 预览和下载。 */
export interface AttachmentArchive {
  status: "READY" | "FAILED";
  mode: "AUDIT" | "REFERENCE";
  error: string;
  members: { attachment_id: string; path: string }[];
}

export interface ParsedData {
  /** 整份内容分类独立于字段来源；分类失败不能当作普通附件跳过。 */
  classification?: {
    status: "COMPLETED" | "UNKNOWN" | "FAILED";
    material_type: MaterialType;
    complete: boolean;
    reason: string;
    source: "MODEL" | "MANUAL";
    pages: number[];
    invoice_pages: number[];
    invoice_count: number;
    version: string;
    model_call_id?: string;
    cached?: boolean;
  } | null;
  /** 当前 PDF、图片分类及字段采用版本；不改变历史参考材料用途。 */
  document_policy_version?: string;
  archive?: AttachmentArchive | null;
  /** 历史包内 PDF 的本地识别结果，与台账状态独立。 */
  reference_pdf?: "INVOICE" | "TRAVEL" | "REVIEW" | null;
  /** 普通附件跳过发票解析，不计入发票金额与完整性。 */
  parse_status?: "PARSED" | "SKIPPED" | "FAILED" | null;
  skip_reason?: string;
  material_type?: MaterialType;
  /** 保留完整材料片段及原件定位；读取失败不能以截断正文冒充完整读取。 */
  material?: {
    complete: boolean;
    summary: string;
    error: string;
    segments: { location: string; text: string }[];
    confirmation?: { material_type: MaterialType; actor_name: string; comment: string };
  };
  fields?: InvoiceFields;
  source?: string;
  reliable?: boolean;
  confirmed?: boolean;
  confirmed_by?: string;
  confirmation_comment?: string;
  raw_text?: string;
  error?: string;
  pipeline_version?: string;
  /** 原件实际类型；PDF 的模型兜底与图片交叉核对分别展示。 */
  file_type?: string;
  /** 独立 PDF 解析版本，更新时不使其他识别策略全量失效。 */
  pdf_parser_version?: string;
  sources?: Record<string, RecognitionSource>;
  recognition?: {
    /** LOCAL 为 PDF 本地完整；VERIFIED 为模型兜底或历史交叉核对；OCR 为图片 OCR 主识别完成。 */
    status: "LOCAL" | "VERIFIED" | "STRUCTURED" | "OCR" | "UNCERTAIN";
    reason: string;
    comparisons: RecognitionComparison[];
  };
  model_call_id?: string;
  local_error?: string;
}
export interface Attachment {
  id: string;
  invoice_id: string | null;
  filename: string;
  sha256: string;
  mime: string;
  size: number;
  active: boolean;
  parsed_data: ParsedData;
}
export interface Claim {
  id: string;
  invoice_id: string;
  approval_id: string | null;
  status: string;
  source_label: string;
}
/** 页面展示用的账单类型、报销单类别和可从票面核对的补充金额。 */
export interface InvoiceDisplayDetails {
  billing_type: string | null;
  expense_type: string | null;
  /** 票面总税额和不含税金额，单位元，以十进制字符串传输。 */
  tax_amount: string | null;
  untaxed_amount: string | null;
  /** 单票价税合计，单位元，以十进制字符串展示为可报销金额。 */
  reimbursable_amount: string | null;
  /** 按主体规则确认的抵扣税率，13 表示 13%；缺失或冲突时为空。 */
  deductible_tax_rate: string | null;
  /** 按主体规则计算的抵扣税额，单位元，十进制字符串；待确认时为空。 */
  deductible_tax_amount: string | null;
}
/** 当前审批的已识别发票按票去重，金额缺项时对应列合计为空。 */
export interface InvoiceTotals {
  amount: string;
  tax_amount: string | null;
  untaxed_amount: string | null;
  /** 去重后的抵扣税额合计，单位元；任一张发票待确认时为空。 */
  deductible_tax_amount: string | null;
}
/** 主体在指定票种和审批费用类型下的抵扣方式。 */
export interface InvoiceDeductionRule {
  billing_type: string;
  expense_type: string;
  /** FULL 按票面税额全额抵扣，NONE 明确不抵扣。 */
  mode: "FULL" | "NONE";
}
/** 管理员读取的主体规则；保存时将 version 作为 expected_version 提交。 */
export interface InvoiceDeductionConfiguration {
  version: number;
  rules: InvoiceDeductionRule[];
  billing_types: string[];
}
export interface Invoice extends Required<InvoiceFields> {
  id: string;
  company_id: string | null;
  verification: string;
  historical: boolean;
  created_at: string;
  display_details?: InvoiceDisplayDetails;
  claim?: Claim | null;
  attachment_id?: string | null;
}
/** 每条规则同时提供结论、风险级别和可定位的证据。 */
export interface Evidence {
  code: string;
  title: string;
  /** 当前有效结论，包含人工确认；原始事实保存在 result。INFO 即使为 false 也仅作提示。 */
  passed: boolean;
  severity: "INFO" | "REVIEW" | "BLOCK";
  detail: string;
  /** 给报销人看的具体原因和处理建议；detail 继续保留原始核对证据。 */
  user_message?: string | null;
  related_approval: string | null;
  /** 历史审核缺少以下字段时，仍按 passed / severity 展示原结论。 */
  source?: "BUILTIN" | "CUSTOM" | "SYSTEM" | null;
  result?: "PASS" | "FAIL" | "UNKNOWN" | "SKIPPED" | null;
  on_failure?: RuleAction | null;
  auto_reject_eligible?: boolean;
  reviewable?: boolean | null;
  reviewed?: boolean;
}
export interface AuditRun {
  id: string;
  rule_version: string;
  attachment_version: string;
  conclusion: Decision;
  evidence: Evidence[];
  model_analysis: {
    status: string;
    summary: string;
    risk_level?: string;
    findings?: string[];
    /** 模型逐条原始判断与配置快照；处理动作由服务端决定。 */
    rule_results?: { id: string; result: "PASS" | "FAIL" | "UNKNOWN"; reason: string }[];
    rules_snapshot?: AuditRules;
    /** 实际调用时的引擎、模型和有效提示词快照；历史记录可能没有这些字段。 */
    engine?: string;
    model_name?: string;
    config_version?: number;
    connection_version?: number;
    prompt_source?: "global" | "node";
    prompt?: string;
    config_fingerprint?: string;
    /** 独立 AI 调用日志，可追溯输入、模型可见思考与解析前输出。 */
    call_id?: string;
    /** 旧版逐项复核指纹；整单结论由 manual_review.content_fingerprint 独立判断。 */
    review_fingerprint?: string;
    manual_review?: ManualReview;
  };
  created_at: string;
}
/** 财务整单或旧版逐项复核，独立保留原始审核证据和实际处理依据。 */
export interface ManualReview {
  scope?: "ALL" | "ITEMS";
  audit_id: string;
  source_audit_id: string;
  actor_id: string;
  actor_name: string;
  comment: string;
  reviewed_codes: string[];
  /** 同一业务内容下累计确认的条目，供定时重审复用。 */
  confirmed_codes?: string[];
  created_at: string;
  review_fingerprint: string;
  /** 整单内容及人工字段指纹，不包含规则、提示词或模型配置。 */
  content_fingerprint?: string;
  conflicting_claims?: { invoice_id: string; claim_id: string; approval_id: string | null; status: string; source_label: string }[];
  /** 明确授权存量升级的来源及升级时间；原复核时间和作者保持不变。 */
  promoted_from?: string | null;
  promoted_at?: string | null;
}
export interface ActionRecord {
  /** STANDARD 为原审核操作；PAYMENT_AUTO_APPROVE 为仅采集自动同意。 */
  execution_source: "STANDARD" | "PAYMENT_AUTO_APPROVE";
  id: string;
  action: string;
  actor_name: string;
  feishu_identity: string;
  comment: string;
  status: string;
  error: string;
  /** 自动操作或整单人工同意绑定的审核快照；其他旧操作可为空。 */
  source_audit_id?: string | null;
  created_at: string;
}
/** 飞书表单的只读展示字段，标签和明细行沿用实例快照顺序。 */
export interface FeishuFormField {
  label: string;
  kind: "text" | "rows" | "attachment" | "account";
  /** 可直接显示的文字，附件控件只含文件名或数量。 */
  text: string | null;
  /** 每行是独立的控件列表，允许明细内继续包含控件组。 */
  rows: FeishuFormField[][];
  /** 仅包含现有付款账户格式化器所需的显示值。 */
  accounts: Record<string, string>[];
}

export interface ApprovalDetail extends Approval {
  /** 本单预算事实及已包含本单占用的实时指标；付款申请为空。 */
  budget_context?: BudgetContext | null;
  /** 工作台报销详情的本地飞书表单快照，兼容尚未返回此字段的服务端。 */
  feishu_form?: FeishuFormField[];
  attachments: Attachment[];
  invoices: Invoice[];
  /** 无已识别发票或为付款审批时为空。 */
  invoice_totals?: InvoiceTotals | null;
  /** 已归档参考包内 PDF 的票面汇总，金额为去重后的十进制字符串。 */
  reference_pdf_summary?: {
    pdf_count: number;
    invoice_count: number;
    travel_count: number;
    review_count: number;
    pending_count: number;
    amount: string | null;
  } | null;
  claims: Claim[];
  audit: AuditRun | null;
  actions: ActionRecord[];
  reviews: ManualReview[];
  feishu_url: string;
  /** 当前未解决问题的易读拒绝草稿，财务可在确认框中编辑后提交。 */
  suggested_reject_comment?: string;
}
export interface Job {
  id: string;
  kind: string;
  status: string;
  attempts: number;
  error: string;
  result: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}
export interface Page<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
}
export interface Activity {
  id: string;
  actor: string;
  action: string;
  target: string;
  detail?: Record<string, unknown>;
  created_at: string;
}
export interface Overview {
  approval_amount: string;
  approval_count: number;
  pending_count: number;
  review_count: number;
  pass_count: number;
  block_count: number;
  invoice_count: number;
  archived_count: number;
  quarantined_count: number;
  failed_jobs: number;
  queue_count: number;
  trend: { date: string; amount: string; count: number }[];
  risks: { name: string; count: number }[];
  recent_approvals: Approval[];
  activity: Activity[];
}
export interface ArchiveRecord {
  id: string;
  approval_id: string;
  attachment_id: string;
  invoice_id: string | null;
  company_id: string | null;
  archive_path: string;
  status: string;
  manifest: {
    filename: string;
    sha256: string;
    size: number;
    serial_number: string;
    invoice_number: string | null;
  };
  created_at: string;
}
/** 模板控件使用稳定的字段 ID 映射，支持明细中的多个主体。 */
export interface FieldMapping {
  amount: string;
  company_tax_id: string;
  expense_type: string;
  description: string;
  attachment_ids: string[];
  /** 报销或付款的申报主体名称控件 ID；按法定名称或允许别名匹配，不能填税号控件。 */
  company?: string;
  /** 付款及补充字段的飞书控件 ID，空值兼容旧模板。 */
  department?: string;
  project?: string;
  payment_method?: string;
  expected_payment_date?: string;
  payee_account?: string;
  related_approvals?: string;
  remarks?: string;
}
/** 违规处理方式；WARN 仅提示，UNKNOWN 始终转人工核对。 */
export type RuleAction = "REJECT" | "REVIEW" | "WARN";
export type BuiltinRuleCode = "DUPLICATE" | "COMPANY" | "BUYER_NAME" | "CLAIM_COMPANY" | "AMOUNT" | "DATE" | "SEMANTIC" | "ATTACHMENTS" | "AMOUNT_VALID";
export interface RulePolicy {
  enabled: boolean;
  on_failure: RuleAction;
}
/** 稳定 ID 用于模型结果匹配、人工确认和历史留痕，编辑时不可重建。 */
export interface CustomAuditRule extends RulePolicy {
  id: string;
  name: string;
  description: string;
}
export interface AuditRules {
  version: string;
  amount_tolerance: string;
  require_semantic: boolean;
  /** 1 或缺省沿用旧策略；仅新版规则表单保存时显式升级为 2。 */
  policy_schema_version?: 1 | 2 | 3;
  builtin_rules?: Partial<Record<BuiltinRuleCode, RulePolicy>>;
  custom_rules?: CustomAuditRule[];
  entries?: AuditRuleEntry[];
}
/** 显式规则列表中缺项即删除，稳定 ID 用于关联审核及人工确认记录。 */
export interface AuditRuleEntry extends CustomAuditRule {
  kind: "BUILTIN" | "CUSTOM";
  builtin_code: string;
  parameters: { comparison?: "AT_LEAST" | "EXACT"; require_completed?: boolean };
}
export interface ApprovalTemplate {
  /** 仅付款可开启；默认开启，优先于 AI 开关与 mode，原配置保留供关闭后恢复。 */
  payment_auto_approve_enabled: boolean;
  code: string;
  name: string;
  business_type: BusinessType;
  /** 同一模板可绑定多个飞书节点 ID 或 custom_node_id。 */
  node_ids: string[];
  /** 首个预审节点 ID，兼容旧版单节点配置。 */
  node_id: string;
  reviewer_open_id: string;
  field_mapping: FieldMapping;
  rules: AuditRules;
  /** 节点业务审核要求；空字符串继承全局默认，系统权限及输出约定始终追加。 */
  agent_prompt: string;
  /** 模板的 AI 审批开关；自动回写还需单独启用自动模式。 */
  ai_approval_enabled: boolean;
  mode: "OBSERVE" | "AUTO";
  active: boolean;
  subscribed: boolean;
  capabilities: Record<string, boolean>;
  sync_cursor?: string | null;
}
export interface ImportBatch {
  id: string;
  filename: string;
  status: string;
  columns: string[];
  mapping: Record<string, string>;
  preview: (InvoiceFields & { row: number; source_label: string })[];
  errors: { row: number; error: string }[];
  imported: number;
  row_count: number;
  valid_count: number;
}
export interface SystemInfo {
  mode: string;
  /** API 进程内持久化任务执行器，固定为 local。 */
  job_mode: "local";
  /** 当前数据库引擎，不返回数据库文件路径。 */
  database: "sqlite";
  storage: string;
  model_configured: boolean;
  model_name: string;
  /** 生效配置来源，以及固定执行语义审核的引擎。 */
  model_source: "environment" | "database";
  agent_engine: string;
  ocr_enabled: boolean;
  ocr_configured: boolean;
  /** 当前配置版本的腾讯云业务调用状态。 */
  ocr_state: "DISABLED" | "UNVERIFIED" | "READY" | "FAILED";
  ocr_last_error: string;
  ocr_last_elapsed_ms: number | null;
  feishu_configured: boolean;
  public_url: string;
}

/** 管理员可见的 OCR 配置摘要，凭据由服务端持有。 */
export interface OCRConfig {
  /** 保存及测试时回传，用于识别过期草稿。 */
  version: number;
  enabled: boolean;
  credentials_configured: boolean;
  credential_error: string;
}

/** 样票测试结果，只包含状态、耗时和脱敏的请求 ID。 */
export interface OCRConnectionResult {
  success: boolean;
  message: string;
  elapsed_ms: number;
  request_id: string;
}

/** 自动同步的保存契约；仅控制定时补偿，手动同步继续沿用管理员权限。 */
export interface SyncConfigurationInput {
  /** 读取时的配置版本，保存时原样回传以避免并发覆盖。 */
  version: number;
  /** 是否启用自动增量补偿同步；演示环境不执行真实同步。 */
  enabled: boolean;
  /** 自动同步间隔，单位秒，整数，范围 1 至 86400。 */
  interval_seconds: number;
}

/** 自动同步的当前生效配置；时间按 UTC 返回，由页面转换为北京时间。 */
export interface SyncConfiguration extends SyncConfigurationInput {
  /** 首次保存前使用部署环境，保存后使用工作台数据库。 */
  source: "environment" | "database";
  /** 预计下次可入队时间；关闭或演示环境时为空，队列繁忙时实际执行可能延后。 */
  next_run_at: string | null;
}

/** 管理员读取的脱敏模型配置，服务端不返回密钥明文或密文。 */
export interface ModelConfiguration {
  /** 付款模板独立默认业务提示词，避免误展示报销全局要求。 */
  payment_default_prompt: string;
  /** 保存时回传，避免不同管理员覆盖彼此的修改。 */
  version: number;
  connection_version: number;
  source: "environment" | "database";
  enabled: boolean;
  base_url: string;
  model_name: string;
  api_key_configured: boolean;
  credential_error: string;
  configured: boolean;
  default_prompt: string;
  builtin_prompt: string;
  system_constraints: string;
  engine: string;
}

/** 保存与连接检查共用的草稿：密钥留空保留，clear_api_key 显式清除并停用。 */
export interface ModelConfigurationInput {
  version: number;
  enabled: boolean;
  base_url: string;
  model_name: string;
  api_key: string;
  clear_api_key: boolean;
  default_prompt: string;
}

/** 连接检查只使用虚拟数据，不保存配置、不触发审批。 */
export interface ModelConnectionResult {
  success: boolean;
  message: string;
  latency_ms: number;
  /** 本次检查的调用日志 ID；旧服务可能不返回。 */
  call_id?: string | null;
}

/** AI 日志列表仅含元数据，大段请求及输出通过详情接口按需加载。 */
export interface AICallSummary {
  id: string;
  /** 审批语义审核、虚拟数据连接测试或原件多模态提取。 */
  source: "AUDIT" | "CONNECTION_TEST" | "INVOICE_EXTRACTION" | "PAYMENT_MATERIAL" | "AGENT_ATTACHMENT";
  approval_id: string | null;
  template_code: string;
  actor: string;
  engine: string;
  model_name: string;
  config_version: number;
  session_id: string;
  status: "RUNNING" | "COMPLETED" | "FAILED" | "SKIPPED";
  stage: string;
  /** 仅反映供应商明确返回的内容，不推断隐藏思考。 */
  has_reasoning: boolean;
  has_output: boolean;
  usage: Record<string, number>;
  finish_reason: string;
  error_type: string;
  error_message: string;
  truncated: boolean;
  started_at: string;
  finished_at: string | null;
  duration_ms: number;
}

/** 文本均由后端脱敏，供应商没有返回的思考或 Token 用量保持为空。 */
export interface AICallDetail extends AICallSummary {
  prompt: string;
  input_json: string;
  reasoning: string;
  raw_output: string;
  parsed_result: Record<string, unknown>;
  events: { type: string; label: string; time: string }[];
}
