import { useEffect, useState } from "react";
import { Alert, Empty, Modal, Segmented, Select, Spin, Tooltip } from "antd";
import { api } from "../api";
import { useWorkbench } from "../context";
import { HelpTip } from "../ui";

/** 聚合接口仅返回供应商用量和覆盖率；管理员全局模式不含其他用户的会话正文。 */
interface UsageSummary {
  plugin: string;
  timezone: string;
  scope: "mine" | "all";
  calls: number;
  reported: number;
  missing: number;
  totals: Record<string, number>;
  coverage: Record<string, number>;
  cache_hit_rate: number | null;
  daily: { date: string; inputTokens: number; outputTokens: number; totalTokens: number; calls: number }[];
  models: { model: string; inputTokens: number; outputTokens: number; totalTokens: number; calls: number }[];
  sources: { source: string; totalTokens: number; calls: number }[];
}

const format = (value: number | undefined) => value === undefined ? "—" : value.toLocaleString("zh-CN");

export function AgentUsagePanel({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { user } = useWorkbench();
  const [days, setDays] = useState(7);
  const [scope, setScope] = useState("mine");
  const [data, setData] = useState<UsageSummary>();
  const [error, setError] = useState("");
  useEffect(() => {
    let active = true;
    if (!open) return;
    setData(undefined);
    setError("");
    void api<UsageSummary>(`/api/v1/agent/usage?days=${days}&scope=${scope}`).then((value) => { if (active) setData(value); })
      .catch((reason) => { if (active) setError((reason as Error).message); });
    return () => { active = false; };
  }, [open, days, scope]);
  const maximum = Math.max(1, ...data?.daily.map((item) => item.totalTokens) || []);
  return <Modal open={open} onCancel={onClose} footer={null} title={<span className="section-title-heading">模型用量统计<HelpTip label="用量统计口径">完整输入包含未缓存输入及缓存读写；推理计入输出。未提供的字段显示“—”，不按字符估算费用。本人范围仅包含自己的助手会话；全局范围还包含审批审核和连接测试。</HelpTip></span>} width={810} zIndex={1450} className="agent-usage-modal">
    <div className="agent-usage-toolbar"><Segmented value={days} onChange={(value) => setDays(Number(value))} options={[{ label: "近 7 天", value: 7 }, { label: "近 30 天", value: 30 }, { label: "近 90 天", value: 90 }]} />
      {user.role === "admin" && <Select aria-label="Token 统计范围" value={scope} onChange={setScope} style={{ width: 165 }} options={[{ value: "mine", label: "我的助手会话" }, { value: "all", label: "工作台全部调用" }]} />}</div>
    {error && <Alert type="error" message={error} showIcon />}
    {!data && !error && <div className="agent-loading"><Spin /></div>}
    {data && <><div className="agent-usage-kpis">{[["完整输入 Token", data.totals.inputTokens], ["输出 Token", data.totals.outputTokens], ["缓存读取 Token", data.totals.cacheReadTokens], ["合计 Token", data.totals.totalTokens]].map(([label, value]) =>
      <div key={String(label)}><span>{label}</span><strong>{format(value as number | undefined)}</strong></div>)}</div>
      <div className="agent-usage-coverage"><span>{data.calls} 次运行／调用 · {data.reported} 次有用量</span>
        {data.cache_hit_rate !== null && <Tooltip title="在同时返回缓存读取与完整输入的样本中，缓存读取 ÷ 完整输入"><span>缓存命中率 {(data.cache_hit_rate * 100).toFixed(1)}%</span></Tooltip>}</div>
      {data.missing > 0 && <Alert type="info" showIcon message={`${data.missing} 次运行／调用未返回完整用量；以下合计仅覆盖已收到的供应商数据。`} />}
      <div className="agent-usage-section"><h4>每日用量 <small>北京时间</small></h4><div className="agent-token-legend"><i /> 输入 <i /> 输出</div>
        {!data.daily.length ? <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="此时间范围内暂无调用记录" /> : <div className="agent-token-chart">{data.daily.map((item) =>
          <Tooltip key={item.date} title={`${item.date} · 输入 ${format(item.inputTokens)} · 输出 ${format(item.outputTokens)} · ${item.calls} 次`}>
            <div className="agent-token-day" aria-label={`${item.date} 使用 ${item.totalTokens} tokens`}><span>{item.date.slice(5)}</span><div className="agent-token-track">
              <i style={{ width: `${item.inputTokens / maximum * 100}%` }} /><i style={{ width: `${item.outputTokens / maximum * 100}%` }} /></div><b>{format(item.totalTokens)}</b></div>
          </Tooltip>)}</div>}</div>
      <div className="agent-usage-section"><h4>按模型统计</h4><div className="agent-field-diff"><table><thead><tr><th>模型</th><th>输入</th><th>输出</th><th>合计</th><th>次数</th></tr></thead><tbody>
        {data.models.map((item) => <tr key={item.model}><td>{item.model || "未配置"}</td><td>{format(item.inputTokens)}</td><td>{format(item.outputTokens)}</td><td>{format(item.totalTokens)}</td><td>{item.calls}</td></tr>)}
      </tbody></table></div></div>
      {data.scope === "all" && <div className="agent-usage-sources">{data.sources.map((item) => <span key={item.source}>{item.source} · {format(item.totalTokens)} tokens · {item.calls} 次</span>)}</div>}
    </>}
  </Modal>;
}
