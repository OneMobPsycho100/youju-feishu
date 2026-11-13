"""腾讯云增值税发票 OCR，逐页取证并按附件汇总为一路候选。"""
import asyncio
import logging
import re
import time
import unicodedata
from collections import defaultdict
from threading import Lock
from typing import TYPE_CHECKING

import httpx
from tencentcloud.common.common_client_async import CommonClient
from tencentcloud.common.credential import Credential
from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
from tencentcloud.common.profile.client_profile import ClientProfile
from tencentcloud.common.profile.http_profile import HttpProfile

from .invoice_images import InvoiceImage
from .invoice_recognition import normalized_candidate

if TYPE_CHECKING:
    from .ocr_config import OCRConfigSnapshot

# SDK 的调试日志含有签名头、原件图像及完整供应商响应。
logging.getLogger("tencentcloud_sdk_common").disabled = True
logger = logging.getLogger(__name__)

PAGE_TIMEOUT = 10
ATTACHMENT_TIMEOUT = 60
MAX_BASE64_BYTES = 10_000_000
REQUEST_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
FIELD_NAMES = {
    "发票号码": "number", "发票代码": "code", "购买方名称": "buyer_name", "购买方识别号": "buyer_tax_id",
    "销售方名称": "seller_name", "开票日期": "issue_date", "小写金额": "amount", "价税合计(小写)": "amount",
    "购买方统一社会信用代码/纳税人识别号": "buyer_tax_id", "购买方纳税人识别号": "buyer_tax_id",
    "购买方税号": "buyer_tax_id", "购方识别号": "buyer_tax_id",
}
DISPLAY_FIELDS = {"发票名称", "发票类型", "合计金额", "合计税额"}
TIMEOUT_ERROR = "腾讯云发票 OCR 请求超时，请稍后重试"
RESPONSE_ERROR = "腾讯云发票 OCR 返回格式异常，请稍后重试"
EMPTY_ERROR = "腾讯云未识别到可用的增值税发票字段"
_state_lock = Lock()
_last_call: tuple[int, str, str, int] | None = None


def _safe_request_id(value) -> str:
    return value if isinstance(value, str) and REQUEST_ID.fullmatch(value) else ""


def _service_error(code) -> str:
    """只根据错误码生成公开说明。远端消息可能含有原件或请求内容。"""
    code = code if isinstance(code, str) else ""
    if code.startswith("AuthFailure") or code.startswith("UnauthorizedOperation"):
        return "腾讯云发票 OCR 鉴权失败，请检查凭据和接口权限"
    if code == "FailedOperation.UnOpenError":
        return "腾讯云账号尚未开通发票 OCR 服务"
    if code.startswith("RequestLimitExceeded"):
        return "腾讯云发票 OCR 调用频率超限，请稍后重试"
    if code in {"ResourceUnavailable.InArrears", "ResourceUnavailable.ResourcePackageRunOut"}:
        return "腾讯云发票 OCR 账号欠费或资源包已用完"
    if code == "ServerNetworkError":
        return "腾讯云发票 OCR 服务暂不可用，请稍后重试"
    return "腾讯云发票识别失败，请检查原件或云服务状态"


def _field_box(polygon) -> list[list[float]]:
    if not isinstance(polygon, dict):
        return []
    points = []
    for corner in ("LeftTop", "RightTop", "RightBottom", "LeftBottom"):
        point = polygon.get(corner)
        if not isinstance(point, dict) or not all(type(point.get(axis)) is int for axis in ("X", "Y")):
            return []
        points.append([point["X"], point["Y"]])
    return points


def _read_fields(entries: list, page: dict) -> dict[str, set[str]]:
    fields = defaultdict(set)
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("Name"), str) or not isinstance(item.get("Value"), str):
            continue
        label = re.sub(r"\s+", "", unicodedata.normalize("NFKC", item["Name"]))
        field = FIELD_NAMES.get(label)
        if not field and label not in DISPLAY_FIELDS:
            continue
        value = " ".join(item["Value"].split())
        if not 0 < len(value) <= 300:
            continue
        page["lines"].append({"text": f"{label}: {value}", "box": _field_box(item.get("Polygon"))})
        if field and (candidate := normalized_candidate(field, value)):
            fields[field].add(candidate)
    return dict(fields)


def _tax_items(items: object) -> list[dict[str, str]]:
    """仅保留展示计算需要的结构化金额和税率，保留整组以便与票面合计核对。"""
    if not isinstance(items, list):
        return []
    fields = {"untaxed_amount": "AmountWithoutTax", "tax_amount": "TaxAmount", "tax_rate": "TaxRate"}
    result = []
    for item in items:
        if not isinstance(item, dict):
            return []
        values = {field: item.get(name) for field, name in fields.items()}
        if any(not isinstance(value, str) or len(value) > 64 for value in values.values()):
            return []
        result.append({field: unicodedata.normalize("NFKC", value).strip() for field, value in values.items()})
    return result


async def _read_page(client: CommonClient, image: InvoiceImage, page: dict, timeout: float) -> tuple[dict[str, set[str]], str]:
    try:
        async with asyncio.timeout(timeout):
            data = await client.call_and_deserialize("VatInvoiceOCR", {"ImageBase64": image.data})
        response = data["Response"]
        page["request_id"] = _safe_request_id(response.get("RequestId"))
        # SDK 根据响应 Content-Type 判断业务错误，这里也校验反序列化后的响应。
        if error := response.get("Error"):
            raise TencentCloudSDKException(code=error.get("Code"), requestId=page["request_id"])
        entries = response["VatInvoiceInfos"]
        if entries is not None and not isinstance(entries, list):
            return {}, RESPONSE_ERROR
        page["tax_items"] = _tax_items(response.get("Items"))
        return _read_fields(entries or [], page), ""
    except TencentCloudSDKException as exc:
        page["request_id"] = _safe_request_id(exc.get_request_id())
        code = exc.get_code()
        return {}, "" if code == "FailedOperation.ImageNoText" else _service_error(code)
    except (TimeoutError, httpx.TimeoutException):
        return {}, TIMEOUT_ERROR
    except (httpx.RequestError, OSError):
        return {}, "腾讯云发票 OCR 网络连接失败，请稍后重试"
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}, RESPONSE_ERROR


async def _fetch_pages(images: list[InvoiceImage], secret_id: str, secret_key: str) -> tuple[list[dict], list[dict[str, set[str]]], str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + ATTACHMENT_TIMEOUT
    pages, candidates = [], []
    profile = ClientProfile(httpProfile=HttpProfile(endpoint="ocr.tencentcloudapi.com", reqTimeout=PAGE_TIMEOUT, keepAlive=True))
    async with CommonClient("ocr", "2018-11-19", Credential(secret_id, secret_key), "", profile=profile) as client:
        for number, image in enumerate(images, 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return pages, candidates, TIMEOUT_ERROR
            page = {"page": number, "width": image.width, "height": image.height, "request_id": "", "lines": []}
            fields, error = await _read_page(client, image, page, min(PAGE_TIMEOUT, remaining))
            pages.append(page)
            candidates.append(fields)
            if error:
                return pages, candidates, error
    return pages, candidates, ""


def _merge_pages(pages: list[dict], candidates: list[dict[str, set[str]]]) -> dict:
    """拒绝多票身份；同一字段有多个有效值时保留页面证据，汇总时不计票。"""
    numbers = {number for fields in candidates for number in fields.get("number", ())}
    if len(numbers) > 1:
        return {"status": "FAILED", "fields": {}, "rejected": True, "error": "腾讯云 OCR 发现不同发票号码，请拆分原件"}
    number = next(iter(numbers), "")
    codes = {code for fields in candidates if number in fields.get("number", ()) for code in fields.get("code", ())}
    if len(number) == 8 and len(codes) > 1:
        return {"status": "FAILED", "fields": {}, "rejected": True, "error": "腾讯云 OCR 发现同一传统票号对应不同代码，请拆分原件"}

    populated = [(page, fields) for page, fields in zip(pages, candidates, strict=True) if fields]
    if len(populated) > 1:
        # 多个有字页面只合并身份匹配的候选，防止把未知页的金额或主体拼到另一张票。
        populated = [(page, fields) for page, fields in populated if fields.get("number") == {number}
            and (len(number) == 20 or fields.get("code") == codes)]
    values = defaultdict(set)
    for _, fields in populated:
        for field, choices in fields.items():
            values[field].update(choices)
    agreed = {field: next(iter(choices)) for field, choices in values.items() if len(choices) == 1}
    raw_text = "\n".join(line["text"] for page, _ in populated for line in page["lines"])
    return {"status": "COMPLETED" if agreed else "EMPTY", "fields": agreed, "raw_text": raw_text[:30000],
            "tax_item_groups": [page["tax_items"] for page, _ in populated if page.get("tax_items")],
            "error": "" if agreed else EMPTY_ERROR}


def recognize_invoice_pages(images: list[InvoiceImage], secret_id: str, secret_key: str) -> dict:
    """从同步任务线程调用 SDK，限制单页和附件总请求时间。每页只发一次。"""
    if any(min(image.width, image.height) < 20 or len(image.data) > MAX_BASE64_BYTES for image in images):
        return {"status": "FAILED", "fields": {}, "pages": [], "error": "原件页面大小超出腾讯云发票 OCR 支持范围"}
    started = time.monotonic()
    try:
        pages, candidates, error = asyncio.run(_fetch_pages(images, secret_id, secret_key))
    except Exception as exc:
        # SDK 初始化及关闭也可能失败，异常文本同样不能进入 API 响应或日志。
        logger.warning("tencent_invoice_ocr_failed error_type=%s", type(exc).__name__)
        pages, candidates, error = [], [], "腾讯云发票 OCR 调用异常，请稍后重试"
    result = _merge_pages(pages, candidates)
    if error and not result.get("rejected"):
        # 任一页调用失败时整路不计票，避免把前几页误当成完整附件。
        result = {"status": "FAILED", "fields": {}, "error": error}
    for page in pages:
        page.pop("tax_items", None)
    return {**result, "pages": pages, "elapsed_ms": round((time.monotonic() - started) * 1000)}


def recognize_invoice(images: list[InvoiceImage], config: "OCRConfigSnapshot") -> dict:
    """只记录实际业务调用，样票测试和旧版本的任务不会覆盖当前运行状态。"""
    result = recognize_invoice_pages(images, config.secret_id, config.secret_key)
    if "elapsed_ms" in result:
        global _last_call
        with _state_lock:
            if _last_call is None or config.version >= _last_call[0]:
                state = "READY" if result["status"] == "COMPLETED" else "FAILED"
                _last_call = (config.version, state, result["error"], result["elapsed_ms"])
    return result


def ocr_status(config: "OCRConfigSnapshot") -> dict:
    """读取当前配置版本的最近一次业务调用状态。"""
    state, error, elapsed = "DISABLED", "", None
    if config.enabled:
        state = "UNVERIFIED"
        if not config.configured:
            state, error = "FAILED", config.credential_error or "缺少腾讯云 OCR 凭据"
        else:
            with _state_lock:
                if _last_call is not None and _last_call[0] == config.version:
                    _, state, error, elapsed = _last_call
    return {"ocr_enabled": config.enabled, "ocr_configured": config.configured,
            "ocr_state": state, "ocr_last_error": error, "ocr_last_elapsed_ms": elapsed}
