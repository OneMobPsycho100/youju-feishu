import { Collapse, Table, Tag, Typography } from "antd";
import type { ParsedData, RecognitionComparison } from "../types";

const sourceLabels: Record<string, string> = {
  TEXT: "PDF 文字层", TENCENT_OCR: "腾讯云发票 OCR", OCR: "本地 OCR", MODEL: "多模态", XML: "结构化 XML", OFD: "OFD 文本",
};

/** 人工确认优先展示，分类、PDF 本地解析与图片 OCR 主识别分别说明。 */
export function recognitionLabel(data: ParsedData): string {
  if (data.archive) {
    if (data.archive.status === "FAILED") return "压缩包 · 待核对";
    return data.archive.mode === "REFERENCE" ? "历史参考包 · 已展开" : "压缩包 · 已展开";
  }
  if (data.reference_pdf === "TRAVEL") return "网约车行程单 · 已读取";
  if (data.reference_pdf === "REVIEW") return "PDF 票面待核对";
  if (data.confirmed) return "人工确认";
  if (data.parse_status === "SKIPPED") return "普通附件 · 已分类";
  if (data.classification && !data.classification.complete) return "附件类别待确认";
  if (data.material_type && data.material_type !== "INVOICE") {
    const names = { CONTRACT: "合同", QUOTE: "报价单", OTHER: "其他材料", UNKNOWN: "待确认材料" };
    return names[data.material_type] + (data.material?.complete ? " · 已读取" : " · 待核对");
  }
  if (data.recognition?.status === "OCR") return "发票 OCR 识别完成";
  if (data.recognition?.status === "LOCAL") return "PDF 本地解析完成";
  if (data.recognition?.status === "VERIFIED") return data.file_type === ".pdf" && data.sources?.MODEL
    ? "PDF 模型兜底完成" : "交叉核对通过";
  if (data.recognition?.status === "STRUCTURED") return "结构化字段完整";
  return "识别待确认";
}

export function InvoiceRecognition({ data }: { data: ParsedData }) {
  if (data.archive) return null;
  const classification = data.classification;
  const firstPage = classification?.scope === "FIRST_PAGE";
  const classificationLabel = classification?.source === "LOCAL" ? "本地分类"
    : classification?.source === "MANUAL" ? "人工分类" : firstPage ? "首页 AI 分类" : "模型内容分类";
  const classificationEvidence = classification && <div className="invoice-classification">
    <strong>{classificationLabel}{classification.cached ? " · 已复用" : ""}</strong>
    <p>{classification.reason}</p>
    {!!classification.pages.length && <small>{firstPage ? "分类依据：首页" : `分类依据：第 ${classification.pages.join("、")} 页`}
      {!!classification.invoice_pages?.length && `；发票位于第 ${classification.invoice_pages.join("、")} 页`}
      {classification.invoice_count != null && `；不同发票 ${classification.invoice_count} 张`}</small>}
  </div>;
  if (data.parse_status === "SKIPPED" || !data.recognition) return classificationEvidence || <Typography.Paragraph type="secondary">{data.skip_reason}</Typography.Paragraph>;
  const sources = Object.entries(data.sources || {});
  return (
    <Collapse
      className="invoice-recognition"
      size="small"
      items={[{
        key: "recognition",
        label: <span>{recognitionLabel(data)} · 查看识别依据</span>,
        children: <>
          {classificationEvidence}
          <p>{data.confirmed ? "已人工确认" : data.recognition.reason}</p>
          <div className="recognition-sources">
            {sources.map(([name, source]) => <div key={name}>
              <strong>{sourceLabels[name] || name}</strong>
              <Tag color={source.status === "COMPLETED" ? "success" : "warning"}>
                {source.status === "COMPLETED" ? source.cached ? "已复用" : "已提取"
                  : source.status === "EMPTY" ? name === "TEXT" ? "无文字层" : "无有效字段"
                  : source.status === "SKIPPED" ? "已跳过" : "未完成"}
              </Tag>
              {source.elapsed_ms !== undefined && <small>{(source.elapsed_ms / 1000).toFixed(1)} 秒</small>}
              {source.error && <p>{source.error}</p>}
            </div>)}
          </div>
          <Table<RecognitionComparison>
            size="small"
            rowKey="field"
            pagination={false}
            scroll={{ x: 660 }}
            dataSource={data.recognition.comparisons}
            columns={[
              { title: "字段", dataIndex: "label", width: 110, fixed: "left" },
              ...sources.map(([name]) => ({
                title: sourceLabels[name] || name,
                key: name,
                width: 180,
                render: (_: unknown, row: RecognitionComparison) => <span>
                  {row.values[name] || "未识别"}
                  {name === "MODEL" && row.values[name] && data.sources?.MODEL.clear_fields?.[row.field as keyof NonNullable<ParsedData["fields"]>] !== true &&
                    <Tag color="warning">不清晰</Tag>}
                </span>,
              })),
              { title: "采用依据", key: "accepted", width: 220, render: (_, row) => row.status === "NOT_REQUIRED" ? "本票种不适用" :
                row.status === "AGREED" ? <><div>{row.accepted}</div><small>{row.matched_sources.map(name => sourceLabels[name] || name).join("、")}</small></> :
                  <Tag color="warning">{row.status === "CONFLICT" ? "存在分歧" : "缺少一致证据"}</Tag> },
            ]}
          />
          {sources.some(([, source]) => source.pages?.length) && <Collapse
            size="small"
            items={[{ key: "ocr", label: "OCR 分页识别明细", children:
              sources.flatMap(([name, source]) => (source.pages || []).map(page => <div key={`${name}-${page.page}`}>
                <strong>第 {page.page} 页 · {page.width} × {page.height} 像素</strong>
                {page.request_id && <div className="muted">腾讯云请求 ID {page.request_id}</div>}
                <div className="recognition-ocr-lines">{page.lines.map((line, index) => <div key={index}>
                  <span>{line.text}</span>
                  {(line.score != null || line.box.length > 0) && <small>{[
                    line.score != null ? `${(line.score * 100).toFixed(1)}%` : "",
                    line.box.map(point => point.map(Math.round).join(",")).join(" / "),
                  ].filter(Boolean).join(" · ")}</small>}
                </div>)}</div>
              </div>)),
            }]}
          />}
          <Typography.Text type="secondary">识别一致性用于票面字段核对，不代表税务验真。流程 {data.pipeline_version || "旧版本"}</Typography.Text>
        </>,
      }]}
    />
  );
}
