"""发票提取：PDF 本地优先、模型兜底；图片使用独立来源交叉核对。"""
import copy
import hashlib
import logging
import re
import unicodedata
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from defusedxml import ElementTree as ET
from .file_types import IMAGE_SUFFIXES, detect_file_type
from .invoice_images import ParseRejected, invoice_page_images, model_image_blocks
from .invoice_recognition import (FIELD_LABELS, PIPELINE_VERSION, PDF_TEXT_VERSION, TENCENT_OCR, compare_sources,
                                  normalized_candidate, recognition_cache_key)
from .pdf_invoice_fields import read_pdf_invoice_fields
from .pdf_text import PdfTextMissing, read_pdf_segments

if TYPE_CHECKING:
    from .ocr_config import OCRConfigSnapshot

logger = logging.getLogger(__name__)


FIELD_ALIASES = {
    "number": ("InvoiceNumber", "InvoiceNo", "Fphm", "FPHM", "发票号码"),
    "code": ("InvoiceCode", "Fpdm", "FPDM", "发票代码"),
    "buyer_name": ("BuyerName", "PurchaserName", "Gfmc", "购买方名称"),
    "buyer_tax_id": ("BuyerTaxID", "BuyerTaxId", "PurchaserTaxID", "Gfsh", "购买方税号"),
    "seller_name": ("SellerName", "Xfmc", "销售方名称"),
    "issue_date": ("IssueDate", "InvoiceDate", "Kprq", "开票日期"),
    "amount": ("TotalAmountWithTax", "TaxInclusiveAmount", "TotalTax-includedAmount", "Jshj", "价税合计"),
}


def invoice_key(number: str, code: str = "") -> tuple[str, str]:
    """数电号码全企业唯一；传统票使用代码＋号码，禁止拼接主体规避查重。"""
    number, code = re.sub(r"\s", "", str(number)), re.sub(r"\s", "", str(code))
    if re.fullmatch(r"\d{20}", number):
        return f"E:{number}", "E_INVOICE"
    if re.fullmatch(r"\d{10}|\d{12}", code) and re.fullmatch(r"\d{8}", number):
        return f"V:{code}:{number}", "VAT"
    raise ValueError("票号格式不完整：数电发票需 20 位号码，传统发票需 10/12 位代码和 8 位号码")


def normalize(fields: dict) -> dict:
    """规范化字段并检查最低建档条件；金额不得通过二进制浮点计算。"""
    data = {key: str(fields.get(key, "")).strip() for key in FIELD_ALIASES}
    data["number"] = re.sub(r"\s", "", data["number"])
    data["buyer_tax_id"] = data["buyer_tax_id"].replace(" ", "").upper()
    data["invoice_key"], data["kind"] = invoice_key(data["number"], data["code"])
    try:
        value = Decimal(data["amount"].replace(",", "").replace("¥", "").replace("￥", ""))
        if not value.is_finite() or value <= 0 or value >= Decimal("10000000000000000"):
            raise ValueError("票面金额必须大于零且在允许范围内")
        if value != value.quantize(Decimal("0.01")):
            raise ValueError("票面金额最多保留两位小数")
        data["amount"] = str(value.quantize(Decimal("0.01")))
    except InvalidOperation as exc:
        raise ValueError("无法识别票面价税合计") from exc
    raw_date = re.sub(r"[年/.]", "-", data["issue_date"]).replace("月", "-").replace("日", "")
    if re.fullmatch(r"\d{8}", raw_date):
        raw_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
    try:
        parts = raw_date[:10].split("-")
        data["issue_date"] = date(*[int(x) for x in parts]).isoformat()
    except (ValueError, TypeError) as exc:
        raise ValueError("无法识别开票日期") from exc
    if not re.fullmatch(r"[A-Z0-9]{15,20}", data["buyer_tax_id"]):
        raise ValueError("购买方税号缺失或格式不正确")
    if not data["buyer_name"]:
        raise ValueError("购买方名称缺失")
    return data


def fields_from_xml(data: bytes) -> tuple[dict, str]:
    root = ET.fromstring(data)
    values: dict[str, list[str]] = {}
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if element.text and element.text.strip():
            values.setdefault(tag, []).append(element.text.strip())
    fields = {}
    for field, aliases in FIELD_ALIASES.items():
        candidates = [x for alias in aliases for x in values.get(alias, [])]
        # 同一个身份字段出现多个不同值时拒绝自动信任，防止错误合并多张发票。
        if len(set(candidates)) == 1:
            fields[field] = candidates[0]
    return fields, "\n".join(f"{key}: {','.join(value)}" for key, value in values.items())


def fields_from_text(text: str) -> dict:
    text = unicodedata.normalize("NFKC", text)
    # OCR 可能把一个标签拆成多个文本框；只拼合已知标签，不拼接票号或猜测字符。
    for label in ("发票号码", "发票编号", "发票代码", "开票日期", "开票时间", "价税合计", "小写",
                  "购买方名称", "销售方名称", "购买方税号", "购方税号", "纳税人识别号", "统一社会信用代码"):
        text = re.sub(r"\s*".join(map(re.escape, label)), label, text)
    if len(set(re.findall(r"(?<!\d)\d{20}(?!\d)", text))) > 1:
        raise ParseRejected("同一附件包含多张数电发票，请拆分为独立原件后重新提交")
    if len(set(re.findall(r"(?:发票号码|发票编号|Invoice\s*(?:Number|No))\s*[:：]?\s*([0-9]{20}|[0-9]{8})(?![0-9])", text, re.I))) > 1:
        raise ParseRejected("同一附件包含不同发票号码，请拆分为独立原件后重新提交")
    patterns = {
        "number": r"(?:发票号码|发票编号|Invoice\s*(?:Number|No))\s*[:：]?\s*(\d{20}|\d{8})(?!\d)",
        "code": r"(?:发票代码|Invoice\s*Code)\s*[:：]?\s*(\d{10,12})",
        "issue_date": r"(?:开票日期|开票时间)\s*[:：]?\s*(\d{4}\s*[年/.-]\s*\d{1,2}\s*[月/.-]\s*\d{1,2}\s*日?)",
        "amount": r"(?:价税合计[^\n]{0,20}?(?:小写)?|小写)\s*[:：)）]?\s*[¥￥]?\s*([\d,]+\.\d{2})",
        "buyer_name": r"(?:购买方名称|购买方\s*名称|购方名称)\s*[:：]?\s*([^\n]+)",
        "buyer_tax_id": r"(?:购买方税号|购方税号|纳税人识别号|统一社会信用代码)\s*[:：]?\s*([A-Z0-9]{15,20})",
        "seller_name": r"(?:销售方名称|销售方\s*名称|销方名称)\s*[:：]?\s*([^\n]+)",
    }
    fields = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, re.I)
        if match:
            fields[key] = match.group(1).strip()
            if key == "issue_date":
                fields[key] = re.sub(r"\s+", "", fields[key])
            if key in {"buyer_name", "seller_name"}:
                fields[key] = re.split(r"\s{2,}|(?:销售方|购买方)(?:信息|名称)", fields[key], maxsplit=1)[0].strip()
    # 常见版式将“购买方/销售方信息”和“名称”拆成不同文本对象，保留上下文辅助提取。
    for field, label in (("buyer_name", "购买方"), ("seller_name", "销售方")):
        if not fields.get(field) and label in text:
            region = text.split(label, 1)[1][:700]
            match = re.search(r"名称\s*[:：]\s*([^\n]+)", region)
            if match:
                candidate = match.group(1).strip()
                suffix = re.search(r"^(.{2,100}?(?:有限责任公司|有限公司|分公司|工作室|中心))", candidate)
                fields[field] = suffix.group(1) if suffix else candidate[:150]
    # PDF 布局模式会把纵排“购买方/销售方”拆到数行，名称仍与首字“购/销”同处一行。
    for field, label in (("buyer_name", "购"), ("seller_name", "销")):
        if not fields.get(field):
            match = re.search(rf"(?:^|\s){label}\s+名称\s*[:：]\s*([^\n]+)", text, re.M)
            if match:
                fields[field] = re.split(r"\s{2,}", match.group(1).strip(), maxsplit=1)[0]
    if not fields.get("number"):
        numbers = set(re.findall(r"(?<!\d)\d{20}(?!\d)", text))
        if len(numbers) == 1:
            fields["number"] = numbers.pop()
    return fields


def _pdf_text_source(path: Path, text: str) -> dict:
    """普通文字与坐标结果共同组成 TEXT；只有规范化后无歧义的候选可参与本地裁决。"""
    fields = fields_from_text(text)
    error = ""
    try:
        positioned = read_pdf_invoice_fields(path)
    except ParseRejected:
        raise
    except Exception as exc:
        logger.warning("pdf_coordinates_failed error_type=%s", type(exc).__name__)
        positioned = {}
        error = f"PDF 坐标补充提取失败（{type(exc).__name__}），保留普通文字候选"
    candidates = copy.deepcopy(positioned.get("field_candidates", {}))
    ambiguous = set(positioned.get("ambiguous_fields", []))
    for field in FIELD_LABELS:
        values = set(candidates.get(field, []))
        if value := normalized_candidate(field, fields.get(field)):
            values.add(value)
        if values:
            candidates[field] = sorted(values)
            if len(values) > 1:
                ambiguous.add(field)
            # 歧义时仍留原候选供界面人工核对，compare_sources 不会将其判为已识别。
            fields[field] = normalized_candidate(field, fields.get(field)) or sorted(values)[0]
    return {"status": "COMPLETED", "fields": fields, "raw_text": text[:30000], "error": error,
            "ambiguous_fields": sorted(ambiguous), "field_candidates": candidates}


def parse(path: Path) -> dict:
    """只读取结构化原件或 PDF 文字层，通用材料可以复用而无需发起云端识别。"""
    text, fields, source = "", {}, "TEXT"
    suffix = ""
    sources = {}
    try:
        suffix, _ = detect_file_type(path.read_bytes(), path.name)
        if suffix == ".xml":
            fields, text = fields_from_xml(path.read_bytes())
            source = "XML"
            sources["XML"] = {"status": "COMPLETED", "fields": fields, "raw_text": text[:30000], "error": ""}
        elif suffix == ".ofd":
            source = "OFD"
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if sum(x.file_size for x in infos) > 100 * 1024 * 1024 or len(infos) > 1500:
                    raise ParseRejected("OFD 解压内容超过允许范围")
                pieces = []
                for info in infos:
                    if info.filename.lower().endswith(".xml"):
                        xml_fields, xml_text = fields_from_xml(archive.read(info))
                        for key, value in xml_fields.items():
                            if key in fields and fields[key] != value:
                                raise ParseRejected("OFD 内的票面身份信息冲突")
                            fields[key] = value
                        root = ET.fromstring(archive.read(info))
                        # OFD 页面文字按 TextObject 输出，不能把 XML 标签名混进发票字段之间。
                        text_objects = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "TextObject"]
                        if text_objects:
                            for node in text_objects:
                                pieces.append("".join(part.text or "" for part in node.iter()
                                    if part.tag.rsplit("}", 1)[-1] == "TextCode"))
                        else:
                            pieces.append(xml_text)
                text = "\n".join(pieces)
                fields = {**fields_from_text(text), **fields}
            sources["OFD"] = {"status": "COMPLETED", "fields": fields, "raw_text": text[:30000], "error": ""}
        elif suffix == ".pdf":
            try:
                text = "\n".join(item["text"] for item in read_pdf_segments(path))
                sources["TEXT"] = _pdf_text_source(path, text)
                fields = sources["TEXT"]["fields"]
            except PdfTextMissing as exc:
                sources["TEXT"] = {"status": "EMPTY", "fields": {}, "raw_text": "", "error": str(exc)}
            except Exception as exc:
                sources["TEXT"] = {"status": "FAILED", "fields": {}, "raw_text": text[:30000],
                                   "error": f"PDF 文字层提取失败（{type(exc).__name__}）"}
                if isinstance(exc, ParseRejected):
                    raise
        elif suffix in IMAGE_SUFFIXES:
            source = TENCENT_OCR
        else:
            raise ParseRejected("暂不支持此附件格式，请核对是否为发票原件")
        recognition = compare_sources(sources, structured=source == "XML", local_pdf=suffix == ".pdf")
        # 未解决时仍保留可编辑候选用于人工核对；只有 recognition_passed 才有自动资格。
        result = {"source": source, "sources": sources,
                  "reliable": source == "XML" or recognition["status"] == "LOCAL", "confirmed": False,
                  "raw_text": text[:30000], "error": "", "file_type": suffix, "pipeline_version": PIPELINE_VERSION,
                  "recognition": recognition,
                  "fallback_allowed": suffix in IMAGE_SUFFIXES or (suffix == ".pdf" and recognition["status"] == "UNCERTAIN")}
        if suffix == ".pdf":
            result["pdf_parser_version"] = PDF_TEXT_VERSION
        return _finalize(result)
    except Exception as exc:
        logger.warning("invoice_parse_failed source=%s file_type=%s error_type=%s", source, suffix, type(exc).__name__)
        message = str(exc) if isinstance(exc, ValueError) else f"本地解析失败（{type(exc).__name__}），请核对原件或人工处理"
        return {"fields": fields, "source": source, "sources": sources, "reliable": False,
                "confirmed": False, "raw_text": text[:30000], "error": message,
                "file_type": suffix, "error_type": type(exc).__name__, "pipeline_version": PIPELINE_VERSION,
                "rejected": isinstance(exc, ParseRejected),
                "recognition": {"status": "UNCERTAIN", "reason": message, "comparisons": []},
                "fallback_allowed": not isinstance(exc, ParseRejected) and suffix in IMAGE_SUFFIXES}


def _finalize(result: dict) -> dict:
    """最终字段用于业务规则；完整性或来源一致性未通过的候选仅供人工核对及临时建档。"""
    recognition = result["recognition"]
    fields = dict(recognition.get("fields") or {})
    for field in FIELD_LABELS:
        if field == "code" and not any(item["field"] == "code" and item["required"] for item in recognition.get("comparisons", [])):
            fields["code"] = ""
            continue
        if not fields.get(field):
            for source in result["sources"].values():
                candidate = normalized_candidate(field, source.get("fields", {}).get(field))
                if candidate:
                    fields[field] = candidate
                    break
    try:
        result["fields"] = normalize(fields)
        if not result.get("rejected"):
            result["error"] = ""
    except ValueError as exc:
        result["fields"] = fields
        if not result.get("rejected"):
            result["error"] = str(exc)
        recognition["status"] = "UNCERTAIN"
    result["local_error"] = "；".join(source["error"] for name, source in result["sources"].items()
                                            if name != "MODEL" and source.get("error"))
    return result


def _reject(result: dict, reason: str) -> dict:
    result.update(rejected=True, fallback_allowed=False, error=reason)
    result["recognition"].update(status="UNCERTAIN", reason=reason, fields={})
    return _finalize(result)


def parse_with_fallback(path: Path, config, *, ocr_config: "OCRConfigSnapshot", approval_id: str | None,
                        template_code: str, parent_db,
                        sha256: str = "", previous: dict | None = None, local_result: dict | None = None,
                        agent_conversation_id: str | None = None, before_request: Callable[[], None] | None = None) -> dict:
    """PDF 优先本地，图片交叉核对；审批调用方在 OCR 或模型请求前复核快照。"""
    result = copy.deepcopy(local_result) if local_result is not None else parse(path)
    pdf_fallback = (result.get("file_type") == ".pdf" and not result.get("rejected")
                    and result.get("recognition", {}).get("status") == "UNCERTAIN")
    result["cache_key"] = recognition_cache_key(sha256 or hashlib.sha256(path.read_bytes()).hexdigest(), config,
        ocr_version=ocr_config.recognition_version, file_type=result.get("file_type", ""), pdf_model=pdf_fallback)
    if pdf_fallback:
        return _model_fallback(path, result, config, approval_id=approval_id, template_code=template_code,
            parent_db=parent_db, previous=previous, agent_conversation_id=agent_conversation_id,
            local_pdf=True, before_request=before_request)
    # XML、OFD、已明确拒绝的原件及本地字段完整的 PDF 不进入图片识别路径。
    if result.get("file_type") not in IMAGE_SUFFIXES or not result.get("fallback_allowed"):
        return result
    pages, image_error = None, None
    if ocr_config.configured:
        from .tencent_invoice_ocr import recognize_invoice
        try:
            pages = invoice_page_images(path, result["file_type"])
        except ParseRejected as exc:
            return _reject(result, str(exc))
        except Exception as exc:
            image_error = exc
            ocr_source = {"status": "FAILED", "fields": {}, "error": "原件无法转换为腾讯云 OCR 可读图像"}
        else:
            if before_request:
                before_request()
            ocr_source = recognize_invoice(pages, ocr_config)
    else:
        ocr_source = {"status": "FAILED" if ocr_config.enabled else "SKIPPED", "fields": {},
            "error": (ocr_config.credential_error or "缺少腾讯云 OCR 凭据") if ocr_config.enabled else "腾讯云发票 OCR 未启用"}
    result["sources"][TENCENT_OCR] = ocr_source
    if not result["raw_text"]:
        result["raw_text"] = ocr_source.get("raw_text", "")
    result["recognition"] = compare_sources(result["sources"])
    if ocr_source.get("rejected"):
        return _reject(result, ocr_source["error"])
    if result["recognition"]["status"] == "VERIFIED":
        return _finalize(result)
    return _model_fallback(path, result, config, approval_id=approval_id, template_code=template_code,
        parent_db=parent_db, previous=previous, agent_conversation_id=agent_conversation_id,
        pages=pages, image_error=image_error, before_request=before_request)


def _model_fallback(path: Path, result: dict, config, *, approval_id: str | None, template_code: str,
                    parent_db, previous: dict | None, agent_conversation_id: str | None,
                    pages=None, image_error=None, local_pdf=False, before_request: Callable[[], None] | None = None) -> dict:
    """共用图像模型调用、日志和缓存；只提供原件图像，不向模型预填本地候选值。"""

    def finish() -> dict:
        result["recognition"] = compare_sources(result["sources"], local_pdf=local_pdf)
        return _finalize(result)

    # 同一原件和配置下，成功的独立图像识别只调用一次。OCR 失败重试时可以继续
    # 复用这份模型证据；模型本身失败时才允许下一轮重新调用。
    previous = previous or {}
    cached_model = previous.get("sources", {}).get("MODEL", {})
    if previous.get("cache_key") == result["cache_key"] and cached_model.get("status") == "COMPLETED":
        result["sources"]["MODEL"] = {**copy.deepcopy(cached_model), "cached": True}
        result["model_call_id"] = previous.get("model_call_id", "")
        return finish()
    if not config.configured:
        result["sources"]["MODEL"] = {"status": "FAILED", "fields": {}, "clear_fields": {},
            "error": config.credential_error or "未配置或启用支持图像的模型，无法完成票面核对"}
        return finish()
    from .agent_runtime import INVOICE_EXTRACTION_PROMPT, INVOICE_EXTRACTION_CONSTRAINTS, extract_invoice
    from .ai_logs import AICallRecorder

    trace = AICallRecorder(config, source="AGENT_ATTACHMENT" if agent_conversation_id else "INVOICE_EXTRACTION",
        prompt=INVOICE_EXTRACTION_PROMPT, agent_conversation_id=agent_conversation_id,
        constraints=INVOICE_EXTRACTION_CONSTRAINTS, approval_id=approval_id, template_code=template_code, parent_db=parent_db)
    try:
        trace.progress("本地字段不完整，准备图像模型兜底" if local_pdf else "准备原件图像，独立复核票面字段", force=True)
        if image_error is not None:
            raise image_error
        if pages is None:
            pages = invoice_page_images(path, result["file_type"])
        images = model_image_blocks(pages)
    except Exception as exc:
        message = str(exc) if isinstance(exc, ParseRejected) else "原件无法转换为模型可读图像，请核对文件是否完整"
        trace.finish({"status": "FAILED", "summary": message}, error_type=type(exc).__name__)
        result["sources"]["MODEL"] = {"status": "FAILED", "fields": {}, "error": message}
        result["model_call_id"] = trace.id
        return finish()
    extraction = extract_invoice(config, images, trace=trace, before_request=before_request)
    result["model_call_id"] = trace.id
    result["sources"]["MODEL"] = {"status": extraction["status"], "fields": extraction.get("fields", {}),
        "clear_fields": extraction.get("clear_fields", {}), "error": "" if extraction["status"] == "COMPLETED" else extraction["summary"]}
    if extraction.get("rejected"):
        return _reject(result, extraction["summary"])
    return finish()
