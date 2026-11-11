import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import { Alert, App as AntApp, Button, DatePicker, Drawer, Empty, Segmented, Select, Space, Spin, Table, Tag, Tooltip } from "antd";
import { BarChartOutlined, DownloadOutlined, FileTextOutlined, ReloadOutlined, TagsOutlined, TeamOutlined, WalletOutlined } from "@ant-design/icons";
import dayjs from "dayjs";
import utc from "dayjs/plugin/utc";
import timezone from "dayjs/plugin/timezone";
import type { Dayjs } from "dayjs";
import { query, useResource } from "../api";
import { useAgentPageContext, useWorkbench } from "../context";
import { ApplicantCell, HelpTip, SectionTitle, StatusTag } from "../ui";
import { StatisticsPieChart } from "../components/StatisticsPieChart";
import type { Page } from "../types";
import type { ReimbursementStatistics, StatisticsDetail, StatisticsFilters, StatisticsGroup, StatisticsOptions } from "../statisticsTypes";

dayjs.extend(utc);
dayjs.extend(timezone);
const ApprovalDrawer = lazy(() => import("./Approvals").then((module) => ({ default: module.ApprovalDrawer })));
const endpoint = "/api/v1/reimbursement-statistics";
type GroupBy = ReimbursementStatistics["group_by"];

/** 默认值和日期快捷选择以北京时间计算，不受浏览器所在地时区影响。 */
function today() { return dayjs().tz("Asia/Shanghai"); }
function defaults(): StatisticsFilters {
  return { startDate: today().startOf("month").format("YYYY-MM-DD"), endDate: today().format("YYYY-MM-DD"), applicantIds: [], expenseTypes: [], status: "APPROVED" };
}

/** 后端金额已经精确合计；直接格式化十进制文本，避免大额经 Number 转换丢分。 */
function amount(value?: string) {
  const [integer, fraction = "00"] = (value || "0.00").split(".");
  return integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",") + "." + fraction.padEnd(2, "0");
}

/** 数组选项重复传参；显式保留 status=，区分全部和后端缺省的已通过。 */
function statisticsQuery(path: string, companyId: string, filters: StatisticsFilters, extra: Record<string, string | number> = {}) {
  const params = new URLSearchParams({ company_id: companyId, start_date: filters.startDate, end_date: filters.endDate, status: filters.status });
  filters.applicantIds.forEach((value) => params.append("applicant_ids", value));
  filters.expenseTypes.forEach((value) => params.append("expense_types", value));
  Object.entries(extra).forEach(([key, value]) => params.set(key, String(value)));
  return path + "?" + params.toString();
}

function RequestError({ message, retry }: { message: string; retry: () => void }) {
  return <Alert showIcon type="error" message={message} action={<Button size="small" onClick={retry} icon={<ReloadOutlined />}>重试</Button>} />;
}

export function Statistics() {
  const { message } = AntApp.useApp();
  const { companyId, tick } = useWorkbench();
  const [filters, setFilters] = useState<StatisticsFilters>(defaults);
  const [groupBy, setGroupBy] = useState<GroupBy>("expense_type");
  // 图表类型仅控制金额分布区域的显示；切换时保留筛选、统计维度和数据，不重复查询。
  const [distributionChart, setDistributionChart] = useState<"bar" | "pie">("bar");
  const [groupPage, setGroupPage] = useState(1);
  const [selectedGroup, setSelectedGroup] = useState<StatisticsGroup | null>(null);
  const [downloading, setDownloading] = useState(false);
  useAgentPageContext({ selection_type: "", selected_ids: [], filters: {} });
  const options = useResource<StatisticsOptions>(query(endpoint + "/options", { company_id: companyId }), tick);
  const resource = useResource<ReimbursementStatistics>(statisticsQuery(endpoint, companyId, filters, { group_by: groupBy }), tick);
  const data = resource.data;
  useEffect(() => {
    setSelectedGroup(null);
    setGroupPage(1);
  }, [companyId, filters, groupBy]);
  useEffect(() => {
    // 原单更新后分组可能消失，分页不能停留在超出当前结果的旧页。
    setGroupPage((page) => Math.max(1, Math.min(page, Math.ceil((data?.groups.length || 0) / 20))));
  }, [data?.groups.length]);
  const applicantOptions = useMemo(() => {
    const items = options.data?.applicants || [];
    const counts = new Map<string, number>();
    items.forEach((item) => counts.set(item.label, (counts.get(item.label) || 0) + 1));
    return items.map((item) => ({ value: item.value, label: item.label
      + ((counts.get(item.label) || 0) > 1 ? `（${item.value}）` : "")
      + (item.identity_pending ? " · 身份待补齐" : "") }));
  }, [options.data]);
  const setRange = (range: [Dayjs, Dayjs]) => setFilters((value) => ({ ...value, startDate: range[0].format("YYYY-MM-DD"), endDate: range[1].format("YYYY-MM-DD") }));
  const exportReport = async () => {
    setDownloading(true);
    try {
      const response = await fetch(statisticsQuery(endpoint + "/export", companyId, filters, { group_by: groupBy }), { credentials: "same-origin" });
      if (!response.ok) {
        if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
        const body = await response.json().catch(() => ({}));
        throw new Error(typeof body.detail === "string" ? body.detail : `导出失败（${response.status}）`);
      }
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `报销统计-${filters.startDate}-${filters.endDate}.xlsx`;
      anchor.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      message.success("报销统计报表已导出");
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setDownloading(false);
    }
  };
  const maximum = data?.trend.reduce((value, point) => Math.max(value, Number(point.amount)), 1) || 1;
  const groupMaximum = Math.max(1, Number(data?.groups[0]?.amount || 0));
  return <div className="reimbursement-statistics">
    <section className="surface statistics-filters">
      <div className="statistics-filter-heading"><div><strong>统计范围</strong><HelpTip label="统计口径">按飞书申请提交时间统计，使用北京时间。每张审批计一次申请总额，跨主体单据不拆分金额；仅包含已同步审批，历史导入发票不单独计入。</HelpTip></div>
        <Space wrap><Button icon={<ReloadOutlined />} onClick={() => setFilters(defaults())}>重置筛选</Button>
          <Button type="primary" icon={<DownloadOutlined />} loading={downloading} disabled={!data || Boolean(resource.error)} onClick={() => void exportReport()}>导出 Excel</Button></Space></div>
      <div className="statistics-filter-grid">
        <label><span>提交时间（北京时间）</span><DatePicker.RangePicker aria-label="提交时间段" allowClear={false} value={[dayjs(filters.startDate), dayjs(filters.endDate)]}
          onChange={(range) => { if (range?.[0] && range[1]) setRange([range[0], range[1]]); }}
          presets={[
            { label: "本月", value: () => [today().startOf("month"), today()] },
            { label: "上月", value: () => [today().subtract(1, "month").startOf("month"), today().subtract(1, "month").endOf("month")] },
            { label: "近30天", value: () => [today().subtract(29, "day"), today()] },
            { label: "今年", value: () => [today().startOf("year"), today()] },
          ]} /></label>
        <label><span>报销人</span><Select aria-label="报销人" mode="multiple" allowClear showSearch optionFilterProp="label" maxTagCount="responsive" placeholder="全部报销人"
          loading={options.loading} value={filters.applicantIds} options={applicantOptions} onChange={(value) => setFilters((current) => ({ ...current, applicantIds: value }))} /></label>
        <label><span>报销类别</span><Select aria-label="报销类别" mode="multiple" allowClear showSearch optionFilterProp="label" maxTagCount="responsive" placeholder="全部类别"
          loading={options.loading} value={filters.expenseTypes} options={options.data?.expense_types.map((value) => ({ value, label: value }))}
          onChange={(value) => setFilters((current) => ({ ...current, expenseTypes: value }))} /></label>
        <label><span>审批状态</span><Select aria-label="审批状态" value={filters.status} onChange={(value) => setFilters((current) => ({ ...current, status: value }))} options={[
          { value: "APPROVED", label: "已通过" }, { value: "", label: "全部状态" }, { value: "PENDING", label: "审批中" },
          { value: "REJECTED", label: "已拒绝" }, { value: "CANCELED", label: "已撤回" }, { value: "REVERTED", label: "已撤销" }, { value: "DELETED", label: "已删除" },
        ]} /></label>
      </div>
      {options.error && <RequestError message={`筛选选项加载失败：${options.error}`} retry={() => void options.reload()} />}
    </section>
    {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
    {resource.loading && <div className="surface statistics-loading"><Spin /><span>正在汇总报销数据</span></div>}
    {data && !resource.error && <>
      {(data.missing_submitted_count > 0 || data.pending_identity_count > 0) && <Alert showIcon type="warning" className="statistics-integrity" message="部分历史信息待补齐"
        description={<>{data.missing_submitted_count > 0 && <div>{data.missing_submitted_count} 单符合其他筛选条件，提交时间待补齐，暂未纳入本次日期统计。</div>}
          {data.pending_identity_count > 0 && <div>已统计单据中有 {data.pending_identity_count} 单身份待补齐，暂按原申请人字段归组。</div>}
          <div>系统正在后台补齐历史信息；失败任务可由财务或管理员在后台任务中重试。</div></>} />}
      <div className="stats-grid statistics-kpis">{[
        { name: "报销申请总额", value: amount(data.totals.amount), unit: "元", icon: <WalletOutlined />, color: "purple" },
        { name: "报销申请单数", value: data.totals.count.toLocaleString("zh-CN"), unit: "单", icon: <FileTextOutlined />, color: "blue" },
        { name: "报销人数", value: data.totals.applicant_count.toLocaleString("zh-CN"), unit: "人", icon: <TeamOutlined />, color: "teal" },
        { name: "报销类别", value: data.totals.expense_type_count.toLocaleString("zh-CN"), unit: "类", icon: <TagsOutlined />, color: "amber" },
      ].map((item) => <div className="stat-card" key={item.name}><div className="stat-label">{item.name}<span className={`stat-icon ${item.color}`}>{item.icon}</span></div>
        <div className="stat-value" title={`${item.value} ${item.unit}`}>{item.value}<small>{item.unit}</small></div><div className="stat-detail"><span>{filters.startDate} 至 {filters.endDate}</span></div></div>)}</div>
      <div className="statistics-group-control"><Segmented aria-label="统计维度" value={groupBy} onChange={(value) => setGroupBy(value as GroupBy)}
        options={[{ label: "按报销类别", value: "expense_type" }, { label: "按报销人", value: "applicant" }]} /><HelpTip label="金额占比口径">金额占比以当前筛选总额为分母。</HelpTip></div>
      <div className="statistics-charts">
        <section className="surface statistics-trend"><SectionTitle title="报销金额趋势" caption={`${data.granularity === "day" ? "按日" : "按月"}汇总`} help="按申请提交时间统计，使用北京时间。" />
          {!data.totals.count ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前筛选下暂无报销申请" /> : <>
            <div className="statistics-chart-scale"><span>金额（元）</span><span>最高 ¥{amount(data.trend.reduce((maximumPoint, point) => Number(point.amount) > Number(maximumPoint.amount) ? point : maximumPoint).amount)}</span></div>
            <div className="statistics-trend-bars" role="img" aria-label="按提交时间统计的报销金额趋势">{data.trend.map((point) => <Tooltip key={point.date} title={<>{point.date}<br />¥{amount(point.amount)} · {point.count} 单</>}>
              <div className="statistics-trend-column"><i className={point.count ? "has-value" : ""} style={{ height: `${Number(point.amount) / maximum * 100}%` }} /></div></Tooltip>)}</div>
            <div className="statistics-trend-axis"><span>{data.trend[0]?.date}</span><span>{data.trend[Math.floor(data.trend.length / 2)]?.date}</span><span>{data.trend[data.trend.length - 1]?.date}</span></div>
          </>}
        </section>
        <section className="surface statistics-ranking statistics-distribution"><SectionTitle title={groupBy === "applicant" ? "报销人金额分布" : "报销类别金额分布"}
          caption={distributionChart === "bar" ? "金额前 10 项" : "全部分组"}
          help={distributionChart === "bar" ? "按金额排序，点击条目查看明细。" : "点击扇区或图例查看明细。"}
          action={<Segmented<"bar" | "pie"> size="small" aria-label="金额分布图表" value={distributionChart} onChange={setDistributionChart}
            options={[{ label: "条形图", value: "bar" }, { label: "饼图", value: "pie" }]} />} />
          {distributionChart === "pie" ? <StatisticsPieChart groups={data.groups} totalAmount={data.totals.amount} dimensionLabel={groupBy === "applicant" ? "报销人" : "报销类别"} formatAmount={amount} onSelect={setSelectedGroup} />
            : !data.groups.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无分组数据" /> : <div className="statistics-rank-list">{data.groups.slice(0, 10).map((group, index) => <button key={group.key} className="statistics-rank-row" onClick={() => setSelectedGroup(group)} title={`${group.name} · ${group.key}`}>
            <span className="statistics-rank-index">{index + 1}</span><span className="statistics-rank-body"><span className="statistics-rank-heading"><strong>{group.name}</strong><span>¥{amount(group.amount)}</span></span>
              <span className="statistics-rank-track"><i style={{ width: `${Number(group.amount) / groupMaximum * 100}%` }} /></span></span><small>{group.percentage}%</small></button>)}</div>}
        </section>
      </div>
      <section className="surface table-surface statistics-group-table"><SectionTitle title="分组汇总" caption={`共 ${data.groups.length} 个分组`} action={<Button icon={<FileTextOutlined />} onClick={() => setSelectedGroup({ key: "", name: "全部报销申请", amount: data.totals.amount, count: data.totals.count, percentage: "100.00", identity_pending: false })}>全部明细</Button>} />
        <Table<StatisticsGroup> rowKey="key" dataSource={data.groups} scroll={{ x: 740 }} pagination={{ current: groupPage, pageSize: 20, showSizeChanger: false, onChange: setGroupPage, showTotal: (total) => `共 ${total} 个分组` }} columns={[
          { title: groupBy === "applicant" ? "报销人" : "报销类别", dataIndex: "name", render: (_, row) => <div className="statistics-group-name"><button className="table-title-link" onClick={() => setSelectedGroup(row)}><strong>{row.name}</strong></button>
            {groupBy === "applicant" && <small title={row.key}>{row.key}</small>}{row.identity_pending && <Tag color="orange">身份待补齐</Tag>}</div> },
          { title: "报销金额（元）", dataIndex: "amount", align: "right", render: (value: string) => <span className="amount-text">¥{amount(value)}</span> },
          { title: "申请单数", dataIndex: "count", align: "right" },
          { title: "金额占比", dataIndex: "percentage", align: "right", render: (value: string) => `${value}%` },
          { title: "操作", width: 120, render: (_, row) => <Button type="link" onClick={() => setSelectedGroup(row)}>查看明细</Button> },
        ]} />
      </section>
      {data.demo_time_simulated && <div className="statistics-demo-note"><BarChartOutlined /> 演示提交时间为模拟数据</div>}
    </>}
    {selectedGroup && <StatisticsDetails key={`${groupBy}:${selectedGroup.key}`} group={selectedGroup} groupBy={groupBy} companyId={companyId} filters={filters} onClose={() => setSelectedGroup(null)} />}
  </div>;
}

function StatisticsDetails({ group, groupBy, companyId, filters, onClose }: {
  group: StatisticsGroup;
  groupBy: GroupBy;
  companyId: string;
  filters: StatisticsFilters;
  onClose: () => void;
}) {
  const { tick } = useWorkbench();
  const [page, setPage] = useState(1);
  const [approvalId, setApprovalId] = useState("");
  const resource = useResource<Page<StatisticsDetail>>(statisticsQuery(endpoint + "/details", companyId, filters,
    { page, ...(group.key ? { group_by: groupBy, group_key: group.key } : {}) }), tick);
  return <>
    <Drawer open width={1200} title={`审批明细 · ${group.name}`} onClose={onClose} destroyOnHidden className="statistics-details">
      <p className="statistics-detail-caption">{filters.startDate} 至 {filters.endDate} · 北京时间 · 申请提交时间</p>
      {resource.error && <RequestError message={resource.error} retry={() => void resource.reload()} />}
      <Table<StatisticsDetail> rowKey="id" dataSource={resource.data?.items} loading={resource.loading} scroll={{ x: 1280 }}
        pagination={{ current: page, pageSize: 20, total: resource.data?.total, showSizeChanger: false, onChange: setPage, showTotal: (total) => `共 ${total} 单` }} columns={[
          { title: "申请编号 / 事由", width: 260, render: (_, row) => <button className="table-title-link" onClick={() => setApprovalId(row.id)}><strong>{row.title}</strong><small>{row.serial_number}</small></button> },
          { title: "报销人 / 部门", width: 220, render: (_, row) => <div><ApplicantCell name={row.applicant} department={row.department} />{row.identity_pending && <Tag color="orange">身份待补齐</Tag>}</div> },
          { title: "报销类别", dataIndex: "expense_type", width: 140 },
          { title: "报销金额", dataIndex: "amount", align: "right", width: 160, render: (value: string) => <span className="amount-text">¥{amount(value)}</span> },
          { title: "提交时间（北京时间）", dataIndex: "submitted_at", width: 190, render: (value: string) => new Intl.DateTimeFormat("zh-CN", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value.endsWith("Z") ? value : value + "Z")) },
          { title: "审批状态", dataIndex: "status", width: 130, render: (value: string) => <StatusTag value={value} /> },
          { title: "操作", width: 100, render: (_, row) => <Button type="link" onClick={() => setApprovalId(row.id)}>查看</Button> },
        ]} />
    </Drawer>
    {approvalId && <Suspense fallback={<Spin fullscreen />}><ApprovalDrawer id={approvalId} onClose={() => setApprovalId("")} /></Suspense>}
  </>;
}
