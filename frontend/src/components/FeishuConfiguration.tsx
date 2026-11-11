import { useEffect, useRef, useState } from "react";
import { Alert, App, Button, Descriptions, Form, Input, Radio, Space, Tag, Typography } from "antd";
import { ApiOutlined, LinkOutlined, ReloadOutlined, SaveOutlined } from "@ant-design/icons";
import { localTime, post, put, useResource } from "../api";
import type { FeishuConfiguration as Configuration, FeishuConfigurationInput, FeishuTestResult } from "../feishuTypes";
import { HelpTip, ResourceState } from "../ui";
import { useWorkbench } from "../context";

interface ConfigurationProps {
  /** 当前使用场景；首次安装在保存后继续飞书绑定，其他场景更新现有配置。 */
  purpose?: "ADMIN" | "INIT" | "RECOVERY";
  /** 外部刷新版本，用于重新读取配置与接入状态。 */
  tick?: number;
  /** 配置成功保存后通知外层刷新状态。 */
  onSaved?: () => void;
}

/** 首次绑定、管理员维护和接入恢复共用表单；密钥只存在输入草稿中。 */
export function FeishuConfiguration({ purpose = "ADMIN", tick = 0, onSaved }: ConfigurationProps) {
  const { message } = App.useApp();
  const resource = useResource<Configuration>("/api/v1/feishu-config", tick);
  const [form] = Form.useForm<FeishuConfigurationInput>();
  const [busy, setBusy] = useState<"test" | "save" | "bind" | null>(null);
  const [result, setResult] = useState<FeishuTestResult | null>(null);
  const [stale, setStale] = useState(false);
  const version = useRef<number | null>(null);
  const dirty = useRef(false);
  const transport = Form.useWatch("event_transport", form) || "websocket";
  const publicUrl = Form.useWatch("public_url", form) || "";
  const appId = Form.useWatch("app_id", form) || "";
  const data = resource.data;
  const fill = (config: Configuration) => {
    form.setFieldsValue({ version: config.version, app_id: config.app_id, app_secret: "",
      public_url: config.status === "ACTIVE" ? config.public_url : config.app_secret_configured ? config.public_url : window.location.origin,
      event_transport: config.event_transport, encrypt_key: "", verification_token: "" });
    version.current = config.version;
    dirty.current = false;
    setStale(false);
    setResult(null);
  };
  useEffect(() => {
    if (!data || data.version === version.current) return;
    if (version.current !== null && dirty.current) setStale(true);
    else fill(data);
  }, [data, form]);
  useEffect(() => {
    if (purpose === "INIT") return;
    // 轮询运行状态时保留未保存的表单；新配置版本仍通过上面的脏数据保护处理。
    const timer = window.setInterval(() => { if (!document.hidden) void resource.reload(); }, 5000);
    return () => window.clearInterval(timer);
  }, [purpose, resource.reload]);

  const execute = async (action: "test" | "save" | "bind") => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setBusy(action);
    setResult(null);
    try {
      const payload = { ...values, version: version.current ?? 0, app_id: values.app_id.trim(),
        public_url: values.public_url.trim().replace(/\/+$/, ""), app_secret: values.app_secret || "",
        encrypt_key: values.encrypt_key || "", verification_token: values.verification_token || "" };
      if (action === "test") {
        setResult(await post<FeishuTestResult>("/api/v1/feishu-config/test", payload));
      } else if (action === "bind") {
        const next = await post<{ login_url: string }>("/api/setup/feishu", payload);
        form.setFieldsValue({ app_secret: "", encrypt_key: "", verification_token: "" });
        window.location.assign(next.login_url);
      } else {
        const updated = await put<Configuration>("/api/v1/feishu-config", payload);
        fill(updated);
        message.success("飞书配置已保存");
        resource.reload();
        onSaved?.();
      }
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(null); }
  };
  const callback = publicUrl.trim().replace(/\/+$/, "");
  const applicationUrl = /^cli_[A-Za-z0-9]+$/.test(appId) ? "https://open.feishu.cn/app/" + appId : "https://open.feishu.cn/app";
  const receiverLabels: Record<string, string> = { WAITING: "等待绑定", STARTING: "正在启动", RUNNING: "接收进程运行中",
    OFFLINE: "接收服务未运行", STOPPED: "已停止", ERROR: "接收服务异常", HTTP: "HTTP 接收已启用" };
  return <section className="feishu-configuration">
    <div className="model-config-heading"><h3>{purpose === "INIT" ? "连接企业飞书" : "飞书应用与事件接收"}</h3>
      {data && <Tag color={data.configured ? "success" : "default"}>{data.configured ? "已绑定企业" : "尚未绑定"}</Tag>}
    </div>
    <ResourceState loading={resource.loading} error={resource.error}>
      {data?.credential_error && <Alert className="template-transfer-notice" type="error" showIcon message={data.credential_error} />}
      {stale && <Alert className="template-transfer-notice" type="warning" showIcon message="其他管理员已更新配置，请重新加载后再保存"
        action={<Button disabled={!!busy} onClick={() => data && fill(data)}>重新加载</Button>} />}
      <Form form={form} layout="vertical" disabled={!!busy} onValuesChange={() => {
        dirty.current = true;
        setResult(null);
      }}>
        <div className="form-two-columns">
          <Form.Item name="app_id" label={<span className="field-label">App ID<HelpTip label="应用 ID 说明">{data?.status === "ACTIVE" ? "此部署已绑定该应用，更换应用或企业请使用独立部署。" : "在飞书应用后台的“凭证与基础信息”中获取。"}</HelpTip></span>} rules={[{ required: true, pattern: /^cli_[A-Za-z0-9]+$/, message: "请输入以 cli_ 开头的应用 ID" }]}>
            <Input disabled={data?.status === "ACTIVE" || !!busy} maxLength={100} placeholder="cli_..." />
          </Form.Item>
          <Form.Item name="app_secret" label={<Space>App Secret<Tag>{data?.app_secret_configured ? "已设置" : "未设置"}</Tag></Space>}
            rules={[{ validator: (_, value) => !value?.trim() && (!data?.app_secret_configured || !!data.credential_error)
              ? Promise.reject(new Error("请输入应用密钥")) : Promise.resolve() }]} extra="留空保留原密钥">
            <Input.Password maxLength={4096} autoComplete="new-password" placeholder={data?.app_secret_configured ? "留空保持原密钥，输入新值可轮换" : "请输入 App Secret"} />
          </Form.Item>
        </div>
        <Form.Item name="public_url" label={<span className="field-label">工作台访问地址<HelpTip label="工作台地址说明">正式部署使用 HTTPS；本机开发可使用 localhost 或 127.0.0.1。</HelpTip></span>} rules={[{ required: true, message: "请输入当前站点访问地址" }]}>
          <Input maxLength={2048} placeholder={window.location.origin} />
        </Form.Item>
        <details className="feishu-guidance">
          <summary>飞书后台配置指南</summary>
          <ol><li>在“安全设置”添加下面的登录回调地址。</li>
            <li>在“权限管理”申请审批读取、任务操作与登录所需权限，完成应用发布和可用范围配置。</li>
            <li>{transport === "websocket" ? "完成绑定后，事件服务会自动启动；再到“事件与回调”保存长连接接收方式，添加审批实例和任务事件。" : "在“事件与回调”填写下面的事件地址，并同步配置加密密钥和验证令牌。"}</li></ol>
          <Button href={applicationUrl} target="_blank" rel="noreferrer" icon={<LinkOutlined />}>打开飞书应用后台</Button>
          <Descriptions column={1} size="small" items={[
            { key: "login", label: "登录回调", children: <Typography.Text copyable={{ text: callback + "/api/auth/callback" }}>{callback + "/api/auth/callback"}</Typography.Text> },
            ...(transport === "http" ? [{ key: "events", label: "事件回调", children: <Typography.Text copyable={{ text: callback + "/api/feishu/events" }}>{callback + "/api/feishu/events"}</Typography.Text> }] : []),
          ]} />
        </details>
        <Form.Item name="event_transport" label="审批事件接收方式">
          <Radio.Group options={[{ value: "websocket", label: "长连接（推荐）" }, { value: "http", label: "HTTP 回调" }]} />
        </Form.Item>
        {transport === "http" && <div className="form-two-columns">
          <Form.Item name="encrypt_key" label={<Space>Encrypt Key<Tag>{data?.encrypt_key_configured ? "已设置" : "未设置"}</Tag></Space>}
            rules={[{ validator: (_, value) => !value?.trim() && !data?.encrypt_key_configured ? Promise.reject(new Error("HTTP 模式需填写 Encrypt Key")) : Promise.resolve() }]}>
            <Input.Password autoComplete="new-password" maxLength={512} placeholder="留空保留已有值" />
          </Form.Item>
          <Form.Item name="verification_token" label={<Space>Verification Token<Tag>{data?.verification_token_configured ? "已设置" : "未设置"}</Tag></Space>}
            rules={[{ validator: (_, value) => !value?.trim() && !data?.verification_token_configured ? Promise.reject(new Error("HTTP 模式需填写 Verification Token")) : Promise.resolve() }]}>
            <Input.Password autoComplete="new-password" maxLength={512} placeholder="留空保留已有值" />
          </Form.Item>
        </div>}
        <Space wrap><Button icon={<ApiOutlined />} loading={busy === "test"} disabled={!!busy} onClick={() => void execute("test")}>测试应用凭据</Button>
          <Button type="primary" icon={<SaveOutlined />} loading={busy === "save" || busy === "bind"} disabled={!!busy || stale}
            onClick={() => void execute(purpose === "INIT" ? "bind" : "save")}>{purpose === "INIT" ? "验证并继续飞书绑定" : "保存并应用"}</Button>
          <Button icon={<ReloadOutlined />} disabled={!!busy} onClick={() => { if (data) fill(data); resource.reload(); }}>重新加载</Button></Space>
      </Form>
      {result && <Alert className="template-transfer-notice" type="success" showIcon message={result.message} />}
      {data?.status === "ACTIVE" && <div className="feishu-runtime-status">
        <h4>当前接入状态</h4>
        {data.receiver_error && <Alert className="template-transfer-notice" type="warning" showIcon message={data.receiver_error} />}
        <Descriptions bordered size="small" column={2} items={[
          { key: "tenant", label: "企业标识", children: data.tenant_key },
          { key: "credential", label: "最近凭据验证", children: data.credential_verified_at ? localTime(data.credential_verified_at) : "尚未在页面验证" },
          { key: "receiver", label: "事件服务", children: receiverLabels[data.receiver_status] || data.receiver_status },
          { key: "configuration", label: "有效配置版本", children: data.version },
          { key: "version", label: "配置应用", children: data.receiver_applied_version === data.connection_version ? "已应用连接版本 " + data.connection_version : "等待应用连接版本 " + data.connection_version, span: 2 },
          { key: "event", label: "最近审批事件", children: data.last_event_at ? localTime(data.last_event_at) : "尚未收到，事件送达以此时间为准", span: 2 },
        ]} />
      </div>}
    </ResourceState>
  </section>;
}

export function FeishuSettings() {
  const { mode, tick, refresh } = useWorkbench();
  if (mode === "demo") return <Alert type="info" showIcon message="演示环境不支持配置飞书接入" />;
  return <FeishuConfiguration tick={tick} onSaved={refresh} />;
}
