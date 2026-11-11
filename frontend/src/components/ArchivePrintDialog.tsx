import { useEffect, useRef, useState } from "react";
import { Alert, Button, Checkbox, Modal, Progress, Space, Spin } from "antd";
import { DownloadOutlined, ExportOutlined, FileTextOutlined, PrinterOutlined } from "@ant-design/icons";
import { api, bytes, post } from "../api";
import type { ArchivedApprovalDetail, ArchivedFile, ArchivePrintInput, ArchivePrintJob } from "../archiveTypes";
import { XlsxPreviewButton } from "./XlsxPreview";
import { isXlsxFile } from "../xlsxPreview";

const originNames: Record<string, string> = {
  FORM: "申请表单", TIMELINE: "审批动态", COMMENT: "审批评论", LOCAL: "本地归档", PACKAGE: "包内文件",
};

/** 与后台打印范围一致：成功原包只分组，失败原包的残留成员不进入本次选择。 */
function selectableFiles(files: ArchivedFile[]) {
  const failedPackages = new Set(files.filter((file) => file.package_status === "FAILED").map((file) => file.id));
  return files.filter((file) => file.package_status !== "READY" &&
    !(file.archive_source && failedPackages.has(file.archive_source.attachment_id)));
}

/** 先选择当前附件，确认后才创建打印任务；审批单正文始终包含在打印稿中。 */
export function ArchivePrintDialog({ approvalIds, onClose }: {
  /** 本次所选审批 ID，按批量勾选顺序展示和合并，窗口打开期间保持不变。 */
  approvalIds: string[];
  /** 关闭并丢弃本次附件选择；下一次打开默认全选。 */
  onClose: () => void;
}) {
  const [details, setDetails] = useState<Record<string, ArchivedApprovalDetail>>({});
  const loadedDetails = useRef(new Set<string>());
  const [selected, setSelected] = useState<Record<string, string[]>>({});
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [attempt, setAttempt] = useState(0);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState("");
  const [initialJob, setInitialJob] = useState<ArchivePrintJob>();

  useEffect(() => {
    const controller = new AbortController();
    let index = 0;
    setLoading(true);
    setLoadError("");
    // 最多四个请求同时读取详情；重试只补未取得的单据，保留成功单据及用户的勾选。
    const worker = async () => {
      while (!controller.signal.aborted && index < approvalIds.length) {
        const id = approvalIds[index++];
        if (loadedDetails.current.has(id)) continue;
        try {
          const detail = await api<ArchivedApprovalDetail>(`/api/v1/archives/approvals/${id}`, { signal: controller.signal });
          if (controller.signal.aborted) return;
          if (!detail.printable) throw new Error(`${detail.serial_number} 当前不是最终通过状态，请重新选择审批单`);
          loadedDetails.current.add(id);
          setDetails((current) => ({ ...current, [id]: detail }));
          setSelected((current) => ({ ...current, [id]: selectableFiles(detail.files).map((file) => file.id) }));
        } catch (reason) {
          if (!controller.signal.aborted) setLoadError((current) => current || (reason as Error).message);
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(4, approvalIds.length) }, worker)).then(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [approvalIds, attempt]);

  const rows = approvalIds.flatMap((id) => details[id] ? [details[id]] : []);
  const selectedCount = rows.reduce((count, row) => count + (selected[row.id]?.length || 0), 0);
  const totalCount = rows.reduce((count, row) => count + selectableFiles(row.files).length, 0);
  const setAll = (checked: boolean) => setSelected(Object.fromEntries(rows.map((row) => [
    row.id, checked ? selectableFiles(row.files).map((file) => file.id) : [],
  ])));
  const selectFile = (approvalId: string, fileId: string, checked: boolean) => {
    setSelected((current) => ({ ...current, [approvalId]: checked
      ? [...(current[approvalId] || []), fileId]
      : (current[approvalId] || []).filter((id) => id !== fileId) }));
  };
  const submit = async () => {
    if (submitting || loading || loadError || rows.length !== approvalIds.length) return;
    setSubmitting(true);
    setSubmitError("");
    try {
      const input: ArchivePrintInput = { approval_ids: approvalIds, file_ids_by_approval: selected };
      setInitialJob(await post<ArchivePrintJob>("/api/v1/archives/print-jobs", input));
    } catch (reason) {
      setSubmitError((reason as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  if (initialJob) return <ArchivePrintPreview initialJob={initialJob} onClose={onClose} />;

  return <Modal open title="选择打印附件" width={980} onCancel={onClose} maskClosable={false}
    closable={!submitting} keyboard={!submitting}
    footer={<Space wrap>
      <Button disabled={submitting} onClick={onClose}>取消</Button>
      <Button type="primary" icon={<PrinterOutlined />} loading={submitting}
        disabled={loading || !!loadError || rows.length !== approvalIds.length}
        onClick={() => void submit()}>生成打印稿</Button>
    </Space>}>
    <p className="muted">审批单正文始终打印。取消勾选不需要的附件，取消全部附件时仅打印正文。</p>
    {loadError && <Alert type="error" showIcon message={loadError}
      action={<Button size="small" disabled={loading} onClick={() => setAttempt((current) => current + 1)}>重试加载</Button>} />}
    {submitError && <Alert type="error" showIcon message={submitError} />}
    <div className="archive-print-selection-toolbar">
      <Space wrap>
        <Button size="small" disabled={loading || submitting || !totalCount} onClick={() => setAll(true)}>全选</Button>
        <Button size="small" disabled={loading || submitting || !selectedCount} onClick={() => setAll(false)}>取消全选</Button>
        <span>已选 {selectedCount} / {totalCount} 个附件 · {approvalIds.length} 张审批</span>
      </Space>
      {loading && <Space><Spin size="small" /><span>正在加载 {rows.length} / {approvalIds.length} 单</span></Space>}
    </div>
    <div className="archive-print-selection">
      {rows.map((row) => {
        const files = selectableFiles(row.files);
        const fileIds = new Set(files.map((file) => file.id));
        const checkedIds = selected[row.id] || [];
        return <section className="archive-print-selection-group" key={row.id}>
          <div className="archive-print-selection-heading">
            <div><strong>{row.title}</strong><small>{row.serial_number} · {row.applicant}</small></div>
            <Checkbox checked={files.length > 0 && checkedIds.length === files.length}
              indeterminate={checkedIds.length > 0 && checkedIds.length < files.length}
              disabled={submitting || !files.length} aria-label={`${row.serial_number} 全选附件`}
              onChange={(event) => {
                const ids = event.target.checked ? files.map((file) => file.id) : [];
                setSelected((current) => ({ ...current, [row.id]: ids }));
              }}>附件 {checkedIds.length} / {files.length}</Checkbox>
          </div>
          {!files.length && <p className="muted">本单无可选择附件，仅打印审批单正文。</p>}
          <ul className="archive-print-files">
            {row.files.map((file) => {
              if (file.package_status === "READY") return <li className="archive-print-package" key={file.id}>
                <FileTextOutlined /><span>{file.filename}（已展开，请选择包内文件）</span>
              </li>;
              if (!fileIds.has(file.id)) return null;
              return <li className={file.archive_source ? "archive-print-member" : undefined} key={file.id}>
                <Checkbox checked={checkedIds.includes(file.id)} disabled={submitting}
                  aria-label={`打印附件 ${file.filename}`}
                  onChange={(event) => selectFile(row.id, file.id, event.target.checked)}>
                  <span className="archive-print-file-body">
                    <strong>{file.filename}</strong>
                    <small>{file.archive_source ? `${file.archive_source.filename} / ${file.archive_source.path}` :
                      originNames[file.origin] || file.origin}{file.size === null ? "" : ` · ${bytes(file.size)}`}</small>
                    {(file.reason || file.status === "MISSING" || file.package_status === "FAILED") &&
                      <small className="archive-file-warning">{file.reason || "原件暂不可用，勾选后会在打印稿中提示"}</small>}
                  </span>
                </Checkbox>
                {file.download_url && (isXlsxFile(file.filename) ?
                  <XlsxPreviewButton url={file.download_url} filename={file.filename} size={file.size} label="查看原件" /> :
                  <Button type="link" size="small" href={file.preview_url || file.download_url}
                    target="_blank" rel="noopener noreferrer">查看原件</Button>)}
              </li>;
            })}
          </ul>
        </section>;
      })}
    </div>
  </Modal>;
}

/** 等待同一批次的一份 PDF，完整稿自动尝试打印，部分稿先展示缺件。 */
function ArchivePrintPreview({ initialJob, onClose }: {
  initialJob: ArchivePrintJob;
  onClose: () => void;
}) {
  const [job, setJob] = useState<ArchivePrintJob>(initialJob);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const frame = useRef<HTMLIFrameElement>(null);
  const attempted = useRef(false);
  const timer = useRef<number | undefined>(undefined);
  const pdfUrl = job?.pdf_url;
  const hasIssues = Boolean(job?.issues.length);
  const pending = !error && job?.status !== "READY" && job?.status !== "FAILED";
  const inline = navigator.pdfViewerEnabled !== false;

  useEffect(() => {
    const controller = new AbortController();
    let poll: number | undefined;
    const accept = (next: ArchivePrintJob) => {
      if (controller.signal.aborted) return;
      setJob(next);
      if (next.status === "QUEUED" || next.status === "RUNNING") {
        poll = window.setTimeout(() => {
          void api<ArchivePrintJob>(`/api/v1/archives/print-jobs/${next.id}`, { signal: controller.signal })
            .then(accept, failed);
        }, 1000);
      }
    };
    const failed = (reason: unknown) => {
      if (!controller.signal.aborted) setError((reason as Error).message);
    };
    accept(initialJob);
    return () => {
      controller.abort();
      window.clearTimeout(poll);
    };
  }, [initialJob]);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const printFrame = () => {
    if (!frame.current?.contentWindow) return false;
    try {
      // 并发撤销或过期时接口可能返回 JSON，不能对错误页调起打印。
      if (frame.current.contentDocument?.contentType === "application/json") return false;
      frame.current.contentWindow.focus();
      frame.current.contentWindow.print();
      return true;
    } catch {
      // 原生 PDF 查看器受浏览器设置控制，始终提供打开和下载入口。
      return false;
    }
  };

  const loaded = () => {
    setReady(true);
    if (!hasIssues && !attempted.current) {
      attempted.current = true;
      timer.current = window.setTimeout(printFrame, 300);
    }
  };
  const failure = error || job?.error;
  return (
    <Modal
      open
      title="审批归档打印"
      onCancel={onClose}
      width={980}
      maskClosable={false}
      closable={!pending}
      keyboard={!pending}
      footer={<Space wrap>
        <Button onClick={onClose} disabled={pending}>关闭</Button>
        {pdfUrl && <>
          <Button icon={<ExportOutlined />} href={pdfUrl} target="_blank" rel="noreferrer">打开 PDF</Button>
          <Button icon={<DownloadOutlined />} href={pdfUrl + "?download=true"}>下载 PDF</Button>
          <Button type="primary" icon={<PrinterOutlined />} disabled={inline && !ready}
            onClick={() => { if (!printFrame()) window.open(pdfUrl, "_blank", "noopener,noreferrer"); }}>
            {hasIssues ? "打印可用内容" : "打印"}
          </Button>
        </>}
      </Space>}
    >
      {pending && <div className="archive-print-progress">
        <strong>{job?.status === "RUNNING" ? "正在转换原件并合并打印稿" : "正在准备打印任务"}</strong>
        <Progress percent={Math.round(100 * (job?.completed ?? 0) / (job?.total || 1))} status="active" />
        {job && <span>已处理 {job.completed} / {job.total} 单</span>}
      </div>}
      {failure && <Alert type="error" showIcon message={failure} />}
      {job && pdfUrl && <>
        {hasIssues ? <Alert type="warning" showIcon message="以下资料未能完整打印"
          description={<ul className="archive-print-issues">
            {job.issues.map((issue, index) => <li key={`${issue.approval_id}-${index}`}>
              <strong>{issue.serial_number} · {issue.filename}</strong>
              <span>{issue.reason}</span>
              {issue.download_url && <a href={issue.download_url}>下载原件</a>}
              {!issue.download_url && issue.feishu_url && <a href={issue.feishu_url} target="_blank" rel="noreferrer">查看飞书原单</a>}
            </li>)}
          </ul>} /> : <p className="muted">已合并 {job.total} 张审批及附件，共 {job.pages} 页。</p>}
        {inline && <iframe ref={frame} className="archive-print-frame" title="审批归档打印稿预览"
          src={pdfUrl} onLoad={loaded} />}
      </>}
    </Modal>
  );
}
