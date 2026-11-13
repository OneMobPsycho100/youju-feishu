import type { Job } from "./types";

/** 单条指令发送时冻结的页面上下文；用于查询默认值，权限由服务端决定。 */
export interface AgentContext {
  route: string;
  company_id: string;
  approval_id: string;
  selection_type: "" | "approvals" | "invoices" | "archives" | "archived_approvals" | "budgets";
  selected_ids: string[];
  filters: Record<string, string>;
}
/** 页面注册给全局助手的筛选和选择；当前主体、路由由工作台统一提供。 */
export type AgentPageContext = Pick<AgentContext, "selection_type" | "selected_ids" | "filters">;

export interface AgentCapabilities {
  available: boolean;
  reason: string;
  model_name: string;
  can_write: boolean;
  can_sync: boolean;
  actions: string[];
  max_selected: number;
  active_conversation_id: string;
  active_run_id: string;
  /** 上传限制来自服务端，避免前端与代理大小限制不一致。 */
  max_attachments: number;
  max_attachment_bytes: number;
  attachment_extensions: string[];
}

/** 私人原件的白名单信息；磁盘路径和解析全文不会随状态流发送。 */
export interface AgentAttachment {
  id: string;
  conversation_id: string;
  upload_id: string;
  filename: string;
  mime: string;
  size: number;
  status: "QUEUED" | "PROCESSING" | "READY" | "FAILED";
  error: string;
  submitted: boolean;
  created_at: string;
  updated_at: string;
}

export interface AgentConversation {
  id: string;
  title: string;
  event_seq: number;
  created_at: string;
  updated_at: string;
}

export interface AgentMessage {
  id: string;
  conversation_id: string;
  run_id: string | null;
  role: "user" | "assistant" | "tool" | "system";
  content: string;
  data: Record<string, unknown>;
  created_at: string;
}

export interface AgentRun {
  id: string;
  conversation_id: string;
  user_message_id: string;
  status: "RUNNING" | "COMPLETED" | "FAILED" | "CANCELED" | "INTERRUPTED";
  stage: string;
  model_name: string;
  config_version: number;
  error: string;
  usage: Record<string, number>;
  started_at: string;
  finished_at: string | null;
}

/** 操作参数不可由确认请求改写；display 完全由服务端根据真实目标生成。 */
export interface AgentProposal {
  id: string;
  run_id: string;
  kind: string;
  target_id: string;
  status: "PENDING" | "SUBMITTED" | "CANCELED" | "EXPIRED" | "INVALID";
  display: Record<string, unknown>;
  error: string;
  job: Job | null;
  operation_status: string;
  expires_at: string;
  created_at: string;
}

export interface AgentConversationDetail extends AgentConversation {
  messages: AgentMessage[];
  runs: AgentRun[];
  proposals: AgentProposal[];
  attachments: AgentAttachment[];
  /** 附件解析用量与 runs 的对话生成用量分别统计，汇总时只相加一次。 */
  attachment_usage: Record<string, number>;
}

/** SSE 只承载已提交的白名单事件，序号用于去重及断线续传。 */
export interface AgentEvent {
  seq: number;
  kind: "message.created" | "message.updated" | "run.updated" | "action.updated" | "attachment.updated" | "attachment.deleted";
  data: { message?: AgentMessage; run?: AgentRun; proposal?: AgentProposal; attachment?: AgentAttachment; attachment_id?: string;
    attachment_usage?: Record<string, number> };
}
