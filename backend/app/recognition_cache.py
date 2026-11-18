"""按同审批内的原件内容复用自动识别，附件位置和业务关联不参与缓存继承。"""
import copy
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from .invoice_recognition import recognition_cache_valid
from .models import Attachment

if TYPE_CHECKING:
    from .model_config import EffectiveModelConfig


# 只继承分类及票面证据。人工确认、付款正文、整单判断、归档成员和发票关联均保留原绑定。
AUTOMATIC_RESULT_FIELDS = frozenset({
    "source", "sources", "fields", "raw_text", "reliable", "error", "local_error", "file_type",
    "pipeline_version", "pdf_parser_version", "cache_key", "recognition", "classification",
    "material_type", "document_policy_version", "parse_status", "skip_reason", "fallback_allowed",
    "rejected", "model_call_id", "confirmed",
})


def has_manual_recognition(result: dict) -> bool:
    """识别人工作业痕迹；人工结果不能因文件内容相同而转移到另一附件记录。"""
    return bool(result.get("confirmed") or result.get("confirmed_by") or result.get("confirmed_at")
        or result.get("confirmation_comment") or result.get("manual_review")
        or (result.get("material") or {}).get("confirmation")
        or (result.get("classification") or {}).get("source") == "MANUAL"
        or result.get("source") == "MANUAL" or "MANUAL" in (result.get("sources") or {}))


def reusable_automatic_recognition(db: Session | None, approval_id: str | None, sha256: str,
                                  file_type: str, config: "EffectiveModelConfig", *,
                                  ocr_version: int) -> dict | None:
    """返回同审批、同内容、同实际格式的最近有效自动缓存副本，找不到时返回 None。

    包括已停用的历史附件，以支持插入、删除或重排后来源键改变的原件。缓存仍须通过
    当前分类、票面策略及模型/OCR 指纹校验；失败、不完整和人工结果不参与复用。
    独立短读会话在返回前关闭，避免模型请求期间持有调用方数据库读快照。
    """
    if db is None or not approval_id or not sha256:
        return None
    statement = select(Attachment.parsed_data).where(
        Attachment.approval_id == approval_id, Attachment.sha256 == sha256
    ).order_by(Attachment.updated_at.desc(), Attachment.created_at.desc(), Attachment.id.desc())
    with Session(bind=db.get_bind()) as lookup:
        for result in lookup.scalars(statement):
            if (not result or result.get("file_type") != file_type or has_manual_recognition(result)
                    or result.get("error") or result.get("rejected")
                    or result.get("parse_status") not in {"PARSED", "SKIPPED"}):
                continue
            if recognition_cache_valid(result, sha256, config, ocr_version=ocr_version):
                return copy.deepcopy({key: value for key, value in result.items() if key in AUTOMATIC_RESULT_FIELDS})
    return None
