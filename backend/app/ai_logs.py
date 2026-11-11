"""AI 调用留痕。只提取已知事件字段，绝不持久化完整 SDK 通知、鉴权头或运行时 stderr。"""
import copy
import json
import logging
import re
import time
from uuid import uuid4
from types import SimpleNamespace

from sqlalchemy import update
from sqlalchemy.orm import Session

from .db import SessionLocal, begin_write
from .model_config import ENGINE, SYSTEM_CONSTRAINTS
from .models import AICall, now

logger = logging.getLogger(__name__)
TEXT_LIMIT = 200000  # 超长供应商输出按字符截断并明确标记。
EVENT_LIMIT = 200


class AICallRecorder:
    """内存中归并一次调用的事件，并将可见状态写入共享数据库。"""

    def __init__(self, config, *, source: str, prompt: str, actor="审批 Agent", approval_id=None,
                 template_code="", parent_db=None, constraints=SYSTEM_CONSTRAINTS, agent_conversation_id=None):
        self._secret = config.api_key
        self._parent = parent_db
        self._bind = parent_db.get_bind() if parent_db is not None else SessionLocal.kw["bind"]
        self._started = time.monotonic()
        self._last_flush = self._started
        self._seen = set()
        self._reasoning_parts, self._output_parts = [], []
        call_id = uuid4().hex
        self.values = dict(id=call_id, source=source, approval_id=approval_id, template_code=template_code,
            agent_conversation_id=agent_conversation_id,
            actor=actor, engine=ENGINE, model_name=config.model_name, config_version=config.version,
            session_id="ai-" + call_id, status="RUNNING", stage="准备审核数据", prompt="", input_json="",
            reasoning="", raw_output="", parsed_result={}, usage={}, events=[], finish_reason="",
            error_type="", error_message="", has_reasoning=False, has_output=False, truncated=False,
            started_at=now(), finished_at=None, duration_ms=0)
        self.values["prompt"] = self._text(prompt + "\n\n" + constraints)
        self._event("call/start", "创建 AI 调用记录")
        # 调用开始、进度和结束都在业务写事务之外持久化；业务回滚不会抹去模型输出。
        with Session(bind=self._bind) as db:
            begin_write(db)
            db.add(AICall(**copy.deepcopy(self.values)))
            db.commit()
        self._remember()

    @property
    def id(self):
        return self.values["id"]

    @property
    def session_id(self):
        return self.values["session_id"]

    def _text(self, value: str) -> str:
        text = str(value)
        if self._secret:
            text = text.replace(self._secret, "[已隐藏密钥]")
        text = re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [已隐藏]", text)
        if len(text) > TEXT_LIMIT:
            self.values["truncated"] = True
            text = text[:TEXT_LIMIT]
        return text

    def _event(self, kind, label):
        if len(self.values["events"]) < EVENT_LIMIT:
            self.values["events"].append({"type": kind, "label": label, "time": now().isoformat() + "Z"})
        else:
            self.values["truncated"] = True

    def _remember(self):
        if self._parent is not None:
            self._parent.info.setdefault("ai_call_snapshots", {})[self.id] = copy.deepcopy(self.values)

    def flush(self, *, force=False):
        moment = time.monotonic()
        if not force and moment - self._last_flush < 1:
            return
        self._last_flush = moment
        self.values["duration_ms"] = round((moment - self._started) * 1000)
        self._remember()
        with Session(bind=self._bind) as db:
            begin_write(db)
            db.execute(update(AICall).where(AICall.id == self.id).values(**copy.deepcopy(self.values)))
            db.commit()

    def progress(self, label: str, kind="call/stage", *, force=False):
        self.values["stage"] = label
        self._event(kind, label)
        self.flush(force=force)

    def set_input(self, payload: dict):
        self.values["input_json"] = self._text(json.dumps(payload, ensure_ascii=False, indent=2))

    def set_output(self, output: str):
        self.values["raw_output"] = self._text(output)
        self.values["has_output"] = bool(output)

    def on_notification(self, notification):
        """SDK 只提供已提交的会话事件；思考内容保持供应商原文，不从最终答案推导。"""
        payload = notification.payload
        if notification.method != "session.event" or payload.get("sessionId") != self.session_id:
            return
        event = payload.get("event")
        if not isinstance(event, dict):
            return
        kind, seq = event.get("type"), event.get("seq")
        identity = (kind, seq)
        if identity in self._seen:
            return
        self._seen.add(identity)
        data = event.get("data") or {}
        if not isinstance(data, dict):
            return
        labels = {"turn/start": "审核轮次开始", "step/start": "模型步骤开始", "request/header": "请求已发送至模型",
                  "assistant/message": "已接收模型回复", "assistant/attempt": "已接收未完成的模型回复",
                  "step/end": "模型步骤结束", "turn/end": "审核轮次结束",
                  "tool/call": "模型请求工具调用", "tool/result": "工具调用返回"}
        if kind not in labels:
            return
        self.values["stage"] = labels[kind]
        self._event(kind, labels[kind])
        if kind in {"assistant/message", "assistant/attempt"}:
            message = data.get("message") or data
            content = message.get("content", []) if isinstance(message, dict) else []
            reasoning, output = [], []
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") in {"reasoning", "thinking"}:
                    text = block.get("text") or block.get("thinking")
                    if isinstance(text, str):
                        reasoning.append(text)
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    output.append(block["text"])
            # 中断的 assistant/attempt 可能只有 compact stream，仍保留已提交的内容。
            if not content:
                for part in data.get("stream", []):
                    if isinstance(part, dict) and part.get("type") in {"reasoning-chunks", "text-chunks"}:
                        texts = [text for text in part.get("texts", []) if isinstance(text, str)]
                        (reasoning if part["type"] == "reasoning-chunks" else output).extend(texts)
            if reasoning:
                self._reasoning_parts.append("".join(reasoning))
                self.values["reasoning"] = self._text("\n\n".join(self._reasoning_parts))
                self.values["has_reasoning"] = True
            if output:
                self._output_parts.append("".join(output))
                self.set_output("\n\n".join(self._output_parts))
            usage = data.get("usage")
            if isinstance(usage, dict):
                for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        self.values["usage"][key] = self.values["usage"].get(key, 0) + value
        elif kind == "turn/end":
            reason = data.get("reason")
            if isinstance(reason, dict) and isinstance(reason.get("kind"), str):
                self.values["finish_reason"] = self._text(reason["kind"])[:100]
        self.flush(force=kind == "request/header")

    def finish(self, result: dict, *, error_type=""):
        self.values["status"] = result["status"]
        self.values["stage"] = {"COMPLETED": "结构化输出校验通过", "FAILED": "调用失败", "SKIPPED": "未调用模型"}[result["status"]]
        # 结构化结果仅保存已知字段，日志 ID、密钥或 SDK 内部对象不会递归进入结果。
        self.values["parsed_result"] = {key: [self._text(item) for item in result[key]] if key == "findings"
            else self._text(result[key]) for key in ("status", "risk_level", "summary", "findings") if key in result}
        if isinstance(result.get("fields"), dict):
            self.values["parsed_result"]["fields"] = {key: self._text(result["fields"][key]) for key in
                ("number", "code", "buyer_name", "buyer_tax_id", "seller_name", "issue_date", "amount", "invoice_key", "kind")
                if key in result["fields"]}
        self.values["error_type"] = error_type[:100]
        self.values["error_message"] = self._text(result.get("summary", ""))[:500] if result["status"] != "COMPLETED" else ""
        self.values["finished_at"] = now()
        self._event("call/end", self.values["stage"])
        self.flush(force=True)
    def recover_session(self, home):
        """关闭时 SDK 可能先断开通知，再提交中断事件；清理临时目录前补读本次 JSONL。"""
        try:
            candidates = [path for path in (home / "sessions").rglob("session*.jsonl") if path.parent.name == self.session_id]
            if not candidates:
                return
            # 每次使用新的 home，通常只有一个文件；有版本文件时只读最高版本，避免重复回放。
            path = max(candidates, key=lambda item: int((re.search(r"\.v(\d+)\.", item.name) or ["", "0"])[1]))
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if len(line) > 4 * TEXT_LIMIT:
                        self.values["truncated"] = True
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # 强制终止可能留下尚未写完的末行。
                    self.on_notification(SimpleNamespace(method="session.event", payload={"sessionId": self.session_id, "event": event}))
        except Exception as exc:
            logger.warning("ai_session_recovery_failed error_type=%s", type(exc).__name__)


def flush_call_snapshots(db):
    """业务事务结束后补存内存快照，并结束解析等准备阶段异常中断的调用。"""
    snapshots = db.info.pop("ai_call_snapshots", {})
    if not snapshots:
        return
    # 解析票据等前置步骤异常时，模型尚未真正调用，也必须结束已创建的追踪记录。
    for snapshot in snapshots.values():
        if snapshot["status"] == "RUNNING":
            snapshot.update(status="FAILED", stage="审核准备或调用被中断", finished_at=now(),
                error_type="AuditInterrupted", error_message="审核过程异常中断，请在后台任务查看失败原因")
    try:
        with Session(bind=db.get_bind()) as target:
            begin_write(target)
            for snapshot in snapshots.values():
                target.execute(update(AICall).where(AICall.id == snapshot["id"]).values(**snapshot))
            target.commit()
    except Exception as exc:
        logger.error("ai_log_persist_failed error_type=%s", type(exc).__name__)
