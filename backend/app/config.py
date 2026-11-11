"""部署配置。模型环境变量仅作为工作台首次保存前的回退，不在浏览器保存密钥。"""
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    app_mode: Literal["demo", "live"] = "demo"
    app_env: Literal["production", "development"] = "production"
    app_public_url: str = "http://127.0.0.1:5173"
    allow_insecure_http: bool = Field(False,
        description="仅 development 测试部署允许 APP_PUBLIC_URL 指定的公网 HTTP 根地址；默认关闭，production 禁止启用")
    feishu_event_transport: Literal["http", "websocket"] = "http"
    database_url: str = ""
    storage_root: Path = Path("./data/files")
    config_root: Path | None = None
    session_secret: str = "demo-only-change-this-before-live-deployment"
    session_hours: int = 8
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_encrypt_key: str = ""
    feishu_verification_token: str = ""
    feishu_tenant_key: str = ""
    feishu_admin_open_ids: str = ""
    feishu_file_host_suffixes: str = "feishu.cn,feishucdn.com,feishucdn.cn,byteimg.com,volces.com,larksuite.com,feishu-3rd-party-domain.com"
    model_base_url: str = ""
    model_api_key: str = ""
    model_name: str = ""
    max_file_mb: int = Field(30, ge=1, le=100)
    sync_enabled: bool = Field(True, description="工作台首次保存前是否启用自动增量补偿同步")
    sync_interval_seconds: int = Field(300, ge=1, le=86400,
        description="自动补偿同步的初始间隔，单位秒；统计元数据补齐继续使用此部署间隔")
    agent_internal_url: str = "http://127.0.0.1:8000"
    agent_storage_root: Path | None = None
    agent_max_concurrency: int = Field(2, ge=1, le=4)
    agent_turn_seconds: int = Field(180, ge=30, le=600)
    print_cjk_font: Path | None = Field(None, description="打印用可嵌入的中文 TrueType 字体；为空时使用镜像或系统字体")
    print_office_python: Path | None = Field(None, description="带 UNO 和 seccomp 的本地 LibreOffice 转换 Python")
    print_ofd_classpath: str = Field("", description="内置 OFDRW 转换器的 Java 类路径")

    @model_validator(mode="after")
    def database_defaults(self):
        """本地数据库按业务模式隔离；容器通过 DATABASE_URL 指向独立持久化卷。"""
        if self.allow_insecure_http and self.app_env != "development":
            raise RuntimeError("ALLOW_INSECURE_HTTP 仅可在 APP_ENV=development 的测试部署启用")
        if self.allow_insecure_http and not self.allows_development_http(self.app_public_url):
            raise RuntimeError("启用测试 HTTP 时 APP_PUBLIC_URL 必须是合法的 HTTP 根地址")
        if not self.database_url:
            self.database_url = f"sqlite:///./data/{self.app_mode}/app.db"
        if "storage_root" not in self.model_fields_set:
            self.storage_root = Path(f"./data/{self.app_mode}/files")
        url = make_url(self.database_url)
        if url.drivername != "sqlite" or url.database in {None, "", ":memory:"} or url.query.get("mode") == "memory":
            raise RuntimeError("DATABASE_URL 必须指向本地持久化 SQLite 文件")
        if self.agent_storage_root is None:
            self.agent_storage_root = Path(url.database).resolve().parent / "agent"
        if self.config_root is None:
            self.config_root = Path(url.database).resolve().parent / "config"
        if self.app_mode == "live":
            from .instance_keys import instance_secret
            self.session_secret = instance_secret(self.config_root, self.session_secret)
        return self

    @property
    def loopback_development(self) -> bool:
        """是否使用本机回环 HTTP 开发入口。"""
        url = urlparse(self.app_public_url)
        return (self.app_env == "development" and url.scheme == "http"
                and url.hostname in {"127.0.0.1", "localhost", "::1"}
                and not url.username and not url.password and url.path in {"", "/"}
                and not url.query and not url.fragment)

    def allows_development_http(self, value: str) -> bool:
        """只允许回环开发地址，或显式启用且与部署根地址完全同源的测试 HTTP 地址。

        地址校验和 Cookie 属性共用此判定，避免地址已放开但 Secure Cookie 导致无法登录。
        公网例外绑定 APP_PUBLIC_URL 的主机和端口，不允许管理员在网页任意扩大 HTTP 范围。
        """
        if self.app_env != "development" or any(ord(char) < 32 for char in value):
            return False
        try:
            parsed, configured = urlparse(value.strip()), urlparse(self.app_public_url.strip())
            if parsed.port is not None and parsed.port < 1:
                return False
        except ValueError:
            return False
        if (parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
            return False
        return (parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                or (self.allow_insecure_http and configured.scheme == "http"
                    and parsed.netloc.lower() == configured.netloc.lower()))

    @property
    def secure_cookies(self) -> bool:
        """允许的测试 HTTP 入口使用普通 Cookie，生产与 HTTPS 入口使用 Secure Cookie。"""
        return self.app_mode == "live" and not self.allows_development_http(self.app_public_url)

    def validate_live(self) -> None:
        if self.app_mode != "live":
            return
        # 飞书未绑定也必须能进入网页向导；应用及回调地址在数据库配置激活时校验。
        if len(self.session_secret) < 32 or self.session_secret.startswith("demo-"):
            raise RuntimeError("真实模式 SESSION_SECRET 必须使用至少 32 位随机密钥")


@lru_cache
def settings() -> Settings:
    return Settings()
