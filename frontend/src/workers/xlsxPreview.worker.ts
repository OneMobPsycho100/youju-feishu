import { read, utils } from "xlsx";
import type { Range, WorkSheet } from "xlsx";
import { XLSX_MAX_BYTES, XLSX_MAX_COLUMNS, XLSX_MAX_ROWS, XLSX_PAGE_SIZE } from "../xlsxPreview";
import type { XlsxPreviewRequest, XlsxPreviewResponse, XlsxSheetPage } from "../xlsxPreview";

/** 仅声明当前 Worker 使用的接口，避免与页面的 DOM 全局类型混淆。 */
const scope = globalThis as unknown as {
  onmessage: (event: MessageEvent<XlsxPreviewRequest>) => void;
  postMessage: (message: XlsxPreviewResponse) => void;
};

let source: ArrayBuffer | undefined;
let sheets: string[] = [];
let current: { name: string; sheet: WorkSheet; range?: Range; fullRange?: Range } | undefined;

function sheetPage(name: string, requestedPage: number): XlsxSheetPage {
  if (!source || !sheets.includes(name)) throw new Error("工作表不存在，请重新打开预览");
  if (current?.name !== name) {
    // 每次只解析并保留一张工作表，切换时复用内存中的原件，不再请求服务器。
    const book = read(source, { type: "array", sheets: [name], dense: true, sheetRows: XLSX_MAX_ROWS,
      cellHTML: false, cellText: true, cellFormula: true, sheetStubs: true });
    const sheet = book.Sheets[name];
    if (!sheet) throw new Error("当前工作表无法读取");
    const hasData = sheet["!data"]?.some((row) => row?.some((cell) => cell && (cell.t !== "z" || cell.f)));
    current = { name, sheet, range: hasData && sheet["!ref"] ? utils.decode_range(sheet["!ref"]) : undefined,
      fullRange: sheet["!fullref"] || sheet["!ref"] ? utils.decode_range(sheet["!fullref"] || sheet["!ref"]!) : undefined };
  }
  const { sheet, range, fullRange } = current;
  const rowCount = range ? Math.max(0, Math.min(range.e.r + 1, XLSX_MAX_ROWS) - range.s.r) : 0;
  const columnCount = range ? Math.min(range.e.c - range.s.c + 1, XLSX_MAX_COLUMNS) : 0;
  const originalRows = fullRange ? fullRange.e.r - fullRange.s.r + 1 : 0;
  const originalColumns = fullRange ? fullRange.e.c - fullRange.s.c + 1 : 0;
  const page = Math.max(1, Math.min(Math.trunc(requestedPage) || 1, Math.ceil(rowCount / XLSX_PAGE_SIZE) || 1));
  const firstIndex = (range?.s.r || 0) + (page - 1) * XLSX_PAGE_SIZE;
  const columns = Array.from({ length: columnCount }, (_, index) => utils.encode_col((range?.s.c || 0) + index));
  const rows = Array.from({ length: Math.min(XLSX_PAGE_SIZE, rowCount - (page - 1) * XLSX_PAGE_SIZE) }, (_, index) =>
    Array.from({ length: columnCount }, (_, column) => {
      const cell = sheet["!data"]?.[firstIndex + index]?.[(range?.s.c || 0) + column];
      if (!cell) return "";
      // 使用文件保存的显示值，不执行公式；没有缓存结果时将公式作为普通文本展示。
      if (cell.t === "z" || cell.v === undefined || cell.v === null) return cell.f ? "=" + cell.f : "";
      return String(cell.w ?? utils.format_cell(cell));
    }));
  return { sheets, sheet: name, page, rowCount, firstRow: firstIndex + 1, columns, rows,
    originalRows, originalColumns, truncated: Boolean(fullRange && fullRange.e.r >= XLSX_MAX_ROWS) || originalColumns > XLSX_MAX_COLUMNS };
}

scope.onmessage = (event) => {
  const request = event.data;
  try {
    if (request.type === "open") {
      if (!request.buffer.byteLength || request.buffer.byteLength > XLSX_MAX_BYTES) throw new Error("文件为空或超过 30 MB，请下载原件查看");
      // 仅接受 XLSX 的 ZIP 容器，避免把登录页、错误响应或其他文件当成表格文本读取。
      const signature = new Uint8Array(request.buffer, 0, Math.min(4, request.buffer.byteLength));
      if (signature[0] !== 0x50 || signature[1] !== 0x4b || signature[2] !== 0x03 || signature[3] !== 0x04)
        throw new Error("原件不是有效的 XLSX 文件，请下载后核对");
      source = request.buffer;
      current = undefined;
      sheets = read(source, { type: "array", bookSheets: true }).SheetNames;
      if (!sheets.length) throw new Error("文件中没有可读取的工作表");
    }
    const data = sheetPage(request.type === "open" ? sheets[0] : request.sheet, request.type === "open" ? 1 : request.page);
    scope.postMessage({ type: "page", id: request.id, data });
  } catch (reason) {
    const message = reason instanceof Error && /[\u4e00-\u9fff]/.test(reason.message)
      ? reason.message : "无法读取 XLSX，文件可能已损坏或加密，请下载原件查看";
    scope.postMessage({ type: "error", id: request.id, message });
  }
};
