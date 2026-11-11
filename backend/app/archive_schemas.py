"""归档中心接口。整单审批 ID 与旧原件归档 ID 使用不同入口。"""
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import Field

from .schemas import Schema


MonthBasis = Literal["approved", "submitted"]
ArchiveScope = Literal["approved", "revoked"]
ArchiveStatus = Literal["", "ARCHIVED", "PARTIAL", "FAILED", "NONE", "QUARANTINED", "REVOKED"]
BusinessType = Literal["", "REIMBURSEMENT", "PAYMENT"]
MAX_PRINT_APPROVALS = 50
MAX_PRINT_FILES = 200
MAX_PRINT_PAGES = 300


class ArchiveSubject(Schema):
    id: str = Field(description="工作台主体 ID")
    name: str = Field(description="公司完整名称")


class ArchivedApproval(Schema):
    id: str = Field(description="飞书审批实例 ID，列表每个 ID 只出现一次")
    serial_number: str = Field(description="飞书申请编号")
    title: str = Field(description="原审批事由")
    business_type: Literal["REIMBURSEMENT", "PAYMENT"] = Field(description="费用报销或付款申请")
    applicant: str = Field(description="已同步的申请人姓名")
    amount: Decimal = Field(description="申报金额，十进制字符串")
    companies: list[ArchiveSubject] = Field(description="与审批关联的全部可识别主体")
    declared_company_names: list[str] = Field(description="原表单中的申报主体名称")
    submitted_at: datetime | None = Field(description="飞书申请提交时间，UTC")
    approved_at: datetime | None = Field(description="飞书最终通过时间，UTC；历史缺失时为空")
    archived_at: datetime | None = Field(description="最近一份原件的归档时间，UTC")
    status: str = Field(description="当前飞书审批状态")
    archive_status: str = Field(description="整单归档进度；与审批状态独立")
    attachment_count: int = Field(description="当前原件和已知缺件的数量，历史版本另列")
    missing_count: int = Field(description="已知未取得的当前附件数")
    print_data_pending: bool = Field(description="整单附件范围或申请人所属部门尚未完成同步；明确未设置部门不算待同步")
    printable: bool = Field(description="当前是否最终通过，且可发起整单打印")


class ArchiveSource(Schema):
    """包内文件的原包引用，用于在归档中追溯成员来源。"""

    attachment_id: str = Field(description="压缩包原件的附件版本 ID")
    filename: str = Field(description="压缩包原件名称")
    path: str = Field(description="文件在压缩包中的相对路径")


class ArchivedFile(Schema):
    id: str = Field(description="附件版本标识，用于当前归档详情和原件下载")
    filename: str = Field(description="原件名称")
    origin: str = Field(description="表单、审批动态、评论或历史原件")
    status: str = Field(description="原件归档状态，补充原件、缺件和历史版本分别标识")
    size: int | None = Field(description="原件字节数，未下载时可为空")
    company_id: str | None = Field(description="原件归属主体 ID")
    download_url: str | None = Field(description="同源鉴权下载地址，缺件为空")
    preview_url: str | None = Field(None, description="支持浏览器预览的鉴权地址，其他原件为空")
    reason: str = Field(description="无法取得或读取原件的说明")
    archive_path: str = Field(description="原件清单的目录路径")
    manifest: dict[str, Any] = Field(description="可公开给工作台使用者的原件清单")
    package_status: Literal["READY", "FAILED"] | None = Field(None, description="压缩包的展开状态")
    archive_source: ArchiveSource | None = Field(None, description="仅包内文件返回，含原包和成员相对路径")


class ArchivedApprovalDetail(ArchivedApproval):
    files: list[ArchivedFile] = Field(description="按表单、审批动态和评论排序的本单原件及缺件")
    history_files: list[ArchivedFile] = Field(description="旧附件版本，供归档留痕查询")
    history_available: bool = Field(description="是否已经取得飞书审批轨迹")
    feishu_url: str = Field(description="飞书原审批详情链接，演示单据为空")


class PrintJobInput(Schema):
    approval_ids: list[str] = Field(min_length=1, max_length=MAX_PRINT_APPROVALS,
        description="所选飞书审批 ID，按请求顺序合并")
    file_ids_by_approval: dict[str, list[str]] | None = Field(None,
        description="每张审批选中的当前附件版本 ID；省略或 null 表示全部附件，传入时必须覆盖全部所选审批，空数组仅打印该单正文")


class PrintIssue(Schema):
    approval_id: str = Field(description="未能完整打印的审批 ID")
    serial_number: str = Field(description="便于财务定位的申请编号")
    filename: str = Field(description="原件名称或待补齐资料名称")
    reason: str = Field(description="缺件、未同步或未能转换的原因")
    download_url: str | None = Field(None, description="可取得原件时的同源鉴权下载地址")
    feishu_url: str = Field("", description="可在飞书查阅原单时的入口")


class PrintJobOut(Schema):
    id: str = Field(description="临时打印任务 ID")
    status: Literal["QUEUED", "RUNNING", "READY", "FAILED"] = Field(description="打印稿生成状态")
    total: int = Field(description="本批审批单数量")
    completed: int = Field(description="已处理的审批单数量")
    pages: int = Field(description="完整生成后可打印的 PDF 页数")
    issues: list[PrintIssue] = Field(description="未能完整打印的资料及原件入口")
    error: str = Field(description="整批无法交付时的提示")
    pdf_url: str | None = Field(description="临时鉴权 PDF 地址，准备完成前为空")
    expires_at: datetime | None = Field(description="打印稿过期时间，UTC")
