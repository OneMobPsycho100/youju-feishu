import { useEffect, useState } from "react";
import {
  App as AntApp,
  Alert,
  Button,
  Descriptions,
  Drawer,
  Form,
  Input,
  Modal,
  Result,
  Select,
  Space,
  Steps,
  Table,
  Tag,
  Upload,
} from "antd";
import {
  DownloadOutlined,
  FileExcelOutlined,
  InboxOutlined,
  LinkOutlined,
  SearchOutlined,
  UploadOutlined,
} from "@ant-design/icons";
import { api, localTime, money, post, query, useResource } from "../api";
import { invoiceDisplayColumns, invoiceDisplayItems } from "../components/InvoiceDetails";
import { useAgentPageContext, useWorkbench } from "../context";
import type { ImportBatch, Invoice, Job, Page } from "../types";
import { CompanyName, ResourceState, StatusTag } from "../ui";

const importFields: [string, string][] = [
  ["number", "发票号码"],
  ["code", "发票代码（传统票必填）"],
  ["buyer_name", "购买方名称"],
  ["buyer_tax_id", "购买方税号"],
  ["seller_name", "销售方名称"],
  ["issue_date", "开票日期"],
  ["amount", "价税合计"],
  ["source_label", "历史审批编号"],
];

export function Invoices() {
  const { tick, companyId, user, navigate, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [q, setQ] = useState("");
  const [page, setPage] = useState(1);
  useEffect(() => {
    setPage(1);
  }, [companyId]);
  const [importOpen, setImportOpen] = useState(false);
  const [selected, setSelected] = useState<Invoice | null>(null);
  useEffect(() => { setSelected(null); }, [companyId, q]);
  useAgentPageContext({ selection_type: "invoices", selected_ids: selected ? [selected.id] : [], filters: { q } });
  const [releaseOpen, setReleaseOpen] = useState(false);
  const [releaseForm] = Form.useForm();
  const resource = useResource<Page<Invoice>>(
    query("/api/v1/invoices", { company_id: companyId, q, page }),
    tick,
  );
  const release = async () => {
    const values = await releaseForm.validateFields().catch(() => null);
    if (!values) return;
    try {
      await post(`/api/v1/claims/${selected!.claim!.id}/release`, {
        ...values,
        payment_and_bookkeeping_checked: true,
      });
      message.success("冻结占用已释放，核查依据已留存");
      setReleaseOpen(false);
      setSelected(null);
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    }
  };
  return (
    <>
      <div className="surface table-surface invoice-ledger">
        <div className="table-toolbar">
          <Input.Search
            allowClear
            placeholder="搜索票号、购买方或销售方"
            prefix={<SearchOutlined />}
            onSearch={(value) => {
              setQ(value);
              setPage(1);
            }}
            style={{ width: 340 }}
          />
          <Space>
            <Tag className="subtle-tag">共 {resource.data?.total || 0} 张</Tag>
            {user.role !== "viewer" && (
              <Button
                icon={<UploadOutlined />}
                onClick={() => setImportOpen(true)}
              >
                导入历史台账
              </Button>
            )}
            <Button
              type="primary"
              icon={<DownloadOutlined />}
              href={query("/api/v1/invoices/export", { company_id: companyId })}
            >
              导出台账
            </Button>
          </Space>
        </div>
        <ResourceState error={resource.error}>
          <Table<Invoice>
            rowKey="id"
            loading={resource.loading}
            dataSource={resource.data?.items}
            scroll={{ x: 2620 }}
            pagination={{
              current: page,
              total: resource.data?.total,
              pageSize: 20,
              showSizeChanger: false,
              onChange: setPage,
              showTotal: (total) => `共 ${total} 张发票`,
            }}
            columns={[
              {
                title: "序号",
                key: "sequence",
                width: 65,
                render: (_, __, index) => (page - 1) * (resource.data?.page_size ?? 20) + index + 1,
              },
              {
                title: "发票号码",
                dataIndex: "number",
                width: 220,
                render: (_, row) => (
                  <button
                    className="table-title-link"
                    onClick={() => setSelected(row)}
                  >
                    <strong className="mono">{row.number}</strong>
                  </button>
                ),
              },
              {
                title: "发票代码",
                dataIndex: "code",
                width: 130,
                render: (value: string) => value || "—",
              },
              {
                title: "开票日期",
                dataIndex: "issue_date",
                width: 120,
                render: (value) => <span className="muted">{value}</span>,
              },
              ...invoiceDisplayColumns,
              {
                title: "购买方主体",
                dataIndex: "company_id",
                width: 230,
                render: (_, row) => (
                  <div className="cell-stack">
                    <CompanyName id={row.company_id} compact />
                    <small className="mono muted">{row.buyer_tax_id}</small>
                  </div>
                ),
              },
              {
                title: "销售方",
                dataIndex: "seller_name",
                width: 240,
                ellipsis: true,
              },
              {
                title: "占用 / 来源",
                dataIndex: "claim",
                width: 205,
                render: (_, row) => (
                  <div className="cell-stack">
                    {row.claim ? (
                      <>
                        <StatusTag value={row.claim.status} />
                        <small>{row.claim.source_label}</small>
                      </>
                    ) : (
                      <span className="muted">未占用</span>
                    )}
                  </div>
                ),
              },
              {
                title: "数据来源",
                dataIndex: "historical",
                width: 120,
                render: (value) => (
                  <Tag className="subtle-tag">
                    {value ? "历史台账" : "审批采集"}
                  </Tag>
                ),
              },
              {
                title: "操作",
                key: "actions",
                fixed: "right",
                width: 130,
                render: (_, row) => (
                  <Button type="link" onClick={() => setSelected(row)}>
                    查看记录
                  </Button>
                ),
              },
            ]}
          />
        </ResourceState>
      </div>
      <Drawer
        open={!!selected}
        width={620}
        title="发票使用记录"
        onClose={() => setSelected(null)}
      >
        {selected && (
          <>
            <div className="invoice-detail-number">
              <span>发票号码</span>
              <strong className="mono">{selected.number}</strong>
              <Tag>未验真</Tag>
            </div>
            <Descriptions
              column={1}
              bordered
              size="small"
              items={[
                {
                  key: "code",
                  label: "发票代码",
                  children: selected.code || "—",
                },
                {
                  key: "buyer",
                  label: "购买方",
                  children: selected.buyer_name,
                },
                {
                  key: "tax",
                  label: "购买方税号",
                  children: <code>{selected.buyer_tax_id}</code>,
                },
                {
                  key: "seller",
                  label: "销售方",
                  children: selected.seller_name || "—",
                },
                {
                  key: "date",
                  label: "开票日期",
                  children: selected.issue_date,
                },
                ...invoiceDisplayItems(selected),
                {
                  key: "created",
                  label: "建档时间",
                  children: localTime(selected.created_at),
                },
                {
                  key: "source",
                  label: "票据来源",
                  children: selected.historical
                    ? "历史台账导入"
                    : "飞书审批采集",
                },
              ]}
            />
            <h4 className="history-heading">当前有效占用</h4>
            {selected.claim ? (
              <div className="claim-card">
                <StatusTag value={selected.claim.status} />
                <h4>{selected.claim.source_label}</h4>
                {selected.claim.approval_id && (
                  <Button
                    type="link"
                    icon={<LinkOutlined />}
                    onClick={() =>
                      navigate(`approvals/${selected.claim!.approval_id}`)
                    }
                  >
                    打开关联审批
                  </Button>
                )}
                {selected.claim.status === "FROZEN" &&
                  user.role === "admin" && (
                    <Button
                      danger
                      onClick={() => {
                        releaseForm.resetFields();
                        setReleaseOpen(true);
                      }}
                    >
                      核查后释放冻结占用
                    </Button>
                  )}
              </div>
            ) : (
              <Alert type="info" message="该发票当前没有有效占用" />
            )}
            {selected.attachment_id && (
              <Button
                className="history-heading"
                icon={<DownloadOutlined />}
                href={`/api/v1/attachments/${selected.attachment_id}/download`}
              >
                下载关联原件
              </Button>
            )}
            <Alert
              className="history-heading"
              showIcon
              type="info"
              message="票面解析与税务验真是不同能力。当前未接入税务验真服务，审批已用也不代表已付款或已记账。"
            />
          </>
        )}
      </Drawer>
      <Modal
        open={releaseOpen}
        title="核查并释放冻结占用"
        onCancel={() => setReleaseOpen(false)}
        onOk={() => void release()}
        okText="已核查付款与入账，确认释放"
        okButtonProps={{ danger: true }}
      >
        <Alert
          type="warning"
          showIcon
          message="仅在核查该笔费用的付款与入账状态、确认允许再次报销后执行。操作依据将永久留痕。"
        />
        <Form form={releaseForm} layout="vertical" className="modal-form">
          <Form.Item
            name="comment"
            label="核查依据"
            rules={[
              {
                required: true,
                min: 5,
                message: "请填写至少 5 个字的核查依据",
              },
            ]}
          >
            <Input.TextArea rows={4} />
          </Form.Item>
        </Form>
      </Modal>
      <ImportDialog open={importOpen} onClose={() => setImportOpen(false)} />
    </>
  );
}

function ImportDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { onJob, refresh } = useWorkbench();
  const { message } = AntApp.useApp();
  const [batch, setBatch] = useState<ImportBatch | null>(null);
  const [mapping, setMapping] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const upload = async (file: File) => {
    setBusy(true);
    try {
      const form = new FormData();
      form.append("file", file);
      const result = await api<ImportBatch>("/api/v1/imports", {
        method: "POST",
        body: form,
      });
      setBatch(result);
      setMapping(result.mapping);
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
    return false;
  };
  const preview = async () => {
    setBusy(true);
    try {
      setBatch(
        await post<ImportBatch>(
          `/api/v1/imports/${batch!.id}/preview`,
          mapping,
        ),
      );
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    setBusy(true);
    try {
      const job = await post<Job>(`/api/v1/imports/${batch!.id}/confirm`);
      await onJob(job, "导入历史台账");
      setBatch(await api<ImportBatch>(`/api/v1/imports/${batch!.id}`));
      refresh();
    } catch (reason) {
      message.error((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const step =
    batch?.status === "COMPLETED"
      ? 3
      : batch?.status === "PREVIEWED"
        ? 2
        : batch
          ? 1
          : 0;
  return (
    <Modal
      open={open}
      width={900}
      title="导入历史发票台账"
      onCancel={() => !busy && onClose()}
      footer={
        <Space>
          <Button disabled={busy} onClick={onClose}>
            关闭
          </Button>
          {batch && (
            <Button disabled={busy} onClick={() => setBatch(null)}>
              选择其他文件
            </Button>
          )}
          {step === 1 && (
            <Button
              type="primary"
              loading={busy}
              onClick={() => void preview()}
            >
              校验并预览
            </Button>
          )}
          {step === 2 && (
            <>
              <Button
                disabled={busy}
                onClick={() => setBatch({ ...batch!, status: "UPLOADED" })}
              >
                调整映射
              </Button>
              <Button
                type="primary"
                loading={busy}
                disabled={!batch?.valid_count}
                onClick={() => void confirm()}
              >
                确认导入 {batch?.valid_count} 行有效数据
              </Button>
            </>
          )}
          {batch?.errors.length ? (
            <Button
              icon={<DownloadOutlined />}
              href={`/api/v1/imports/${batch.id}/errors`}
            >
              下载错误报告
            </Button>
          ) : null}
        </Space>
      }
    >
      <Steps
        current={step}
        size="small"
        className="import-steps"
        items={[
          { title: "上传台账" },
          { title: "字段映射" },
          { title: "校验预览" },
          { title: "导入结果" },
        ]}
      />
      {!batch && (
        <>
          <Alert
            showIcon
            type="info"
            message="发票号码、代码和税号请在 Excel 中以文本保存，避免前导零丢失和长号码精度损失。有效历史记录将占用发票，参与全企业查重。"
          />
          <div className="import-upload">
            <Upload.Dragger
              accept=".csv,.xlsx"
              maxCount={1}
              showUploadList={false}
              beforeUpload={(file) => {
                void upload(file);
                return false;
              }}
              disabled={busy}
            >
              <p className="ant-upload-drag-icon">
                <InboxOutlined />
              </p>
              <p className="ant-upload-text">
                {busy ? "正在读取台账…" : "点击或拖拽 CSV / XLSX 台账到这里"}
              </p>
              <p className="ant-upload-hint">
                单批最多 10,000 行，文件不超过 20 MB
              </p>
            </Upload.Dragger>
          </div>
          <Button icon={<DownloadOutlined />} href="/api/v1/import-template">
            下载标准 Excel 模板
          </Button>
        </>
      )}
      {batch && step === 1 && (
        <>
          <div className="import-file-summary">
            <FileExcelOutlined />
            <strong>{batch.filename}</strong>
            <span>
              {batch.row_count} 行 · {batch.columns.length} 列
            </span>
          </div>
          <div className="form-two-columns import-mapping">
            {importFields.map(([key, title]) => (
              <div key={key}>
                <label>{title}</label>
                <Select
                  allowClear
                  placeholder="选择对应的台账列"
                  value={mapping[key] || undefined}
                  onChange={(value) =>
                    setMapping((previous) => ({
                      ...previous,
                      [key]: value || "",
                    }))
                  }
                  options={batch.columns.map((column) => ({
                    value: column,
                    label: column,
                  }))}
                  style={{ width: "100%" }}
                />
              </div>
            ))}
          </div>
        </>
      )}
      {batch && step === 2 && (
        <>
          <Alert
            showIcon
            type={batch.errors.length ? "warning" : "success"}
            message={`${batch.valid_count} 行通过校验，${batch.errors.length} 行需要修正；确认后仅导入有效行。`}
          />
          <Table
            size="small"
            rowKey="row"
            className="import-preview-table"
            dataSource={batch.preview}
            scroll={{ x: 880, y: 280 }}
            pagination={false}
            columns={[
              { title: "原行号", dataIndex: "row", width: 90 },
              { title: "发票号码", dataIndex: "number", width: 260 },
              { title: "购买方", dataIndex: "buyer_name", width: 230 },
              {
                title: "价税合计",
                dataIndex: "amount",
                width: 150,
                render: (value) => `¥${money(value)}`,
              },
              { title: "开票日期", dataIndex: "issue_date", width: 150 },
            ]}
          />
          {batch.valid_count > 100 && (
            <p className="muted">仅展示前 100 行预览，确认时处理全部有效行。</p>
          )}
        </>
      )}
      {batch && step === 3 && (
        <Result
          status={batch.imported ? "success" : "warning"}
          title={`已导入 ${batch.imported} 张历史发票`}
          subTitle={`${batch.errors.length} 行未导入。既有发票及有效占用不会被历史台账覆盖。`}
        />
      )}
      {batch && step >= 2 && batch.errors.length > 0 && (
        <div className="import-errors">
          <h4>逐行错误</h4>
          {batch.errors.slice(0, 20).map((error, index) => (
            <div key={index}>
              <Tag color="orange">第 {error.row} 行</Tag>
              {error.error}
            </div>
          ))}
          {batch.errors.length > 20 && <p>其余错误请下载完整报告。</p>}
        </div>
      )}
    </Modal>
  );
}
