import { createContext, useContext, useEffect } from "react";
import type { Company, Job, User } from "./types";
import type { AgentPageContext } from "./agentTypes";

/** 页面共用的会话与刷新边界，避免各模块维护不一致的权限状态。 */
export interface WorkbenchState {
  user: User;
  mode: string;
  companies: Company[];
  tick: number;
  companyId: string;
  setCompanyId: (id: string) => void;
  refresh: () => void;
  onJob: (job: Job, label: string) => Promise<void>;
  navigate: (path: string) => void;
  setAgentPageContext: (context: AgentPageContext) => void;
  openAgent: () => void;
}

/** 页面变化立即更新助手上下文，离开页面时清除旧选择，防止跨页面误引用。 */
export function useAgentPageContext(value: AgentPageContext) {
  const { setAgentPageContext } = useWorkbench();
  const serialized = JSON.stringify(value);
  useEffect(() => {
    setAgentPageContext(JSON.parse(serialized) as AgentPageContext);
    return () => setAgentPageContext({ selection_type: "", selected_ids: [], filters: {} });
  }, [serialized, setAgentPageContext]);
}
export const WorkbenchContext = createContext<WorkbenchState | null>(null);
export function useWorkbench() {
  const value = useContext(WorkbenchContext);
  if (!value) throw new Error("缺少工作台上下文");
  return value;
}
