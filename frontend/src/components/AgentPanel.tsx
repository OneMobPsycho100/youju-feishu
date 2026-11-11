import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert, App, Button, Checkbox, Drawer, Empty, Input, Modal, Spin, Tag, Tooltip } from "antd";
import { ArrowUpOutlined, CheckOutlined, CloseOutlined, CopyOutlined, EditOutlined, ExpandOutlined,
  FileTextOutlined, HistoryOutlined, LoadingOutlined, PlusOutlined, RobotOutlined, ShrinkOutlined,
  StopOutlined, ThunderboltFilled, BarChartOutlined } from "@ant-design/icons";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { api, localTime, money, post, put } from "../api";
import { useWorkbench } from "../context";
import { XlsxPreview } from "./XlsxPreview";
import { isXlsxFile } from "../xlsxPreview";
import type { Page } from "../types";
import type { AgentAttachment, AgentCapabilities, AgentContext, AgentConversation, AgentConversationDetail, AgentEvent,
  AgentMessage, AgentProposal, AgentRun } from "../agentTypes";
import { AgentUsagePanel } from "./AgentUsagePanel";
import { AgentAttachmentCard, AgentAttachmentPicker, attachmentUrl } from "./AgentAttachments";
import { requestId } from "../requestId";

const record = (value: unknown): Record<string, unknown> => value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const str = (value: unknown) => typeof value === "string" || typeof value === "number" ? String(value) : "";
const records = (value: unknown) => Array.isArray(value) ? value.map(record) : [];
const utcDate = (value: string) => new Date(/[Zz]|[+]\d\d:\d\d$/.test(value) ? value : value + "Z");
const count = (value: number) => value.toLocaleString("zh-CN");
const names: Record<string, string> = { PASS: "预审通过", REVIEW: "需人工复核", BLOCK: "审核阻断", PENDING: "待处理",
  RUNNING: "执行中", SUCCEEDED: "已完成", FAILED: "失败", APPROVED: "已通过", REJECTED: "已拒绝",
  ARCHIVED: "已归档", QUARANTINED: "待确认", UNKNOWN: "结果未知，正在对账", REQUESTED: "已提交", EXECUTING: "回写中",
  RESERVED: "待审批占用", USED: "已使用", HISTORICAL: "历史台账占用", FROZEN: "冻结占用", RELEASED: "已释放" };

/** 只接受已知业务链接，模型 Markdown 和工具卡片均不能导航到脚本或任意站点。 */
function safeLink(value: unknown) {
  const href = str(value);
  if (/^#\/approvals\/[A-Za-z0-9_-]+$/.test(href)) return href;
  if (/^\/api\/v1\/(attachments\/[A-Za-z0-9_-]+\/download|invoices\/export|archives\/(?:download|approval-download))(\?|$)/.test(href)) return href;
  if (/^\/api\/v1\/agent\/attachments\/[a-f0-9]{32}\/download(?:\?preview=true)?$/.test(href)) return href;
  try {
    const url = new URL(href);
    if (url.protocol === "https:" && !url.username && !url.password && (url.hostname === "feishu.cn" || url.hostname.endsWith(".feishu.cn"))) return href;
  } catch { /* 非法 URL 作为普通文字呈现。 */ }
  return undefined;
}

function Markdown({ text }: { text: string }) {
  return <div className="agent-markdown"><ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml
    urlTransform={(url) => safeLink(url) || ""} components={{
      a: ({ href, children }) => safeLink(href) ? <a href={href} target={href?.startsWith("#") ? undefined : "_blank"} rel="noopener noreferrer">{children}</a> : <span>{children}</span>,
      img: ({ alt }) => <span>{alt || "图片"}</span>,
    }}>{text}</ReactMarkdown></div>;
}

type Preview = { url: string; title: string; mime?: string };

function OriginalPreview({ value, onClose }: { value?: Preview; onClose: () => void }) {
  const spreadsheet = Boolean(value && isXlsxFile(value.title, value.mime));
  const [file, setFile] = useState<{ url: string; mime: string; source: string }>();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    setFile(undefined);
    setError("");
    if (!value || !safeLink(value.url) || spreadsheet) return;
    const controller = new AbortController();
    let objectUrl = "";
    let disposed = false;
    setLoading(true);
    // 先检查受鉴权响应类型，XML/OFD 等不会因为打开预览而意外触发浏览器下载。
    void fetch(value.url, { credentials: "same-origin", signal: controller.signal }).then(async (response) => {
      if (!response.ok) throw new Error("原件暂时无法访问，请刷新单据后重试");
      const mime = (response.headers.get("content-type") || "").split(";")[0];
      if (mime !== "application/pdf" && !["image/png", "image/jpeg", "image/webp"].includes(mime)) {
        await response.body?.cancel();
        if (!disposed) setError("此原件格式请下载后核对。");
        return;
      }
      const blob = await response.blob();
      if (!disposed) { objectUrl = URL.createObjectURL(blob); setFile({ url: objectUrl, mime, source: value.url }); }
    }).catch((reason) => { if (!disposed) setError((reason as Error).message); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [value?.url, spreadsheet]);
  return <Modal open={Boolean(value)} title={value?.title} onCancel={onClose} footer={<Button href={safeLink(value?.url)} target="_blank">下载原件／在新窗口打开</Button>} width={1000} zIndex={1500}>
    {spreadsheet && value ? <XlsxPreview key={value.url} url={value.url} filename={value.title} /> : <>
    {loading && <div className="agent-loading"><Spin /><span>正在读取原件</span></div>}
    {error && <Alert type="info" showIcon message={error} />}
    {file && file.source === value?.url && (file.mime === "application/pdf"
      ? <iframe className="agent-original-preview" title="业务原件预览" src={file.url} sandbox="allow-same-origin allow-scripts allow-downloads" referrerPolicy="no-referrer" />
      : <img className="agent-original-preview agent-original-image" alt="票据原件" src={file?.url} />)}</>}
  </Modal>;
}

function ResultCards({ result, preview }: { result: Record<string, unknown>; preview: (value: Preview) => void }) {
  const { navigate } = useWorkbench();
  const entity = str(result.entity);
  const openApproval = (id: unknown) => { if (/^[A-Za-z0-9_-]+$/.test(str(id))) navigate(`approvals/${str(id)}`); };
  if (result.error) return <Alert type="warning" showIcon message={str(result.error)} />;
  if (entity === "proposal") return null;
  if (entity === "attachment") {
    const file = record(result.attachment);
    return <div className="agent-business-card"><div className="agent-result-label">附件原文 · {str(file.filename)}</div>
      {records(result.segments).slice(0, 2).map((part, index) => <p className="agent-file-excerpt" key={index}><small>{str(part.location)}</small>{str(part.text).slice(0, 260)}{str(part.text).length > 260 && "…"}</p>)}
      <small>正文共 {str(result.total_characters)} 字{result.next_offset !== null && " · 本次为部分原文，可继续读取"}</small>
      <Button size="small" type="link" onClick={() => preview({ url: attachmentUrl(str(file.id), true), title: str(file.filename) })}>查看原件</Button>
    </div>;
  }
  if (entity === "invoice_usage") {
    const file = record(result.attachment);
    const status = str(result.status);
    return <div className="agent-business-card"><div className="agent-result-label">发票使用查询 · 全部公司</div>
      <div className="agent-result-heading"><strong>{str(result.number || file.filename)}</strong>
        <Tag color={status === "NOT_FOUND" || status === "REGISTERED" || status === "RELEASED" ? "green" : status === "UNKNOWN" ? "gold" : "red"}>{status === "UNKNOWN" ? "无法判定" : names[status] || "无使用记录"}</Tag></div>
      <p>{str(result.summary)}</p>
      {str(result.code) && <small>发票代码：{str(result.code)}</small>}
      {records(result.items).map((item) => <div className="agent-result-item" key={str(item.id)}>
        <div className="agent-result-heading"><strong>{str(item.serial_number || item.source_label)}</strong><Tag>{names[str(item.status)] || str(item.status)}</Tag></div>
        <div className="agent-result-meta">{str(item.company_name)} · {str(item.title)}{item.approval_status ? ` · ${names[str(item.approval_status)] || str(item.approval_status)}` : ""}</div>
        {str(item.approval_id) && <Button size="small" type="link" onClick={() => openApproval(item.approval_id)}>查看关联审批 →</Button>}
      </div>)}
      <small>{str(result.matched_by)} · 查询于 {localTime(str(result.queried_at))}</small>
      <Button size="small" type="link" onClick={() => preview({ url: attachmentUrl(str(file.id), true), title: str(file.filename) })}>核对上传原件</Button>
    </div>;
  }
  if (entity === "overview") return <div className="agent-business-card"><div className="agent-result-label">财务概况 · 实时查询</div>
    <div className="agent-metrics">{[["待处理", result.pending_count], ["需复核", result.review_count], ["预审通过", result.pass_count], ["已归档原件", result.archived_count]].map(([label, value]) =>
      <div key={str(label)}><strong>{str(value)}</strong><span>{str(label)}</span></div>)}</div>
    <small>累计报销金额 ¥{money(str(result.approval_amount))} · {str(result.approval_count)} 张审批</small></div>;
  if (entity === "downloads") return <div className="agent-business-card"><div className="agent-result-label">下载文件</div>
    {records(result.links).map((link) => <div className="agent-result-row" key={str(link.url)}>
      <FileTextOutlined /><a href={safeLink(link.url)} target="_blank" rel="noopener noreferrer">{str(link.label)}</a>
      {safeLink(link.preview_url) && <Button size="small" onClick={() => preview({ url: str(link.preview_url), title: str(link.label) })}>预览</Button>}
    </div>)}</div>;
  if (entity === "approval") {
    const audit = record(result.audit);
    return <div className="agent-business-card"><div className="agent-result-label">审批证据</div>
      <div className="agent-result-heading"><strong>{str(result.title)}</strong><Tag>{names[str(result.decision)] || str(result.decision)}</Tag></div>
      <p>{str(result.serial_number)} · ¥{money(str(result.amount))} · {str(result.applicant)}</p>
      <ul className="agent-evidence">{records(audit.evidence).filter((item) => !item.passed && item.severity !== "INFO").slice(0, 6).map((item, index) =>
        <li key={index}><b>{str(item.title)}</b><span>{str(item.detail)}</span></li>)}</ul>
      <Button size="small" onClick={() => openApproval(result.id)}>打开审批详情</Button>
      {records(result.attachments).filter((item) => item.active).slice(0, 5).map((item) => <Button size="small" type="link" key={str(item.id)}
        onClick={() => preview({ url: `/api/v1/attachments/${str(item.id)}/download?preview=true`, title: str(item.filename), mime: str(item.mime) })}>原件：{str(item.filename)}</Button>)}
    </div>;
  }
  const items = entity === "job" ? [result] : records(result.items);
  if (!items.length) return <div className="agent-query-empty">本次查询未找到匹配记录。</div>;
  return <div className="agent-business-card"><div className="agent-result-label">{{ approvals: "审批记录", archived_approvals: "审批归档", invoices: "发票与占用", archives: "归档原件", jobs: "后台任务", job: "任务状态", companies: "公司主体" }[entity] || "查询结果"}
    {result.total !== undefined && <span>共 {str(result.total)} 条 · 显示 {items.length} 条</span>}</div>
    <div className="agent-result-list">{items.map((item, index) => <div className="agent-result-item" key={str(item.id) || index}>
      <div className="agent-result-heading"><strong>{str(item.title || item.number || item.archive_path || item.name || `${str(item.kind)} · ${str(item.id).slice(0, 10)}`)}</strong>
        {(item.amount !== undefined) && <b className="agent-amount">¥{money(str(item.amount))}</b>}</div>
      <div className="agent-result-meta"><span>{str(item.serial_number || item.buyer_name || item.approval_id || item.tax_id)}</span>
        {Boolean(item.status || item.decision) && <Tag>{names[str(item.operation_status || item.decision || item.status)] || str(item.status)}</Tag>}</div>
      {str(record(item.claim).source_label) && <small>当前占用：{str(record(item.claim).source_label)} · {str(record(item.claim).status)}</small>}
      {Boolean(str(item.error)) && <p className="agent-error-text">{str(item.error)}</p>}
      {(entity === "approvals" || entity === "archived_approvals" || Boolean(item.approval_id)) &&
        <Button size="small" type="link" onClick={() => openApproval(item.approval_id || item.id)}>查看单据 →</Button>}
      {str(item.attachment_id) && <Button size="small" type="link" onClick={() => preview({ url: `/api/v1/attachments/${str(item.attachment_id)}/download?preview=true`, title: str(item.number || item.archive_path || "发票原件") })}>查看原件</Button>}
    </div>)}</div></div>;
}

const fieldNames: Record<string, string> = { number: "发票号码", code: "发票代码", buyer_name: "购买方名称", buyer_tax_id: "购买方税号", seller_name: "销售方名称", issue_date: "开票日期", amount: "价税合计" };

function ProposalCard({ proposal, busy, onConfirm, onCancel, onAdjust, preview }: {
  proposal: AgentProposal; busy: boolean; onConfirm: (id: string, checked: boolean) => void; onCancel: (id: string) => void;
  onAdjust: (text: string) => void; preview: (value: Preview) => void;
}) {
  const [checked, setChecked] = useState(false);
  const d = proposal.display;
  const expired = proposal.status === "EXPIRED" || (proposal.status === "PENDING" && utcDate(proposal.expires_at).getTime() <= Date.now());
  const pending = proposal.status === "PENDING" && !expired;
  const label = proposal.job ? names[proposal.operation_status || proposal.job.status] || proposal.job.status
    : expired ? "方案已过期" : { PENDING: "等待你确认", CANCELED: "已取消", INVALID: "方案已失效", EXPIRED: "方案已过期", SUBMITTED: "已提交" }[proposal.status];
  return <section className={`agent-proposal ${pending ? "awaiting" : ""}`} aria-label={`${str(d.action)}确认方案`}>
    <div className="agent-proposal-header"><span><ThunderboltFilled /> {str(d.action)}</span><Tag color={pending ? "gold" : proposal.job?.status === "SUCCEEDED" ? "green" : "default"}>{label}</Tag></div>
    <strong>{str(d.title)}</strong><div className="agent-result-meta">{str(d.serial_number)}{d.amount !== undefined && ` · ¥${money(str(d.amount))}`}{d.expected_version !== undefined && ` · 版本 ${str(d.expected_version)}`}</div>
    <p>{str(d.description)}</p>
    {str(d.comment) && <div className="agent-proposal-comment"><span>处理依据</span>{str(d.comment)}</div>}
    {str(d.warning) && <Alert type="warning" showIcon message={str(d.warning)} />}
    {proposal.kind === "CORRECT" && <><div className="agent-field-diff"><table><thead><tr><th>票面字段</th><th>当前记录</th><th>确认后</th></tr></thead>
      <tbody>{Object.entries(record(d.after)).map(([key, value]) => <tr key={key} className={str(record(d.before)[key]) !== str(value) ? "changed" : ""}>
        <th>{fieldNames[key] || key}</th><td>{str(record(d.before)[key]) || "未提取"}</td><td>{str(value) || "—"}</td></tr>)}</tbody></table></div>
      <Button size="small" onClick={() => preview({ url: str(d.preview_url), title: "核对票面原件", mime: str(d.preview_mime) })}>预览原件</Button>
      {pending && <Checkbox checked={checked} onChange={(event) => setChecked(event.target.checked)}>我已核对原件，确认以上完整票面字段与依据</Checkbox>}</>}
    {(proposal.error || proposal.job?.error) && <div className="agent-error-text">{proposal.error || proposal.job?.error}</div>}
    {proposal.job && <div className="agent-job-foot">任务 {proposal.job.id.slice(0, 12)} · 已尝试 {proposal.job.attempts} 次
      {proposal.operation_status === "UNKNOWN" && <p>回写结果尚不确定，后台将先核对飞书状态。</p>}</div>}
    {pending && <><div className="agent-proposal-actions"><Button type="primary" icon={<CheckOutlined />} loading={busy}
      disabled={proposal.kind === "CORRECT" && !checked} onClick={() => onConfirm(proposal.id, checked)}>确认{str(d.action)}</Button>
      <Button disabled={busy} onClick={() => onCancel(proposal.id)}>取消</Button>
      <Button type="text" disabled={busy} onClick={() => onAdjust(`请调整“${str(d.title)}”的${str(d.action)}方案：`)}>调整方案</Button></div>
      <small>确认前会重新校验权限和业务版本 · 有效至 {localTime(proposal.expires_at)}</small></>}
  </section>;
}

function UsageChip({ run }: { run: AgentRun }) {
  const input = run.usage.inputTokens;
  const output = run.usage.outputTokens;
  if (input === undefined && output === undefined) return <span className="agent-usage">Token 用量未返回</span>;
  return <Tooltip title={`完整输入 ${count(input || 0)} · 输出 ${count(output || 0)}${run.usage.uncachedInputTokens !== undefined ? ` · 未缓存输入 ${count(run.usage.uncachedInputTokens)}` : ""}${run.usage.cacheReadTokens !== undefined ? ` · 缓存读取 ${count(run.usage.cacheReadTokens)}` : ""}${run.usage.reasoningTokens !== undefined ? ` · 推理 ${count(run.usage.reasoningTokens)}（已含在输出）` : ""}${run.usage.reportedCalls !== undefined ? ` · ${run.usage.reportedCalls} 次已计量模型请求` : ""}`}>
    <span className="agent-usage">↑ {input === undefined ? "—" : count(input)} &nbsp; ↓ {output === undefined ? "—" : count(output)} tokens
      {input && run.usage.cacheReadTokens !== undefined ? ` · 缓存 ${(run.usage.cacheReadTokens / input * 100).toFixed(0)}%` : ""}</span>
  </Tooltip>;
}

export function AgentPanel({ open, onClose, context, onBusyChange }: {
  open: boolean; onClose: () => void; context: AgentContext; onBusyChange: (busy: boolean) => void;
}) {
  const { user, companies, tick, navigate, refresh } = useWorkbench();
  const { message } = App.useApp();
  const [capabilities, setCapabilities] = useState<AgentCapabilities>();
  const [history, setHistory] = useState<AgentConversation[]>([]);
  const [historyPage, setHistoryPage] = useState(1);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [selectedId, setSelectedId] = useState(() => sessionStorage.getItem(`youju-agent:${user.id}`) || "");
  const [detail, setDetail] = useState<AgentConversationDetail>();
  const [full, setFull] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [busyProposal, setBusyProposal] = useState("");
  const [connection, setConnection] = useState("正在连接");
  const [error, setError] = useState("");
  const [preview, setPreview] = useState<Preview>();
  const [rename, setRename] = useState<AgentConversation>();
  const [renameText, setRenameText] = useState("");
  const [usageOpen, setUsageOpen] = useState(false);
  const [removed, setRemoved] = useState<Record<string, boolean>>({});
  const scroller = useRef<HTMLDivElement>(null);
  const textarea = useRef<React.ComponentRef<typeof Input.TextArea>>(null);
  const stickToBottom = useRef(true);
  const streamEpoch = useRef(0);
  const pendingRequest = useRef<{ key: string; baseKey: string; requestId: string; attachmentIds: string[] } | null>(null);
  const selectedRef = useRef(selectedId);
  selectedRef.current = selectedId;
  const creatingConversation = useRef<Promise<string> | null>(null);
  const [, setClock] = useState(Date.now());
  const view = detail?.id === selectedId ? detail : undefined;
  const draftAttachments = view?.attachments.filter((item) => !item.submitted) || [];
  const attachmentsReady = draftAttachments.every((item) => item.status === "READY") && !attachmentBusy;
  const activeRun = view?.runs.find((run) => run.status === "RUNNING");
  const lastRun = view?.runs.at(-1);
  const otherConversation = capabilities?.active_conversation_id && capabilities.active_conversation_id !== selectedId ? capabilities.active_conversation_id : "";
  const loadHistory = useCallback(async (page = 1) => {
    const data = await api<Page<AgentConversation>>(`/api/v1/agent/conversations?page=${page}`);
    setHistory((current) => page === 1 ? data.items : [...current, ...data.items]);
    setHistoryPage(page);
    setHistoryTotal(data.total);
  }, []);

  useEffect(() => { void api<AgentCapabilities>("/api/v1/agent/capabilities").then(setCapabilities).catch(() => {}); }, [tick]);
  useEffect(() => { if (open) void loadHistory().catch((reason) => setError((reason as Error).message)); }, [open, loadHistory]);
  useEffect(() => { const timer = window.setInterval(() => setClock(Date.now()), 30000); return () => clearInterval(timer); }, []);
  useEffect(() => { setRemoved({}); }, [context.route, context.company_id, JSON.stringify(context.selected_ids), JSON.stringify(context.filters)]);
  useEffect(() => { onBusyChange(Boolean(activeRun || capabilities?.active_run_id)); }, [activeRun?.id, capabilities?.active_run_id, onBusyChange]);

  // 快照携带已提交游标，随后从这个位置订阅，消除读取与订阅间的消息丢失窗口。
  // 关闭抽屉不终止订阅或服务端运行，顶部入口仍能反映真实完成状态。
  useEffect(() => {
    const epoch = ++streamEpoch.current;
    let stream: EventSource | undefined;
    let disposed = false;
    if (!selectedId) { setDetail(undefined); return; }
    setError("");
    setConnection("正在连接");
    sessionStorage.setItem(`youju-agent:${user.id}`, selectedId);
    void api<AgentConversationDetail>(`/api/v1/agent/conversations/${selectedId}`).then((data) => {
      if (disposed || streamEpoch.current !== epoch) return;
      setDetail(data);
      stickToBottom.current = true;
      stream = new EventSource(`/api/v1/agent/conversations/${selectedId}/events?after=${data.event_seq}`);
      stream.onopen = () => setConnection("实时连接");
      stream.onerror = () => setConnection("正在重连，任务继续执行");
      stream.addEventListener("session-ended", () => { stream?.close(); setError("会话已失效，请重新登录"); });
      stream.addEventListener("actions", (event) => {
        if (!disposed) setDetail((current) => current?.id === selectedId ? { ...current, proposals: JSON.parse((event as MessageEvent).data) as AgentProposal[] } : current);
      });
      stream.addEventListener("update", (event) => {
        if (disposed) return;
        const next = JSON.parse((event as MessageEvent).data) as AgentEvent;
        if (next.data.run) {
          const run = next.data.run;
          setCapabilities((current) => current ? { ...current, active_conversation_id: run.status === "RUNNING" ? run.conversation_id : current.active_run_id === run.id ? "" : current.active_conversation_id,
            active_run_id: run.status === "RUNNING" ? run.id : current.active_run_id === run.id ? "" : current.active_run_id } : current);
        }
        setDetail((current) => {
          if (!current || current.id !== selectedId || next.seq <= current.event_seq) return current;
          const result = { ...current, event_seq: next.seq };
          if (next.data.message) {
            const item = next.data.message;
            result.messages = current.messages.some((entry) => entry.id === item.id) ? current.messages.map((entry) => entry.id === item.id ? item : entry) : [...current.messages, item];
          }
          if (next.data.run) {
            const item = next.data.run;
            result.runs = current.runs.some((entry) => entry.id === item.id) ? current.runs.map((entry) => entry.id === item.id ? item : entry) : [...current.runs, item];
          }
          if (next.data.proposal) {
            const item = next.data.proposal;
            result.proposals = current.proposals.some((entry) => entry.id === item.id) ? current.proposals.map((entry) => entry.id === item.id ? item : entry) : [...current.proposals, item];
          }
          if (next.data.attachment) {
            const item = next.data.attachment;
            result.attachments = current.attachments.some((entry) => entry.id === item.id) ? current.attachments.map((entry) => entry.id === item.id ? item : entry) : [...current.attachments, item];
          }
          if (next.kind === "attachment.deleted") result.attachments = current.attachments.filter((item) => item.id !== next.data.attachment_id);
          if (next.data.attachment_usage) result.attachment_usage = next.data.attachment_usage;
          return result;
        });
        if (next.data.run && next.data.run.status !== "RUNNING") { void loadHistory().catch(() => {}); refresh(); }
      });
    }).catch((reason) => { if (!disposed) { setError((reason as Error).message); setConnection("连接失败"); } });
    return () => { disposed = true; stream?.close(); };
  }, [selectedId, user.id, loadHistory, refresh]);

  useEffect(() => {
    if (open && stickToBottom.current) requestAnimationFrame(() => scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: "instant" }));
  }, [open, view?.messages, view?.proposals, activeRun?.stage]);

  const messageContext = useMemo<AgentContext>(() => ({
    route: context.route.split("/")[0], company_id: removed.company ? "" : context.company_id,
    approval_id: removed.approval ? "" : context.approval_id,
    selection_type: removed.selection ? "" : context.selection_type,
    selected_ids: removed.selection ? [] : context.selected_ids.slice(0, 20), filters: removed.filters ? {} : context.filters,
  }), [context, removed]);

  const ensureConversation = useCallback(async () => {
    if (selectedRef.current) return selectedRef.current;
    if (!creatingConversation.current) creatingConversation.current = post<AgentConversation>("/api/v1/agent/conversations", { title: "新对话" })
      .then((value) => { selectedRef.current = value.id; setSelectedId(value.id); return value.id; })
      .finally(() => { creatingConversation.current = null; });
    return creatingConversation.current;
  }, []);
  const changeAttachment = useCallback((value: AgentAttachment) => setDetail((current) => current?.id === value.conversation_id ? { ...current,
    attachments: current.attachments.some((item) => item.id === value.id) ? current.attachments.map((item) => item.id === value.id ? value : item) : [...current.attachments, value] } : current), []);
  const uploadedAttachment = useCallback(async (id: string, value: AgentAttachment) => {
    changeAttachment(value);
    const snapshot = await api<AgentConversationDetail>(`/api/v1/agent/conversations/${id}`);
    if (selectedRef.current === id) setDetail((current) => current?.id === id && current.event_seq > snapshot.event_seq ? current : snapshot);
    void loadHistory();
  }, [changeAttachment, loadHistory]);
  const removeAttachment = useCallback((id: string) => setDetail((current) => current ? { ...current, attachments: current.attachments.filter((item) => item.id !== id) } : current), []);

  const sendText = async (text: string, frozenContext = messageContext, frozenAttachmentIds?: string[]) => {
    if (sending || activeRun || otherConversation || !attachmentsReady && frozenAttachmentIds === undefined) return;
    let attachmentIds = frozenAttachmentIds ?? draftAttachments.map((item) => item.id);
    let content = text.trim() || (attachmentIds.length ? "请分析附件；如为发票，请检查是否已被使用并说明依据" : "");
    const baseKey = JSON.stringify([selectedId, content, frozenContext]);
    // 请求结果未知时保留原附件集合，即使 SSE 已把文件标记为已发送，也复用同一消息标识。
    if (!attachmentIds.length && pendingRequest.current?.baseKey === baseKey) attachmentIds = pendingRequest.current.attachmentIds;
    if (!content && !attachmentIds.length) return;
    setSending(true);
    setError("");
    let accepted = false;
    try {
      const id = await ensureConversation();
      content = content || "请分析附件；如为发票，请检查是否已被使用并说明依据";
      const key = JSON.stringify([id, content, frozenContext, attachmentIds]);
      if (pendingRequest.current?.key !== key) pendingRequest.current = { key, baseKey: JSON.stringify([id, content, frozenContext]), requestId: requestId(), attachmentIds };
      const startedRun = await post<AgentRun>(`/api/v1/agent/conversations/${id}/messages`, { text: content, context: frozenContext, attachment_ids: attachmentIds, request_id: pendingRequest.current.requestId });
      setCapabilities((current) => current ? { ...current, active_conversation_id: startedRun.conversation_id, active_run_id: startedRun.id } : current);
      accepted = true;
      pendingRequest.current = null;
      setDraft("");
      stickToBottom.current = true;
      const data = await api<AgentConversationDetail>(`/api/v1/agent/conversations/${id}`);
      setDetail((current) => current?.id === id && current.event_seq > data.event_seq ? current : data);
      void loadHistory();
    } catch (reason) { setError(accepted ? "消息已经提交，正在恢复连接；无需重复发送。" : (reason as Error).message); if (!accepted) setDraft(content); }
    finally { setSending(false); }
  };

  const updateProposal = (proposal: AgentProposal) => setDetail((current) => current ? { ...current,
    proposals: current.proposals.map((item) => item.id === proposal.id ? proposal : item) } : current);
  const act = async (id: string, kind: "confirm" | "cancel", checked = false) => {
    setBusyProposal(id);
    try {
      const proposal = await post<AgentProposal>(`/api/v1/agent/actions/${id}/${kind}`, kind === "confirm" ? { original_checked: checked } : undefined);
      updateProposal(proposal);
      if (kind === "confirm") { message.success("已提交业务任务，执行结果将在卡片中更新"); refresh(); }
    } catch (reason) {
      message.error((reason as Error).message);
      if (selectedId) void api<AgentConversationDetail>(`/api/v1/agent/conversations/${selectedId}`).then(setDetail).catch(() => {});
    } finally { setBusyProposal(""); }
  };
  const showPreview = (value: Preview) => { if (safeLink(value.url)) setPreview(value); };
  const adjust = (text: string) => { setDraft(text); textarea.current?.focus(); };
  const proposalCard = (proposal: AgentProposal) => <ProposalCard key={proposal.id} proposal={proposal} busy={Boolean(busyProposal)}
    onConfirm={(id, checked) => void act(id, "confirm", checked)} onCancel={(id) => void act(id, "cancel")} onAdjust={adjust} preview={showPreview} />;
  const linkedProposals = new Set((view?.messages || []).map((item) => str(record(record(item.data.result).proposal).id)).filter(Boolean));
  const lastAssistant = new Map<string, string>();
  for (const item of view?.messages || []) if (item.role === "assistant" && item.run_id && item.content) lastAssistant.set(item.run_id, item.id);

  const renderMessage = (item: AgentMessage) => {
    if (item.role === "tool") {
      const result = record(item.data.result);
      const proposal = view?.proposals.find((entry) => entry.id === str(record(result.proposal).id));
      return <div className="agent-tool-block" key={item.id}><details className="agent-tool-details"><summary>
        {item.data.status === "RUNNING" ? <LoadingOutlined spin /> : item.data.status === "FAILED" ? <CloseOutlined /> : <CheckOutlined />}
        <span>{item.content}{str(record(item.data.input).entity) && ` · ${str(record(item.data.input).entity)}`}</span>
        <small>{item.data.status === "RUNNING" ? "处理中" : item.data.status === "FAILED" ? "未完成" : "查看过程"}</small></summary>
        <div><b>调用参数</b><pre>{JSON.stringify(item.data.input, null, 2)}</pre>{item.data.result !== undefined && <><b>返回结果</b><pre>{JSON.stringify(item.data.result, null, 2)}</pre></>}</div>
      </details>{item.data.result !== undefined && <ResultCards result={result} preview={showPreview} />}{proposal && proposalCard(proposal)}</div>;
    }
    if (item.role === "assistant" && !item.content) return null;
    const run = view?.runs.find((entry) => entry.id === item.run_id);
    return <article key={item.id} className={`agent-message ${item.role}`}>
      {item.role === "assistant" && <div className="agent-message-avatar"><RobotOutlined /></div>}
      <div className="agent-message-main">{item.role === "user" ? <><div className="agent-user-text">{item.content}</div>
        {Array.isArray(item.data.attachment_ids) && <div className="agent-message-files">{item.data.attachment_ids.map((id) => view?.attachments.find((file) => file.id === id)).filter((file): file is AgentAttachment => Boolean(file))
          .map((file) => <AgentAttachmentCard key={file.id} value={file} preview={showPreview} />)}</div>}
        <div className="agent-user-context">{str(record(item.data.context).approval_id) && <span>附有审批上下文</span>}
          {Array.isArray(record(item.data.context).selected_ids) && (record(item.data.context).selected_ids as unknown[]).length > 0 && <span>附有选中记录</span>}</div></>
        : <><Markdown text={item.content} /><div className="agent-message-foot"><Tooltip title="复制回复"><Button size="small" type="text" icon={<CopyOutlined />} aria-label="复制回复"
          onClick={() => void navigator.clipboard.writeText(item.content).then(() => message.success("已复制")).catch(() => message.error("浏览器未允许复制"))} /></Tooltip>
          {run && lastAssistant.get(run.id) === item.id && run.status !== "RUNNING" && <UsageChip run={run} />}
          {Boolean(item.data.interrupted) && <span>本段回复未完整完成</span>}</div></>}
      </div>
    </article>;
  };

  return <><Drawer open={open} onClose={onClose} rootClassName={`agent-drawer ${full ? "agent-expanded" : ""}`}
    width={full ? "min(1280px, 100vw)" : "min(680px, 100vw)"} closeIcon={false} mask={full} zIndex={1250}
    title={<div className="agent-panel-title"><span className="agent-title-mark"><RobotOutlined /></span><strong>业务助手</strong></div>}
    extra={<div className="agent-header-actions"><Tooltip title="Token 用量统计"><Button type="text" icon={<BarChartOutlined />} aria-label="Token 用量统计" onClick={() => setUsageOpen(true)} /></Tooltip><Tooltip title="历史会话"><Button type="text" icon={<HistoryOutlined />} aria-label="历史会话" onClick={() => setHistoryOpen((value) => !value)} /></Tooltip>
      <Tooltip title="新对话"><Button type="text" icon={<PlusOutlined />} aria-label="新对话" disabled={sending || attachmentBusy} onClick={() => { selectedRef.current = ""; setSelectedId(""); setDetail(undefined); setDraft(""); pendingRequest.current = null; sessionStorage.removeItem(`youju-agent:${user.id}`); }} /></Tooltip>
      <Tooltip title={full ? "收起大窗口" : "展开大窗口"}><Button type="text" icon={full ? <ShrinkOutlined /> : <ExpandOutlined />} aria-label="切换大窗口" onClick={() => { setFull((value) => !value); if (!full) setHistoryOpen(true); }} /></Tooltip>
      <Button type="text" icon={<CloseOutlined />} aria-label="关闭业务助手" onClick={onClose} /></div>}>
    <div className="agent-panel-layout">
      {historyOpen && <aside className="agent-history"><div className="agent-history-heading">历史会话<Button size="small" type="text" icon={<CloseOutlined />} aria-label="收起历史" onClick={() => setHistoryOpen(false)} /></div>
        <Button block icon={<PlusOutlined />} disabled={sending || attachmentBusy} onClick={() => { selectedRef.current = ""; setSelectedId(""); setDetail(undefined); setDraft(""); pendingRequest.current = null; sessionStorage.removeItem(`youju-agent:${user.id}`); }}>新建对话</Button>
        <div className="agent-history-list">{history.map((item) => <div className={`agent-history-item ${selectedId === item.id ? "selected" : ""}`} key={item.id}>
          <button disabled={sending || attachmentBusy} onClick={() => { selectedRef.current = item.id; setSelectedId(item.id); if (window.innerWidth < 760) setHistoryOpen(false); }}><span>{item.title}</span><small>{localTime(item.updated_at)}</small></button>
          <Tooltip title="重命名"><Button type="text" size="small" icon={<EditOutlined />} aria-label={`重命名 ${item.title}`} onClick={() => { setRename(item); setRenameText(item.title); }} /></Tooltip>
        </div>)}{!history.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="暂无历史对话" />}
          {history.length < historyTotal && <Button type="text" block onClick={() => void loadHistory(historyPage + 1)}>加载更多会话</Button>}</div>
        <small className="agent-private-note">会话内容仅当前账号可见</small></aside>}
      <div className="agent-chat-main"><div className="agent-conversation-bar"><span>{view?.title || "新对话"}</span><small><i className={connection === "实时连接" ? "connected" : ""} />{selectedId ? connection : capabilities?.available ? "助手已就绪" : "等待配置模型"}</small></div>
        <div className="agent-chat-scroll" ref={scroller} onScroll={() => { const element = scroller.current; if (element) stickToBottom.current = element.scrollHeight - element.scrollTop - element.clientHeight < 100; }}>
          {selectedId && !view && !error && <div className="agent-loading"><Spin /><span>正在恢复对话</span></div>}
          {(!selectedId || view?.messages.length === 0) && <div className="agent-welcome"><div className="agent-welcome-symbol"><ThunderboltFilled /></div>
            <h2>开始对话</h2>
            <div className="agent-suggestions">{[
              ["查看待办", "帮我查一下当前公司有哪些需要人工复核的审批，列出主要风险"],
              ["解释当前单据", "帮我分析当前打开的审批，为什么需要复核？请给出证据和处理建议"],
              ["查找重复票据", "帮我查询当前公司的发票占用，看看是否有重复报销风险"],
              ["检查归档", "帮我检查当前公司的归档记录，找出需要处理的原件"],
            ].map(([label, text]) => <button key={label} onClick={() => adjust(text)}><span>{label}</span><ArrowUpOutlined /></button>)}</div></div>}
          {view?.messages.map(renderMessage)}
          {view?.proposals.filter((item) => !linkedProposals.has(item.id)).map(proposalCard)}
          {lastRun && lastRun.status !== "RUNNING" && lastRun.status !== "COMPLETED" && <div className="agent-run-error"><Alert type={lastRun.status === "CANCELED" ? "info" : "warning"} showIcon message={lastRun.error || lastRun.stage} />
            <Button size="small" disabled={sending || !capabilities?.available} onClick={() => { const original = view?.messages.find((item) => item.id === lastRun.user_message_id); if (original) void sendText(original.content, record(original.data.context) as unknown as AgentContext, Array.isArray(original.data.attachment_ids) ? original.data.attachment_ids as string[] : []); }}>重新发送这一轮</Button></div>}
          {activeRun && <div className="agent-live-status" role="status"><LoadingOutlined spin /><span>{activeRun.stage}</span><small>已确认的任务独立执行</small></div>}
          {error && <Alert className="agent-inline-error" type="error" showIcon message={error} closable onClose={() => setError("")} />}
        </div>
        <div className="agent-composer-area">
          {otherConversation && <Alert type="info" message="另一个会话正在执行，可以先写下问题。" action={<Button size="small" onClick={() => setSelectedId(otherConversation)}>返回处理中的会话</Button>} />}
          {capabilities && !capabilities.available && <Alert type="info" showIcon message={capabilities.reason} action={user.role === "admin" ? <Button size="small" onClick={() => { navigate("settings/integration"); onClose(); }}>配置模型</Button> : undefined} />}
          <div className="agent-context-chips"><span>本轮上下文</span>
            <Tag closable={Boolean(messageContext.company_id)} onClose={() => setRemoved((value) => ({ ...value, company: true }))}>{companies.find((item) => item.id === messageContext.company_id)?.name || "全部公司"}</Tag>
            {messageContext.approval_id && <Tag closable onClose={() => setRemoved((value) => ({ ...value, approval: true }))}>当前审批单</Tag>}
            {messageContext.selected_ids.length > 0 && <Tag closable onClose={() => setRemoved((value) => ({ ...value, selection: true }))}>{messageContext.selected_ids.length} 条选中记录</Tag>}
            {Object.values(messageContext.filters).some(Boolean) && <Tag closable onClose={() => setRemoved((value) => ({ ...value, filters: true }))}>当前列表筛选</Tag>}
          </div>
          <AgentAttachmentPicker conversationId={selectedId} attachments={draftAttachments}
            extensions={capabilities?.attachment_extensions || [".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".xml", ".ofd", ".docx", ".xlsx", ".csv", ".txt", ".md"]}
            maxFiles={capabilities?.max_attachments || 5} maxBytes={capabilities?.max_attachment_bytes || 20 * 1024 * 1024}
            disabled={sending || Boolean(selectedId && !view)} ensureConversation={ensureConversation} onUploaded={uploadedAttachment}
            onChanged={changeAttachment} onRemoved={removeAttachment} onBusyChange={setAttachmentBusy} preview={showPreview} />
          <div className="agent-composer"><Input.TextArea ref={textarea} aria-label="给业务助手发送消息" value={draft} onChange={(event) => setDraft(event.target.value)}
            autoSize={{ minRows: 2, maxRows: 6 }} maxLength={8000} placeholder={activeRun ? "可以先写下一个问题，等待本轮完成…" : "描述你想查询或处理的工作…"}
            onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void sendText(draft); } }} />
            <div className="agent-composer-bottom"><span><i />{capabilities?.model_name || "尚未配置模型"}{user.role === "viewer" && " · 只读权限"}</span>
              {activeRun ? <Button shape="circle" icon={<StopOutlined />} aria-label="停止生成" onClick={() => void post<AgentRun>(`/api/v1/agent/runs/${activeRun.id}/cancel`).catch((reason) => message.error((reason as Error).message))} />
                : <Button type="primary" shape="circle" loading={sending} icon={<ArrowUpOutlined />} aria-label="发送消息" disabled={(!draft.trim() && !draftAttachments.length) || !attachmentsReady || !capabilities?.available || Boolean(otherConversation)} onClick={() => void sendText(draft)} />}</div>
          </div><div className="agent-composer-note"><span>Enter 发送 · Shift + Enter 换行</span><button onClick={() => setUsageOpen(true)}>{view?.runs.some((run) => run.usage.totalTokens !== undefined) || view?.attachment_usage.totalTokens !== undefined
            ? `会话累计 ${count((view?.runs.reduce((sum, run) => sum + (run.usage.totalTokens || 0), 0) || 0) + (view?.attachment_usage.totalTokens || 0))} tokens` : "查看 Token 用量"}</button></div>
        </div>
      </div>
    </div>
  </Drawer>
    <AgentUsagePanel open={usageOpen} onClose={() => setUsageOpen(false)} />
    <Modal open={Boolean(rename)} title="重命名会话" zIndex={1450} onCancel={() => setRename(undefined)} onOk={() => {
      if (rename && renameText.trim()) void put<AgentConversation>(`/api/v1/agent/conversations/${rename.id}`, { title: renameText.trim() }).then((value) => {
        setHistory((current) => current.map((item) => item.id === value.id ? value : item)); setDetail((current) => current?.id === value.id ? { ...current, title: value.title } : current); setRename(undefined);
      }).catch((reason) => message.error((reason as Error).message));
    }}><Input aria-label="会话标题" maxLength={100} value={renameText} onChange={(event) => setRenameText(event.target.value)} /></Modal>
    <OriginalPreview value={preview} onClose={() => setPreview(undefined)} />
  </>;
}
