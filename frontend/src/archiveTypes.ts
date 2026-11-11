/** 审批级归档，以审批 ID 为选择单位，金额沿用后端十进制字符串。 */
export interface ArchivedApproval {
  id: string;
  serial_number: string;
  title: string;
  business_type: "REIMBURSEMENT" | "PAYMENT";
  applicant: string;
  amount: string;
  companies: { id: string; name: string }[];
  declared_company_names: string[];
  submitted_at: string | null;
  approved_at: string | null;
  archived_at: string | null;
  status: string;
  archive_status: string;
  attachment_count: number;
  missing_count: number;
  print_data_pending: boolean;
  printable: boolean;
}

/** 当前原件、缺件和历史版本共用的展示信息。 */
export interface ArchivedFile {
  id: string;
  filename: string;
  origin: string;
  status: string;
  size: number | null;
  company_id: string | null;
  download_url: string | null;
  /** 同源鉴权的原件预览地址，仅在浏览器支持该格式时返回。 */
  preview_url?: string | null;
  reason: string;
  archive_path: string;
  manifest: Record<string, unknown>;
  package_status?: "READY" | "FAILED" | null;
  /** 包内文件的原包版本、原包名称和包内路径。 */
  archive_source?: { attachment_id: string; filename: string; path: string } | null;
}

export interface ArchivedApprovalDetail extends ArchivedApproval {
  files: ArchivedFile[];
  history_files: ArchivedFile[];
  history_available: boolean;
  feishu_url: string;
}

/** 缺件或转换失败时显示来源审批、原因和可用的原件入口。 */
export interface ArchivePrintIssue {
  approval_id: string;
  serial_number: string;
  filename: string;
  reason: string;
  download_url: string | null;
  feishu_url: string;
}

/** 打印范围；不传附件映射时打印全部，传入时每单必须有键，空数组仅打印正文。 */
export interface ArchivePrintInput {
  /** 所选审批 ID，决定整单合并顺序。 */
  approval_ids: string[];
  /** 每单选中的当前附件版本 ID，服务端按原件顺序合并。 */
  file_ids_by_approval?: Record<string, string[]>;
}

export interface ArchivePrintJob {
  id: string;
  status: "QUEUED" | "RUNNING" | "READY" | "FAILED";
  total: number;
  completed: number;
  pages: number;
  issues: ArchivePrintIssue[];
  error: string;
  pdf_url: string | null;
  expires_at: string | null;
}
