import { post } from "./api";
import type { ApprovalTemplate, AuditRules, BusinessType, FieldMapping } from "./types";

/** 定义描述只含控件元数据；path 是外层明细名称，不包含申请值或默认值。 */
export interface DefinitionField {
  id: string;
  custom_id: string;
  name: string;
  type: string;
  path: string[];
}
/** 定义节点供管理员核对；飞书可能把发起和结束也返回为普通类型。 */
export interface DefinitionNode {
  id: string;
  custom_id: string;
  name: string;
  type: string;
}
export interface ApprovalDefinition {
  code: string;
  name: string;
  status: string;
  fields: DefinitionField[];
  nodes: DefinitionNode[];
  /** 提交时服务端重新读取定义并比对该摘要。 */
  fingerprint: string;
  demo: boolean;
}
/** 可移植配置保留源规则，不携带源审批人、启用开关、订阅或联调状态。 */
export interface PortableTemplate {
  source_code: string;
  name: string;
  business_type: BusinessType;
  field_mapping: FieldMapping;
  rules: AuditRules;
  agent_prompt: string;
  prompt_source: "INHERIT" | "OVERRIDE";
  fields: DefinitionField[];
  /** 版本 2 保留全部源节点，版本 1 只有单个 node。 */
  nodes?: DefinitionNode[];
  node?: DefinitionNode | null;
  definition_warning: string;
}
export interface TemplateBundle {
  /** 与规则结构版本独立，导出为 2，支持导入 1 和 2。 */
  schema_version: 1 | 2;
  exported_at: string;
  templates: PortableTemplate[];
}
export interface TemplateInspection {
  definition: ApprovalDefinition;
  field_mapping: FieldMapping;
  /** 已保存或当前识别出的预审节点集合。 */
  node_ids: string[];
  node_id: string;
  unresolved_fields: string[];
  business_type: BusinessType;
  reviewer_open_id: string;
  reviewer_name: string;
  reviewer_source: "SAVED" | "PENDING_TASK" | "NONE";
  /** 同编码模板已接入时保留原配置及规则。 */
  existing_template: ApprovalTemplate | null;
  notices: string[];
}
/** 目标绑定全部来自当前公司；cleared_fields 记录人工明确清除的可选映射。 */
export interface TemplateBinding {
  source_code: string;
  selected: boolean;
  target_reference: string;
  name: string;
  /** 当前目标定义中已选的全部真实节点 ID。 */
  node_ids: string[] | null;
  node_id: string;
  node_confirmed: boolean;
  reviewer_open_id: string;
  field_mapping: FieldMapping;
  cleared_fields: string[];
  attachment_scope_confirmed: boolean;
  conflict_action: "SKIP" | "OVERWRITE";
  definition_fingerprint: string;
  /** 空串表示预览时目标不存在，null 表示尚未读取。 */
  expected_revision: string | null;
}
export interface TemplatePreviewItem {
  source_code: string;
  binding: TemplateBinding;
  definition: ApprovalDefinition | null;
  conflict: boolean;
  unresolved_fields: string[];
  warnings: string[];
  error: string;
}
export interface TemplatePreview {
  items: TemplatePreviewItem[];
}
export interface TemplateImportResult {
  templates: ApprovalTemplate[];
  skipped: string[];
}
export type ScalarMappingKey = Exclude<keyof FieldMapping, "attachment_ids">;

/** 展示名称和现有字段契约集中维护，编辑页与迁移向导使用同一份列表。 */
export const scalarMappingFields: { key: ScalarMappingKey; label: string; paymentOnly?: boolean }[] = [
  { key: "amount", label: "申请金额" },
  { key: "company_tax_id", label: "主体税号" },
  { key: "expense_type", label: "费用类型" },
  { key: "description", label: "申请事由" },
  { key: "company", label: "主体名称" },
  { key: "department", label: "部门", paymentOnly: true },
  { key: "project", label: "所属项目", paymentOnly: true },
  { key: "payment_method", label: "付款方式", paymentOnly: true },
  { key: "expected_payment_date", label: "期望付款日期", paymentOnly: true },
  { key: "payee_account", label: "收款账户", paymentOnly: true },
  { key: "related_approvals", label: "关联审批", paymentOnly: true },
  { key: "remarks", label: "备注", paymentOnly: true },
];

export const emptyFieldMapping = (): FieldMapping => ({
  amount: "", company_tax_id: "", expense_type: "", description: "", attachment_ids: [],
  company: "", department: "", project: "", payment_method: "", expected_payment_date: "",
  payee_account: "", related_approvals: "", remarks: "",
});

export function fieldLabel(field: DefinitionField): string {
  return (field.path.length ? field.path.join(" / ") + " / " : "") + (field.name || "未命名控件")
    + " · " + field.type + " · " + field.id;
}

/** 默认选择器展示业务名称，实际提交的值仍为真实控件 ID。 */
export function fieldName(field: DefinitionField): string {
  return (field.path.length ? field.path.join(" / ") + " / " : "") + (field.name || "未命名控件");
}

/** v1 预览继续提供单节点值，管理员可在新版页面调整为多节点。 */
export function bindingNodeIds(binding: TemplateBinding): string[] {
  return binding.node_ids ?? (binding.node_id ? [binding.node_id] : []);
}

/** 下载由服务端白名单生成的配置；空配置或下载失败不会生成伪文件。 */
export async function downloadTemplates(codes: string[]): Promise<void> {
  const bundle = await post<TemplateBundle>("/api/v1/templates/export", { codes });
  const url = URL.createObjectURL(new Blob([JSON.stringify(bundle, null, 2) + "\n"], { type: "application/json;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = "审批模板配置-" + new Date().toISOString().slice(0, 10) + ".json";
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** 页面仅用于提前提示，所有约束仍在服务端提交时重新校验。 */
export function bindingIssues(source: PortableTemplate, item: TemplatePreviewItem): string[] {
  const binding = item.binding;
  if (!binding.selected || (item.conflict && binding.conflict_action === "SKIP")) return [];
  const issues: string[] = [];
  if (!item.definition || !binding.definition_fingerprint) issues.push("读取目标模板");
  if (!binding.name.trim()) issues.push("填写模板名称");
  if (!bindingNodeIds(binding).length || !binding.node_confirmed) issues.push("确认专用预审节点");
  if (!binding.reviewer_open_id.trim()) issues.push("选择或填写目标审批人");
  for (const { key, label } of scalarMappingFields) {
    if ((key === "amount" || source.field_mapping[key]) && !binding.field_mapping[key]
      && (key === "amount" || !binding.cleared_fields.includes(key))) issues.push("绑定或处理" + label);
  }
  if (source.field_mapping.attachment_ids.length && !binding.field_mapping.attachment_ids.length
    && !binding.cleared_fields.includes("attachment_ids")) issues.push("确认附件读取范围");
  if (binding.field_mapping.attachment_ids.length > 0
    && binding.field_mapping.attachment_ids.length < source.field_mapping.attachment_ids.length
    && !binding.attachment_scope_confirmed) issues.push("确认减少后的附件范围");
  return issues;
}
