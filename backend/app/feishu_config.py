"""单企业飞书配置、独立密钥加密和版本化运行快照。此模块不依赖登录或 SDK 模块。"""
import base64
from dataclasses import dataclass, field, replace
from datetime import timedelta
from urllib.parse import urlparse

import httpx
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import select

from .config import settings
from .db import SessionLocal, begin_write
from .errors import DomainError
from .feishu_schemas import FeishuInput, FeishuOut
from .models import FeishuConfig, SetupAccess, now


def config_cipher(purpose: str = "active") -> Fernet:
    """飞书有效配置与初始化草稿分别派生密钥，不与模型密钥共用用途。"""
    cfg = settings()
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
               info=f"youju:feishu-config:v1:{cfg.app_mode}:{purpose}".encode()).derive(cfg.session_secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def normalize_public_url(value: str) -> str:
    parsed = urlparse(value.strip())
    allowed_http = settings().allows_development_http(value)
    if (not parsed.netloc or (parsed.scheme != "https" and not allowed_http) or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise DomainError("请使用 HTTPS 根地址，或部署管理员已启用的测试 HTTP 地址")
    return value.strip().rstrip("/")


def cookie_secure(public_url: str = "") -> bool:
    """登录、OAuth 状态及安装会话共用同一个部署级 HTTP 例外。"""
    cfg = settings()
    return cfg.app_mode == "live" and not cfg.allows_development_http(public_url or cfg.app_public_url)


@dataclass(frozen=True)
class FeishuSnapshot:
    """每次外部调用使用不可变快照；凭据不参与 repr 或公开序列化。"""
    version: int
    connection_version: int
    status: str
    app_id: str
    tenant_key: str
    public_url: str
    event_transport: str
    app_secret: str = field(repr=False)
    encrypt_key: str = field(repr=False)
    verification_token: str = field(repr=False)
    initial_admin_ids: tuple[str, ...] = ()
    credential_error: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.status == "ACTIVE" and self.app_id and self.app_secret and self.tenant_key and not self.credential_error)


def load_feishu_config(db=None) -> FeishuSnapshot:
    if db is None:
        with SessionLocal() as session:
            return load_feishu_config(session)
    row = db.scalar(select(FeishuConfig).where(FeishuConfig.id == 1).execution_options(populate_existing=True))
    if row is None:
        raise DomainError("飞书配置尚未初始化，请先执行数据库迁移")
    values, error = {}, ""
    for name in ("app_secret", "encrypt_key", "verification_token"):
        encrypted = getattr(row, name + "_encrypted")
        try:
            values[name] = config_cipher().decrypt(encrypted.encode()).decode() if encrypted else ""
        except (InvalidToken, ValueError, UnicodeError):
            values[name], error = "", "飞书密钥无法解密，请恢复原实例密钥或通过接入恢复重新填写密钥"
    return FeishuSnapshot(version=row.version, connection_version=row.connection_version, status=row.status,
        app_id=row.app_id, tenant_key=row.tenant_key, public_url=row.public_url, event_transport=row.event_transport,
        initial_admin_ids=tuple(row.initial_admin_ids or []), credential_error=error, **values)


def feishu_ready(db=None) -> bool:
    return settings().app_mode == "live" and load_feishu_config(db).configured


def effective_public_url() -> str:
    if settings().app_mode == "live":
        return load_feishu_config().public_url or settings().app_public_url
    return settings().app_public_url


def public_config(db) -> FeishuOut:
    current = load_feishu_config(db)
    row = db.get(FeishuConfig, 1)
    status = row.receiver_status
    if current.status != "ACTIVE":
        status = "WAITING"
    elif current.event_transport == "http":
        status = "HTTP"
    elif not row.receiver_heartbeat_at or row.receiver_heartbeat_at < now() - timedelta(seconds=20):
        status = "OFFLINE"
    base = current.public_url.rstrip("/")
    return FeishuOut(version=row.version, connection_version=row.connection_version, status=row.status,
        configured=current.configured, source="imported" if row.imported_from_env else "database",
        app_id=row.app_id, tenant_key=row.tenant_key, public_url=base, event_transport=row.event_transport,
        app_secret_configured=bool(row.app_secret_encrypted), encrypt_key_configured=bool(row.encrypt_key_encrypted),
        verification_token_configured=bool(row.verification_token_encrypted), credential_error=current.credential_error,
        credential_verified_at=row.credential_verified_at, oauth_callback_url=base + "/api/auth/callback" if base else "",
        event_callback_url=base + "/api/feishu/events" if base else "", receiver_status=status,
        receiver_applied_version=row.receiver_applied_version, receiver_heartbeat_at=row.receiver_heartbeat_at,
        receiver_error=row.receiver_error, last_event_at=row.last_event_at)


def ensure_feishu_config(db) -> str:
    """启动时只迁入一次旧配置；未完成绑定则生成安装码，返回值只交付安装控制台。"""
    if settings().app_mode != "live":
        return ""
    begin_write(db)
    row = db.get(FeishuConfig, 1)
    if row is None:
        raise DomainError("飞书配置数据尚未迁移")
    if not row.managed:
        cfg = settings()
        row.app_id, row.tenant_key = cfg.feishu_app_id.strip(), cfg.feishu_tenant_key.strip()
        row.public_url = cfg.app_public_url.rstrip("/")
        row.event_transport = cfg.feishu_event_transport if cfg.feishu_app_id else "websocket"
        for name in ("app_secret", "encrypt_key", "verification_token"):
            secret = getattr(cfg, "feishu_" + name).strip()
            setattr(row, name + "_encrypted", config_cipher().encrypt(secret.encode()).decode() if secret else "")
        row.initial_admin_ids = [value.strip() for value in cfg.feishu_admin_open_ids.split(",") if value.strip()]
        complete = bool(row.app_id and cfg.feishu_app_secret and row.tenant_key and row.initial_admin_ids)
        if row.event_transport == "http":
            complete = complete and bool(cfg.feishu_encrypt_key and cfg.feishu_verification_token)
        if complete:
            normalize_public_url(row.public_url)
            row.status, row.imported_from_env = "ACTIVE", True
            row.version, row.connection_version = 1, 1
        row.managed = True
    db.commit()
    if row.status != "ACTIVE":
        from .setup_access import ensure_initial_code
        return ensure_initial_code(db)
    return ""


def prepare_feishu_config(current: FeishuSnapshot, data: FeishuInput) -> FeishuSnapshot:
    if current.status == "ACTIVE" and data.app_id != current.app_id:
        raise DomainError("此部署已绑定应用，不能更换 App ID；更换企业或应用请使用独立部署")
    # 不完整旧部署仍可能已有用户；已知 App ID 也不能被初始化流程换成其他应用。
    if current.app_id and current.tenant_key and data.app_id != current.app_id:
        raise DomainError("已有企业配置不能改绑其他应用")
    secret = data.app_secret.get_secret_value().strip() or current.app_secret
    encrypt = data.encrypt_key.get_secret_value().strip() or current.encrypt_key
    token = data.verification_token.get_secret_value().strip() or current.verification_token
    if not secret:
        raise DomainError("请填写 App Secret")
    if current.credential_error and not data.app_secret.get_secret_value().strip():
        raise DomainError(current.credential_error)
    if data.event_transport == "http" and not (encrypt and token):
        raise DomainError("HTTP 事件接收需填写 Encrypt Key 和 Verification Token")
    return replace(current, app_id=data.app_id, app_secret=secret, public_url=normalize_public_url(data.public_url),
                   event_transport=data.event_transport, encrypt_key=encrypt, verification_token=token, credential_error="")


def test_credentials(candidate: FeishuSnapshot) -> None:
    """仅获取应用凭证验证身份，不创建审批、订阅模板或宣称已具备审批操作权限。"""
    try:
        with httpx.Client(timeout=15, follow_redirects=False) as client:
            response = client.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                                   json={"app_id": candidate.app_id, "app_secret": candidate.app_secret})
            response.raise_for_status()
            result = response.json()
        if result.get("code") != 0 or not result.get("tenant_access_token"):
            code = result.get("code")
            suffix = f"（错误码 {code}）" if isinstance(code, int) else ""
            raise DomainError("飞书应用凭据验证失败" + suffix + "，请核对 App ID、App Secret 和应用启用状态")
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("暂时无法验证飞书应用，请检查网络后重试") from exc


def write_candidate(row: FeishuConfig, candidate: FeishuSnapshot, *, binding: bool = False) -> list[str]:
    """在已取得写锁的事务中写入已验证快照，不负责提交。"""
    current = {"app_id": row.app_id, "public_url": row.public_url, "event_transport": row.event_transport}
    changed = [name for name, value in current.items() if value != getattr(candidate, name)]
    previous = {}
    for name in ("app_secret", "encrypt_key", "verification_token"):
        encrypted = getattr(row, name + "_encrypted")
        try:
            previous[name] = config_cipher().decrypt(encrypted.encode()).decode() if encrypted else ""
        except (InvalidToken, ValueError, UnicodeError):
            previous[name] = None
        value = getattr(candidate, name)
        if previous[name] != value:
            changed.append(name)
            setattr(row, name + "_encrypted", config_cipher().encrypt(value.encode()).decode() if value else "")
    for name in current:
        setattr(row, name, getattr(candidate, name))
    if binding:
        row.status, row.tenant_key = "ACTIVE", candidate.tenant_key
    if binding or any(name in changed for name in ("app_id", "app_secret", "event_transport", "encrypt_key", "verification_token")):
        row.connection_version += 1
        row.receiver_status, row.receiver_error = "WAITING", ""
    row.version += 1
    row.managed, row.credential_verified_at = True, now()
    return changed


def save_feishu_config(db, data: FeishuInput, actor: str, *, recovery_generation: str = "") -> FeishuOut:
    current = load_feishu_config(db)
    if current.status != "ACTIVE":
        raise DomainError("请先通过初始化向导完成飞书登录绑定")
    if data.version != current.version:
        raise DomainError("飞书配置已被其他管理员更新，请重新加载")
    candidate = prepare_feishu_config(current, data)
    test_credentials(candidate)
    begin_write(db)
    row = db.get(FeishuConfig, 1)
    if row.version != data.version:
        raise DomainError("验证期间飞书配置已变化，请重新加载后保存")
    if recovery_generation:
        access = db.get(SetupAccess, 1)
        if (not access.code_hash or access.purpose != "RECOVERY" or access.generation != recovery_generation
                or not access.code_expires_at or access.code_expires_at <= now()):
            raise DomainError("接入恢复授权已失效，请重新生成恢复码")
        # 恢复成功即消耗本代恢复授权，与配置修改在同一事务内提交。
        access.code_hash, access.generation = "", ""
    changed = write_candidate(row, candidate)
    from .services import log
    log(db, actor, "更新飞书接入配置", row.app_id, {"changed_fields": changed, "version": row.version})
    db.commit()
    return public_config(db)
