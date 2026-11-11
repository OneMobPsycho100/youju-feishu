import { useState } from "react";
import { Alert, App, Button, Checkbox, Collapse, Descriptions, Drawer, Form, Input, Modal, Radio, Select, Space, Tag, Upload } from "antd";
import { DownloadOutlined, UploadOutlined } from "@ant-design/icons";
import { post } from "../api";
import { HelpTip } from "../ui";
import { editableRules } from "../auditRules";
import type { ApprovalTemplate, User } from "../types";
import type { TemplateBinding, TemplateBundle, TemplateImportResult, TemplatePreview, TemplatePreviewItem } from "../templateTransfer";
import { bindingIssues, bindingNodeIds, downloadTemplates, emptyFieldMapping, fieldLabel, scalarMappingFields } from "../templateTransfer";
import { AttachmentIdInput, ControlIdInput, NodeIdsInput, ReviewerIdInput } from "./TemplateDefinitionInputs";

interface TemplateTransferProps {
  templates: ApprovalTemplate[];
  users: User[];
  /** 提示词继承时展示目标环境当前有效的业务要求，文件不会覆盖全局配置。 */
  defaultPrompt?: string;
  paymentDefaultPrompt?: string;
  onImported: () => void;
}

/** 导入草稿只存在当前页面；预览与人工选择均不会提前保存或订阅。 */
export function TemplateTransferActions({ templates, users, defaultPrompt, paymentDefaultPrompt, onImported }: TemplateTransferProps) {
  const { message } = App.useApp();
  const [exportOpen, setExportOpen] = useState(false);
  const [exportCodes, setExportCodes] = useState<string[]>([]);
  const [open, setOpen] = useState(false);
  const [bundle, setBundle] = useState<TemplateBundle | null>(null);
  const [items, setItems] = useState<TemplatePreviewItem[]>([]);
  const [index, setIndex] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const source = bundle?.templates[index];
  const item = items[index];
  const binding = item?.binding;
  const selectedNodes = binding ? bindingNodeIds(binding) : [];
  const sourceNodeCount = source?.nodes?.length || (source?.node ? 1 : 0);
  const validNodes = selectedNodes.length > 0 && selectedNodes.every(id => item?.definition?.nodes.some(node => node.id === id));
  const issues = source && item ? bindingIssues(source, item) : [];
  const skipped = items.filter(row => !row.binding.selected || (row.conflict && row.binding.conflict_action === "SKIP")).length;
  const update = (patch: Partial<TemplateBinding>) => {
    setItems(rows => rows.map((row, position) => position === index ? { ...row, binding: { ...row.binding, ...patch } } : row));
    setError("");
  };
  const selectFile = async (file: File) => {
    if (file.size > 5 * 1024 * 1024) {
      message.error("配置文件不能超过 5 MB");
      return false;
    }
    setBusy(true);
    setError("");
    try {
      let parsed: TemplateBundle;
      try { parsed = JSON.parse(await file.text()) as TemplateBundle; }
      catch { throw new Error("文件不是有效的 JSON，请选择从本系统导出的模板配置文件"); }
      const result = await post<TemplatePreview>("/api/v1/templates/import/preview", { bundle: parsed });
      setBundle(parsed);
      setItems(result.items);
      setIndex(0);
    } catch (reason) {
      setError((reason as Error).message);
    } finally { setBusy(false); }
    return false;
  };
  const readTarget = async () => {
    if (!bundle || !binding?.target_reference.trim()) {
      setError("请先填写目标公司的模板编辑链接或 approval_code");
      return;
    }
    setBusy(true);
    setError("");
    try {
      // 只重建当前条目的匹配，避免重新读取一个模板时丢掉其他条目的人工选择。
      const result = await post<TemplatePreview>("/api/v1/templates/import/preview", { bundle, bindings: [binding] });
      const replacement = result.items.find(row => row.source_code === binding.source_code);
      if (!replacement) throw new Error("未取得目标模板预览，请重试");
      setItems(rows => rows.map((row, position) => position === index ? replacement : row));
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  };
  const submit = async () => {
    if (!bundle) return;
    const incomplete = items.findIndex((row, position) => bindingIssues(bundle.templates[position], row).length > 0);
    if (incomplete !== -1) {
      setIndex(incomplete);
      setError("请先处理此模板的待确认项，再导入配置");
      return;
    }
    setBusy(true);
    setError("");
    try {
      const result = await post<TemplateImportResult>("/api/v1/templates/import", { bundle, bindings: items.map(row => row.binding) });
      message.success("已导入 " + result.templates.length + " 个模板，跳过 " + result.skipped.length + " 个；导入的模板均未启用");
      setOpen(false);
      setBundle(null);
      setItems([]);
      onImported();
    } catch (reason) { setError((reason as Error).message); }
    finally { setBusy(false); }
  };
  const exportSelected = async () => {
    setBusy(true);
    try {
      await downloadTemplates(exportCodes);
      setExportOpen(false);
      message.success("模板配置已导出");
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  const currentPrompt = source?.agent_prompt || (source?.business_type === "PAYMENT" ? paymentDefaultPrompt : defaultPrompt);
  const currentRules = source ? editableRules(source.rules, source.business_type).entries || [] : [];
  const unavailable = busy || !binding?.selected;
  return <>
    <Space><Button icon={<DownloadOutlined />} disabled={!templates.length || busy} onClick={() => {
      setExportCodes(templates.map(template => template.code));
      setExportOpen(true);
    }}>导出配置</Button>
    <Button icon={<UploadOutlined />} disabled={busy} onClick={() => { setOpen(true); setError(""); }}>导入配置</Button></Space>
    <Modal title="导出审批模板配置" open={exportOpen} onCancel={() => !busy && setExportOpen(false)}
      onOk={() => void exportSelected()} confirmLoading={busy} okText="下载 JSON" okButtonProps={{ disabled: !exportCodes.length }}>
      <Checkbox checked={!!templates.length && exportCodes.length === templates.length}
        indeterminate={exportCodes.length > 0 && exportCodes.length < templates.length}
        disabled={busy} onChange={event => setExportCodes(event.target.checked ? templates.map(template => template.code) : [])}>全选</Checkbox>
      <Checkbox.Group className="template-export-list" value={exportCodes} disabled={busy}
        onChange={values => setExportCodes(values as string[])}>
        {templates.map(template => <Checkbox key={template.code} value={template.code}>
          {template.name}<small>{template.code}</small>
        </Checkbox>)}
      </Checkbox.Group>
    </Modal>
    <Drawer title="导入审批模板配置" open={open} width={1100} onClose={() => !busy && setOpen(false)}
      maskClosable={!busy} keyboard={!busy} className="template-transfer-drawer"
      footer={<div className="template-import-footer">
        <span className="muted">{bundle ? "共 " + items.length + " 个模板，计划导入 " + (items.length - skipped) + " 个，跳过 " + skipped + " 个" : "先上传配置文件，再绑定目标模板"}</span>
        <Space><Button disabled={busy} onClick={() => setOpen(false)}>取消</Button>
          <Button type="primary" disabled={!bundle || !items.length} loading={busy} onClick={() => void submit()}>确认导入配置</Button></Space>
      </div>}>
      <Alert type="info" showIcon message="导入后为未启用的观察模式，需重新订阅并验证" />
      <details className="template-setup-details"><summary>导入准备</summary>
        <p>先在目标公司创建飞书审批模板，再确认控件、预审节点和审批人。
          <a href="https://www.feishu.cn/approval/admin/approvalList?devMode=on" target="_blank" rel="noreferrer">打开飞书审批后台</a></p>
      </details>
      {error && <Alert className="template-transfer-notice" type="error" showIcon message={error} />}
      {!bundle ? <Upload.Dragger className="template-import-upload" accept=".json,application/json" disabled={busy} showUploadList={false}
        beforeUpload={file => selectFile(file)}>
        <p className="ant-upload-drag-icon"><UploadOutlined /></p>
        <p className="ant-upload-text">{busy ? "正在校验配置文件…" : "点击或拖入模板配置 JSON"}</p>
        <p className="ant-upload-hint">支持单个或批量模板，文件不超过 5 MB</p>
      </Upload.Dragger> : source && item && binding && <>
        <div className="template-transfer-toolbar">
          <Select aria-label="选择待导入模板" style={{ flex: 1, minWidth: 200 }} value={index} disabled={busy}
            onChange={value => { setIndex(value); setError(""); }}
            options={bundle.templates.map((template, position) => ({ value: position,
              label: (position + 1) + ". " + template.name + " · " + (template.business_type === "PAYMENT" ? "付款申请" : "费用报销")
                + (items[position].definition ? " · 已读取" : " · 待绑定") }))} />
          <Button disabled={busy} onClick={() => { setBundle(null); setItems([]); setError(""); }}>更换文件</Button>
        </div>
        <div className="template-transfer-toolbar">
          <Checkbox checked={binding.selected} disabled={busy} onChange={event => update({ selected: event.target.checked })}>导入此模板</Checkbox>
          <span className="muted">源模板：{source.name} · {source.source_code}</span>
        </div>
        <Form layout="vertical" disabled={unavailable} className="modal-form">
          <Form.Item label={<span className="field-label">目标飞书模板<HelpTip label="目标模板填写说明">在飞书后台编辑目标模板，复制含 definitionCode 的地址，或填写 approval_code。重新读取会更新匹配建议。</HelpTip></span>} htmlFor="template-import-reference" required>
            <Input.Search id="template-import-reference" value={binding.target_reference} enterButton="读取并匹配" loading={busy} disabled={unavailable}
              placeholder="粘贴目标公司模板编辑链接，或填写 approval_code"
              onChange={event => {
                const reference = event.target.value;
                // 更换目标立即清除旧定义、节点和映射，禁止把上一模板的选择带到新目标。
                setItems(rows => rows.map((row, position) => position === index ? {
                  ...row, definition: null, conflict: false, error: "", unresolved_fields: [],
                  binding: { ...row.binding, target_reference: reference, node_ids: [], node_id: "", node_confirmed: false,
                    reviewer_open_id: "", field_mapping: emptyFieldMapping(), cleared_fields: [],
                    attachment_scope_confirmed: false, definition_fingerprint: "", expected_revision: null, conflict_action: "SKIP" },
                } : row));
                setError("");
              }} onSearch={() => void readTarget()} />
          </Form.Item>
          {item.error && <Alert className="template-transfer-notice" type="error" showIcon message={item.error} />}
          {item.warnings.map((warning, position) => <Alert className="template-transfer-notice" type="warning" showIcon key={position} message={warning} />)}
          {item.conflict && <Form.Item label="目标模板已存在" required>
            <Radio.Group value={binding.conflict_action} onChange={event => update({ conflict_action: event.target.value })}>
              <Radio value="SKIP">跳过此模板</Radio><Radio value="OVERWRITE">覆盖配置，并让进行中的审批重新审核</Radio>
            </Radio.Group>
          </Form.Item>}
          {item.definition && <>
            <div className="template-transfer-notice"><Tag color="blue">{item.definition.name}</Tag>
              <span className="muted">已读取 {item.definition.fields.length} 个控件、{item.definition.nodes.length} 个审批节点</span></div>
            {item.definition.demo && <Alert type="warning" showIcon message="当前为演示定义，不代表真实公司的飞书接入能力。" />}
            <div className="form-two-columns">
              <Form.Item label="导入后的模板名称" htmlFor="template-import-name" required><Input id="template-import-name" value={binding.name} maxLength={150} onChange={event => update({ name: event.target.value })} /></Form.Item>
              <Form.Item label={<span className="field-label">目标应用审批人<HelpTip label="目标审批人说明">选择此应用授权的实际审批成员；未登录过的成员可手填 open_id。</HelpTip></span>} htmlFor="template-import-reviewer" required>
                <ReviewerIdInput id="template-import-reviewer" users={users} value={binding.reviewer_open_id} disabled={unavailable} onChange={value => update({ reviewer_open_id: value })} />
              </Form.Item>
            </div>
            <Form.Item label="目标专用预审节点" htmlFor="template-import-node" required
              extra={sourceNodeCount > 0 ? `源模板配置 ${sourceNodeCount} 个节点，当前已选 ${selectedNodes.length} 个` : undefined}>
              <NodeIdsInput id="template-import-node" definition={item.definition} value={selectedNodes} disabled={unavailable} allowManual={false}
                onChange={value => update({ node_ids: value, node_id: "", node_confirmed: false })} />
            </Form.Item>
            <Form.Item><Checkbox checked={binding.node_confirmed}
              disabled={unavailable || !validNodes}
              onChange={event => update({ node_confirmed: event.target.checked })}>确认所选节点为目标公司的专用预审节点</Checkbox></Form.Item>
            <h4>审批表单控件映射</h4>
            <div className="form-two-columns">
              {scalarMappingFields.filter(field => !field.paymentOnly || source.business_type === "PAYMENT" || source.field_mapping[field.key]).map(({ key, label }) => {
                const sourceId = source.field_mapping[key];
                const descriptor = source.fields.find(field => field.id === sourceId);
                const cleared = binding.cleared_fields.includes(key);
                return <Form.Item key={key} label={key === "company" ? source.business_type === "PAYMENT" ? "付款主体" : "报销主体" : label} htmlFor={"template-import-" + key} required={key === "amount"}
                  extra={<><div className="template-source-field">源：{descriptor ? fieldLabel(descriptor) : sourceId || "未映射"}</div>
                    {key !== "amount" && sourceId && <Checkbox checked={cleared} disabled={unavailable}
                      onChange={event => update({
                        field_mapping: { ...binding.field_mapping, [key]: "" },
                        cleared_fields: event.target.checked ? [...binding.cleared_fields.filter(value => value !== key), key] : binding.cleared_fields.filter(value => value !== key),
                      })}>明确清除此映射</Checkbox>}</>}>
                  <ControlIdInput id={"template-import-" + key} definition={item.definition} value={binding.field_mapping[key] || ""} disabled={unavailable || cleared}
                    onChange={value => update({ field_mapping: { ...binding.field_mapping, [key]: value || "" } })} />
                </Form.Item>;
              })}
            </div>
            <Form.Item label="目标票据附件控件" htmlFor="template-import-attachments" extra={<>
              <div className="template-source-field">源：{source.field_mapping.attachment_ids.length ? source.field_mapping.attachment_ids.map(id => {
                const field = source.fields.find(candidate => candidate.id === id);
                return field ? fieldLabel(field) : id;
              }).join("；") : "读取全部附件控件和补充附件"}</div>
              {source.field_mapping.attachment_ids.length > 0 && <Checkbox checked={binding.cleared_fields.includes("attachment_ids")} disabled={unavailable}
                onChange={event => update({ field_mapping: { ...binding.field_mapping, attachment_ids: [] }, attachment_scope_confirmed: false,
                  cleared_fields: event.target.checked ? [...binding.cleared_fields.filter(key => key !== "attachment_ids"), "attachment_ids"] : binding.cleared_fields.filter(key => key !== "attachment_ids") })}>
                明确改为读取全部附件控件和补充附件</Checkbox>}
            </>}>
              <AttachmentIdInput id="template-import-attachments" definition={item.definition} value={binding.field_mapping.attachment_ids}
                disabled={unavailable || binding.cleared_fields.includes("attachment_ids")}
                onChange={value => update({ field_mapping: { ...binding.field_mapping, attachment_ids: value }, attachment_scope_confirmed: false })} />
            </Form.Item>
            {binding.field_mapping.attachment_ids.length > 0 && binding.field_mapping.attachment_ids.length < source.field_mapping.attachment_ids.length
              && <Form.Item><Checkbox checked={binding.attachment_scope_confirmed} disabled={unavailable}
                onChange={event => update({ attachment_scope_confirmed: event.target.checked })}>已确认减少附件控件数量，采用当前选择的附件范围</Checkbox></Form.Item>}
          </>}
        </Form>
        <Collapse className="template-transfer-review" items={[{ key: "rules", label: "核对审核规则与提示词", children: <>
          <Descriptions size="small" column={2} items={[
            { key: "business", label: "业务类型", children: source.business_type === "PAYMENT" ? "付款申请" : "费用报销" },
            { key: "version", label: "规则版本", children: source.rules.version + "（保留原有规则格式）" },
            { key: "amount", label: "金额容差", children: source.rules.amount_tolerance + " 元" },
            { key: "prompt", label: "提示词来源", children: source.prompt_source === "OVERRIDE" ? "模板独立提示词" : "继承目标系统默认要求" },
          ]} />
          <div className="template-rule-preview">{currentRules.length ? currentRules.map(rule => <div key={rule.id}>
            <strong>{rule.name}</strong><Tag>{rule.enabled ? "启用" : "停用"}</Tag>
            <Tag>{({ REJECT: "拒绝", REVIEW: "人工复核", WARN: "仅提示" })[rule.on_failure]}</Tag>
            <p>{rule.description}</p>
          </div>) : <p>源模板没有业务规则，导入后保持为空。</p>}</div>
          <div className="agent-prompt-preview"><strong>{source.prompt_source === "OVERRIDE" ? "将导入的模板提示词" : "目标系统当前默认提示词"}</strong>
            <pre>{currentPrompt || "默认审核要求尚未读取，请到运行与模型中确认。"}</pre></div>
        </> }]} />
        {issues.length > 0 && <Alert className="template-transfer-notice" type="warning" showIcon message={"待处理：" + issues.join("、")} />}
      </>}
    </Drawer>
  </>;
}
