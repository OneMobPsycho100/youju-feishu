import { useEffect, useRef, useState } from "react";
import { Alert, App as AntApp, Button, Form, InputNumber, Select, Space, Switch, Tag } from "antd";
import { SaveOutlined } from "@ant-design/icons";
import { localTime, put, useResource } from "../api";
import { useWorkbench } from "../context";
import type { SyncConfiguration as SyncConfig } from "../types";
import { HelpTip, ResourceState } from "../ui";

/** 编辑时允许选择秒、分钟或小时；提交接口统一转换为整数秒。 */
interface SyncFormValues {
  enabled: boolean;
  interval: number;
  unit: number;
}

export function SyncConfiguration() {
  const { tick, refresh, mode } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<SyncConfig>("/api/v1/sync-config", tick);
  const [form] = Form.useForm<SyncFormValues>();
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const loadedVersion = useRef<number | null>(null);
  const dirty = useRef(false);
  const unit = Form.useWatch("unit", form) || 60;
  const data = resource.data;

  const fill = (config: SyncConfig) => {
    const intervalUnit = config.interval_seconds % 3600 === 0 ? 3600 : config.interval_seconds % 60 === 0 ? 60 : 1;
    form.setFieldsValue({ enabled: config.enabled, interval: config.interval_seconds / intervalUnit, unit: intervalUnit });
    loadedVersion.current = config.version;
    dirty.current = false;
    setStale(false);
  };

  useEffect(() => {
    if (!data || data.version === loadedVersion.current) return;
    // 定时刷新只更新展示状态，不能覆盖管理员尚未保存的草稿。
    if (loadedVersion.current !== null && dirty.current) setStale(true);
    else fill(data);
  }, [data, form]);

  const save = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values || loadedVersion.current === null) return;
    setBusy(true);
    try {
      const config = await put<SyncConfig>("/api/v1/sync-config", {
        version: loadedVersion.current, enabled: values.enabled, interval_seconds: values.interval * values.unit,
      });
      fill(config);
      message.success(config.enabled ? "自动同步配置已保存" : "自动同步已关闭，仍可在审批列表手动同步");
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
      void resource.reload();
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="model-configuration">
      <div className="model-config-heading">
        <h3>自动同步</h3>
        {data && <Space wrap>
          <Tag color={mode === "live" && data.enabled ? "success" : "default"}>
            {mode === "demo" ? "演示环境不执行" : data.enabled ? "自动同步已开启" : "自动同步已关闭"}
          </Tag>
          <Tag>{data.source === "database" ? "工作台配置" : "部署环境配置"}</Tag>
        </Space>}
      </div>
      <ResourceState loading={resource.loading} error={resource.error}>
        {stale && <Alert type="warning" showIcon message="配置已被其他管理员修改，请重新加载后保存。"
          action={<Button size="small" disabled={busy} onClick={() => data && fill(data)}>重新加载</Button>} />}
        <Form name="sync-config" form={form} layout="vertical" disabled={busy || stale} onValuesChange={() => { dirty.current = true; }}>
          <Form.Item name="enabled" label={<span className="field-label">启用自动同步<HelpTip label="自动同步开关说明">关闭后跳过未开始的自动同步；运行中的任务继续完成，飞书事件与手动同步照常处理。</HelpTip></span>} valuePropName="checked">
            <Switch />
          </Form.Item>
          <div className="form-two-columns">
            <Form.Item name="interval" label={<span className="field-label">同步间隔<HelpTip label="同步间隔范围">默认 5 分钟，可设置 1 秒至 24 小时。</HelpTip></span>} dependencies={["unit"]}
              rules={[{ required: true, message: "请输入同步间隔" }, { validator: (_, value) => {
                const seconds = value * form.getFieldValue("unit");
                return Number.isInteger(seconds) && seconds >= 1 && seconds <= 86400
                  ? Promise.resolve() : Promise.reject(new Error("同步间隔需为 1 秒至 24 小时，且为整数秒"));
              } }]}>
              <InputNumber min={1} max={86400 / unit} precision={0} style={{ width: "100%" }} />
            </Form.Item>
            <Form.Item name="unit" label="时间单位" rules={[{ required: true, message: "请选择时间单位" }]}>
              <Select options={[{ value: 1, label: "秒" }, { value: 60, label: "分钟" }, { value: 3600, label: "小时" }]} />
            </Form.Item>
          </div>
          <div className="model-config-actions">
            <Button type="primary" icon={<SaveOutlined />} loading={busy} onClick={() => void save()}>保存同步配置</Button>
            <span className="muted">{mode === "demo" ? "演示环境不执行同步"
              : data?.enabled ? `下次同步：${localTime(data.next_run_at || undefined)}` : "自动同步已关闭"}</span>
          </div>
        </Form>
      </ResourceState>
    </section>
  );
}
