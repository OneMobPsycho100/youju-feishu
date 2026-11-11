import { useEffect, useRef, useState } from "react";
import { App, Button, Progress, Tooltip } from "antd";
import { CheckCircleOutlined, CloseOutlined, DownloadOutlined, FileTextOutlined, LoadingOutlined,
  PaperClipOutlined, ReloadOutlined, WarningOutlined } from "@ant-design/icons";
import { api, post } from "../api";
import { requestId } from "../requestId";
import type { AgentAttachment } from "../agentTypes";
import { isXlsxFile } from "../xlsxPreview";

const fileSize = (size: number) => size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KB` : `${(size / 1024 / 1024).toFixed(1)} MB`;
export const attachmentUrl = (id: string, preview = false) => `/api/v1/agent/attachments/${id}/download${preview ? "?preview=true" : ""}`;
type PreviewHandler = (value: { url: string; title: string; mime?: string }) => void;

/** 草稿和历史消息共用附件卡；已发送原件仅提供预览和下载，操作始终使用真实附件 ID。 */
export function AgentAttachmentCard({ value, preview, retry, remove, disabled }: {
  value: AgentAttachment; preview: PreviewHandler; retry?: () => void; remove?: () => void; disabled?: boolean;
}) {
  const processing = value.status === "QUEUED" || value.status === "PROCESSING";
  const canPreview = isXlsxFile(value.filename, value.mime) || ["application/pdf", "image/png", "image/jpeg", "image/webp"].includes(value.mime);
  return <div className={`agent-file-card ${value.status.toLowerCase()}`}>
    <div className="agent-file-symbol"><FileTextOutlined /></div>
    <div className="agent-file-info"><strong title={value.filename}>{value.filename}</strong>
      <span>{fileSize(value.size)} · {processing ? <><LoadingOutlined spin /> {value.status === "QUEUED" ? "等待解析" : "正在解析"}</>
        : value.status === "FAILED" ? <><WarningOutlined /> 解析失败</> : <><CheckCircleOutlined /> 已解析</>}</span>
      {value.error && <small className="agent-file-warning">{value.error}</small>}</div>
    <div className="agent-file-actions">
      {retry && (value.status === "FAILED" || value.error) && !processing && <Tooltip title="重试解析"><Button size="small" type="text" aria-label={`重试解析 ${value.filename}`} icon={<ReloadOutlined />} disabled={disabled} onClick={retry} /></Tooltip>}
      {canPreview && <Button size="small" type="link" onClick={() => preview({ url: attachmentUrl(value.id, true), title: value.filename, mime: value.mime })}>预览</Button>}
      <Tooltip title="下载原件"><Button type="text" size="small" icon={<DownloadOutlined />} aria-label={`下载 ${value.filename}`} href={attachmentUrl(value.id)} target="_blank" rel="noopener noreferrer" /></Tooltip>
      {remove && <Tooltip title="移除附件"><Button type="text" size="small" icon={<CloseOutlined />} aria-label={`移除 ${value.filename}`} disabled={disabled} onClick={remove} /></Tooltip>}
    </div>
  </div>;
}

type PendingUpload = { id: string; conversationId: string; file: File; percent: number; error: string };

/** XHR 仅用于上传进度；鉴权仍使用同源 HttpOnly Cookie，失败重试保留 upload_id。 */
function upload(conversationId: string, item: PendingUpload, progress: (percent: number) => void): Promise<AgentAttachment> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/agent/conversations/${conversationId}/attachments`);
    xhr.withCredentials = true;
    xhr.responseType = "json";
    xhr.timeout = 120000;
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) progress(Math.min(99, Math.round(event.loaded / event.total * 100))); };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300 && xhr.response?.id) resolve(xhr.response as AgentAttachment);
      else {
        if (xhr.status === 401) window.dispatchEvent(new Event("session-expired"));
        reject(new Error(typeof xhr.response?.detail === "string" ? xhr.response.detail : `附件上传失败（${xhr.status}）`));
      }
    };
    xhr.onerror = () => reject(new Error("上传连接中断，请点击重试"));
    xhr.ontimeout = () => reject(new Error("上传超时，请点击重试；已上传内容不会重复处理"));
    const form = new FormData();
    form.append("file", item.file);
    form.append("upload_id", item.id);
    xhr.send(form);
  });
}

/** 上传队列按会话隔离；切换历史对话不会把前一会话的文件带入新消息。 */
export function AgentAttachmentPicker({ conversationId, attachments, extensions, maxFiles, maxBytes, disabled,
  ensureConversation, onUploaded, onChanged, onRemoved, onBusyChange, preview }: {
  conversationId: string; attachments: AgentAttachment[]; extensions: string[]; maxFiles: number; maxBytes: number; disabled: boolean;
  ensureConversation: () => Promise<string>;
  onUploaded: (conversationId: string, value: AgentAttachment) => Promise<void>;
  onChanged: (value: AgentAttachment) => void; onRemoved: (id: string) => void;
  onBusyChange: (busy: boolean) => void; preview: PreviewHandler;
}) {
  const { message } = App.useApp();
  const input = useRef<HTMLInputElement>(null);
  const adding = useRef(false);
  const [reserving, setReserving] = useState(false);
  const [pending, setPending] = useState<PendingUpload[]>([]);
  const [dragging, setDragging] = useState(false);
  const [busyFiles, setBusyFiles] = useState<string[]>([]);
  const current = pending.filter((item) => item.conversationId === conversationId && !attachments.some((file) => file.upload_id === item.id));
  useEffect(() => { onBusyChange(reserving || current.length > 0 || busyFiles.length > 0); }, [reserving, current.length, busyFiles.length, onBusyChange]);

  const transfer = async (item: PendingUpload) => {
    setPending((values) => values.map((value) => value.id === item.id ? { ...value, error: "", percent: 0 } : value));
    try {
      const value = await upload(item.conversationId, item, (percent) => setPending((values) => values.map((file) => file.id === item.id ? { ...file, percent } : file)));
      await onUploaded(item.conversationId, value);
      setPending((values) => values.filter((file) => file.id !== item.id));
    } catch (reason) {
      setPending((values) => values.map((file) => file.id === item.id ? { ...file, error: (reason as Error).message } : file));
    }
  };
  const addFiles = async (files: File[]) => {
    if (disabled || adding.current || !files.length) return;
    const remaining = maxFiles - attachments.length - current.length;
    if (files.length > remaining) { message.warning(`每轮最多 ${maxFiles} 个附件，还能添加 ${Math.max(0, remaining)} 个`); return; }
    for (const file of files) {
      const suffix = "." + file.name.split(".").at(-1)?.toLowerCase();
      if (!extensions.includes(suffix)) { message.warning(`暂不支持 ${file.name} 的文件格式`); return; }
      if (!file.size || file.size > maxBytes) { message.warning(`${file.name} 为空或超过 ${Math.round(maxBytes / 1024 / 1024)}MB`); return; }
    }
    adding.current = true;
    setReserving(true);
    try {
      const id = await ensureConversation();
      const items = files.map((file) => ({ id: requestId(), conversationId: id, file, percent: 0, error: "" }));
      setPending((values) => [...values, ...items]);
      await Promise.all(items.map(transfer));
    } catch (reason) { message.error((reason as Error).message); }
    finally { adding.current = false; setReserving(false); }
  };
  const act = async (value: AgentAttachment, action: "retry" | "remove") => {
    setBusyFiles((values) => [...values, value.id]);
    try {
      if (action === "retry") onChanged(await post<AgentAttachment>(`/api/v1/agent/attachments/${value.id}/retry`));
      else { await api(`/api/v1/agent/attachments/${value.id}`, { method: "DELETE" }); onRemoved(value.id); }
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusyFiles((values) => values.filter((id) => id !== value.id)); }
  };
  return <div className={`agent-attachment-picker ${dragging ? "dragging" : ""}`}
    onDragOver={(event) => { if (event.dataTransfer.types.includes("Files")) { event.preventDefault(); setDragging(true); } }}
    onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDragging(false); }}
    onDrop={(event) => { event.preventDefault(); setDragging(false); void addFiles(Array.from(event.dataTransfer.files)); }}>
    <input type="file" ref={input} multiple hidden accept={extensions.join(",")} aria-label="选择 Agent 附件"
      onChange={(event) => { void addFiles(Array.from(event.target.files || [])); event.target.value = ""; }} />
    {(attachments.length > 0 || current.length > 0) && <div className="agent-draft-files">
      {attachments.map((value) => <AgentAttachmentCard key={value.id} value={value} preview={preview} disabled={disabled || busyFiles.includes(value.id)}
        retry={() => void act(value, "retry")} remove={() => void act(value, "remove")} />)}
      {current.map((item) => <div className="agent-file-card" key={item.id}><div className="agent-file-symbol"><FileTextOutlined /></div>
        <div className="agent-file-info"><strong>{item.file.name}</strong><span>{fileSize(item.file.size)} · {item.error || "正在上传"}</span>
          {!item.error && <Progress percent={item.percent} size="small" showInfo={false} />}</div>
        {item.error && <div className="agent-file-actions"><Button size="small" icon={<ReloadOutlined />} onClick={() => void transfer(item)}>重试</Button>
          <Button size="small" type="text" icon={<CloseOutlined />} aria-label={`移除上传 ${item.file.name}`} onClick={() => setPending((values) => values.filter((file) => file.id !== item.id))} /></div>}</div>)}
    </div>}
    <div className="agent-attachment-toolbar"><Button size="small" type="text" icon={<PaperClipOutlined />} loading={reserving}
      disabled={disabled || attachments.length + current.length >= maxFiles} onClick={() => input.current?.click()}>添加附件</Button>
      <small>{dragging ? "松开即可上传" : `可拖拽文件 · 最多 ${maxFiles} 个 · 每个 ${Math.round(maxBytes / 1024 / 1024)}MB`}</small></div>
  </div>;
}
