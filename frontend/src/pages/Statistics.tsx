import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Alert, App as AntApp, Button, DatePicker, Drawer, Empty, Segmented, Select, Space, Spin, Table, Tag, Tooltip } from "antd";
import { BarChartOutlined, DownloadOutlined, FileTextOutlined, ReloadOutlined, TagsOutlined, TeamOutlined, WalletOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import type { Dayjs } from "dayjs";
import { formatDecimalAmount as amount, query, useResource } from "../api";
import { useAgentPageContext, useWorkbench } from "../context";
import { ApplicantCell, HelpTip, SectionTitle, StatusTag } from "../ui";
import { StatisticsPieChart } from "../components/StatisticsPieChart";
import type { Page } from "../types";
import type { ReimbursementStatistics, StatisticsDetail, StatisticsFilters, StatisticsGroup, StatisticsOptions } from "../statisticsTypes";

dayjs.extend(utc); dayjs.extend(timezone);
const ApprovalDrawer = lazy(() => import("./Approvals").then((module) => ({ default: module.ApprovalDrawer })));
const endpoint = "/api/v1/reimbursement-statistics";
type GroupBy = ReimbursementStatistics["group_by"];
const dimensionNames: Record<GroupBy, string> = { department: "部门", applicant: "员工", expense_type: "费用类别" };
const stateNames: Record<string, string> = { USED: "已使用", RESERVED: "在途占用", FROZEN: "冻结占用" };
const stateSeries = [{ key: "used_amount", label: "已使用", color: "#4f83ef" }, { key: "reserved_amount", label: "在途占用", color: "#e9ad48" }, { key: "frozen_amount", label: "冻结占用", color: "#9a7ad4" }] as const;

/** 统计窗口与自然日统一使用北京时间，保留原来的本月、已通过默认条件。 */
function today() { return dayjs().tz("Asia/Shanghai"); }
function defaults(): StatisticsFilters {
  return { startDate: today().startOf("month").format("YYYY-MM-DD"), endDate: today().format("YYYY-MM-DD"),
    applicantIds: [], expenseTypes: [], status: "APPROVED", measure: "application", department: "", usageState: "", budgetCoverage: "all" };
}

/** 显式发送空状态，申请状态与消耗状态分别传递，避免口径切换沿用另一种状态。 */
function statisticsQuery(path: string, companyId: string, filters: StatisticsFilters, extra: Record<string, string | number> = {}) {
  const params = new URLSearchParams({ company_id: companyId, start_date: filters.startDate, end_date: filters.endDate,
    measure: filters.measure, department: filters.department, status: filters.measure === "application" ? filters.status : "",
    usage_state: filters.measure === "consumption" ? filters.usageState : "", budget_coverage: filters.measure === "consumption" ? filters.budgetCoverage : "all" });
  filters.applicantIds.forEach((value) => params.append("applicant_ids", value));
  filters.expenseTypes.forEach((value) => params.append("expense_types", value));
  Object.entries(extra).forEach(([key, value]) => params.set(key, String(value)));
  return path + "?" + params.toString();
}

function RequestError({ message, retry }: { message: string; retry: () => void }) {
  return <Alert showIcon type="error" message={message} action={<Button size="small" onClick={retry} icon={<ReloadOutlined />}>重试</Button>} />;
}

/** 下钻冻结打开时的范围；图表类别、日期、未匹配预算都使用同一明细接口。 */
interface DetailSelection {
  title: string;
  groupBy?: GroupBy;
  groupKey?: string;
  filters?: Partial<StatisticsFilters>;
}

function TrendChart({ data, consumption, onSelect }: { data: ReimbursementStatistics; consumption: boolean; onSelect: (date: string) => void }) {
  const maximum = data.trend.reduce((value, point) => Math.max(value, Number(point.amount)), 1);
  return <section className="surface statistics-trend statistics-full-trend">
    <SectionTitle title={consumption ? "报销消耗趋势" : "报销金额趋势"} caption={`${data.granularity === "day" ? "按日" : "按月"}汇总`}
      help="按申请提交日期展示当前金额分布。点击日期查看明细；预算消耗曲线不表示当时的历史余额。" />
    {consumption && <div className="statistics-state-legend">{stateSeries.map((item) => <span key={item.key}><i style={{ background: item.color }} />{item.label}</span>)}</div>}
    {!data.totals.count ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前条件下暂无可统计单据" /> : <>
      <div className="statistics-chart-scale"><span>金额（元）</span><span>最高 ¥{amount(data.trend.reduce((a, b) => Number(a.amount) > Number(b.amount) ? a : b).amount)}</span></div>
      <div className="statistics-trend-bars" role="group" aria-label={consumption ? "已使用、在途和冻结消耗堆叠柱形图" : "报销金额趋势图"}>{data.trend.map((point) =>
        <Tooltip key={point.date} title={<><strong>{point.date}</strong><br />¥{amount(point.amount)} · {point.count} 单
          {consumption && stateSeries.map((item) => <div key={item.key}>{item.label} ¥{amount(point[item.key])}</div>)}<div>点击查看明细</div></>}>
          <button className="statistics-time-column" aria-label={`${point.date}，¥${amount(point.amount)}，${point.count}单，查看明细`} onClick={() => onSelect(point.date)}>
            <span className="statistics-time-stack" style={{ height: `${Number(point.amount) / maximum * 100}%` }}>
              {consumption ? stateSeries.map((item) => <i key={item.key} style={{ height: `${Number(point[item.key]) / Math.max(Number(point.amount), 0.01) * 100}%`, background: item.color }} />)
                : <i style={{ height: "100%", background: "#4f83ef" }} />}
            </span>
          </button>
        </Tooltip>)}</div>
      <div className="statistics-trend-axis"><span>{data.trend[0]?.date}</span><span>{data.trend[Math.floor(data.trend.length / 2)]?.date}</span><span>{data.trend[data.trend.length - 1]?.date}</span></div>
    </>}
  </section>;
}

export function Statistics() {
  const { message } = AntApp.useApp();
  const { companyId, tick } = useWorkbench();
  const [filters, setFilters] = useState<StatisticsFilters>(defaults);
  const [rankBy, setRankBy] = useState<"department" | "applicant">("department");
  const [tableBy, setTableBy] = useState<GroupBy>("department");
  const [groupPage, setGroupPage] = useState(1);
  const [selection, setSelection] = useState<DetailSelection | null>(null);
  const [downloading, setDownloading] = useState(false);
  const previousCompany = useRef(companyId);
  const validatedOptionsScope = useRef("");
  const consumption = filters.measure === "consumption";
  useAgentPageContext({ selection_type: "", selected_ids: [], filters: {} });
  const options = useResource<StatisticsOptions>(query(endpoint + "/options", { company_id: companyId, measure: filters.measure, department: filters.department }), tick);
  const resource = useResource<ReimbursementStatistics>(statisticsQuery(endpoint, companyId, filters, { group_by: "department" }), tick);
  const data = resource.data;
  useEffect(() => { setSelection(null); setGroupPage(1); }, [companyId, filters, tableBy]);
  useEffect(() => {
    if (previousCompany.current !== companyId) {
      previousCompany.current = companyId;
      setFilters((value) => ({ ...value, department: "", applicantIds: [], expenseTypes: [] }));
    }
  }, [companyId]);
  useEffect(() => {
    if (!options.data) return;
    const scope = JSON.stringify([companyId, filters.measure, filters.department]);
    if (validatedOptionsScope.current === scope) return;
    validatedOptionsScope.current = scope;
    const available = options.data;
    // 仅在范围切换后清理不兼容选项；定时刷新不能因费用释放而把个人或部门筛选扩大为全部。
    setFilters((value) => {
      const department = !value.department || available.departments.includes(value.department) ? value.department : "";
      const people = new Set(available.applicants.map((item) => item.value));
      const applicantIds = value.applicantIds.filter((id) => people.has(id));
      const expenseTypes = value.expenseTypes.filter((type) => available.expense_types.includes(type));
      return department === value.department && applicantIds.length === value.applicantIds.length && expenseTypes.length === value.expenseTypes.length
        ? value : { ...value, department, applicantIds, expenseTypes };
    });
  }, [options.data, companyId, filters.measure, filters.department]);
  const applicantOptions = useMemo(() => {
    const items = options.data?.applicants || [];
    const counts = new Map<string, number>();
    items.forEach((item) => counts.set(item.label, (counts.get(item.label) || 0) + 1));
    return items.map((item) => ({ value: item.value, label: item.label + ((counts.get(item.label) || 0) > 1 ? `（${item.value}）` : "") + (item.identity_pending ? " · 身份待补齐" : "") }));
  }, [options.data]);
  const groupsFor = (by: GroupBy): StatisticsGroup[] => !data ? [] : by === "department" ? data.department_groups : by === "applicant" ? data.applicant_groups : data.expense_type_groups;
  const ranking = groupsFor(rankBy);
  const tableGroups = groupsFor(tableBy);
  const rankMaximum = Math.max(1, Number(ranking[0]?.amount || 0));
  useEffect(() => { setGroupPage((page) => Math.max(1, Math.min(page, Math.ceil(tableGroups.length / 20)))); }, [tableGroups.length]);
  const setRange = (range: [Dayjs, Dayjs]) => setFilters((value) => ({ ...value, startDate: range[0].format("YYYY-MM-DD"), endDate: range[1].format("YYYY-MM-DD") }));
  const selectDepartment = (department: string) => {
    setFilters((value) => ({ ...value, department, applicantIds: [] })); setRankBy("applicant"); setTableBy("applicant");
  };
  const viewGroup = (group: StatisticsGroup, groupBy: GroupBy) => setSelection({ title: `${dimensionNames[groupBy]} · ${group.name}`, groupBy, groupKey: group.key });
  const viewDate = (key: string) => {
    const start = key.length === 7 ? key + "-01" : key;
    const end = key.length === 7 ? dayjs(start).endOf("month").format("YYYY-MM-DD") : key;
    setSelection({ title: `${key} · ${consumption ? "预算消耗" : "报销金额"}`, filters: { startDate: start < filters.startDate ? filters.startDate : start, endDate: end > filters.endDate ? filters.endDate : end } });
  };
  const exportReport = async () => {
    setDownloading(true);
    try {
      const response = await fetch(statisticsQuery(endpoint + "/export", companyId, filters, { group_by: tableBy }), { credentials: "same-origin" });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
        const body = await response.json().catch(() => ({})); throw new Error(typeof body.detail === "string" ? body.detail : `导出失败（${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob()); const anchor = document.createElement("a");
      anchor.href = url; anchor.download = `${consumption ? "预算消耗" : "报销金额"}-${filters.startDate}-${filters.endDate}.xlsx`; anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000); message.success("当前统计报表已导出");
    } catch (reason) { message.error((reason as Error).message); }
    finally { setDownloading(false); }
  };
  const cards = data ? consumption ? [
    { name: "预算消耗总额", value: amount(data.totals.amount), unit: "元", icon: <WalletOutlined />, color: "purple" },
    { name: "已使用", value: amount(data.totals.used_amount), unit: "元", icon: <FileTextOutlined />, color: "blue" },
    { name: "在途占用", value: amount(data.totals.reserved_amount), unit: "元", icon: <TeamOutlined />, color: "amber" },
    { name: "冻结占用", value: amount(data.totals.frozen_amount), unit: "元", icon: <TagsOutlined />, color: "purple" },
  ] : [
    { name: "报销申请总额", value: amount(data.totals.amount), unit: "元", icon: <WalletOutlined />, color: "purple" },
    { name: "报销申请单数", value: data.totals.count.toLocaleString("zh-CN"), unit: "单", icon: <FileTextOutlined />, color: "blue" },
    { name: "报销人数", value: data.totals.applicant_count.toLocaleString("zh-CN"), unit: "人", icon: <TeamOutlined />, color: "teal" },
    { name: "报销类别", value: data.totals.expense_type_count.toLocaleString("zh-CN"), unit: "类", icon: <TagsOutlined />, color: "amber" },
  ] : [];
  return <div className="reimbursement-statistics">
    <section className="surface statistics-filters">
      <div className="statistics-filter-heading"><Space wrap><strong>统计范围</strong><Segmented<StatisticsFilters["measure"]> aria-label="统计口径" value={filters.measure}
        options={[{ label: "报销金额", value: "application" }, { label: "预算消耗", value: "consumption" }]}
        onChange={(measure) => setFilters((value) => ({ ...value, measure, status: measure === "application" ? "APPROVED" : "", usageState: "", budgetCoverage: "all" }))} />
        <HelpTip label="统计口径说明">报销金额按原审批申请及状态统计；预算消耗按每单唯一用量统计已使用、在途和冻结，释放后不计入。两种口径均按申请提交时间，使用北京时间，金额不代表付款或入账结果。</HelpTip></Space>
        <Space wrap><Button icon={<ReloadOutlined />} onClick={() => { setFilters(defaults()); setRankBy("department"); setTableBy("department"); }}>重置筛选</Button>
          <Button type="primary" icon={<DownloadOutlined />} loading={downloading} disabled={!data || Boolean(resource.error)} onClick={() => void exportReport()}>导出 Excel</Button></Space></div>
      <div className="statistics-filter-grid statistics-analysis-filters">
        <label><span>提交时间（北京时间）</span><DatePicker.RangePicker aria-label="提交时间段" allowClear={false} value={[dayjs(filters.startDate), dayjs(filters.endDate)]}
          onChange={(range) => { if (range?.[0] && range[1]) setRange([range[0], range[1]]); }} presets={[
            { label: "本月", value: () => [today().startOf("month"), today()] }, { label: "上月", value: () => [today().subtract(1, "month").startOf("month"), today().subtract(1, "month").endOf("month")] },
            { label: "近30天", value: () => [today().subtract(29, "day"), today()] }, { label: "今年", value: () => [today().startOf("year"), today()] },
          ]} /></label>
        <label><span>申请部门</span><Select aria-label="申请部门" showSearch allowClear optionFilterProp="label" placeholder="全部部门" loading={options.loading}
          value={filters.department || undefined} options={options.data?.departments.map((value) => ({ value, label: value }))}
          onChange={(value) => { if (value) selectDepartment(value); else { setFilters((current) => ({ ...current, department: "", applicantIds: [] })); setRankBy("department"); } }} /></label>
        <label><span>报销员工</span><Select aria-label="报销员工" mode="multiple" allowClear showSearch optionFilterProp="label" maxTagCount="responsive" placeholder="全部员工"
          loading={options.loading} value={filters.applicantIds} options={applicantOptions} onChange={(value) => setFilters((current) => ({ ...current, applicantIds: value }))} /></label>
        <label><span>费用类别</span><Select aria-label="费用类别" mode="multiple" allowClear showSearch optionFilterProp="label" maxTagCount="responsive" placeholder="全部类别"
          loading={options.loading} value={filters.expenseTypes} options={options.data?.expense_types.map((value) => ({ value, label: value }))}
          onChange={(value) => setFilters((current) => ({ ...current, expenseTypes: value }))} /></label>
        <label><span>{consumption ? "消耗状态" : "审批状态"}</span>{consumption ? <Select<StatisticsFilters["usageState"]> aria-label="消耗状态" value={filters.usageState}
          onChange={(value) => setFilters((current) => ({ ...current, usageState: value }))} options={[{ value: "", label: "全部占用" }, ...Object.entries(stateNames).map(([value, label]) => ({ value, label }))]} />
          : <Select aria-label="审批状态" value={filters.status} onChange={(value) => setFilters((current) => ({ ...current, status: value }))} options={[
            { value: "APPROVED", label: "已通过" }, { value: "", label: "全部状态" }, { value: "PENDING", label: "审批中" }, { value: "REJECTED", label: "已拒绝" },
            { value: "CANCELED", label: "已撤回" }, { value: "REVERTED", label: "已撤销" }, { value: "DELETED", label: "已删除" },
          ]} />}</label>
      </div>
      {options.error && <RequestError message={`筛选选项加载失败：${options.error}`} retry={() => void options.reload()} />}
    </section>
    {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
    {resource.loading && <div className="surface statistics-loading"><Spin /><span>正在汇总当前统计口径</span></div>}
    {data && !resource.error && <>
      {(data.missing_submitted_count > 0 || data.pending_identity_count > 0 || data.missing_attribution_count > 0 || data.backfill_remaining > 0) && <Alert showIcon type="warning" className="statistics-integrity" message="部分信息待补齐"
        description={<>{data.missing_submitted_count > 0 && <div>{data.missing_submitted_count} 单提交时间待补齐，尚未纳入日期统计。</div>}
          {data.missing_attribution_count > 0 && <div>当前主体范围及主体未知记录中有 {data.missing_attribution_count} 单、¥{amount(data.missing_attribution_amount)} 的归属或时间待确认，暂未计入消耗。</div>}
          {data.backfill_remaining > 0 && <div>全局还有 {data.backfill_remaining} 单预算用量正在后台建账。</div>}
          {data.pending_identity_count > 0 && <div>已统计单据有 {data.pending_identity_count} 单身份待补齐，暂按原申请人字段归组。</div>}</>} />}
      <div className="stats-grid statistics-kpis">{cards.map((item) => <div className="stat-card" key={item.name}><div className="stat-label">{item.name}<span className={`stat-icon ${item.color}`}>{item.icon}</span></div>
        <div className="stat-value" title={`${item.value} ${item.unit}`}>{item.value}<small>{item.unit}</small></div><div className="stat-detail"><span>{filters.startDate} 至 {filters.endDate}</span></div></div>)}</div>
      {consumption && <Alert showIcon className="statistics-coverage-note" type={data.totals.unmatched_count ? "warning" : "info"}
        message={<span>共 {data.totals.count} 单 · {data.totals.applicant_count} 位员工；未匹配预算 {data.totals.unmatched_count} 单、¥{amount(data.totals.unmatched_amount)}，已包含在总消耗中。</span>}
        action={data.totals.unmatched_count > 0 ? <Button size="small" onClick={() => setSelection({ title: "未匹配预算的报销", filters: { budgetCoverage: "unmatched" } })}>查看明细</Button> : undefined} />}
      {filters.applicantIds.length === 1 && <div className="statistics-person-focus"><TeamOutlined /> 正在查看 {applicantOptions.find((item) => item.value === filters.applicantIds[0])?.label || filters.applicantIds[0]} 的{consumption ? "预算消耗" : "报销金额"}
        <Button type="link" size="small" onClick={() => setFilters((value) => ({ ...value, applicantIds: [] }))}>返回全部员工</Button></div>}
      <div className={`statistics-distributions${filters.applicantIds.length === 1 ? " statistics-person-only" : ""}`}>
        {filters.applicantIds.length !== 1 && <section className="surface statistics-ranking"><SectionTitle title={`${dimensionNames[rankBy]}${consumption ? "消耗" : "报销金额"}排行`} caption="金额前10项，完整分组见下表"
          action={<Segmented<"department" | "applicant"> aria-label="排行维度" size="small" value={rankBy} options={[{ label: "按部门", value: "department" }, { label: "按员工", value: "applicant" }]} onChange={setRankBy} />} />
          {!ranking.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无排行数据" /> : <div className="statistics-rank-list">{ranking.slice(0, 10).map((group, index) => <button key={group.key} className="statistics-rank-row"
            title={`${group.name} · ${group.key}`} onClick={() => rankBy === "department" ? selectDepartment(group.key) : setFilters((value) => ({ ...value, applicantIds: [group.key] }))}>
            <Tooltip title={<>{group.name}<br />¥{amount(group.amount)} · {group.count} 单 · {group.percentage}%<br />点击查看{rankBy === "department" ? "部门员工" : "个人分析"}</>}>
              <span className="statistics-rank-inner"><span className="statistics-rank-index">{index + 1}</span><span className="statistics-rank-body"><span className="statistics-rank-heading"><strong>{group.name}</strong><span>¥{amount(group.amount)}</span></span>
                <span className="statistics-rank-track"><i style={{ width: `${Number(group.amount) / rankMaximum * 100}%` }} /></span></span><small>{group.percentage}%</small></span>
            </Tooltip></button>)}</div>}
        </section>}
        <section className="surface statistics-ranking statistics-distribution"><SectionTitle title="费用类别占比" caption={`¥${amount(data.totals.amount)} · ${data.expense_type_groups.length} 类`} help="按当前公司、部门、员工和日期统计，点击类别查看对应单据。" />
          <StatisticsPieChart donut groups={data.expense_type_groups} totalAmount={data.totals.amount} dimensionLabel="费用类别" formatAmount={amount} onSelect={(group) => viewGroup(group, "expense_type")} />
        </section>
      </div>
      <TrendChart data={data} consumption={consumption} onSelect={viewDate} />
      <section className="surface table-surface statistics-group-table"><SectionTitle title="分组汇总" caption={`共 ${tableGroups.length} 个分组`} action={<Space wrap>
        <Segmented<GroupBy> aria-label="汇总维度" size="small" value={tableBy} onChange={setTableBy} options={[{ value: "department", label: "部门" }, { value: "applicant", label: "员工" }, { value: "expense_type", label: "类别" }]} />
        <Button icon={<FileTextOutlined />} onClick={() => setSelection({ title: "全部报销明细" })}>全部明细</Button></Space>} />
        <Table<StatisticsGroup> rowKey="key" dataSource={tableGroups} scroll={{ x: 740 }} pagination={{ current: groupPage, pageSize: 20, showSizeChanger: false, onChange: setGroupPage, showTotal: (total) => `共 ${total} 个分组` }} columns={[
          { title: dimensionNames[tableBy], dataIndex: "name", render: (_, row) => <div className="statistics-group-name"><button className="table-title-link" onClick={() => viewGroup(row, tableBy)}><strong>{row.name}</strong></button>
            {tableBy === "applicant" && <small title={row.key}>{row.key}</small>}{row.identity_pending && <Tag color="orange">身份待补齐</Tag>}</div> },
          { title: `${consumption ? "消耗" : "报销金额"}（元）`, dataIndex: "amount", align: "right", render: (value: string) => <span className="amount-text">¥{amount(value)}</span> },
          { title: "申请单数", dataIndex: "count", align: "right" }, { title: "金额占比", dataIndex: "percentage", align: "right", render: (value: string) => `${value}%` },
          { title: "操作", width: 120, render: (_, row) => <Button type="link" onClick={() => viewGroup(row, tableBy)}>查看明细</Button> },
        ]} />
      </section>
      {data.demo_time_simulated && <div className="statistics-demo-note"><BarChartOutlined /> 演示提交时间为模拟数据</div>}
    </>}
    {selection && <StatisticsDetails key={JSON.stringify(selection)} selection={selection} companyId={companyId} filters={{ ...filters, ...selection.filters }} onClose={() => setSelection(null)} />}
  </div>;
}

function StatisticsDetails({ selection, companyId, filters, onClose }: { selection: DetailSelection; companyId: string; filters: StatisticsFilters; onClose: () => void }) {
  const { tick } = useWorkbench();
  const [page, setPage] = useState(1);
  const [approvalId, setApprovalId] = useState("");
  const consumption = filters.measure === "consumption";
  const resource = useResource<Page<StatisticsDetail>>(statisticsQuery(endpoint + "/details", companyId, filters,
    { page, ...(selection.groupBy ? { group_by: selection.groupBy, group_key: selection.groupKey || "" } : {}) }), tick);
  return <>
    <Drawer open width={1250} title={`审批明细 · ${selection.title}`} onClose={onClose} destroyOnHidden className="statistics-details">
      <p className="statistics-detail-caption">{consumption ? "预算消耗" : "报销金额"} · {filters.startDate} 至 {filters.endDate} · 北京时间 · 申请提交时间</p>
      {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
      <Table<StatisticsDetail> rowKey="id" dataSource={resource.data?.items} loading={resource.loading} scroll={{ x: consumption ? 1460 : 1280 }}
        pagination={{ current: page, pageSize: 20, total: resource.data?.total, showSizeChanger: false, onChange: setPage, showTotal: (total) => `共 ${total} 单` }} columns={[
          { title: "申请编号 / 事由", width: 260, render: (_, row) => <button className="table-title-link" onClick={() => setApprovalId(row.id)}><strong>{row.title}</strong><small>{row.serial_number}</small></button> },
          { title: "报销人 / 部门", width: 220, render: (_, row) => <div><ApplicantCell name={row.applicant} department={row.department} />{row.identity_pending && <Tag color="orange">身份待补齐</Tag>}</div> },
          { title: "报销类别", dataIndex: "expense_type", width: 140 },
          { title: consumption ? "消耗金额" : "报销金额", dataIndex: "amount", align: "right", width: 160, render: (value: string) => <span className="amount-text">¥{amount(value)}</span> },
          { title: "提交时间（北京时间）", dataIndex: "submitted_at", width: 190, render: (value: string) => new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value.endsWith("Z") ? value : value + "Z")) },
          { title: "审批状态", dataIndex: "status", width: 130, render: (value: string) => <StatusTag value={value} /> },
          ...(consumption ? [{ title: "消耗 / 预算覆盖", key: "budget", width: 175, render: (_: unknown, row: StatisticsDetail) => <div><Tag color={row.usage_state === "FROZEN" ? "warning" : "blue"}>{stateNames[row.usage_state]}</Tag><Tag color={row.budget_matched ? undefined : "warning"}>{row.budget_matched ? "已匹配预算" : "未匹配预算"}</Tag></div> }] : []),
          { title: "操作", width: 100, render: (_, row) => <Button type="link" onClick={() => setApprovalId(row.id)}>查看</Button> },
        ]} />
    </Drawer>
    {approvalId && <Suspense fallback={<Spin fullscreen />}><ApprovalDrawer id={approvalId} onClose={() => setApprovalId("")} /></Suspense>}
  </>;
}
