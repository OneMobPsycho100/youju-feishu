"""飞书与可选模型适配层：业务规则不依赖供应商响应形状。"""
import base64
import hashlib
import hmac
import json
import time
from functools import lru_cache
from typing import Protocol
from urllib.parse import urlparse

import httpx
import lark_oapi as lark
from requests.exceptions import RequestException
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from lark_oapi.api.approval.v4 import (
    ApproveTaskRequest, GetApprovalRequest, GetInstanceRequest, ListInstanceRequest,
    RejectTaskRequest, SubscribeApprovalRequest, TaskApprove,
)
from sqlalchemy.orm import Session
from .archive_department import ArchiveDepartment
from .config import settings
from .db import begin_write
from .models import Approval
from .storage import resolve
from .errors import DomainError
from .feishu_config import load_feishu_config


class Gateway(Protocol):
    """审批供应商契约，演示与真实模式使用相同的业务处理器。"""
    def get_instance(self, instance_code: str) -> dict: ...
    def download(self, descriptor: dict) -> bytes: ...
    def operate(self, template_code: str, instance_code: str, task_id: str,
                reviewer: str, action: str, comment: str) -> None: ...


@lru_cache(maxsize=4)
def _versioned_sdk_client(version: int, app_id: str, app_secret: str):
    return (lark.Client.builder().app_id(app_id).app_secret(app_secret)
            .log_level(lark.LogLevel.ERROR).timeout(20).build())


def sdk_client():
    """每次取当前连接版本，避免页面保存后仍复用旧进程的应用凭据。"""
    cfg = load_feishu_config()
    if not cfg.configured:
        raise DomainError(cfg.credential_error or "请先在页面完成飞书接入")
    return _versioned_sdk_client(cfg.connection_version, cfg.app_id, cfg.app_secret)


def sdk_result(response) -> dict:
    """统一转换 SDK 结果；订阅、同意等成功响应可能只有 code/msg，没有 data 属性。"""
    if not response.success():
        # 飞书响应原文可能含字段值或凭据，界面仅展示定位需要的错误码。
        raise DomainError(f"飞书调用失败，错误码 {response.code}，请核对应用权限及模板可见范围")
    data = getattr(response, "data", None)
    return json.loads(lark.JSON.marshal(data)) if data is not None else {}


class FeishuGateway:
    def get_instance(self, instance_code: str) -> dict:
        request = GetInstanceRequest.builder().instance_id(instance_code).user_id_type("open_id").build()
        return sdk_result(sdk_client().approval.v4.instance.get(request))

    def archive_people(self, ids: list[str], *, strict: bool = False) -> dict[str, str]:
        """按飞书身份批量查询姓名。姓名补齐任务可用 strict 保留权限及网络错误供重试。"""
        from lark_oapi.api.contact.v3 import BatchUserRequest

        result = {}
        for kind in ("open_id", "user_id"):
            members = [item.removeprefix("user_id:") for item in ids
                       if item.startswith("user_id:") == (kind == "user_id")]
            for start in range(0, len(members), 50):
                request = BatchUserRequest.builder().user_id_type(kind).user_ids(members[start:start + 50]).build()
                try:
                    rows = sdk_result(sdk_client().contact.v3.user.batch(request)).get("items") or []
                except (DomainError, httpx.HTTPError, RequestException):
                    if strict:
                        raise
                    break
                for row in rows:
                    identity, name = row.get(kind), row.get("name")
                    if identity and name:
                        key = "user_id:" + identity if kind == "user_id" else identity
                        result[key] = name
        return result

    def archive_department(self, department_id: str) -> ArchiveDepartment:
        """按实例部门 ID 读取组织名称，明确区分未设置、授权失败及可重试的网络故障。"""
        from lark_oapi.api.contact.v3 import GetDepartmentRequest

        department_id = department_id.strip()
        if not department_id:
            return ArchiveDepartment(department_id, status="NOT_SET")
        kind = "open_department_id" if department_id.startswith("od-") else "department_id"
        request = (GetDepartmentRequest.builder().department_id(department_id)
                   .department_id_type(kind).user_id_type("open_id").build())
        try:
            response = sdk_client().contact.v3.department.get(request)
            http_status = getattr(getattr(response, "raw", None), "status_code", 0) or 0
            if http_status == 429 or http_status >= 500:
                return ArchiveDepartment(department_id, error_code=response.code, retryable=True,
                    error="飞书所属部门服务暂时不可用或请求过于频繁，打印资料已保留，任务将自动重试。")
            if not response.success():
                # 供应商 msg 可能包含完整请求内容，仅保存错误码和固定的操作指引。
                if response.code == 40004:
                    return ArchiveDepartment(department_id, error_code=response.code,
                        error="所属部门不在应用的通讯录数据权限范围内（飞书错误码 40004）。请在权限管理 → 数据权限 "
                              "→ 通讯录权限范围中加入该部门，并发布生效后重新补齐打印资料。")
                return ArchiveDepartment(department_id, error_code=response.code,
                    error=f"所属部门同步失败，飞书错误码 {response.code}。请开通 contact:contact.base:readonly "
                          "和 contact:department.base:readonly，确认通讯录数据范围覆盖该部门，并发布生效后重试。")
            department = sdk_result(response).get("department") or {}
            name = str(department.get("name") or "").strip()
            if not name:
                return ArchiveDepartment(department_id,
                    error="飞书未返回所属部门名称，请核对 contact:department.base:readonly、通讯录数据范围及应用发布状态后重试。")
            return ArchiveDepartment(department_id, name=name, status="READY")
        except (httpx.HTTPError, RequestException, json.JSONDecodeError):
            return ArchiveDepartment(department_id, retryable=True,
                error="飞书所属部门查询暂时不可用，打印资料已保留，任务将自动重试。")
        except DomainError as exc:
            return ArchiveDepartment(department_id, error=str(exc))

    def definition(self, code: str) -> dict:
        return sdk_result(sdk_client().approval.v4.approval.get(
            GetApprovalRequest.builder().approval_code(code).locale("zh-CN").user_id_type("open_id").build()))

    def subscribe(self, code: str) -> None:
        response = sdk_client().approval.v4.approval.subscribe(
            SubscribeApprovalRequest.builder().approval_code(code).build())
        if response.code != 1390007:  # 原生审批实际返回 subscription existed，重复订阅视为成功。
            sdk_result(response)

    def list_instances(self, code: str, start_ms: int, end_ms: int) -> list[str]:
        found, page_token = [], ""
        for _ in range(100):
            builder = (ListInstanceRequest.builder().approval_code(code)
                       .start_time(start_ms).end_time(end_ms).page_size(100))
            if page_token:
                builder.page_token(page_token)
            data = sdk_result(sdk_client().approval.v4.instance.list(builder.build()))
            found.extend(data.get("instance_code_list") or [])
            if not data.get("has_more"):
                return found
            page_token = data.get("page_token", "")
        raise DomainError("补偿同步分页过多，请缩小同步窗口")

    def setup_instance_candidates(self, code: str) -> list[str]:
        """首次接入仅取近七天一页候选，不触发全量同步，限制后续详情读取数量。"""
        end_ms = int(time.time() * 1000)
        request = (ListInstanceRequest.builder().approval_code(code).start_time(end_ms - 7 * 86400000)
                   .end_time(end_ms).page_size(5).build())
        data = sdk_result(sdk_client().approval.v4.instance.list(request))
        return list(data.get("instance_code_list") or [])[:3]

    def download(self, descriptor: dict) -> bytes:
        url = descriptor.get("url", "")
        suffixes = [x.strip().lower() for x in settings().feishu_file_host_suffixes.split(",") if x.strip()]
        with httpx.Client(timeout=40, follow_redirects=False) as client:
            for _ in range(5):
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme != "https" or not any(host == x or host.endswith("." + x) for x in suffixes):
                    raise DomainError("附件下载地址未在允许的飞书文件域名范围内")
                with client.stream("GET", url) as response:
                    if response.is_redirect:
                        from urllib.parse import urljoin
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    response.raise_for_status()
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > settings().max_file_mb * 1024 * 1024:
                            raise DomainError("飞书附件超过大小限制")
                        chunks.append(chunk)
                    return b"".join(chunks)
        raise DomainError("附件下载重定向次数超过限制")

    def operate(self, template_code: str, instance_code: str, task_id: str,
                reviewer: str, action: str, comment: str) -> None:
        if action == "APPROVE":
            body = (TaskApprove.builder().approval_code(template_code).instance_code(instance_code)
                    .task_id(task_id).user_id(reviewer).comment(comment).build())
            request = ApproveTaskRequest.builder().user_id_type("open_id").request_body(body).build()
            sdk_result(sdk_client().approval.v4.task.approve(request))
        else:
            body = (TaskApprove.builder().approval_code(template_code).instance_code(instance_code)
                    .task_id(task_id).user_id(reviewer).comment(comment).build())
            request = RejectTaskRequest.builder().user_id_type("open_id").request_body(body).build()
            sdk_result(sdk_client().approval.v4.task.reject(request))


class DemoGateway:
    def __init__(self, db: Session):
        self.db = db

    def get_instance(self, instance_code: str) -> dict:
        approval = self.db.get(Approval, instance_code)
        if not approval or not approval.external_snapshot:
            raise DomainError("演示审批不存在")
        return json.loads(json.dumps(approval.external_snapshot))

    def download(self, descriptor: dict) -> bytes:
        return resolve(descriptor["demo_storage_key"]).read_bytes()

    def operate(self, template_code: str, instance_code: str, task_id: str,
                reviewer: str, action: str, comment: str) -> None:
        begin_write(self.db)
        approval = self.db.get(Approval, instance_code)
        snapshot = self.get_instance(instance_code)
        task = next((x for x in snapshot["task_list"] if x["id"] == task_id), None)
        if not task or task["status"] != "PENDING" or task["open_id"] != reviewer:
            raise DomainError("演示待办已变化，请刷新审批")
        task["status"] = "APPROVED" if action == "APPROVE" else "REJECTED"
        if action == "REJECT":
            snapshot["status"] = "REJECTED"
        approval.external_snapshot = snapshot
        self.db.commit()


def gateway(db: Session) -> Gateway:
    return DemoGateway(db) if settings().app_mode == "demo" else FeishuGateway()


def verify_callback(raw: bytes, headers: dict[str, str]) -> dict:
    """验证签名、时间和租户后解密。持久化成功前不得确认收到业务事件。"""
    cfg = load_feishu_config()
    if not cfg.configured or cfg.event_transport != "http":
        raise DomainError("当前未启用 HTTP 事件接收")
    if len(raw) > 1024 * 1024:
        raise DomainError("回调请求超过大小限制")
    try:
        payload = json.loads(raw)
        encrypted = payload.get("encrypt")
        if encrypted:
            timestamp, nonce = headers.get("x-lark-request-timestamp", ""), headers.get("x-lark-request-nonce", "")
            signature = headers.get("x-lark-signature", "")
            expected = hashlib.sha256((timestamp + nonce + cfg.encrypt_key).encode() + raw).hexdigest()
            decoded = base64.b64decode(encrypted, validate=True)
            key = hashlib.sha256(cfg.encrypt_key.encode()).digest()
            decryptor = Cipher(algorithms.AES(key), modes.CBC(decoded[:16])).decryptor()
            padded = decryptor.update(decoded[16:]) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            payload = json.loads(unpadder.update(padded) + unpadder.finalize())
            # 飞书地址验证可能不携带事件签名；解密后的 VerificationToken 仍须匹配。
            if payload.get("type") != "url_verification":
                if not signature or not hmac.compare_digest(signature, expected) or abs(time.time() - float(timestamp)) > 300:
                    raise DomainError("回调签名无效或已过期")
        elif payload.get("type") != "url_verification":
            raise DomainError("真实业务事件必须启用飞书加密回调")
        token = payload.get("token") or payload.get("header", {}).get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token, cfg.verification_token):
            raise DomainError("回调验证令牌无效")
        if payload.get("type") != "url_verification":
            header, event = payload.get("header", {}), payload.get("event", {})
            if (header.get("app_id") or event.get("app_id")) != cfg.app_id:
                raise DomainError("回调应用身份不匹配")
            if (header.get("tenant_key") or event.get("tenant_key")) != cfg.tenant_key:
                raise DomainError("回调企业身份不匹配")
        return payload
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("无法验证或解密飞书回调") from exc
