/** 生成请求幂等键和临时条目标识，兼容公网 HTTP 不提供 randomUUID 的环境。 */
export function requestId(): string {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  // HTTP 入口仍可使用 getRandomValues；保留 128 位随机性，避免重复提交共用同一标识。
  const bytes = globalThis.crypto.getRandomValues(new Uint8Array(16));
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}
