import { Alert, Tag } from "antd";
import { budgetMoney, budgetPeriodLabels, budgetStateLabels, budgetStatusLabels, type BudgetContext } from "../budgetTypes";
import { BudgetUsageActions } from "./BudgetUsageActions";

/** 审批中的本单已计入汇总，在卡片中直接显示可用余额，避免重复扣减。 */
export function BudgetContextCard({ context }: { context?: BudgetContext | null }) {
  if (!context) return null;
  const { usage, budgets, message } = context;
  const warning = usage?.attribution_pending || budgets.some((item) => item.budget_status !== "NORMAL");
  return <section className="approval-budget-context" aria-label="报销预算">
    <div className="budget-context-heading"><h4>部门预算</h4>{usage && <span>{budgetStateLabels[usage.state]} ¥{budgetMoney(usage.amount)}</span>}</div>
    <Alert showIcon type={warning ? "warning" : "info"} message={message} />
    {budgets.map((item) => <div className="budget-context-row" key={item.id}>
      <div><a href={`#/budgets/${item.id}`}>{item.name}</a><small>{item.department} · {item.expense_type || "全部费用"} · {budgetPeriodLabels[item.period_type]}</small></div>
      <div><Tag color={item.budget_status === "NORMAL" ? "success" : item.budget_status === "EXCEEDED" ? "error" : "warning"}>{budgetStatusLabels[item.budget_status]}</Tag>
        <strong className={item.available_amount.startsWith("-") ? "budget-negative" : ""}>可用 ¥{budgetMoney(item.available_amount)}</strong></div>
    </div>)}
    {usage?.historical_review && <p className="budget-integrity-note">冻结金额待财务核对；历史缺少通过快照或归属来源已有变化。</p>}
    {usage && <BudgetUsageActions usage={usage} />}
  </section>;
}
