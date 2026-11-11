"""飞书事件监督进程：按数据库连接版本重建 SDK 子进程，不要求管理员重启容器。"""
import json
import logging
import multiprocessing
import signal
import threading
import time

import lark_oapi as lark

from .config import settings
from .db import SessionLocal, begin_write
from .feishu_config import load_feishu_config
from .jobs import enqueue
from .models import EventInbox, FeishuConfig, now

logger = logging.getLogger(__name__)
APPROVAL_EVENTS = {"approval_instance", "approval_task", "approval",
                   "approval.instance.status_changed_v4", "approval.task.status_changed_v4"}


def receive_approval(data: lark.CustomizedEvent) -> None:
    """SDK 已完成连接鉴权；仍核对当前应用与企业，入库提交后才允许 SDK 确认事件。"""
    cfg = load_feishu_config()
    payload = json.loads(lark.JSON.marshal(data))
    header, event = payload.get("header") or {}, payload.get("event") or {}
    event_id = payload.get("uuid") or header.get("event_id")
    kind = event.get("type") or header.get("event_type")
    if kind not in APPROVAL_EVENTS:
        return
    if not cfg.configured or (header.get("app_id") or event.get("app_id")) != cfg.app_id:
        raise ValueError("长连接事件应用身份不匹配")
    if (header.get("tenant_key") or event.get("tenant_key")) != cfg.tenant_key:
        raise ValueError("长连接事件租户身份不匹配")
    if not isinstance(event_id, str) or not 1 <= len(event_id) <= 100:
        raise ValueError("长连接事件缺少合法幂等标识")
    with SessionLocal() as db:
        begin_write(db)
        if not db.get(EventInbox, event_id):
            db.add(EventInbox(id=event_id, event_type=kind, payload=payload))
            enqueue(db, "EVENT", {"event_id": event_id}, f"event:{event_id}")
        db.get(FeishuConfig, 1).last_event_at = now()
        db.commit()
    logger.info("审批事件已持久化 event_id=%s type=%s", event_id, kind)


def receiver_state(version: int, status: str | None = None, error: str | None = None) -> None:
    """只更新运行状态，不增加配置版本；旧子进程不能覆盖新一代连接的状态。"""
    with SessionLocal() as db:
        begin_write(db)
        row = db.get(FeishuConfig, 1)
        if not row or row.connection_version != version:
            db.rollback()
            return
        if status is not None:
            row.receiver_status = status
        if error is not None:
            row.receiver_error = error
        row.receiver_applied_version = version
        row.receiver_heartbeat_at = now()
        db.commit()


def run_sdk(version: int) -> None:
    """SDK 没有公开的停止接口，用独立子进程隔离其事件循环和连接生命周期。"""
    try:
        cfg = load_feishu_config()
        if not cfg.configured or cfg.connection_version != version or cfg.event_transport != "websocket":
            return
        # SDK 异常日志可能含连接 URL；只保留自己生成的脱敏状态，不转发 SDK 原文。
        from lark_oapi.core.log import logger as sdk_logger
        sdk_logger.disabled = True
        builder = lark.EventDispatcherHandler.builder("", "")
        for kind in ("approval_instance", "approval_task", "approval"):
            builder.register_p1_customized_event(kind, receive_approval)
        for kind in ("approval.instance.status_changed_v4", "approval.task.status_changed_v4"):
            builder.register_p2_customized_event(kind, receive_approval)
        client = lark.ws.Client(cfg.app_id, cfg.app_secret, event_handler=builder.build(), log_level=lark.LogLevel.ERROR)
        receiver_state(version, "RUNNING", "")
        client.start()
    except Exception as exc:
        receiver_state(version, "ERROR", "事件接收进程连接失败（" + type(exc).__name__ + "），请核对应用凭据和飞书事件配置")
        raise SystemExit(1)


def stop_child(child) -> None:
    if child is None:
        return
    if child.is_alive():
        child.terminate()
    child.join(timeout=5)
    if child.is_alive():
        child.kill()
        child.join(timeout=2)


def supervise(stopped: threading.Event) -> None:
    """每三秒读取版本；断线重试有退避，配置变化立即打断退避并换用新凭据。"""
    context = multiprocessing.get_context("spawn")
    child, observed, retry_at, failures = None, None, 0.0, 0
    try:
        while not stopped.is_set():
            try:
                cfg = load_feishu_config()
                identity = (cfg.connection_version, cfg.event_transport, cfg.configured)
                if identity != observed:
                    stop_child(child)
                    child, observed, retry_at, failures = None, identity, 0.0, 0
                if not cfg.configured:
                    receiver_state(cfg.connection_version, "ERROR" if cfg.credential_error else "WAITING", cfg.credential_error)
                elif cfg.event_transport == "http":
                    receiver_state(cfg.connection_version, "HTTP", "")
                else:
                    if child is not None and not child.is_alive():
                        child.join()
                        child = None
                        failures += 1
                        retry_at = time.monotonic() + min(5 * 2 ** min(failures - 1, 4), 60)
                        receiver_state(cfg.connection_version, "ERROR", "事件接收进程已退出，将自动重试；可检查飞书事件配置或更新应用密钥")
                    if child is None and time.monotonic() >= retry_at:
                        receiver_state(cfg.connection_version, "STARTING", "")
                        child = context.Process(target=run_sdk, args=(cfg.connection_version,), daemon=True)
                        child.start()
                    receiver_state(cfg.connection_version)
            except Exception as exc:
                logger.error("事件监督进程等待配置或存储 error_type=%s", type(exc).__name__)
            stopped.wait(3)
    finally:
        stop_child(child)
        try:
            cfg = load_feishu_config()
            receiver_state(cfg.connection_version, "STOPPED")
        except Exception:
            pass


def main() -> None:
    settings().validate_live()
    if settings().app_mode != "live":
        raise RuntimeError("演示模式不启动真实飞书事件接收")
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    supervise(stopped)


if __name__ == "__main__":
    main()
