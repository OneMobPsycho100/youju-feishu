import { useCallback, useEffect, useRef, useState } from "react";
import type { Job } from "./types";

/** API 请求始终携带同源 HttpOnly 会话，不在 localStorage 中保存令牌。 */
export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(path, {
    credentials: "same-origin",
    ...options,
    headers: {
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401)
      window.dispatchEvent(new Event("session-expired"));
    throw new Error(
      typeof body.detail === "string"
        ? body.detail
        : `请求失败（${response.status}）`,
    );
  }
  return response.json() as Promise<T>;
}

export const post = <T>(path: string, data?: unknown) =>
  api<T>(path, {
    method: "POST",
    ...(data === undefined ? {} : { body: JSON.stringify(data) }),
  });
export const put = <T>(path: string, data: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(data) });
export const query = (
  path: string,
  values: Record<string, string | number | undefined>,
) => {
  const params = new URLSearchParams();
  Object.entries(values).forEach(([key, value]) => {
    if (value !== undefined && value !== "") params.set(key, String(value));
  });
  return `${path}?${params}`;
};

export function useResource<T>(path: string, tick = 0) {
  const [result, setResult] = useState<{ path: string; value: T }>();
  const [failure, setFailure] = useState<{ path: string; message: string }>();
  const sequence = useRef(0);
  const reload = useCallback(async () => {
    const request = ++sequence.current;
    try {
      const value = await api<T>(path);
      if (request === sequence.current) {
        setResult({ path, value });
        setFailure(undefined);
      }
    } catch (reason) {
      if (request === sequence.current)
        setFailure({ path, message: (reason as Error).message });
    }
  }, [path]);
  // 切换单据时立即隐藏旧路径的数据，且只接受最后一个请求的响应，避免错单操作。
  useEffect(() => {
    void reload();
    return () => {
      sequence.current++;
    };
  }, [reload, tick]);
  const data = result?.path === path ? result.value : undefined;
  const error = failure?.path === path ? failure.message : "";
  return { data, error, loading: data === undefined && !error, reload };
}

/** 只轮询持久化任务状态；HTTP 请求成功不代表飞书写入成功。 */
export async function waitJob(job: Job): Promise<Job> {
  for (let i = 0; i < 90; i++) {
    const next = await api<Job>(`/api/v1/jobs/${job.id}`);
    if (next.status === "SUCCEEDED") return next;
    if (next.status === "FAILED")
      throw new Error(next.error || "任务失败，请查看任务中心");
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error("任务仍在后台执行，可在系统管理的任务列表查看结果");
}

export const money = (amount: string | number | undefined) =>
  Number(amount || 0).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
export function localTime(value?: string, short = false, includeYear = false) {
  if (!value) return "—";
  const date = new Date(/[Zz]|[+]\d\d:\d\d$/.test(value) ? value : value + "Z");
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    ...(includeYear ? { year: "numeric" } : {}),
    month: "2-digit",
    day: "2-digit",
    ...(short ? {} : { hour: "2-digit", minute: "2-digit", hour12: false }),
  }).format(date);
}
export const bytes = (value: number) =>
  value > 1024 * 1024
    ? `${(value / 1024 / 1024).toFixed(1)} MB`
    : `${Math.max(1, Math.round(value / 1024))} KB`;
