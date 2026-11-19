"""发票提取：PDF、图片先分类；PDF 本地优先，图片 OCR 主识别。"""
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
                                  normalized_candidate, recognition_cache_key, compare_image_sources, DOCUMENT_RECOGNITION_VERSION)
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


def fields_from_text(text: str, *, allow_unlabeled_number: bool = True) -> dict:
    """提取票面文字字段。PDF 关闭无标签号码推断，由调用方补充坐标候选。"""
    text = unicodedata.normalize("NFKC", text)
    # OCR 可能把一个标签拆成多个文本框；只拼合已知标签，不拼接票号或猜测字符。
    for label in ("发票号码", "发票编号", "发票代码", "开票日期", "开票时间", "价税合计", "小写",
                  "购买方名称", "销售方名称", "购买方税号", "购方税号", "纳税人识别号", "统一社会信用代码"):
        text = re.sub(r"\s*".join(map(re.escape, label)), label, text)
    # OFD 兼容无标签的数电票号。PDF 备注中的银行账号也可能恰好是 20 位数字。
    numeric_candidates = set()
    if allow_unlabeled_number:
        numeric_candidates = set(re.findall(r"(?<!\d)\d{20}(?!\d)", text))
        if len(numeric_candidates) > 1:
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
    if not fields.get("number") and len(numeric_candidates) == 1:
        fields["number"] = numeric_candidates.pop()
    return fields


def _pdf_text_source(path: Path, text: str) -> dict:
    """普通文字与坐标结果共同组成 TEXT；只有规范化后无歧义的候选可参与本地裁决。"""
    fields = fields_from_text(text, allow_unlabeled_number=False)
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
    """先分类再按需识别票面；首页分类不承担字段提取、多票或正文完整性校验。

    PDF 本地字段完整时不调用票面模型；图片 OCR 完整时不调用模型补读。
    高清图像仅在票面识别需要时创建，不能复用分类阶段的低分辨率首页。
    """
    from .document_classification import LEGACY_CLASSIFICATION_VERSION, classify_document
    from .invoice_recognition import recognition_cache_valid
    previous = previous or {}
    sha256 = sha256 or hashlib.sha256(path.read_bytes()).hexdigest()
    suffix, _ = detect_file_type(path.read_bytes(), path.name)
    if previous.get("confirmed") and not previous.get("error"):
        return {**copy.deepcopy(previous), "material_type": "INVOICE", "parse_status": "PARSED"}
    if recognition_cache_valid(previous, sha256, config, ocr_version=ocr_config.recognition_version):
        cached = copy.deepcopy(previous)
        if cached.get("classification"):
            cached["classification"]["cached"] = True
        return cached
    if suffix in {".pdf", *IMAGE_SUFFIXES}:
        from .recognition_cache import has_manual_recognition, reusable_automatic_recognition
        if not has_manual_recognition(previous):
            cached = reusable_automatic_recognition(parent_db, approval_id, sha256, suffix, config,
                ocr_version=ocr_config.recognition_version)
            if cached is not None:
                # 来源序号改变不影响文件内容。只复用自动证据，当前附件自己的付款材料仍独立校验。
                if previous.get("material"):
                    cached["material"] = copy.deepcopy(previous["material"])
                if cached.get("classification"):
                    cached["classification"]["cached"] = True
                return cached
    classification = classify_document(path, config, sha256=sha256, file_type=suffix,
        approval_id=approval_id, template_code=template_code, parent_db=parent_db, previous=previous,
        agent_conversation_id=agent_conversation_id, before_request=before_request)
    # 非发票和待确认材料不进入 parse()，防止为分类预先读取整份 PDF、渲染全部页面。
    result = {"source": "TEXT" if suffix == ".pdf" else TENCENT_OCR if suffix in IMAGE_SUFFIXES else suffix[1:].upper(),
              "sources": {}, "fields": {}, "raw_text": "", "file_type": suffix,
              "confirmed": False, "error": "", "reliable": False, "pipeline_version": PIPELINE_VERSION,
              "classification": classification, "material_type": classification["material_type"],
              "document_policy_version": DOCUMENT_RECOGNITION_VERSION}
    if previous.get("material"):
        result["material"] = copy.deepcopy(previous["material"])

    def uncertain(reason, *, rejected=False):
        # 未确认类别时不把候选写进发票台账；旧台账关联由调用方保留，禁止借分类释放占用。
        result.update(material_type="UNKNOWN" if not rejected else "INVOICE", fields={}, reliable=False,
                      error=reason, rejected=rejected, parse_status="FAILED", fallback_allowed=False)
        result["recognition"] = {"status": "UNCERTAIN", "reason": reason, "fields": {}, "comparisons": []}
        return result

    if classification.get("status") != "COMPLETED" or not classification.get("complete"):
        return uncertain(classification["reason"])
    if classification["material_type"] != "INVOICE":
        # 已有可信票面与新类别冲突时仍交人工；不为这项核对重新解析普通附件全文。
        prior_ocr = previous.get("sources", {}).get(TENCENT_OCR, {})
        prior_fields = prior_ocr.get("fields") or {}
        prior_text = previous.get("sources", {}).get("TEXT") or {}
        positive = (suffix == ".pdf" and previous.get("pdf_parser_version") == PDF_TEXT_VERSION
                    and compare_sources({"TEXT": prior_text}, local_pdf=True)["status"] == "LOCAL") or (
            prior_ocr.get("status") == "COMPLETED" and normalized_candidate("number", prior_fields.get("number"))
            and normalized_candidate("amount", prior_fields.get("amount")))
        if positive and classification.get("source") != "MANUAL":
            result["classification"] = {**classification, "status": "UNKNOWN", "complete": False, "description": None,
                "material_type": "UNKNOWN", "reason": "材料分类与已有有效票面字段矛盾，请人工核对原件"}
            return uncertain(result["classification"]["reason"])
        result.update(parse_status="SKIPPED", recognition=None, skip_reason=classification["reason"],
                      fallback_allowed=False, rejected=False)
        result["cache_key"] = recognition_cache_key(sha256, config, ocr_version=ocr_config.recognition_version, file_type=suffix)
        return result

    # 只有确认是发票才读取完整票面。既有本地字段缓存仍独立于分类模型配置。
    cached_text = previous.get("sources", {}).get("TEXT")
    if (suffix == ".pdf" and cached_text and previous.get("pdf_parser_version") == PDF_TEXT_VERSION
            and previous.get("file_type") == suffix):
        local = {"source": "TEXT", "sources": {"TEXT": copy.deepcopy(cached_text)},
            "raw_text": cached_text.get("raw_text", ""), "confirmed": False, "error": "", "file_type": suffix,
            "pipeline_version": PIPELINE_VERSION, "pdf_parser_version": PDF_TEXT_VERSION}
        local["recognition"] = compare_sources(local["sources"], local_pdf=True)
        local = _finalize(local)
    else:
        local = copy.deepcopy(local_result) if local_result is not None else parse(path)
    result.update(local)
    result.update(classification=classification, material_type="INVOICE",
                  document_policy_version=DOCUMENT_RECOGNITION_VERSION)
    if result.get("rejected"):
        return uncertain(result["error"], rejected=True)
    if suffix in {".xml", ".ofd"}:
        # XML 的字段条件和 OFD 的人工核对规则沿用原策略，分类成功不代表字段合格。
        result["parse_status"] = "PARSED"
        result["cache_key"] = recognition_cache_key(sha256, config, ocr_version=ocr_config.recognition_version, file_type=suffix)
        return result

    pages = None

    def full_page_images():
        nonlocal pages
        if pages is None:
            try:
                pages = invoice_page_images(path, suffix)
            except ParseRejected:
                raise
            except Exception as exc:
                raise ParseRejected("原件页面无法完整读取，请核对文件") from exc
        return pages

    def fallback_model():
        """仅在票面字段不完整时独立补读；兼容复用旧全页分类留下的高清字段证据。"""
        if (classification.get("version") == LEGACY_CLASSIFICATION_VERSION
                and classification.get("invoice_count") == 1 and classification.get("fields")):
            result["model_call_id"] = classification.get("model_call_id", "")
            return {"status": "COMPLETED", "fields": classification["fields"],
                    "clear_fields": classification.get("clear_fields", {}), "error": "", "cached": True}
        from .agent_runtime import INVOICE_EXTRACTION_PROMPT, INVOICE_EXTRACTION_CONSTRAINTS, extract_invoice
        from .ai_logs import AICallRecorder
        images = model_image_blocks(full_page_images())
        trace = AICallRecorder(config, source="AGENT_ATTACHMENT" if agent_conversation_id else "INVOICE_EXTRACTION",
            prompt=INVOICE_EXTRACTION_PROMPT, constraints=INVOICE_EXTRACTION_CONSTRAINTS, approval_id=approval_id,
            template_code=template_code, parent_db=parent_db, agent_conversation_id=agent_conversation_id)
        read = extract_invoice(config, images, trace=trace, before_request=before_request)
        result["model_call_id"] = trace.id
        return {"status": read["status"], "fields": read.get("fields", {}), "clear_fields": read.get("clear_fields", {}),
                "rejected": bool(read.get("rejected")),
                "error": "" if read["status"] == "COMPLETED" else read.get("summary", "票面补读未完成")}

    pdf_model = suffix == ".pdf" and result.get("recognition", {}).get("status") != "LOCAL"
    result["cache_key"] = recognition_cache_key(sha256, config, ocr_version=ocr_config.recognition_version,
        file_type=suffix, pdf_model=pdf_model)
    try:
        if suffix == ".pdf":
            if pdf_model:
                result["sources"]["MODEL"] = fallback_model()
                if result["sources"]["MODEL"].get("rejected"):
                    return uncertain(result["sources"]["MODEL"]["error"], rejected=True)
            result["recognition"] = compare_sources(result["sources"], local_pdf=True)
        else:
            if ocr_config.configured:
                from .tencent_invoice_ocr import recognize_invoice
                images = full_page_images()
                if before_request:
                    before_request()
                ocr_source = recognize_invoice(images, ocr_config)
            else:
                ocr_source = {"status": "FAILED" if ocr_config.enabled else "SKIPPED", "fields": {},
                    "error": (ocr_config.credential_error or "缺少腾讯云 OCR 凭据") if ocr_config.enabled else "腾讯云发票 OCR 未启用"}
            result["sources"][TENCENT_OCR] = ocr_source
            result["raw_text"] = ocr_source.get("raw_text", "")
            if ocr_source.get("rejected"):
                return uncertain(ocr_source["error"], rejected=True)
            result["recognition"] = compare_image_sources(result["sources"])
            if result["recognition"]["status"] != "OCR":
                result["sources"]["MODEL"] = fallback_model()
                if result["sources"]["MODEL"].get("rejected"):
                    return uncertain(result["sources"]["MODEL"]["error"], rejected=True)
                result["recognition"] = compare_image_sources(result["sources"])
    except ParseRejected as exc:
        return uncertain(str(exc), rejected=True)
    result["reliable"] = result["recognition"]["status"] in {"LOCAL", "OCR"}
    result["parse_status"] = "PARSED"
    return _finalize(result)
