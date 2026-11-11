"""费用报销与付款申请的 A4 打印模板，以及附件转印所用的中文排版。"""
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
from threading import Lock
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont, TTFError
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from .archive_center import BEIJING
from .archive_department import department_label
from .config import settings
from .errors import DomainError
from .feishu_form import _account_values
from .payment_service import display
from .schemas import FeishuFormField
from .template_matching import normalized_name


FONT = "ArchiveChinese"
FONT_LOCK = Lock()
PAGE_WIDTH = 170 * mm
MARGIN = (A4[0] - PAGE_WIDTH) / 2
LABEL_FILL = colors.HexColor("#eff0f2")
BORDER = colors.HexColor("#252b32")


def ensure_print_font() -> None:
    """嵌入可用的中文 TrueType 字体，保证浏览器和打印机显示一致。"""
    with FONT_LOCK:
        if FONT in pdfmetrics.getRegisteredFontNames():
            return
        configured = settings().print_cjk_font
        system_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        candidates = [configured] if configured else [
            Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
            system_fonts / "msyh.ttc", system_fonts / "simhei.ttf",
        ]
        for candidate in candidates:
            if candidate and candidate.is_file():
                try:
                    pdfmetrics.registerFont(TTFont(FONT, str(candidate), subfontIndex=0))
                    return
                except (OSError, ValueError, TTFError):
                    continue
        raise DomainError("打印环境缺少可用的中文字体，请检查镜像或 PRINT_CJK_FONT")


def _text(value, *, size=8.9, leading=14, align=0, color=colors.HexColor("#1e252d")):
    style = ParagraphStyle("archive", fontName=FONT, fontSize=size, leading=leading, textColor=color,
                           alignment=align, wordWrap="CJK", splitLongWords=True)
    content = str(value if value not in ("", None) else "—")
    # 表单、评论和原件内容只作为文本；ReportLab 的段落标记不由数据控制。
    content = "".join(char for char in content if ord(char) >= 32 or char in "\n\t")
    return Paragraph(escape(content).replace("\n", "<br/>").replace("\t", "    "), style)


@dataclass
class FormTable:
    rows: list = field(default_factory=list)
    styles: list = field(default_factory=list)

    def section(self, label: str, *, centered=True):
        row = len(self.rows)
        self.rows.append([_text(label, align=TA_CENTER if centered else 0), "", "", "", ""])
        self.styles.append(("SPAN", (0, row), (4, row)))

    def pair(self, label, value, other_label=None, other_value=None):
        row = len(self.rows)
        self.styles.append(("BACKGROUND", (0, row), (0, row), LABEL_FILL))
        if other_label is None:
            self.rows.append([_text(label), _text(value), "", "", ""])
            self.styles.append(("SPAN", (1, row), (4, row)))
        else:
            self.rows.append([_text(label), _text(value), _text(other_label), "", _text(other_value)])
            self.styles.extend([("SPAN", (2, row), (3, row)), ("BACKGROUND", (2, row), (3, row), LABEL_FILL)])

    def event(self, label, action, opinion):
        if not opinion:
            self.pair(label, action)
            return
        row = len(self.rows)
        self.rows.append([_text(label), _text(action), "", _text(opinion), ""])
        self.styles.extend([("SPAN", (1, row), (2, row)), ("SPAN", (3, row), (4, row)),
                            ("BACKGROUND", (0, row), (0, row), LABEL_FILL)])

    def build(self):
        table = Table(self.rows, colWidths=[32.7 * mm, 52.3 * mm, 13 * mm, 19.7 * mm, 52.3 * mm],
                      hAlign="CENTER", splitInRow=1)
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 3.8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3.8), ("LEFTPADDING", (0, 0), (-1, -1), 7),
            ("RIGHTPADDING", (0, 0), (-1, -1), 7), ("GRID", (0, 0), (-1, -1), 0.6, BORDER),
            *self.styles,
        ]))
        return table


@dataclass(frozen=True)
class PrintForm:
    """生成单份审批页所需的脱离数据库的快照。"""
    summary: dict
    fields: list[FeishuFormField]
    mapping: dict
    department: str
    payment: dict
    snapshot: dict
    # 附件索引是否仍匹配审批当前版本，与部门的授权或同步状态独立。
    attachments_current: bool = False

    def pick(self, key="", *aliases, fields=None):
        choices = self.fields if fields is None else fields
        mapped = self.mapping.get(key)
        if mapped and fields is None:
            for item in choices:
                if item.control_id == mapped:
                    return item
        labels = {normalized_name(alias) for alias in aliases}
        return next((item for item in choices if normalized_name(item.label) in labels), None)


def _time(value, pattern="%Y-%m-%d %H:%M") -> str:
    if not value:
        return "待补齐"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.removesuffix("Z"))
        except ValueError:
            return "待补齐"
    return (value + BEIJING).strftime(pattern)


def _account(value) -> str:
    accounts = value if isinstance(value, list) and all(isinstance(item, dict) and
        isinstance(item.get("widgetAccountName"), str) for item in value) else _account_values(value)
    lines = []
    for item in accounts:
        bank_number = " ".join(filter(None, (item.get("widgetAccountBankName"), item.get("widgetAccountNumber"))))
        lines.extend(filter(None, (
            "/".join(filter(None, (item.get("widgetAccountType"), item.get("widgetAccountName"), bank_number))),
            "/".join(filter(None, (item.get("widgetAccountBankArea"), item.get("widgetAccountBankBranch")))),
        )))
    return "\n".join(lines) or display(value)


def _value(item: FeishuFormField | None, fallback="") -> str:
    if item is None:
        return fallback
    if item.kind == "account":
        return _account(item.accounts) or fallback
    if item.kind == "attachment":
        return f"{item.attachment_count} 个"
    return item.text or fallback


def _amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def _upper_rmb(value: Decimal) -> str:
    digits = "零壹贰叁肆伍陆柒捌玖"
    cents = int(value * 100)
    number, tail = divmod(abs(cents), 100)
    groups = []
    while number:
        number, group = divmod(number, 10000)
        groups.append(group)
    parts, skipped = [], False
    for position in range(len(groups) - 1, -1, -1):
        group = groups[position]
        if not group:
            skipped = True
            continue
        if parts and (skipped or group < 1000):
            parts.append("零")
        segment, zero = [], False
        for offset, digit in zip(range(3, -1, -1), f"{group:04d}"):
            if digit == "0":
                zero = bool(segment)
            else:
                if zero:
                    segment.append("零")
                segment.append(digits[int(digit)] + ("", "拾", "佰", "仟")[offset])
                zero = False
        parts.append("".join(segment) + ("", "万", "亿", "兆", "京")[position])
        skipped = False
    yuan = ("".join(parts) or "零") + "元"
    jiao, fen = divmod(tail, 10)
    fraction = (digits[jiao] + "角" if jiao else "零" if fen and groups else "") + (digits[fen] + "分" if fen else "")
    return ("负" if cents < 0 else "") + yuan + (fraction or "整")


def _subject(form: PrintForm, key: str, *labels):
    field = form.pick(key, *labels)
    fallback = "、".join(form.summary["declared_company_names"]) or "、".join(
        company["name"] for company in form.summary["companies"])
    return _value(field, fallback or "待确认主体")


def _additional(table: FormTable, fields, used):
    """实例包含样张之外的实际业务控件时，按原顺序保留有值字段。"""
    chosen = {id(item) for item in used if item is not None}
    for item in fields:
        if id(item) in chosen or item.label == "未命名字段":
            continue
        if item.kind == "rows":
            if not item.rows:
                continue
            table.section(item.label, centered=False)
            for index, row in enumerate(item.rows, 1):
                if len(item.rows) > 1:
                    table.section(f"第 {index} 项", centered=False)
                _additional(table, row, [])
        elif value := _value(item):
            table.pair(item.label, value)


def _reimbursement(table: FormTable, form: PrintForm) -> None:
    subject = form.pick("company", "报销主体", "报销公司", "费用承担主体")
    reason = form.pick("description", "报销事由", "申请事由", "报销说明")
    department = form.pick("department", "部门", "申请部门", "所属部门")
    total = form.pick("amount", "费用汇总", "费用合计", "报销总额", "申请金额")
    group = next((item for item in form.fields if item.kind == "rows" and "明细" in item.label), None)
    group = group or next((item for item in form.fields if item.kind == "rows"), None)
    summary_sheet = form.pick("", "汇总表", "费用汇总表", "报销汇总表")
    account = form.pick("payee_account", "收款账户", "银行账户", "收款账号")
    table.pair("报销主体", _subject(form, "company", "报销主体", "报销公司", "费用承担主体"),
               "报销事由", _value(reason, form.summary["title"]))
    table.pair("部门", _value(department, form.department), "费用汇总", _amount(form.summary["amount"]))
    if group and group.rows:
        table.section("费用明细", centered=False)
        for index, row in enumerate(group.rows, 1):
            kind = form.pick("", "报销类型", "费用类别", "费用类型", "报销类别", fields=row)
            content = form.pick("", "内容", "费用内容", "明细内容", "说明", "费用说明", fields=row)
            amount = form.pick("", "金额", "报销金额", "费用金额", fields=row)
            attachments = [item for item in row if item.kind == "attachment"]
            if len(group.rows) > 1:
                table.section(f"第 {index} 项", centered=False)
            table.pair("报销类型", _value(kind))
            table.pair("内容", _value(content))
            amount_text = _value(amount)
            if amount_text:
                try:
                    amount_text = _amount(Decimal(amount_text.removeprefix("¥ ").replace(",", ""))) + " CNY-人民币元"
                except InvalidOperation:
                    pass
            table.pair("金额", amount_text, "对应附件", f"{sum(item.attachment_count for item in attachments)} 个")
            _additional(table, row, [kind, content, amount, *attachments])
    else:
        table.pair("报销类型", _value(form.pick("expense_type", "报销类型", "费用类型")))
    if summary_sheet:
        table.pair("汇总表", _value(summary_sheet))
    if account:
        table.pair("收款账户", _value(account))
    _additional(table, form.fields, [subject, reason, department, total, group, summary_sheet, account,
        form.pick("company_tax_id", "主体税号"), form.pick("expense_type", "报销类型", "费用类型")])


def _payment(table: FormTable, form: PrintForm) -> None:
    payment = form.payment
    subject = form.pick("company", "付款主体", "付款公司")
    reason = form.pick("description", "付款事由", "申请事由")
    department = form.pick("department", "部门", "申请部门")
    amount = form.pick("amount", "付款金额", "申请金额")
    category = form.pick("expense_type", "费用类别", "费用类型")
    method = form.pick("payment_method", "付款方式", "支付方式")
    due = form.pick("expected_payment_date", "期望付款日期", "付款日期")
    account = form.pick("payee_account", "收款账户", "银行账户")
    related = form.pick("related_approvals", "关联审批", "相关审批")
    remarks = form.pick("remarks", "备注", "补充说明")
    project = form.pick("project", "所属项目", "项目")
    value = form.summary["amount"]
    table.pair("付款主体", _value(subject, payment.get("company_name") or _subject(form, "company", "付款主体")))
    table.pair("付款事由", _value(reason, form.summary["title"]))
    table.pair("部门", _value(department, form.department))
    table.pair("付款金额", f"{_amount(value)} CNY-人民币元  {_upper_rmb(value)}")
    table.pair("费用类别", _value(category, payment.get("expense_type")), "付款方式", _value(method, payment.get("payment_method")))
    table.pair("期望付款日期", _value(due, payment.get("expected_payment_date")))
    table.pair("收款账户", _value(account, _account(payment.get("payee_account"))))
    if value := form.snapshot.get("related_approvals") or _value(related):
        table.pair("关联审批", value)
    table.pair("附件", f"{form.summary['attachment_count']} 个")
    if value := _value(remarks, payment.get("remarks")):
        table.pair("备注", value)
    if value := _value(project, payment.get("project")):
        table.pair("所属项目", value)
    _additional(table, form.fields, [subject, reason, department, amount, category, method, due, account,
        related, remarks, project, *(item for item in form.fields if item.kind == "attachment")])


def _document(path: Path):
    # Frame 默认有 6 pt 内边距，将其计入页边距后表格与样张的纸面位置一致。
    return SimpleDocTemplate(str(path), pagesize=A4, leftMargin=MARGIN - 6, rightMargin=MARGIN - 6,
                             topMargin=13.5 * mm - 6, bottomMargin=15 * mm, allowSplitting=1)


def render_approval(path: Path, form: PrintForm, printer: str, printed_at: datetime) -> None:
    """以样张的表头、业务字段和审批轨迹渲染审批页，长内容允许在行内分页。"""
    ensure_print_font()
    row = form.summary
    payment = row["business_type"] == "PAYMENT"
    title = "付款申请" if payment else "费用报销"
    document = _document(path)
    document.title = f"{title} {row['serial_number']}"
    document.author = printer
    meta_color = colors.HexColor("#68717c")
    meta = Table([[_text("申请编号：" + row["serial_number"], size=7.7, leading=11, color=meta_color),
        _text(f"打印人员：{printer}\n打印时间：{_time(printed_at)}", size=7.7, leading=11, align=TA_RIGHT, color=meta_color)]],
        colWidths=[PAGE_WIDTH / 2] * 2, hAlign="CENTER")
    meta.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    table = FormTable()
    table.pair("申请人", row["applicant"], "申请时间", _time(row["submitted_at"], "%Y-%m-%d"))
    table.pair("所属部门", department_label(form.snapshot), "审批状态", "已通过")
    table.section("审批详情")
    if payment:
        _payment(table, form)
    else:
        _reimbursement(table, form)
    table.section("审批记录")
    for event in form.snapshot.get("events", []):
        action = " / ".join((event["actor"], event["action"], _time(event["at"])))
        table.event(event["node"], action, event["opinion"])
    if not form.snapshot.get("history_available"):
        table.pair("审批记录", "飞书审批记录待同步")

    def continuation(canvas, doc):
        canvas.setFont(FONT, 8)
        canvas.drawString(MARGIN, A4[1] - 10 * mm, f"{title} · 申请编号 {row['serial_number']}")
        canvas.drawRightString(A4[0] - MARGIN, A4[1] - 10 * mm, f"第 {doc.page} 页")

    document.build([_text(title, size=14, leading=21, align=TA_CENTER), Spacer(1, 1.5 * mm), meta,
                    Spacer(1, 3.5 * mm), table.build()], onLaterPages=continuation)


def render_plain(path: Path, title: str, lines: list[str]) -> None:
    """将结构化原件、文本或打印例外逐行排版为普通 A4 打印页。"""
    ensure_print_font()
    document = _document(path)
    document.title = title
    document.build([_text(title, size=14, leading=20, align=TA_CENTER), Spacer(1, 5 * mm),
                    *[_text(line, size=9.5, leading=15) for line in lines]])
