import { useEffect, useState } from "react";
import { DeleteOutlined, PlusOutlined } from "@ant-design/icons";
import { Alert, App as AntApp, Button, Col, Form, Input, Modal, Row, Select } from "antd";
import { put, useResource } from "../api";
import type { Company, InvoiceDeductionConfiguration, InvoiceDeductionRule } from "../types";

const modes = [
  { value: "FULL", label: "按票面全额抵扣" },
  { value: "NONE", label: "明确不抵扣" },
];

export function InvoiceDeductionRules({ company, onClose, onSaved }: {
  company: Company;
  onClose: () => void;
  onSaved: () => void;
}) {
  const path = `/api/v1/companies/${encodeURIComponent(company.id)}/invoice-deduction-rules`;
  const { data, error, loading, reload } = useResource<InvoiceDeductionConfiguration>(path);
  const { message } = AntApp.useApp();
  const [form] = Form.useForm<{ rules: InvoiceDeductionRule[] }>();
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (data) form.setFieldsValue({ rules: data.rules });
  }, [data, form]);

  const save = async () => {
    if (!data || saving) return;
    const values = await form.validateFields().catch(() => null);
    if (!values) return;
    setSaving(true);
    try {
      await put<InvoiceDeductionConfiguration>(path, { expected_version: data.version, rules: values.rules });
      message.success("抵扣规则已保存");
      onSaved();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open
      title={`${company.name} · 抵扣规则`}
      width={820}
      loading={loading}
      onCancel={() => !saving && onClose()}
      onOk={() => void save()}
      okText="保存规则"
      okButtonProps={{ disabled: !data || !!error }}
      confirmLoading={saving}
    >
      {error ? (
        <Alert type="error" showIcon message={error}
          action={<Button size="small" onClick={() => void reload()}>重新加载</Button>} />
      ) : (
        <Form form={form} layout="vertical" disabled={saving} className="modal-form">
          <Form.List name="rules">
            {(fields, { add, remove }) => (
              <>
                {fields.map((field) => (
                  <Row key={field.key} gutter={12} align="bottom">
                    <Col xs={24} sm={9}>
                      <Form.Item name={[field.name, "billing_type"]} label="票种"
                        rules={[{ required: true, message: "请选择细分票种" }]}>
                        <Select options={data?.billing_types.map((value) => ({ value, label: value }))} />
                      </Form.Item>
                    </Col>
                    <Col xs={24} sm={7}>
                      <Form.Item name={[field.name, "expense_type"]} label="费用类型"
                        rules={[{ required: true, whitespace: true, message: "请输入审批中的费用类型" }]}>
                        <Input maxLength={80} placeholder="与审批中的费用类型一致" />
                      </Form.Item>
                    </Col>
                    <Col xs={20} sm={6}>
                      <Form.Item name={[field.name, "mode"]} label="抵扣方式"
                        rules={[{ required: true, message: "请选择抵扣方式" }]}>
                        <Select options={modes} />
                      </Form.Item>
                    </Col>
                    <Col xs={4} sm={2}>
                      <Form.Item>
                        <Button aria-label={`删除第 ${field.name + 1} 条规则`} icon={<DeleteOutlined />}
                          onClick={() => remove(field.name)} />
                      </Form.Item>
                    </Col>
                  </Row>
                ))}
                <Button icon={<PlusOutlined />} onClick={() => add()}>添加规则</Button>
              </>
            )}
          </Form.List>
        </Form>
      )}
    </Modal>
  );
}
