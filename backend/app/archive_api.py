"""归档中心的审批级检索、补齐、下载和打印 HTTP 入口。"""
from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from . import archive_center as center
from . import archive_schemas as schema
from .archive_print import approval_ids, manager
from .auth import current_user, finance_user
from .db import begin_write, get_db
from .file_types import ARCHIVE_MIMES, INLINE_PREVIEW_MIMES
from .jobs import enqueue
from .schemas import JobOut, PageOut
from .storage import resolve


router = APIRouter(prefix="/api/v1/archives", tags=["归档中心"])


@router.get("/approvals", response_model=PageOut[schema.ArchivedApproval], summary="按主体和月份检索审批级归档")
def approvals(
    company_id: str = Query("", description="归属主体 ID，空值表示全部"),
    month: str = Query("", description="审批月份 YYYY-MM，空值表示不限"),
    month_basis: schema.MonthBasis = Query("approved", description="approved 最终通过月 / submitted 申请提交月，均为北京时间"),
    invoice_month: str = Query("", description="开票月份 YYYY-MM，空值表示不限"),
    scope: schema.ArchiveScope = Query("approved", description="approved 最终通过 / revoked 撤销留痕"),
    archive_status: schema.ArchiveStatus = Query("", description="归档状态，空值表示不限"),
    q: str = Query("", max_length=200, description="申请编号、事由、申请人或原件名称"),
    business_type: schema.BusinessType = Query("", description="费用报销或付款申请，空值表示不限"),
    page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
    user=Depends(current_user), db=Depends(get_db),
):
    return center.list_archive_approvals(db, page, page_size, company_id=company_id, month=month,
        month_basis=month_basis, invoice_month=invoice_month, scope=scope, archive_status=archive_status,
        q=q, business_type=business_type)


@router.get("/approvals/{approval_id}", response_model=schema.ArchivedApprovalDetail, summary="查看整单原件、缺件和历史留痕")
def approval_detail(approval_id: str, user=Depends(current_user), db=Depends(get_db)):
    return center.archive_detail(db, approval_id)


@router.get("/approval-download", summary="按审批 ID 下载整单原件及清单")
def approval_download(ids: str = Query(max_length=5050, description="有序的审批 ID，逗号分隔，最多五十单"),
                      user=Depends(current_user), db=Depends(get_db)):
    return center.archive_approval_zip(db, user.name, approval_ids(ids.split(",")))


@router.get("/approvals/{approval_id}/files/{file_id}", summary="鉴权下载或预览该审批的指定原件版本")
def approval_original(approval_id: str, file_id: str, preview: bool = Query(False, description="在浏览器预览受支持的格式"),
                      user=Depends(current_user), db=Depends(get_db)):
    item = center.archive_file(db, approval_id, file_id)
    inline = preview and item.mime in INLINE_PREVIEW_MIMES
    media = item.mime if inline or item.mime in ARCHIVE_MIMES else "application/octet-stream"
    return FileResponse(resolve(item.storage_key), filename=item.filename, media_type=media,
                        content_disposition_type="inline" if inline else "attachment",
                        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/approvals/{approval_id}/refresh-print-data", response_model=JobOut, summary="补齐审批记录和全部原件")
def refresh_print_data(approval_id: str, user=Depends(finance_user), db=Depends(get_db)):
    """通过现有任务机制补齐历史资料，不触发预审或审批回写。"""
    from sqlalchemy import select
    from .models import Job

    begin_write(db)
    center.get_archived_approval(db, approval_id)
    previous = db.scalar(select(Job).where(Job.kind == "ARCHIVE_METADATA", Job.status.in_(["PENDING", "RUNNING"]),
        Job.payload["instance_code"].as_string() == approval_id).order_by(Job.created_at.desc()).limit(1))
    result = previous or enqueue(db, "ARCHIVE_METADATA", {"instance_code": approval_id})
    db.commit()
    return result


@router.post("/print-jobs", status_code=202, response_model=schema.PrintJobOut, summary="生成所选审批及指定附件的 PDF，默认包含全部附件")
def print_job(data: schema.PrintJobInput, user=Depends(current_user), db=Depends(get_db)):
    return manager.submit(db, user, approval_ids(data.approval_ids), data.file_ids_by_approval)


@router.get("/print-jobs/{job_id}", response_model=schema.PrintJobOut, summary="轮询本人临时打印任务")
def print_status(job_id: str, user=Depends(current_user), db=Depends(get_db)):
    return manager.state(db, job_id, user.id)


@router.get("/print-jobs/{job_id}/pdf", summary="预览或下载本人生成的打印稿")
def print_pdf(job_id: str, download: bool = Query(False, description="是否以附件下载 PDF"),
              user=Depends(current_user), db=Depends(get_db)):
    path = manager.file(db, job_id, user.id)
    return FileResponse(path, filename="审批归档打印稿.pdf", media_type="application/pdf",
        content_disposition_type="attachment" if download else "inline",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "SAMEORIGIN"})
