"""私人附件的持久化、异步提取和只读工具；解析与业务发票建档完全分开。"""
import csv
import hashlib
import io
import logging
import os
import re
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select

from .agent_store import attachment_dump, clean, emit, owned
from .agent_usage import attachment_usage
from .config import settings
from .db import SessionLocal, begin_write
from .file_types import IMAGE_SUFFIXES, detect_file_type
from .integrations import DomainError
from .model_config import load_model_config
from .ocr_config import OCRConfigSnapshot, load_ocr_config
from .pdf_text import read_pdf_segments
from .models import (AICall, AgentAttachment, AgentConversation, Approval, Attachment, Company,
                     Invoice, InvoiceClaim, User, now, uid)
from .storage import resolve, safe_name

logger = logging.getLogger(__name__)
MAX_FILES = 5
MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT = 60000
EXTENSIONS = {".pdf", ".xml", ".ofd", ".docx", ".xlsx", ".csv", ".txt", ".md", *IMAGE_SUFFIXES}
upload_lock = threading.Lock()


def attachment_owned(db, attachment_id: str, user, *, conversation_id="", submitted=False):
    """所有下载、重试及工具读取均复核会话属主；管理员没有私人附件特权。"""
    row = db.get(AgentAttachment, attachment_id)
    if not row or row.deleted or conversation_id and row.conversation_id != conversation_id:
        raise HTTPException(404, "附件不存在或不属于当前会话")
    owned(db, row.conversation_id, user)
    if submitted and not row.submitted:
        raise HTTPException(404, "附件尚未发送，当前轮次不能读取")
    return row


def create_attachment(db, conversation_id, user, data: bytes, filename: str, upload_id: str):
    """逐文件上传，上传标识与内容共同校验幂等；文件只写到独立的会话目录。"""
    if not data:
        raise HTTPException(422, "附件内容为空")
    if len(data) > min(MAX_BYTES, settings().max_file_mb * 1024 * 1024):
        raise HTTPException(413, "单个附件不能超过 20MB")
    suffix, mime = detect_file_type(data, filename)
    if suffix not in EXTENSIONS:
        raise HTTPException(415, "暂不支持此文件格式，请上传 PDF、图片、XML、OFD、DOCX、XLSX、CSV、TXT 或 Markdown")
    digest = hashlib.sha256(data).hexdigest()
    filename = safe_name(filename)
    if Path(filename).suffix.lower() != suffix:
        filename = str(Path(filename).with_suffix(suffix))
    with upload_lock:
        begin_write(db)
        owned(db, conversation_id, user)
        previous = db.scalar(select(AgentAttachment).where(AgentAttachment.conversation_id == conversation_id,
            AgentAttachment.upload_id == upload_id))
        if previous:
            if previous.deleted or previous.sha256 != digest or previous.filename != filename:
                raise HTTPException(409, "同一上传标识不能用于其他附件，请重新选择文件")
            return previous
        drafts = db.scalar(select(func.count()).select_from(AgentAttachment).where(
            AgentAttachment.conversation_id == conversation_id, AgentAttachment.submitted.is_(False), AgentAttachment.deleted.is_(False)))
        if drafts >= MAX_FILES:
            raise HTTPException(422, "每轮最多上传五个附件，请先发送或移除已有附件")
        file_id = uid()
        key = f"agent-attachments/{conversation_id}/{file_id}/{filename}"
        path = resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
        try:
            with temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary.name, path)
            row = AgentAttachment(id=file_id, conversation_id=conversation_id, upload_id=upload_id,
                filename=filename, storage_key=key, sha256=digest, mime=mime, size=len(data),
                status="QUEUED", error="", segments=[], recognition={}, submitted=False, deleted=False)
            db.add(row)
            db.flush()
            emit(db, conversation_id, "attachment.updated", {"attachment": attachment_dump(row)})
            db.commit()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    attachment_manager.submit(file_id)
    db.expire_all()
    return db.get(AgentAttachment, file_id)


def _bounded(segments: list[dict]) -> list[dict]:
    if len(segments) > 2000 or sum(len(item["text"]) for item in segments) > MAX_TEXT:
        raise ValueError("附件正文超过 60000 字或 2000 个定位片段，请拆分后重新上传")
    if not any(item["text"].strip() for item in segments):
        raise ValueError("附件中没有可读取的正文，请检查原件或上传清晰版本")
    return segments


def _decode_text(data: bytes) -> str:
    encodings = ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            if "\x00" not in text and not re.search(r"[\x01-\x08\x0b\x0e-\x1f]", text):
                return text
        except UnicodeError:
            pass
    raise ValueError("文本编码无法识别，请使用 UTF-8、UTF-16 或 GB18030 文本")


def _image_reading(path, config, conversation_id) -> tuple[list[dict], bool]:
    """读取图像正文及分类，只有识别为发票的附件才进入发票专用 OCR。"""
    from .agent_runtime import _run_structured_model
    from .ai_logs import AICallRecorder
    from .invoice_images import invoice_images
    from .payment_materials import MATERIAL_CONSTRAINTS, MATERIAL_PROMPT, MaterialReading
    images = invoice_images(path, path.suffix.lower())
    trace = AICallRecorder(config, source="AGENT_ATTACHMENT", prompt=MATERIAL_PROMPT,
        actor="会话附件解析", constraints=MATERIAL_CONSTRAINTS, agent_conversation_id=conversation_id)
    result = _run_structured_model(config, MATERIAL_PROMPT, {"image_count": len(images)},
        lambda content: MaterialReading.model_validate_json(content).model_dump(),
        constraints=MATERIAL_CONSTRAINTS, trace=trace, images=images)
    if result["status"] != "COMPLETED" or not result.get("complete"):
        raise ValueError("图片正文无法完整读取；请提供清晰原件，或检查当前模型是否支持图片")
    segments = result.get("segments") or []
    if not segments or any(set(item) != {"location", "text"} or not all(isinstance(v, str) for v in item.values()) for item in segments):
        raise ValueError("图像读取结果缺少有效的原件定位，请重试或提供文字版文档")
    return _bounded(segments), result.get("material_type") == "INVOICE"


def parse_attachment(path: Path, config, ocr_config: OCRConfigSnapshot, conversation_id: str,
                     sha256: str, previous: dict) -> tuple[list, dict, str]:
    """PDF 正文本地读取；已识别为发票的缺失字段可使用图像模型兜底。"""
    from .parser import parse, parse_with_fallback
    from .payment_materials import read_office_segments

    suffix = path.suffix.lower()
    segments, invoice_result, warning = [], {}, ""
    suspected, local = False, None
    if suffix in {".docx", ".xlsx"}:
        return _bounded(read_office_segments(path)), {}, ""
    if suffix in {".txt", ".md", ".csv"}:
        text = _decode_text(path.read_bytes())
        if suffix == ".csv":
            cells = 0
            for index, row in enumerate(csv.reader(io.StringIO(text)), 1):
                cells += len(row)
                if cells > 20000:
                    raise ValueError("CSV 超过 20000 个单元格，请拆分后上传")
                segments.append({"location": f"第{index}行", "text": " | ".join(row)})
        else:
            segments = [{"location": f"第{index}行", "text": line} for index, line in enumerate(text.splitlines(), 1) if line.strip()]
        return _bounded(segments), {}, ""
    if suffix == ".pdf":
        from .pdf_text import PdfTextMissing
        try:
            segments = read_pdf_segments(path, max_text=MAX_TEXT)
        except PdfTextMissing:
            # 扫描 PDF 仍进入整份图像分类，正文缺失单独提示，不提前误报文件解析失败。
            warning = "扫描 PDF 无本地文字层，请结合原件核对正文"
        suspected = True
    elif suffix in {".xml", ".ofd"}:
        local = parse(path)
        text = local.get("raw_text", "")
        if len(text) >= 30000:
            raise ValueError("结构化文件正文过长，请拆分后上传")
        segments, suspected = [{"location": "结构化原件", "text": text}], True
    else:
        segments, suspected = _image_reading(path, config, conversation_id)
        suspected = True
    if suspected:
        invoice_result = parse_with_fallback(path, config, ocr_config=ocr_config, approval_id=None, template_code="", parent_db=None,
            sha256=sha256, previous=previous, local_result=local, agent_conversation_id=conversation_id)
        if invoice_result.get("rejected"):
            warning = invoice_result.get("error") or "原件含多张发票或身份存在冲突，无法判定使用状态"
        elif invoice_result.get("material_type") == "UNKNOWN":
            warning = invoice_result.get("error") or "附件类别尚未确认"
        elif invoice_result.get("material_type") == "INVOICE" and not ((invoice_result.get("recognition") or {}).get("fields") or {}).get("number"):
            warning = "发票号码尚未取得可信一致结果，使用状态无法判定；可上传清晰原件重试"
    return _bounded(segments), invoice_result, warning


class AttachmentManager:
    """单进程专用队列，与业务审批任务和 Agent 轮次独立；数据库只在状态变化时短暂加锁。"""
    def __init__(self):
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agent-attachment")
        self.pending = set()
        self.stopping = False

    def start(self):
        with SessionLocal() as db:
            begin_write(db)
            for row in db.scalars(select(AgentAttachment).where(AgentAttachment.status.in_(["QUEUED", "PROCESSING"]), AgentAttachment.deleted.is_(False))):
                row.status, row.error = "FAILED", "服务重启，附件解析已中断，请点击重试"
                emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row)})
            for call in db.scalars(select(AICall).where(AICall.agent_conversation_id.is_not(None), AICall.status == "RUNNING")):
                call.status, call.stage, call.error_message, call.finished_at = "FAILED", "附件解析中断", "服务重启，识别未完成", now()
            db.commit()

    def submit(self, attachment_id):
        with self.lock:
            if attachment_id in self.pending:
                return
            if self.stopping or len(self.pending) >= 32:
                with SessionLocal() as db:
                    begin_write(db)
                    row = db.get(AgentAttachment, attachment_id)
                    row.status, row.error = "FAILED", "附件解析队列繁忙，请稍后点击重试"
                    emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row),
                        "attachment_usage": attachment_usage(db, row.conversation_id)})
                    db.commit()
                return
            self.pending.add(attachment_id)
            self.executor.submit(self._work, attachment_id)

    def _work(self, attachment_id):
        try:
            with SessionLocal() as db:
                begin_write(db)
                row = db.get(AgentAttachment, attachment_id)
                if not row or row.deleted or self.stopping:
                    return
                conversation = db.get(AgentConversation, row.conversation_id)
                user = db.get(User, conversation.owner_id)
                if not user or not user.enabled:
                    raise ValueError("账号已停用，附件解析已停止")
                row.status, row.error = "PROCESSING", ""
                emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row)})
                config = load_model_config(db)
                ocr_config = load_ocr_config(db)
                path, conversation_id, digest, previous = resolve(row.storage_key), row.conversation_id, row.sha256, row.recognition
                db.commit()
            # 文件读取、OCR、模型调用在数据库会话和写锁之外执行。
            segments, recognition, warning = parse_attachment(path, config, ocr_config, conversation_id, digest, previous)
            with SessionLocal() as db:
                begin_write(db)
                row = db.get(AgentAttachment, attachment_id)
                if not row.deleted:
                    row.status, row.error, row.segments, row.recognition = "READY", warning, segments, recognition
                    emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row),
                        "attachment_usage": attachment_usage(db, row.conversation_id)})
                    db.commit()
        except Exception as exc:
            logger.warning("agent_attachment_failed id=%s error_type=%s", attachment_id, type(exc).__name__)
            with SessionLocal() as db:
                begin_write(db)
                row = db.get(AgentAttachment, attachment_id)
                if row and not row.deleted:
                    row.status = "FAILED"
                    row.error = clean(str(exc))[:500] if isinstance(exc, ValueError) else "附件无法完整读取，请检查文件是否损坏、加密或格式正确"
                    emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row),
                        "attachment_usage": attachment_usage(db, row.conversation_id)})
                    db.commit()
        finally:
            retry_queued = False
            try:
                with SessionLocal() as db:
                    row = db.get(AgentAttachment, attachment_id)
                    if row and row.deleted:
                        resolve(row.storage_key).unlink(missing_ok=True)
                    retry_queued = bool(row and not row.deleted and row.status == "QUEUED")
            finally:
                with self.lock:
                    self.pending.discard(attachment_id)
            # 状态事件提交后用户可能立即点击重试；确保旧线程收尾时不会吞掉这个新请求。
            if retry_queued and not self.stopping:
                self.submit(attachment_id)

    def shutdown(self):
        with self.lock:
            self.stopping = True
        self.executor.shutdown(wait=False, cancel_futures=True)


attachment_manager = AttachmentManager()


def read_attachment(db, data, handle, user):
    row = attachment_owned(db, data.attachment_id, user, conversation_id=handle.conversation_id, submitted=True)
    if row.status != "READY":
        raise DomainError(row.error or "附件尚未解析完成")
    # 字符游标按全文连续累计，保留每段定位；分页不会把整份材料反复灌入上下文。
    total, pieces, end = 0, [], data.offset + data.limit
    for segment in row.segments:
        text = segment["text"]
        start = total
        total += len(text)
        if total > data.offset and start < end:
            pieces.append({"location": segment["location"], "text": text[max(0, data.offset-start):max(0, end-start)]})
    return {"entity": "attachment", "attachment": attachment_dump(row), "segments": pieces,
            "total_characters": total, "next_offset": end if end < total else None,
            "invoice_candidate": (row.recognition or {}).get("material_type", "INVOICE") == "INVOICE" and bool(row.recognition), "warning": row.error}


def check_invoice_usage(db, data, handle, user):
    """查询时间点的全企业占用；历史已释放与当前有效占用分别返回，上传不建档、不占用。"""
    from .parser import invoice_key
    row = attachment_owned(db, data.attachment_id, user, conversation_id=handle.conversation_id, submitted=True)
    if row.status != "READY":
        raise DomainError(row.error or "附件尚未解析完成")
    parsed = row.recognition or {}
    identity = (parsed.get("recognition") or {}).get("fields") or {}
    result = {"entity": "invoice_usage", "attachment": attachment_dump(row), "scope": "全部公司",
              "queried_at": now().isoformat()+"Z", "items": [], "status": "UNKNOWN", "summary": "无法判定"}
    if parsed.get("rejected"):
        return {**result, "summary": "无法判定：" + (parsed.get("error") or "原件含多张发票或身份冲突")}
    key = ""
    try:
        key, _ = invoice_key(identity.get("number", ""), identity.get("code", ""))
    except ValueError:
        pass
    # 完全相同原件可关联已建档身份；若哈希关联多个身份或与可信号码冲突，拒绝任选其一。
    matched_ids = set(db.scalars(select(Attachment.invoice_id).where(Attachment.sha256 == row.sha256, Attachment.invoice_id.is_not(None))))
    direct = db.scalar(select(Invoice).where(Invoice.invoice_key == key)) if key else None
    if len(matched_ids) > 1 or direct and matched_ids and direct.id not in matched_ids:
        return {**result, "summary": "无法判定：同一原件关联的发票身份存在冲突，请核对原件"}
    invoice = direct or (db.get(Invoice, next(iter(matched_ids))) if matched_ids else None)
    if invoice and key and invoice.invoice_key != key:
        return {**result, "summary": "无法判定：原件哈希与识别票号不一致，请核对原件"}
    if not invoice and not key:
        return {**result, "summary": "无法判定：未取得可信票号，请上传清晰原件；传统票还需要发票代码"}
    result.update(number=invoice.number if invoice else identity["number"], code=invoice.code if invoice else identity.get("code", ""),
                  matched_by="原件哈希及票号" if matched_ids and key else "原件哈希" if matched_ids else "可信票号")
    if not invoice:
        return {**result, "status": "NOT_FOUND", "summary": "本系统未查到使用记录"}
    claims = list(db.scalars(select(InvoiceClaim).where(InvoiceClaim.invoice_id == invoice.id).order_by(InvoiceClaim.created_at.desc())))
    company = db.get(Company, invoice.company_id) if invoice.company_id else None
    for claim in claims:
        approval = db.get(Approval, claim.approval_id) if claim.approval_id else None
        source_companies = [db.get(Company, company_id) for company_id in approval.company_ids] if approval else []
        company_name = "、".join(company.name for company in source_companies if company) or (company.name if company else invoice.buyer_name)
        result["items"].append({"id": claim.id, "status": claim.status, "active": claim.active_key == invoice.id,
            "source_label": claim.source_label, "company_name": company_name,
            "approval_id": approval.id if approval else None, "serial_number": approval.serial_number if approval else "",
            "title": approval.title if approval else claim.source_label, "approval_status": approval.status if approval else "",
            "created_at": claim.created_at.isoformat()+"Z", "updated_at": claim.updated_at.isoformat()+"Z"})
    active = next((claim for claim in claims if claim.active_key == invoice.id), None)
    status = active.status if active else "RELEASED" if claims else "REGISTERED"
    labels = {"RESERVED": "发票当前被待审批单据占用", "USED": "发票已使用", "HISTORICAL": "发票已在历史台账中占用",
              "FROZEN": "发票占用已冻结，需人工核对", "RELEASED": "存在已释放的历史记录，当前无有效占用",
              "REGISTERED": "发票已登记，当前未查到有效占用"}
    return {**result, "invoice_id": invoice.id, "amount": str(invoice.amount), "buyer_name": invoice.buyer_name,
            "status": status, "summary": labels.get(status, "请核对占用记录"), "historical": invoice.historical}
