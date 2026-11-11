import { useEffect, useRef, useState } from "react";
import { Alert, Button, Empty, Modal, Pagination, Select, Space, Spin } from "antd";
import { DownloadOutlined } from "@ant-design/icons";
import { XLSX_MAX_BYTES, XLSX_MAX_COLUMNS, XLSX_MAX_ROWS, XLSX_PAGE_SIZE } from "../xlsxPreview";
import type { XlsxPreviewRequest, XlsxPreviewResponse, XlsxSheetPage } from "../xlsxPreview";

/** 原件读取沿用已有的同源鉴权下载接口，无需转换或上传到外部预览服务。 */
export interface XlsxPreviewProps {
  /** 同源原件下载地址，包括需要 Cookie 鉴权的归档和助手附件地址。 */
  url: string;
  /** 展示名称，用于表格的无障碍标签。 */
  filename: string;
  /** 已知的原件字节数，未知时在下载后校验。 */
  size?: number | null;
}

/** 浏览器 Worker 读取 XLSX；主线程只渲染当前页，切换文件和关闭窗口时释放内存。 */
export function XlsxPreview({ url, filename, size }: XlsxPreviewProps) {
  const [result, setResult] = useState<{ url: string; data: XlsxSheetPage }>();
  const [failure, setFailure] = useState<{ url: string; message: string }>();
  const [busy, setBusy] = useState(true);
  const [attempt, setAttempt] = useState(0);
  const changePage = useRef<((sheet: string, page: number) => void) | undefined>(undefined);
  const data = result?.url === url ? result.data : undefined;
  const error = failure?.url === url ? failure.message : "";

  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let sequence = 0;
    let timer: number | undefined;
    let worker: Worker | undefined;
    setResult(undefined);
    setFailure(undefined);
    setBusy(true);
    const failed = (message: string) => {
      if (disposed) return;
      window.clearTimeout(timer);
      controller.abort();
      worker?.terminate();
      changePage.current = undefined;
      setBusy(false);
      setFailure({ url, message });
    };
    const send = (message: XlsxPreviewRequest, transfer: Transferable[] = []) => {
      setBusy(true);
      window.clearTimeout(timer);
      timer = window.setTimeout(() => failed("读取时间过长，请重试或下载原件查看"), 30_000);
      worker?.postMessage(message, transfer);
    };
    const load = async () => {
      try {
        const address = new URL(url, window.location.href);
        if (address.origin !== window.location.origin || !address.pathname.startsWith("/api/"))
          throw new Error("原件地址不可用于预览");
        if (size && size > XLSX_MAX_BYTES) throw new Error("文件超过 30 MB，请下载原件查看");
        worker = new Worker(new URL("../workers/xlsxPreview.worker.ts", import.meta.url), { type: "module" });
        worker.onmessage = (event: MessageEvent<XlsxPreviewResponse>) => {
          if (disposed || event.data.id !== sequence) return;
          window.clearTimeout(timer);
          setBusy(false);
          if (event.data.type === "error") failed(event.data.message);
          else setResult({ url, data: event.data.data });
        };
        worker.onerror = () => failed("表格预览暂时无法加载，请重试或下载原件查看");
        changePage.current = (sheet, page) => send({ type: "page", id: ++sequence, sheet, page });
        timer = window.setTimeout(() => failed("原件读取超时，请重试或下载原件查看"), 30_000);
        const response = await fetch(address, { credentials: "same-origin", signal: controller.signal });
        if (!response.ok) {
          if (response.status === 401) window.dispatchEvent(new Event("session-expired"));
          const body = await response.json().catch(() => ({}));
          throw new Error(typeof body.detail === "string" ? body.detail : "原件暂时无法访问，请重试");
        }
        if (Number(response.headers.get("content-length")) > XLSX_MAX_BYTES) throw new Error("文件超过 30 MB，请下载原件查看");
        const buffer = await response.arrayBuffer();
        if (!disposed) send({ type: "open", id: ++sequence, buffer }, [buffer]);
      } catch (reason) {
        if (!controller.signal.aborted) failed((reason as Error).message);
      }
    };
    void load();
    return () => {
      disposed = true;
      controller.abort();
      window.clearTimeout(timer);
      worker?.terminate();
      changePage.current = undefined;
    };
  }, [url, size, attempt]);

  return <section className="xlsx-preview" aria-label={`${filename} 数据预览`}>
    {error ? <Alert type="warning" showIcon message={error}
      action={<Button size="small" onClick={() => setAttempt((value) => value + 1)}>重试</Button>}
      description={<a href={url}>下载原件</a>} /> : <>
      {data && <div className="xlsx-preview-toolbar">
        <Space wrap>
          <span>工作表</span>
          <Select aria-label="工作表" value={data.sheet} disabled={busy} style={{ minWidth: 180, maxWidth: "100%" }}
            options={data.sheets.map((name) => ({ value: name, label: name }))}
            onChange={(sheet) => changePage.current?.(sheet, 1)} />
          <span className="muted">{data.rowCount} 行 · {data.columns.length} 列</span>
        </Space>
        {data.rowCount > XLSX_PAGE_SIZE && <Pagination size="small" simple current={data.page} disabled={busy}
          pageSize={XLSX_PAGE_SIZE} total={data.rowCount} showSizeChanger={false}
          onChange={(page) => changePage.current?.(data.sheet, page)} />}
      </div>}
      {data?.truncated && <Alert type="info" showIcon className="xlsx-preview-notice" message={
        `原工作表有 ${data.originalRows} 行、${data.originalColumns} 列，预览最多读取前 ${XLSX_MAX_ROWS} 行、${XLSX_MAX_COLUMNS} 列，完整数据可下载原件查看。`} />}
      {(busy || !data) && <div className="xlsx-preview-loading"><Spin size="small" /><span>正在读取工作表</span></div>}
      {data && !data.rowCount && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前工作表没有数据" />}
      {data && data.rowCount > 0 && <div className="xlsx-preview-scroll" aria-busy={busy}>
        <table className="xlsx-preview-table" aria-label={`${filename} · ${data.sheet}`}>
          <thead><tr><th aria-label="行号" />{data.columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr></thead>
          <tbody>{data.rows.map((row, index) => <tr key={data.firstRow + index}>
            <th scope="row">{data.firstRow + index}</th>
            {row.map((value, column) => <td key={column} title={value}>{value}</td>)}
          </tr>)}</tbody>
        </table>
      </div>}
    </>}
  </section>;
}

/** 归档和打印选择共用的 XLSX 弹窗入口；打开前不下载原件或加载解析 Worker。 */
export function XlsxPreviewButton({ url, filename, size, label = "预览" }: XlsxPreviewProps & { label?: string }) {
  const [open, setOpen] = useState(false);
  return <>
    <Button type="link" size="small" onClick={() => setOpen(true)} aria-label={`${label} ${filename}`}>{label}</Button>
    {open && <Modal open title={filename} width={1100} onCancel={() => setOpen(false)}
      footer={<Space><Button icon={<DownloadOutlined />} href={url}>下载原件</Button>
        <Button onClick={() => setOpen(false)}>关闭</Button></Space>}>
      <XlsxPreview url={url} filename={filename} size={size} />
    </Modal>}
  </>;
}
