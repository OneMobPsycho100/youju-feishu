/** 付款统计接口按北京时间日期筛选，金额以两位小数的元字符串传输。 */
export interface PaymentDateRange {
  start_date: string;
  end_date: string;
}

export interface PaymentStatisticsFilters extends PaymentDateRange {
  company_id: string;
  /** 项目选项返回的完整分组键，留空包含全部项目。 */
  project_keys: string[];
}

export type PaymentGroupBy = "project" | "department" | "payment_method";

/** 累计只取截止日，待补齐范围使用主体和项目条件。 */
export type PaymentDetailScope = "period" | "cumulative" | "missing_approved_at";

export interface PaymentProjectOption {
  key: string;
  name: string;
  company_name: string;
}

export interface PaymentStatisticsOptions {
  projects: PaymentProjectOption[];
}

export interface PaymentStatisticsGroup {
  /** 下钻时原样传回，空字符串可表示未填写部门或付款方式。 */
  key: string;
  name: string;
  company_name: string | null;
  amount: string;
  count: number;
  percentage: string;
}

export interface PaymentStatisticsTrend {
  /** 北京时间的自然日 YYYY-MM-DD 或月份 YYYY-MM。 */
  date: string;
  amount: string;
  count: number;
}

export interface PaymentStatisticsReport {
  scope: PaymentStatisticsFilters;
  timezone: "Asia/Shanghai";
  totals: {
    period_amount: string;
    /** 相同主体及项目截至结束日的累计，包含所选期间之前的已同步记录。 */
    cumulative_amount: string;
    count: number;
    project_count: number;
  };
  /** 各维度的区间汇总同时返回，切换表格分组时保留当前统计结果。 */
  groups: Record<PaymentGroupBy, PaymentStatisticsGroup[]>;
  /** 区间内按金额降序排列的前十个项目分组，包含未填写项目，跨主体分别排名。 */
  project_ranking: PaymentStatisticsGroup[];
  granularity: "day" | "month";
  trend: PaymentStatisticsTrend[];
  missing_approved_count: number;
}

export interface PaymentStatisticsDetail {
  id: string;
  serial_number: string;
  title: string;
  applicant: string;
  department: string;
  project: string;
  company_name: string;
  payment_method: string;
  amount: string;
  /** 飞书最终通过时间，UTC，待补齐时为空。 */
  approved_at: string | null;
}
