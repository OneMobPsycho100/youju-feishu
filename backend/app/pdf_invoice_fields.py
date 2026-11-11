"""PDF 发票文字坐标补充提取；只读文字层，不渲染页面或调用云端识别。"""
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import re
import unicodedata

from .invoice_images import MAX_PAGES, ParseRejected, _pdf_lock
from .invoice_recognition import normalized_candidate


@dataclass(frozen=True)
class TextBox:
    """文字及 PDF 页面坐标，原点在左下角；标签框由其全部字符框合并而成。"""
    text: str
    left: float
    bottom: float
    right: float
    top: float

    @property
    def height(self) -> float:
        return self.top - self.bottom

    @property
    def center_y(self) -> float:
        return (self.top + self.bottom) / 2


def _characters(textpage) -> list[TextBox]:
    """保留字符到坐标的一一对应；去掉 PDFium 自动插入的空白和零面积字符。"""
    count = textpage.count_chars()
    if count > 50000:
        raise ValueError("PDF 单页字符过多，跳过坐标补充提取")
    result, seen = [], set()
    for index in range(count):
        text = unicodedata.normalize("NFKC", textpage.get_text_range(index, 1))
        if not text.strip():
            continue
        left, bottom, right, top = textpage.get_charbox(index)
        if not all(map(isfinite, (left, bottom, right, top))) or right <= left or top <= bottom:
            continue
        for char in text:
            key = (char, round(left, 2), round(bottom, 2), round(right, 2), round(top, 2))
            if not char.isspace() and key not in seen:
                result.append(TextBox(char, left, bottom, right, top))
                seen.add(key)
    return result


def _labels(characters: list[TextBox], pattern: str) -> list[TextBox]:
    """在原文字序列识别标签，要求字符实际同处一行或一列，防止跨区域拼出假标签。"""
    result = []
    for match in re.finditer(pattern, "".join(item.text for item in characters)):
        parts = characters[match.start():match.end()]
        height = max(item.height for item in parts)
        horizontal = max(item.center_y for item in parts) - min(item.center_y for item in parts) <= height * 0.7
        centers_x = [(item.left + item.right) / 2 for item in parts]
        vertical = max(centers_x) - min(centers_x) <= height * 0.7
        if not horizontal and not vertical:
            continue
        # 标签可拆成多个绘制对象，但相邻字符不能隔着整块表单。
        if any(max(abs(a.left - b.left), abs(a.bottom - b.bottom)) > height * 3
               for a, b in zip(parts, parts[1:])):
            continue
        result.append(TextBox(match.group(), min(item.left for item in parts), min(item.bottom for item in parts),
                              max(item.right for item in parts), max(item.top for item in parts)))
    return result


def _same_line(a: TextBox, b: TextBox) -> bool:
    return abs(a.center_y - b.center_y) <= max(a.height, b.height) * 0.6


def _value_right(characters: list[TextBox], label: TextBox, labels: list[TextBox], right: float) -> str:
    """只取标签右侧同行的值，到下一个字段标签或购销方区域边界为止。"""
    boundaries = [other.left for other in labels if other.left > label.right and _same_line(label, other)]
    limit = min([right, *boundaries])
    parts = [item for item in characters if item.left >= label.right - 0.1
             and item.right <= limit and _same_line(label, item)]
    parts.sort(key=lambda item: (item.left, -item.top))
    return "".join(item.text for item in parts).lstrip(":()")


def _page_candidates(characters: list[TextBox], width: float) -> dict[str, set[str]]:
    names = _labels(characters, r"名称:")
    taxes = _labels(characters, r"(?:统一社会信用代码/?纳税人识别号|纳税人识别号|统一社会信用代码):")
    parties = {
        "buyer": _labels(characters, r"购买方信息|购买方(?!名称|税号)|购方(?!名称|税号)"),
        "seller": _labels(characters, r"销售方信息|销售方(?!名称|税号)|销方(?!名称|税号)"),
    }
    headers = {field: _labels(characters, pattern) for field, pattern in {
        "number": r"(?:发票号码|发票编号):",
        "code": r"发票代码:",
        "issue_date": r"(?:开票日期|开票时间):",
        "amount": r"小写",
    }.items()}
    labels = [*names, *taxes, *(label for group in headers.values() for label in group)]
    candidates = defaultdict(set)

    def add(field: str, value: str) -> None:
        if normalized := normalized_candidate(field, value):
            candidates[field].add(normalized)

    for field, group in headers.items():
        for label in group:
            add(field, _value_right(characters, label, labels, width))
    anchors = [anchor for group in parties.values() for anchor in group]
    for party, group in parties.items():
        for anchor in group:
            vertical = anchor.height > (anchor.right - anchor.left) * 1.5
            height = anchor.height / len(anchor.text) if vertical else anchor.height
            left = anchor.right if vertical else anchor.left
            bottom, top = anchor.bottom - height * (2 if vertical else 6), anchor.top + height
            # 同一高度有另一方标签时，它就是当前区域的横向边界；不假设买方一定在左侧。
            neighbors = [other.left for other in anchors if other.left > anchor.right
                         and other.top >= bottom and other.bottom <= top]
            right = min([width, *neighbors])
            below = [other.top for other in anchors if other.top < anchor.bottom
                     and abs(other.left - anchor.left) <= height * 2]
            if below:
                bottom = max(bottom, max(below))
            for label in names + (taxes if party == "buyer" else []):
                if label.left >= left - height * 0.2 and label.right < right and bottom <= label.center_y <= top:
                    field = party + "_name" if label in names else "buyer_tax_id"
                    add(field, _value_right(characters, label, labels, right))
    return candidates


def read_pdf_invoice_fields(path: Path) -> dict:
    """返回 fields、ambiguous_fields 和 field_candidates，均属于同一路本地 TEXT 证据。

    同一字段出现不同有效值时不选择任何一个；不同发票号码直接拒绝整份附件。
    调用方仍须执行原有的整份 PDF 文字层、加密及页数校验。
    """
    import pypdfium2 as pdfium

    candidates = defaultdict(set)
    # 与页面渲染共用 PDFium 全局锁；本段只读字符坐标，不生成或上传图像。
    with _pdf_lock, closing(pdfium.PdfDocument(str(path))) as document:
        if not 0 < len(document) <= MAX_PAGES:
            raise ParseRejected(f"本地解析仅支持不超过 {MAX_PAGES} 页的 PDF")
        for index in range(len(document)):
            with closing(document[index]) as page, closing(page.get_textpage()) as textpage:
                for field, values in _page_candidates(_characters(textpage), page.get_width()).items():
                    candidates[field].update(values)
    if len(candidates.get("number", set())) > 1:
        raise ParseRejected("同一附件包含不同发票号码，请拆分为独立原件后重新提交")
    return {
        "fields": {field: next(iter(values)) for field, values in candidates.items() if len(values) == 1},
        "ambiguous_fields": sorted(field for field, values in candidates.items() if len(values) > 1),
        "field_candidates": {field: sorted(values) for field, values in candidates.items()},
    }
