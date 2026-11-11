import { useEffect, useRef, useState } from "react";
import { Alert, App as AntApp, Button, Checkbox, Form, Input, Space, Switch, Tag } from "antd";
import { SaveOutlined } from "@ant-design/icons";
import { api, put, useResource } from "../api";
import { useWorkbench } from "../context";
import type { OCRConfig, OCRConnectionResult } from "../types";
import { ResourceState } from "../ui";

interface OCRFormValues {
  enabled: boolean;
  secret_id: string;
  secret_key: string;
  clear_credentials: boolean;
}

const configPath = "/api/v1/ocr-config";

export function OCRConfiguration() {
  const { tick, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<OCRConfig>(configPath, tick);
  const [form] = Form.useForm<OCRFormValues>();
  const [busy, setBusy] = useState<"save" | "test" | null>(null);
  const [stale, setStale] = useState(false);
  const [sample, setSample] = useState<File | null>(null);
  const [result, setResult] = useState<OCRConnectionResult | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const loadedVersion = useRef<number | null>(null);
  const dirty = useRef(false);
  const clearing = Form.useWatch("clear_credentials", form);
  const formDisabled = !!busy || stale;
  const data = resource.data;

  const fill = (config: OCRConfig) => {
    form.setFieldsValue({ enabled: config.enabled, secret_id: "", secret_key: "", clear_credentials: false });
    loadedVersion.current = config.version;
    dirty.current = false;
    setStale(false);
    setSample(null);
    setResult(null);
    if (fileInput.current) fileInput.current.value = "";
  };

  useEffect(() => {
    if (!data || data.version === loadedVersion.current) return;
    // 定时刷新可以提示版本冲突，管理员输入的凭据由管理员主动清除或提交。
    if (dirty.current) setStale(true);
    else fill(data);
  }, [data, form]);

  const execute = async (action: "save" | "test") => {
    if (!data || loadedVersion.current === null || stale || busy) return;
    const values = form.getFieldsValue();
    const secretId = values.secret_id?.trim() || "";
    const secretKey = values.secret_key?.trim() || "";
    const enabled = !values.clear_credentials && values.enabled;
    const canReuse = data.credentials_configured && !data.credential_error;
    if (Boolean(secretId) !== Boolean(secretKey)) {
      message.error("请同时填写 SecretId 和 SecretKey，或将两项都留空");
      return;
    }
    if ((enabled || action === "test") && !canReuse && !secretId) {
      message.error("请填写一组完整的腾讯云凭据");
      return;
    }
    setBusy(action);
    setResult(null);
    try {
      const version = loadedVersion.current;
      if (action === "test" && sample) {
        const body = new FormData();
        body.append("file", sample);
        body.append("version", String(version));
        body.append("secret_id", secretId);
        body.append("secret_key", secretKey);
        setResult(await api<OCRConnectionResult>(`${configPath}/test`, { method: "POST", body }));
      } else if (action === "save") {
        const config = await put<OCRConfig>(configPath, {
          version, enabled, secret_id: secretId, secret_key: secretKey, clear_credentials: values.clear_credentials,
        });
        fill(config);
        refresh();
        message.success("腾讯云发票 OCR 配置已保存");
      }
    } catch (reason) {
      message.error((reason as Error).message);
      void resource.reload();
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="model-configuration">
      <div className="model-config-heading">
        <h3>腾讯云发票 OCR</h3>
        {data && <Tag color={data.credentials_configured && !data.credential_error ? "success" : "default"}>
          {data.credential_error ? "凭据读取失败" : data.credentials_configured ? "已保存凭据" : "尚未保存凭据"}
        </Tag>}
      </div>
      <ResourceState loading={resource.loading} error={resource.error}>
        {data?.credential_error && <Alert type="error" showIcon message={data.credential_error} />}
        {stale && <Alert type="warning" showIcon message="配置已被其他管理员更新，请重新加载后编辑。"
          action={<Button size="small" disabled={!!busy} onClick={() => data && fill(data)}>重新加载</Button>} />}
        <Form name="invoice-ocr-config" form={form} layout="vertical" disabled={formDisabled}
          onFinish={() => void execute("save")} onValuesChange={(changed) => {
            dirty.current = true;
            setResult(null);
            if (changed.clear_credentials) form.setFieldsValue({ enabled: false, secret_id: "", secret_key: "" });
          }}>
          <Form.Item name="enabled" label="启用腾讯云发票 OCR" valuePropName="checked"
            extra="启用后，进入发票识别的图片会逐页发送给腾讯云。PDF 仅本地解析。配置生效时，待处理审批会重新预审。">
            <Switch disabled={formDisabled || clearing} />
          </Form.Item>
          <div className="form-two-columns">
            <Form.Item name="secret_id" label="SecretId" extra="两项留空会沿用已保存的凭据">
              <Input.Password maxLength={200} autoComplete="new-password" disabled={formDisabled || clearing} />
            </Form.Item>
            <Form.Item name="secret_key" label="SecretKey" extra="更新凭据时请同时填写两项">
              <Input.Password maxLength={200} autoComplete="new-password" disabled={formDisabled || clearing} />
            </Form.Item>
          </div>
          <Form.Item name="clear_credentials" valuePropName="checked">
            <Checkbox disabled={formDisabled || !data?.credentials_configured}>保存时清除凭据并停用</Checkbox>
          </Form.Item>
          <Form.Item label="连接测试样票" htmlFor="invoice-ocr-sample"
            extra="请选择 10 MB 以内的单页 JPG 或 PNG 脱敏样票。点击测试会把样票发送给腾讯云。">
            <input id="invoice-ocr-sample" ref={fileInput} type="file" accept="image/jpeg,image/png"
              style={{ maxWidth: "100%" }}
              disabled={formDisabled || clearing} onChange={(event) => {
                const file = event.currentTarget.files?.[0];
                setResult(null);
                if (file && file.size > 10 * 1024 * 1024) {
                  message.error("测试样票不能超过 10 MB");
                  event.currentTarget.value = "";
                  setSample(null);
                } else {
                  setSample(file || null);
                }
              }} />
          </Form.Item>
          {result && <Alert type={result.success ? "success" : "error"} showIcon message={result.message}
            description={<Space wrap><span>耗时 {(result.elapsed_ms / 1000).toFixed(2)} 秒</span>
              {result.request_id && <span>请求 ID {result.request_id}</span>}</Space>} />}
          <Space wrap>
            <Button loading={busy === "test"} disabled={formDisabled || !sample || clearing} onClick={() => void execute("test")}>测试样票</Button>
            <Button type="primary" htmlType="submit" icon={<SaveOutlined />} loading={busy === "save"}>保存 OCR 配置</Button>
          </Space>
        </Form>
      </ResourceState>
    </section>
  );
}
