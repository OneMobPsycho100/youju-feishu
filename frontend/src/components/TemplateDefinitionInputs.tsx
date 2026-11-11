import { useState } from "react";
import { Button, Input, Select, Tooltip } from "antd";
import type { ApprovalDefinition } from "../templateTransfer";
import { fieldLabel, fieldName } from "../templateTransfer";
import type { User } from "../types";

interface IdInputProps {
  value?: string;
  onChange?: (value: string) => void;
  definition?: ApprovalDefinition | null;
  disabled?: boolean;
  id?: string;
}
interface NamedOption {
  value: string;
  /** 下拉包含类型、明细路径和 ID，便于处理同名控件。 */
  label: string;
  /** 已选结果只展示业务名称，用户无需理解 ID。 */
  title: string;
}
interface NamedIdInputProps extends Omit<IdInputProps, "definition"> {
  options: NamedOption[];
  placeholder: string;
}

function NamedIdInput({ options, placeholder, ...props }: NamedIdInputProps) {
  const [manual, setManual] = useState(false);
  const showManual = manual || !options.length;
  return <div className="template-id-input"><div className="template-id-row">
    {showManual ? <Input {...props} onChange={event => props.onChange?.(event.target.value)} allowClear placeholder="手工填写 ID，或先识别模板" />
      : <Select id={props.id} disabled={props.disabled} style={{ width: "100%" }} showSearch allowClear
        value={props.value || undefined} onChange={value => props.onChange?.(value || "")}
        placeholder={placeholder} options={options} optionFilterProp="label" optionLabelProp="title"
        classNames={{ popup: { root: "template-control-options" } }} />}
    {options.length > 0 && <Tooltip title={showManual ? "返回按名称选择" : "高级：查看或手工填写原始 ID"}>
      <Button size="small" type="text" disabled={props.disabled} onClick={() => setManual(!showManual)}>
        {showManual ? "选择" : "手工"}
      </Button>
    </Tooltip>}
  </div></div>;
}

/** 按业务名称选择，值始终为真实控件 ID；手工入口保留已有配置能力。 */
export function ControlIdInput({ definition, ...props }: IdInputProps) {
  return <NamedIdInput {...props} placeholder="选择对应的表单字段"
    options={definition?.fields.map(field => ({ value: field.id, label: fieldLabel(field), title: fieldName(field) })) || []} />;
}

interface IdsInputProps extends Omit<IdInputProps, "value" | "onChange"> {
  value?: string[];
  onChange?: (value: string[]) => void;
}

const parseNodeLines = (text: string) => Array.from(new Set(text.split(/\r?\n/).map(line => line.trim()).filter(Boolean)));

/** 配置可手填 ID，迁移只能选目标定义中的节点。显示旧自定义 ID 时保留原值。 */
export function NodeIdsInput({ definition, value = [], onChange, allowManual = true, ...props }: IdsInputProps & { allowManual?: boolean }) {
  const [manual, setManual] = useState(false);
  const [draft, setDraft] = useState("");
  const nodes = definition?.nodes || [];
  const options = nodes.map(node => {
    const name = node.name || "未命名节点";
    const sameName = nodes.some(other => other.id !== node.id && other.name === node.name);
    return {
      value: node.custom_id && value.includes(node.custom_id) && !value.includes(node.id) ? node.custom_id : node.id,
      label: name + " · " + node.id + (node.custom_id ? " · 自定义 " + node.custom_id : ""),
      title: sameName ? name + " · " + node.id.slice(-8) : name,
    };
  });
  const showManual = allowManual && (manual || !options.length);
  const savedText = value.join("\n");
  // 表单值保存规范化 ID，文本草稿保留用户正在输入的空行和末尾换行。
  const manualText = parseNodeLines(draft).join("\n") === savedText ? draft : savedText;
  return <div className="template-id-input"><div className="template-id-row">
    {showManual ? <Input.TextArea {...props} value={manualText} autoSize={{ minRows: 2, maxRows: 6 }}
        placeholder="每行填写一个专用预审节点 ID" onChange={event => {
          setDraft(event.target.value);
          onChange?.(parseNodeLines(event.target.value));
        }} />
      : <Select {...props} style={{ width: "100%" }} mode="multiple" showSearch allowClear value={value} onChange={onChange}
        placeholder="选择一个或多个专用预审节点" options={options} optionFilterProp="label" optionLabelProp="title"
        classNames={{ popup: { root: "template-control-options" } }} />}
    {allowManual && options.length > 0 && <Tooltip title={showManual ? "返回按名称选择" : "逐行填写节点 ID"}>
      <Button size="small" type="text" disabled={props.disabled} onClick={() => setManual(!showManual)}>
        {showManual ? "选择" : "手工"}
      </Button>
    </Tooltip>}
  </div></div>;
}

interface ReviewerInputProps extends Omit<IdInputProps, "definition"> {
  users: User[];
}

/** 当前工作台用户和已识别成员按姓名展示；未登录过的成员仍能手填本应用 open_id。 */
export function ReviewerIdInput({ users, ...props }: ReviewerInputProps) {
  return <NamedIdInput {...props} placeholder="按姓名选择专用审批人"
    options={users.filter(user => user.enabled).map(user => ({ value: user.id, label: user.name + " · " + user.id, title: user.name }))} />;
}

/** 默认读取全部附件，无需逐个填写 ID；迁移时改变源范围仍需要明确确认。 */
export function AttachmentIdInput({ definition, ...props }: IdsInputProps) {
  return <Select {...props} style={{ width: "100%" }} mode="tags" allowClear tokenSeparators={[",", "，"]}
    placeholder="留空读取全部附件"
    classNames={{ popup: { root: "template-control-options" } }} optionFilterProp="label" optionLabelProp="title"
    options={definition?.fields.filter(field => ["attachment", "attachmentv2", "image", "imagev2"].includes(field.type.toLowerCase()))
      .map(field => ({ value: field.id, label: fieldLabel(field), title: fieldName(field) })) || []} />;
}
