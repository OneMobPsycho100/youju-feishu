import { useEffect, useRef, useState } from "react";
import {
  App as AntApp,
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  InputNumber,
  Modal,
  Segmented,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import {
  ArrowRightOutlined,
  CheckCircleFilled,
  CheckOutlined,
  CloseOutlined,
  CloudSyncOutlined,
  DownloadOutlined,
  FileTextOutlined,
  FileSearchOutlined,
  LinkOutlined,
  InfoCircleOutlined,
  ReloadOutlined,
  SearchOutlined,
} from "@ant-design/icons";
import { bytes, localTime, money, post, query, useResource } from "../api";
import { useAgentPageContext, useWorkbench } from "../context";
import { AICallLogs } from "../components/AICallLogs";
import { FeishuApprovalForm } from "../components/FeishuApprovalForm";
import { XlsxPreview } from "../components/XlsxPreview";
import { isXlsxFile } from "../xlsxPreview";
import { ApprovalInvoiceTable, invoiceAmount } from "../components/InvoiceDetails";
import { InvoiceRecognition, recognitionLabel } from "../components/InvoiceRecognition";
import { requestId } from "../requestId";
import { paymentAccountText } from "../paymentDetails";
import { isAIApprovalActive, useApprovalPolling } from "../approvalStatus";
import type {
  Approval,
  ApprovalDetail,
  Attachment,
  Decision,
  Evidence,
  InvoiceFields,
  Job,
  Page,
} from "../types";
import {
  ApplicantCell,
  AIApprovalStatusTag,
  ApprovalCompanies,
  DecisionTag,
  ResourceState,
  StatusTag,
} from "../ui";

export function Approvals({ detailId }: { detailId?: string }) {
  const { tick, companyId, navigate, user, mode, onJob } = useWorkbench();
  const { message } = AntApp.useApp();
  const [syncing, setSyncing] = useState(false);
  const [q, setQ] = useState("");
  const [draft, setDraft] = useState("");
  const [decision, setDecision] = useState("");
  const [status, setStatus] = useState("PENDING");
  const [businessType, setBusinessType] = useState("");
  const [page, setPage] = useState(1);
  // 审批中心只提供筛选和详情查看，助手沿用筛选条件，不再附带列表勾选。
  useAgentPageContext({ selection_type: "approvals", selected_ids: [], filters: { q, decision, status, business_type: businessType } });
  useEffect(() => {
    setPage(1);
  }, [companyId, q, decision, status, businessType]);
  const resource = useResource<Page<Approval>>(
    query("/api/v1/approvals", {
      q,
      decision,
      status,
      business_type: businessType,
      company_id: companyId,
      page,
    }),
    tick,
  );
  useApprovalPolling(!!resource.data?.items.some(row => row.ai_approval_enabled && isAIApprovalActive(row.ai_status)), resource.reload);
  const sync = async () => {
    setSyncing(true);
    try {
      // 全局补偿沿用管理员接口，不将列表筛选条件误当作同步范围；完成后由 onJob 刷新列表。
      await onJob(await post<Job>("/api/v1/sync"), "增量补偿同步");
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setSyncing(false);
    }
  };
  return (
    <>
      <div className="surface table-surface">
        <div className="table-toolbar">
          <Segmented
            aria-label="审批列表范围"
            value={decision || (status === "PENDING" ? "PENDING" : "")}
            onClickCapture={() => {
              // 重复点击当前页签也清除附加状态；切换目标范围由 onChange 随后设置。
              if (decision || status !== "PENDING") setStatus("");
              setPage(1);
            }}
            onChange={(value) => {
              // 页签决定基础范围；状态下拉框可继续叠加筛选预审结果。
              setDecision(value === "PENDING" ? "" : String(value));
              setStatus(value === "PENDING" ? "PENDING" : "");
              setPage(1);
            }}
            options={[
              { label: "审批中", value: "PENDING" },
              { label: "全部审批", value: "" },
              { label: "需复核", value: "REVIEW" },
              { label: "预审通过", value: "PASS" },
              { label: "规则阻断", value: "BLOCK" },
            ]}
          />
          <div className="toolbar-controls">
            {user.role === "admin" && <Tooltip title={mode === "demo" ? "演示环境无需同步真实飞书审批" : "同步所有已启用的审批模板，不受自动同步开关和列表筛选条件限制"}>
              <Button icon={<CloudSyncOutlined />} loading={syncing} disabled={mode === "demo"} onClick={() => void sync()}>
                增量补偿同步
              </Button>
            </Tooltip>}
            <Select aria-label="审批业务类型" value={businessType} style={{ width: 130 }} onChange={value => setBusinessType(value)}
              options={[{ value: "", label: "全部类型" }, { value: "REIMBURSEMENT", label: "费用报销" }, { value: "PAYMENT", label: "付款申请" }]} />
            <Select
              aria-label="审批状态"
              placeholder="审批状态"
              allowClear
              value={status || undefined}
              onChange={(x) => {
                setStatus(x || "");
                setPage(1);
              }}
              style={{ width: 145 }}
              options={[
                { label: "审批中", value: "PENDING" },
                { label: "已通过", value: "APPROVED" },
                { label: "已拒绝", value: "REJECTED" },
                { label: "已撤回", value: "CANCELED" },
              ]}
            />
            <Input
              placeholder="搜索事由、编号或申请人"
              prefix={<SearchOutlined />}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onPressEnter={() => {
                setQ(draft);
                setPage(1);
              }}
              allowClear
              onClear={() => setQ("")}
              style={{ width: 320 }}
              suffix={
                <Button
                  size="small"
                  type="text"
                  onClick={() => {
                    setQ(draft);
                    setPage(1);
                  }}
                >
                  查询
                </Button>
              }
            />
          </div>
        </div>
        <ResourceState error={resource.error}>
          <Table<Approval>
            rowKey="id"
            loading={resource.loading}
            dataSource={resource.data?.items}
            tableLayout="fixed"
            scroll={{ x: 1840 }}
            pagination={{
              current: page,
              total: resource.data?.total,
              pageSize: 20,
              showSizeChanger: false,
              onChange: setPage,
              showTotal: (total) => `共 ${total} 张审批单`,
            }}
            columns={[
            { title: "类型", dataIndex: "business_type", width: 100, render: (value: string) => <Tag color={value === "PAYMENT" ? "purple" : "blue"}>{value === "PAYMENT" ? "付款" : "报销"}</Tag> },
              {
                title: "审批申请 / 编号",
                dataIndex: "title",
                width: 280,
                render: (_, row) => (
                  <button
                    className="table-title-link"
                    onClick={() => navigate(`approvals/${row.id}`)}
                  >
                    <strong title={row.title}>{row.title}</strong>
                    <small title={row.serial_number}>{row.serial_number}</small>
                  </button>
                ),
              },
              {
                title: "申请人 / 部门",
                dataIndex: "applicant",
                width: 240,
                render: (_, row) => (
                  <ApplicantCell name={row.applicant} department={row.department} />
                ),
              },
              {
                title: "公司主体",
                dataIndex: "company_ids",
                width: 220,
                render: (_, row) => <ApprovalCompanies approval={row} compact />,
              },
              {
                title: "金额",
                dataIndex: "amount",
                width: 145,
                align: "right",
                render: (value: string) => (
                  <span className="amount-text">¥{money(value)}</span>
                ),
              },
              {
                title: "预审结果",
                dataIndex: "decision",
                width: 145,
                render: (value: Decision, row) => <DecisionTag value={value} previous={row.ai_approval_enabled && !row.manual_review_valid && row.ai_status !== "COMPLETED"} />,
              },
              {
                title: "AI审批状态",
                dataIndex: "ai_status",
                width: 140,
                render: (value: Approval["ai_status"], row) => <AIApprovalStatusTag value={value} enabled={row.ai_approval_enabled} />,
              },
              {
                title: "审批状态",
                dataIndex: "status",
                width: 140,
                render: (value: string) => <StatusTag value={value} />,
              },
              {
                title: "采集时间",
                dataIndex: "created_at",
                width: 160,
                render: (value: string) => (
                  <span className="muted">{localTime(value)}</span>
                ),
              },
              {
                title: "操作",
                key: "view",
                fixed: "right",
                width: 120,
                render: (_, row) => (
                  <Button
                    type="link"
                    onClick={() => navigate(`approvals/${row.id}`)}
                  >
                    查看
                    <ArrowRightOutlined />
                  </Button>
                ),
              },
            ]}
          />
        </ResourceState>
      </div>
      {detailId && (
        <ApprovalDrawer id={detailId} onClose={() => navigate("approvals")} />
      )}
    </>
  );
}

function accountText(value: unknown): string {
  if (value == null || value === "" || Array.isArray(value) && !value.length) return "—";
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}

/** 展示原件定位及完整读取状态，Office 材料不渲染成发票票面。 */
function MaterialText({ attachment }: { attachment: Attachment }) {
  const material = attachment.parsed_data.material;
  if (!material) return null;
  return <section className="payment-material-text">
    <Alert type={material.complete ? "success" : "warning"} showIcon message={recognitionLabel(attachment.parsed_data)} description={material.error || material.summary} />
    {material.confirmation && <p>{material.confirmation.actor_name} 已确认材料类别：{material.confirmation.comment}</p>}
    {!!material.segments?.length && <details><summary>查看材料正文与定位</summary>
      {material.segments.map((segment, index) => <div key={index}><strong>{segment.location}</strong><pre>{segment.text}</pre></div>)}
    </details>}
  </section>;
}

function InvoicePreview({ attachment, reference = false }: { attachment: Attachment; reference?: boolean }) {
  // 普通及包内 XLSX 都直接预览数据，不受发票解析的 SKIPPED 状态影响。
  if (isXlsxFile(attachment.filename, attachment.mime)) return <XlsxPreview key={attachment.id}
    url={`/api/v1/attachments/${attachment.id}/download`} filename={attachment.filename} size={attachment.size} />;
  const fields = attachment.parsed_data.fields || {};
  if (!reference && attachment.parsed_data.parse_status === "SKIPPED") return <Empty description="普通附件保留原件，可下载查看" />;
  if (attachment.mime === "application/pdf")
    return (
      <iframe
        className="attachment-iframe"
        title={attachment.filename}
        src={`/api/v1/attachments/${attachment.id}/download?preview=true`}
      />
    );
  if (["image/png", "image/jpeg", "image/webp"].includes(attachment.mime))
    return (
      <img
        className="attachment-image"
        alt={attachment.filename}
        src={`/api/v1/attachments/${attachment.id}/download?preview=true`}
      />
    );
  if (reference) return <Empty description="包内原件仅供查阅，可下载查看" />;
  if (attachment.parsed_data.material_type && attachment.parsed_data.material_type !== "INVOICE") return <MaterialText attachment={attachment} />;
  return (
    <div className="invoice-preview">
      <div className="invoice-preview-header">
        <span>票面信息视图</span>
        <small>
          {attachment.parsed_data.source || "待解析"} · 原件可下载核对
        </small>
      </div>
      <div className="invoice-paper">
        <div className="invoice-paper-heading">
          <span>电子发票</span>
          <small>字段展示，不代表税务验真</small>
        </div>
        <div className="invoice-number">
          <label>发票号码</label>
          <strong>{fields.number || "尚未识别"}</strong>
        </div>
        <div className="invoice-paper-grid">
          <div>
            <label>购买方名称</label>
            <b>{fields.buyer_name || "待确认"}</b>
          </div>
          <div>
            <label>纳税人识别号</label>
            <b className="mono">{fields.buyer_tax_id || "待确认"}</b>
          </div>
          <div>
            <label>销售方名称</label>
            <b>{fields.seller_name || "—"}</b>
          </div>
          <div>
            <label>开票日期</label>
            <b>{fields.issue_date || "待确认"}</b>
          </div>
        </div>
        <div className="invoice-total">
          <span>价税合计（小写）</span>
          <strong>¥ {money(fields.amount)}</strong>
        </div>
        <div className="invoice-paper-note">
          请以原始附件为准。票面字段经过人工纠正时，确认人与依据会单独留痕。
        </div>
      </div>
      {attachment.parsed_data.raw_text && (
        <details className="raw-text">
          <summary>查看本地解析原文</summary>
          <pre>{attachment.parsed_data.raw_text}</pre>
        </details>
      )}
    </div>
  );
}

function ReferencePdfInvoiceTable({ attachments, onView }: {
  attachments: Attachment[];
  onView: (attachmentId: string) => void;
}) {
  const invoices = new Map<string, Attachment>();
  for (const file of attachments) {
    const fields = file.parsed_data.fields;
    if (file.active && file.parsed_data.reference_pdf === "INVOICE" && fields?.number) {
      const key = (fields.code || "") + ":" + fields.number;
      if (!invoices.has(key)) invoices.set(key, file);
    }
  }
  return <section className="approval-invoice-summary" aria-label="包内 PDF 发票">
    <h4>包内 PDF 发票 · 补充识别</h4>
    <Table<Attachment>
      rowKey="id"
      size="small"
      pagination={false}
      scroll={{ x: 580 }}
      locale={{ emptyText: "暂无可展示的包内发票票面" }}
      dataSource={[...invoices.values()]}
      columns={[
        { title: "发票号码", key: "number", render: (_, file) => <span className="mono">{file.parsed_data.fields?.number}</span> },
        { title: "开票日期", key: "date", width: 120, render: (_, file) => file.parsed_data.fields?.issue_date },
        { title: "价税合计", key: "amount", width: 120, align: "right", render: (_, file) => invoiceAmount(file.parsed_data.fields?.amount) },
        { title: "操作", key: "view", width: 105, render: (_, file) => <Button type="link" onClick={() => onView(file.id)}>查看票面</Button> },
      ]}
    />
  </section>;
}

export function ApprovalDrawer({
  id,
  onClose,
}: {
  id: string;
  onClose: () => void;
}) {
  const { user, mode, tick, onJob, navigate, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<ApprovalDetail>(`/api/v1/approvals/${id}`, tick);
  const row = resource.data;
  useApprovalPolling(!!row?.ai_approval_enabled && isAIApprovalActive(row.ai_status), resource.reload);
  const [busy, setBusy] = useState(false);
  const [selectedAttachment, setSelectedAttachment] = useState("");
  const attachmentPreview = useRef<HTMLDivElement>(null);
  const detailTabs = useRef<React.ComponentRef<typeof Tabs>>(null);
  const [tab, setTab] = useState("audit");
  const [action, setAction] = useState<{
    kind: "APPROVE" | "REJECT";
    key: string;
  } | null>(null);
  const [correction, setCorrection] = useState<Attachment | null>(null);
  const [materialConfirmation, setMaterialConfirmation] = useState<{ file: Attachment; version: number } | null>(null);
  // 打开窗口时冻结审核 ID、版本和待核对项，防止刷新后误确认另一份结论。
  const [review, setReview] = useState<{
    auditId: string | null;
    version: number;
    evidence: Evidence[];
  } | null>(null);
  const [actionForm] = Form.useForm();
  const [correctionForm] = Form.useForm();
  const [reviewForm] = Form.useForm();
  const [materialForm] = Form.useForm();
  const attachment =
    row?.attachments.find((x) => x.id === selectedAttachment) ||
    row?.attachments.find((x) => x.active);
  const memberSources = new Map(row?.attachments.flatMap((parent) =>
    parent.parsed_data.archive?.members.map((member) =>
      [member.attachment_id, { path: member.path, parent }] as const) ?? []) ?? []);
  const selectedSource = attachment ? memberSources.get(attachment.id) : undefined;
  const selectedPackage = attachment?.parsed_data.archive;
  const memberPackage = selectedSource?.parent.parsed_data.archive;
  const referenceMember = memberPackage?.mode === "REFERENCE";
  const readOnlyPackage = Boolean(selectedPackage || referenceMember || memberPackage?.status === "FAILED");
  const referenceSummary = row?.reference_pdf_summary;
  const fileRecognitionLabel = (file: Attachment) => {
    if (memberSources.get(file.id)?.parent.parsed_data.archive?.mode === "REFERENCE" && !file.parsed_data.reference_pdf)
      return file.mime === "application/pdf" ? "PDF 待识别" : "仅供查阅";
    return recognitionLabel(file.parsed_data);
  };
  const viewAttachment = (attachmentId: string) => {
    setSelectedAttachment(attachmentId);
    attachmentPreview.current?.scrollIntoView({ block: "start" });
  };
  const viewInvoice = (invoiceId: string) => {
    const file = row?.attachments.find((item) => item.active && item.invoice_id === invoiceId);
    if (file) viewAttachment(file.id);
  };
  const canWrite = user.role !== "viewer";
  const outstanding = row?.actions.some((x) =>
    ["REQUESTED", "EXECUTING", "UNKNOWN"].includes(x.status),
  );
  const canAct =
    canWrite &&
    row?.status === "PENDING" &&
    row.task_status === "PENDING" &&
    Boolean(row.task_id) &&
    !outstanding;
  let taskUnavailable = "";
  if (mode !== "demo" && row?.status === "PENDING" && !outstanding) {
    if (row.task_status === "AMBIGUOUS") {
      taskUnavailable = "匹配到多个专用预审待办，请在飞书核对";
    } else if (row.task_status === "MISSING" || (row.task_status === "PENDING" && !row.task_id)) {
      taskUnavailable = "未找到匹配的专用预审待办";
    }
  }
  const pendingEvidence = row?.ai_approval_enabled
    ? row.audit?.evidence.filter((item) => !item.passed && item.severity !== "INFO") || []
    : [];
  const canReview = canAct && !row?.manual_review_valid;
  const canApprove = !!row?.manual_review_valid || (row?.ai_approval_enabled && row.decision === "PASS");
  const rejectionEvidence = pendingEvidence.filter((item) => item.auto_reject_eligible);
  const wasReviewed = (item: Evidence) => item.reviewed || (!item.source && item.code === "SEMANTIC"
    && row?.audit?.model_analysis.manual_review?.reviewed_codes.includes("SEMANTIC"));
  const automaticRejection = row?.actions.find((item) => item.action === "REJECT" && item.source_audit_id
    && (item.source_audit_id === row.audit?.id || row.status === "REJECTED"));
  const startReview = () => {
    if (!row) return;
    reviewForm.setFieldsValue({ comment: row.business_type === "PAYMENT" ? "已核对付款用途、收款信息和原始材料，确认符合申请要求" : "已核对报销用途及票据，确认可报销",
    });
    setReview({ auditId: row.audit?.id || null, version: row.version, evidence: pendingEvidence });
  };
  const submitReview = async () => {
    if (!review) return;
    const values = await reviewForm.validateFields().catch(() => null);
    if (!values) return;
    setBusy(true);
    try {
      const updated = await post<ApprovalDetail>(`/api/v1/approvals/${id}/reviews`, {
        scope: "ALL",
        audit_id: review.auditId,
        expected_version: review.version,
        comment: values.comment?.trim() || undefined,
      });
      setReview(null);
      setTab("audit");
      refresh();
      await resource.reload();
      message.success(updated.manual_review_valid ? "整单人工复核通过，现在可以同意预审" : "复核已保存，请刷新查看");
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const run = async (path: string, label: string) => {
    setBusy(true);
    try {
      const job = await post<Job>(path);
      await onJob(job, label);
      await resource.reload();
    } catch (reason) {
      if (!busy) message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const startCorrection = (file: Attachment) => {
    const fields = file.parsed_data.fields || {};
    correctionForm.setFieldsValue({ ...fields, comment: "已与原始票据核对，确认票面字段无误" });
    setCorrection(file);
  };
  const submitCorrection = async () => {
    const fields = await correctionForm.validateFields().catch(() => null);
    if (!fields) return;
    setBusy(true);
    try {
      const job = await post<Job>(
        `/api/v1/approvals/${id}/attachments/${correction!.id}/correct`,
        { ...fields, expected_version: row!.version },
      );
      setCorrection(null);
      await onJob(job, row?.ai_approval_enabled ? "确认字段并重新审核" : "确认票面字段");
      await resource.reload();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const submitAction = async () => {
    const values = await actionForm.validateFields().catch(() => null);
    if (!values) return;
    setBusy(true);
    try {
      const job = await post<Job>(`/api/v1/approvals/${id}/actions`, {
        action: action!.kind,
        comment: values.comment,
        expected_version: row!.version,
        idempotency_key: action!.key,
      });
      const label = action!.kind === "APPROVE" ? "同意预审" : "拒绝预审";
      // 入队成功即关闭确认框；后台失败后再次操作会生成新标识，避免反复取回同一个失败任务。
      // 请求本身未返回成功时保留原标识，网络中断重试仍沿用幂等保护。
      setAction(null);
      await onJob(job, label);
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      await resource.reload();
      setBusy(false);
    }
  };
  const submitMaterial = async () => {
    const values = await materialForm.validateFields().catch(() => null);
    if (!values || !materialConfirmation) return;
    setBusy(true);
    try {
      const job = await post<Job>(`/api/v1/approvals/${id}/attachments/${materialConfirmation.file.id}/material`, { ...values, expected_version: materialConfirmation.version });
      setMaterialConfirmation(null);
      await onJob(job, row?.ai_approval_enabled ? "确认材料类别并重新审核" : "确认材料类别");
      await resource.reload();
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  return (
    <Drawer
      open
      width="min(1050px, 100vw)"
      onClose={onClose}
      destroyOnHidden
      title={
        <div className="drawer-title">
          <span>审批详情</span>
          <Tag className="subtle-tag">{row?.serial_number || "加载中"}</Tag>
        </div>
      }
      extra={
        <Button
          type="text"
          icon={<ReloadOutlined />}
          onClick={() => void resource.reload()}
        >
          刷新
        </Button>
      }
      footer={
        row && (
          <div className="drawer-footer">
            {taskUnavailable ? (
              <span className="drawer-task-hint">{taskUnavailable}</span>
            ) : (
              <span className="muted">
                {canWrite ? "所有处理意见和执行身份将留存审计记录" : "当前为只读权限"}
              </span>
            )}
            <Space>
              {taskUnavailable && (
                <Button icon={<LinkOutlined />} href={row.feishu_url} target="_blank" rel="noreferrer">
                  打开飞书原审批
                </Button>
              )}
              {row.status === "APPROVED" && canWrite && (
                <Button
                  loading={busy}
                  icon={<FolderOpenIcon />}
                  onClick={() =>
                    void run(`/api/v1/approvals/${id}/archive`, "归档原件")
                  }
                >
                  重新归档
                </Button>
              )}
              {mode === "demo" &&
                row.status === "PENDING" &&
                row.task_status === "APPROVED" &&
                canWrite && (
                  <Button
                    type="primary"
                    loading={busy}
                    onClick={() =>
                      void run(
                        `/api/v1/demo/approvals/${id}/finish`,
                        "模拟后续审批通过并归档",
                      )
                    }
                  >
                    模拟后续审批通过
                  </Button>
                )}
              {canAct && (
                <>
                  {canReview && (
                    <Button icon={<FileSearchOutlined />} disabled={busy} onClick={startReview}>
                      人工复核
                    </Button>
                  )}
                  <Button
                    danger
                    icon={<CloseOutlined />}
                    disabled={busy}
                    onClick={() => {
                      setAction({ kind: "REJECT", key: requestId() });
                      actionForm.resetFields();
                      actionForm.setFieldsValue({ comment: row.suggested_reject_comment || "" });
                    }}
                  >
                    拒绝预审
                  </Button>
                  <Tooltip
                    title={
                      !canApprove
                        ? "完成整单人工复核后即可同意，无需等待 AI 或全部规则通过"
                        : "只同意专用预审节点，原有后续审批继续流转"
                    }
                  >
                    <Button
                      type="primary"
                      icon={<CheckOutlined />}
                      disabled={!canApprove || busy}
                      onClick={() => {
                        setAction({
                          kind: "APPROVE",
                          key: requestId(),
                        });
                        actionForm.resetFields();
                      }}
                    >
                      同意预审
                    </Button>
                  </Tooltip>
                </>
              )}
            </Space>
          </div>
        )
      }
    >
      <ResourceState loading={resource.loading} error={resource.error}>
        {row && (
          <>
            <div className="detail-heading">
              <div>
                <h2>{row.title}</h2>
                <span>
                  {row.applicant} · {row.department || "未填写部门"}
                  <i />
                  {row.business_type === "REIMBURSEMENT"
                    ? row.submitted_at ? "提交于 " + localTime(row.submitted_at, false, true) : "提交时间未同步"
                    : localTime(row.created_at)}
                </span>
              </div>
              <div className="detail-amount">
                <small>{row.business_type === "PAYMENT" ? "付款金额" : "报销总额"}</small>
                <strong>¥ {money(row.amount)}</strong>
              </div>
            </div>
            <div className="detail-tags">
              <Tag color={row.business_type === "PAYMENT" ? "purple" : "blue"}>{row.business_type === "PAYMENT" ? "付款申请" : "费用报销"}</Tag>
              <DecisionTag value={row.decision} previous={row.ai_approval_enabled && !row.manual_review_valid && row.ai_status !== "COMPLETED"} />
              <Space size={4}>
                {row.ai_approval_enabled && <span className="muted">AI审批</span>}
                <AIApprovalStatusTag value={row.ai_status} enabled={row.ai_approval_enabled} />
              </Space>
              <StatusTag value={row.status} />
              <ApprovalCompanies approval={row} />
              <Tag>未验真</Tag>
            </div>
            {row.business_type === "REIMBURSEMENT" && (
              <FeishuApprovalForm
                fields={row.feishu_form ?? []}
                feishuUrl={mode === "demo" ? undefined : row.feishu_url}
                onViewAttachments={row.attachments.some(file => file.active) ? () => {
                  setTab("attachments");
                  detailTabs.current?.nativeElement?.scrollIntoView({ block: "start" });
                } : undefined}
              />
            )}
            <Descriptions
              size="small"
              column={3}
              className="detail-description"
              items={[
                {
                  key: "expense",
                  label: "费用类型",
                  children: row.expense_type,
                },
                {
                  key: "task",
                  label: "飞书预审节点",
                  children: <StatusTag value={row.task_status} />,
                },
                {
                  key: "version",
                  label: "数据版本",
                  children: `V${row.version}`,
                },
              ]}
            />
            {row.business_type === "PAYMENT" && <>
              <Descriptions size="small" column={2} className="detail-description" items={[
                { key: "company", label: "付款主体", children: row.payment_details.company_name || "待确认" },
                { key: "project", label: "所属项目", children: row.payment_details.project || "—" },
                { key: "method", label: "付款方式", children: row.payment_details.payment_method || "—" },
                { key: "date", label: "期望付款日期", children: row.payment_details.expected_payment_date || "—" },
                { key: "account", label: "收款账户", children: <pre className="payment-account">{paymentAccountText(row.payment_details.payee_account)}</pre> },
                { key: "related", label: "关联审批", children: accountText(row.payment_details.related_approvals) },
                { key: "remarks", label: "备注", children: row.payment_details.remarks || "—" },
              ]} />
              {row.payment_details.material_issues.map(issue => <Alert key={issue.key} type="warning" showIcon message={`${issue.filename}：${issue.reason}`} />)}
            </>}
            {outstanding && (
              <Alert
                className="detail-alert"
                showIcon
                type="info"
                message="此待办正在回写或核对结果，请等待后台完成，避免重复操作。"
              />
            )}
            {canReview && row.decision !== "PASS" && (
              <Alert
                className="detail-alert"
                showIcon
                type={row.decision === "BLOCK" ? "error" : "warning"}
                message={row.decision === "BLOCK" ? "存在待处理的阻断项" : "待人工复核"}
                description={rejectionEvidence.length
                  ? `命中规则：${[...new Set(rejectionEvidence.map((item) => item.title))].join("、")}。核对整单及原始附件后，可由人工复核确认。`
                  : "人工复核可直接确认整单，包括票面、金额及重复占用异常，无需等待 AI 或全部规则匹配。"}
                action={<Button size="small" onClick={startReview} disabled={busy}>去复核</Button>}
              />
            )}
            {automaticRejection && (
              <Alert className="detail-alert" showIcon
                type={automaticRejection.status === "SUCCEEDED" ? "success" : automaticRejection.status === "FAILED" ? "error" : "info"}
                message={{ REQUESTED: "按规则拒绝已入队", EXECUTING: "正在回写拒绝结果", UNKNOWN: "拒绝结果核对中",
                  SUCCEEDED: "已按标准拒绝", FAILED: "自动拒绝失败" }[automaticRejection.status] || "自动拒绝处理记录"}
                description={<><span className="rejection-message">{automaticRejection.comment}</span>{automaticRejection.error && <p>{automaticRejection.error}</p>}</>} />
            )}
            <Tabs
              ref={detailTabs}
              activeKey={tab}
              onChange={setTab}
              items={[
                {
                  key: "ai-logs",
                  label: "AI 调用日志",
                  children: <AICallLogs approvalId={row.id} />,
                },
                {
                  key: "audit",
                  label: row.ai_approval_enabled ? "Agent 预审" : "人工预审",
                  children: (
                    <>
                      <div
                        className={`audit-summary summary-${row.decision.toLowerCase()}`}
                      >
                        <div>
                          <DecisionTag value={row.decision} previous={row.ai_approval_enabled && !row.manual_review_valid && row.ai_status !== "COMPLETED"} />
                          <strong>
                            {referenceSummary ? "原预审记录，当前票面见下方补充识别" : row.manual_review_valid
                              ? row.status === "PENDING" ? "整单人工复核已通过，可直接同意预审" : "整单人工复核已通过"
                              : !row.ai_approval_enabled
                              ? row.status === "PENDING" ? "请核对原件并完成整单人工复核" : "模板当前由财务人工预审"
                              : row.ai_status === "QUEUED"
                              ? "AI预审排队中，处理完成后自动更新结果"
                              : row.ai_status === "RUNNING"
                                ? "AI预审正在处理附件和审核，完成后自动更新结果"
                                : row.ai_status === "FAILED"
                                  ? "本轮AI预审失败，可重新预审"
                                  : row.ai_status === "NOT_STARTED"
                                    ? "等待开始AI预审"
                                    : row.decision === "PASS"
                              ? "核验项目已通过，可继续处理预审节点"
                              : row.decision === "BLOCK"
                                ? `${rejectionEvidence.length ? "命中拒绝标准" : "存在规则阻断"}：${[...new Set(pendingEvidence.filter((item) => item.severity === "BLOCK").map((item) => item.title))].join("、")}`
                                : "部分信息需要财务进一步核对"}
                          </strong>
                        </div>
                        <span>
                          {row.audit
                            ? `${row.ai_approval_enabled ? `规则 V${row.audit.rule_version}` : "复核记录"} · ${localTime(row.audit.created_at)}`
                            : row.ai_approval_enabled ? "等待预审处理" : ""}
                        </span>
                      </div>
                      {referenceSummary && <Alert
                        type="info"
                        showIcon
                        message={"包内 PDF 补充识别 · " + referenceSummary.pdf_count + " 份原件"}
                        description={[
                          "已识别 " + referenceSummary.invoice_count + " 张发票",
                          referenceSummary.amount !== null ? "票面合计 ¥" + money(referenceSummary.amount) : "",
                          referenceSummary.travel_count > 0 ? referenceSummary.travel_count + " 份网约车行程单" : "",
                          referenceSummary.review_count > 0 ? referenceSummary.review_count + " 份待核对" : "",
                          referenceSummary.pending_count > 0 ? referenceSummary.pending_count + " 份待识别" : "",
                        ].filter(Boolean).join(" · ")}
                      />}
                      {!row.ai_approval_enabled && !!row.audit?.evidence.length && <h4>历史预审依据</h4>}
                      <div className="audit-evidence-list">
                        {row.audit?.evidence.map((item, index) => referenceSummary && ["ATTACHMENTS", "AMOUNT"].includes(item.code) ? (
                          <details className="audit-technical-evidence" key={item.code + "-" + index}>
                            <summary>原预审 · {item.title}</summary>
                            <p>{item.detail}</p>
                          </details>
                        ) : (
                          <div
                            className={`evidence-row ${item.result === "SKIPPED" ? "info" : item.passed ? "passed" : item.severity === "INFO" ? "info" : item.severity === "BLOCK" ? "blocked" : "review"}`}
                            key={`${item.code}-${index}`}
                          >
                            <span className="evidence-icon">
                              {item.result === "SKIPPED" ? <InfoCircleOutlined /> : item.passed ? (
                                <CheckCircleFilled />
                              ) : item.severity === "INFO" ? (
                                <InfoCircleOutlined />
                              ) : (
                                <span>!</span>
                              )}
                            </span>
                            <div>
                              <strong>{item.title}</strong>
                              {item.source === "CUSTOM" && <Tag className="rule-source-tag">文字标准</Tag>}
                              <p>{wasReviewed(item) ? "原判定依据：" : ""}{item.user_message || item.detail}</p>
                              {item.user_message && <details className="audit-technical-evidence">
                                <summary>查看核对依据</summary><p>{item.detail}</p>
                              </details>}
                              {wasReviewed(item) && <p>此项已人工确认，复核意见详见操作留痕。</p>}
                              {item.related_approval && (
                                <Button
                                  size="small"
                                  type="link"
                                  onClick={() =>
                                    navigate(
                                      `approvals/${item.related_approval}`,
                                    )
                                  }
                                >
                                  查看关联审批
                                  <LinkOutlined />
                                </Button>
                              )}
                            </div>
                            <Tag
                              color={
                                item.result === "SKIPPED" ? "default" : item.passed
                                  ? "success"
                                  : item.severity === "INFO" ? "default" : item.severity === "BLOCK"
                                    ? "error"
                                    : "warning"
                              }
                            >
                              {item.result === "SKIPPED" ? "已跳过" : item.passed
                                ? wasReviewed(item) ? "人工通过" : "通过"
                                : item.auto_reject_eligible ? "拒绝" : item.result === "UNKNOWN" ? "待确认" : item.severity === "INFO" ? "提示" : item.severity === "BLOCK"
                                  ? "阻断"
                                  : "需复核"}
                            </Tag>
                          </div>
                        ))}
                      </div>
                      {row.audit && (
                        <div className="model-summary">
                          {row.audit.model_analysis.manual_review && (
                            <Alert
                              showIcon
                              type={row.audit.model_analysis.manual_review.scope === "ALL" && !row.manual_review_valid ? "warning" : "success"}
                              message={`${row.audit.model_analysis.manual_review.actor_name} 已完成${row.audit.model_analysis.manual_review.scope === "ALL" ? "整单人工" : "标准"}复核${row.audit.model_analysis.manual_review.scope === "ALL" && !row.manual_review_valid ? "，内容已变化，需重新确认" : ""}`}
                              description={`${row.audit.model_analysis.manual_review.comment} · ${localTime(row.audit.model_analysis.manual_review.created_at)}`}
                            />
                          )}
                          {(row.ai_approval_enabled || row.audit.model_analysis.engine) && <>
                          <h4>
                            {row.ai_approval_enabled ? "Agent 辅助分析" : "历史 AI 分析"}
                            <Tag>
                              {row.audit.model_analysis.status === "COMPLETED"
                                ? "已分析"
                                : row.audit.model_analysis.status === "FAILED"
                                  ? "调用失败"
                                  : "规则预审"}
                            </Tag>
                          </h4>
                          <p>{row.audit.model_analysis.summary}</p>
                          {row.audit.model_analysis.engine && (
                            <p className="muted">
                              {row.audit.model_analysis.engine} · 模型 {row.audit.model_analysis.model_name || "未启用"}
                              {" · "}{row.audit.model_analysis.prompt_source === "node" ? "节点提示词" : "全局提示词"}
                              {" · "}连接版本 {row.audit.model_analysis.connection_version}
                            </p>
                          )}
                          {row.audit.model_analysis.findings?.map((x) => (
                            <p key={x}>· {x}</p>
                          ))}
                          <Button type="link" onClick={() => setTab("ai-logs")}>查看 AI 调用过程、思考与原始输出</Button>
                          {row.audit.model_analysis.prompt && (
                            <details className="agent-prompt-preview">
                              <summary>查看本次审核使用的提示词</summary>
                              <pre>{row.audit.model_analysis.prompt}</pre>
                            </details>
                          )}
                          </>}
                        </div>
                      )}
                      <div className="detail-inline-actions">
                        {canWrite && (row.ai_approval_enabled || referenceSummary) && (
                          <Button
                            icon={<ReloadOutlined />}
                            loading={busy}
                            onClick={() =>
                              void run(
                                `/api/v1/approvals/${id}/audit`,
                                referenceSummary ? "识别包内 PDF" : "重新预审",
                              )
                            }
                          >
                            {referenceSummary ? "识别包内 PDF" : "重新获取附件并审核"}
                          </Button>
                        )}
                        <Button
                          icon={<FileTextOutlined />}
                          onClick={() => setTab("attachments")}
                        >
                          核对票据与原件
                        </Button>
                        {mode !== "demo" && !taskUnavailable && (
                          <Button
                            icon={<LinkOutlined />}
                            href={row.feishu_url}
                            target="_blank"
                            rel="noreferrer"
                          >
                            打开飞书原审批
                          </Button>
                        )}
                      </div>
                    </>
                  ),
                },
                {
                  key: "attachments",
                  label: `${row.business_type === "PAYMENT" ? "付款材料" : "发票与附件"} (${row.attachments.filter((x) => x.active).length})`,
                  children: (
                    <div className="attachments-layout">
                      {row.business_type === "REIMBURSEMENT" && (!referenceSummary || row.invoices.length > 0) && (
                        <ApprovalInvoiceTable invoices={row.invoices} totals={row.invoice_totals} onView={viewInvoice} />
                      )}
                      {referenceSummary && <ReferencePdfInvoiceTable attachments={row.attachments} onView={viewAttachment} />}
                      <div className="attachment-list">
                        {row.attachments.map((file) => (
                          <button
                            key={file.id}
                            onClick={() => setSelectedAttachment(file.id)}
                            className={`attachment-item ${attachment?.id === file.id ? "selected" : ""} ${!file.active ? "inactive" : ""}`}
                          >
                            <FileTextOutlined />
                            <div>
                              <strong>{file.filename}</strong>
                              {memberSources.has(file.id) && <small title={memberSources.get(file.id)?.path}>包内 · {memberSources.get(file.id)?.path}</small>}
                              <small>
                                {bytes(file.size)} ·{" "}
                                {file.active ? fileRecognitionLabel(file) : "历史版本"}
                              </small>
                            </div>
                          </button>
                        ))}
                      </div>
                      <div className="attachment-preview-wrap" ref={attachmentPreview}>
                        {attachment ? (
                          <>
                            <div className="attachment-toolbar">
                              <Tag
                                color={
                                  attachment.parsed_data.parse_status === "SKIPPED" ? "default" : attachment.parsed_data.confirmed ||
                                  ["LOCAL", "VERIFIED", "STRUCTURED"].includes(attachment.parsed_data.recognition?.status || "")
                                    ? "success"
                                    : "warning"
                                }
                              >
                                {fileRecognitionLabel(attachment)}
                              </Tag>
                              <Space>
                                {canWrite &&
                                  row.status === "PENDING" &&
                                  attachment.active && !readOnlyPackage && (!attachment.parsed_data.material_type || attachment.parsed_data.material_type === "INVOICE") && (
                                    <Button
                                      size="small"
                                      onClick={() =>
                                        startCorrection(attachment)
                                      }
                                    >
                                核对并确认票面
                                    </Button>
                                  )}
                                 {canAct && row.business_type === "PAYMENT" && attachment.active && !readOnlyPackage && <Button size="small" onClick={() => {
                                  materialForm.setFieldsValue({ material_type: attachment.parsed_data.material_type === "UNKNOWN" ? undefined : attachment.parsed_data.material_type, comment: "" });
                                  setMaterialConfirmation({ file: attachment, version: row.version });
                                }}>确认材料类别</Button>}
                                <Button
                                  size="small"
                                  icon={<DownloadOutlined />}
                                  href={`/api/v1/attachments/${attachment.id}/download`}
                                >
                                  原件
                                </Button>
                              </Space>
                            </div>
                            {attachment.parsed_data.error && (
                              <Alert
                                showIcon
                                type="warning"
                                message={attachment.parsed_data.error}
                              />
                            )}
                            {selectedSource && <p className="muted break-word">包内来源 · {selectedSource.parent.filename} / {selectedSource.path}</p>}
                            <InvoiceRecognition data={attachment.parsed_data} />
                            {selectedPackage ? <section aria-label="包内文件">
                              {selectedPackage.members.length > 0 && <ul>
                                {selectedPackage.members.map((member) => <li key={member.attachment_id}>
                                  <Button type="link" onClick={() => setSelectedAttachment(member.attachment_id)}
                                    style={{ whiteSpace: "normal", height: "auto" }}>{member.path}</Button>
                                </li>)}
                              </ul>}
                            </section> : <InvoicePreview attachment={attachment} reference={referenceMember} />}
                            {row.business_type === "PAYMENT" && (attachment.mime === "application/pdf" || attachment.mime.startsWith("image/")) && <MaterialText attachment={attachment} />}
                            <div className="attachment-hash">
                              <span>SHA256</span>
                              <code>{attachment.sha256}</code>
                            </div>
                            {attachment.parsed_data.confirmed && (
                              <Alert
                                type="success"
                                message={`${attachment.parsed_data.confirmed_by} 已核对确认`}
                                description={
                                  attachment.parsed_data.confirmation_comment
                                }
                              />
                            )}
                          </>
                        ) : (
                          <Empty description="尚未获取附件" />
                        )}
                      </div>
                    </div>
                  ),
                },
                {
                  key: "history",
                  label: "占用与操作留痕",
                  children: (
                    <>
                      <h4>本单发票占用</h4>
                      <Table
                        rowKey="id"
                        pagination={false}
                        size="small"
                        dataSource={row.claims}
                        columns={[
                          { title: "来源单据", dataIndex: "source_label" },
                          {
                            title: "占用状态",
                            dataIndex: "status",
                            render: (value) => <StatusTag value={value} />,
                          },
                        ]}
                      />
                      <h4 className="history-heading">预审处理记录</h4>
                      {row.reviews?.length > 0 && (
                        <div className="action-history">
                          {row.reviews.map((item) => (
                            <div key={item.audit_id}>
                              <div>
                                <strong>{item.actor_name} · {item.scope === "ALL" ? "整单人工复核" : "人工复核审核标准"}</strong>
                                <Tag color="success">已确认</Tag>
                              </div>
                              <p>{item.comment}</p>
                              <small>{item.scope === "ALL" ? "确认范围：整单及全部审核规则" : `确认条目：${item.reviewed_codes.map((code) => row.audit?.evidence.find((entry) => entry.code === code)?.title || code).join("、")}`}</small>
                              {item.promoted_at && <small>原复核记录已于 {localTime(item.promoted_at)} 升级为整单复核</small>}
                              {item.conflicting_claims?.map((claim) => <small key={claim.claim_id}>人工放行关联占用：{claim.source_label} · {claim.status}</small>)}
                              <small>{localTime(item.created_at)}</small>
                            </div>
                          ))}
                        </div>
                      )}
                      {row.actions.length ? (
                        <div className="action-history">
                          {row.actions.map((item) => (
                            <div key={item.id}>
                              <div>
                                <strong>
                                  {item.actor_name} ·{" "}
                                  {item.action === "APPROVE"
                                    ? "同意预审"
                                    : "拒绝预审"}
                                </strong>
                                <StatusTag value={item.status} />
                              </div>
                              <p>{item.comment}</p>
                              <small>
                                {localTime(item.created_at)} · 飞书身份{" "}
                                {item.feishu_identity}
                                {item.source_audit_id && ` · 来源审核 ${item.source_audit_id}`}
                              </small>
                              {item.error && (
                                <Alert type="warning" message={item.error} />
                              )}
                            </div>
                          ))}
                        </div>
                      ) : (
                        !row.reviews?.length && <Empty description="尚无预审处理记录" />
                      )}
                    </>
                  ),
                },
              ]}
            />
          </>
        )}
      </ResourceState>
      <Modal open={!!materialConfirmation} title="确认付款材料类别" onCancel={() => !busy && setMaterialConfirmation(null)} onOk={() => void submitMaterial()} confirmLoading={busy} okText={row?.ai_approval_enabled ? "确认并重新审核" : "保存确认"}>
        <p>请先核对原件。材料类别确认不会直接同意审批，已入台账的发票仍保留占用约束。</p>
        <Form form={materialForm} layout="vertical">
          <Form.Item name="material_type" label="材料类别" rules={[{ required: true }]}><Select options={[{ value: "INVOICE", label: "发票" }, { value: "CONTRACT", label: "合同" }, { value: "QUOTE", label: "报价单" }, { value: "OTHER", label: "其他材料" }]} /></Form.Item>
          <Form.Item name="comment" label="核对依据" rules={[{ required: true, whitespace: true, min: 2 }]}><Input.TextArea maxLength={600} rows={3} /></Form.Item>
        </Form>
      </Modal>
      <Modal
        open={!!review}
        centered
        styles={{ body: { maxHeight: "70dvh", overflowY: "auto" } }}
        title="人工复核"
        onCancel={() => !busy && setReview(null)}
        onOk={() => void submitReview()}
        confirmLoading={busy}
        okText="确认整单复核通过"
      >
        <Alert
          showIcon
          type="info"
          message="确认整单及原始附件"
          description="确认后人工结论优先于全部审核规则，包括金额、票面和重复占用异常，可直接手动同意预审。原始判断及关联记录保留；单据或附件变化后需要重新复核。"
        />
        <div className="model-summary">
          {review?.evidence.map((item, index) => (
            <div key={`${item.code}-${index}`}>
              <strong>{item.title}</strong>
              <p>{item.user_message || item.detail}</p>
            </div>
          ))}
        </div>
        <Space wrap>
          <Button icon={<FileTextOutlined />} disabled={busy} onClick={() => {
            setReview(null);
            setTab("attachments");
          }}>核对票据与原件</Button>
          {row && mode !== "demo" && (
            <Button icon={<LinkOutlined />} href={row.feishu_url} target="_blank" rel="noreferrer">打开飞书原审批</Button>
          )}
        </Space>
          <Form form={reviewForm} layout="vertical" className="modal-form">
            <Form.Item name="comment" label="复核意见"
              rules={[{ max: 600, message: "复核意见最多 600 字" }, {
                validator: (_, value: string | undefined) => value?.trim() && value.trim().length < 2
                  ? Promise.reject(new Error("复核意见至少 2 个字")) : Promise.resolve(),
              }]}>
              <Input.TextArea rows={2} maxLength={600} showCount />
            </Form.Item>
          </Form>
      </Modal>
      <Modal
        open={!!action}
        title={
          action?.kind === "APPROVE" ? "同意专用预审节点" : "拒绝此审批申请"
        }
        onCancel={() => !busy && setAction(null)}
        onOk={() => void submitAction()}
        confirmLoading={busy}
        okText={action?.kind === "APPROVE" ? "确认同意" : "确认拒绝"}
        okButtonProps={{ danger: action?.kind === "REJECT" }}
      >
        <Alert
          showIcon
          type={action?.kind === "APPROVE" ? "info" : "warning"}
          message={
            action?.kind === "APPROVE"
              ? "同意后进入飞书原有后续审批，尚未完成最终审批。"
              : "拒绝后飞书审批将结束；本单未报销票据的有效占用会释放。"
          }
        />
        <Form form={actionForm} layout="vertical" className="modal-form">
          <Form.Item
            name="comment"
            label="处理意见"
            rules={[
              {
                required: true,
                min: 2,
                max: 600,
                message: "请填写至少 2 个字的处理依据",
              },
            ]}
          >
            <Input.TextArea
              rows={4}
              maxLength={600}
              showCount
              placeholder="说明核对依据及处理结论"
            />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        open={!!correction}
        width={680}
        title="核对原件并确认票面字段"
        onCancel={() => !busy && setCorrection(null)}
        onOk={() => void submitCorrection()}
        confirmLoading={busy}
        okText={row?.ai_approval_enabled ? "确认字段并重新审核" : "保存字段"}
      >
        <Alert
          showIcon
          type="info"
          message="修改仅保存为人工核对记录，原件保持完整。确认后会重新执行主体、金额和跨单查重。"
        />
        <Form form={correctionForm} layout="vertical" className="modal-form">
          <div className="form-two-columns">
            {(
              [
                ["number", "发票号码"],
                ["code", "发票代码（传统票填写）"],
                ["buyer_name", "购买方名称"],
                ["buyer_tax_id", "购买方税号"],
                ["seller_name", "销售方名称"],
                ["issue_date", "开票日期"],
              ] as [keyof InvoiceFields, string][]
            ).map(([name, label]) => (
              <Form.Item
                key={name}
                name={name}
                label={label}
                dependencies={name === "code" ? ["number"] : undefined}
                rules={
                  name === "number"
                    ? [{ required: true, message: "请填写发票号码" }, {
                        validator: (_, value: string) => /^(?:[0-9]{20}|[0-9]{8})$/.test(String(value || "").replace(/\s/g, ""))
                          ? Promise.resolve()
                          : Promise.reject(new Error("数电发票号码须为 20 位数字，传统发票号码须为 8 位数字，请按原件核对")),
                      }]
                    : name === "code"
                      ? [({ getFieldValue }) => ({
                          validator: (_, value: string) => {
                            const number = String(getFieldValue("number") || "").replace(/\s/g, "");
                            const code = String(value || "").replace(/\s/g, "");
                            return number.length !== 8 || /^(?:[0-9]{10}|[0-9]{12})$/.test(code)
                              ? Promise.resolve()
                              : Promise.reject(new Error("传统发票须填写 10 位或 12 位数字的发票代码"));
                          },
                        })]
                    : name === "seller_name"
                    ? []
                    : [{ required: true, whitespace: true, message: `请填写${label}` }]
                }
              >
                <Input
                  placeholder={name === "issue_date" ? "YYYY-MM-DD" : label}
                />
              </Form.Item>
            ))}
          </div>
          <Form.Item
            name="amount"
            label="票面价税合计"
            rules={[{ required: true, message: "请核对金额" }]}
          >
            <InputNumber
              stringMode
              min="0.01"
              precision={2}
              style={{ width: "100%" }}
              prefix="¥"
            />
          </Form.Item>
          <Form.Item
            name="comment"
            label="人工确认依据"
            rules={[{ required: true, min: 2, message: "请填写核对依据" }]}
          >
            <Input.TextArea
              rows={2}
              placeholder="例如：已与原始 XML 核对，补全购买方税号"
            />
          </Form.Item>
        </Form>
      </Modal>
    </Drawer>
  );
}

function FolderOpenIcon() {
  return <FileTextOutlined />;
}
