"""SQLite 连接和短写事务。外部下载、OCR、模型调用不得持有写事务。"""
import sqlite3
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


cfg = settings()
database_path = make_url(cfg.database_url).database
Path(database_path).resolve().parent.mkdir(parents=True, exist_ok=True)
if sqlite3.sqlite_version_info < (3, 51, 3):
    raise RuntimeError("SQLite 版本须至少为 3.51.3，请更新 Python/SQLite 或使用项目 Docker 镜像")
engine = create_engine(cfg.database_url, pool_pre_ping=True,
                       connect_args={"check_same_thread": False, "timeout": 30})


@event.listens_for(engine, "connect")
def sqlite_pragmas(connection, _record):
    # 接管 BEGIN，避免 Python sqlite3 的旧事务模式让 SAVEPOINT 脱离外层事务。
    connection.isolation_level = None
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")


@event.listens_for(engine, "begin")
def sqlite_begin(connection):
    mode = " IMMEDIATE" if connection.get_execution_options().get("sqlite_write") else ""
    connection.exec_driver_sql("BEGIN" + mode)


SessionLocal = sessionmaker(engine, expire_on_commit=False, autoflush=False)


@event.listens_for(Session, "after_transaction_end")
def reset_write_flag(session, transaction):
    if transaction.parent is None:
        session.info.pop("sqlite_write", None)


def begin_write(session: Session) -> None:
    """结束只读快照，再获取 SQLite 写锁；调用方负责提交或回滚，可在同一写事务内重入。

    必须在修改 ORM 对象前调用。重新读取对象后再校验版本，防止把旧读快照升级为写事务。
    不在此处自动重试整个业务操作，避免重复发送飞书写入或模型请求。
    """
    if session.info.get("sqlite_write"):
        return
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("请在修改业务对象前调用 begin_write")
    session.rollback()
    session.expire_all()
    session.connection(execution_options={"sqlite_write": True})
    session.info["sqlite_write"] = True


def get_db():
    """每个 HTTP 请求使用独立会话；任何异常均回滚未完成的业务事务。"""
    with SessionLocal() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise
