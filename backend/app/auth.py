"""飞书 OAuth 登录和工作台授权。Cookie 只承载签名身份，不保存飞书访问凭据。"""
import secrets
from urllib.parse import urlencode
import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from .config import settings
from .db import begin_write, get_db
from .models import User
from .schemas import UserOut
from .feishu_config import cookie_secure, load_feishu_config
from .setup_access import SETUP_COOKIE, activate_setup, read_setup_draft

router = APIRouter(prefix="/api/auth", tags=["登录与授权"])


def signer(salt="session"):
    return URLSafeTimedSerializer(settings().session_secret, salt=salt)


def cookie_name():
    return f"finance_session_{settings().app_mode}"


def set_session(response, user: User, public_url: str = ""):
    response.set_cookie(cookie_name(), signer().dumps({"id": user.id, "mode": settings().app_mode}),
                        max_age=settings().session_hours * 3600, httponly=True, samesite="lax",
                        secure=cookie_secure(public_url), path="/")


def current_user(request: Request, db=Depends(get_db)) -> User:
    try:
        token = signer().loads(request.cookies.get(cookie_name(), ""), max_age=settings().session_hours * 3600)
        if token.get("mode") != settings().app_mode:
            raise BadSignature("mode")
        user = db.get(User, token["id"])
        if not user or not user.enabled:
            raise BadSignature("disabled")
        return user
    except (BadSignature, SignatureExpired, KeyError):
        raise HTTPException(401, "请先登录财务工作台")


def require_role(*roles):
    def dependency(user=Depends(current_user)):
        if user.role not in roles:
            raise HTTPException(403, "当前账号没有执行此操作的权限")
        return user
    return dependency


finance_user = require_role("admin", "finance")
admin_user = require_role("admin")


@router.get("/me", response_model=UserOut, summary="获取当前工作台用户")
def me(user=Depends(current_user)):
    return user


@router.get("/session", response_model=UserOut | None, summary="探测可选会话，未登录返回空值")
def optional_session(request: Request, db=Depends(get_db)):
    try:
        return current_user(request, db)
    except HTTPException:
        return None


@router.post("/demo", response_model=UserOut, summary="登录独立演示环境")
def demo_login(response: Response, role: str = "admin", db=Depends(get_db)):
    if settings().app_mode != "demo":
        raise HTTPException(404)
    if role not in {"admin", "finance", "viewer"}:
        raise HTTPException(400, "演示角色无效")
    user = db.get(User, f"demo-{role}")
    set_session(response, user)
    return user


@router.post("/logout", summary="退出工作台")
def logout(response: Response):
    response.delete_cookie(cookie_name(), path="/")
    return {"message": "已退出登录"}


@router.get("/feishu", summary="开始飞书 OAuth 登录或首次绑定")
def oauth_start(request: Request, purpose: str = "", db=Depends(get_db)):
    if settings().app_mode != "live":
        raise HTTPException(400, "演示模式请使用演示账号")
    if purpose == "setup":
        cfg, draft_version, _, access = read_setup_draft(request, db)
        payload = {"kind": "setup", "draft_version": draft_version, "generation": access["generation"]}
    else:
        cfg = load_feishu_config(db)
        if not cfg.configured:
            raise HTTPException(409, "飞书登录当前不可用，请先完成初始化或使用接入恢复")
        payload = {"kind": "login", "connection_version": cfg.connection_version}
    payload["nonce"] = secrets.token_urlsafe(24)
    state = signer("oauth-state").dumps(payload)
    query = urlencode({"app_id": cfg.app_id, "redirect_uri": cfg.public_url + "/api/auth/callback", "state": state})
    response = RedirectResponse("https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + query)
    response.set_cookie("finance_oauth_state", state, max_age=600, httponly=True,
                        secure=cookie_secure(cfg.public_url), samesite="lax", path="/api/auth")
    return response


def exchange_identity(cfg, code: str) -> dict:
    """只在服务端交换用户身份，不返回访问令牌或外部请求原文。"""
    with httpx.Client(timeout=20) as client:
        token_response = client.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token", json={
            "grant_type": "authorization_code", "client_id": cfg.app_id, "client_secret": cfg.app_secret,
            "code": code, "redirect_uri": cfg.public_url + "/api/auth/callback"})
        token_response.raise_for_status()
        access_token = token_response.json().get("access_token")
        if not access_token:
            raise ValueError("missing token")
        response = client.get("https://open.feishu.cn/open-apis/authen/v1/user_info",
                              headers={"Authorization": "Bearer " + access_token})
        response.raise_for_status()
        info = response.json().get("data", {})
    if not info.get("open_id", "").startswith("ou_") or not info.get("tenant_key"):
        raise ValueError("missing identity")
    return info


@router.get("/callback", summary="完成飞书登录或原子完成首次企业绑定")
def oauth_callback(request: Request, code: str = "", state: str = "", db=Depends(get_db)):
    if settings().app_mode != "live" or not secrets.compare_digest(state, request.cookies.get("finance_oauth_state", "")):
        raise HTTPException(400, "登录状态校验失败，请重新发起登录")
    try:
        payload = signer("oauth-state").loads(state, max_age=600)
    except (BadSignature, ValueError):
        raise HTTPException(400, "登录状态已过期，请重新发起登录")
    if payload.get("kind") == "setup":
        cfg, _, _, access = read_setup_draft(request, db, expected_version=payload.get("draft_version"))
        if access["generation"] != payload.get("generation"):
            raise HTTPException(400, "初始化授权已更新，请重新发起绑定")
        try:
            # 用户取消授权时飞书可能只返回 error；仍回到向导保留草稿，不暴露回调参数。
            if not code:
                raise ValueError("authorization cancelled")
            identity = exchange_identity(cfg, code)
            user, public_url = activate_setup(request, db, identity, payload["draft_version"])
        except Exception:
            db.rollback()
            redirect = RedirectResponse(cfg.public_url + "/?setup_error=oauth#/setup", status_code=303)
            redirect.delete_cookie("finance_oauth_state", path="/api/auth")
            return redirect
        redirect = RedirectResponse(public_url + "/#/settings/templates", status_code=303)
        redirect.delete_cookie(SETUP_COOKIE, path="/")
    else:
        cfg = load_feishu_config(db)
        if not cfg.configured or payload.get("connection_version") != cfg.connection_version:
            raise HTTPException(400, "飞书接入配置已变化，请重新登录")
        try:
            if not code:
                raise ValueError("authorization cancelled")
            identity = exchange_identity(cfg, code)
            if identity["tenant_key"] != cfg.tenant_key:
                raise ValueError("tenant mismatch")
        except Exception:
            raise HTTPException(400, "飞书登录失败，请核对企业、应用权限与重定向地址")
        begin_write(db)
        latest = load_feishu_config(db)
        if latest.connection_version != cfg.connection_version:
            raise HTTPException(400, "登录期间接入配置已变化，请重新登录")
        user = db.get(User, identity["open_id"])
        if not user:
            user = User(id=identity["open_id"], name=identity.get("name") or identity["open_id"],
                        role="admin" if identity["open_id"] in cfg.initial_admin_ids else "viewer", enabled=True)
            db.add(user)
        else:
            user.name = identity.get("name") or user.name
        if not user.enabled:
            raise HTTPException(403, "账号已被管理员停用")
        db.commit()
        public_url = cfg.public_url
        redirect = RedirectResponse(public_url + "/", status_code=303)
    redirect.delete_cookie("finance_oauth_state", path="/api/auth")
    set_session(redirect, user, public_url)
    return redirect
