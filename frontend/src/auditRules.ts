import type { AuditRuleEntry, AuditRules, BuiltinRuleCode, BusinessType, Evidence, RuleAction } from "./types";

/** 与服务端稳定编码对应；文案、默认值和界面选择集中维护。 */
export const builtinStandards: { code: BuiltinRuleCode; name: string; description: string; action: RuleAction }[] = [
  { code: "DUPLICATE", name: "跨审批重复占票", description: "全企业查重；同票不同格式附件只计一次。重复占票始终拦截通过。", action: "REJECT" },
  { code: "COMPANY", name: "购买方主体匹配", description: "票面购买方税号须匹配已启用的公司主体。", action: "REJECT" },
  { code: "BUYER_NAME", name: "购买方名称核对", description: "税号匹配后，核对购买方名称是否为法定名称或已配置别名。", action: "REJECT" },
  { code: "CLAIM_COMPANY", name: "报销主体与票据一致性", description: "按申报主体名称或税号核对票面购买方；主体缺值、无法唯一匹配或名称与税号冲突时转人工。", action: "REJECT" },
  { code: "AMOUNT", name: "发票金额足额核对", description: "去重后的价税合计加容差应不小于报销金额，发票金额更大可通过。", action: "REJECT" },
  { code: "DATE", name: "开票日期检查", description: "开票日期不得晚于当前日期。", action: "REVIEW" },
  { code: "SEMANTIC", name: "费用语义核对", description: "模型检查事由、费用类别与票据用途的具体矛盾，独立于文字标准判定。", action: "REVIEW" },
  { code: "ATTACHMENTS", name: "发票原件完整性", description: "应提供本次申请的发票原件。", action: "REVIEW" },
  { code: "AMOUNT_VALID", name: "申请金额有效性", description: "申请金额应大于零。", action: "REVIEW" },
];
export const ruleActionOptions = [
  { label: "拒绝", value: "REJECT" },
  { label: "人工复核", value: "REVIEW" },
  { label: "仅提示", value: "WARN" },
];

export function defaultEntries(businessType: BusinessType = "REIMBURSEMENT"): AuditRuleEntry[] {
  if (businessType === "PAYMENT") return [
    ["payment_materials", "材料完备", "应有可完整读取的合同、报价单或其他能够说明付款依据的材料。没有发票本身不违规；没有附件或关键依据缺失返回 UNKNOWN。"],
    ["payment_company", "付款主体一致", "表单付款主体应与材料中的付款方或采购方一致；无法确认返回 UNKNOWN。"],
    ["payment_account", "收款信息一致", "表单收款人及账户应与材料中的收款约定一致，材料未提供或代收依据不足返回 UNKNOWN。"],
    ["payment_amount", "付款金额有依据", "申请金额应大于零并有付款约定支持。允许有明确依据的预付款或部分付款，不要求发票足额。"],
    ["payment_purpose", "用途与类别合理", "付款事由、类别及材料应基本一致，不存在明确用途矛盾；不因事由简短提高风险。"],
  ].map(([id, name, description]) => ({ id, name, description, kind: "CUSTOM", builtin_code: "", enabled: true, on_failure: "REVIEW", parameters: {} }));
  return builtinStandards.filter(item => item.code !== "DUPLICATE").map(item => ({
    id: `builtin_${item.code.toLowerCase()}`, kind: "BUILTIN", builtin_code: item.code, name: item.name,
    description: item.description, enabled: true, on_failure: item.action, parameters: item.code === "SEMANTIC" ? { require_completed: true } : {},
  }));
}

/** 旧配置只转换编辑草稿；版本 3 的显式空列表不得填回默认条件。 */
export function editableRules(rules?: AuditRules, businessType: BusinessType = "REIMBURSEMENT"): AuditRules {
  let entries = defaultEntries(businessType);
  if (rules?.policy_schema_version === 3) entries = rules.entries?.map(item => ({ ...item, parameters: { ...item.parameters } })) || [];
  else if (rules) {
    entries = entries.map(item => ({ ...item, ...(rules.builtin_rules?.[item.builtin_code as BuiltinRuleCode] || {}),
      on_failure: rules.policy_schema_version === 2 ? rules.builtin_rules?.[item.builtin_code as BuiltinRuleCode]?.on_failure || item.on_failure : "REVIEW",
      parameters: item.builtin_code === "SEMANTIC" ? { require_completed: rules.require_semantic }
        : item.builtin_code === "AMOUNT" && rules.policy_schema_version !== 2 ? { comparison: "EXACT" } : {},
    }));
    entries.push(...(rules.custom_rules || []).map(item => ({ ...item, kind: "CUSTOM" as const, builtin_code: "", parameters: {} })));
  }
  return {
    version: "1.0", amount_tolerance: "0.01", require_semantic: false, ...rules,
    policy_schema_version: 3, entries, builtin_rules: {}, custom_rules: [],
  };
}

export function canReviewEvidence(item: Evidence): boolean {
  return !item.passed && item.severity === "REVIEW" && (item.reviewable ?? item.code === "SEMANTIC");
}
