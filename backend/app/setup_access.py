"""安装初始化码、受限网页配置会话及首次 OAuth 绑定，不提供业务数据访问权限。"""
import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import secrets
import time

from fastapi import HTTPException
from itsdangerous import BadSignature, URLSafeTimedSerializer

from .config import settings
from .db import SessionLocal, begin_write
from .errors import DomainError
from .feishu_config import config_cipher, load_feishu_config, prepare_feishu_config, test_credentials, write_candidate
from .feishu_schemas import FeishuInput, SetupStatus
from .models import FeishuConfig, SetupAccess, User, now

SETUP_COOKIE = "youju_setup_access"


def setup_signer():
    return URLSafeTimedSerializer(settings().session_secret, salt="youju-setup-session-v1")


def code_digest(value: str) -> str:
    return hmac.new(settings().session_secret.encode(), ("setup-code:" + value).encode(), hashlib.sha256).hexdigest()


def issue_access_code(db, *, recovery: bool = False) -> str:
    """仅安装控制台调用；新一代授权会使旧码、旧配置会话和旧 OAuth 草稿全部失效。"""
    if settings().app_mode != "live":
        raise DomainError("演示环境不接收真实飞书配置")
    begin_write(db)
    config = db.get(FeishuConfig, 1)
    access = db.get(SetupAccess, 1)
    if not config or not access:
        raise DomainError("请先执行数据库迁移")
    if recovery != (config.status == "ACTIVE"):
        raise DomainError("已绑定实例请使用 --recovery；未绑定实例请生成初始化码")
    code = secrets.token_urlsafe(32)
    access.code_hash, access.generation = code_digest(code), secrets.token_hex(24)
    access.purpose = "RECOVERY" if recovery else "INIT"
    access.code_expires_at = now() + timedelta(minutes=10) if recovery else None
    access.draft_encrypted, access.draft_owner = "", ""
    access.draft_expires_at = None
    access.draft_version += 1
    db.commit()
    return code


def ensure_initial_code(db) -> str:
    access = db.get(SetupAccess, 1)
    if access and access.code_hash:
        return ""
    return issue_access_code(db)


def setup_session(request, db, *, optional: bool = False) -> dict | None:
    """受限会话不能替代工作台登录；每次都复核授权代次、用途和到期时间。"""
    try:
        if settings().app_mode != "live":
            raise ValueError("mode")
        payload = setup_signer().loads(request.cookies.get(SETUP_COOKIE, ""), max_age=1800)
        access = db.get(SetupAccess, 1, populate_existing=True)
        config = db.get(FeishuConfig, 1, populate_existing=True)
        if (not access or not config or not access.code_hash or payload.get("generation") != access.generation
                or payload.get("purpose") != access.purpose or not isinstance(payload.get("nonce"), str)
                or payload.get("expires", 0) <= time.time()
                or (access.code_expires_at and access.code_expires_at <= now())
                or (access.purpose == "INIT" and config.status == "ACTIVE")
                or (access.purpose == "RECOVERY" and config.status != "ACTIVE")):
            raise ValueError("access")
        return payload
    except (BadSignature, ValueError, TypeError):
        if optional:
            return None
        raise HTTPException(401, "配置授权已失效，请重新输入初始化码或恢复码")


def unlock_setup(db, code: str) -> dict:
    access = db.get(SetupAccess, 1, populate_existing=True)
    config = db.get(FeishuConfig, 1, populate_existing=True)
    if (settings().app_mode != "live" or not access or not config or not access.code_hash
            or (access.code_expires_at and access.code_expires_at <= now())
            or not secrets.compare_digest(access.code_hash, code_digest(code.strip()))
            or (access.purpose == "INIT" and config.status == "ACTIVE")):
        raise HTTPException(401, "初始化码或恢复码不正确、已使用或已过期")
    duration = 600 if access.purpose == "RECOVERY" else 1800
    return {"generation": access.generation, "purpose": access.purpose,
            "nonce": secrets.token_urlsafe(24), "expires": int(time.time()) + duration}


def setup_status(request, db) -> SetupStatus:
    if settings().app_mode != "live":
        return SetupStatus(mode="demo", setup_required=False, unlocked=False)
    config = load_feishu_config(db)
    access = setup_session(request, db, optional=True)
    return SetupStatus(mode="live", setup_required=config.status != "ACTIVE", unlocked=bool(access),
        purpose=access["purpose"] if access else "",
        expires_at=datetime.fromtimestamp(access["expires"], timezone.utc) if access else None)


def stage_setup(request, db, data: FeishuInput) -> None:
    access = setup_session(request, db)
    if access["purpose"] != "INIT":
        raise HTTPException(403, "已绑定实例请在恢复页面保存配置")
    current = load_feishu_config(db)
    if current.version != data.version or current.status == "ACTIVE":
        raise DomainError("初始化配置已变化，请重新加载")
    try:
        previous, _, _, _ = read_setup_draft(request, db)
        current = previous
    except DomainError:
        pass
    candidate = prepare_feishu_config(current, data)
    test_credentials(candidate)
    begin_write(db)
    access = setup_session(request, db)
    if db.get(FeishuConfig, 1).version != data.version:
        raise DomainError("验证期间配置已变化，请重新加载")
    row = db.get(SetupAccess, 1)
    # SecretStr 不能直接序列化成草稿，否则会丢失密钥；仅在加密边界内显式列出敏感字段。
    payload = {name: getattr(candidate, name) for name in (
        "app_id", "app_secret", "public_url", "event_transport", "encrypt_key", "verification_token")}
    payload["base_version"] = current.version
    row.draft_encrypted = config_cipher("setup-draft").encrypt(json.dumps(payload).encode()).decode()
    row.draft_owner = hashlib.sha256(access["nonce"].encode()).hexdigest()
    row.draft_expires_at = now() + timedelta(minutes=30)
    row.draft_version += 1
    db.commit()


def read_setup_draft(request, db, *, expected_version: int | None = None):
    access = setup_session(request, db)
    row = db.get(SetupAccess, 1, populate_existing=True)
    if (access["purpose"] != "INIT" or not row.draft_encrypted or not row.draft_expires_at
            or row.draft_expires_at <= now() or row.draft_owner != hashlib.sha256(access["nonce"].encode()).hexdigest()
            or (expected_version is not None and row.draft_version != expected_version)):
        raise DomainError("首次接入草稿已失效，请重新验证应用并发起飞书绑定")
    try:
        data = json.loads(config_cipher("setup-draft").decrypt(row.draft_encrypted.encode()))
    except Exception as exc:
        raise DomainError("初始化草稿无法读取，请重新填写应用凭据") from exc
    candidate = replace(load_feishu_config(db), **{name: data[name] for name in (
        "app_id", "app_secret", "public_url", "event_transport", "encrypt_key", "verification_token")})
    return candidate, row.draft_version, data["base_version"], access


def activate_setup(request, db, identity: dict, expected_version: int):
    """OAuth 身份可信后原子认领；授权码、草稿和工作台管理员不能分开提交。"""
    begin_write(db)
    candidate, _, base_version, _ = read_setup_draft(request, db, expected_version=expected_version)
    row = db.get(FeishuConfig, 1)
    tenant, open_id = identity.get("tenant_key"), identity.get("open_id", "")
    if (row.status == "ACTIVE" or row.version != base_version or not tenant or not open_id.startswith("ou_")
            or (row.tenant_key and row.tenant_key != tenant)):
        raise DomainError("企业绑定信息不一致或实例已被绑定，请重新加载")
    user = db.get(User, open_id)
    if user is not None and not user.enabled:
        raise DomainError("此成员已被停用，请使用可用成员完成初始化")
    if user is None:
        user = User(id=open_id, name=identity.get("name") or open_id, role="admin", enabled=True)
        db.add(user)
    else:
        user.role, user.name = "admin", identity.get("name") or user.name
    write_candidate(row, replace(candidate, tenant_key=tenant, status="ACTIVE"), binding=True)
    row.initial_admin_ids = []
    access = db.get(SetupAccess, 1)
    access.code_hash, access.generation = "", secrets.token_hex(24)
    access.draft_encrypted, access.draft_owner, access.draft_expires_at = "", "", None
    access.draft_version += 1
    from .services import log
    log(db, user.name, "完成飞书首次绑定", row.app_id, {"version": row.version})
    db.commit()
    return user, row.public_url


def main():
    parser = argparse.ArgumentParser(description="为网页接入生成新的安装码或有效期十分钟的恢复码")
    parser.add_argument("--recovery", action="store_true", help="仅修复已绑定应用的凭据，不创建工作台账号")
    args = parser.parse_args()
    with SessionLocal() as db:
        code = issue_access_code(db, recovery=args.recovery)
    print(("接入恢复码（10 分钟有效）：" if args.recovery else "安装初始化码（首次绑定后失效）：") + code)
    print("请在工作台的初始化/接入恢复页面输入；不要发送到聊天或公开日志。")


if __name__ == "__main__":
    main()
