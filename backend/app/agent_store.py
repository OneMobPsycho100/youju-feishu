"""Agent 的短事务存储、属主校验和事件投影；SSE 游标只在事务提交后可见。"""
import json
import re

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select

from . import agent_schemas as a
from .db import begin_write
from .models import (AgentAttachment, AgentConversation, AgentEvent, AgentMessage, AgentProposal, AgentRun,
                     ApprovalAction, Job, now, uid)
from .schemas import JobOut


def clean(value, secrets=()):
    """只投影可公开的数据；明确剔除凭据、部署路径和远端原始快照。"""
    if isinstance(value, dict):
        blocked = {"api_key", "api_key_encrypted", "authorization", "headers", "storage_key", "external_snapshot"}
        return {str(key): clean(item, secrets) for key, item in value.items() if str(key).lower() not in blocked}
    if isinstance(value, (tuple, list)):
        return [clean(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[已隐藏密钥]")
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/-]{12,}", "Bearer [已隐藏]", value)[:100000]
    return jsonable_encoder(value)


def owned(db, conversation_id, user):
    conversation = db.get(AgentConversation, conversation_id)
    if not conversation or conversation.owner_id != user.id:
        raise HTTPException(404, "会话不存在或不属于当前账号")
    return conversation


def emit(db, conversation_id: str, kind: str, data: dict):
    """调用方必须持有短写事务；游标与业务变化一起提交，避免重连漏掉事件。"""
    begin_write(db)
    conversation = db.get(AgentConversation, conversation_id)
    conversation.event_seq += 1
    conversation.updated_at = now()
    db.add(AgentEvent(conversation_id=conversation_id, seq=conversation.event_seq, kind=kind, data=clean(data)))


def message_dump(row):
    return a.AgentMessageOut.model_validate(row).model_dump(mode="json")


def run_dump(row):
    return a.AgentRunOut.model_validate(row).model_dump(mode="json")


def attachment_dump(row):
    """事件、快照和文件工具共享白名单投影，禁止把 storage_key 或整份正文发到状态流。"""
    return a.AgentAttachmentOut.model_validate(row).model_dump(mode="json")


def add_message(db, conversation_id, run_id, role, content="", data=None):
    begin_write(db)
    message = AgentMessage(id=uid(), conversation_id=conversation_id, run_id=run_id, role=role,
                           content=content, data=data or {}, created_at=now())
    db.add(message)
    db.flush()
    emit(db, conversation_id, "message.created", {"message": message_dump(message)})
    return message


def proposal_dump(db, row):
    job = db.get(Job, row.job_id) if row.job_id else None
    operation = None
    if job and job.kind == "ACTION":
        operation = db.get(ApprovalAction, job.payload.get("action_id"))
    return {"id": row.id, "run_id": row.run_id, "kind": row.kind, "target_id": row.target_id,
        "status": "EXPIRED" if row.status == "PENDING" and row.expires_at <= now() else row.status,
        "display": row.display, "error": row.error,
        "job": JobOut.model_validate(job).model_dump(mode="json") if job else None,
        "operation_status": operation.status if operation else "", "expires_at": row.expires_at,
        "created_at": row.created_at}


def snapshot(db, conversation_id, user):
    from .agent_usage import attachment_usage
    conversation = owned(db, conversation_id, user)
    return {**a.ConversationOut.model_validate(conversation).model_dump(mode="json"),
        "messages": list(db.scalars(select(AgentMessage).where(AgentMessage.conversation_id == conversation_id)
                                    .order_by(AgentMessage.created_at, AgentMessage.id))),
        "runs": list(db.scalars(select(AgentRun).where(AgentRun.conversation_id == conversation_id)
                                .order_by(AgentRun.started_at))),
        "attachments": [attachment_dump(row) for row in db.scalars(select(AgentAttachment).where(
            AgentAttachment.conversation_id == conversation_id, AgentAttachment.deleted.is_(False)).order_by(AgentAttachment.created_at))],
        "attachment_usage": attachment_usage(db, conversation_id),
        "proposals": [proposal_dump(db, row) for row in db.scalars(select(AgentProposal)
            .where(AgentProposal.conversation_id == conversation_id).order_by(AgentProposal.created_at))]}


def proposal_updates(db, conversation_id):
    """每轮把用户已确认的真实任务结果补入上下文，历史模型文字不作为执行证据。"""
    return clean([proposal_dump(db, row) for row in db.scalars(select(AgentProposal)
        .where(AgentProposal.conversation_id == conversation_id).order_by(AgentProposal.created_at.desc()).limit(20))])
