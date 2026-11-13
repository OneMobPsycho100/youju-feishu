"""从已保存的原件解析记录提取展示信息，与审批判定分离。"""
import re
import unicodedata
from decimal import Decimal

from .invoice_recognition import TENCENT_OCR, normalized_candidate
from .models import Attachment, Invoice
from .parser import invoice_key
from .schemas import InvoiceDisplayOut

NET_TAGS = {"totalamwithouttax", "totalamtwithouttax", "totalamountwithouttax", "totalamountexcludingtax", "hjje", "不含税金额合计", "合计不含税金额"}
TAX_TAGS = {"totaltaxam", "totaltaxamount", "taxtotalamount", "hjse", "合计税额", "税额合计"}
TYPE_TAGS = {"invoicename", "invoicetypename", "invoicetype", "typeofinvoice", "labelname", "发票名称", "发票类型"}
RATE_TAGS = {"taxrate", "taxrateorlevyrate", "税率", "征收率", "税率/征收率"}
MONEY = r"[0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?"
MONEY_CELL = r"¥?[ \t]*([0-9]+(?:,[0-9]{3})*\.[0-9]{2})"
TOTAL_ROW = re.compile(rf"合[ \t]*计[ \t]*:?[ \t]*{MONEY_CELL}(?:[ \t]+|(?=¥)){MONEY_CELL}")
SIGNED_MONEY = rf"[+-]?{MONEY}"
PERCENT = r"[0-9]{1,3}(?:\.[0-9]{1,6})?[ \t]*%"
DETAIL_HEADER = re.compile(r"税[ \t]*率.*税[ \t]*额")
DETAIL_HINT = re.compile(r"%|免税|不征税|零税率|(?:\.[0-9]{2}|\*{3})[ \t]*$")
DETAIL_TAX = re.compile(
    rf"(?<![0-9.])({PERCENT}|免税|不征税|零税率)[ \t]*"
    rf"(?:税[ \t]*额[ \t]*:?[ \t]*)?¥?[ \t]*({SIGNED_MONEY}|\*{{3}})[ \t]*$"
)

ELECTRONIC_TYPES = {
    "普通发票": "数电票（普通发票）",
    "增值税专用发票": "数电票（增值税专用发票）",
}
TRADITIONAL_TYPES = {
    f"增值税{form}{category}发票"
    for form in ("", "电子")
    for category in ("普通", "专用")
}
BILLING_TYPES = (*ELECTRONIC_TYPES.values(), *sorted(TRADITIONAL_TYPES))


def _matches_invoice(fields: dict, invoice: Invoice) -> bool:
    """校验解析来源仍对应台账票号和价税合计，防止复用纠正前的候选。"""
    try:
        key, _ = invoice_key(fields.get("number", ""), fields.get("code", ""))
    except ValueError:
        return False
    amount = normalized_candidate("amount", fields.get("amount"))
    return key == invoice.invoice_key and bool(amount) and Decimal(amount) == invoice.amount


def _invoice_sources(attachment: Attachment, invoice: Invoice):
    """读取与台账身份一致的原件证据，兼容旧记录和腾讯云结构化字段。"""
    parsed = attachment.parsed_data or {}
    if (parsed.get("rejected") or parsed.get("error") or parsed.get("material_type", "INVOICE") != "INVOICE"
            or not _matches_invoice(parsed.get("fields") or {}, invoice)):
        return
    sources = parsed.get("sources") or {}
    # 旧记录只有纠正后的字段时，无法用原来源的票号核对旧原文。
    if (not sources and not parsed.get("confirmed") and parsed.get("source") in {"XML", "TEXT", "OCR"}
            and parsed.get("raw_text")):
        yield parsed["source"], parsed
    for name in ("XML", "TEXT", TENCENT_OCR, "OCR"):
        source = sources.get(name) or {}
        if (source.get("status") == "COMPLETED" and source.get("raw_text")
                and _matches_invoice(source.get("fields") or {}, invoice)):
            yield name, source


def _amount(value: str, *, signed: bool = False) -> Decimal | None:
    value = value.strip().removeprefix("¥").strip()
    return Decimal(value.replace(",", "")) if re.fullmatch(SIGNED_MONEY if signed else MONEY, value) else None


def _percent(value: str) -> Decimal | None:
    value = value.strip()
    if not re.fullmatch(PERCENT, value):
        return None
    percent = Decimal(value.removesuffix("%").strip())
    return percent if percent <= 100 else None


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


def _display_candidates(name: str, lines: list[str], kind: str) -> tuple[set[str], set[tuple[Decimal, Decimal]]]:
    types, pairs = set(), set()
    if name in {"XML", TENCENT_OCR}:
        net_values, tax_values = set(), set()
        for line in lines:
            tag, separator, value = line.partition(":")
            if not separator:
                continue
            tag = tag.strip().casefold()
            if tag in NET_TAGS or (name == TENCENT_OCR and tag == "合计金额"):
                net_values.add(_amount(value))
            elif tag in TAX_TAGS:
                tax_values.add(_amount(value))
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


def _pdf_tax_rates(lines: list[str], net: Decimal, tax: Decimal) -> set[Decimal | None]:
    """只接受明细区内能与整票税额勾稽的税率，列头可能和第一行明细合在一行。"""
    rates = set()
    start = None
    for index, line in enumerate(lines):
        if start is None and DETAIL_HEADER.search(line):
            start = index
        if start is None or not (total := TOTAL_ROW.fullmatch(line.strip())):
            continue
        rows = lines[start:index]
        start = None
        if tuple(Decimal(value.replace(",", "")) for value in total.groups()) != (net, tax):
            continue
        items = [DETAIL_TAX.search(row) for row in rows if DETAIL_HINT.search(row)]
        if not items or any(item is None for item in items):
            continue
        values = [item.groups() for item in items]
        # 免税等文字税率可能以 *** 显示税额，保留其非数值税率，避免与应税行合成单一税率。
        if any(amount == "***" and "%" in rate for rate, amount in values):
            continue
        if sum(Decimal(amount.replace(",", "")) for _, amount in values if amount != "***") == tax:
            rates.update(_percent(rate) for rate, _ in values)
    return rates


def _ocr_tax_rates(source: dict, net: Decimal, tax: Decimal) -> set[Decimal | None]:
    """逐组核对结构化明细，避免重复页或只含部分明细的页面给出整票税率。"""
    rates = set()
    for items in source.get("tax_item_groups", []):
        net_amounts = [_amount(item["untaxed_amount"], signed=True) for item in items]
        tax_amounts = [_amount(item["tax_amount"], signed=True) for item in items]
        if not items or None in net_amounts or None in tax_amounts:
            continue
        if sum(net_amounts) == net and sum(tax_amounts) == tax:
            rates.update(_percent(item["tax_rate"]) for item in items)
    return rates


def _source_tax_rates(name: str, source: dict, lines: list[str], net: Decimal, tax: Decimal) -> set[Decimal | None]:
    if name == TENCENT_OCR:
        return _ocr_tax_rates(source, net, tax)
    if name == "XML":
        rates = set()
        for line in lines:
            tag, separator, value = line.partition(":")
            if separator and tag.strip().casefold() in RATE_TAGS:
                rates.update(_percent(part) for part in value.split(","))
        return rates
    return _pdf_tax_rates(lines, net, tax)


def invoice_display(invoice: Invoice, attachments: list[Attachment], expense_type: str | None, *,
                    deduction_modes: dict[str, str] | None = None) -> InvoiceDisplayOut:
    """按同票已核对的证据和主体规则计算页面字段，税率缺失或冲突时留空。"""
    types, pairs, evidence = set(), set(), []
    for attachment in attachments:
        for name, source in _invoice_sources(attachment, invoice):
            lines = unicodedata.normalize("NFKC", source["raw_text"]).splitlines()
            source_types, source_pairs = _display_candidates(name, lines, invoice.kind)
            types.update(source_types)
            pairs.update(source_pairs)
            evidence.append((name, source, lines, source_pairs))
    net, tax = next(iter(pairs)) if len(pairs) == 1 else (None, None)
    if net is not None and tax is not None and net + tax == invoice.amount:
        net, tax = net.quantize(Decimal("0.01")), tax.quantize(Decimal("0.01"))
    else:
        net, tax = None, None
    billing_type = next(iter(types)) if len(types) == 1 else None
    mode = (deduction_modes or {}).get(billing_type) if billing_type else None
    deductible_rate, deductible_amount = None, None
    if mode == "NONE":
        deductible_rate, deductible_amount = Decimal(0), Decimal("0.00")
    elif mode == "FULL" and net is not None and tax is not None:
        deductible_amount = tax
        rates = set()
        for name, source, lines, source_pairs in evidence:
            if (net, tax) in source_pairs:
                rates.update(_source_tax_rates(name, source, lines, net, tax))
        if len(rates) == 1:
            deductible_rate = next(iter(rates))
    return InvoiceDisplayOut(
        billing_type=billing_type or ("数电发票" if invoice.kind == "E_INVOICE" else "传统发票"),
        expense_type=(expense_type or "").strip() or None,
        tax_amount=tax,
        untaxed_amount=net,
        reimbursable_amount=invoice.amount,
        deductible_tax_rate=deductible_rate,
        deductible_tax_amount=deductible_amount,
    )
