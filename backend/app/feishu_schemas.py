"""飞书接入页面契约；密钥只能输入，读取仅返回配置标记。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class FeishuInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=0, description="读取时的配置版本，用于并发保存校验")
    app_id: str = Field(min_length=5, max_length=100, pattern=r"^cli_[A-Za-z0-9]+$", description="企业自建应用 App ID；绑定后不能更换")
    app_secret: SecretStr = Field(default=SecretStr(""), max_length=4096, description="App Secret；留空保持已有密钥")
    public_url: str = Field(min_length=1, max_length=2048,
        description="工作台访问根地址；正式环境要求 HTTPS，测试部署可显式允许与部署配置同源的 HTTP 地址")
    event_transport: Literal["websocket", "http"] = Field("websocket", description="长连接或 HTTP 事件接收")
    encrypt_key: SecretStr = Field(default=SecretStr(""), max_length=512, description="HTTP Encrypt Key；留空保留")
    verification_token: SecretStr = Field(default=SecretStr(""), max_length=512, description="HTTP Verification Token；留空保留")


class FeishuOut(BaseModel):
    version: int = Field(description="配置版本")
    connection_version: int = Field(description="事件服务应应用的连接版本")
    status: str = Field(description="UNCONFIGURED / ACTIVE")
    configured: bool = Field(description="已完成企业绑定且凭据可以解密，不代表所有审批权限已验证")
    source: str = Field(description="database / imported")
    app_id: str = Field(description="应用标识")
    tenant_key: str = Field(description="由飞书登录确认的企业标识，仅展示")
    public_url: str = Field(description="工作台访问地址")
    event_transport: str = Field(description="事件接收方式")
    app_secret_configured: bool = Field(description="App Secret 是否已设置")
    encrypt_key_configured: bool = Field(description="Encrypt Key 是否已设置")
    verification_token_configured: bool = Field(description="Verification Token 是否已设置")
    credential_error: str = Field(description="脱敏的密钥解密错误")
    credential_verified_at: datetime | None = Field(description="最近成功验证应用凭据时间")
    oauth_callback_url: str = Field(description="需加入飞书后台的登录回调地址")
    event_callback_url: str = Field(description="HTTP 模式的事件回调地址")
    receiver_status: str = Field(description="事件服务状态，RUNNING 仅表示进程运行，不等同已成功连接")
    receiver_applied_version: int = Field(description="事件服务已应用的连接版本")
    receiver_heartbeat_at: datetime | None = Field(description="事件监督进程最近心跳")
    receiver_error: str = Field(description="脱敏的接收服务错误")
    last_event_at: datetime | None = Field(description="最近一条已验证并持久化的审批事件时间")


class FeishuTestOut(BaseModel):
    credential_valid: bool = Field(description="仅验证应用凭据，不验证模板权限或实际回写")
    message: str = Field(description="可展示的结果说明")
    checked_at: datetime = Field(description="本次验证时间")


class SetupUnlockInput(BaseModel):
    code: SecretStr = Field(min_length=1, max_length=4096, description="安装初始化码或短时恢复码")


class SetupStatus(BaseModel):
    mode: str = Field(description="业务模式")
    setup_required: bool = Field(description="是否尚未完成企业绑定")
    unlocked: bool = Field(description="当前浏览器是否具有受限配置会话")
    purpose: str = Field("", description="INIT / RECOVERY；无授权时为空")
    expires_at: datetime | None = Field(None, description="当前受限会话失效时间")


class SetupDraftOut(BaseModel):
    login_url: str = Field(description="继续飞书绑定的同源入口")
    message: str = Field(description="下一步操作说明")
