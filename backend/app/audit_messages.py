"""面向报销人的审核说明；技术证据独立保留，不用规则编码或数据库术语拼接拒绝意见。"""
from datetime import timedelta, timezone

from .models import Approval, InvoiceClaim


def concise(value: str, limit: int = 100) -> str:
    """压缩业务文本中的空白并限制单项长度，避免长标题挤掉提交时间和处理建议。"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def invoice_label(number: str) -> str:
    return f"发票（号码：{number}）" if number else "这张发票"


def duplicate_message(number: str, claim: InvoiceClaim | None, related: Approval | None,
                      applicant_name: str = "", *, business_type: str = "REIMBURSEMENT") -> str:
    """引用真实关联审批与提交时间；缺失时间时不拿采集时间代替，未知姓名不展示 open_id。"""
    label = f"这张发票（号码：{number}）" if number else "这张发票"
    operation = "申请付款" if business_type == "PAYMENT" else "报销"
    if related:
        submitted = ""
        if related.submitted_at:
            utc_time = related.submitted_at
            if utc_time.tzinfo is None:
                utc_time = utc_time.replace(tzinfo=timezone.utc)
            submitted = utc_time.astimezone(timezone(timedelta(hours=8))).strftime("%Y年%m月%d日 %H:%M（北京时间）")
        name = applicant_name or related.applicant
        if name.startswith(("ou_", "on_", "user_id:")) or name == related.applicant_id:
            name = ""
        if submitted:
            by = f"{concise(name, 30)}于{submitted}提交的" if name else f"{submitted}提交的"
        else:
            by = f"{concise(name, 30)}提交的" if name else "另一份"
        reference = f"《{concise(related.title, 90)}》（编号：{concise(related.serial_number, 50)}）"
        kind = "付款申请" if getattr(related, "business_type", "REIMBURSEMENT") == "PAYMENT" else "报销申请"
        reason = f"{label}已在{by}{kind}{reference}中使用。"
        if claim and claim.status == "FROZEN":
            return reason + f"原申请涉及撤销或变更，发票还在核查中，暂不能再次{operation}。请联系财务确认。"
        if related.status == "PENDING":
            return reason + f"该申请仍在审批中，不能重复{operation}。请核对是否重复提交；如需改用本次申请，请先联系财务处理原申请。"
        return reason + f"不能重复{operation}。请核对原申请，避免再次提交同一张发票；如有疑问请联系财务。"
    if claim and claim.status == "HISTORICAL":
        source = f"（来源：{concise(claim.source_label, 80)}）" if claim.source_label else ""
        return f"{label}已在历史报销台账{source}中登记使用，不能再次{operation}。请联系财务核对原记录。"
    return f"{label}已有使用记录，暂不能再次{operation}。请联系财务核实原记录。"


def model_message(title: str, reason: str, *, custom=False) -> str:
    """模型只提供具体疑点；拒绝说明由程序补齐标准名称和处理建议。"""
    prefix = f"这笔费用未满足“{concise(title, 70)}”的报销要求" if custom else "报销事由与票据用途存在疑点"
    reason = concise(reason, 220).rstrip("。；; ")
    return f"{prefix}：{reason}。请核对并说明实际用途，补充或更正相关资料后重新提交。"


def evidence_message(item: dict) -> str:
    """兼容历史审核：未存用户说明时按规则提供通俗解释，不把原始技术证据直接回写。"""
    if item.get("user_message"):
        return item["user_message"]
    fallback = {
        "DUPLICATE": "发票已用于其他报销申请，不能重复报销。请联系财务核对原申请。",
        "COMPANY": "发票的购买方公司未匹配到可报销公司。请核对发票抬头和税号，或联系财务确认。",
        "BUYER_NAME": "发票抬头与公司全称不一致。请核对并更换正确的发票；如属于曾用名，请联系财务确认。",
        "CLAIM_COMPANY": "发票上的购买方公司与本次选择的报销公司不一致。请更正报销公司或更换对应公司的发票。",
        "AMOUNT": "发票金额未满足本次报销金额要求。请核对报销金额，并补充足额发票或调整报销金额。",
        "DATE": "发票的开票日期晚于今天。请核对是否上传错误票据或日期识别有误，确认后重新提交。",
        "ATTACHMENTS": "未取得本次报销的发票附件。请补充发票后重新提交。",
        "RECOGNITION": "发票上的部分信息还未核对清楚。请上传清晰原件，或联系财务核对。",
        "PARSE": "发票附件无法正常读取。请更换可打开且内容完整的原件后重新提交。",
        "IDENTITY": "发票号码等必要信息缺失或无法确认。请补充完整、清晰的发票原件。",
        "FIELD_CONFLICT": "同一张发票的多个附件内容不一致。请核对并重新上传正确的原件。",
        "CANONICAL": "这张发票的信息与已有报销记录不一致。请联系财务核对原票据和报销记录。",
        "REMOVED_AFTER_APPROVAL": "原申请通过后，发票附件发生了变更。请联系财务核对原申请和发票。",
        "AMOUNT_VALID": "本次报销金额必须大于零。请填写正确的报销金额后重新提交。",
    }
    if item.get("result") == "UNKNOWN":
        return f"暂时无法确认“{concise(item['title'], 70)}”是否符合要求，请补充相关信息或联系财务核对。"
    code = item["code"]
    if code == "SEMANTIC" or code.startswith("CUSTOM:"):
        return model_message(item["title"], item["detail"], custom=code.startswith("CUSTOM:"))
    return fallback.get(code, f"“{concise(item['title'], 70)}”仍有问题，请联系财务核对后重新提交。")


def rejection_comment(evidence: list[dict], business_type: str = "REIMBURSEMENT") -> str:
    """为飞书与人工拒绝建议生成同一份说明；按完整条目收录，给省略提示预留空间。

    飞书处理意见最多 600 字。先去掉相同原因，超出时明确引导查看其余问题，
    不用字符串硬切导致审批名称、提交时间或发票号码只剩一半。
    """
    messages = list(dict.fromkeys(evidence_message(item) for item in evidence))
    if not messages:
        return ""
    comment = "本次付款申请暂未通过，请核对以下问题：" if business_type == "PAYMENT" else "本次报销暂未通过，请核对以下问题："
    for index, message in enumerate(messages):
        line = f"\n{index + 1}. {message}"
        remaining = len(messages) - index - 1
        suffix = f"\n另有 {remaining} 项问题，请在财务工作台查看完整原因或联系财务。" if remaining else ""
        if len(comment + line + suffix) <= 600:
            comment += line
            continue
        # 通常每条原因小于 400 字；兼容异常长旧说明时也只取完整首句。
        if index == 0:
            first_sentence = message.split("。", 1)[0] + "。"
            if len(comment + "\n1. " + first_sentence) <= 540:
                comment += "\n1. " + first_sentence
        omitted = len(messages) - index
        return comment + f"\n还有 {omitted} 项问题的完整原因，请在财务工作台查看或联系财务核对。"
    return comment
