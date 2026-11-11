"""统一读取表单申报主体；主体展示和确定性审核共用同一份来源。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Iterable, Literal

from .invoice_recognition import normalized_candidate

if TYPE_CHECKING:
    from .models import Company


COMPANY_FIELD_NAMES = (
    "报销主体", "报销公司", "费用承担主体", "公司主体", "申请公司",
    "付款主体", "支付主体", "付款公司",
)
COMPANY_FIELD_TYPES = {"radiov2", "radio", "checkbox", "checkboxv2", "input", "textarea"}


@dataclass(frozen=True)
class CompanyDeclaration:
    """申报快照；present 表示存在主体控件或映射，issues 非空时不能判为一致。"""

    # 表单实际提交的显示名称，保留原值用于展示与核对依据。
    names: tuple[str, ...]
    # 仅包含显式税号控件中格式有效的值，不从票面补填。
    tax_ids: tuple[str, ...]
    present: bool
    issues: tuple[str, ...]


def _decoded(value):
    if isinstance(value, str) and value.strip().startswith(("[", "{")):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _form_fields(value, depth=0):
    """递归读取顶层和明细控件，不把单选选项或普通文本解释为表单控件。"""
    if depth > 12:
        return
    value = _decoded(value)
    if isinstance(value, list):
        for child in value:
            yield from _form_fields(child, depth + 1)
    elif isinstance(value, dict) and value.get("visible") is not False:
        yield value
        children = _decoded(value.get("value"))
        if isinstance(children, list) and str(value.get("type", "")).lower() in {
            "fieldlist", "mutablegroup", "leavegroupv2", "workgroup", "tripgroup", "outgroup",
        }:
            yield from _form_fields(children, depth + 1)


def _field_texts(field: dict) -> list[str]:
    """解析文本、结构化选中值及选项标签；缺值保留空串，避免部分缺失被静默忽略。"""
    options = _decoded(field.get("option"))
    labels = {}
    for option in options if isinstance(options, list) else [options]:
        if not isinstance(option, dict):
            continue
        label = next((option[key].strip() for key in ("text", "label", "name")
                      if isinstance(option.get(key), str) and option[key].strip()), "")
        if label:
            for key in ("key", "id", "value"):
                if isinstance(option.get(key), (str, int)):
                    option_id = str(option[key])
                    labels[option_id] = "" if option_id in labels and labels[option_id] != label else label

    def texts(value, depth=0):
        if depth > 12:
            return [""]
        value = _decoded(value)
        if isinstance(value, str):
            text = value.strip()
            return [labels.get(text, text)]
        if isinstance(value, list):
            return [text for item in value for text in texts(item, depth + 1)] or [""]
        if isinstance(value, dict):
            for key in ("name", "text", "label", "value"):
                if value.get(key):
                    return texts(value[key], depth + 1)
            # 只有快照提供了对应标签，才能把选项标识还原成主体名称。
            for key in ("key", "id"):
                if str(value.get(key)) in labels:
                    return [labels[str(value[key])]]
        return [""]

    return texts(field.get("value"))


def read_company_declaration(form: list, mapping: dict | None = None, *, include_names=True) -> CompanyDeclaration:
    """优先读取 company 映射；旧报销模板按明确的主体控件名称兼容，税号只读显式映射。

    已配置但缺失、同名控件有歧义或税号格式错误均保留为待确认，不能回退成不适用。
    include_names=False 用于保留付款流程原有的独立主体审核，仅读取其税号约束。
    """
    mapping = mapping or {}
    fields = list(_form_fields(form))
    name_id = mapping.get("company", "") if include_names else ""
    name_fields = [field for field in fields if field.get("id") == name_id] if name_id else [
        field for field in fields if include_names and str(field.get("name", "")).strip() in COMPANY_FIELD_NAMES
        and str(field.get("type") or "").lower() in (COMPANY_FIELD_TYPES | {""})]
    tax_id = mapping.get("company_tax_id", "")
    tax_fields = [field for field in fields if tax_id and field.get("id") == tax_id]
    names = [text for field in name_fields for text in _field_texts(field)]
    raw_tax_ids = [text for field in tax_fields for text in _field_texts(field)]
    issues = []
    if (name_id or name_fields) and (not names or any(not text for text in names)):
        issues.append("表单申报主体名称缺失或无法读取")
    if not name_id and len({field.get("id") for field in name_fields}) > 1:
        issues.append("存在多个申报主体控件，请在模板中明确映射")
    tax_ids = [normalized_candidate("buyer_tax_id", text) for text in raw_tax_ids]
    if tax_id and (not tax_ids or any(not value for value in tax_ids)):
        issues.append("已配置主体税号映射，但表单未提供完整有效的税号")
    return CompanyDeclaration(
        names=tuple(dict.fromkeys(text for text in names if text)),
        tax_ids=tuple(sorted({value for value in tax_ids if value})),
        present=bool(name_id or name_fields or tax_id), issues=tuple(issues))


def declared_company_names(form: list, mapping: dict | None = None) -> list[str]:
    """返回实际申报名称供列表展示；不能据此修改票面主体归属。"""
    return list(read_company_declaration(form, mapping).names)


def compare_declared_company(declaration: CompanyDeclaration, companies: Iterable[Company],
                             buyer_name: str, buyer_tax_id: str) -> tuple[Literal["PASS", "FAIL", "UNKNOWN", "SKIPPED"], str, str]:
    """返回事实结果、核对依据和申报名称；处理动作继续由模板的 CLAIM_COMPANY 策略决定。"""
    companies = list(companies)
    by_tax_id = {company.tax_id: company.name for company in companies}
    by_name: dict[str, set[str]] = {}
    for company in companies:
        if company.active:
            for name in {company.name, *company.aliases}:
                normalized = normalized_candidate("buyer_name", name)
                if normalized:
                    by_name.setdefault(normalized, set()).add(company.tax_id)
    issues = list(declaration.issues)
    named_tax_ids = set()
    for name in declaration.names:
        matches = by_name.get(normalized_candidate("buyer_name", name), set())
        if len(matches) == 1:
            named_tax_ids.update(matches)
        else:
            issues.append(f"申报主体《{name}》未唯一匹配到已启用公司，请核对主体配置或别名")
    tax_ids = set(declaration.tax_ids)
    # 名称和显式税号都存在时必须相互印证，不能选取其中一项来掩盖另一个字段的冲突。
    if tax_ids and named_tax_ids and tax_ids != named_tax_ids:
        issues.append("表单申报主体名称与所填主体税号不一致")
    declared_label = "、".join(declaration.names) or "、".join(
        by_tax_id.get(value, f"税号 {value} 的公司") for value in declaration.tax_ids)
    detail = f"表单申报主体：{declared_label or '未取得有效值'}；票面购买方：{buyer_name}（税号：{buyer_tax_id}）"
    if declaration.tax_ids:
        detail += "；表单主体税号：" + "、".join(declaration.tax_ids)
    if not declaration.present:
        return "SKIPPED", "未提供申报主体名称且未配置主体税号映射，此项不适用", ""
    if issues:
        return "UNKNOWN", detail + "；" + "；".join(dict.fromkeys(issues)), declared_label
    matches = tax_ids or named_tax_ids
    return ("PASS" if buyer_tax_id in matches else "FAIL"), detail, declared_label
