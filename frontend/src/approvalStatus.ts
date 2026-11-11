import { useEffect } from "react";
import type { AIApprovalStatus } from "./types";

/** 仅排队与执行中的预审需要高频刷新；审核结论与是否通过无关。 */
export const isAIApprovalActive = (status?: AIApprovalStatus) => status === "QUEUED" || status === "RUNNING";

/** 列表与详情共用：可见时每三秒刷新，在途请求串行执行，切换单据或隐藏页面时清理定时器。 */
export function useApprovalPolling(active: boolean, reload: () => Promise<void>) {
  useEffect(() => {
    let stopped = false;
    let inFlight = false;
    let timer: number | undefined;
    const schedule = () => {
      if (!stopped && active && !document.hidden) timer = window.setTimeout(() => void refresh(), 3000);
    };
    const refresh = async () => {
      if (stopped || inFlight || document.hidden) return;
      window.clearTimeout(timer);
      inFlight = true;
      try {
        await reload();
      } finally {
        inFlight = false;
        schedule();
      }
    };
    const onVisibility = () => {
      window.clearTimeout(timer);
      // 即使后台已经结束，也在页面重新可见时立即读取最新结果。
      if (!document.hidden) void refresh();
    };
    schedule();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stopped = true;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [active, reload]);
}
