import { useEffect, useState } from "react";
import {
  Alert, App as AntApp, Button, Collapse, DatePicker, Descriptions, Drawer,
  Input, Segmented, Select, Space, Table, Tag, Tooltip,
} from "antd";
import {
  ApartmentOutlined, DownloadOutlined, FileTextOutlined, FolderOpenOutlined,
  PrinterOutlined, ReloadOutlined,
} from "@ant-design/icons";
import dayjs from "dayjs";
import { bytes, localTime, money, post, query, useResource } from "../api";
import type { ArchivedApproval, ArchivedApprovalDetail, ArchivedFile } from "../archiveTypes";
import { ArchivePrintDialog } from "../components/ArchivePrintDialog";
import { XlsxPreviewButton } from "../components/XlsxPreview";
import { isXlsxFile } from "../xlsxPreview";
import { useAgentPageContext, useWorkbench } from "../context";
import type { Job, Page } from "../types";
import { ResourceState, StatusTag } from "../ui";

const archiveEndpoint = "/api/v1/archives";
const selectionLimit = 50;
const archiveStatuses = [
  { value: "ARCHIVED", label: "已归档" },
  { value: "PARTIAL", label: "部分待确认" },
  { value: "QUARANTINED", label: "待确认主体" },
  { value: "NONE", label: "待归档或无附件" },
  { value: "FAILED", label: "归档失败" },
];
const originNames: Record<string, string> = {
  FORM: "申请表单", TIMELINE: "审批动态", COMMENT: "审批评论", LOCAL: "本地归档", PACKAGE: "包内文件",
};
const fileStatuses: Record<string, string> = {
  AVAILABLE: "已取得", SUPPLEMENT: "补充原件", MISSING: "缺件", SUPERSEDED: "历史版本",
};

function currentMonth() {
  const fields = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
  }).formatToParts(new Date());
  return fields.find((item) => item.type === "year")!.value + "-" +
    fields.find((item) => item.type === "month")!.value;
}

function subjects(row: ArchivedApproval) {
  const names = row.companies.length ? row.companies.map((item) => item.name) : row.declared_company_names;
  return names.length ? names.join("、") : "待确认主体";
}

function ArchiveStatusTag({ row }: { row: ArchivedApproval }) {
  if (row.archive_status === "NONE") return <Tag>{row.attachment_count ? "待归档" : "暂无原件"}</Tag>;
  if (row.archive_status === "FAILED") return <Tag color="error">归档失败</Tag>;
  return <StatusTag value={row.archive_status} />;
}

function AttachmentList({ files }: { files: ArchivedFile[] }) {
  if (!files.length) return <p className="muted">本单暂无附件</p>;
  return <ul className="archive-originals">
    {files.map((file) => <li key={file.id}>
      <FileTextOutlined className="archive-original-icon" />
      <div className="archive-original-body">
        <strong>{file.filename}</strong>
        <small>{originNames[file.origin] || file.origin}{file.size === null ? "" : ` · ${bytes(file.size)}`}</small>
        {file.package_status === "READY" && <small>压缩包已展开</small>}
        {file.archive_source && <small className="break-word">{file.archive_source.filename} / {file.archive_source.path}</small>}
        {file.reason && <small className="archive-file-warning">{file.reason}</small>}
        {Object.keys(file.manifest).length > 0 && <details>
          <summary>原件清单</summary>
          {file.archive_path && <code className="break-word">{file.archive_path}</code>}
          <pre>{JSON.stringify(file.manifest, null, 2)}</pre>
        </details>}
      </div>
      {fileStatuses[file.status] ? <Tag color={file.status === "MISSING" ? "warning" : undefined}>{fileStatuses[file.status]}</Tag> :
        <StatusTag value={file.status} />}
      {file.download_url && isXlsxFile(file.filename) ? <XlsxPreviewButton url={file.download_url} filename={file.filename} size={file.size} /> :
        file.archive_source && file.preview_url && <Button type="link" href={file.preview_url}
          target="_blank" rel="noopener noreferrer">预览</Button>}
      {file.download_url && <Button type="link" icon={<DownloadOutlined />} href={file.download_url}
        aria-label={`下载${file.filename}`}>原件</Button>}
    </li>)}
  </ul>;
}

function ArchiveDrawer({ id, onClose, onPrint, printBusy }: {
  id: string;
  onClose: () => void;
  onPrint: (ids: string[]) => void;
  printBusy: boolean;
}) {
  const { tick, user, navigate, onJob } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<ArchivedApprovalDetail>(`${archiveEndpoint}/approvals/${id}`, tick);
  const row = resource.data;
  const [busy, setBusy] = useState(false);
  const run = async (path: string, label: string) => {
    setBusy(true);
    try {
      await onJob(await post<Job>(path), label);
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
      void resource.reload();
    }
  };
  return <Drawer open onClose={onClose} title="审批归档详情" width={720} loading={resource.loading}>
    <ResourceState error={resource.error}>
      {row && <>
        <div className="archive-detail-icon">
          <FolderOpenOutlined />
          <h3>{row.title}</h3>
          <ArchiveStatusTag row={row} />
        </div>
        <Descriptions bordered column={1} size="small" items={[
          { key: "serial", label: "申请编号", children: row.serial_number },
          { key: "company", label: "归属主体", children: subjects(row) },
          { key: "applicant", label: "申请人", children: row.applicant },
          { key: "amount", label: "申请金额", children: `¥ ${money(row.amount)}` },
          { key: "submitted", label: "申请提交时间", children: row.submitted_at ? localTime(row.submitted_at, false, true) : "待补齐" },
          { key: "approved", label: "最终通过时间", children: row.approved_at ? localTime(row.approved_at, false, true) : "待补齐" },
          { key: "status", label: "审批状态", children: <StatusTag value={row.status} /> },
          { key: "archived", label: "最近归档时间", children: localTime(row.archived_at ?? undefined, false, true) },
        ]} />
        <Space wrap className="history-heading">
          {row.printable && <Button type="primary" icon={<PrinterOutlined />} disabled={printBusy}
            onClick={() => onPrint([row.id])}>打印整单</Button>}
          {row.files.some((item) => item.download_url) && <Button icon={<DownloadOutlined />}
            href={query(archiveEndpoint + "/approval-download", { ids: row.id })}>打包下载</Button>}
          <Button onClick={() => navigate(`approvals/${row.id}`)}>查看原审批</Button>
          {row.feishu_url && <Button href={row.feishu_url} target="_blank" rel="noreferrer">打开飞书原单</Button>}
          {user.role !== "viewer" && <Button icon={<ReloadOutlined />} disabled={busy}
            onClick={() => void run(`${archiveEndpoint}/approvals/${row.id}/refresh-print-data`, "补齐打印资料")}>补齐打印资料</Button>}
          {user.role !== "viewer" && row.printable && <Button icon={<ReloadOutlined />} disabled={busy}
            onClick={() => void run(`/api/v1/approvals/${row.id}/archive`, "重新归档")}>重新归档</Button>}
        </Space>
        {(row.print_data_pending || !row.history_available || row.missing_count > 0) && <Alert className="archive-detail-notice"
          type="warning" showIcon message={row.missing_count ? `有 ${row.missing_count} 个附件待补齐` : "部分打印资料待同步"}
          description="可查看已取得的原件。整单打印时会列出缺件和待同步的资料。" />}
        <h4>当前附件（{row.attachment_count}）</h4>
        <AttachmentList files={row.files} />
        {row.history_files.length > 0 && <Collapse items={[{
          key: "history", label: `历史版本（${row.history_files.length}）`, children: <AttachmentList files={row.history_files} />,
        }]} />}
      </>}
    </ResourceState>
  </Drawer>;
}

export function Archives() {
  const { tick, companyId, setCompanyId, companies } = useWorkbench();
  const { message } = AntApp.useApp();
  const [filters, setFilters] = useState({
    month: currentMonth(), month_basis: "approved", invoice_month: "", archive_status: "",
    scope: "approved", business_type: "", q: "",
  });
  const [page, setPage] = useState(1);
  const [selectedKeys, setSelectedKeys] = useState<React.Key[]>([]);
  const [detailId, setDetailId] = useState("");
  const [printIds, setPrintIds] = useState<string[]>();
  const selectFilter = (next: Partial<typeof filters>) => {
    setFilters((current) => ({ ...current, ...next }));
    setPage(1);
    setSelectedKeys([]);
    setDetailId("");
  };
  useEffect(() => {
    setPage(1);
    setSelectedKeys([]);
    setDetailId("");
  }, [companyId]);

  useAgentPageContext({
    selection_type: "archived_approvals",
    selected_ids: selectedKeys.slice(0, 20).map(String),
    filters: {
      q: filters.q, approval_month: filters.month, invoice_month: filters.invoice_month,
      month_basis: filters.month_basis, archive_status: filters.archive_status,
      scope: filters.scope, business_type: filters.business_type,
    },
  });
  const resource = useResource<Page<ArchivedApproval>>(query(archiveEndpoint + "/approvals", {
    ...filters, company_id: companyId, page,
  }), tick);
  const changeCompany = (id: string, status = "") => {
    setCompanyId(id);
    selectFilter({ archive_status: status });
  };
  const startPrint = (ids: string[]) => {
    if (!printIds) setPrintIds(ids);
  };
  const allSubjects = companies.find((item) => item.id === companyId)?.name || "全部主体";
  const monthLabel = filters.month_basis === "approved" ? "最终通过月" : "申请提交月";
  const dateValue = (value: string) => value ? dayjs(value + "-01") : null;
  const statusOptions = filters.scope === "revoked" ? [{ value: "REVOKED", label: "已撤销留痕" }] : archiveStatuses;
  return <>
    <div className="archive-layout">
      <aside className="surface archive-tree">
        <div className="archive-tree-title"><FolderOpenOutlined />归档主体</div>
        <button className={!companyId && filters.archive_status !== "QUARANTINED" ? "selected" : ""}
          onClick={() => changeCompany("")}><FolderOpenOutlined />全部主体</button>
        {companies.map((company) => <button key={company.id} title={company.name}
          className={companyId === company.id ? "selected" : ""} onClick={() => changeCompany(company.id)}>
          <ApartmentOutlined /><span>{company.name}</span>
        </button>)}
        <div className="archive-tree-divider" />
        <button className={filters.archive_status === "QUARANTINED" ? "selected" : ""}
          onClick={() => { setCompanyId(""); selectFilter({ archive_status: "QUARANTINED", scope: "approved" }); }}>
          <FileTextOutlined />待确认主体
        </button>
      </aside>
      <section className="surface table-surface archive-table">
        <div className="table-toolbar archive-toolbar">
          <div className="toolbar-controls">
            <Segmented aria-label="归档单据范围" value={filters.scope}
              options={[{ label: "最终通过", value: "approved" }, { label: "撤销留痕", value: "revoked" }]}
              onChange={(scope) => selectFilter({ scope, archive_status: "" })} />
            <Select aria-label="月份口径" value={filters.month_basis}
              options={[{ value: "approved", label: "最终通过月" }, { value: "submitted", label: "申请提交月" }]}
              onChange={(month_basis) => selectFilter({ month_basis })} />
            <Tooltip title="按北京时间自然月筛选，清除后可查看时间待补齐的单据">
              <DatePicker picker="month" allowClear placeholder="不限月份" aria-label={monthLabel} value={dateValue(filters.month)}
                onChange={(value) => selectFilter({ month: value?.format("YYYY-MM") || "" })} />
            </Tooltip>
            <DatePicker picker="month" allowClear placeholder="开票月份" aria-label="开票月份" value={dateValue(filters.invoice_month)}
              onChange={(value) => selectFilter({ invoice_month: value?.format("YYYY-MM") || "" })} />
            <Select allowClear aria-label="业务类型" placeholder="业务类型" value={filters.business_type || undefined}
              options={[{ value: "REIMBURSEMENT", label: "费用报销" }, { value: "PAYMENT", label: "付款申请" }]}
              onChange={(business_type) => selectFilter({ business_type: business_type || "" })} />
            <Select allowClear aria-label="归档状态" placeholder="归档状态" value={filters.archive_status || undefined}
              options={statusOptions} onChange={(archive_status) => selectFilter({ archive_status: archive_status || "" })} />
          </div>
          <Space wrap>
            <Button icon={<DownloadOutlined />} disabled={!selectedKeys.length}
              href={selectedKeys.length ? query(archiveEndpoint + "/approval-download", { ids: selectedKeys.join(",") }) : undefined}>
              打包下载{selectedKeys.length ? ` (${selectedKeys.length})` : ""}
            </Button>
            {filters.scope === "approved" && <Button type="primary" icon={<PrinterOutlined />}
              disabled={!selectedKeys.length || !!printIds} onClick={() => startPrint(selectedKeys.map(String))}>
              打印所选{selectedKeys.length ? ` (${selectedKeys.length})` : ""}
            </Button>}
          </Space>
        </div>
        <div className="archive-subtoolbar">
          <span><FolderOpenOutlined />{filters.archive_status === "QUARANTINED" && !companyId ? "待确认主体" : allSubjects}
            <i>/</i><span>{monthLabel} · {filters.month || "不限"}</span>
            {filters.invoice_month && <><i>/</i><span>开票 · {filters.invoice_month}</span></>}
            <small>{resource.data?.total ?? 0} 张审批</small>
          </span>
          <Input.Search allowClear placeholder="搜索审批编号、申请人或附件" aria-label="搜索归档审批"
            maxLength={200} onSearch={(q) => selectFilter({ q })} />
        </div>
        <ResourceState error={resource.error}>
          <Table<ArchivedApproval>
            rowKey="id" loading={resource.loading} dataSource={resource.data?.items}
            rowSelection={{
              columnWidth: 46, selectedRowKeys: selectedKeys, preserveSelectedRowKeys: true,
              onChange: (keys) => {
                if (keys.length > selectionLimit) {
                  message.warning(`一次最多选择 ${selectionLimit} 张审批，请分批处理`);
                  return;
                }
                setSelectedKeys(keys);
              },
            }}
            scroll={{ x: 940 }}
            pagination={{
              current: page, total: resource.data?.total, pageSize: 20, onChange: setPage, showSizeChanger: false,
              showTotal: (total) => `共 ${total} 张审批`,
            }}
            columns={[
              { title: "审批单", width: 220, render: (_, row) =>
                <button className="table-title-link" onClick={() => setDetailId(row.id)}>
                  <strong>{row.title}</strong>
                  <small>{row.serial_number} · {row.business_type === "PAYMENT" ? "付款申请" : "费用报销"} · {row.applicant}</small>
                </button> },
              { title: "归属主体", width: 154, render: (_, row) => subjects(row) },
              { title: "申请金额", width: 110, align: "right", render: (_, row) => <span className="archive-amount">¥ {money(row.amount)}</span> },
              { title: monthLabel === "最终通过月" ? "最终通过时间" : "申请提交时间", width: 124, render: (_, row) => {
                const stamp = filters.month_basis === "approved" ? row.approved_at : row.submitted_at;
                return stamp ? <span className="muted">{localTime(stamp, false, true)}</span> : <Tag>待补齐</Tag>;
              } },
              { title: "归档状态", width: 95, render: (_, row) => <ArchiveStatusTag row={row} /> },
              { title: "附件", width: 62, render: (_, row) => <Tooltip title={row.missing_count ? `缺 ${row.missing_count} 个` :
                row.print_data_pending ? "整单附件待核对" : undefined}>
                <span>{row.attachment_count} 份{(row.missing_count > 0 || row.print_data_pending) && <sup className="archive-file-warning"> ·</sup>}</span>
              </Tooltip> },
              { title: "操作", fixed: "right", width: 125, render: (_, row) => <Space size={4} className="archive-row-actions">
                <Button type="link" size="small" onClick={() => setDetailId(row.id)}>详情</Button>
                {row.printable && <Button type="link" size="small" disabled={!!printIds}
                  onClick={() => startPrint([row.id])}>打印整单</Button>}
              </Space> },
            ]}
          />
        </ResourceState>
      </section>
    </div>
    {detailId && <ArchiveDrawer key={detailId} id={detailId} onClose={() => setDetailId("")} onPrint={startPrint} printBusy={!!printIds} />}
    {printIds && <ArchivePrintDialog approvalIds={printIds} onClose={() => setPrintIds(undefined)} />}
  </>;
}
