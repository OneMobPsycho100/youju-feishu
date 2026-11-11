import { useEffect, useRef, useState } from "react";
import { Alert, App as AntApp, Button, Checkbox, Form, Input, Space, Switch, Tag } from "antd";
import { ApiOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { post, put, useResource } from "../api";
import { useWorkbench } from "../context";
import type { ModelConfiguration as ModelConfig, ModelConfigurationInput, ModelConnectionResult } from "../types";
import { HelpTip, ResourceState } from "../ui";
import { AICallDrawer } from "../components/AICallLogs";

export function ModelConfiguration() {
  const { tick, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<ModelConfig>("/api/v1/model-config", tick);
  const [form] = Form.useForm<ModelConfigurationInput>();
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [result, setResult] = useState<ModelConnectionResult | null>(null);
  const [stale, setStale] = useState(false);
  const [traceOpen, setTraceOpen] = useState(false);
  const loadedVersion = useRef<number | null>(null);
  const dirty = useRef(false);
  const enabled = Form.useWatch("enabled", form);
  const clearing = Form.useWatch("clear_api_key", form);
  const data = resource.data;

  const fill = (config: ModelConfig) => {
    form.setFieldsValue({ version: config.version, enabled: config.enabled, base_url: config.base_url,
      model_name: config.model_name, api_key: "", clear_api_key: false, default_prompt: config.default_prompt });
    loadedVersion.current = config.version;
    dirty.current = false;
    setStale(false);
    setResult(null);
  };

  useEffect(() => {
    if (!data || data.version === loadedVersion.current) return;
    // 后台刷新不覆盖正在编辑的草稿；明确提示并发更新，由管理员主动重新加载。
    if (loadedVersion.current !== null && dirty.current) setStale(true);
    else fill(data);
  }, [data, form]);

  const execute = async (action: "save" | "test") => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setBusy(action);
    setResult(null);
    try {
      const payload = { ...values, version: loadedVersion.current ?? 0,
        enabled: values.clear_api_key ? false : values.enabled,
        base_url: values.base_url.trim(), model_name: values.model_name.trim(),
        default_prompt: values.default_prompt.trim(), api_key: values.clear_api_key ? "" : values.api_key || "" };
      if (action === "test") {
        setResult(await post<ModelConnectionResult>("/api/v1/model-config/test", payload));
      } else {
        const config = await put<ModelConfig>("/api/v1/model-config", payload);
        form.resetFields();
        fill(config);
        message.success("配置已保存，相关待办将重新审核");
        refresh();
      }
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="model-configuration">
      <div className="model-config-heading">
        <h3>模型与审核要求</h3>
        {data && <Space wrap><Tag color="blue">{data.engine}</Tag>
          <Tag>{data.source === "database" ? "工作台配置" : "部署环境配置"}</Tag></Space>}
      </div>
      <ResourceState loading={resource.loading} error={resource.error}>
        {data?.credential_error && <Alert type="error" showIcon message={data.credential_error} />}
        {stale && <Alert type="warning" showIcon message="配置已被更新，当前草稿尚未覆盖。请重新加载后再保存。"
          action={<Button size="small" disabled={!!busy} onClick={() => data && fill(data)}>重新加载</Button>} />}
        <Form form={form} layout="vertical" disabled={!!busy} onValuesChange={(changed) => {
          dirty.current = true;
          setResult(null);
          if (changed.clear_api_key) form.setFieldsValue({ enabled: false, api_key: "" });
        }}>
          <Form.Item name="enabled" label={<span className="field-label">启用模型语义审核<HelpTip label="模型审核开关说明">关闭后仍执行规则预审；需要模型核对的模板将转人工复核。</HelpTip></span>} valuePropName="checked">
            <Switch disabled={!!busy || clearing} />
          </Form.Item>
          <div className="form-two-columns">
            <Form.Item name="base_url" label={<span className="field-label">模型 API 根地址<HelpTip label="模型地址填写说明">包含 /v1 等供应商前缀，服务需支持流式 Chat Completions。</HelpTip></span>} rules={[
              { required: enabled && !clearing, message: "请输入模型 API 根地址" },
              { validator: (_, value) => {
                if (!value?.trim()) return Promise.resolve();
                try {
                  const url = new URL(value.trim());
                  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash
                    || url.pathname.replace(/\/+$/, "").endsWith("/chat/completions")) throw new Error();
                  return Promise.resolve();
                } catch { return Promise.reject(new Error("请填写 HTTP(S) 根地址，不含账号、查询参数或 /chat/completions")); }
              } },
            ]}>
              <Input maxLength={2048} placeholder="https://your-model-service.example/v1" />
            </Form.Item>
            <Form.Item name="model_name" label="模型名称" rules={[
              { required: enabled && !clearing, whitespace: true, message: "请输入模型名称" },
            ]}>
              <Input maxLength={200} placeholder="填写供应商的模型 ID" />
            </Form.Item>
          </div>
          <Form.Item name="api_key" label={<Space>API Key<Tag color={data?.api_key_configured ? "success" : "default"}>
            {data?.api_key_configured ? "已设置" : "未设置"}</Tag></Space>}
            rules={[{ validator: (_, value) => enabled && !clearing && !value?.trim() && (!data?.api_key_configured || !!data?.credential_error)
              ? Promise.reject(new Error("请输入可用的 API Key")) : Promise.resolve() }]}
            extra="留空保留原密钥">
            <Input.Password maxLength={8192} autoComplete="new-password" disabled={!!busy || clearing}
              placeholder={data?.api_key_configured ? "留空保持原密钥，输入新值可更换" : "请输入 API Key"} />
          </Form.Item>
          <Form.Item name="clear_api_key" valuePropName="checked">
            <Checkbox>清除已保存的密钥并停用模型</Checkbox>
          </Form.Item>
          <div className="model-prompt-heading"><div className="section-title-heading"><h4>全局默认审核提示词</h4><HelpTip label="默认审核提示词说明">填写审核关注点和判断标准。模板未单独配置提示词时使用此内容。</HelpTip></div>
            <Button type="link" icon={<ReloadOutlined />} onClick={() => {
              dirty.current = true;
              form.setFieldsValue({ default_prompt: data?.builtin_prompt }); setResult(null);
            }}>恢复内置默认</Button></div>
          <Form.Item name="default_prompt" rules={[{ required: true, whitespace: true, message: "请填写全局审核要求" }]}>
            <Input.TextArea rows={5} maxLength={20000} showCount />
          </Form.Item>
          <details className="agent-prompt-preview"><summary>系统输出约定</summary>
            <pre>{data?.system_constraints}</pre></details>
          {result && <Alert showIcon type={result.success ? "success" : "error"}
            message={result.message} description={<>
              {`耗时 ${(result.latency_ms / 1000).toFixed(2)} 秒；本次检查未保存配置。`}
              {result.call_id && <Button type="link" onClick={() => setTraceOpen(true)}>查看 AI 调用日志</Button>}
            </>} />}
          <div className="model-config-actions"><Space wrap>
            <Button type="primary" icon={<SaveOutlined />} loading={busy === "save"} disabled={!!busy || stale}
              onClick={() => void execute("save")}>保存配置</Button>
            <Button icon={<ApiOutlined />} loading={busy === "test"} disabled={!!busy || !!clearing}
              onClick={() => void execute("test")}>测试连接</Button>
          </Space><HelpTip label="连接测试说明">连接测试使用虚拟数据，不保存配置。保存后才影响审核。</HelpTip></div>
        </Form>
      </ResourceState>
      <AICallDrawer callId={traceOpen ? result?.call_id || null : null} onClose={() => setTraceOpen(false)} />
    </section>
  );
}
