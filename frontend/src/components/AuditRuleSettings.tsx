import { Alert, Button, Checkbox, Form, Input, InputNumber, Select, Space, Switch, Tag } from "antd";
import type { FormInstance } from "antd";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { builtinStandards, ruleActionOptions } from "../auditRules";
import type { AuditRuleEntry } from "../types";
import { HelpTip } from "../ui";
import { requestId } from "../requestId";

/** 显式列表保存后才生效；删除及清空均保持原样，不重新补入预置值。 */
export function AuditRuleSettings({ form }: { form: FormInstance }) {
  const entries = Form.useWatch<AuditRuleEntry[]>(["rules", "entries"], form) || [];
  const conditions = entries.filter(item => item.enabled && item.on_failure !== "WARN");
  return <div className="audit-rule-settings">
    <Form.Item name={["rules", "policy_schema_version"]} hidden><InputNumber /></Form.Item>
    {!conditions.length && <Alert type="warning" showIcon message="当前没有启用的同意条件，审核将转人工，不会自动同意。" />}
    <div className="section-title-heading"><h4>业务规则</h4><HelpTip label="业务规则说明">所有启用的同意条件通过后才可同意；仅提示项不参与同意判定。发票占用、字段冲突及权限等执行约束始终有效。</HelpTip></div>
    <Form.List name={["rules", "entries"]}>
      {(fields, { add, remove }) => <>
        {fields.map(field => {
          const entry = entries[field.name];
          return <div className="custom-rule-card" key={field.key}>
            <Form.Item name={[field.name, "id"]} hidden><Input /></Form.Item>
            <Form.Item name={[field.name, "kind"]} hidden><Input /></Form.Item>
            <Form.Item name={[field.name, "builtin_code"]} hidden><Input /></Form.Item>
            <div className="custom-rule-heading">
              <Form.Item name={[field.name, "name"]} label="条件名称" rules={[{ required: true, whitespace: true }, { max: 100 }]}>
                <Input maxLength={100} />
              </Form.Item>
              <Form.Item name={[field.name, "enabled"]} label="状态" valuePropName="checked">
                <Switch checkedChildren="启用" unCheckedChildren="停用" />
              </Form.Item>
              <Form.Item name={[field.name, "on_failure"]} label="明确违规后" rules={[{ required: true }]}>
                <Select options={ruleActionOptions} />
              </Form.Item>
              <Button aria-label={`删除${entry?.name || "业务规则"}`} icon={<DeleteOutlined />} onClick={() => remove(field.name)} />
            </div>
            <Tag>{entry?.kind === "BUILTIN" ? "预置条件" : "自定义同意条件"}</Tag>
            {entry?.on_failure === "WARN" && <Tag>仅提示，不作为同意条件</Tag>}
            <Form.Item name={[field.name, "description"]} label="条件说明" rules={[{ required: true, whitespace: true }, { max: 2000 }]}>
              <Input.TextArea rows={2} maxLength={2000} showCount readOnly={entry?.kind === "BUILTIN"} />
            </Form.Item>
            {entry?.builtin_code === "AMOUNT" && <Form.Item name={[field.name, "parameters", "comparison"]} label="金额比较" initialValue="AT_LEAST">
              <Select options={[{ value: "AT_LEAST", label: "发票合计覆盖申请金额" }, { value: "EXACT", label: "发票合计等于申请金额" }]} />
            </Form.Item>}
            {entry?.builtin_code === "SEMANTIC" && <div className="rule-option">
              <Form.Item name={[field.name, "parameters", "require_completed"]} valuePropName="checked" initialValue={true} noStyle>
                <Checkbox>模型必须完成核对</Checkbox>
              </Form.Item>
              <HelpTip label="语义核对要求说明">勾选后，模型未配置或失败时转人工；取消后跳过不可用的语义核对。</HelpTip>
            </div>}
          </div>;
        })}
        <Space wrap>
          <Button icon={<PlusOutlined />} disabled={fields.length >= 40} onClick={() => add({ id: requestId(), kind: "CUSTOM", builtin_code: "", name: "", description: "", enabled: true, on_failure: "REVIEW", parameters: {} })}>新增同意条件</Button>
          <Select placeholder="添加预置条件" value={undefined} style={{ width: 200 }} disabled={fields.length >= 40}
            options={builtinStandards.filter(item => item.code !== "DUPLICATE" && !entries.some(entry => entry.builtin_code === item.code)).map(item => ({ value: item.code, label: item.name }))}
            onChange={code => { const item = builtinStandards.find(item => item.code === code)!; add({ id: requestId(), kind: "BUILTIN", builtin_code: code, name: item.name, description: item.description, enabled: true, on_failure: item.action, parameters: code === "SEMANTIC" ? { require_completed: true } : {} }); }} />
        </Space>
      </>}
    </Form.List>
    <Form.Item name={["rules", "amount_tolerance"]} label="金额舍入容差（元）" rules={[{ required: true }]}>
      <InputNumber stringMode min="0" max="1" step="0.01" />
    </Form.Item>
  </div>;
}
