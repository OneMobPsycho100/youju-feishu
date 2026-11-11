"""HTTP 接口与生命周期。业务操作统一进入持久化后台任务。"""
import csv
import io
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import SecretStr
from sqlalchemy import String, cast, func, or_, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import defer
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from . import business, schemas as s
from .auth import admin_user, current_user, finance_user, router as auth_router
from .config import settings
from .db import SessionLocal, begin_write, get_db
from .imports import IMPORT_COLUMNS, preview_import, read_ledger
from .integrations import DomainError, FeishuGateway, verify_callback
from .jobs import enqueue, start_archive_metadata_runner, start_local_runner
from .agent_runtime import run_model_analysis
from .ai_logs import AICallRecorder
from .ai_status import invalidate_ai_audit
from .manual_review import full_review
from .model_config import ENGINE, load_model_config, prepare_model_config, save_model_config
from .sync_config import load_sync_config, save_sync_config
from .models import (ActivityLog, AICall, AgentConversation, Approval, ApprovalAction, ApprovalTemplate, ArchiveRecord, Attachment, FeishuConfig,
                     AuditRun, Company, EventInbox, ImportBatch, Invoice, InvoiceClaim, Job, User, now)
from .parser import normalize
from .invoice_images import OCR_SAMPLE_LIMIT, ParseRejected, sample_invoice_image
from .invoice_recognition import PIPELINE_VERSION
from .audit_policy import AUDIT_POLICY_VERSION
from .ocr_config import load_ocr_config, resolve_ocr_credentials, save_ocr_config
from .tencent_invoice_ocr import ocr_status, recognize_invoice_pages
from .seed import seed_demo
from .services import (StaleSnapshotError, clear_disabled_ai_state, feishu_link, invalidate_pending, log,
                       requeue_requested_audit, request_action, settle_disabled_ai_work, sync_instance, update_capability)
from .storage import resolve
from .agent_api import router as agent_router, internal as agent_internal_router, mcp_route, mcp_sessions
from .reimbursement_api import router as reimbursement_router
from .template_api import router as template_router
from .template_config import apply_template_config, set_template_ai_approval
from .feishu_api import router as feishu_config_router
from .feishu_config import effective_public_url, ensure_feishu_config, load_feishu_config
from .agent_chat_runtime import manager as agent_manager
from .agent_attachments import attachment_manager
from .archive_api import router as archive_router
from .archive_center import make_archive_zip, original_ref
from .archive_print import manager as print_manager

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app):
    settings().validate_live()
    settings().storage_root.mkdir(parents=True, exist_ok=True)
    with SessionLocal() as db:
        installation_code = ensure_feishu_config(db)
        if installation_code:
            # 只在安装控制台首次交付；业务日志和配置接口均不记录或返回安装码。
            print("YOUJU 安装初始化码：" + installation_code + "（首次绑定后失效，请在网页接入向导输入）", flush=True)
        db.execute(text("SELECT 1"))
        seed_demo(db)
        begin_write(db)
        settle_disabled_ai_work(db)
        # 代码升级也会改变审批资格：旧策略下的 PASS 必须先失效，禁止待执行操作沿用旧结论。
        pending = select(Approval, ApprovalTemplate.ai_approval_enabled).join(ApprovalTemplate).where(
            Approval.status == "PENDING")
        for approval, ai_enabled in db.execute(pending):
            reviewed = bool(full_review(db, approval))
            if not ai_enabled:
                if clear_disabled_ai_state(approval, reviewed=reviewed):
                    approval.version += 1
                continue
            if reviewed:
                continue
            audit = db.get(AuditRun, approval.last_audit_id) if approval.last_audit_id else None
            if audit and (audit.model_analysis.get("recognition_policy_version") != PIPELINE_VERSION
                          or audit.model_analysis.get("audit_policy_version") != AUDIT_POLICY_VERSION):
                approval.decision, approval.audit_attachment_version = "PENDING", ""
                approval.audit_requested = True
                invalidate_ai_audit(approval)
                approval.version += 1
        db.flush()
        for approval in db.scalars(select(Approval).where(Approval.audit_requested.is_(True))):
            requeue_requested_audit(db, approval)
        db.commit()
    runner = start_local_runner()
    _app.state.runner = runner
    archive_runner = start_archive_metadata_runner()
    print_manager.start()
    agent_manager.start()
    attachment_manager.start()
    async with mcp_sessions.run():
        try:
            yield
        finally:
            # 关闭对话进程时仍保持事件循环可用，内部 MCP 与模型流才能完成清理。
            await run_in_threadpool(agent_manager.shutdown)
            await run_in_threadpool(attachment_manager.shutdown)
            await run_in_threadpool(print_manager.shutdown)
            archive_runner[0].set()
            runner[0].set()
            await run_in_threadpool(runner[1].join, 100)
            await run_in_threadpool(archive_runner[1].join, 10)


app = FastAPI(title="有据 · 飞书审批财务工作台", version="1.0.0",
              docs_url="/api/docs", redoc_url="/api/redoc", openapi_url="/api/openapi.json",
              description="原生审批预审、跨主体发票查重、人工复核与电子原件归档。金额以十进制字符串传输。", lifespan=lifespan)
app.include_router(auth_router)
app.include_router(agent_router)
app.include_router(reimbursement_router)
app.include_router(template_router)
app.include_router(feishu_config_router)
app.include_router(archive_router)
app.include_router(agent_internal_router)
app.router.routes.append(mcp_route)


@app.middleware("http")
async def browser_boundary(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD", "OPTIONS"} and request.url.path != "/api/feishu/events":
        allowed = urlparse(origin).netloc == request.headers.get("host")
        if not allowed:
            allowed = origin.rstrip("/") == await run_in_threadpool(effective_public_url)
        if not allowed:
            return JSONResponse({"detail": "请求来源与工作台不一致"}, status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(StaleSnapshotError)
@app.exception_handler(DomainError)
def domain_error(_request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(IntegrityError)
def conflict_error(_request, _exc):
    return JSONResponse({"detail": "记录已存在或发生并发冲突，请刷新后重试"}, status_code=409)


@app.exception_handler(RequestValidationError)
def validation_error(_request, exc):
    fields = [".".join(str(x) for x in error["loc"][1:]) for error in exc.errors()]
    if _request.url.path.startswith("/api/v1/templates/"):
        # 迁移文件的规则校验说明不含原始输入，便于管理员定位无效条目。
        reasons = [error["msg"].removeprefix("Value error, ") for error in exc.errors()
                   if error["type"] == "value_error"]
        if reasons:
            return JSONResponse({"detail": "配置校验失败：" + "；".join(dict.fromkeys(reasons))}, status_code=422)
    return JSONResponse({"detail": "请求字段格式不正确：" + "、".join(fields)}, status_code=422)


@app.exception_handler(OperationalError)
def storage_error(_request, exc):
    code = getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return JSONResponse({"detail": "数据库正在处理其他写入，请稍后重试"}, status_code=503,
                            headers={"Retry-After": "1"})
    logger.error("storage_error error_type=%s", type(exc.orig).__name__)
    return JSONResponse({"detail": "数据存储暂时不可用，请检查服务日志"}, status_code=503)


@app.get("/api/health/live", tags=["运行状态"], summary="检查 API 进程存活")
def live():
    return {"status": "ok"}


@app.get("/api/health/ready", tags=["运行状态"], summary="检查业务依赖可用性")
def ready(request: Request, db=Depends(get_db)):
    db.execute(text("SELECT 1"))
    runner = getattr(request.app.state, "runner", None)
    if not runner or runner[0].is_set() or not runner[1].is_alive():
        raise HTTPException(503, "持久化任务处理器尚未就绪")
    return {"status": "ready"}


@app.get("/api/meta", tags=["运行状态"], summary="获取登录页的公开环境标识")
def meta():
    return {"mode": settings().app_mode, "name": "有据", "version": "1.0.0"}


@app.post("/api/feishu/events", tags=["飞书事件"], summary="接收加密的原生审批事件")
async def feishu_events(request: Request):
    if settings().app_mode != "live":
        raise HTTPException(403, "演示环境不接收真实飞书回调")
    if load_feishu_config().event_transport != "http":
        raise HTTPException(403, "当前环境使用经 SDK 鉴权的长连接，不开放 HTTP 事件入口")
    # 有上限地读取原始请求，签名必须对原始字节验证，不能先重新序列化 JSON。
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 1024 * 1024:
            raise HTTPException(413, "回调请求过大")
        chunks.append(chunk)
    payload = verify_callback(b"".join(chunks), dict(request.headers))
    if payload.get("type") == "url_verification":
        return {"challenge": payload["challenge"]}
    header, event = payload.get("header", {}), payload.get("event", {})
    event_id, kind = payload.get("uuid") or header.get("event_id"), event.get("type") or header.get("event_type")
    if not event_id or not isinstance(event_id, str) or len(event_id) > 100:
        raise HTTPException(400, "事件缺少合法的幂等标识")
    if kind not in {"approval_instance", "approval_task", "approval", "approval.instance.status_changed_v4", "approval.task.status_changed_v4"}:
        return {"message": "ignored"}
    # 锁等待在线程池执行，不阻塞异步 HTTP 事件循环；提交后才向飞书确认成功。
    await run_in_threadpool(persist_event, event_id, kind, payload)
    return {"message": "success"}


def persist_event(event_id: str, kind: str, payload: dict) -> None:
    """事件与任务原子提交；重复事件只确认已有持久化记录。"""
    with SessionLocal() as db:
        begin_write(db)
        if not db.get(EventInbox, event_id):
            db.add(EventInbox(id=event_id, event_type=kind, payload=payload))
            enqueue(db, "EVENT", {"event_id": event_id}, f"event:{event_id}")
        db.get(FeishuConfig, 1).last_event_at = now()
        db.commit()


def page_result(db, statement, serializer, page, page_size):
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    items = db.scalars(statement.offset((page-1)*page_size).limit(page_size)).all()
    return {"items": [serializer(item) for item in items], "total": total, "page": page, "page_size": page_size}


def audit_dump(row):
    return s.AuditOut.model_validate(row).model_dump(mode="json") if row else None


def approval_dump(row):
    return s.ApprovalOut.model_validate(row).model_dump(mode="json")


@app.get("/api/v1/overview", response_model=s.OverviewOut, tags=["财务总览"], summary="查询当前真实业务数据的汇总指标")
def overview(company_id: str = "", user=Depends(current_user), db=Depends(get_db)):
    return business.overview(db, company_id)


@app.get("/api/v1/companies", response_model=list[s.CompanyOut], tags=["主体管理"], summary="查询公司主体")
def companies(user=Depends(current_user), db=Depends(get_db)):
    return db.scalars(select(Company).order_by(Company.created_at)).all()


@app.post("/api/v1/companies", response_model=s.CompanyOut, tags=["主体管理"], summary="新增公司主体")
def add_company(data: s.CompanyInput, user=Depends(admin_user), db=Depends(get_db)):
    begin_write(db)
    row = Company(**data.model_dump())
    db.add(row)
    db.flush()
    invalidate_pending(db)
    log(db, user.name, "新增公司主体", row.name)
    db.commit()
    return row


@app.put("/api/v1/companies/{company_id}", response_model=s.CompanyOut, tags=["主体管理"], summary="更新公司主体并重新审核待办")
def edit_company(company_id: str, data: s.CompanyInput, user=Depends(admin_user), db=Depends(get_db)):
    begin_write(db)
    row = db.get(Company, company_id)
    if not row:
        raise HTTPException(404, "主体不存在")
    for key, value in data.model_dump().items():
        setattr(row, key, value)
    invalidate_pending(db)
    log(db, user.name, "更新公司主体", row.name)
    db.commit()
    return row


@app.get("/api/v1/approvals", response_model=s.PageOut[s.ApprovalOut], tags=["审批中心"], summary="分页查询审批单")
def list_approvals(q: str = "", decision: str = "", status: str = "", company_id: str = "",
                   business_type: Literal["", "REIMBURSEMENT", "PAYMENT"] = "",
                   page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
                   user=Depends(current_user), db=Depends(get_db)):
    return business.list_approvals(db, q, decision, status, company_id, page, page_size, business_type)


@app.get("/api/v1/approvals/{approval_id}", response_model=s.ApprovalDetail, tags=["审批中心"], summary="读取飞书报销表单、审核证据、原件与操作记录")
def approval_detail(approval_id: str, user=Depends(current_user), db=Depends(get_db)):
    return business.approval_detail(db, approval_id, include_feishu_form=True)


@app.post("/api/v1/approvals/{approval_id}/audit", response_model=s.JobOut, tags=["审批中心"], summary="重新预审或补充识别已通过单据的包内 PDF")
def audit_approval(approval_id: str, user=Depends(finance_user), db=Depends(get_db)):
    """历史参考包使用本地票面补充识别，保留原预审与台账。"""
    return business.audit_approval(db, approval_id, user)


@app.post("/api/v1/approvals/{approval_id}/actions", response_model=s.JobOut, tags=["审批中心"], summary="提交同意或拒绝预审任务")
def approval_action(approval_id: str, data: s.ActionInput, user=Depends(finance_user), db=Depends(get_db)):
    return business.approval_action(db, approval_id, data, user)


@app.post("/api/v1/approvals/{approval_id}/reviews", response_model=s.ApprovalDetail, tags=["审批中心"], summary="人工整单或逐项复核并保存处理依据")
def review_approval(approval_id: str, data: s.ManualReviewInput, user=Depends(finance_user), db=Depends(get_db)):
    """仅财务或管理员可操作当前待办；整单复核直接允许手动同意，不等待 AI 或全部规则匹配。"""
    return business.review_approval(db, approval_id, data, user)


@app.post("/api/v1/approvals/{approval_id}/attachments/{attachment_id}/correct", response_model=s.JobOut, tags=["审批中心"], summary="保存人工票面字段，按模板配置同步或重审")
def correct_attachment(approval_id: str, attachment_id: str, data: s.CorrectionInput, user=Depends(finance_user), db=Depends(get_db)):
    return business.correct_attachment(db, approval_id, attachment_id, data, user)


@app.post("/api/v1/approvals/{approval_id}/attachments/{attachment_id}/material", response_model=s.JobOut,
          tags=["审批中心"], summary="保存人工确认的付款材料类别，按模板配置同步或重审")
def confirm_payment_material(approval_id: str, attachment_id: str, data: s.MaterialConfirmationInput,
                             user=Depends(finance_user), db=Depends(get_db)):
    from .payment_service import confirm_material
    return confirm_material(db, approval_id, attachment_id, data, user)


@app.post("/api/v1/approvals/{approval_id}/archive", response_model=s.JobOut, tags=["归档中心"], summary="重新获取最终原件并归档")
def archive(approval_id: str, user=Depends(finance_user), db=Depends(get_db)):
    return business.archive(db, approval_id, user)


@app.post("/api/v1/demo/approvals/{approval_id}/finish", response_model=s.JobOut, tags=["演示流程"], summary="演示原有后续节点完成后触发归档")
def demo_finish(approval_id: str, user=Depends(finance_user), db=Depends(get_db)):
    begin_write(db)
    if settings().app_mode != "demo":
        raise HTTPException(404)
    approval = db.get(Approval, approval_id)
    if not approval or approval.status != "PENDING" or approval.task_status != "APPROVED":
        raise DomainError("请先在工作台同意预审节点，再模拟后续审批通过")
    snapshot = json.loads(json.dumps(approval.external_snapshot))
    snapshot["status"] = "APPROVED"
    snapshot["end_time"] = int(now().replace(tzinfo=timezone.utc).timestamp() * 1000)
    snapshot["archive_time_simulated"] = True
    approval.external_snapshot = snapshot
    approval.version += 1
    log(db, user.name, "演示后续审批通过", approval.serial_number)
    job = enqueue(db, "ARCHIVE", {"instance_code": approval_id})
    db.commit()
    return job


@app.get("/api/v1/attachments/{attachment_id}/download", tags=["原件访问"], summary="鉴权下载或预览不可变原件")
def attachment_download(attachment_id: str, preview: bool = Query(False, description="在浏览器预览受支持的格式"),
                        user=Depends(current_user), db=Depends(get_db)):
    from .file_types import ARCHIVE_MIMES, INLINE_PREVIEW_MIMES

    row = db.get(Attachment, attachment_id)
    if not row or not resolve(row.storage_key).is_file():
        raise HTTPException(404, "附件原件不存在")
    inline = preview and row.mime in INLINE_PREVIEW_MIMES
    return FileResponse(resolve(row.storage_key), filename=row.filename,
        media_type=row.mime if inline or row.mime in ARCHIVE_MIMES else "application/octet-stream",
        content_disposition_type="inline" if inline else "attachment",
        headers={"X-Content-Type-Options": "nosniff"})


@app.get("/api/v1/invoices", response_model=s.PageOut[s.InvoiceEntry], tags=["发票台账"], summary="分页检索跨主体发票及占用来源")
def invoices(q: str = "", company_id: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
             user=Depends(current_user), db=Depends(get_db)):
    return business.invoices(db, q, company_id, page, page_size)


@app.get("/api/v1/invoices/export", tags=["发票台账"], summary="导出带文本票号格式的 Excel 发票台账")
def export_invoices(company_id: str = "", user=Depends(current_user), db=Depends(get_db)):
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    statement = select(Invoice).order_by(Invoice.issue_date.desc())
    if company_id:
        statement = statement.where(Invoice.company_id == company_id)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "发票台账"
    sheet.append([*IMPORT_COLUMNS.values(), "占用状态", "验真状态"])
    for invoice in db.scalars(statement.limit(50000)):
        claim = db.scalar(select(InvoiceClaim).where(InvoiceClaim.active_key == invoice.id))
        values = [invoice.number, invoice.code, invoice.buyer_name, invoice.buyer_tax_id, invoice.seller_name,
                  invoice.issue_date.isoformat(), str(invoice.amount), claim.source_label if claim else "", claim.status if claim else "未占用", "未验真"]
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            cell.data_type, cell.number_format = "s", "@"
    for cell in sheet[1]:
        cell.font = Font(color="FFFFFF", bold=True)
        cell.fill = PatternFill("solid", fgColor="1665F5")
    for column in sheet.columns:
        sheet.column_dimensions[column[0].column_letter].width = 26
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    buffer = io.BytesIO()
    workbook.save(buffer)
    begin_write(db)
    log(db, user.name, "导出发票台账", company_id or "全部主体")
    db.commit()
    return Response(buffer.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="invoice-ledger.xlsx"'})


@app.get("/api/v1/import-template", tags=["台账导入"], summary="下载历史台账 Excel 模板")
def import_template(user=Depends(finance_user)):
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "历史发票"
    sheet.append(list(IMPORT_COLUMNS.values()))
    for column in "ABCDEFGH":
        sheet.column_dimensions[column].width = 26
    for row in sheet.iter_rows(min_row=2, max_row=1000, max_col=8):
        for cell in row:
            cell.number_format = "@"
    sheet.freeze_panes = "A2"
    buffer = io.BytesIO()
    workbook.save(buffer)
    return Response(buffer.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="historical-invoice-template.xlsx"'})


def import_dump(batch):
    return {**s.ImportOut.model_validate(batch).model_dump(mode="json"), "preview": batch.preview[:100], "row_count": len(batch.rows), "valid_count": len(batch.preview)}


@app.post("/api/v1/imports", response_model=s.ImportOut, tags=["台账导入"], summary="上传历史台账并识别列名")
async def upload_import(file: UploadFile = File(description="CSV 或 XLSX 历史发票台账"), user=Depends(finance_user), db=Depends(get_db)):
    data = await file.read(20*1024*1024+1)
    columns, rows = read_ledger(data, file.filename or "ledger.csv")
    mapping = {key: title if title in columns else key if key in columns else "" for key, title in IMPORT_COLUMNS.items()}
    begin_write(db)
    batch = ImportBatch(filename=(file.filename or "ledger")[:240], actor_id=user.id, columns=columns, rows=rows, mapping=mapping)
    db.add(batch)
    log(db, user.name, "上传历史发票台账", batch.filename)
    db.commit()
    return import_dump(batch)


@app.post("/api/v1/imports/{batch_id}/preview", response_model=s.ImportOut, tags=["台账导入"], summary="应用字段映射并生成逐行校验预览")
def preview_ledger(batch_id: str, mapping: dict[str, str] = Body(description="标准字段到上传表头的映射"), user=Depends(finance_user), db=Depends(get_db)):
    begin_write(db)
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "导入批次不存在")
    preview_import(batch, mapping)
    db.commit()
    return import_dump(batch)


@app.post("/api/v1/imports/{batch_id}/confirm", response_model=s.JobOut, tags=["台账导入"], summary="确认有效行进入历史查重库")
def confirm_ledger(batch_id: str, user=Depends(finance_user), db=Depends(get_db)):
    begin_write(db)
    batch = db.scalar(select(ImportBatch).where(ImportBatch.id == batch_id))
    if not batch or batch.status != "PREVIEWED" or not batch.preview:
        raise DomainError("没有可导入的有效预览，请先完成字段映射")
    batch.status = "IMPORTING"
    log(db, user.name, "确认导入历史台账", batch.filename, {"batch_id": batch_id, "valid_rows": len(batch.preview)})
    job = enqueue(db, "IMPORT", {"batch_id": batch_id, "actor_name": user.name}, f"import:{batch_id}")
    db.commit()
    return job


@app.get("/api/v1/imports/{batch_id}", response_model=s.ImportOut, tags=["台账导入"], summary="获取导入结果及错误行")
def get_import(batch_id: str, user=Depends(finance_user), db=Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "导入批次不存在")
    return import_dump(batch)


@app.get("/api/v1/imports/{batch_id}/errors", tags=["台账导入"], summary="下载逐行导入错误报告")
def import_errors(batch_id: str, user=Depends(finance_user), db=Depends(get_db)):
    batch = db.get(ImportBatch, batch_id)
    if not batch:
        raise HTTPException(404, "导入批次不存在")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["行号", "错误原因"])
    writer.writerows([item["row"], "'"+item["error"] if item["error"].startswith(("=", "+", "-", "@")) else item["error"]] for item in batch.errors)
    return Response(buffer.getvalue().encode("utf-8-sig"), media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="import-errors.csv"'})


@app.post("/api/v1/claims/{claim_id}/release", tags=["发票台账"], summary="管理员核查付款与记账后释放已冻结占用")
def release_claim(claim_id: str, data: s.ClaimReleaseInput, user=Depends(admin_user), db=Depends(get_db)):
    begin_write(db)
    claim = db.scalar(select(InvoiceClaim).where(InvoiceClaim.id == claim_id))
    if not claim or claim.status != "FROZEN":
        raise DomainError("只能释放已经冻结且核查完毕的占用")
    comment = data.comment.strip()
    if len(comment) < 5 or data.payment_and_bookkeeping_checked is not True:
        raise DomainError("请填写核查依据并明确确认付款及入账状态")
    claim.status, claim.active_key = "RELEASED", None
    log(db, user.name, "人工释放冻结占用", claim.source_label, {"comment": comment, "invoice_id": claim.invoice_id})
    db.commit()
    return {"message": "已释放冻结占用，核查依据已留存"}


@app.get("/api/v1/archives", response_model=s.PageOut[s.ArchiveOut], tags=["归档中心"], summary="按主体、开票月份和审批月份检索归档原件")
def archives(company_id: str = "", month: str = Query("", description="开票月份 YYYY-MM；空值表示不限"),
             status: str = "", q: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
             approval_month: str = Query("", description="审批月份 YYYY-MM，按申请提交时间的北京时间自然月筛选；空值表示不限"),
             user=Depends(current_user), db=Depends(get_db)):
    return business.archives(db, company_id, month, status, q, page, page_size, approval_month=approval_month)


@app.get("/api/v1/archives/download", tags=["归档中心"], summary="按归档清单打包原件和 manifest.json")
def archive_zip(ids: str = Query(description="逗号分隔的归档记录 ID，最多 200 个"), user=Depends(current_user), db=Depends(get_db)):
    selected = list(dict.fromkeys(ids.split(",")))
    if not selected or len(selected) > 200:
        raise DomainError("一次请选择 1 至 200 个归档文件")
    rows = list(db.scalars(select(ArchiveRecord).where(ArchiveRecord.id.in_(selected))))
    if len(rows) != len(selected):
        raise DomainError("部分归档记录不存在，请刷新列表")
    return make_archive_zip(db, user.name, [(row.archive_path, original_ref(db.get(Attachment, row.attachment_id), row))
                                           for row in rows])


@app.get("/api/v1/templates", response_model=list[s.TemplateOut], tags=["接入配置"], summary="查询审批模板与实际联调能力")
def templates(user=Depends(admin_user), db=Depends(get_db)):
    return db.scalars(select(ApprovalTemplate).order_by(ApprovalTemplate.created_at)).all()


@app.put("/api/v1/templates/{template_code}", response_model=s.TemplateOut, tags=["接入配置"], summary="保存模板字段映射与自动化策略")
def save_template(template_code: str, data: s.TemplateInput, user=Depends(admin_user), db=Depends(get_db)):
    if template_code != data.code:
        raise DomainError("模板编码与请求路径不一致")
    row = apply_template_config(db, data, user.name)
    db.commit()
    return row


@app.put("/api/v1/templates/{template_code}/ai-approval", response_model=s.TemplateOut, tags=["接入配置"],
         summary="开启或关闭单个模板的 AI 审批")
def update_template_ai_approval(template_code: str, data: s.TemplateAIApprovalInput,
                                user=Depends(admin_user), db=Depends(get_db)):
    """管理员显式切换 AI 预审，切换后进入观察模式；重复保存同一状态不会重复安排任务。"""
    row = set_template_ai_approval(db, template_code, data.ai_approval_enabled, user.name)
    if row is None:
        raise HTTPException(404, "模板不存在")
    db.commit()
    return row


@app.post("/api/v1/templates/{template_code}/connect", response_model=s.TemplateOut, tags=["接入配置"], summary="读取模板并订阅原生审批事件")
def connect_template(template_code: str, sample_instance: str = "", user=Depends(admin_user), db=Depends(get_db)):
    row = db.get(ApprovalTemplate, template_code)
    if not row:
        raise HTTPException(404, "模板不存在")
    if settings().app_mode == "live":
        remote = FeishuGateway()
        remote.definition(row.code)
        remote.subscribe(row.code)
        if sample_instance:
            sync_instance(db, sample_instance, row.code)
    begin_write(db)
    row = db.get(ApprovalTemplate, template_code)
    row.subscribed = True
    update_capability(row, "definition_read")
    log(db, user.name, "订阅审批定义", row.name)
    db.commit()
    return row


@app.get("/api/v1/system", response_model=s.SystemOut, tags=["接入配置"], summary="获取脱敏的运行与供应商状态")
def system_info(user=Depends(admin_user), db=Depends(get_db)):
    cfg = settings()
    model = load_model_config(db)
    feishu = load_feishu_config(db) if cfg.app_mode == "live" else None
    return {"mode": cfg.app_mode, "job_mode": "local", "database": "sqlite", "storage": "持久化文件卷",
            "model_configured": model.configured, "model_source": model.source, "agent_engine": ENGINE,
            "model_name": model.model_name or "尚未配置", **ocr_status(load_ocr_config(db)),
            "feishu_configured": bool(feishu and feishu.configured),
            "public_url": feishu.public_url if feishu else cfg.app_public_url, "verification": "UNVERIFIED"}


@app.get("/api/v1/ocr-config", response_model=s.OCRConfigOut, tags=["OCR 配置"], summary="读取脱敏的腾讯云发票 OCR 配置")
def ocr_config_info(user=Depends(admin_user), db=Depends(get_db)):
    """仅公开开关、版本和凭据状态。"""
    return load_ocr_config(db).public()


@app.put("/api/v1/ocr-config", response_model=s.OCRConfigOut, tags=["OCR 配置"], summary="保存腾讯云发票 OCR 配置")
def update_ocr_config(data: s.OCRConfigInput, user=Depends(admin_user), db=Depends(get_db)):
    """配置、待办结论失效和重审任务原子提交。审计日志只记录发生变更的字段名。"""
    actor_name = user.name
    config, changed, recheck = save_ocr_config(db, data)
    if recheck:
        invalidate_pending(db)
    if changed:
        log(db, actor_name, "更新腾讯云发票 OCR 配置", "发票识别", {"fields": changed, "version": config.version})
    db.commit()
    return config.public()


@app.post("/api/v1/ocr-config/test", response_model=s.OCRConnectionOut, tags=["OCR 配置"], summary="用一张样票测试腾讯云 OCR 草稿")
def test_ocr_connection(
    file: UploadFile = File(description="管理员选择的单页 JPG 或 PNG 脱敏样票，最多 10 MB"),
    version: int = Form(ge=0, description="读取时的配置版本，用于定位可复用的已存凭据"),
    secret_id: SecretStr = Form(SecretStr(""), max_length=200, description="草稿 SecretId，与 SecretKey 成对提交"),
    secret_key: SecretStr = Form(SecretStr(""), max_length=200, description="草稿 SecretKey，两项留空则使用已存凭据"),
    user=Depends(admin_user), db=Depends(get_db),
):
    """只将所选样票发送到腾讯云，结果独立于业务识别及配置保存。"""
    current = load_ocr_config(db)
    draft_id, draft_key = resolve_ocr_credentials(current, version, secret_id.get_secret_value(), secret_key.get_secret_value())
    if not draft_id:
        raise DomainError(current.credential_error or "测试前请填写 SecretId 和 SecretKey")
    db.rollback()
    try:
        with file.file as sample:
            page = sample_invoice_image(sample.read(OCR_SAMPLE_LIMIT + 1))
    except ParseRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    result = recognize_invoice_pages([page], draft_id, draft_key)
    success = result["status"] == "COMPLETED"
    pages = result["pages"]
    return {"success": success, "message": "腾讯云已从样票提取发票字段" if success else result["error"],
            "elapsed_ms": result.get("elapsed_ms", 0), "request_id": pages[0]["request_id"] if pages else ""}


@app.get("/api/v1/model-config", response_model=s.ModelConfigOut, tags=["模型配置"], summary="读取脱敏模型配置及全局审核要求")
def model_config_info(user=Depends(admin_user), db=Depends(get_db)):
    """模型密钥只在服务端解密，绝不返回明文、密文或可逆占位符。"""
    return load_model_config(db).public()


@app.put("/api/v1/model-config", response_model=s.ModelConfigOut, tags=["模型配置"], summary="保存模型配置并使相关待办重新审核")
def update_model_config(data: s.ModelConfigInput, user=Depends(admin_user), db=Depends(get_db)):
    """配置、旧结论失效及重审发件箱在同一事务中提交，日志仅记录变更字段名。"""
    config, changed = save_model_config(db, data)
    if any(name in changed for name in ("enabled", "base_url", "model_name", "api_key")):
        invalidate_pending(db)
    elif "default_prompt" in changed:
        invalidate_pending(db, inherited_only=True)
    if changed:
        log(db, user.name, "更新模型与审核提示词", "全局 Agent 配置", {"fields": changed, "version": config.version})
    db.commit()
    return config.public()


@app.post("/api/v1/model-config/test", response_model=s.ModelConnectionOut, tags=["模型配置"], summary="通过 Harness 验证草稿连接，不保存配置")
def test_model_connection(data: s.ModelConfigInput, user=Depends(admin_user), db=Depends(get_db)):
    """只发送固定的虚拟报销数据，不读取或修改任何真实审批。测试可使用未保存的新密钥。"""
    candidate = prepare_model_config(load_model_config(db), data, testing=True)
    actor_name = user.name
    # 调用模型前结束只读事务，避免长调用持续占用数据库事务。
    db.rollback()
    started = time.monotonic()
    trace = AICallRecorder(candidate, source="CONNECTION_TEST", prompt=candidate.default_prompt, actor=actor_name)
    result = run_model_analysis(candidate, candidate.default_prompt,
        {"description": "连接检查：购买办公文具", "expense_type": "办公费",
         "invoices": [{"seller_name": "虚拟文具供应商", "amount": "10.00", "description": "办公文具"}]}, trace=trace)
    success = result["status"] == "COMPLETED"
    return {"success": success, "message": "Harness 连接成功，结构化输出校验通过" if success else result["summary"],
            "latency_ms": round((time.monotonic() - started) * 1000), "call_id": trace.id}


@app.get("/api/v1/ai-calls", response_model=s.PageOut[s.AICallSummary], tags=["AI 调用日志"], summary="分页查询 AI 调用、耗时与完成状态")
def ai_calls(approval_id: str = "", source: Literal["AUDIT", "CONNECTION_TEST", "INVOICE_EXTRACTION", "PAYMENT_MATERIAL", "AGENT_ATTACHMENT"] | None = None,
             status: Literal["RUNNING", "COMPLETED", "FAILED", "SKIPPED"] | None = None,
             page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
             user=Depends(current_user), db=Depends(get_db)):
    """管理员可查全局；其他角色沿用审批读取权限，只能查询具体审批的审核日志。"""
    if user.role != "admin" and (not approval_id or source == "CONNECTION_TEST"):
        raise HTTPException(403, "当前账号只能查看具体审批的 AI 审核日志")
    if approval_id and not db.get(Approval, approval_id):
        raise HTTPException(404, "审批单不存在")
    statement = select(AICall).options(*(defer(getattr(AICall, name)) for name in
        ("prompt", "input_json", "reasoning", "raw_output", "parsed_result", "events")))
    # 私人附件即使出现在管理员全局日志中，也只能由会话属主读取正文和调用详情。
    own_conversations = select(AgentConversation.id).where(AgentConversation.owner_id == user.id)
    statement = statement.where(or_(AICall.agent_conversation_id.is_(None), AICall.agent_conversation_id.in_(own_conversations)))
    if approval_id:
        statement = statement.where(AICall.approval_id == approval_id)
    if source:
        statement = statement.where(AICall.source == source)
    if user.role != "admin":
        statement = statement.where(AICall.source.in_(["AUDIT", "INVOICE_EXTRACTION", "PAYMENT_MATERIAL"]))
    if status:
        statement = statement.where(AICall.status == status)
    return page_result(db, statement.order_by(AICall.started_at.desc(), AICall.id.desc()),
        lambda row: s.AICallSummary.model_validate(row).model_dump(mode="json"), page, page_size)


@app.get("/api/v1/ai-calls/{call_id}", response_model=s.AICallDetail, tags=["AI 调用日志"], summary="查看提示词、输入、可见思考、原始输出与阶段事件")
def ai_call_detail(call_id: str, user=Depends(current_user), db=Depends(get_db)):
    """不返回请求头、API Key、运行时 stderr 或供应商隐藏的推理数据。"""
    call = db.get(AICall, call_id)
    if not call:
        raise HTTPException(404, "AI 调用日志不存在；历史调用在日志功能上线前未保存")
    if call.agent_conversation_id:
        conversation = db.get(AgentConversation, call.agent_conversation_id)
        if not conversation or conversation.owner_id != user.id:
            raise HTTPException(404, "AI 调用日志不存在或不属于当前账号")
        return call
    if user.role != "admin" and (call.source not in {"AUDIT", "INVOICE_EXTRACTION", "PAYMENT_MATERIAL"} or not call.approval_id or not db.get(Approval, call.approval_id)):
        raise HTTPException(403, "当前账号不能查看此 AI 调用日志")
    return call


@app.get("/api/v1/users", response_model=list[s.UserOut], tags=["权限管理"], summary="查询工作台用户与角色")
def list_users(user=Depends(admin_user), db=Depends(get_db)):
    return db.scalars(select(User).order_by(User.created_at)).all()


@app.put("/api/v1/users/{user_id}", response_model=s.UserOut, tags=["权限管理"], summary="调整财务与只读访问权限")
def update_user(user_id: str, data: s.UserUpdate, user=Depends(admin_user), db=Depends(get_db)):
    begin_write(db)
    row = db.get(User, user_id)
    if not row:
        raise HTTPException(404, "用户首次通过飞书登录后才会出现在列表中")
    if row.id == user.id and (data.role != "admin" or not data.enabled):
        raise DomainError("不能撤销当前管理员自身的管理权限")
    row.role, row.enabled = data.role, data.enabled
    log(db, user.name, "调整工作台权限", row.name, data.model_dump())
    db.commit()
    return row


@app.get("/api/v1/jobs", response_model=s.PageOut[s.JobOut], tags=["任务与日志"], summary="查询后台任务与重试状态")
def jobs(status: str = "", page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(current_user), db=Depends(get_db)):
    statement = select(Job).order_by(Job.created_at.desc())
    if status:
        statement = statement.where(Job.status == status)
    return page_result(db, statement, lambda row: s.JobOut.model_validate(row).model_dump(mode="json"), page, page_size)


@app.get("/api/v1/jobs/{job_id}", response_model=s.JobOut, tags=["任务与日志"], summary="轮询单个后台操作结果")
def get_job(job_id: str, user=Depends(current_user), db=Depends(get_db)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "后台任务不存在")
    return job


@app.post("/api/v1/jobs/{job_id}/retry", response_model=s.JobOut, tags=["任务与日志"], summary="重试失败任务，回写任务先对账再执行")
def retry_job(job_id: str, user=Depends(finance_user), db=Depends(get_db)):
    return business.retry_job(db, job_id, user)


@app.get("/api/v1/sync-config", response_model=s.SyncConfigOut, tags=["接入配置"], summary="读取自动增量补偿同步开关与间隔")
def sync_config_info(user=Depends(admin_user), db=Depends(get_db)):
    """仅管理员可读；返回生效配置及 UTC 下次入队时间，不触发同步。"""
    return load_sync_config(db)


@app.put("/api/v1/sync-config", response_model=s.SyncConfigOut, tags=["接入配置"], summary="保存自动增量补偿同步开关与间隔")
def update_sync_config(data: s.SyncConfigInput, user=Depends(admin_user), db=Depends(get_db)):
    """保存后按新间隔重新计时；跳过旧待执行自动任务，手动和运行中任务继续完成。"""
    config, changed = save_sync_config(db, data)
    if changed:
        log(db, user.name, "更新自动增量补偿同步配置", "全局补偿同步", {
            "fields": changed, "enabled": config.enabled, "interval_seconds": config.interval_seconds, "version": config.version})
    db.commit()
    return config


@app.post("/api/v1/sync", response_model=s.JobOut, tags=["接入配置"], summary="立即发起一次审批增量补偿同步")
def manual_sync(user=Depends(admin_user), db=Depends(get_db)):
    """手动同步独立入队，不受自动同步开关和间隔限制。"""
    return business.manual_sync(db, user)


@app.get("/api/v1/logs", response_model=s.PageOut[s.ActivityOut], tags=["任务与日志"], summary="分页查询操作者、决策与配置修改记录")
def activity_logs(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user=Depends(admin_user), db=Depends(get_db)):
    return page_result(db, select(ActivityLog).order_by(ActivityLog.created_at.desc()),
        lambda row: {"id": row.id, "actor": row.actor, "action": row.action, "target": row.target, "detail": row.detail, "created_at": row.created_at.isoformat()+"Z"}, page, page_size)
