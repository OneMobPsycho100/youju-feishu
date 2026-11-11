"""发票识别策略：PDF 接受完整本地字段，图片按独立来源交叉核对。"""
import hashlib
import hmac
import json
import re
import unicodedata
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from .config import settings

PIPELINE_VERSION = "pdf-local-image-cross-check-v3"
# 仅参与 PDF 缓存键，不改变图片识别策略或触发启动时的全量审核失效。
PDF_TEXT_VERSION = "pdf-text-coordinates-model-fallback-v1"
TENCENT_OCR = "TENCENT_OCR"
FIELD_LABELS = {
    "number": "发票号码", "code": "发票代码", "buyer_name": "购买方名称",
    "buyer_tax_id": "购买方税号", "seller_name": "销售方名称",
    "issue_date": "开票日期", "amount": "价税合计",
}


def normalized_candidate(field: str, value) -> str:
    """逐字段规范化部分候选；无效值返回空串，禁止纠正、补齐或猜测号码。"""
    text = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or "")))
    if field == "number":
        return text if re.fullmatch(r"[0-9]{20}|[0-9]{8}", text) else ""
    if field == "code":
        return text if re.fullmatch(r"[0-9]{10}|[0-9]{12}", text) else ""
    if field == "buyer_tax_id":
        text = text.upper()
        return text if re.fullmatch(r"[A-Z0-9]{15,20}", text) else ""
    if field in {"buyer_name", "seller_name"}:
        return text if 1 < len(text) <= 150 else ""
    if field == "amount":
        try:
            number = Decimal(text.replace(",", "").replace("¥", "").replace("￥", ""))
            if number.is_finite() and 0 < number < Decimal("10000000000000000") and number == number.quantize(Decimal("0.01")):
                return str(number.quantize(Decimal("0.01")))
        except (InvalidOperation, ValueError):
            pass
        return ""
    if field == "issue_date":
        text = re.sub(r"[年/.]", "-", text).replace("月", "-").replace("日", "")
        if re.fullmatch(r"[0-9]{8}", text):
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        if re.fullmatch(r"[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}", text):
            try:
                return date(*map(int, text.split("-"))).isoformat()
            except ValueError:
                pass
    return ""


def _compare_pdf_sources(sources: dict) -> dict:
    """本地明确字段优先；模型只补缺失项或确认已有歧义候选，不覆盖相互冲突的值。"""
    text, model = sources.get("TEXT", {}), sources.get("MODEL", {})
    fields, comparisons, unresolved = {}, [], []
    model_used = False
    for field, label in FIELD_LABELS.items():
        local = normalized_candidate(field, (text.get("fields") or {}).get(field))
        choices = {value for raw in text.get("field_candidates", {}).get(field, [])
                   if (value := normalized_candidate(field, raw))}
        if local:
            choices.add(local)
        ambiguous = field in text.get("ambiguous_fields", []) or len(choices) > 1
        local_clear = text.get("status") == "COMPLETED" and bool(local) and not ambiguous
        fallback = normalized_candidate(field, (model.get("fields") or {}).get(field))
        model_clear = (model.get("status") == "COMPLETED" and bool(fallback)
                       and (model.get("clear_fields") or {}).get(field) is True)
        accepted, matched, status = "", [], "CONFLICT" if ambiguous else "MISSING"
        if local_clear:
            if model_clear and local != fallback:
                status = "CONFLICT"
            else:
                accepted, matched, status = local, ["TEXT"], "AGREED"
                if model_clear:
                    matched.append("MODEL")
        elif model_clear:
            if choices and fallback not in choices:
                status = "CONFLICT"
            else:
                accepted, matched, status = fallback, ["MODEL"], "AGREED"
                if choices and text.get("status") == "COMPLETED":
                    matched.insert(0, "TEXT")
        required = field != "code" or len(fields.get("number", "")) == 8
        if not required:
            accepted, matched, status = "", [], "NOT_REQUIRED"
        fields[field] = accepted
        if required and not accepted:
            unresolved.append(label)
        model_used |= required and "MODEL" in matched
        comparisons.append({"field": field, "label": label, "required": required,
            "values": {name: str((source.get("fields") or {}).get(field) or "")
                       for name, source in sources.items() if name in {"TEXT", "MODEL"}},
            "accepted": accepted, "matched_sources": matched, "status": status})
    if unresolved:
        reason = "PDF 字段仍缺失或存在分歧，请人工核对：" + "、".join(unresolved)
    else:
        reason = "PDF 缺失或歧义字段已由多模态清晰辨认并补齐" if model_used else "PDF 本地解析的必需字段完整"
    return {"status": "UNCERTAIN" if unresolved else "VERIFIED" if model_used else "LOCAL",
            "fields": fields, "comparisons": comparisons, "reason": reason}


def compare_sources(sources: dict, *, structured=False, local_pdf=False) -> dict:
    """逐字段校验来源；XML 和 PDF 本地文字层允许单路完整字段通过。

    PDF 采用 TEXT 与当前配置的 MODEL 兜底，不混入 OCR。图片仍须两个独立
    来源一致；重复页和重复运行不增加票数，不清晰字段不参与裁决。
    """
    if local_pdf:
        return _compare_pdf_sources(sources)
    minimum_sources = 1 if structured else 2
    fields, comparisons, unresolved = {}, [], []
    for field, label in FIELD_LABELS.items():
        votes, values = defaultdict(list), {}
        for name, source in sources.items():
            raw = str((source.get("fields") or {}).get(field) or "")
            values[name] = raw
            clear = name != "MODEL" or (source.get("clear_fields") or {}).get(field) is True
            value = normalized_candidate(field, raw)
            if source.get("status") == "COMPLETED" and clear and field not in source.get("ambiguous_fields", []) and value:
                votes[value].append(name)
        required = field != "code" or len(fields.get("number", "")) == 8
        winners = [(value, names) for value, names in votes.items() if len(names) >= minimum_sources]
        accepted, matched = winners[0] if len(winners) == 1 else ("", [])
        status = "AGREED" if accepted else "CONFLICT" if len(votes) > 1 else "MISSING"
        if not required:
            accepted, matched, status = "", [], "NOT_REQUIRED"
        fields[field] = accepted
        if required and not accepted:
            unresolved.append(label)
        comparisons.append({"field": field, "label": label, "required": required, "values": values,
                            "accepted": accepted, "matched_sources": matched, "status": status})
    if unresolved:
        prefix = "以下字段未取得有效一致结果："
        reason = prefix + "、".join(unresolved)
    else:
        reason = ("结构化字段完整" if structured
                  else "全部必需字段均已取得至少两个独立来源的一致结果")
    return {"status": "UNCERTAIN" if unresolved else "STRUCTURED" if structured else "VERIFIED",
            "fields": fields, "comparisons": comparisons, "reason": reason}


def recognition_passed(result: dict) -> bool:
    """人工确认绑定当前附件；本地解析或交叉核对通过均须使用当前策略版本。"""
    return bool(not result.get("error") and (result.get("confirmed") or
        (result.get("pipeline_version") == PIPELINE_VERSION and
         result.get("recognition", {}).get("status") in {"LOCAL", "VERIFIED", "STRUCTURED"})))


def recognition_cache_key(sha256: str, config, *, ocr_version: int = 0, file_type: str = "", pdf_model=False) -> str:
    """本地 PDF 仅绑定原件与解析版本；模型兜底另绑定模型配置，密钥使用 HMAC。"""
    if file_type == ".pdf" and not pdf_model:
        material = {"sha256": sha256, "pipeline": PIPELINE_VERSION, "file_type": file_type,
                    "pdf_parser": PDF_TEXT_VERSION}
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    cfg = settings()
    key_token = hmac.new(cfg.session_secret.encode(), b"invoice-recognition:v1\0" + config.api_key.encode(),
                         hashlib.sha256).hexdigest()
    material = {"sha256": sha256, "pipeline": PIPELINE_VERSION, "ocr_version": ocr_version,
                "model_enabled": config.enabled, "base_url": config.base_url, "model_name": config.model_name,
                "connection_version": config.connection_version, "key_token": key_token}
    if file_type == ".pdf":
        material.update(pdf_parser=PDF_TEXT_VERSION, ocr_version=0)
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def recognition_cache_valid(result: dict, sha256: str, config, *, ocr_version: int) -> bool:
    """成功或明确拒绝的结果复用；临时失败可重试，已有人工确认始终保留。"""
    if result.get("confirmed") and not result.get("error"):
        return True
    return bool((recognition_passed(result) or result.get("rejected")) and
                result.get("cache_key") == recognition_cache_key(sha256, config, ocr_version=ocr_version,
                    file_type=result.get("file_type", ""),
                    pdf_model=result.get("file_type") == ".pdf" and "MODEL" in result.get("sources", {})))
