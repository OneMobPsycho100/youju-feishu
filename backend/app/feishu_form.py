"""从已同步的飞书表单生成只读展示值，保留控件顺序和明细行。"""
import csv
import re

from .payment_service import decoded
from .schemas import FeishuFormField
from .template_transfer import ATTACHMENT_TYPES, DISPLAY_TYPES, FIELD_LABELS, GROUP_TYPES


UNREADABLE = "此字段暂无法展示，请到飞书查看"
CHOICE_TYPES = {"radio", "radiov2", "checkbox", "checkboxv2"}
DISPLAY_KEYS = ("name", "text", "label", "department_name", "value")
ACCOUNT_KEYS = {
    "widgetAccountName": DISPLAY_KEYS,
    "widgetAccountNumber": DISPLAY_KEYS,
    "widgetAccountBankName": ("bankNameZh", *DISPLAY_KEYS, "bankNameEn"),
    "widgetAccountBankBranch": ("name", "bankBranchNameZh", *DISPLAY_KEYS[1:], "bankBranchNameEn"),
    "widgetAccountBankArea": DISPLAY_KEYS,
    "widgetAccountType": ("text", "name", "label", "value"),
}


def _display(value, keys=DISPLAY_KEYS, *, depth=0) -> str:
    """从结构化值中取显示文本，保留零值和提交的长文本。"""
    if depth > 12:
        return ""
    if isinstance(value, str):
        return value if value.strip() else ""
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "、".join(filter(None, (_display(item, keys, depth=depth + 1) for item in value)))
    if isinstance(value, dict):
        for key in keys:
            text = _display(decoded(value.get(key)), keys, depth=depth + 1)
            if text:
                return text
    return ""


def _choice_text(value, option) -> str:
    """实例值可能是选项名称或外部选项标识，使用快照中与选中值匹配的标签。"""
    options = option if isinstance(option, list) else [option]
    labels = {}
    for item in options:
        if isinstance(item, dict):
            text = _display(item, ("text", "label", "name"))
            if text:
                for key in ("key", "id", "value"):
                    if isinstance(item.get(key), (str, int)):
                        labels[str(item[key])] = text
    selections = []
    for item in value if isinstance(value, list) else [value]:
        key = _display(item, ("text", "label", "name", "value", "key", "id"))
        if key:
            selections.append(labels.get(key, key))
    return "、".join(selections)


def attachment_values(value) -> list:
    """兼容附件数组、文件容器和旧版逗号分隔的下载地址。"""
    value = decoded(value)
    if isinstance(value, dict):
        container = value.get("files") or value.get("attachments")
        if isinstance(container, list):
            value = container
    if isinstance(value, list):
        return [item for item in value if item]
    if isinstance(value, str):
        parts = re.split(r",\s*(?=https://)", value) if value.startswith("https://") else value.split(",")
        return [part.strip() for part in parts if part.strip()]
    return [value] if value else []


def attachment_names(ext, count: int) -> list[str]:
    """按顺序返回飞书控件中的附件名称。名称与原件数量不匹配时不做位置猜测。"""
    ext = decoded(ext)
    if isinstance(ext, str) and ext.strip():
        # 飞书的 ext 支持单引号包裹的逗号分隔文件名，文件名本身也可能包含逗号。
        ext = ext.strip()
        if ext.startswith("[") and ext.endswith("]") and ext[1:-1].lstrip().startswith("'"):
            ext = ext[1:-1].lstrip()
        quote = "'" if ext.lstrip().startswith("'") else '"'
        try:
            names = next(csv.reader([ext], quotechar=quote, skipinitialspace=True), [])
        except csv.Error:
            names = []
    else:
        names = ext if isinstance(ext, list) else []
    if len(names) != count:
        return [""] * count
    return [attachment_name(name) for name in names]


def attachment_name(value) -> str:
    """读取附件对象或控件提供的名称，排除用作占位值的临时链接。"""
    if isinstance(value, dict):
        candidates = (value.get(key) for key in ("name", "file_name", "filename", "title"))
        return next((name.strip() for name in candidates if isinstance(name, str) and name.strip() and "://" not in name), "")
    return value.strip() if isinstance(value, str) and "://" not in value else ""


def _attachment_text(value, ext) -> tuple[str | None, int]:
    """附件控件的 value 可能含短效地址，只从名称元数据生成摘要。"""
    files = attachment_values(value)
    if not files:
        return None, 0
    names = [attachment_name(file) if isinstance(file, dict) else "" for file in files]
    names = [name or hint for name, hint in zip(names, attachment_names(ext, len(files)))]
    if all(names):
        return "、".join(names), len(files)
    return f"{len(files)} 个附件", len(files)


def _account_values(value) -> list[dict[str, str]]:
    """只提供现有前端账户格式化器需要的值，去掉飞书银行控件的其他元数据。"""
    values = value if isinstance(value, list) else [value]
    accounts = []
    for item in values:
        item = decoded(item)
        if isinstance(item, dict):
            account = {key: text for key, keys in ACCOUNT_KEYS.items()
                       if (text := _display(decoded(item.get(key)), keys))}
            if account:
                accounts.append(account)
    return accounts


def reimbursement_form(form, mapping: dict) -> list[FeishuFormField]:
    """将实例控件转换为展示字段，仅在历史快照缺少控件名时使用本地模板映射。"""
    labels = {"amount": "报销总额", "description": "报销事由", "company": "报销主体"}
    fallback = {}
    for key, label in FIELD_LABELS.items():
        ids = mapping.get(key)
        for field_id in ids if isinstance(ids, list) else [ids]:
            if isinstance(field_id, str) and field_id:
                fallback.setdefault(field_id, labels.get(key, label))
    attachment_ids = mapping.get("attachment_ids") or []
    if isinstance(attachment_ids, str):
        attachment_ids = [attachment_ids]

    def fields(items, depth=0) -> list[FeishuFormField]:
        if not isinstance(items, list):
            return []
        result = []
        for item in items:
            if not isinstance(item, dict) or item.get("visible") is False:
                continue
            kind = str(item.get("type") or "").lower()
            if kind in DISPLAY_TYPES:
                continue
            field_id = str(item.get("id") or "")
            label = str(item.get("name") or "").strip() or fallback.get(field_id, "未命名字段")
            raw = item.get("value")
            value = raw if kind in {"input", "textarea"} else decoded(raw)
            if kind == "fieldlist":
                # 每条明细由一组控件构成，逐行解析，避免同名字段被合并。
                if depth < 12 and isinstance(value, list) and all(isinstance(row, list) for row in value):
                    field = FeishuFormField(label=label, kind="rows", rows=[fields(row, depth + 1) for row in value])
                else:
                    field = FeishuFormField(label=label, text=UNREADABLE if value else None)
            elif kind in ATTACHMENT_TYPES or field_id in attachment_ids:
                summary, count = _attachment_text(value, item.get("ext"))
                field = FeishuFormField(label=label, kind="attachment", text=summary, attachment_count=count)
            elif kind == "account" and (accounts := _account_values(value)):
                field = FeishuFormField(label=label, kind="account", accounts=accounts)
            elif kind in GROUP_TYPES:
                field = FeishuFormField(label=label, text=UNREADABLE if value else None)
            else:
                keys = (*DISPLAY_KEYS, "open_id", "id") if kind in {"department", "contact", "connect"} else DISPLAY_KEYS
                text = _choice_text(value, item.get("option")) if kind in CHOICE_TYPES else _display(value, keys)
                ext = item.get("ext")
                if text and kind == "amount" and isinstance(ext, dict) and isinstance(ext.get("currency"), str):
                    currency = ext["currency"]
                    text = f"¥ {text}" if currency == "CNY" else f"{currency} {text}".strip()
                field = FeishuFormField(label=label, text=text or (UNREADABLE if value else None))
            result.append(field.model_copy(update={"control_id": field_id}))
        return result

    return fields(decoded(form))
