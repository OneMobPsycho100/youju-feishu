import { lazy, Suspense, useEffect, useState } from "react";
import { Alert, App as AntApp, Button, DatePicker, Drawer, Empty, Segmented, Select, Space, Spin, Table, Tooltip } from "antd";
import { AppstoreOutlined, DownloadOutlined, FileTextOutlined, FundOutlined, ReloadOutlined, WalletOutlined } from "@ant-design/icons";
import dayjs, { type Dayjs } from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import { formatDecimalAmount, localTime, query, useResource } from "../api";
import { useAgentPageContext, useWorkbench } from "../context";
import type {
  PaymentDateRange, PaymentDetailScope, PaymentGroupBy, PaymentStatisticsDetail,
  PaymentStatisticsFilters, PaymentStatisticsGroup, PaymentStatisticsOptions, PaymentStatisticsReport,
} from "../paymentStatisticsTypes";
import type { Page } from "../types";
import { ApplicantCell, HelpTip, SectionTitle } from "../ui";

dayjs.extend(utc);
dayjs.extend(timezone);

const ApprovalDrawer = lazy(() => import("./Approvals").then((module) => ({ default: module.ApprovalDrawer })));
const endpoint = "/api/v1/payment-statistics";
const groupLabels: Record<PaymentGroupBy, string> = { project: "项目", department: "申请部门", payment_method: "付款方式" };
const today = () => dayjs().tz("Asia/Shanghai");

function defaultRange(): PaymentDateRange {
  const end = today();
  return { start_date: end.startOf("month").format("YYYY-MM-DD"), end_date: end.format("YYYY-MM-DD") };
}

const presets: { label: string; value: () => [Dayjs, Dayjs] }[] = [
  { label: "本月", value: () => { const end = today(); return [end.startOf("month"), end]; } },
  { label: "上月", value: () => { const month = today().subtract(1, "month"); return [month.startOf("month"), month.endOf("month")]; } },
  { label: "近 30 天", value: () => { const end = today(); return [end.subtract(29, "day"), end]; } },
  { label: "今年", value: () => { const end = today(); return [end.startOf("year"), end]; } },
];

function statisticsPath(path: string, { project_keys, ...filters }: PaymentStatisticsFilters, extra: Record<string, string> = {}) {
  const params = new URLSearchParams({ ...filters, ...extra });
  project_keys.forEach((key) => params.append("project_keys", key));
  return `${endpoint}${path}?${params}`;
}

/** 月份下钻取自然月与当前筛选区间的交集，保证柱形图和明细金额一致。 */
function trendRange(date: string, range: PaymentDateRange): PaymentDateRange {
  if (date.length === 10) return { start_date: date, end_date: date };
  const start = `${date}-01`;
  const end = dayjs(start).endOf("month").format("YYYY-MM-DD");
  return { start_date: start < range.start_date ? range.start_date : start, end_date: end > range.end_date ? range.end_date : end };
}

interface DetailSelection {
  summaryPath: string;
  title: string;
  scope: PaymentDetailScope;
  range: PaymentDateRange;
  group?: { by: PaymentGroupBy; key: string };
}

function RequestError({ message, retry }: { message: string; retry: () => void }) {
  return <Alert showIcon type="error" message={message} action={<Button size="small" icon={<ReloadOutlined />} onClick={retry}>重试</Button>} />;
}

function PaymentTrend({ report, onSelect }: { report: PaymentStatisticsReport; onSelect: (date: string) => void }) {
  // 只有绘图尺寸使用浮点数，金额标签直接使用后端十进制文本。
  const scale = report.trend.reduce((best, point) => Math.max(best, Number(point.amount)), 0) || 1;
  const dates = report.trend.map((point) => point.date);
  const axis = [...new Set([dates[0], dates[Math.floor(dates.length / 2)], dates[dates.length - 1]])];
  return <section className="surface statistics-trend">
    <SectionTitle title="付款趋势" caption={report.granularity === "day" ? "按日" : "按月"} />
    {report.totals.count === 0 ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="所选期间暂无付款" /> : <>
      <div className="statistics-chart-scale"><span>金额（元）</span></div>
      <div className="payment-trend-scroll">
        <div style={{ minWidth: dates.length * 5 }}>
          <div className="statistics-trend-bars" role="group" aria-label="付款金额趋势">
            {report.trend.map((point) => {
              const amount = formatDecimalAmount(point.amount);
              return <Tooltip key={point.date} trigger={["hover", "focus"]} title={<>{point.date}<br />¥{amount} · {point.count} 单</>}>
                <button type="button" className="statistics-time-column" aria-label={`${point.date}，¥${amount}，${point.count} 单，查看明细`}
                  onClick={() => onSelect(point.date)}>
                  <span className="payment-trend-bar" style={{ height: `${Number(point.amount) / scale * 100}%` }} />
                </button>
              </Tooltip>;
            })}
          </div>
          <div className="statistics-trend-axis">{axis.map((date) => <span key={date}>{date}</span>)}</div>
        </div>
      </div>
    </>}
  </section>;
}

function ProjectRanking({ groups, showCompany, onSelect }: {
  groups: PaymentStatisticsGroup[];
  showCompany: boolean;
  onSelect: (group: PaymentStatisticsGroup) => void;
}) {
  const scale = Number(groups[0]?.amount) || 1;
  return <section className="surface statistics-ranking">
    <SectionTitle title="项目付款排行" caption="金额前 10 项" />
    {!groups.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="所选期间暂无项目排行" /> :
      <div className="statistics-rank-list">{groups.map((group, index) => {
        const label = showCompany ? `${group.name} · ${group.company_name}` : group.name;
        const amount = formatDecimalAmount(group.amount);
        return <Tooltip key={group.key} trigger={["hover", "focus"]} title={<>{label}<br />¥{amount} · {group.count} 单</>}>
          <button type="button" className="statistics-rank-row" aria-label={`${label}，¥${amount}，查看明细`} onClick={() => onSelect(group)}>
            <span className="statistics-rank-inner">
              <span className="statistics-rank-index">{index + 1}</span>
              <span className="statistics-rank-body">
                <span className="statistics-rank-heading"><strong>{label}</strong><span>¥{amount}</span></span>
                <span className="statistics-rank-track"><i style={{ width: `${Number(group.amount) / scale * 100}%` }} /></span>
              </span>
              <small>{group.percentage}%</small>
            </span>
          </button>
        </Tooltip>;
      })}</div>}
  </section>;
}

export function PaymentStatistics() {
  const { companyId, tick } = useWorkbench();
  const { message } = AntApp.useApp();
  const [range, setRange] = useState(defaultRange);
  const [projectSelection, setProjectSelection] = useState<{ companyId: string; keys: string[] }>({ companyId, keys: [] });
  const [groupBy, setGroupBy] = useState<PaymentGroupBy>("project");
  const [selection, setSelection] = useState<DetailSelection | null>(null);
  const [downloading, setDownloading] = useState(false);
  useAgentPageContext({ selection_type: "", selected_ids: [], filters: {} });

  const projectKeys = projectSelection.companyId === companyId ? projectSelection.keys : [];
  const filters = { ...range, company_id: companyId, project_keys: projectKeys };
  const summaryPath = statisticsPath("", filters);
  const options = useResource<PaymentStatisticsOptions>(query(`${endpoint}/options`, { company_id: companyId }), tick);
  const resource = useResource<PaymentStatisticsReport>(summaryPath, tick);
  const report = resource.data;
  useEffect(() => {
    setProjectSelection((current) => current.companyId === companyId ? current : { companyId, keys: [] });
  }, [companyId]);
  useEffect(() => setSelection(null), [summaryPath]);

  const viewDetails = (title: string, scope: PaymentDetailScope = "period") => setSelection({ summaryPath, title, scope, range });
  const viewGroup = (by: PaymentGroupBy, group: PaymentStatisticsGroup) => setSelection({
    summaryPath, title: group.company_name && !companyId ? `${group.name} · ${group.company_name}` : group.name,
    scope: "period", range, group: { by, key: group.key },
  });
  const exportReport = async () => {
    setDownloading(true);
    try {
      const response = await fetch(statisticsPath("/export", filters, { group_by: groupBy }), { credentials: "same-origin" });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : `导出失败（${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = `付款统计-${range.start_date}-${range.end_date}.xlsx`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      message.success("已导出付款统计");
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setDownloading(false);
    }
  };

  const cards = report ? [
    { label: "所选期间已支付金额", value: formatDecimalAmount(report.totals.period_amount), unit: "元", icon: <WalletOutlined />, color: "purple", caption: `${range.start_date} 至 ${range.end_date}`, help: true },
    { label: "截至结束日累计已支付金额", value: formatDecimalAmount(report.totals.cumulative_amount), unit: "元", icon: <FundOutlined />, color: "blue", caption: `截至 ${range.end_date}`, clickable: true },
    { label: "所选期间已支付单数", value: report.totals.count.toLocaleString("zh-CN"), unit: "单", icon: <FileTextOutlined />, color: "teal" },
    { label: "所选期间涉及项目数", value: report.totals.project_count.toLocaleString("zh-CN"), unit: "个", icon: <AppstoreOutlined />, color: "amber" },
  ] : [];

  return <div className="payment-statistics">
    <section className="surface statistics-filters" aria-label="付款筛选">
      <div className="statistics-filter-grid payment-filter-grid">
        <label><span>最终通过时间（北京时间）</span>
          <DatePicker.RangePicker aria-label="付款日期范围" allowClear={false} presets={presets}
            classNames={{ popup: { root: "payment-date-popup" } }}
            value={[dayjs(range.start_date), dayjs(range.end_date)]}
            onChange={(dates) => {
              if (dates?.[0] && dates[1]) setRange({ start_date: dates[0].format("YYYY-MM-DD"), end_date: dates[1].format("YYYY-MM-DD") });
            }} />
        </label>
        <label><span>所属项目</span>
          <Select<string[]> aria-label="所属项目" mode="multiple" showSearch optionFilterProp="label" allowClear maxTagCount="responsive"
            placeholder="全部项目" loading={options.loading} value={projectKeys}
            options={options.data?.projects.map((project) => ({ value: project.key, label: companyId ? project.name : `${project.name} · ${project.company_name}` }))}
            onChange={(keys) => setProjectSelection({ companyId, keys })} />
        </label>
        <Space wrap className="payment-filter-actions">
          <Button icon={<ReloadOutlined />} onClick={() => { setRange(defaultRange()); setProjectSelection({ companyId, keys: [] }); setGroupBy("project"); setSelection(null); }}>重置筛选</Button>
          <Button type="primary" icon={<DownloadOutlined />} loading={downloading} disabled={!report || Boolean(resource.error)} onClick={() => void exportReport()}>导出 Excel</Button>
        </Space>
      </div>
      {options.error && <RequestError message={options.error} retry={() => void options.reload()} />}
    </section>
    {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
    {resource.loading && <div className="surface statistics-loading"><Spin /><span>正在汇总付款记录</span></div>}
    {report && !resource.error && <>
      {report.missing_approved_count > 0 && <Alert showIcon type="warning" className="statistics-integrity"
        message={`${report.missing_approved_count} 单通过时间待补齐`}
        description="取得飞书最终通过时间后纳入对应区间。"
        action={<Button size="small" onClick={() => viewDetails("通过时间待补齐", "missing_approved_at")}>查看待补齐明细</Button>} />}
      <div className="stats-grid statistics-kpis payment-statistics-kpis">{cards.map((card) => <div className="stat-card" key={card.label}>
        <div className="stat-label"><span className="payment-stat-label">{card.label}{card.help && <HelpTip label="已支付统计口径">
          飞书付款申请最终通过后按申请金额计为已支付。原单撤销后，统计随最新同步状态更新。
        </HelpTip>}</span><span className={`stat-icon ${card.color}`} aria-hidden>{card.icon}</span></div>
        {card.clickable ? <button type="button" className="stat-value payment-stat-link" title={`${card.value} ${card.unit}`}
          aria-label={`${card.label} ${card.value} ${card.unit}，查看明细`} onClick={() => viewDetails("截至结束日累计", "cumulative")}>
          {card.value}<small>{card.unit}</small>
        </button> : <div className="stat-value" title={`${card.value} ${card.unit}`}>{card.value}<small>{card.unit}</small></div>}
        {card.caption && <div className="stat-detail"><span>{card.caption}</span></div>}
      </div>)}</div>
      <div className="statistics-distributions payment-statistics-charts">
        <PaymentTrend report={report} onSelect={(date) => setSelection({ summaryPath, title: date, scope: "period", range: trendRange(date, range) })} />
        <ProjectRanking groups={report.project_ranking} showCompany={!companyId} onSelect={(group) => viewGroup("project", group)} />
      </div>
      <section className="surface table-surface statistics-group-table">
        <SectionTitle title="分组汇总" action={<Space wrap>
          <Segmented<PaymentGroupBy> aria-label="汇总维度" value={groupBy} onChange={setGroupBy}
            options={[{ value: "project", label: "项目" }, { value: "department", label: "申请部门" }, { value: "payment_method", label: "付款方式" }]} />
          <Button icon={<FileTextOutlined />} onClick={() => viewDetails("所选期间")}>全部明细</Button>
        </Space>} />
        <Table<PaymentStatisticsGroup> key={groupBy} rowKey="key" dataSource={report.groups[groupBy]} scroll={{ x: 660 }}
          pagination={{ pageSize: 20, showSizeChanger: false, showTotal: (total) => `共 ${total} 个分组` }}
          locale={{ emptyText: "所选期间暂无汇总记录" }} columns={[
            { title: groupLabels[groupBy], key: "name", render: (_, row) => <button type="button" className="table-title-link" onClick={() => viewGroup(groupBy, row)}>
              <strong>{row.name}</strong>{row.company_name && !companyId && <small>{row.company_name}</small>}
            </button> },
            { title: "已支付金额（元）", dataIndex: "amount", width: 190, align: "right", render: (value: string) => <span className="amount-text">¥{formatDecimalAmount(value)}</span> },
            { title: "单数", dataIndex: "count", width: 90, align: "right" },
            { title: "区间占比", dataIndex: "percentage", width: 125, align: "right", render: (value: string) => `${value}%` },
          ]} />
      </section>
    </>}
    {selection?.summaryPath === summaryPath && <PaymentDetails selection={selection} filters={filters} onClose={() => setSelection(null)} />}
  </div>;
}

function PaymentDetails({ selection, filters, onClose }: { selection: DetailSelection; filters: PaymentStatisticsFilters; onClose: () => void }) {
  const { tick } = useWorkbench();
  const [page, setPage] = useState(1);
  const [approvalId, setApprovalId] = useState("");
  const { title, scope, range, group } = selection;
  const extra: Record<string, string> = { scope, page: String(page) };
  if (group) {
    extra.group_by = group.by;
    extra.group_key = group.key;
  }
  const resource = useResource<Page<PaymentStatisticsDetail>>(statisticsPath("/details", { ...filters, ...range }, extra), tick);
  const caption = scope === "cumulative" ? `截至 ${range.end_date}` : `${range.start_date} 至 ${range.end_date}`;
  return <>
    <Drawer open width="min(100vw, 1180px)" title={`付款明细 · ${title}`} onClose={onClose} destroyOnHidden>
      {scope !== "missing_approved_at" && <p className="statistics-detail-caption">{caption} · 北京时间</p>}
      {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
      <Table<PaymentStatisticsDetail> rowKey="id" dataSource={resource.data?.items} loading={resource.loading} scroll={{ x: 1120 }}
        pagination={{ current: page, pageSize: 20, total: resource.data?.total, showSizeChanger: false, onChange: setPage, showTotal: (total) => `共 ${total} 单` }}
        locale={{ emptyText: "当前范围暂无付款明细" }} columns={[
          { title: "审批编号 / 事由", width: 230, render: (_, row) => <button type="button" className="table-title-link" onClick={() => setApprovalId(row.id)}>
            <strong>{row.serial_number}</strong><small>{row.title}</small>
          </button> },
          { title: "项目 / 付款主体", width: 195, render: (_, row) => <div className="statistics-group-name"><strong>{row.project}</strong><small>{row.company_name}</small></div> },
          { title: "申请人 / 部门", width: 160, render: (_, row) => <ApplicantCell name={row.applicant} department={row.department} /> },
          { title: "付款方式", dataIndex: "payment_method", width: 140 },
          { title: "付款金额（元）", dataIndex: "amount", width: 180, align: "right", render: (value: string) => <span className="amount-text">¥{formatDecimalAmount(value)}</span> },
          { title: "最终通过时间（北京时间）", dataIndex: "approved_at", width: 215, render: (value: string | null) => value ? localTime(value, false, true) : "待补齐" },
        ]} />
    </Drawer>
    {approvalId && <Suspense fallback={<Spin fullscreen />}><ApprovalDrawer id={approvalId} onClose={() => setApprovalId("")} /></Suspense>}
  </>;
}
