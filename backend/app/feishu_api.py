"""飞书接入维护与首次初始化 HTTP 入口；初始化会话仅授权这些配置接口。"""
from dataclasses import dataclass

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .auth import current_user
from .config import settings
from .errors import DomainError
from .db import get_db
from .feishu_config import cookie_secure, load_feishu_config, prepare_feishu_config, public_config, save_feishu_config, test_credentials
from .feishu_schemas import FeishuInput, FeishuOut, FeishuTestOut, SetupDraftOut, SetupStatus, SetupUnlockInput
from .models import now
from .setup_access import SETUP_COOKIE, read_setup_draft, setup_session, setup_signer, setup_status, stage_setup, unlock_setup

router = APIRouter(tags=["飞书接入"])


@dataclass(frozen=True)
class ConfigAccess:
    actor: str
    purpose: str
    generation: str = ""


def config_access(request: Request, db=Depends(get_db)) -> ConfigAccess:
    if settings().app_mode != "live":
        raise HTTPException(403, "演示环境不保存真实飞书凭据，请使用独立真实接入部署")
    # 恢复页可能仍带有效管理员 Cookie；优先使用短时授权，保证修复成功后消费恢复码。
    access = setup_session(request, db, optional=True)
    if access:
        return ConfigAccess("接入恢复" if access["purpose"] == "RECOVERY" else "首次接入", access["purpose"], access["generation"])
    try:
        user = current_user(request, db)
    except HTTPException:
        user = None
    if user is not None and user.role == "admin":
        return ConfigAccess(user.name, "ADMIN")
    raise HTTPException(403 if user else 401, "仅工作台管理员或持有安装授权的配置者可以操作")


@router.get("/api/setup/status", response_model=SetupStatus, summary="检查是否需要首次绑定及当前配置会话")
def read_setup_status(request: Request, db=Depends(get_db)):
    return setup_status(request, db)


@router.post("/api/setup/unlock", response_model=SetupStatus, summary="验证安装初始化码或短时接入恢复码")
def unlock(request: Request, data: SetupUnlockInput, response: Response, db=Depends(get_db)):
    payload = unlock_setup(db, data.code.get_secret_value())
    response.set_cookie(SETUP_COOKIE, setup_signer().dumps(payload),
                        max_age=600 if payload["purpose"] == "RECOVERY" else 1800,
                        httponly=True, secure=cookie_secure(str(request.base_url)), samesite="lax", path="/")
    return SetupStatus(mode="live", setup_required=payload["purpose"] == "INIT", unlocked=True,
                       purpose=payload["purpose"], expires_at=setup_status_expiry(payload))


def setup_status_expiry(payload):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(payload["expires"], timezone.utc)


@router.post("/api/setup/feishu", response_model=SetupDraftOut, summary="验证初始化草稿并准备飞书身份绑定")
def prepare_setup(request: Request, data: FeishuInput, db=Depends(get_db)):
    stage_setup(request, db, data)
    return SetupDraftOut(login_url="/api/auth/feishu?purpose=setup",
                         message="应用凭据已验证，请在飞书后台保存回调地址后继续登录绑定")


@router.get("/api/v1/feishu-config", response_model=FeishuOut, summary="读取脱敏飞书接入配置及事件服务状态")
def read_config(request: Request, access=Depends(config_access), db=Depends(get_db)):
    result = public_config(db)
    if access.purpose == "INIT":
        try:
            draft, _, _, _ = read_setup_draft(request, db)
            return result.model_copy(update={"app_id": draft.app_id, "public_url": draft.public_url,
                "event_transport": draft.event_transport, "app_secret_configured": bool(draft.app_secret),
                "encrypt_key_configured": bool(draft.encrypt_key), "verification_token_configured": bool(draft.verification_token),
                "oauth_callback_url": draft.public_url + "/api/auth/callback", "event_callback_url": draft.public_url + "/api/feishu/events"})
        except DomainError:
            pass
    return result


@router.post("/api/v1/feishu-config/test", response_model=FeishuTestOut, summary="只验证应用凭据草稿，不保存或订阅")
def test_config(request: Request, data: FeishuInput, access=Depends(config_access), db=Depends(get_db)):
    current = load_feishu_config(db)
    if access.purpose == "INIT":
        try:
            current, _, _, _ = read_setup_draft(request, db)
        except DomainError:
            pass
    candidate = prepare_feishu_config(current, data)
    test_credentials(candidate)
    return FeishuTestOut(credential_valid=True, checked_at=now(),
        message="应用凭据验证成功；审批读取、任务操作及事件订阅仍需在模板接入时分别验证")


@router.put("/api/v1/feishu-config", response_model=FeishuOut, summary="验证并保存飞书配置，后台自动应用连接版本")
def save_config(data: FeishuInput, response: Response, access=Depends(config_access), db=Depends(get_db)):
    if access.purpose == "INIT":
        raise HTTPException(403, "首次配置需通过飞书登录完成企业和管理员绑定")
    result = save_feishu_config(db, data, access.actor,
        recovery_generation=access.generation if access.purpose == "RECOVERY" else "")
    if access.purpose == "RECOVERY":
        response.delete_cookie(SETUP_COOKIE, path="/")
    return result
