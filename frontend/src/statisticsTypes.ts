/** 报销统计的公共筛选；空状态表示全部，不能在构造 URL 时省略。 */
export interface StatisticsFilters {
  /** 北京时间起止日期，包含起止日期全天，格式 YYYY-MM-DD。 */
  startDate: string;
  endDate: string;
  /** 使用接口选项返回的身份值，允许多选，空数组表示全部。 */
  applicantIds: string[];
  expenseTypes: string[];
  /** 最终审批状态，APPROVED 为默认值；空字符串显式表示全部。 */
  status: string;
}

/** 稳定身份用于筛选；显示名可能重复，旧记录的 value 带 legacy: 前缀。 */
export interface StatisticsApplicant {
  value: string;
  label: string;
  identity_pending: boolean;
}

export interface StatisticsOptions {
  /** 当前公司范围内的全部人员选项，不随日期或审批状态收窄。 */
  applicants: StatisticsApplicant[];
  /** 当前公司范围内的全部类别。 */
  expense_types: string[];
}

/** 分类或人员分组；金额和百分数沿用后端十进制字符串，前端不重新合计。 */
export interface StatisticsGroup {
  /** 下钻时原样传回，不能用显示名称代替。 */
  key: string;
  name: string;
  /** 元，后端精确累计的两位十进制文本。 */
  amount: string;
  count: number;
  /** 金额占筛选总额的百分数，0 到 100；总额为零时返回 0.00。 */
  percentage: string;
  identity_pending: boolean;
}

/** 汇总结果始终回显有效范围；图表数值转换仅用于绘制尺寸。 */
export interface ReimbursementStatistics {
  scope: {
    company_id: string;
    start_date: string;
    end_date: string;
    applicant_ids: string[];
    expense_types: string[];
    status: string;
  };
  timezone: "Asia/Shanghai";
  group_by: "expense_type" | "applicant";
  totals: { amount: string; count: number; applicant_count: number; expense_type_count: number };
  groups: StatisticsGroup[];
  granularity: "day" | "month";
  trend: { date: string; amount: string; count: number }[];
  missing_submitted_count: number;
  pending_identity_count: number;
  demo_time_simulated: boolean;
}

/** 下钻明细为轻量审批投影；submitted_at 为 UTC，详情通过 id 复用现有抽屉。 */
export interface StatisticsDetail {
  id: string;
  serial_number: string;
  title: string;
  applicant_id: string;
  applicant: string;
  identity_pending: boolean;
  department: string;
  expense_type: string;
  amount: string;
  submitted_at: string;
  status: string;
}
