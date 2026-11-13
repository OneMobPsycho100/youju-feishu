import { useState } from "react";
import { Alert, App as AntApp, Button, Form, Input, Modal, Select, Space } from "antd";
import { post, query, useResource } from "../api";
import { useWorkbench } from "../context";
import { requestId } from "../requestId";
import { budgetMoney, type BudgetOptions, type BudgetUsage } from "../budgetTypes";

/** 预算用量操作窗口冻结打开时的版本，数据刷新后由服务端明确拒绝旧版本。 */
function UsageMutationModal({ usage, kind, onClose, onChanged }: {
  usage: BudgetUsage; kind: "attribution" | "release"; onClose: () => void; onChanged?: () => void;
}) {
  const { companies, companyId, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [form] = Form.useForm();
  const selectedCompany = Form.useWatch("company_id", form) as string | undefined;
  const options = useResource<BudgetOptions>(query("/api/v1/budgets/options", { company_id: selectedCompany || usage.company_id || companyId }));
  const [busy, setBusy] = useState(false);
  const [key, setKey] = useState(requestId);
  const submit = async () => {
    const values = await form.validateFields();
    setBusy(true);
    try {
      await post(`/api/v1/approvals/${usage.approval_id}/budget-${kind}`, {
        expected_version: usage.version, request_id: key, reason: values.reason,
        ...(kind === "attribution" ? { company_id: values.company_id, department: values.departments[0] } : {}),
      });
      message.success(kind === "release" ? "冻结预算已整单释放，核查依据已保存" : "预算归属已确认");
      refresh(); onChanged?.(); onClose();
    } catch (reason) { message.error((reason as Error).message); }
    finally { setBusy(false); }
  };
  return <Modal open title={kind === "release" ? "释放冻结预算" : "确认整单预算归属"} onCancel={() => { if (!busy) onClose(); }}
    onOk={() => void submit()} confirmLoading={busy} cancelButtonProps={{ disabled: busy }} destroyOnHidden>
    <p><strong>{usage.serial_number}</strong> · {usage.applicant} · ¥{budgetMoney(usage.amount)}</p>
    <Alert showIcon type={kind === "release" ? "warning" : "info"} message={kind === "release"
      ? "请先核对资金情况；释放会恢复本单在所有匹配预算中的冻结额度。发票占用仍按台账规则管理。"
      : "整单计入下列主体和申请部门，所有匹配周期随之重新核算。确认依据将保留，不修改原审批表单。"} />
    <Form form={form} layout="vertical" disabled={busy} style={{ marginTop: 20 }} onValuesChange={() => setKey(requestId())}
      initialValues={{ company_id: usage.company_id || companyId || companies.find((item) => item.active)?.id,
        departments: usage.department ? [usage.department] : [], reason: "" }}>
      {kind === "attribution" && <>
        <Form.Item name="company_id" label="预算主体" rules={[{ required: true, message: "请选择费用承担主体" }]}>
          <Select showSearch optionFilterProp="label" options={companies.filter((item) => item.active).map((item) => ({ value: item.id, label: item.name }))} />
        </Form.Item>
        <Form.Item name="departments" label="申请部门" rules={[{ required: true, type: "array", len: 1, message: "请选择或输入一个部门" }]}>
          <Select mode="tags" maxCount={1} placeholder="选择或输入表单中的部门名称" loading={options.loading}
            options={options.data?.departments.map((value) => ({ value, label: value }))} />
        </Form.Item>
        {options.error && <Alert type="warning" message="部门选项暂不可用，仍可手动输入已核对的部门名称" />}
      </>}
      <Form.Item name="reason" label="核查依据" rules={[{ required: true, whitespace: true, min: 2, max: 600, message: "请填写2至600字的核查依据" }]}>
        <Input.TextArea rows={3} maxLength={600} showCount placeholder="填写已核对的归属或可以释放预算的原因" />
      </Form.Item>
    </Form>
  </Modal>;
}

/** 审批详情与预算明细复用操作入口，服务端继续校验实时角色。 */
export function BudgetUsageActions({ usage, onChanged }: { usage: BudgetUsage; onChanged?: () => void }) {
  const { user } = useWorkbench();
  const [operation, setOperation] = useState<{ kind: "attribution" | "release"; usage: BudgetUsage }>();
  if (user.role === "viewer") return null;
  return <>
    <Space size={4} wrap>
      <Button size="small" type="link" onClick={() => setOperation({ kind: "attribution", usage })}>确认归属</Button>
      {usage.state === "FROZEN" && <Button size="small" type="link" onClick={() => setOperation({ kind: "release", usage })}>核对并释放</Button>}
    </Space>
    {operation && <UsageMutationModal usage={operation.usage} kind={operation.kind} onClose={() => setOperation(undefined)} onChanged={onChanged} />}
  </>;
}
