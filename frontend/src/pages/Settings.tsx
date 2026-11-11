import { useState } from "react";
import {
  App as AntApp,
  Alert,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
} from "antd";
import {
  CheckCircleFilled,
  CloudSyncOutlined,
  CodeOutlined,
  DownloadOutlined,
  EditOutlined,
  LinkOutlined,
  PlusOutlined,
  ReloadOutlined,
  SafetyCertificateOutlined,
} from "@ant-design/icons";
import { localTime, post, put, query, useResource } from "../api";
import { useWorkbench } from "../context";
import type {
  Activity,
  ApprovalTemplate,
  BusinessType,
  Company,
  FieldMapping,
  Job,
  ModelConfiguration as ModelConfig,
  Page,
  SystemInfo,
  User,
} from "../types";
import { HelpTip, ResourceState, SectionTitle, StatusTag } from "../ui";
import { ModelConfiguration } from "./ModelConfiguration";
import { AICallLogs } from "../components/AICallLogs";
import { AuditRuleSettings } from "../components/AuditRuleSettings";
import { SyncConfiguration } from "../components/SyncConfiguration";
import { OCRConfiguration } from "../components/OCRConfiguration";
import { defaultEntries, editableRules } from "../auditRules";
import type { ApprovalDefinition, TemplateInspection } from "../templateTransfer";
import { downloadTemplates, emptyFieldMapping, scalarMappingFields } from "../templateTransfer";
import { AttachmentIdInput, ControlIdInput, NodeIdsInput, ReviewerIdInput } from "../components/TemplateDefinitionInputs";
import { TemplateTransferActions } from "../components/TemplateTransfer";
import { FeishuSettings } from "../components/FeishuConfiguration";

export function Settings({ section }: { section?: string }) {
  const { navigate, user } = useWorkbench();
  if (user.role === "finance")
    return (
      <div className="surface settings-surface">
        <JobSettings />
      </div>
    );
  return (
    <div className="surface settings-surface">
      <Tabs
        activeKey={section || "companies"}
        onChange={(value) => navigate(`settings/${value}`)}
        items={[
          {
            key: "companies",
            label: "公司主体",
            children: <CompanySettings />,
          },
          {
            key: "templates",
            label: "审批接入与规则",
            children: <TemplateSettings />,
          },
          {
            key: "integration",
            label: "运行与模型",
            children: <IntegrationSettings />,
          },
          { key: "feishu", label: "飞书接入", children: <FeishuSettings /> },
          { key: "users", label: "用户权限", children: <UserSettings /> },
          { key: "jobs", label: "后台任务", children: <JobSettings /> },
          { key: "ai-logs", label: "AI 调用日志", children: <AICallLogs /> },
          { key: "logs", label: "审计日志", children: <LogSettings /> },
        ]}
      />
    </div>
  );
}

function CompanySettings() {
  const { companies, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [editing, setEditing] = useState<Company | "new" | null>(null);
  const [busy, setBusy] = useState(false);
  const [form] = Form.useForm();
  const open = (company: Company | "new") => {
    form.resetFields();
    form.setFieldsValue(
      company === "new"
        ? { active: true }
        : { ...company, aliases: company.aliases.join("，") },
    );
    setEditing(company);
  };
  const save = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setBusy(true);
    const data = {
      ...values,
      aliases: String(values.aliases || "")
        .split(/[,，\n]/)
        .map((x) => x.trim())
        .filter(Boolean),
    };
    try {
      if (editing === "new") await post("/api/v1/companies", data);
      else await put(`/api/v1/companies/${(editing as Company).id}`, data);
      message.success("主体配置已保存，待办将重新审核");
      setEditing(null);
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <SectionTitle
        title="公司主体"
        action={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => open("new")}
          >
            新增主体
          </Button>
        }
      />
      <Table<Company>
        rowKey="id"
        dataSource={companies}
        pagination={false}
        scroll={{ x: 1100 }}
        columns={[
          {
            title: "公司名称",
            dataIndex: "name",
            render: (value) => <strong>{value}</strong>,
          },
          {
            title: "购买方税号",
            dataIndex: "tax_id",
            render: (value) => <code>{value}</code>,
          },
          {
            title: "允许的名称别名",
            dataIndex: "aliases",
            render: (values: string[]) =>
              values.length ? (
                values.join("、")
              ) : (
                <span className="muted">未设置</span>
              ),
          },
          {
            title: "状态",
            dataIndex: "active",
            render: (value) => (
              <Tag color={value ? "success" : "default"}>
                {value ? "启用" : "停用"}
              </Tag>
            ),
          },
          {
            title: "操作",
            key: "edit",
            render: (_, row) => (
              <Button
                type="link"
                icon={<EditOutlined />}
                onClick={() => open(row)}
              >
                编辑
              </Button>
            ),
          },
        ]}
      />
      <Modal
        open={!!editing}
        title={editing === "new" ? "新增公司主体" : "编辑公司主体"}
        onCancel={() => !busy && setEditing(null)}
        onOk={() => void save()}
        confirmLoading={busy}
        okText="保存主体"
      >
        <Alert type="info" showIcon message="保存后将重新审核进行中的审批。" />
        <Form layout="vertical" form={form} className="modal-form">
          <Form.Item
            name="name"
            label="公司法定名称"
            rules={[{ required: true, min: 2, message: "请输入公司名称" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="tax_id"
            label="购买方税号"
            rules={[
              {
                required: true,
                pattern: /^[0-9A-Z]{15,20}$/,
                message: "请输入 15 至 20 位大写字母或数字税号",
              },
            ]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="aliases"
            label="曾用名及名称别名"
            extra="多个别名用中文逗号分隔"
          >
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item name="active" label="用于自动归类" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}

const capabilityLabels: Record<string, string> = {
  definition_read: "模板读取",
  details_read: "实例读取",
  files_downloaded: "原件下载",
  node_matched: "节点与身份",
  task_write: "待办回写",
};
function TemplateSettings() {
  const { tick, refresh, onJob, mode } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<ApprovalTemplate[]>("/api/v1/templates", tick);
  const modelResource = useResource<ModelConfig>("/api/v1/model-config", tick);
  const userResource = useResource<User[]>("/api/v1/users", tick);
  const [editing, setEditing] = useState<ApprovalTemplate | "new" | null>(null);
  const [connecting, setConnecting] = useState<ApprovalTemplate | null>(null);
  const [sample, setSample] = useState("");
  const [busy, setBusy] = useState(false);
  const [reference, setReference] = useState("");
  const [definition, setDefinition] = useState<ApprovalDefinition | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [inspectError, setInspectError] = useState("");
  const [inspection, setInspection] = useState<TemplateInspection | null>(null);
  const [identifiedReviewer, setIdentifiedReviewer] = useState<User | null>(null);
  const [businessChosen, setBusinessChosen] = useState(false);
  const [manualSetup, setManualSetup] = useState(false);
  const [inspectSample, setInspectSample] = useState("");
  const [loadedExisting, setLoadedExisting] = useState(false);
  const [adjusting, setAdjusting] = useState(true);
  const [form] = Form.useForm();
  const currentCode = Form.useWatch<string>("code", form);
  const currentDefinition = definition?.code === currentCode ? definition : null;
  const selectedNodes = Form.useWatch<string[]>("node_ids", form) || [];
  const selectedReviewer = Form.useWatch<string>("reviewer_open_id", form);
  const selectedAmount = Form.useWatch<string>(["field_mapping", "amount"], form);
  const selectedMapping = Form.useWatch<FieldMapping>("field_mapping", form);
  const selectedName = Form.useWatch<string>("name", form);
  const showTemplateForm = !!currentDefinition || editing !== "new" || manualSetup;
  const setupMissing = [!selectedAmount && "申请总额", !selectedNodes.length && "专用预审节点", !selectedReviewer && "专用审批人"].filter(Boolean);
  const nodeSummary = selectedNodes.map(id => {
    const node = currentDefinition?.nodes.find(candidate => id === candidate.id || id === candidate.custom_id);
    return node ? (node.name || "未命名节点") + " · " + id : id;
  }).join("、");
  const reviewerOptions = identifiedReviewer && !userResource.data?.some(user => user.id === identifiedReviewer.id)
    ? [...(userResource.data || []), identifiedReviewer] : userResource.data || [];
  const nodePrompt = Form.useWatch<string>("agent_prompt", form);
  const businessType = Form.useWatch<string>("business_type", form) || "REIMBURSEMENT";
  const visibleMappingFields = scalarMappingFields.filter(field => !field.paymentOnly || businessType === "PAYMENT");
  const primaryKeys = new Set(["amount", "expense_type", "description", "company", ...(businessType === "PAYMENT" ? ["payee_account"] : [])]);
  const renderMappingField = ({ key, label }: (typeof scalarMappingFields)[number]) => (
    <Form.Item key={key} name={["field_mapping", key]} label={key === "amount" ? "申请总额" : key === "company" ? businessType === "PAYMENT" ? "付款主体" : "报销主体" : label}
      extra={key === "company_tax_id" ? "仅映射真实税号；留空时仍按申报主体名称核对" : key === "company" ? "选择主体名称控件，按公司全称或允许的别名核对" : undefined}
      rules={key === "amount" ? [{ required: true, message: "请选择申请总额，不能使用明细中的单行金额" }] : []}>
      <ControlIdInput definition={currentDefinition} />
    </Form.Item>
  );
  const open = (template: ApprovalTemplate | "new") => {
    setReference(template === "new" ? "" : template.code);
    setDefinition(null);
    setInspectError("");
    setInspection(null);
    setIdentifiedReviewer(null);
    setBusinessChosen(false);
    setManualSetup(false);
    setInspectSample("");
    setLoadedExisting(false);
    setAdjusting(true);
    form.resetFields();
    form.setFieldsValue(
      template === "new"
        ? {
            active: true,
            business_type: "REIMBURSEMENT",
            node_ids: [],
            agent_prompt: "",
            rules: editableRules(),
            field_mapping: emptyFieldMapping(),
          }
        : {
            ...template,
            node_ids: template.node_ids ?? (template.node_id ? [template.node_id] : []),
            rules: editableRules(template.rules, template.business_type),
          },
    );
    setEditing(template);
  };
  const inspect = async (businessOverride?: BusinessType) => {
    if (!reference.trim()) {
      setInspectError("请粘贴飞书模板编辑链接，或填写 approval_code");
      return;
    }
    setInspecting(true);
    setInspectError("");
    try {
      const specifiedBusiness = businessOverride || (editing && editing !== "new" ? editing.business_type
        : businessChosen ? form.getFieldValue("business_type") : undefined);
      const sameTemplate = reference.trim() === form.getFieldValue("code");
      const result = await post<TemplateInspection>("/api/v1/templates/inspect", {
        reference, business_type: specifiedBusiness, sample_instance: inspectSample.trim(),
        node_ids: sameTemplate ? form.getFieldValue("node_ids") : undefined,
      });
      if (editing && editing !== "new" && result.definition.code !== editing.code)
        throw new Error("已有模板不能更换 approval_code，请通过添加模板接入另一个审批");
      if (editing === "new" && result.existing_template) {
        // 添加入口读到已接入模板时载入原配置，不能拿新模板默认规则覆盖已有规则。
        form.resetFields();
        form.setFieldsValue({ ...result.existing_template,
          rules: editableRules(result.existing_template.rules, result.existing_template.business_type) });
        setEditing(result.existing_template);
        setLoadedExisting(true);
      } else if (editing === "new") {
        const previousType = form.getFieldValue("business_type");
        form.setFieldsValue({ code: result.definition.code, business_type: result.business_type,
          name: form.getFieldValue("name") || result.definition.name });
        if (previousType !== result.business_type)
          form.setFieldValue("rules", editableRules(undefined, result.business_type));
      }
      // 首次识别填入唯一结果，已有配置或当前草稿带回完整节点集合。
      form.setFieldsValue({ field_mapping: result.field_mapping, node_ids: result.node_ids ?? (result.node_id ? [result.node_id] : []),
        reviewer_open_id: result.reviewer_open_id });
      if (result.reviewer_open_id) setIdentifiedReviewer({
        id: result.reviewer_open_id, name: result.reviewer_name || "已识别的专用审批人",
        role: "viewer", enabled: true,
      });
      else setIdentifiedReviewer(null);
      setDefinition(result.definition);
      setInspection(result);
      setAdjusting(result.unresolved_fields.length > 0);
      setReference(result.definition.code);
      message.success(result.unresolved_fields.length ? "已自动填入识别结果，请确认剩余必填项" : "字段、预审节点和审批人已自动填入，可直接保存");
    } catch (reason) { setInspectError((reason as Error).message); }
    finally { setInspecting(false); }
  };
  const exportOne = async (code: string) => {
    setBusy(true);
    try { await downloadTemplates([code]); message.success("模板配置已导出"); }
    catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  const save = async () => {
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setBusy(true);
    const data = {
      ...values,
      field_mapping: {
        ...values.field_mapping,
        attachment_ids: values.field_mapping?.attachment_ids || [],
      },
      mode: editing === "new" ? "OBSERVE" : (editing as ApprovalTemplate).mode,
    };
    try {
      const saved = await put<ApprovalTemplate>(`/api/v1/templates/${encodeURIComponent(values.code)}`, data);
      message.success(saved.ai_approval_enabled
        ? "模板配置已保存，待办将重新审核。节点、审批人或映射变化后进入观察模式"
        : "模板配置已保存，待办由财务整单人工复核");
      setEditing(null);
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const connect = async () => {
    setBusy(true);
    try {
      await post(
        query(`/api/v1/templates/${connecting!.code}/connect`, {
          sample_instance: sample,
        }),
      );
      message.success("审批定义已订阅，能力状态已更新");
      setConnecting(null);
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const toggleAIApproval = async (template: ApprovalTemplate, enabled: boolean) => {
    setBusy(true);
    try {
      await put(`/api/v1/templates/${encodeURIComponent(template.code)}/ai-approval`, { ai_approval_enabled: enabled });
      message.success(enabled ? "AI 审批已开启，当前为观察模式" : "AI 审批已关闭，待办由财务整单人工复核");
      await resource.reload();
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const toggle = async (template: ApprovalTemplate, checked: boolean) => {
    setBusy(true);
    try {
      await put(`/api/v1/templates/${template.code}`, {
        ...template,
        mode: checked ? "AUTO" : "OBSERVE",
      });
      message.success(checked ? "自动处理已开启，将按当前模板策略执行" : "已切换为观察模式");
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const sync = async () => {
    try {
      await onJob(await post<Job>("/api/v1/sync"), "增量补偿同步");
    } catch (reason) {
      message.error((reason as Error).message);
    }
  };
  return (
    <>
      <SectionTitle
        title="审批接入与规则"
        help="模板默认由财务人工预审。开启 AI 审批后进入观察模式。完成接入验证和一次人工回写后，可单独开启自动处理。"
        action={
          <Space wrap>
            <TemplateTransferActions templates={resource.data || []} users={userResource.data || []}
              defaultPrompt={modelResource.data?.default_prompt} paymentDefaultPrompt={modelResource.data?.payment_default_prompt}
              onImported={refresh} />
            <Button
              icon={<CloudSyncOutlined />}
              disabled={mode === "demo"}
              onClick={() => void sync()}
            >
              补偿同步
            </Button>
            <Button
              type="primary"
              icon={<PlusOutlined />}
              onClick={() => open("new")}
            >
              添加审批模板
            </Button>
          </Space>
        }
      />
      <ResourceState
        loading={resource.loading}
        error={resource.error}
        empty={!resource.loading && !resource.data?.length}
      >
        {resource.data?.map((template) => {
          const ready =
            template.subscribed &&
            Object.keys(capabilityLabels).every(
              (key) => template.capabilities[key],
            );
          return (
            <div className="template-card" key={template.code}>
              <div className="template-card-top">
                <div>
                  <h3>
                    {template.name}
                    {template.ai_approval_enabled
                      ? <Tag color={template.mode === "AUTO" ? "success" : "blue"}>
                        {template.mode === "AUTO" ? (template.rules.policy_schema_version || 1) >= 2 ? "按规则自动处理" : "低风险自动同意" : "观察模式"}
                      </Tag>
                      : <Tag>AI 审批已关闭</Tag>}
                    {!template.active && <Tag>已停用</Tag>}
                  </h3>
                  <code>{template.code}</code>
                </div>
                <Space>
                  <Button icon={<DownloadOutlined />} disabled={busy} onClick={() => void exportOne(template.code)}>导出</Button>
                  <Button
                    icon={<LinkOutlined />}
                    onClick={() => {
                      setConnecting(template);
                      setSample("");
                    }}
                  >
                    订阅并验证
                  </Button>
                  <Button
                    icon={<EditOutlined />}
                    onClick={() => open(template)}
                  >
                    配置
                  </Button>
                </Space>
              </div>
              <div className="capabilities-grid">
                {Object.entries(capabilityLabels).map(([key, label]) => (
                  <div
                    key={key}
                    className={template.capabilities[key] ? "verified" : ""}
                  >
                    <CheckCircleFilled />
                    <span>{label}</span>
                    <small>
                      {template.capabilities[key] ? "已验证" : "待验证"}
                    </small>
                  </div>
                ))}
              </div>
              <div className="template-card-bottom">
                <span className="muted">
                  规则 V{template.rules.version} · 专用审批人{" "}
                  <code>{template.reviewer_open_id}</code>
                  {" · "}{template.agent_prompt?.trim() ? "独立节点提示词" : template.business_type === "PAYMENT" ? "付款审核默认要求" : "继承全局提示词"}
                  {template.rules.policy_schema_version !== 3 && " · 保存配置后启用可删除规则列表"}
                </span>
                <Space wrap size={16}>
                  <Space>
                    <span>开启 AI 审批</span>
                    <Switch aria-label="开启 AI 审批" checked={template.ai_approval_enabled}
                      disabled={busy} onChange={(checked) => void toggleAIApproval(template, checked)} />
                  </Space>
                  <Space>
                    <span>{(template.rules.policy_schema_version || 1) >= 2 ? "自动同意 / 按规则拒绝" : "自动同意低风险预审"}</span>
                    <Switch
                      aria-label="开启自动处理"
                      checked={template.ai_approval_enabled && template.mode === "AUTO"}
                      disabled={busy || !template.ai_approval_enabled || (!ready && template.mode !== "AUTO")}
                      onChange={(checked) => void toggle(template, checked)}
                    />
                  </Space>
                </Space>
              </div>
              {template.ai_approval_enabled && !ready && (
                <p className="template-ready-note">
                  先在观察模式完成人工回写，再开启自动处理。
                </p>
              )}
            </div>
          );
        })}
      </ResourceState>
      <Modal
        open={!!editing}
        width={960}
        centered
        styles={{ body: { maxHeight: "70dvh", overflowY: "auto", paddingRight: 8 } }}
        title={editing === "new" ? "添加飞书审批模板" : "配置模板与预审规则"}
        onCancel={() => !busy && !inspecting && setEditing(null)}
        onOk={() => void (showTemplateForm ? save() : inspect())}
        confirmLoading={busy}
        okButtonProps={{ disabled: inspecting }}
        okText={showTemplateForm ? "保存配置" : "识别并继续"}
      >
        <Form form={form} layout="vertical" className="modal-form" disabled={busy || inspecting}>
          <Form.Item label={<span className="field-label">飞书审批模板<HelpTip label="模板链接获取方式">在<a href="https://www.feishu.cn/approval/admin/approvalList?devMode=on" target="_blank" rel="noreferrer">飞书审批后台</a>编辑目标模板，复制浏览器地址；也可填写模板编码。</HelpTip></span>}>
            <Input.Search value={reference} onChange={event => setReference(event.target.value)} disabled={busy || inspecting}
              enterButton={currentDefinition ? "重新识别" : "识别并填入"} loading={inspecting}
              onSearch={() => void inspect()} placeholder="粘贴飞书模板编辑链接，或填写模板编码" />
          </Form.Item>
          {inspectError && <Alert className="template-transfer-notice" type="error" showIcon message={inspectError} />}
          {!showTemplateForm && <Button type="link" size="small" onClick={() => setManualSetup(true)}>手工配置</Button>}
          {inspection && currentDefinition && <Alert className="template-transfer-notice" type={setupMissing.length ? "info" : "success"} showIcon
            message={setupMissing.length ? "已自动填入识别结果，只需补充：" + setupMissing.join("、") : "字段、预审节点和审批人均已就绪，可直接保存"}
            description={currentDefinition.name} />}
          {loadedExisting && <Alert className="template-transfer-notice" type="info" showIcon message="此模板已接入，已载入原配置和审核规则，保存将更新该模板。" />}
          {inspection?.notices.map((notice, position) => <Alert key={position} className="template-transfer-notice" type="info" showIcon message={notice} />)}
          <div hidden={!showTemplateForm}>
            {inspection && currentDefinition && <Descriptions className="template-recognition-summary" size="small" column={2} items={[
              { key: "business", label: "业务类型", children: businessType === "PAYMENT" ? "付款申请" : "费用报销" },
              { key: "name", label: "模板名称", children: selectedName },
              { key: "node", label: "预审节点", children: nodeSummary ? <span style={{ overflowWrap: "anywhere" }}>{nodeSummary}</span> : "待选择" },
              { key: "reviewer", label: "专用审批人", children: reviewerOptions.find(user => user.id === selectedReviewer)?.name || (selectedReviewer ? "已指定成员" : "待选择") },
              ...visibleMappingFields.filter(field => primaryKeys.has(field.key)).map(({ key, label }) => {
                const field = currentDefinition.fields.find(candidate => candidate.id === selectedMapping?.[key]);
                return { key, label: key === "amount" ? "申请总额" : key === "company" ? businessType === "PAYMENT" ? "付款主体" : "报销主体" : label,
                  children: field ? (field.path.length ? field.path.join(" / ") + " / " : "") + field.name : "未映射（可按需调整）" };
              }),
              { key: "files", label: "附件", children: selectedMapping?.attachment_ids?.length ? "读取指定的附件范围" : "全部附件和补充附件" },
            ]} />}
            <details className="template-setup-details" open={adjusting} onToggle={event => setAdjusting(event.currentTarget.open)}>
              <summary>调整识别结果（业务类型、节点、审批人与字段）</summary>
            <div className="form-two-columns">
              <Form.Item name="business_type" label="业务类型" rules={[{ required: true }]}>
                <Select disabled={editing !== "new"} options={[{ value: "REIMBURSEMENT", label: "费用报销" }, { value: "PAYMENT", label: "付款申请" }]}
                  onChange={(value: BusinessType) => {
                    setBusinessChosen(true);
                    form.setFieldValue(["rules", "entries"], defaultEntries(value));
                    if (currentDefinition) void inspect(value);
                  }} />
              </Form.Item>
              <Form.Item name="name" label="模板名称" rules={[{ required: true }]}><Input /></Form.Item>
              <Form.Item name="node_ids" label={<span className="field-label">专用预审节点<HelpTip label="预审节点说明">可选择不同分支的多个节点，所有节点共用下方的专用审批人。</HelpTip></span>}
                rules={[{ type: "array", required: true, min: 1, message: "请至少选择一个专用预审节点" },
                  { type: "array", defaultField: { type: "string", min: 1, max: 100, message: "节点 ID 需要 1 至 100 个字符" } }]}>
                <NodeIdsInput definition={currentDefinition} />
              </Form.Item>
              <Form.Item name="reviewer_open_id" label="专用审批人" rules={[{ required: true, message: "请按姓名选择专用审批人" }]}
                extra={inspection?.reviewer_source === "PENDING_TASK" ? "已从所选预审节点的真实待办识别"
                  : inspection?.reviewer_source === "SAVED" ? "已沿用当前模板配置" : "选择实际负责所选预审节点的同一名成员"}>
                <ReviewerIdInput users={reviewerOptions} />
              </Form.Item>
            </div>
            <h4>已识别的业务字段</h4>
            <div className="form-two-columns">
              {visibleMappingFields.filter(field => primaryKeys.has(field.key)).map(renderMappingField)}
            </div>
            </details>
            <details className="template-setup-details">
              <summary>更多字段与手工配置</summary>
              <div className="form-two-columns">
                {visibleMappingFields.filter(field => !primaryKeys.has(field.key)).map(renderMappingField)}
              </div>
              <Form.Item name={["field_mapping", "attachment_ids"]} label={<span className="field-label">附件范围<HelpTip label="附件范围说明">留空读取全部附件和补充附件；需要限制范围时再指定控件。</HelpTip></span>}>
                <AttachmentIdInput definition={currentDefinition} />
              </Form.Item>
              <Form.Item name="code" label="飞书模板编码" rules={[{ required: true }]}><Input disabled={editing !== "new"} /></Form.Item>
              <Form.Item label={<span className="field-label">从审批单识别专用审批人<HelpTip label="审批样本说明">填写当前停留在任一已选预审节点的审批 instance_code，再点击重新识别。</HelpTip></span>}>
                <Input value={inspectSample} maxLength={100} onChange={event => setInspectSample(event.target.value)}
                  placeholder="有现成的预审待办时可提供样本，无需创建测试审批" />
              </Form.Item>
            </details>
            <details className="template-setup-details">
              <summary>审核规则与提示词</summary>
          <h4>预审规则</h4>
          {editing && editing !== "new" && editing.rules.policy_schema_version !== 3 && (
            <Alert type="info" showIcon message="旧配置已转换为编辑草稿；保存后启用可删除的业务规则列表，并重新审核进行中的审批。" />
          )}
          <Form.Item
            name={["rules", "version"]}
            label="规则版本"
            rules={[{ required: true }]}
          >
            <Input />
          </Form.Item>
          {/* 保留已有模板的舍入配置随表单提交，简化界面时不能将历史值重置为默认值。 */}
          <AuditRuleSettings form={form} />
          <Form.Item
            name={["rules", "require_semantic"]}
            valuePropName="checked"
            hidden
          >
            <Checkbox>
              必须完成模型语义核对；模型未配置或失败时进入人工复核
            </Checkbox>
          </Form.Item>
          <h4>审核提示词</h4>
          <Form.Item name="agent_prompt" extra={businessType === "PAYMENT" ? "留空使用付款审核默认要求" : "留空继承全局默认"}>
            <Input.TextArea rows={5} maxLength={20000} showCount placeholder="可填写此审核节点的关注点、判断标准与说明风格" />
          </Form.Item>
          <div className="agent-prompt-preview">
            <strong>{nodePrompt?.trim() ? "当前使用节点覆盖" : businessType === "PAYMENT" ? "当前使用付款审核默认要求" : "当前继承全局默认"}</strong>
            {modelResource.error ? <Alert type="error" message={modelResource.error} />
              : <pre>{nodePrompt?.trim() || (businessType === "PAYMENT" ? modelResource.data?.payment_default_prompt : modelResource.data?.default_prompt) || "正在读取审核要求…"}</pre>}
            <details><summary>查看系统固定追加的约束</summary><pre>{modelResource.data?.system_constraints}</pre></details>
          </div>

            </details>
            <Form.Item name="active" valuePropName="checked"><Checkbox>启用此模板</Checkbox></Form.Item>
          </div>
        </Form>
      </Modal>
      <Modal
        open={!!connecting}
        title="订阅审批定义并验证读取能力"
        onCancel={() => !busy && setConnecting(null)}
        onOk={() => void connect()}
        confirmLoading={busy}
        okText="订阅并验证"
      >
        <p>
          先在飞书应用中配置加密事件回调与审批权限，再为此模板订阅原生审批事件。
        </p>
        <Input
          value={sample}
          onChange={(e) => setSample(e.target.value)}
          placeholder="可选：真实 instance_code，用于验证实例及附件读取"
        />
        <Alert
          className="history-heading"
          showIcon
          type="info"
          message="待办回写能力只在实际同意或拒绝成功后标记，不能通过配置直接声明成功。"
        />
      </Modal>
    </>
  );
}

function IntegrationSettings() {
  const { tick } = useWorkbench();
  const resource = useResource<SystemInfo>("/api/v1/system", tick);
  const data = resource.data;
  return (
    <ResourceState loading={resource.loading} error={resource.error}>
      <SectionTitle
        title="运行与模型"
      />
      <div className="integration-grid">
        {[
          {
            title: "飞书审批",
            ready: data?.feishu_configured,
            text:
              data?.mode === "demo"
                ? "演示环境"
                : "",
          },
          {
            title: "腾讯云发票 OCR",
            ready: data?.ocr_state === "READY",
            failed: data?.ocr_state === "FAILED",
            status: ({ DISABLED: "未启用", UNVERIFIED: "已启用 · 尚未调用", READY: "最近调用成功", FAILED: "识别失败" })[data?.ocr_state || "DISABLED"],
            text:
              data?.ocr_last_error || (data?.ocr_last_elapsed_ms != null
                ? `最近耗时 ${(data.ocr_last_elapsed_ms / 1000).toFixed(1)} 秒` : ""),
          },
          {
            title: "审核模型",
            ready: data?.model_configured,
            text: data?.model_configured
              ? data.model_name
              : "",
          },
        ].map((item) => (
          <div className="integration-card" key={item.title}>
            <SafetyCertificateOutlined />
            <h3>{item.title}</h3>
            <Tag color={item.ready ? "success" : item.failed ? "error" : "default"}>
              {item.status || (item.ready ? "已配置" : "尚未配置")}
            </Tag>
            {item.text && <p>{item.text}</p>}
          </div>
        ))}
      </div>
      <SyncConfiguration />
      <OCRConfiguration />
      <ModelConfiguration />
      <details className="template-setup-details"><summary>部署状态</summary>
      <Descriptions
        bordered
        column={2}
        size="small"
        items={[
          {
            key: "mode",
            label: "业务模式",
            children: data?.mode === "demo" ? "独立演示" : "企业真实接入",
          },
          {
            key: "worker",
            label: "后台处理",
            children: "API 内置持久化任务处理器",
          },
          { key: "database", label: "业务数据库", children: data?.database === "sqlite" ? "SQLite" : "加载中" },
          { key: "store", label: "原件存储", children: data?.storage },
          {
            key: "verify",
            label: "税务验真",
            children: "未接入",
          },
          {
            key: "url",
            label: "工作台地址",
            children: data?.public_url,
            span: 2,
          },
        ]}
      />
      </details>
      <details className="config-help">
        <summary>
          <CodeOutlined />
          部署配置
        </summary>
        <p>
          同步、腾讯云发票 OCR 和模型在上方配置。飞书凭据在「飞书接入」配置。
        </p>
        <pre>
          MODEL_BASE_URL=https://your-model-service.example/v1{`\n`}
          MODEL_NAME=your-model{`\n`}MODEL_API_KEY=通过部署环境注入
        </pre>
        <p>
          PDF 发票优先本地解析，缺失或歧义字段由当前多模态模型读取原件图像兜底；普通 PDF 正文在本地读取。
          模型未启用、字段不清晰或分歧未解决时转人工；图片发票仍使用腾讯云与多模态交叉核对。
        </p>
      </details>
    </ResourceState>
  );
}

function UserSettings() {
  const { tick, user, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const resource = useResource<User[]>("/api/v1/users", tick);
  const change = async (row: User, role: User["role"], enabled: boolean) => {
    try {
      await put(`/api/v1/users/${row.id}`, { role, enabled });
      message.success("用户权限已更新");
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    }
  };
  return (
    <>
      <SectionTitle
        title="工作台用户与权限"
        help="新用户首次登录后默认只读，可在此调整权限。"
      />
      <ResourceState error={resource.error}>
        <Table<User>
          rowKey="id"
          loading={resource.loading}
          dataSource={resource.data}
          pagination={false}
          scroll={{ x: 1000 }}
          columns={[
            {
              title: "姓名",
              dataIndex: "name",
              render: (name, row) => (
                <strong>
                  {name}
                  {row.id === user.id && (
                    <Tag className="subtle-tag">当前账号</Tag>
                  )}
                </strong>
              ),
            },
            {
              title: "身份标识",
              dataIndex: "id",
              render: (value) => <code>{value}</code>,
            },
            {
              title: "角色",
              dataIndex: "role",
              render: (_, row) => (
                <Select
                  value={row.role}
                  style={{ width: 135 }}
                  disabled={row.id === user.id}
                  onChange={(role) => void change(row, role, row.enabled)}
                  options={[
                    { value: "admin", label: "管理员" },
                    { value: "finance", label: "财务人员" },
                    { value: "viewer", label: "只读用户" },
                  ]}
                />
              ),
            },
            {
              title: "允许登录",
              dataIndex: "enabled",
              render: (_, row) => (
                <Switch
                  checked={row.enabled}
                  disabled={row.id === user.id}
                  onChange={(enabled) => void change(row, row.role, enabled)}
                />
              ),
            },
          ]}
        />
      </ResourceState>
    </>
  );
}

function JobSettings() {
  const { tick, onJob, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [status, setStatus] = useState("");
  const [page, setPage] = useState(1);
  const resource = useResource<Page<Job>>(
    query("/api/v1/jobs", { status, page }),
    tick,
  );
  const names: Record<string, string> = {
    AUDIT: "发票预审",
    ACTION: "飞书待办回写",
    ARCHIVE: "原件归档",
    EVENT: "审批事件处理",
    SYNC: "增量补偿同步",
    REFRESH: "撤销原单状态对账",
    IMPORT: "历史台账导入",
    REIMBURSEMENT_METADATA: "报销统计元数据补齐",
    APPROVAL_APPLICANT_NAME: "申请人姓名补齐",
  };
  const retry = async (job: Job) => {
    try {
      await onJob(
        await post<Job>(`/api/v1/jobs/${job.id}/retry`),
        "重试后台任务",
      );
    } catch (reason) {
      message.error((reason as Error).message);
    }
  };
  return (
    <>
      <SectionTitle
        title="后台任务"
        help="回写结果未知时，先核对飞书中的审批状态，再决定是否重试。"
        action={
          <Space>
            <Select
              allowClear
              placeholder="任务状态"
              style={{ width: 130 }}
              value={status || undefined}
              onChange={(value) => {
                setStatus(value || "");
                setPage(1);
              }}
              options={[
                { value: "FAILED", label: "失败" },
                { value: "PENDING", label: "等待执行" },
                { value: "RUNNING", label: "执行中" },
                { value: "SUCCEEDED", label: "已完成" },
              ]}
            />
            <Button icon={<ReloadOutlined />} onClick={refresh}>
              刷新
            </Button>
          </Space>
        }
      />
      <ResourceState error={resource.error}>
        <Table<Job>
          rowKey="id"
          scroll={{ x: 1250 }}
          loading={resource.loading}
          dataSource={resource.data?.items}
          pagination={{
            current: page,
            total: resource.data?.total,
            pageSize: 20,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: "任务",
              dataIndex: "kind",
              render: (_, row) => (
                <div className="cell-stack">
                  <strong>{names[row.kind] || row.kind}</strong>
                  <code className="muted">{row.id.slice(0, 16)}</code>
                </div>
              ),
            },
            {
              title: "状态",
              dataIndex: "status",
              render: (value) => <StatusTag value={value} />,
            },
            { title: "执行次数", dataIndex: "attempts", width: 110 },
            {
              title: "失败原因",
              dataIndex: "error",
              width: 330,
              render: (value) => value || <span className="muted">—</span>,
            },
            {
              title: "创建时间",
              dataIndex: "created_at",
              render: (value) => localTime(value),
            },
            {
              title: "操作",
              key: "retry",
              render: (_, row) =>
                row.status === "FAILED" ? (
                  <Button type="link" onClick={() => void retry(row)}>
                    重试任务
                  </Button>
                ) : (
                  <span className="muted">—</span>
                ),
            },
          ]}
        />
      </ResourceState>
    </>
  );
}

function LogSettings() {
  const { tick } = useWorkbench();
  const [page, setPage] = useState(1);
  const [selected, setSelected] = useState<Activity | null>(null);
  const resource = useResource<Page<Activity>>(
    query("/api/v1/logs", { page }),
    tick,
  );
  return (
    <>
      <SectionTitle
        title="审计日志"
      />
      <ResourceState error={resource.error}>
        <Table<Activity>
          rowKey="id"
          scroll={{ x: 1350 }}
          loading={resource.loading}
          dataSource={resource.data?.items}
          pagination={{
            current: page,
            total: resource.data?.total,
            pageSize: 20,
            showSizeChanger: false,
            onChange: setPage,
          }}
          columns={[
            {
              title: "时间",
              dataIndex: "created_at",
              render: (value) => localTime(value),
            },
            {
              title: "操作者",
              dataIndex: "actor",
              render: (value) => <strong>{value}</strong>,
            },
            { title: "操作", dataIndex: "action" },
            { title: "对象", dataIndex: "target" },
            {
              title: "详情",
              key: "details",
              render: (_, row) => (
                <Button type="link" onClick={() => setSelected(row)}>
                  查看留痕
                </Button>
              ),
            },
          ]}
        />
      </ResourceState>
      <Drawer
        open={!!selected}
        onClose={() => setSelected(null)}
        title="操作审计详情"
        width={620}
      >
        {selected && (
          <>
            <Descriptions
              column={1}
              bordered
              items={[
                { key: "actor", label: "操作者", children: selected.actor },
                { key: "action", label: "操作", children: selected.action },
                { key: "target", label: "对象", children: selected.target },
                {
                  key: "time",
                  label: "时间",
                  children: localTime(selected.created_at),
                },
              ]}
            />
            <div className="manifest-preview">
              <pre>{JSON.stringify(selected.detail || {}, null, 2)}</pre>
            </div>
          </>
        )}
      </Drawer>
    </>
  );
}
