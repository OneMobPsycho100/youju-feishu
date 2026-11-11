"""读取和保存腾讯云 OCR 配置。识别任务使用事务开始时读取的不可变快照。"""
import base64
import json
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import update

from .config import settings
from .db import begin_write
from .errors import DomainError
from .instance_keys import instance_secret
from .models import InvoiceOCRConfig
from .schemas import OCRConfigInput, OCRConfigOut


@dataclass(frozen=True)
class OCRConfigSnapshot:
    version: int
    enabled: bool
    credentials_configured: bool
    credential_error: str = ""
    secret_id: str = field(default="", repr=False)
    secret_key: str = field(default="", repr=False)

    @property
    def recognition_version(self) -> int:
        """停用期间编辑凭据不会改变票面识别策略，也无需重新审核。"""
        return self.version if self.enabled else 0

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.secret_id and self.secret_key)

    def public(self) -> OCRConfigOut:
        """公开白名单独立于内部快照，避免将解密后的字段序列化。"""
        return OCRConfigOut(version=self.version, enabled=self.enabled,
            credentials_configured=self.credentials_configured, credential_error=self.credential_error)


def _cipher() -> Fernet:
    """OCR、飞书和模型使用不同派生用途，演示与真实部署使用不同派生密钥。"""
    cfg = settings()
    # 演示登录使用公开的默认会话密钥，保存 OCR 凭据时仍须使用本实例的随机密钥。
    instance_key = cfg.session_secret if cfg.app_mode == "live" else instance_secret(cfg.config_root, "")
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
        info=f"youju:invoice-ocr-config:v1:{cfg.app_mode}".encode()).derive(instance_key.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def _config_row(db) -> InvoiceOCRConfig:
    row = db.get(InvoiceOCRConfig, 1, populate_existing=True)
    if row is None:
        raise DomainError("腾讯云 OCR 配置尚未初始化，请先执行数据库迁移")
    return row


def _snapshot(row: InvoiceOCRConfig) -> OCRConfigSnapshot:
    secret_id, secret_key, error = "", "", ""
    if row.credentials_encrypted:
        try:
            pair = json.loads(_cipher().decrypt(row.credentials_encrypted.encode()))
            if not isinstance(pair, list) or len(pair) != 2 or not all(isinstance(value, str) and value for value in pair):
                raise ValueError
            secret_id, secret_key = pair
        except (InvalidToken, ValueError):
            error = "腾讯云凭据无法解密，请恢复原实例密钥或重新填写两项凭据"
        except (OSError, RuntimeError):
            error = "无法读取 OCR 实例密钥，请检查持久化配置卷"
    return OCRConfigSnapshot(row.version, row.enabled, bool(row.credentials_encrypted), error, secret_id, secret_key)


def load_ocr_config(db) -> OCRConfigSnapshot:
    """从调用方事务读取生效配置，支持审核结束时重新读取核对。"""
    return _snapshot(_config_row(db))


def resolve_ocr_credentials(current: OCRConfigSnapshot, version: int, secret_id: str, secret_key: str) -> tuple[str, str]:
    """保存和样票测试共用成对替换规则，留空时使用该版本的已存凭据。"""
    if version != current.version:
        raise DomainError("腾讯云 OCR 配置已被其他管理员更新，请重新加载")
    secret_id, secret_key = secret_id.strip(), secret_key.strip()
    if bool(secret_id) != bool(secret_key):
        raise DomainError("请同时填写 SecretId 和 SecretKey，或将两项都留空")
    return (secret_id, secret_key) if secret_id else (current.secret_id, current.secret_key)


def save_ocr_config(db, data: OCRConfigInput) -> tuple[OCRConfigSnapshot, list[str], bool]:
    """保存配置并返回变更字段和是否需重审。调用方在同一事务内提交配置及重审任务。"""
    begin_write(db)
    row = _config_row(db)
    current = _snapshot(row)
    secret_id, secret_key = resolve_ocr_credentials(current, data.version,
        data.secret_id.get_secret_value(), data.secret_key.get_secret_value())
    replacing = bool(data.secret_id.get_secret_value().strip())
    if data.clear_credentials and replacing:
        raise DomainError("清除凭据时请将两项输入留空")
    enabled = data.enabled and not data.clear_credentials
    if enabled and not secret_id:
        raise DomainError(current.credential_error or "启用腾讯云 OCR 前请填写 SecretId 和 SecretKey")

    changed = ["enabled"] if enabled != current.enabled else []
    encrypted = row.credentials_encrypted
    if data.clear_credentials and encrypted:
        encrypted = ""
        changed.append("credentials")
    elif replacing and (secret_id, secret_key) != (current.secret_id, current.secret_key):
        try:
            encrypted = _cipher().encrypt(json.dumps([secret_id, secret_key]).encode()).decode()
        except (OSError, RuntimeError) as exc:
            raise DomainError("无法保存 OCR 实例密钥，请检查持久化配置卷") from exc
        changed.append("credentials")
    if not changed:
        return current, [], False

    result = db.execute(update(InvoiceOCRConfig).where(InvoiceOCRConfig.id == 1, InvoiceOCRConfig.version == data.version)
        .values(enabled=enabled, credentials_encrypted=encrypted, version=current.version + 1)
        .execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise DomainError("腾讯云 OCR 配置已被其他管理员更新，请重新加载")
    updated = load_ocr_config(db)
    return updated, changed, current.recognition_version != updated.recognition_version
