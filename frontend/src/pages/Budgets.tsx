import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Alert, App as AntApp, Button, DatePicker, Descriptions, Drawer, Dropdown, Empty, Form, Input, InputNumber, Modal, Progress, Segmented, Select, Space, Spin, Table, Tabs, Tag, Tooltip } from "antd";
import type { TableColumnsType } from "antd";
import { CopyOutlined, DownloadOutlined, EditOutlined, MoreOutlined, PlusOutlined, ReloadOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import type { Dayjs } from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import quarterOfYear from "dayjs/plugin/quarterOfYear";
import { api, localTime, post, query, useResource } from "../api";
import { useAgentPageContext, useWorkbench } from "../context";
import { requestId } from "../requestId";
import type { Page } from "../types";
import { BudgetUsageActions } from "../components/BudgetUsageActions";
import { budgetMoney, budgetPeriodLabels, budgetStateLabels, budgetStatusLabels, type Budget, type BudgetCompleteness, type BudgetDetail, type BudgetGroup, type BudgetList, type BudgetOperation, type BudgetOptions, type BudgetPeriod, type BudgetStatus, type BudgetUsage, type BudgetUsageState } from "../budgetTypes";

dayjs.extend(utc); dayjs.extend(timezone); dayjs.extend(quarterOfYear);
const endpoint = "/api/v1/budgets";
const ApprovalDrawer = lazy(() => import("./Approvals").then((module) => ({ default: module.ApprovalDrawer })));
const color = (status: BudgetStatus) => status === "NORMAL" ? "success" : status === "EXCEEDED" ? "error" : "warning";
const amountRules = [{ required: true, message: "请填写预算额度" }, { pattern: /^\d{1,16}(\.\d{1,2})?$/, message: "请输入最多16位整数、两位小数的金额" },
  { validator: (_: unknown, value: string) => !value || Number(value) > 0 ? Promise.resolve() : Promise.reject(new Error("预算额度必须大于零")) }];

function RequestError({ error, retry }: { error: string; retry: () => void }) {
  return <Alert showIcon type="error" message={error} action={<Button size="small" onClick={retry}>重试</Button>} />;
}

function CompletenessNote({ value }: { value?: BudgetCompleteness }) {
  if (!value || !(value.pending_count || value.backfill_remaining || value.historical_review_count)) return null;
  return <Alert showIcon type="warning" className="budget-completeness" message="预算数据仍有待核对项" description={<>
    {!!value.pending_count && <div>当前主体范围及主体未知记录中有 {value.pending_count} 单、¥{budgetMoney(value.pending_amount)} 的归属或提交时间待确认，尚未计入明确预算，余额可能偏高。</div>}
    {!!value.backfill_remaining && <div>全局还有 {value.backfill_remaining} 单历史报销正在分批建账；失败任务可在后台任务中重试。</div>}
    {!!value.historical_review_count && <div>有 {value.historical_review_count} 单冻结金额缺少历史通过快照或来源已有变化，请财务核对。</div>}
  </>} />;
}

function BudgetProgress({ budget }: { budget: Budget }) {
  return <Tooltip title={`总占用 ¥${budgetMoney(budget.occupied_amount)}；预警阈值 ${budget.warning_threshold}%`}>
    <div className="budget-progress"><Progress size="small" percent={Math.min(100, Number(budget.usage_percentage))}
      status={budget.budget_status === "EXCEEDED" ? "exception" : "normal"} showInfo={false} />
      <span>{budget.usage_percentage}%</span><Tag color={color(budget.budget_status)}>{budgetStatusLabels[budget.budget_status]}</Tag></div>
  </Tooltip>;
}

/** 自然周期统一从选择的日期推导，前端和后端都校验完整自然月、季、年。 */
function periodBounds(kind: BudgetPeriod, anchor: Dayjs): [Dayjs, Dayjs] {
  const start = kind === "YEAR" ? anchor.startOf("year") : kind === "QUARTER" ? anchor.month(Math.floor(anchor.month() / 3) * 3).startOf("month") : anchor.startOf("month");
  return [start, kind === "YEAR" ? start.endOf("year") : kind === "QUARTER" ? start.add(2, "month").endOf("month") : start.endOf("month")];
}

/** 预算维护表单；部门和类别用单项标签数组承接可输入选择器，提交时转换为单一名称。 */
interface BudgetFormValues {
  name: string; company_id: string; departments: string[]; categories: string[]; period_type: BudgetPeriod;
  anchor: Dayjs; range: [Dayjs, Dayjs]; amount: string; warning_threshold: number; notes: string;
}

function BudgetEditor({ budget, mode, onClose }: { budget?: Budget; mode: "create" | "copy" | "edit"; onClose: () => void }) {
  const { companyId, companies, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<BudgetFormValues>();
  const kind = Form.useWatch("period_type", form) as BudgetPeriod | undefined;
  const selectedCompany = Form.useWatch("company_id", form) as string | undefined;
  const options = useResource<BudgetOptions>(query(endpoint + "/options", { company_id: selectedCompany || companyId }));
  const [busy, setBusy] = useState(false);
  const anchor = mode === "copy" && budget ? dayjs(budget.end_date).add(1, "day") : dayjs().tz("Asia/Shanghai");
  const editing = mode === "edit";
  const submit = async () => {
    const values = await form.validateFields();
    setBusy(true);
    try {
      if (editing && budget) {
        await api(endpoint + "/" + budget.id, { method: "PATCH", body: JSON.stringify({ expected_version: budget.version,
          name: values.name, warning_threshold: values.warning_threshold, notes: values.notes || "" }) });
      } else {
        const range = values.period_type === "CUSTOM" ? values.range : periodBounds(values.period_type, values.anchor);
        await post(endpoint, { name: values.name, company_id: values.company_id, department: values.departments[0],
          expense_type: values.categories?.[0] || "", period_type: values.period_type, start_date: range[0].format("YYYY-MM-DD"),
          end_date: range[1].format("YYYY-MM-DD"), amount: values.amount, warning_threshold: values.warning_threshold, notes: values.notes || "" });
      }
      message.success(editing ? "预算已更新" : "预算已创建，周期内已有报销已纳入统计");
      refresh(); onClose();
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <Modal open width={660} title={{ create: "新建部门预算", copy: "复制预算到新周期", edit: "维护预算" }[mode]}
    onCancel={() => { if (!busy) onClose(); }} onOk={() => void submit()} confirmLoading={busy} cancelButtonProps={{ disabled: busy }} destroyOnHidden>
    {editing && budget && <Alert type="info" showIcon message={`${budget.company_name} · ${budget.department} · ${budget.expense_type || "全部费用"} · ${budget.start_date} 至 ${budget.end_date}`} />}
    {mode === "copy" && <Alert type="info" showIcon message="复制范围和额度到新周期，不复制使用金额或调整记录；新周期内已有报销会自动计入。" />}
    <Form form={form} layout="vertical" disabled={busy} style={{ marginTop: 18 }} initialValues={{
      name: budget ? (mode === "copy" ? budget.name + "（复制）" : budget.name) : "", company_id: budget?.company_id || companyId || companies.find((item) => item.active)?.id,
      departments: budget ? [budget.department] : [], categories: budget?.expense_type ? [budget.expense_type] : [],
      period_type: budget?.period_type || "MONTH", anchor,
      range: [anchor, budget ? anchor.add(dayjs(budget.end_date).diff(dayjs(budget.start_date), "day"), "day") : anchor.endOf("month")],
      amount: budget?.amount || "", warning_threshold: budget?.warning_threshold || 80, notes: budget?.notes || "",
    }}>
      <Form.Item name="name" label="预算名称" rules={[{ required: true, whitespace: true, max: 150, message: "请填写150字以内的预算名称" }]}><Input maxLength={150} /></Form.Item>
      {!editing && <>
        <Form.Item name="company_id" label="费用承担主体" rules={[{ required: true, message: "请选择公司主体" }]}>
          <Select showSearch optionFilterProp="label" options={companies.filter((item) => item.active).map((item) => ({ value: item.id, label: item.name }))} />
        </Form.Item>
        <div className="budget-form-grid">
          <Form.Item name="departments" label="申请部门" rules={[{ required: true, type: "array", len: 1, message: "请选择或输入一个部门" }]}>
            <Select mode="tags" maxCount={1} placeholder="选择或输入表单部门名称" options={options.data?.departments.map((value) => ({ value, label: value }))} />
          </Form.Item>
          <Form.Item name="categories" label="费用类别" extra="留空表示该部门全部费用">
            <Select mode="tags" maxCount={1} allowClear placeholder="全部费用" options={options.data?.expense_types.map((value) => ({ value, label: value }))} />
          </Form.Item>
        </div>
        {options.error && <Alert type="warning" message="选项加载失败，仍可输入已核对的部门和类别名称" />}
        <div className="budget-form-grid">
          <Form.Item name="period_type" label="预算周期" rules={[{ required: true }]}><Select options={Object.entries(budgetPeriodLabels).map(([value, label]) => ({ value, label }))} /></Form.Item>
          {kind === "CUSTOM" ? <Form.Item name="range" label="起止日期" rules={[{ required: true, message: "请选择完整日期范围" }]}>
            <DatePicker.RangePicker allowClear={false} style={{ width: "100%" }} />
          </Form.Item> : <Form.Item name="anchor" label="所属期间" rules={[{ required: true, message: "请选择期间" }]}>
            <DatePicker allowClear={false} picker={kind === "YEAR" ? "year" : kind === "QUARTER" ? "quarter" : "month"} style={{ width: "100%" }} />
          </Form.Item>}
        </div>
        <Form.Item name="amount" label="预算额度" rules={amountRules}><Input inputMode="decimal" suffix="元" maxLength={19} /></Form.Item>
      </>}
      <Form.Item name="warning_threshold" label="占用预警阈值" rules={[{ required: true }]}><InputNumber min={1} max={100} precision={0} suffix="%" /></Form.Item>
      <Form.Item name="notes" label="备注"><Input.TextArea rows={2} maxLength={2000} /></Form.Item>
    </Form>
  </Modal>;
}

function BudgetAdjustmentModal({ budget, onClose }: { budget: Budget; onClose: () => void }) {
  const { refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const [busy, setBusy] = useState(false);
  const [key, setKey] = useState(requestId);
  const submit = async () => {
    const values = await form.validateFields();
    setBusy(true);
    try {
      await post(`${endpoint}/${budget.id}/adjustments`, { ...values, expected_version: budget.version, request_id: key });
      message.success("预算额度已调整，操作依据已留存"); refresh(); onClose();
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <Modal open title="调整预算额度" onCancel={() => { if (!busy) onClose(); }} onOk={() => void submit()}
    confirmLoading={busy} cancelButtonProps={{ disabled: busy }} destroyOnHidden>
    <p>{budget.name} · 当前额度 <strong>¥{budgetMoney(budget.amount)}</strong></p>
    <Form form={form} layout="vertical" disabled={busy} initialValues={{ amount: budget.amount }} onValuesChange={() => setKey(requestId())}>
      <Form.Item name="amount" label="调整后的总额度" rules={amountRules}><Input inputMode="decimal" suffix="元" maxLength={19} /></Form.Item>
      <Form.Item name="reason" label="调整原因" rules={[{ required: true, whitespace: true, min: 2, max: 600, message: "请填写2至600字的调整原因" }]}>
        <Input.TextArea rows={3} maxLength={600} />
      </Form.Item>
    </Form>
  </Modal>;
}

/** 明细、待归属和冻结列表共享分页及操作；切换筛选立即隐藏旧数据。 */
function UsageTable({ budgetId, companyId, pendingOnly = false, state = "", applicantId = "", expenseType = "" }: {
  budgetId?: string; companyId?: string; pendingOnly?: boolean; state?: BudgetUsageState | ""; applicantId?: string; expenseType?: string;
}) {
  const { tick } = useWorkbench();
  const [page, setPage] = useState(1);
  const [approvalId, setApprovalId] = useState("");
  useEffect(() => { setPage(1); }, [budgetId, companyId, pendingOnly, state, applicantId, expenseType]);
  const path = query(budgetId ? `${endpoint}/${budgetId}/details` : endpoint + "/usages", {
    company_id: companyId, pending_only: pendingOnly ? "true" : undefined, state, applicant_id: applicantId, expense_type: expenseType, page, page_size: 20,
  });
  const resource = useResource<Page<BudgetUsage>>(path, tick);
  const columns: TableColumnsType<BudgetUsage> = [
    { title: "报销申请", key: "approval", width: 230, render: (_, row) => <div><Button type="link" className="budget-link" onClick={() => setApprovalId(row.approval_id)}>{row.serial_number}</Button><small className="budget-cell-secondary">{row.title}</small></div> },
    { title: "报销人 / 部门", key: "applicant", width: 155, render: (_, row) => <div>{row.applicant}<small className="budget-cell-secondary">{row.department || "部门待确认"}</small></div> },
    { title: "主体 / 类别", key: "scope", width: 180, render: (_, row) => <div>{row.company_name || "主体待确认"}<small className="budget-cell-secondary">{row.expense_type}</small></div> },
    { title: "金额（元）", dataIndex: "amount", width: 130, align: "right", render: (value: string) => budgetMoney(value) },
    { title: "占用状态", key: "state", width: 150, render: (_, row) => <><Tag color={row.state === "FROZEN" ? "warning" : undefined}>{budgetStateLabels[row.state]}</Tag>
      {row.historical_review && <Tag color="warning">金额待核对</Tag>}{row.attribution_pending && <Tooltip title={row.attribution_reason}><Tag color="warning">信息待确认</Tag></Tooltip>}</> },
    { title: "提交时间", dataIndex: "submitted_at", width: 145, render: (value: string | null) => value ? localTime(value, false, true) : "时间待补齐" },
    { title: "预算操作", key: "actions", width: 180, render: (_, row) => <BudgetUsageActions usage={row} onChanged={() => void resource.reload()} /> },
  ];
  return <>
    {resource.error && <RequestError error={resource.error} retry={() => void resource.reload()} />}
    <Table<BudgetUsage> rowKey="approval_id" size="small" columns={columns} dataSource={resource.data?.items || []}
      loading={resource.loading} scroll={{ x: 1150 }} pagination={{ current: page, total: resource.data?.total || 0, pageSize: 20, showSizeChanger: false, onChange: setPage, showTotal: (total) => `共 ${total} 单` }} />
    {approvalId && <Suspense fallback={<Spin />}><ApprovalDrawer id={approvalId} onClose={() => setApprovalId("")} /></Suspense>}
  </>;
}

function HistoryTable({ budgetId }: { budgetId: string }) {
  const { tick, companies } = useWorkbench();
  const [page, setPage] = useState(1);
  const resource = useResource<Page<BudgetOperation>>(query(`${endpoint}/${budgetId}/history`, { page, page_size: 20 }), tick);
  const actionNames = { CREATE: "创建预算", EDIT: "维护预算", ADJUST: "调整额度", ATTRIBUTION: "确认归属", RELEASE: "释放冻结" };
  const change = (row: BudgetOperation) => {
    if (row.action === "CREATE") return `额度 ¥${budgetMoney(String(row.after.amount))}`;
    if (row.action === "ADJUST") return `¥${budgetMoney(String(row.before.amount))} → ¥${budgetMoney(String(row.after.amount))}`;
    if (row.action === "RELEASE") return `整单释放 ¥${budgetMoney(String(row.before.amount))}`;
    if (row.action === "ATTRIBUTION") {
      const name = companies.find((item) => item.id === row.after.company_id)?.name || "已确认主体";
      return `${name} · ${String(row.after.department || "")}`;
    }
    return `${String(row.after.name || "")} · 预警 ${String(row.after.warning_threshold)}% · ${row.after.archived ? "已归档" : "未归档"}`;
  };
  return <>
    {resource.error && <RequestError error={resource.error} retry={() => void resource.reload()} />}
    <Table<BudgetOperation> rowKey="id" size="small" loading={resource.loading} dataSource={resource.data?.items || []} scroll={{ x: 800 }} columns={[
      { title: "操作", dataIndex: "action", width: 105, render: (value: BudgetOperation["action"]) => actionNames[value] },
      { title: "变更内容", key: "change", width: 290, render: (_, row) => change(row) },
      { title: "操作人", dataIndex: "actor_name", width: 100 }, { title: "操作依据", dataIndex: "reason", render: (value) => value || "—" },
      { title: "操作时间", dataIndex: "created_at", width: 155, render: (value) => localTime(value, false, true) },
    ]} pagination={{ current: page, total: resource.data?.total || 0, pageSize: 20, showSizeChanger: false, onChange: setPage }} />
  </>;
}

function BudgetDetailDrawer({ id, onClose, onEdit, onAdjust, onCopy }: {
  id: string; onClose: () => void; onEdit: (row: Budget) => void; onAdjust: (row: Budget) => void; onCopy: (row: Budget) => void;
}) {
  const { tick, user } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<BudgetDetail>(`${endpoint}/${id}`, tick);
  const row = resource.data;
  const maximum = row?.trend.reduce((value, point) => Math.max(value, Number(point.amount)), 1) || 1;
  const [tab, setTab] = useState("details");
  const [drill, setDrill] = useState<{ state?: BudgetUsageState; applicantId?: string; expenseType?: string }>({});
  const [downloading, setDownloading] = useState(false);
  const exportReport = async () => {
    setDownloading(true);
    try {
      const response = await fetch(`${endpoint}/${id}/export`, { credentials: "same-origin" });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
        const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : "预算导出失败");
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = `${row?.name || "预算"}.xlsx`; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      message.success("预算报表已导出");
    } catch (reason) { message.error((reason as Error).message); }
    finally { setDownloading(false); }
  };
  const groups = (items: BudgetGroup[], kind: "applicantId" | "expenseType") => <Table<BudgetGroup> rowKey="key" size="small" dataSource={items}
    pagination={{ pageSize: 5, showSizeChanger: false }} columns={[
      { title: kind === "applicantId" ? "报销人" : "费用类别", key: "name", render: (_, item) => <Tooltip title={item.key}><span>{item.name}</span></Tooltip> },
      { title: "占用金额（元）", dataIndex: "amount", align: "right", render: (value: string, item) => <Button type="link" onClick={() => { setDrill({ [kind]: item.key }); setTab("details"); }}>{budgetMoney(value)}</Button> },
      { title: "单数", dataIndex: "count", width: 60, align: "right" },
    ]} />;
  return <Drawer open width={1120} title={row?.name || "预算详情"} onClose={onClose} destroyOnHidden extra={<Space wrap>
    {user.role !== "viewer" && row && <><Button size="small" onClick={() => onEdit(row)}>维护</Button><Button size="small" onClick={() => onAdjust(row)}>调额</Button><Button size="small" onClick={() => onCopy(row)}>复制</Button></>}
    <Button size="small" icon={<DownloadOutlined />} loading={downloading} disabled={!row} onClick={() => void exportReport()}>导出 Excel</Button>
  </Space>}>
    {resource.loading && <Spin />}{resource.error && <RequestError error={resource.error} retry={() => void resource.reload()} />}
    {row && !resource.error && <>
      <Descriptions size="small" column={2} items={[
        { key: "company", label: "主体 / 部门", children: `${row.company_name} · ${row.department}` },
        { key: "category", label: "费用类别", children: row.expense_type || "全部费用" },
        { key: "period", label: "预算周期", children: `${budgetPeriodLabels[row.period_type]} · ${row.start_date} 至 ${row.end_date}` },
        { key: "threshold", label: "预警阈值", children: `${row.warning_threshold}%${row.archived ? " · 已归档" : ""}` },
        { key: "notes", label: "备注", children: row.notes || "—", span: 2 },
      ]} />
      <div className="budget-amount-cards">{[
        { label: "预算额度", value: row.amount, state: undefined }, { label: "已使用", value: row.used_amount, state: "USED" as const },
        { label: "在途占用", value: row.reserved_amount, state: "RESERVED" as const }, { label: "冻结占用", value: row.frozen_amount, state: "FROZEN" as const },
        { label: "可用余额", value: row.available_amount, state: undefined },
      ].map((item) => <div className="budget-amount-card" key={item.label}><span>{item.label}</span>
        {item.state ? <button className="budget-metric-button" onClick={() => { setDrill({ state: item.state }); setTab("details"); }}>¥{budgetMoney(item.value)}</button>
          : <strong className={item.value.startsWith("-") ? "budget-negative" : ""}>¥{budgetMoney(item.value)}</strong>}</div>)}</div>
      <BudgetProgress budget={row} />
      {row.budget_status === "EXCEEDED" && <Alert type="warning" showIcon message={`当前超出预算 ¥${budgetMoney(row.exceeded_amount)}；预算仅提醒，审批流程继续按原规则处理。`} />}
      <CompletenessNote value={row.completeness} />
      <p className="budget-integrity-note">按申请提交时间归入周期，每张报销计入一次整单金额。金额表示审批用量，付款与入账结果以财务核对为准。</p>
      <Tabs activeKey={tab} onChange={setTab} items={[
        { key: "details", label: "报销明细", children: <>
          <Space wrap className="budget-detail-filters"><Select<BudgetUsageState | ""> aria-label="预算明细占用状态" style={{ width: 155 }} value={drill.state || ""}
            onChange={(state) => setDrill((value) => ({ ...value, state: state || undefined }))} options={[{ value: "", label: "全部占用状态" }, ...Object.entries(budgetStateLabels).map(([value, label]) => ({ value, label }))]} />
            {(drill.applicantId || drill.expenseType) && <Tag closable onClose={() => setDrill((value) => ({ state: value.state }))}>{drill.expenseType || row.applicants.find((item) => item.key === drill.applicantId)?.name || drill.applicantId}</Tag>}
            <Button size="small" onClick={() => setDrill({})}>查看全部明细</Button>
          </Space><UsageTable budgetId={id} {...drill} /></> },
        { key: "distribution", label: "使用分析", children: <>
          <div className="budget-breakdown-grid"><section><h4>报销人占用贡献</h4>{groups(row.applicants, "applicantId")}</section><section><h4>费用类别占用贡献</h4>{groups(row.expense_types, "expenseType")}</section></div>
          <section className="budget-trend"><h4>当前占用按提交日期分布</h4>{row.usage_count ? <>
            <div className="budget-trend-bars" role="img" aria-label="报销占用按提交日期分布">{row.trend.map((point) => <Tooltip key={point.date} title={`${point.date} · ¥${budgetMoney(point.amount)}`}>
              <div><i style={{ height: `${Number(point.amount) / maximum * 100}%` }} /></div>
            </Tooltip>)}</div><div className="budget-trend-axis"><span>{row.trend[0]?.date}</span><span>{row.trend[row.trend.length - 1]?.date}</span></div>
          </> : <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前预算暂无占用" />}</section>
        </> },
        { key: "history", label: "操作记录", children: tab === "history" ? <HistoryTable budgetId={id} /> : null },
      ]} />
    </>}
  </Drawer>;
}

export function Budgets({ budgetId }: { budgetId?: string }) {
  const { companyId, tick, user, refresh, navigate } = useWorkbench();
  const { message, modal } = AntApp.useApp();
  const [mode, setMode] = useState("budgets");
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState({ department: "", expense_type: "", period_type: "", period_scope: "current", budget_status: "", archived: "active", q: "", start_date: "", end_date: "" });
  const [editor, setEditor] = useState<{ mode: "create" | "copy" | "edit"; budget?: Budget }>();
  const [adjustment, setAdjustment] = useState<Budget>();
  const previousCompany = useRef(companyId);
  const options = useResource<BudgetOptions>(query(endpoint + "/options", { company_id: companyId }), tick);
  const resource = useResource<BudgetList>(query(endpoint, { company_id: companyId, ...filters, page, page_size: 20 }), tick);
  useEffect(() => { setPage(1); }, [companyId, filters]);
  useEffect(() => {
    if (previousCompany.current !== companyId) {
      previousCompany.current = companyId;
      setFilters((value) => ({ ...value, department: "", expense_type: "" }));
      if (budgetId) navigate("budgets");
    }
  }, [companyId, budgetId, navigate]);
  useAgentPageContext({ selection_type: "budgets", selected_ids: budgetId ? [budgetId] : [], filters: {
    department: filters.department, expense_type: filters.expense_type, period_type: filters.period_type, period_scope: filters.period_scope,
    budget_status: filters.budget_status, archived: filters.archived, q: filters.q, start_date: filters.start_date, end_date: filters.end_date,
  } });
  const updateFilter = (key: keyof typeof filters, value: string) => setFilters((current) => ({ ...current, [key]: value }));
  const archive = (row: Budget) => modal.confirm({ title: row.archived ? "恢复预算" : "归档预算", content: `${row.name}：历史用量仍随权威审批状态更新。`,
    onOk: async () => {
      try {
        await api(`${endpoint}/${row.id}`, { method: "PATCH", body: JSON.stringify({ expected_version: row.version, archived: !row.archived }) });
        message.success(row.archived ? "预算已恢复" : "预算已归档"); refresh();
      } catch (reason) { message.error((reason as Error).message); throw reason; }
    },
  });
  const columns: TableColumnsType<Budget> = [
    { title: "预算 / 周期", key: "name", width: 250, fixed: "left", render: (_, row) => <div><Button type="link" className="budget-link" onClick={() => navigate(`budgets/${row.id}`)}>{row.name}</Button>
      <small className="budget-cell-secondary">{budgetPeriodLabels[row.period_type]} · {row.start_date} 至 {row.end_date}</small>
      <Tag color={color(row.budget_status)}>{budgetStatusLabels[row.budget_status]}</Tag>{row.archived && <Tag>已归档</Tag>}</div> },
    { title: "主体 / 部门 / 类别", key: "scope", width: 220, render: (_, row) => <div>{row.department}<small className="budget-cell-secondary">{row.company_name}</small><Tag>{row.expense_type || "全部费用"}</Tag></div> },
    ...([ ["预算额度", "amount"], ["已使用", "used_amount"], ["在途占用", "reserved_amount"], ["冻结占用", "frozen_amount"], ["可用余额", "available_amount"] ] as const).map(([title, dataIndex]) => ({
      title: `${title}（元）`, dataIndex, key: dataIndex, width: 135, align: "right" as const,
      render: (value: string) => <span className={value.startsWith("-") ? "budget-negative" : ""}>{budgetMoney(value)}</span>,
    })),
    { title: "占用进度", key: "progress", width: 225, render: (_, row) => <BudgetProgress budget={row} /> },
    { title: "操作", key: "actions", width: 90, fixed: "right", render: (_, row) => user.role === "viewer" ? <Button type="link" onClick={() => navigate(`budgets/${row.id}`)}>查看</Button> : <Dropdown menu={{ items: [
      { key: "view", label: "查看详情" }, { key: "edit", label: "维护预算", icon: <EditOutlined /> }, { key: "adjust", label: "调整额度" },
      { key: "copy", label: "复制预算", icon: <CopyOutlined /> }, { key: "archive", label: row.archived ? "恢复预算" : "归档预算" },
    ], onClick: ({ key }) => {
      if (key === "view") navigate(`budgets/${row.id}`);
      else if (key === "adjust") setAdjustment(row);
      else if (key === "archive") archive(row);
      else setEditor({ mode: key as "copy" | "edit", budget: row });
    } }}><Button type="text" aria-label={`管理${row.name}`} icon={<MoreOutlined />} /></Dropdown> },
  ];
  return <div className="budget-page">
    <Space className="budget-actions" wrap>
      <Button icon={<ReloadOutlined />} onClick={refresh}>刷新</Button>
      {user.role !== "viewer" && (
        <Button type="primary" icon={<PlusOutlined />} onClick={() => setEditor({ mode: "create" })}>新建预算</Button>
      )}
    </Space>
    <div className="stats-grid budget-summary-cards">{[
      ["预算条目", resource.data?.total || 0], ["接近或用尽额度", resource.data?.warning_count || 0], ["已超预算", resource.data?.exceeded_count || 0], ["待确认报销", resource.data?.completeness.pending_count || 0],
    ].map(([label, value]) => <div className="stat-card" key={label}><div className="stat-label">{label}</div><div className="stat-value">{value}<small>{label === "待确认报销" ? "单" : "项"}</small></div></div>)}</div>
    <CompletenessNote value={resource.data?.completeness} />
    <Segmented className="budget-view-switch" value={mode} onChange={setMode} options={[{ value: "budgets", label: "预算台账" }, { value: "pending", label: "待归属 / 待补齐" }, { value: "frozen", label: "冻结待核对" }]} />
    {mode === "budgets" ? <>
      <section className="surface budget-filters"><div className="budget-filter-grid">
        <label><span>预算名称</span><Input.Search allowClear placeholder="搜索预算" onSearch={(value) => updateFilter("q", value)} /></label>
        <label><span>申请部门</span><Select aria-label="预算部门" allowClear showSearch placeholder="全部部门" value={filters.department || undefined} options={options.data?.departments.map((value) => ({ value, label: value }))} onChange={(value) => updateFilter("department", value || "")} /></label>
        <label><span>费用类别</span><Select aria-label="预算费用类别" allowClear showSearch placeholder="全部预算类别" value={filters.expense_type || undefined} options={options.data?.expense_types.map((value) => ({ value, label: value }))} onChange={(value) => updateFilter("expense_type", value || "")} /></label>
        <label><span>周期类型</span><Select aria-label="预算周期类型" value={filters.period_type} options={[{ value: "", label: "全部周期类型" }, ...Object.entries(budgetPeriodLabels).map(([value, label]) => ({ value, label }))]} onChange={(value) => updateFilter("period_type", value)} /></label>
        <label><span>周期范围</span><Select aria-label="预算周期范围" value={filters.period_scope} options={[{ value: "current", label: "覆盖今天" }, { value: "all", label: "全部历史与未来" }]} onChange={(value) => updateFilter("period_scope", value)} /></label>
        <label><span>预警状态</span><Select aria-label="预算预警状态" value={filters.budget_status} options={[{ value: "", label: "全部状态" }, ...Object.entries(budgetStatusLabels).map(([value, label]) => ({ value, label }))]} onChange={(value) => updateFilter("budget_status", value)} /></label>
        <label><span>归档状态</span><Select aria-label="预算归档状态" value={filters.archived} options={[{ value: "active", label: "未归档" }, { value: "archived", label: "已归档" }, { value: "all", label: "全部" }]} onChange={(value) => updateFilter("archived", value)} /></label>
        <label><span>相交日期范围</span><DatePicker.RangePicker aria-label="预算相交日期范围" onChange={(range) => setFilters((value) => ({ ...value, period_scope: "all", start_date: range?.[0]?.format("YYYY-MM-DD") || "", end_date: range?.[1]?.format("YYYY-MM-DD") || "" }))} /></label>
      </div>{options.error && <RequestError error={options.error} retry={() => void options.reload()} />}</section>
      <p className="budget-integrity-note">每项预算独立核算，月度、年度及专项预算可能覆盖同一笔报销，金额不跨预算叠加。</p>
      {resource.error && <RequestError error={resource.error} retry={() => void resource.reload()} />}
      <section className="surface table-surface"><Table<Budget> rowKey="id" columns={columns} dataSource={resource.data?.items || []} loading={resource.loading} scroll={{ x: 1460 }}
        pagination={{ current: page, total: resource.data?.total || 0, pageSize: 20, showSizeChanger: false, onChange: setPage, showTotal: (total) => `共 ${total} 项预算` }} /></section>
    </> : <section className="surface table-surface"><UsageTable key={mode + companyId} companyId={companyId} pendingOnly={mode === "pending"} state={mode === "frozen" ? "FROZEN" : ""} /></section>}
    {budgetId && <BudgetDetailDrawer key={budgetId} id={budgetId} onClose={() => navigate("budgets")} onEdit={(budget) => setEditor({ mode: "edit", budget })}
      onAdjust={setAdjustment} onCopy={(budget) => setEditor({ mode: "copy", budget })} />}
    {editor && <BudgetEditor {...editor} onClose={() => setEditor(undefined)} />}
    {adjustment && <BudgetAdjustmentModal budget={adjustment} onClose={() => setAdjustment(undefined)} />}
  </div>;
}
