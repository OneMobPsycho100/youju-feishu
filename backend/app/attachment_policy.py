"""报销附件格式白名单；普通附件保留原件，不送入发票识别。"""
import copy

from .file_types import IMAGE_SUFFIXES, detect_file_type
from .storage import resolve


INVOICE_SUFFIXES = {".pdf", ".xml", ".ofd", *IMAGE_SUFFIXES}


def invoice_attachment(attachment) -> bool:
    """按实际内容判断是否属于支持的发票容器，兼容误命名的 XLSX 或无扩展名原件。"""
    suffix, _ = detect_file_type(resolve(attachment.storage_key).read_bytes(), attachment.filename)
    return suffix in INVOICE_SUFFIXES


def skip_invoice_parsing(attachment) -> None:
    """将非发票格式标记为普通附件；保留原解析快照，便于追溯旧版本误解析。"""
    previous = attachment.parsed_data or {}
    if previous.get("parse_status") == "SKIPPED":
        return
    suffix, _ = detect_file_type(resolve(attachment.storage_key).read_bytes(), attachment.filename)
    attachment.parsed_data = {"material_type": "OTHER", "parse_status": "SKIPPED", "file_type": suffix,
        "skip_reason": "普通附件，已跳过发票解析，不参与发票金额及完整性核对",
        "fields": {}, "error": "", "confirmed": False,
        "previous_invoice_parse": copy.deepcopy(previous)}
