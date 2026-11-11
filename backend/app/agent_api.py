"""私人会话 HTTP 接口，以及仅能由短期运行令牌访问的 MCP 和流式模型桥接。"""
import asyncio
import json
import time
import threading
from contextvars import ContextVar

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from pydantic import ValidationError
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool
from starlette.routing import Route

from . import agent_schemas as a
from .agent_chat_runtime import manager
from .agent_store import (add_message, attachment_dump, clean, emit, message_dump, owned, proposal_dump, run_dump, snapshot)
from .agent_attachments import (EXTENSIONS, MAX_BYTES, MAX_FILES, attachment_manager, attachment_owned,
                                check_invoice_usage, create_attachment, read_attachment)
from .agent_tools import actor, confirm_proposal, create_proposal, download_links, read_business
from .agent_usage import attachment_usage, usage_summary
from .auth import current_user
from .config import settings
from .db import SessionLocal, begin_write, get_db
from .integrations import DomainError
from .model_config import load_model_config
from .models import AgentAttachment, AgentConversation, AgentEvent, AgentMessage, AgentProposal, AgentRun, User, now, uid
from .schemas import PageOut
from .storage import resolve

router = APIRouter(prefix="/api/v1/agent", tags=["业务 Agent"])
internal = APIRouter(include_in_schema=False)
runtime_context = ContextVar("business_agent_runtime", default=None)
admission_lock = threading.Lock()


@router.get("/usage", response_model=a.UsageSummaryOut, summary="Token Meter 用量统计；本人会话或管理员查看全局汇总")
def token_usage(days: int = Query(7, ge=1, le=90), scope: str = Query("mine", pattern="^(mine|all)$"),
                user=Depends(current_user), db=Depends(get_db)):
    if scope == "all" and user.role != "admin":
        raise HTTPException(403, "全局用量统计仅管理员可查看")
    return usage_summary(db, user, days, scope == "all")


@router.get("/capabilities", response_model=a.CapabilitiesOut, summary="读取本人可用的业务助手能力")
def capabilities(user=Depends(current_user), db=Depends(get_db)):
    config = load_model_config(db)
    writable = user.role in {"admin", "finance"}
    actions = ["AUDIT", "ARCHIVE", "APPROVE", "REJECT", "CORRECT", "RETRY"] if writable else []
    can_sync = user.role == "admin" and settings().app_mode == "live"
    if can_sync:
        actions.append("SYNC")
    active = db.scalar(select(AgentRun).where(AgentRun.active_owner == user.id))
    return {"available": config.configured, "reason": "已就绪，模型工具能力将在调用时验证" if config.configured else
        config.credential_error or "请由管理员在运行与模型中配置并启用模型", "model_name": config.model_name,
        "can_write": writable, "can_sync": can_sync, "actions": actions,
        "attachment_extensions": sorted(EXTENSIONS), "max_attachments": MAX_FILES,
        "max_attachment_bytes": min(MAX_BYTES, settings().max_file_mb * 1024 * 1024),
        "active_conversation_id": active.conversation_id if active else "", "active_run_id": active.id if active else ""}


@router.get("/conversations", response_model=PageOut[a.ConversationOut], summary="分页读取本人历史会话")
def conversations(page: int = Query(1, ge=1), page_size: int = Query(30, ge=1, le=100),
                  user=Depends(current_user), db=Depends(get_db)):
    from .business import page_result
    return page_result(db, select(AgentConversation).where(AgentConversation.owner_id == user.id)
        .order_by(AgentConversation.updated_at.desc()),
        lambda row: a.ConversationOut.model_validate(row).model_dump(mode="json"), page, page_size)


@router.post("/conversations", response_model=a.ConversationOut, summary="创建本人独立业务会话")
def create_conversation(data: a.ConversationInput, user=Depends(current_user), db=Depends(get_db)):
    begin_write(db)
    row = AgentConversation(id=uid(), owner_id=user.id, title=data.title, event_seq=0)
    db.add(row)
    db.commit()
    return row


@router.get("/conversations/{conversation_id}", response_model=a.ConversationDetail, summary="读取本人会话及业务处理状态")
def conversation_detail(conversation_id: str, user=Depends(current_user), db=Depends(get_db)):
    return snapshot(db, conversation_id, user)


@router.put("/conversations/{conversation_id}", response_model=a.ConversationOut, summary="重命名本人会话")
def rename_conversation(conversation_id: str, data: a.ConversationInput, user=Depends(current_user), db=Depends(get_db)):
    begin_write(db)
    row = owned(db, conversation_id, user)
    row.title = data.title
    db.commit()
    return row


@router.post("/conversations/{conversation_id}/attachments", response_model=a.AgentAttachmentOut,
             summary="上传本人会话附件并异步解析；每文件 20MB，上传重试复用 upload_id")
async def upload_attachment(conversation_id: str, file: UploadFile = File(description="支持的文档或发票原件"),
                            upload_id: str = Form(min_length=8, max_length=100, description="客户端生成的上传幂等标识"),
                            user=Depends(current_user), db=Depends(get_db)):
    owned(db, conversation_id, user)
    db.rollback()
    try:
        data = await file.read(MAX_BYTES + 1)
        return await run_in_threadpool(create_attachment, db, conversation_id, user, data, file.filename or "附件", upload_id)
    finally:
        await file.close()


@router.get("/attachments/{attachment_id}", response_model=a.AgentAttachmentOut, summary="读取本人附件的异步解析状态")
def attachment_detail(attachment_id: str, user=Depends(current_user), db=Depends(get_db)):
    return attachment_owned(db, attachment_id, user)


@router.get("/attachments/{attachment_id}/download", summary="鉴权下载本人附件，PDF 和常见图片支持受限预览")
def attachment_download(attachment_id: str, preview: bool = False, user=Depends(current_user), db=Depends(get_db)):
    row = attachment_owned(db, attachment_id, user)
    path = resolve(row.storage_key)
    if not path.is_file():
        raise HTTPException(404, "附件原件不存在，请重新上传")
    inline = preview and row.mime in {"application/pdf", "image/png", "image/jpeg", "image/webp"}
    return FileResponse(path, filename=row.filename, media_type=row.mime if inline else "application/octet-stream",
                        content_disposition_type="inline" if inline else "attachment")


@router.post("/attachments/{attachment_id}/retry", response_model=a.AgentAttachmentOut,
             summary="重试本人失败的附件解析，成功识别证据仍按原件及配置缓存复用")
def retry_attachment(attachment_id: str, user=Depends(current_user), db=Depends(get_db)):
    begin_write(db)
    row = attachment_owned(db, attachment_id, user)
    if row.status not in {"FAILED", "READY"}:
        return row
    if row.status == "READY" and not row.error:
        return row
    row.status, row.error = "QUEUED", ""
    emit(db, row.conversation_id, "attachment.updated", {"attachment": attachment_dump(row)})
    db.commit()
    attachment_manager.submit(attachment_id)
    db.expire_all()
    return db.get(AgentAttachment, attachment_id)


@router.delete("/attachments/{attachment_id}", summary="移除尚未发送的本人附件；已发送原件随会话保留")
def delete_attachment(attachment_id: str, user=Depends(current_user), db=Depends(get_db)):
    begin_write(db)
    row = attachment_owned(db, attachment_id, user)
    if row.submitted:
        raise HTTPException(409, "已发送附件随历史会话保留，不能作为草稿移除")
    row.deleted = True
    emit(db, row.conversation_id, "attachment.deleted", {"attachment_id": row.id})
    key, processing = row.storage_key, row.status == "PROCESSING"
    db.commit()
    # 解析中的文件交给工作线程 finally 清理，避免删除正在读取的原件。
    if not processing:
        resolve(key).unlink(missing_ok=True)
    return {"deleted": True}


@router.post("/conversations/{conversation_id}/messages", response_model=a.AgentRunOut, summary="提交带上下文快照的幂等消息")
def send_message(conversation_id: str, data: a.AgentMessageInput, user=Depends(current_user), db=Depends(get_db)):
    # 单进程部署下串行准入，先提交可恢复记录再启动进程；数据库唯一键同时约束本人并发与消息重放。
    with admission_lock:
        begin_write(db)
        conversation = owned(db, conversation_id, user)
        existing = db.scalar(select(AgentRun).where(AgentRun.conversation_id == conversation_id, AgentRun.request_id == data.request_id))
        if existing:
            message = db.get(AgentMessage, existing.user_message_id)
            if (message.content != clean(data.text, (load_model_config(db).api_key,))
                    or message.data.get("context") != data.context.model_dump()
                    or message.data.get("attachment_ids", []) != data.attachment_ids):
                raise DomainError("同一消息标识不能用于不同内容或上下文")
            return existing
        if db.scalar(select(AgentRun.id).where(AgentRun.active_owner == user.id)):
            raise HTTPException(409, "你已有一轮对话正在执行，请先等待或停止生成")
        config = load_model_config(db)
        if not config.configured:
            raise HTTPException(409, config.credential_error or "模型尚未配置或已停用，请联系管理员")
        with manager.lock:
            if manager.shutting_down or len(manager.handles) >= settings().agent_max_concurrency:
                raise HTTPException(429, "助手正在处理其他会话，请稍后再发；当前输入不会丢失")
        text = clean(data.text, (config.api_key,))
        context = data.context.model_dump()
        if context["company_id"]:
            from .models import Company
            if not db.get(Company, context["company_id"]):
                raise HTTPException(422, "当前公司不存在，请重新选择")
        attachments = [attachment_owned(db, file_id, user, conversation_id=conversation_id) for file_id in data.attachment_ids]
        if any(row.status != "READY" for row in attachments):
            raise HTTPException(409, "附件尚未解析完成；失败附件请重试或移除")
        for row in attachments:
            row.submitted = True
            emit(db, conversation_id, "attachment.updated", {"attachment": attachment_dump(row)})
        run_id = uid()
        message = add_message(db, conversation_id, run_id, "user", text,
            {"context": context, "attachment_ids": data.attachment_ids,
             "attachments": [attachment_dump(row) for row in attachments]})
        if conversation.title == "新对话":
            conversation.title = text[:30]
        run = AgentRun(id=run_id, conversation_id=conversation_id, owner_id=user.id, active_owner=user.id,
            request_id=data.request_id, user_message_id=message.id, status="RUNNING", stage="准备会话",
            model_name=config.model_name, config_version=config.version, usage={}, started_at=now())
        db.add(run)
        db.flush()
        emit(db, conversation_id, "run.updated", {"run": run_dump(run)})
        db.commit()
        try:
            manager.submit(run, context, config)
        except Exception:
            begin_write(db)
            run = db.get(AgentRun, run_id)
            run.status, run.active_owner, run.error, run.finished_at = "FAILED", None, "运行未启动，请稍后重试", now()
            emit(db, conversation_id, "run.updated", {"run": run_dump(run)})
            db.commit()
            raise
        return run


@router.get("/runs/{run_id}", response_model=a.AgentRunOut, summary="读取本人运行状态、耗时和供应商用量")
def run_detail(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = db.get(AgentRun, run_id)
    if not run or run.owner_id != user.id:
        raise HTTPException(404, "运行不存在")
    return run


@router.post("/runs/{run_id}/cancel", response_model=a.AgentRunOut, summary="停止本人当前生成，已确认任务继续执行")
def cancel_run(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    run = run_detail(run_id, user, db)
    db.rollback()
    if run.status == "RUNNING":
        manager.cancel(run_id)
    db.expire_all()
    return db.get(AgentRun, run_id)


@router.post("/actions/{proposal_id}/confirm", response_model=a.ProposalOut, summary="本人确认不可变方案，复核业务版本后提交任务")
def confirm_action(proposal_id: str, data: a.ConfirmInput, user=Depends(current_user), db=Depends(get_db)):
    return confirm_proposal(db, proposal_id, user, data.original_checked)


@router.post("/actions/{proposal_id}/cancel", response_model=a.ProposalOut, summary="取消本人尚未提交的方案")
def cancel_action(proposal_id: str, user=Depends(current_user), db=Depends(get_db)):
    begin_write(db)
    proposal = db.get(AgentProposal, proposal_id)
    if not proposal:
        raise HTTPException(404, "操作方案不存在")
    owned(db, proposal.conversation_id, user)
    if proposal.status == "PENDING":
        proposal.status = "CANCELED"
        emit(db, proposal.conversation_id, "action.updated", {"proposal": clean(proposal_dump(db, proposal))})
    elif proposal.status == "SUBMITTED":
        raise DomainError("方案已提交，不能通过取消卡片撤销业务操作")
    result = clean(proposal_dump(db, proposal))
    db.commit()
    return result


def event_page(conversation_id, owner_id, cursor, include_actions):
    with SessionLocal() as db:
        user = actor(db, owner_id)
        owned(db, conversation_id, user)
        events = [{"seq": row.seq, "kind": row.kind, "data": row.data} for row in db.scalars(select(AgentEvent)
            .where(AgentEvent.conversation_id == conversation_id, AgentEvent.seq > cursor).order_by(AgentEvent.seq).limit(100))]
        actions = [clean(proposal_dump(db, row)) for row in db.scalars(select(AgentProposal)
            .where(AgentProposal.conversation_id == conversation_id).order_by(AgentProposal.created_at.desc()).limit(200))] if include_actions else None
        return events, actions


@router.get("/conversations/{conversation_id}/events", summary="同源 SSE；Last-Event-ID 或 after 游标支持断线续传")
async def conversation_events(conversation_id: str, request: Request, after: int = Query(0, ge=0),
                              user=Depends(current_user), db=Depends(get_db)):
    try:
        after = max(after, int(request.headers.get("last-event-id", "0")))
    except ValueError:
        raise HTTPException(422, "事件游标必须是整数")
    owner_id = user.id
    db.rollback()  # 认证依赖的只读快照不得随着 SSE 保留十五分钟。
    await run_in_threadpool(event_page, conversation_id, owner_id, after, False)

    async def stream():
        cursor, started, heartbeat, last_actions = after, time.monotonic(), 0.0, ""
        yield "retry: 1500\n\n"
        while time.monotonic() - started < 900:
            if await request.is_disconnected():
                break
            try:
                events, actions = await run_in_threadpool(event_page, conversation_id, owner_id, cursor, True)
            except HTTPException:
                yield 'event: session-ended\ndata: {}\n\n'
                break
            for event in events:
                cursor = event["seq"]
                yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            # 任务由独立处理器执行；观察其真实状态不需要 Agent 继续占用一个模型轮次。
            encoded = json.dumps(actions, ensure_ascii=False)
            if encoded != last_actions:
                yield f"event: actions\ndata: {encoded}\n\n"
                last_actions = encoded
            if time.monotonic() - heartbeat > 15:
                yield ": heartbeat\n\n"
                heartbeat = time.monotonic()
            await asyncio.sleep(1)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


# MCP 只在内部路径运行，不暴露浏览器确认接口。每个请求从独立运行令牌恢复身份。
mcp_server = Server("youju-business")


@mcp_server.list_tools()
async def mcp_list_tools():
    handle = runtime_context.get()
    role = await run_in_threadpool(handle.valid)
    handle.discovered.set()
    tools = [Tool(name="query", description="查询工作台真实数据、单据证据与任务状态；业务事实必须先查此工具。",
                  inputSchema=a.QueryInput.model_json_schema(), annotations=ToolAnnotations(readOnlyHint=True)),
             Tool(name="download", description="生成鉴权原件、归档 ZIP 或完整主体台账的下载入口，不自动下载。",
                  inputSchema=a.DownloadInput.model_json_schema(), annotations=ToolAnnotations(readOnlyHint=True)),
             Tool(name="read_attachment", description="分页读取本私人会话已发送附件的原文及页码、段落或单元格位置；不可按文件名猜测内容。",
                  inputSchema=a.ReadAttachmentInput.model_json_schema(), annotations=ToolAnnotations(readOnlyHint=True)),
             Tool(name="check_invoice_usage", description="根据已上传原件的可信身份查询全企业发票占用及历史记录；不受当前公司筛选限制。无法识别时不得声称未使用。",
                  inputSchema=a.CheckInvoiceUsageInput.model_json_schema(), annotations=ToolAnnotations(readOnlyHint=True))]
    if role in {"admin", "finance"}:
        tools.append(Tool(name="propose", description="提出单项业务方案。不会执行；必须等用户点击网页确认卡。每轮最多二十项。",
            inputSchema=a.ProposalInput.model_json_schema(), annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False)))
    return tools


def perform_tool(handle, name, arguments):
    handle.valid()
    with handle.lock:
        handle.tool_count += 1
        if handle.tool_count > 40:
            raise DomainError("本轮工具调用过多，请缩小范围或分批处理")
    schema = {"query": a.QueryInput, "propose": a.ProposalInput, "download": a.DownloadInput,
              "read_attachment": a.ReadAttachmentInput, "check_invoice_usage": a.CheckInvoiceUsageInput}.get(name)
    if not schema:
        raise DomainError("未开放的业务工具")
    data = schema.model_validate(arguments)
    with SessionLocal() as db:
        begin_write(db)
        actor(db, handle.owner_id, write=name == "propose")
        row = add_message(db, handle.conversation_id, handle.run_id, "tool",
            {"query": "查询业务数据", "propose": "准备操作方案", "download": "准备下载入口",
             "read_attachment": "读取会话附件", "check_invoice_usage": "查询发票使用记录"}[name],
            {"name": name, "input": handle.public(arguments), "status": "RUNNING"})
        message_id = row.id
        db.commit()
    handle.stage(row.content)
    error = ""
    try:
        with SessionLocal() as db:
            user = actor(db, handle.owner_id, write=name == "propose")
            if name == "query":
                result = read_business(db, data, handle.context)
            elif name == "download":
                result = download_links(db, data, handle.context)
            elif name == "read_attachment":
                result = read_attachment(db, data, handle, user)
            elif name == "check_invoice_usage":
                result = check_invoice_usage(db, data, handle, user)
            else:
                result = create_proposal(db, data, handle)
        result = handle.public(result)
        if len(json.dumps(result, ensure_ascii=False)) > 100000:
            raise DomainError("查询结果过长，请缩小范围或查看单条记录")
    except Exception as exc:
        error = str(exc) if isinstance(exc, DomainError) else str(exc.detail) if isinstance(exc, HTTPException) else "业务查询暂时失败，请稍后重试或打开原页面核对"
        result = {"error": error}
    with SessionLocal() as db:
        begin_write(db)
        row = db.get(AgentMessage, message_id)
        row.data = {**row.data, "status": "FAILED" if error else "COMPLETED", "result": result}
        emit(db, handle.conversation_id, "message.updated", {"message": message_dump(row)})
        db.commit()
    return result, bool(error)


@mcp_server.call_tool(validate_input=False)
async def mcp_call_tool(name: str, arguments: dict):
    handle = runtime_context.get()
    try:
        result, failed = await run_in_threadpool(perform_tool, handle, name, arguments)
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                              structuredContent=result, isError=failed)
    except (DomainError, HTTPException, ValidationError) as exc:
        message = str(exc) if isinstance(exc, DomainError) else str(exc.detail) if isinstance(exc, HTTPException) else "工具参数不完整或格式错误，请补全所需业务字段"
        return CallToolResult(content=[TextContent(type="text", text=message)], isError=True)


mcp_sessions = StreamableHTTPSessionManager(mcp_server, stateless=True, json_response=True,
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=["127.0.0.1:*", "localhost:*", "[::1]:*"]), max_request_body_size=1024*1024)


def internal_handle(request: Request):
    authorization = request.headers.get("authorization", "")
    handle = manager.lookup(authorization[7:]) if authorization.startswith("Bearer ") else None
    if not handle or handle.stopped.is_set():
        raise HTTPException(401, "内部运行凭据无效或已过期")
    return handle


class MCPApplication:
    async def __call__(self, scope, receive, send):
        try:
            handle = internal_handle(Request(scope))
        except HTTPException:
            return await JSONResponse({"detail": "内部运行凭据无效或已过期"}, status_code=401)(scope, receive, send)
        token = runtime_context.set(handle)
        try:
            await mcp_sessions.handle_request(scope, receive, send)
        finally:
            runtime_context.reset(token)


mcp_route = Route("/_internal/agent/mcp", MCPApplication(), methods=["GET", "POST", "DELETE"])


@internal.post("/_internal/agent/model/chat/completions")
async def model_bridge(request: Request):
    handle = internal_handle(request)
    await run_in_threadpool(handle.valid)
    parts, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > 4*1024*1024:
            handle.error = "会话上下文过长，请新建对话继续"
            raise HTTPException(413, handle.error)
        parts.append(chunk)
    try:
        payload = json.loads(b"".join(parts))
    except ValueError:
        raise HTTPException(422, "模型请求格式错误")
    allowed = {"mcp__business__query", "mcp__business__download", "mcp__business__propose",
               "mcp__business__read_attachment", "mcp__business__check_invoice_usage"}
    names = {tool.get("function", {}).get("name") for tool in payload.get("tools", [])}
    if not names or names - allowed:
        handle.error = "运行时业务工具未正确加载，已停止本轮"
        raise HTTPException(409, handle.error)
    payload["model"], payload["stream"] = handle.config.model_name, True
    client = httpx.AsyncClient(timeout=httpx.Timeout(settings().agent_turn_seconds, connect=15), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("POST", handle.config.base_url.rstrip("/")+"/chat/completions",
            json=payload, headers={"Authorization": "Bearer "+handle.config.api_key, "Accept": "text/event-stream"}), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        handle.error = "模型服务连接失败，请检查连接配置后重试"
        raise HTTPException(502, handle.error)
    if upstream.status_code >= 300 or "text/event-stream" not in upstream.headers.get("content-type", ""):
        # 只用于判断工具不受支持，不回显供应商正文、URL 或请求头。
        sample = b""
        async for part in upstream.aiter_bytes():
            sample += part
            if len(sample) > 16000:
                break
        detail = sample.decode(errors="ignore").lower()
        handle.error = "当前模型不支持工具调用，请管理员更换支持 tools 的模型" if any(word in detail for word in
            ("tool", "function_call", "function calling")) else "模型服务拒绝请求或未返回流式回复，请检查连接和上下文长度"
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(502, handle.error)
    await run_in_threadpool(handle.begin_step)

    async def stream():
        data_lines = []
        checked_at = time.monotonic()
        try:
            async for line in upstream.aiter_lines():
                if handle.stopped.is_set():
                    break
                if time.monotonic() - checked_at > 2:
                    try:
                        await run_in_threadpool(handle.valid)
                    except HTTPException:
                        break
                    checked_at = time.monotonic()
                if line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
                elif not line and data_lines:
                    raw = "\n".join(data_lines)
                    data_lines.clear()
                    if raw != "[DONE]":
                        try:
                            item = json.loads(raw)
                            choices = item.get("choices") or []
                            text = choices[0].get("delta", {}).get("content") if choices else None
                            if isinstance(text, str) and text:
                                await run_in_threadpool(handle.append_text, text)
                        except (ValueError, KeyError, IndexError):
                            pass
                # 原样转发 SSE 的字段与空行；只旁路提取 text，不把思考或工具参数当正文。
                yield (line+"\n").encode("utf-8")
        except httpx.HTTPError:
            handle.error = "模型流式连接中断，已保留收到的内容，请重试"
        finally:
            await upstream.aclose()
            await client.aclose()
            await run_in_threadpool(handle.flush_text)

    return StreamingResponse(stream(), media_type="text/event-stream")
