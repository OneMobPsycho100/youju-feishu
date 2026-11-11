"""仅在本机执行一次 OAuth 身份绑定，结果写入已忽略的开发配置；不发放工作台会话。"""
import html
import json
import secrets
import sys
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from dotenv import dotenv_values, set_key

ENV_FILE = Path(__file__).resolve().parents[2] / "data/feishu-development/app.env"
ORIGIN = "http://127.0.0.1:18081"
CALLBACK = ORIGIN + "/api/auth/callback"


def main() -> None:
    """使用浏览器现有飞书登录态获取本应用 open_id 与 tenant_key，最长等待十分钟。"""
    cfg = dotenv_values(ENV_FILE)
    if not cfg.get("FEISHU_APP_ID") or not cfg.get("FEISHU_APP_SECRET"):
        raise RuntimeError("请先将当前飞书应用凭据写入开发配置")
    state = secrets.token_urlsafe(32)
    started = time.monotonic()
    completed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format, *_args):
            """OAuth 查询串包含临时授权码，禁止沿用 HTTPServer 的默认访问日志。"""

        def reply(self, status: int, message: str) -> None:
            body = ("<!doctype html><meta charset=utf-8><title>飞书开发绑定</title>"
                    "<h2>飞书开发绑定</h2><p>" + html.escape(message) + "</p>").encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            nonlocal completed
            if self.headers.get("Host") != "127.0.0.1:18081":
                return self.reply(403, "仅接受本机开发地址")
            url = urlparse(self.path)
            if url.path == "/api/auth/feishu":
                query = urlencode({"app_id": cfg["FEISHU_APP_ID"], "redirect_uri": CALLBACK, "state": state})
                self.send_response(302)
                self.send_header("Location", "https://accounts.feishu.cn/open-apis/authen/v1/authorize?" + query)
                self.send_header("Set-Cookie", f"feishu_dev_binding={state}; HttpOnly; SameSite=Lax; Path=/api/auth; Max-Age=600")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            if url.path != "/api/auth/callback":
                return self.reply(404, "请从 /api/auth/feishu 开始开发身份绑定")
            query = parse_qs(url.query)
            cookies = SimpleCookie(self.headers.get("Cookie", ""))
            cookie = cookies.get("feishu_dev_binding")
            if (time.monotonic() - started > 600 or not cookie
                    or not secrets.compare_digest(cookie.value, state)
                    or not secrets.compare_digest(query.get("state", [""])[0], state)):
                return self.reply(400, "身份绑定状态无效，请重新启动绑定程序")
            try:
                with httpx.Client(timeout=20) as client:
                    response = client.post("https://open.feishu.cn/open-apis/authen/v2/oauth/token", json={
                        "grant_type": "authorization_code", "client_id": cfg["FEISHU_APP_ID"],
                        "client_secret": cfg["FEISHU_APP_SECRET"], "code": query.get("code", [""])[0],
                        "redirect_uri": CALLBACK})
                    response.raise_for_status()
                    token = response.json().get("access_token")
                    if not token:
                        raise ValueError("未获取身份凭据")
                    response = client.get("https://open.feishu.cn/open-apis/authen/v1/user_info",
                                          headers={"Authorization": "Bearer " + token})
                    response.raise_for_status()
                    info = response.json().get("data", {})
                if not info.get("open_id", "").startswith("ou_") or not info.get("tenant_key"):
                    raise ValueError("缺少应用内身份")
                # 只有成功完成带随机 state 的当前浏览器授权后，才写入显式管理员名单。
                values = {"APP_MODE": "live", "APP_ENV": "development", "APP_PUBLIC_URL": ORIGIN,
                          "FEISHU_EVENT_TRANSPORT": "websocket", "SESSION_SECRET": secrets.token_urlsafe(48),
                          "FEISHU_TENANT_KEY": info["tenant_key"], "FEISHU_ADMIN_OPEN_IDS": info["open_id"]}
                for key, value in values.items():
                    set_key(str(ENV_FILE), key, value, quote_mode="never")
                binding = {key: info[key] for key in ("open_id", "tenant_key", "name") if key in info}
                ENV_FILE.with_name("developer-binding.json").write_text(
                    json.dumps(binding, ensure_ascii=False, indent=2), encoding="utf8")
                completed = True
                self.reply(200, "开发身份绑定成功，凭据已保存在本地。可关闭此页面。")
            except Exception as exc:
                print("开发身份绑定未完成，错误类型：" + type(exc).__name__, flush=True)
                self.reply(400, "绑定失败，请核对应用发布状态、登录权限及重定向地址后重新授权")

    with HTTPServer(("127.0.0.1", 18081), Handler) as server:
        server.timeout = 1
        print("开发绑定已就绪：" + ORIGIN + "/api/auth/feishu", flush=True)
        while not completed and time.monotonic() - started < 600:
            server.handle_request()
    if not completed:
        sys.exit("开发绑定超时，未修改管理员配置")
    print("开发身份绑定完成，未输出任何访问令牌。", flush=True)


if __name__ == "__main__":
    main()
