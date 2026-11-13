/** 周期按北京时间的自然月、自然季、自然年或自定义日期解释。 */
export type BudgetPeriod = "MONTH" | "QUARTER" | "YEAR" | "CUSTOM";
/** 预警只提供提示，继续允许原人工与自动预审。 */
export type BudgetStatus = "NORMAL" | "WARNING" | "EXHAUSTED" | "EXCEEDED";
/** 每单只有一条用量事实，已释放记录不计入金额指标。 */
export type BudgetUsageState = "RESERVED" | "USED" | "FROZEN" | "RELEASED";

/** 预算金额均为元的十进制字符串，前端直接展示后端计算结果。 */
export interface Budget {
  /** 预算稳定标识及用于维护的独立版本。 */
  id: string;
  version: number;
  name: string;
  company_id: string;
  company_name: string;
  /** 表单部门名称精确匹配，空费用类别表示全部类别。 */
  department: string;
  expense_type: string;
  period_type: BudgetPeriod;
  start_date: string;
  end_date: string;
  amount: string;
  warning_threshold: number;
  notes: string;
  archived: boolean;
  used_amount: string;
  reserved_amount: string;
  frozen_amount: string;
  occupied_amount: string;
  /** 允许负值；超过预算时 exceeded_amount 为其绝对值。 */
  available_amount: string;
  exceeded_amount: string;
  usage_percentage: string;
  budget_status: BudgetStatus;
  usage_count: number;
  created_at: string;
  updated_at: string;
}

/** 主体未知的待归属记录也计入提示；历史待建账数为全局数量。 */
export interface BudgetCompleteness {
  pending_count: number;
  pending_amount: string;
  backfill_remaining: number;
  historical_review_count: number;
}

/** 不同预算可能重叠，列表只汇总条目数，不合计额度或使用金额。 */
export interface BudgetList {
  items: Budget[];
  total: number;
  page: number;
  page_size: number;
  warning_count: number;
  exceeded_count: number;
  completeness: BudgetCompleteness;
}

/** 每单预算事实；version 用于确认归属、释放冻结，与审批审核版本独立。 */
export interface BudgetUsage {
  approval_id: string;
  serial_number: string;
  title: string;
  applicant_id: string;
  applicant: string;
  company_id: string | null;
  company_name: string;
  department: string;
  expense_type: string;
  submitted_at: string | null;
  amount: string;
  state: BudgetUsageState;
  attribution_pending: boolean;
  attribution_reason: string;
  attribution_source: "AUTO" | "MANUAL" | "REVIEW";
  historical_review: boolean;
  version: number;
}

/** 人员使用身份作为 key，同名人员的贡献不合并。 */
export interface BudgetGroup { key: string; name: string; amount: string; count: number }

/** 金额分布按当前用量的提交时间分组，不代表当时的历史余额。 */
export interface BudgetDetail extends Budget {
  completeness: BudgetCompleteness;
  applicants: BudgetGroup[];
  expense_types: BudgetGroup[];
  trend: { date: string; amount: string }[];
}

/** 预算维护与整单归属、冻结释放记录；快照中的金额为文本。 */
export interface BudgetOperation {
  id: string;
  budget_id: string | null;
  approval_id: string | null;
  action: "CREATE" | "EDIT" | "ADJUST" | "ATTRIBUTION" | "RELEASE";
  actor_name: string;
  reason: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  created_at: string;
}

/** 审批详情附带的实时指标，本单已计入预算汇总，不能再次从余额扣减。 */
export interface BudgetContext { usage: BudgetUsage | null; budgets: Budget[]; message: string }

/** 可搜索并可输入新部门、类别，不依赖额外飞书通讯录权限。 */
export interface BudgetOptions { departments: string[]; expense_types: string[] }

/** 不经浮点数格式化金额，保留大额十进制文本中的分。 */
export function budgetMoney(value?: string) {
  const [integer, fraction = "00"] = (value || "0.00").split(".");
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + fraction.padEnd(2, "0");
}

export const budgetStatusLabels: Record<BudgetStatus, string> = { NORMAL: "正常", WARNING: "接近预算", EXHAUSTED: "额度用尽", EXCEEDED: "超预算" };
export const budgetStateLabels: Record<BudgetUsageState, string> = { RESERVED: "在途占用", USED: "已使用", FROZEN: "冻结占用", RELEASED: "已释放" };
export const budgetPeriodLabels: Record<BudgetPeriod, string> = { MONTH: "月度", QUARTER: "季度", YEAR: "年度", CUSTOM: "自定义" };
