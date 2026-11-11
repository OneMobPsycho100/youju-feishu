import { FileTextOutlined, LinkOutlined } from "@ant-design/icons";
import { Button, Space } from "antd";
import { paymentAccountText } from "../paymentDetails";
import type { FeishuFormField } from "../types";

function FormFields({ fields }: { fields: FeishuFormField[] }) {
  return (
    <dl className="feishu-form-fields">
      {fields.map((field, index) => (
        <div className={field.kind === "rows" || field.kind === "account" ? "feishu-form-wide" : undefined} key={index}>
          <dt>{field.label}</dt>
          <dd>
            {field.kind === "rows" && field.rows.length > 0 ? (
              <ol className="feishu-form-rows">
                {field.rows.map((row, rowIndex) => (
                  <li key={rowIndex}>
                    <span>第 {rowIndex + 1} 行</span>
                    {row.length ? <FormFields fields={row} /> : "—"}
                  </li>
                ))}
              </ol>
            ) : field.kind === "account" ? paymentAccountText(field.accounts) : field.text || "—"}
          </dd>
        </div>
      ))}
    </dl>
  );
}

/** 展示已同步的飞书报销表单。原件访问和飞书链接沿用详情抽屉的权限和入口。 */
export function FeishuApprovalForm({ fields, feishuUrl, onViewAttachments }: {
  fields: FeishuFormField[];
  /** 演示单据不传入飞书链接。 */
  feishuUrl?: string;
  /** 打开现有附件页签，缺少已同步原件时省略。 */
  onViewAttachments?: () => void;
}) {
  return (
    <section className="feishu-form" aria-label="飞书提交详情">
      <div className="feishu-form-heading">
        <h3>飞书提交详情</h3>
        <Space wrap>
          {onViewAttachments && <Button size="small" icon={<FileTextOutlined />} onClick={onViewAttachments}>查看发票与附件</Button>}
          {feishuUrl && <Button size="small" icon={<LinkOutlined />} href={feishuUrl} target="_blank" rel="noreferrer">打开飞书原审批</Button>}
        </Space>
      </div>
      {fields.length ? <FormFields fields={fields} /> : <p className="muted">暂无已同步的飞书表单</p>}
    </section>
  );
}
