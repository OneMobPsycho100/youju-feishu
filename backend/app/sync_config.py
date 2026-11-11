"""自动增量补偿同步配置；API 与调度器共享数据库配置，不使用进程缓存。"""
from datetime import timedelta

from sqlalchemy import select, update

from .config import settings
from .db import begin_write
from .integrations import DomainError
from .models import Job, SyncConfig, now
from .schemas import SyncConfigInput, SyncConfigOut
from .feishu_config import feishu_ready


def load_sync_config(db) -> SyncConfigOut:
    """读取最新配置；未在工作台保存时兼容原有部署环境的同步间隔。"""
    row = db.scalar(select(SyncConfig).where(SyncConfig.id == 1).execution_options(populate_existing=True))
    if row is None:
        raise DomainError("补偿同步配置尚未初始化，请先执行数据库迁移")
    cfg = settings()
    enabled = row.enabled if row.managed else cfg.sync_enabled
    interval = row.interval_seconds if row.managed else cfg.sync_interval_seconds
    next_run_at = None
    if enabled and cfg.app_mode == "live" and feishu_ready(db):
        next_run_at = row.schedule_anchor_at + timedelta(seconds=interval) if row.schedule_anchor_at else now()
    return SyncConfigOut(version=row.version, enabled=enabled, interval_seconds=interval,
        source="database" if row.managed else "environment", next_run_at=next_run_at)


def save_sync_config(db, data: SyncConfigInput) -> tuple[SyncConfigOut, list[str]]:
    """调用方提交事务；配置、计时重置和旧自动任务的跳过原子生效。"""
    begin_write(db)
    current = load_sync_config(db)
    if data.version != current.version:
        raise DomainError("补偿同步配置已被其他管理员修改，请重新加载后保存")
    changed = [name for name in ("enabled", "interval_seconds") if getattr(data, name) != getattr(current, name)]
    if current.source == "environment":
        changed.append("source")
    if not changed:
        return current, []
    row = db.get(SyncConfig, 1)
    row.managed, row.enabled, row.interval_seconds = True, data.enabled, data.interval_seconds
    row.version += 1
    row.schedule_anchor_at = now() if data.enabled else None
    # 不新增任务状态；未开始的旧自动任务以“已跳过”结果结束，手动任务没有 sync: 前缀。
    # 正在执行的任务继续完成；执行器领取旧任务时也会复核开关，覆盖保存与领取的竞争。
    db.execute(update(Job).where(Job.kind == "SYNC", Job.dedupe_key.like("sync:%"), Job.status == "PENDING")
        .values(status="SUCCEEDED", error="", result={"skipped": True, "reason": "自动补偿同步配置已变更，旧调度已跳过"},
            updated_at=now()))
    db.flush()
    return load_sync_config(db), changed
