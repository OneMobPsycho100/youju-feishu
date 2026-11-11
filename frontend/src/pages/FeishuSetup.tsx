import { useEffect, useState } from "react";
import { Alert, App, Button, Form, Input, Result, Space, Steps } from "antd";
import { SafetyCertificateOutlined } from "@ant-design/icons";
import { post, useResource } from "../api";
import type { SetupStatus } from "../feishuTypes";
import { FeishuConfiguration } from "../components/FeishuConfiguration";
import { HelpTip, ResourceState } from "../ui";

/** 首次向导不依赖工作台用户上下文；安装授权只允许配置接入，不能读取审批业务。 */
export function FeishuSetup() {
  const { message } = App.useApp();
  const status = useResource<SetupStatus>("/api/setup/status");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [form] = Form.useForm<{ code: string }>();
  const data = status.data;
  useEffect(() => {
    const reload = () => status.reload();
    window.addEventListener("session-expired", reload);
    return () => window.removeEventListener("session-expired", reload);
  }, [status.reload]);
  const recovery = data && !data.setup_required;
  const unlock = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setBusy(true);
    try {
      await post<SetupStatus>("/api/setup/unlock", { code: values.code.trim() });
      form.resetFields();
      status.reload();
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <div className="feishu-setup-page"><div className="feishu-setup-card">
    <header><SafetyCertificateOutlined /><h1>{recovery ? "飞书接入恢复" : "连接飞书"}</h1></header>
    <ResourceState loading={status.loading} error={status.error}>
      {done ? <Result status="success" title="接入配置已修复" subTitle="恢复码已失效"
        extra={<Button type="primary" onClick={() => window.location.assign("/")}>返回飞书登录</Button>} />
        : <>
          <Steps size="small" current={data?.unlocked ? 1 : 0} items={[
            { title: "确认安装授权" }, { title: "配置飞书应用" }, { title: recovery ? "保存并应用" : "飞书登录绑定" },
          ]} />
          {new URLSearchParams(window.location.search).get("setup_error") && <Alert className="template-transfer-notice" type="error" showIcon
            message="飞书绑定未完成，配置尚未生效" description="请核对应用发布状态、登录权限和回调地址后重试。未过期的凭据草稿已保留，无需重复输入。" />}
          {!data?.unlocked ? <div className="feishu-setup-unlock">
            <Alert type="info" showIcon message={<span className="field-label">{recovery ? "请输入安装控制台生成的恢复码，10 分钟内有效" : "请输入安装控制台显示的初始化码"}<HelpTip label="安装授权说明">此授权仅用于飞书接入配置，不授予业务数据访问权限。</HelpTip></span>} />
            <Form form={form} layout="vertical" onFinish={() => void unlock()} disabled={busy}>
              <Form.Item name="code" label={recovery ? "接入恢复码" : "安装初始化码"} rules={[{ required: true, message: "请输入安装控制台提供的授权码" }]}>
                <Input.Password autoComplete="off" placeholder="粘贴安装授权码" maxLength={4096} />
              </Form.Item>
              <Space><Button type="primary" htmlType="submit" loading={busy}>验证并继续</Button>
                {recovery && <Button onClick={() => window.location.assign("/")}>返回登录</Button>}</Space>
            </Form>
            <details className="template-setup-details"><summary>安装码丢失或需要生成恢复码</summary>
              <p>在部署服务器的 API 容器执行以下命令，新的授权码只显示在安装控制台：</p>
              <pre>{"python -m app.setup_access" + (recovery ? " --recovery" : "")}</pre>
            </details>
          </div> : <FeishuConfiguration purpose={data.purpose === "RECOVERY" ? "RECOVERY" : "INIT"} onSaved={() => setDone(true)} />}
        </>}
    </ResourceState>
  </div></div>;
}
