"""从已保存的原件解析记录提取展示信息，与审批判定分离。"""
import re
import unicodedata
from decimal import Decimal

from .invoice_recognition import TENCENT_OCR, normalized_candidate
from .models import Attachment, Invoice
from .parser import invoice_key
from .schemas import InvoiceDisplayOut

NET_TAGS = {"totalamwithouttax", "totalamtwithouttax", "totalamountwithouttax", "hjje", "不含税金额合计", "合计不含税金额"}
TAX_TAGS = {"totaltaxam", "totaltaxamount", "taxtotalamount", "hjse", "合计税额", "税额合计"}
TYPE_TAGS = {"invoicename", "invoicetypename", "invoicetype", "labelname", "发票名称", "发票类型"}
MONEY = r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?"
MONEY_CELL = r"¥?[ \t]*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})"
TOTAL_ROW = re.compile(rf"合[ \t]*计[ \t]*:?[ \t]*{MONEY_CELL}(?:[ \t]+|(?=¥)){MONEY_CELL}")

ELECTRONIC_TYPES = {
    "普通发票": "数电票（普通发票）",
    "增值税专用发票": "数电票（增值税专用发票）",
}
TRADITIONAL_TYPES = {
    f"增值税{form}{category}发票"
    for form in ("", "电子")
    for category in ("普通", "专用")
}


def _matches_invoice(fields: dict, invoice: Invoice) -> bool:
    """校验解析来源仍对应台账票号和价税合计，防止复用纠正前的候选。"""
    try:
        key, _ = invoice_key(fields.get("number", ""), fields.get("code", ""))
    except ValueError:
        return False
    amount = normalized_candidate("amount", fields.get("amount"))
    return key == invoice.invoice_key and bool(amount) and Decimal(amount) == invoice.amount


def _source_texts(attachment: Attachment, invoice: Invoice):
    """读取与台账身份一致的原件证据，兼容旧记录和腾讯云结构化字段。"""
    parsed = attachment.parsed_data or {}
    if (parsed.get("rejected") or parsed.get("error") or parsed.get("material_type", "INVOICE") != "INVOICE"
            or not _matches_invoice(parsed.get("fields") or {}, invoice)):
        return
    sources = parsed.get("sources") or {}
    # 旧记录只有纠正后的字段时，无法用原来源的票号核对旧原文。
    if (not sources and not parsed.get("confirmed") and parsed.get("source") in {"XML", "TEXT", "OCR"}
            and parsed.get("raw_text")):
        yield parsed["source"], parsed["raw_text"]
    for name in ("XML", "TEXT", TENCENT_OCR, "OCR"):
        source = sources.get(name) or {}
        if (source.get("status") == "COMPLETED" and source.get("raw_text")
                and _matches_invoice(source.get("fields") or {}, invoice)):
            yield name, source["raw_text"]


def _xml_amount(value: str) -> Decimal | None:
    value = value.strip().removeprefix("¥").strip()
    return Decimal(value.replace(",", "")) if re.fullmatch(MONEY, value) else None


def _invoice_type(title: str, kind: str, *, xml: bool) -> str | None:
    title = re.sub(r"\s+", "", title)
    if not xml:
        # PDF 文字层与 OCR 会把标题和同一行右侧的票号或日期拼在一起。
        title = re.split(r"发票号码|发票代码|开票日期", title, maxsplit=1)[0]
    if kind == "E_INVOICE":
        for label, display in ELECTRONIC_TYPES.items():
            if title in {f"电子发票({label})", f"数电票({label})", f"数电发票({label})"} or (xml and title == label):
                return display
    elif title in TRADITIONAL_TYPES:
        return title
    return None


def _display_candidates(name: str, text: str, kind: str) -> tuple[set[str], set[tuple[Decimal, Decimal]]]:
    types, pairs = set(), set()
    lines = unicodedata.normalize("NFKC", text).splitlines()
    if name in {"XML", TENCENT_OCR}:
        net_values, tax_values = set(), set()
        for line in lines:
            tag, separator, value = line.partition(":")
            if not separator:
                continue
            tag = tag.strip().casefold()
            if tag in NET_TAGS or (name == TENCENT_OCR and tag == "合计金额"):
                net_values.add(_xml_amount(value))
            elif tag in TAX_TAGS:
                tax_values.add(_xml_amount(value))
            elif tag in TYPE_TAGS:
                types.update(label for part in value.split(",") if (label := _invoice_type(part, kind, xml=name == "XML")))
        if len(net_values) == len(tax_values) == 1 and None not in net_values and None not in tax_values:
            pairs.add((net_values.pop(), tax_values.pop()))
    else:
        for line in lines:
            if label := _invoice_type(line, kind, xml=False):
                types.add(label)
            # 仅使用整票“合计”行的两列，避开明细行税额和“价税合计”。
            if match := TOTAL_ROW.fullmatch(line.strip()):
                net, tax = match.groups()
                pairs.add((Decimal(net.replace(",", "")), Decimal(tax.replace(",", ""))))
    return types, pairs


def invoice_display(invoice: Invoice, attachments: list[Attachment], expense_type: str | None) -> InvoiceDisplayOut:
    """金额须通过价税勾稽；同票多个来源的候选有分歧时整组留空。"""
    types, pairs = set(), set()
    for attachment in attachments:
        for name, text in _source_texts(attachment, invoice):
            source_types, source_pairs = _display_candidates(name, text, invoice.kind)
            types.update(source_types)
            pairs.update(source_pairs)
    net, tax = next(iter(pairs)) if len(pairs) == 1 else (None, None)
    if net is not None and tax is not None and net + tax == invoice.amount:
        net, tax = net.quantize(Decimal("0.01")), tax.quantize(Decimal("0.01"))
    else:
        net, tax = None, None
    fallback_type = "数电发票" if invoice.kind == "E_INVOICE" else "传统发票"
    return InvoiceDisplayOut(
        billing_type=next(iter(types)) if len(types) == 1 else fallback_type,
        expense_type=(expense_type or "").strip() or None,
        tax_amount=tax,
        untaxed_amount=net,
    )
