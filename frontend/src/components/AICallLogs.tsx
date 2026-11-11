import { useEffect, useState } from "react";
import { Alert, Button, Descriptions, Drawer, Empty, Select, Space, Switch, Table, Tabs, Tag, Timeline } from "antd";
import { ReloadOutlined } from "@ant-design/icons";
import { localTime, query, useResource } from "../api";
import { useWorkbench } from "../context";
import type { AICallDetail, AICallSummary, Page } from "../types";
import { ResourceState, SectionTitle } from "../ui";

const states = { RUNNING: ["运行中", "processing"], COMPLETED: ["完成", "success"], FAILED: ["失败", "error"], SKIPPED: ["未调用", "default"] } as const;
const sourceLabel = (source: AICallSummary["source"]) => ({ AUDIT: "审批审核", CONNECTION_TEST: "连接测试", INVOICE_EXTRACTION: "发票识别", PAYMENT_MATERIAL: "付款材料读取", AGENT_ATTACHMENT: "会话附件解析" })[source];
const duration = (milliseconds: number) => `${(milliseconds / 1000).toFixed(2)} 秒`;

export function AICallStatus({ value }: { value: AICallSummary["status"] }) {
  const [label, color] = states[value];
  return <Tag color={color}>{label}</Tag>;
}

/** 全局页面与审批详情共用。后端根据角色及审批 ID 校验访问范围。 */
export function AICallLogs({ approvalId }: { approvalId?: string }) {
  const { tick, navigate } = useWorkbench();
  const [page, setPage] = useState(1);
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [selected, setSelected] = useState<string | null>(null);
  const resource = useResource<Page<AICallSummary>>(query("/api/v1/ai-calls", {
    page, status, source, approval_id: approvalId,
  }), tick);

  useEffect(() => {
    if (!autoRefresh) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void resource.reload();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [autoRefresh, resource.reload]);

  return (
    <div className="ai-call-logs">
      <SectionTitle title="AI 调用日志"
        action={<Space><span className="muted">自动刷新</span><Switch size="small" checked={autoRefresh} onChange={setAutoRefresh} />
          <Button icon={<ReloadOutlined />} onClick={() => void resource.reload()}>刷新</Button></Space>} />
      <Space className="ai-log-filters" wrap>
        {!approvalId && <Select aria-label="调用来源" style={{ width: 145 }} value={source} onChange={(value) => { setSource(value); setPage(1); }}
          options={[{ value: "", label: "全部来源" }, { value: "AUDIT", label: "审批审核" }, { value: "INVOICE_EXTRACTION", label: "发票识别" }, { value: "PAYMENT_MATERIAL", label: "付款材料读取" }, { value: "CONNECTION_TEST", label: "连接测试" }]} />}
        <Select aria-label="调用状态" style={{ width: 135 }} value={status} onChange={(value) => { setStatus(value); setPage(1); }}
          options={[{ value: "", label: "全部状态" }, ...Object.entries(states).map(([value, [label]]) => ({ value, label }))]} />
      </Space>
      <ResourceState error={resource.error}>
        <Table<AICallSummary> rowKey="id" loading={resource.loading} tableLayout="fixed"
          scroll={{ x: approvalId ? 1020 : 1260 }} dataSource={resource.data?.items}
          locale={{ emptyText: <Empty description="暂无调用日志" /> }}
          pagination={{ current: page, total: resource.data?.total, pageSize: 20, showSizeChanger: false, onChange: setPage }}
          columns={[
            { title: "开始时间", dataIndex: "started_at", width: 145, render: (value) => localTime(value) },
            { title: "来源", dataIndex: "source", width: 100, render: sourceLabel },
            ...(!approvalId ? [{ title: "审批", key: "approval", width: 240, render: (_: unknown, row: AICallSummary) => row.approval_id
              ? <Button className="ai-log-approval-link" type="link" title={row.approval_id}
                  onClick={() => navigate(`approvals/${row.approval_id}`)}>{row.approval_id}</Button>
              : <span className="muted">虚拟数据检查</span> }] : []),
            { title: "模型", dataIndex: "model_name", width: 200, ellipsis: true, render: (value) => value || "未配置" },
            { title: "状态与阶段", key: "state", width: 200, render: (_, row) => <><AICallStatus value={row.status} /><div className="muted ai-log-stage">{row.stage}</div></> },
            { title: "耗时", key: "duration", width: 110, render: (_, row) => row.status === "RUNNING" ? "执行中" : duration(row.duration_ms) },
            { title: "可见内容", key: "content", width: 150, render: (_, row) => <Space size={0} wrap>
              {row.has_reasoning && <Tag>思考内容</Tag>}{row.has_output && <Tag>原始输出</Tag>}
              {!row.has_reasoning && !row.has_output && <span className="muted">尚无返回</span>}</Space> },
            { title: "详情", key: "detail", width: 115, fixed: "right", render: (_, row) => <Button type="link" onClick={() => setSelected(row.id)}>查看调用</Button> },
          ]} />
      </ResourceState>
      <AICallDrawer callId={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

export function AICallDrawer({ callId, onClose }: { callId: string | null; onClose: () => void }) {
  return <Drawer open={!!callId} width={920} title="AI 调用详情" onClose={onClose} destroyOnHidden>
    {callId && <AICallDetails key={callId} callId={callId} />}
  </Drawer>;
}

function AICallDetails({ callId }: { callId: string }) {
  const resource = useResource<AICallDetail>(`/api/v1/ai-calls/${encodeURIComponent(callId)}`);
  const data = resource.data;
  useEffect(() => {
    if (data?.status !== "RUNNING") return;
    const timer = window.setInterval(() => void resource.reload(), 1500);
    return () => window.clearInterval(timer);
  }, [data?.status, resource.reload]);

  return <ResourceState loading={resource.loading} error={resource.error}>
    {data && <>
      <Descriptions bordered size="small" column={2} items={[
        { key: "id", label: "调用 ID", children: <code>{data.id}</code>, span: 2 },
        { key: "status", label: "状态", children: <AICallStatus value={data.status} /> },
        { key: "stage", label: "当前阶段", children: data.stage },
        { key: "model", label: "模型", children: data.model_name || "未配置" },
        { key: "source", label: "来源", children: sourceLabel(data.source) },
        { key: "actor", label: "触发者", children: data.actor },
        { key: "version", label: "配置版本", children: data.config_version },
        { key: "start", label: "开始时间", children: localTime(data.started_at) },
        { key: "duration", label: "耗时", children: data.status === "RUNNING" ? "执行中" : duration(data.duration_ms) },
        { key: "finish", label: "结束原因", children: data.finish_reason || "—" },
        { key: "tokens", label: "Token 用量", children: Object.keys(data.usage).length
          ? `输入 ${data.usage.inputTokens ?? "—"} / 输出 ${data.usage.outputTokens ?? "—"} / 总计 ${data.usage.totalTokens ?? "—"}` : "供应商未提供" },
        { key: "engine", label: "引擎", children: data.engine, span: 2 },
        { key: "session", label: "会话 ID", children: <code>{data.session_id}</code>, span: 2 },
      ]} />
      {data.error_message && <Alert className="ai-log-alert" showIcon type={data.status === "FAILED" ? "error" : "info"}
        message={data.error_message} description={data.error_type || undefined} />}
      {data.truncated && <Alert className="ai-log-alert" type="warning" showIcon message="本次日志达到长度上限，部分文本或阶段事件已截断。" />}
      <Tabs className="ai-log-tabs" defaultActiveKey="output" items={[
        { key: "output", label: "最终输出", children: <>
          {data.has_output ? <pre className="ai-trace-text">{data.raw_output}</pre>
            : <Empty description={data.status === "RUNNING" ? "正在等待模型输出" : "本次调用未收到模型输出"} />}
          {Object.keys(data.parsed_result).length > 0 && <details className="agent-prompt-preview"><summary>查看结构化解析结果</summary>
            <pre>{JSON.stringify(data.parsed_result, null, 2)}</pre></details>}
        </> },
        { key: "reasoning", label: "思考过程", children: <>
          <p className="muted">仅展示模型服务明确返回的 reasoning / thinking 文本。</p>
          {data.has_reasoning ? <pre className="ai-trace-text">{data.reasoning}</pre>
            : <Empty description={data.status === "RUNNING" ? "等待供应商返回可见思考内容" : "供应商未返回可见思考内容，或本次未调用模型"} />}
        </> },
        { key: "events", label: "调用过程", children: <Timeline className="ai-trace-timeline" items={data.events.map((event, index) => ({
          key: `${event.type}-${index}`, children: <><strong>{event.label}</strong><div className="muted">{localTime(event.time)} · {event.type}</div></>,
        }))} /> },
        { key: "input", label: "提示词与输入", children: <><h4>实际系统提示词</h4><pre className="ai-trace-text">{data.prompt}</pre>
          <h4>结构化输入</h4><pre className="ai-trace-text">{data.input_json || "尚未准备输入"}</pre></> },
      ]} />
    </>}
  </ResourceState>;
}
