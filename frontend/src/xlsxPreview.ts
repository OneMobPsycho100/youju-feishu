/** XLSX 只读预览的边界；分页避免一次在页面中创建整张工作表。 */
export const XLSX_PAGE_SIZE = 100;
export const XLSX_MAX_ROWS = 10_000;
export const XLSX_MAX_COLUMNS = 200;
export const XLSX_MAX_BYTES = 30 * 1024 * 1024;

/** 后缀用于归档文件，MIME 用于只有原件类型的助手结果。 */
export function isXlsxFile(filename: string, mime?: string) {
  return /\.xlsx$/i.test(filename) || mime?.split(";")[0].trim() ===
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
}

/** 一页已经格式化的纯文本，单元格内容不作为 HTML 或脚本执行。 */
export interface XlsxSheetPage {
  /** 文件中的工作表名称，按原始顺序保留。 */
  sheets: string[];
  /** 当前工作表名称及从 1 开始的页码。 */
  sheet: string;
  page: number;
  /** 预览范围内的行数，以及当前页首行的 Excel 行号。 */
  rowCount: number;
  firstRow: number;
  /** 当前页的 Excel 列标及对应的单元格文本。 */
  columns: string[];
  rows: string[][];
  /** 原工作表的有效范围大小，用于明确提示预览截断。 */
  originalRows: number;
  originalColumns: number;
  truncated: boolean;
}

/** 原件只传入 Worker 一次，之后按工作表和页码请求数据；id 用于忽略过期响应。 */
export type XlsxPreviewRequest =
  | { type: "open"; id: number; buffer: ArrayBuffer }
  | { type: "page"; id: number; sheet: string; page: number };

/** Worker 只回传当前页，避免将完整工作簿复制到页面线程。 */
export type XlsxPreviewResponse =
  | { type: "page"; id: number; data: XlsxSheetPage }
  | { type: "error"; id: number; message: string };
