import { Button, Table } from "antd";
import type { TableColumnsType } from "antd";
import type { Invoice, InvoiceTotals } from "../types";

export function invoiceAmount(value: string | null | undefined): string {
  if (value == null || value === "") return "—";
  // 后端以十进制字符串传输元金额，直接分组展示可保留大额票据的分位精度。
  const [whole, fraction = ""] = value.split(".");
  return `¥${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.padEnd(2, "0")}`;
}

interface DisplayField {
  key: string;
  label: string;
  width: number;
  value: (invoice: Invoice) => string | null | undefined;
  align?: "right";
  format?: "amount" | "rate";
  pending?: boolean;
  totalKey?: keyof InvoiceTotals;
}

// 审批和台账共用字段顺序、格式及待确认语义，金额汇总复用后端的按票去重结果。
const fields: DisplayField[] = [
  { key: "billing_type", label: "账单类型", width: 195,
    value: (row) => row.display_details?.billing_type || (row.kind === "E_INVOICE" ? "数电发票" : "传统发票") },
  { key: "expense_type", label: "消费类型", width: 115, value: (row) => row.display_details?.expense_type },
  { key: "amount", label: "价税合计", width: 125, value: (row) => row.amount, align: "right", format: "amount", totalKey: "amount" },
  { key: "tax_amount", label: "税额", width: 100, value: (row) => row.display_details?.tax_amount, align: "right", format: "amount", totalKey: "tax_amount" },
  { key: "untaxed_amount", label: "不含税金额", width: 125, value: (row) => row.display_details?.untaxed_amount, align: "right", format: "amount", totalKey: "untaxed_amount" },
  { key: "reimbursable_amount", label: "可报销金额", width: 125, value: (row) => row.display_details?.reimbursable_amount ?? row.amount,
    align: "right", format: "amount", totalKey: "amount" },
  { key: "deductible_tax_rate", label: "可抵扣税率（%）", width: 145, value: (row) => row.display_details?.deductible_tax_rate,
    align: "right", format: "rate", pending: true },
  { key: "deductible_tax_amount", label: "可抵扣税额", width: 125, value: (row) => row.display_details?.deductible_tax_amount,
    align: "right", format: "amount", pending: true, totalKey: "deductible_tax_amount" },
];

function displayValue(field: DisplayField, value: string | null | undefined): string {
  if (value == null || value === "") return field.pending ? "待确认" : "—";
  if (field.format === "amount") return invoiceAmount(value);
  return field.format === "rate" ? `${value}%` : value;
}

export const invoiceDisplayColumns: TableColumnsType<Invoice> = fields.map((field) => ({
  key: field.key,
  title: field.label,
  width: field.width,
  align: field.align,
  render: (_, invoice) => field.format === "amount"
    ? <span className="amount-text">{displayValue(field, field.value(invoice))}</span>
    : displayValue(field, field.value(invoice)),
}));

export function invoiceDisplayItems(invoice: Invoice) {
  return fields.map((field) => ({ key: field.key, label: field.label, children: displayValue(field, field.value(invoice)) }));
}

export function ApprovalInvoiceTable({ invoices, totals, onView }: {
  invoices: Invoice[];
  totals?: InvoiceTotals | null;
  onView: (invoiceId: string) => void;
}) {
  const identityColumns: TableColumnsType<Invoice> = [
    { title: "序号", key: "sequence", width: 65, render: (_, __, index) => index + 1 },
    { title: "发票号码", dataIndex: "number", width: 215, render: (value: string) => <span className="mono">{value}</span> },
    { title: "发票代码", dataIndex: "code", width: 130, render: (value: string) => value || "—" },
    { title: "开票日期", dataIndex: "issue_date", width: 115 },
  ];
  return (
    <section className="approval-invoice-summary" aria-label="已识别发票">
      <h4>已识别发票</h4>
      <Table<Invoice>
        rowKey="id"
        size="small"
        pagination={false}
        scroll={{ x: 1700 }}
        locale={{ emptyText: "暂无已识别发票" }}
        dataSource={invoices}
        columns={[
          ...identityColumns,
          ...invoiceDisplayColumns,
          { title: "操作", key: "actions", width: 140, fixed: "right",
            render: (_, invoice) => <Button type="link" onClick={() => onView(invoice.id)}>查看账单明细</Button> },
        ]}
        summary={totals ? () => (
          <Table.Summary.Row>
            <Table.Summary.Cell index={0} colSpan={identityColumns.length}><strong>合计</strong></Table.Summary.Cell>
            {fields.map((field, index) => (
              <Table.Summary.Cell key={field.key} index={identityColumns.length + index} align={field.align}>
                {field.totalKey ? <strong>{displayValue(field, totals[field.totalKey])}</strong> : field.align && "—"}
              </Table.Summary.Cell>
            ))}
            <Table.Summary.Cell index={identityColumns.length + fields.length} />
          </Table.Summary.Row>
        ) : undefined}
      />
    </section>
  );
}
