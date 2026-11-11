"""预审标准的统一策略映射；模型只提供事实判定，处理动作始终由程序配置决定。"""
from copy import deepcopy


AUDIT_POLICY_VERSION = "manual-review-priority-v3"
PAYMENT_PDF_POLICY_VERSION = "payment-pdf-prefix-v1"
POLICY_SCHEMA_VERSION = 3
BUILTIN_DEFAULT_ACTIONS = {
    "DUPLICATE": "REJECT", "COMPANY": "REJECT", "BUYER_NAME": "REJECT",
    "CLAIM_COMPANY": "REJECT", "AMOUNT": "REJECT", "DATE": "REVIEW", "SEMANTIC": "REVIEW",
}
BUSINESS_DEFAULT_ACTIONS = {key: value for key, value in BUILTIN_DEFAULT_ACTIONS.items() if key != "DUPLICATE"}
BUSINESS_DEFAULT_ACTIONS.update(ATTACHMENTS="REVIEW", AMOUNT_VALID="REVIEW")
BUILTIN_LABELS = {
    "COMPANY": ("购买方主体匹配", "票面购买方税号须匹配已启用公司。"),
    "BUYER_NAME": ("购买方名称核对", "购买方名称应为公司法定名称或允许的别名。"),
    "CLAIM_COMPANY": ("申报主体与票据一致", "按表单主体名称或税号核对票面购买方；主体缺值、歧义或名称与税号冲突时转人工确认。"),
    "AMOUNT": ("发票金额足额核对", "去重后的发票价税合计应覆盖申请金额。"),
    "DATE": ("开票日期检查", "开票日期不得晚于今天。"),
    "SEMANTIC": ("费用语义核对", "事由、费用类别与票据用途不存在明确矛盾。"),
    "ATTACHMENTS": ("发票原件完整性", "应提供本次申请的发票原件。"),
    "AMOUNT_VALID": ("申请金额有效性", "申请金额应大于零。"),
}
PAYMENT_DEFAULT_CONDITIONS = [
    ("payment_materials", "材料完备", "应有可完整读取的合同、报价单或其他能够说明付款依据的材料。没有发票本身不违规；没有附件或关键依据缺失返回 UNKNOWN。"),
    ("payment_company", "付款主体一致", "表单付款主体应与材料中的付款方或采购方一致；仅接受已提供的明确名称和事实，无法确认返回 UNKNOWN。"),
    ("payment_account", "收款信息一致", "表单收款人及账户应与材料中的收款约定一致。材料未写明收款账户或存在代收安排但依据不足时返回 UNKNOWN，不猜测账户。"),
    ("payment_amount", "付款金额有依据", "申请金额应大于零，并有合同、报价或付款约定支持。允许预付款或部分付款，但需材料明确支持本次金额；不要求发票合计覆盖付款金额。"),
    ("payment_purpose", "用途与类别合理", "付款事由、费用类别及材料描述应基本一致，不存在明确的个人消费或用途矛盾；不因事由简短自行提高风险。"),
]


def configurable(rules: dict | None) -> bool:
    """缺少结构版本的旧模板维持原行为，不能因读取或切换模式而自动升级。"""
    return (rules or {}).get("policy_schema_version", 1) in {2, 3}


def explicit_rules(rules: dict | None) -> bool:
    return (rules or {}).get("policy_schema_version") == 3


def default_rule_entries(business_type="REIMBURSEMENT") -> list[dict]:
    """仅创建模板时使用默认值；读取、切换模式和保存空列表不能调用此函数补值。"""
    if business_type == "PAYMENT":
        return [{"id": key, "kind": "CUSTOM", "builtin_code": "", "name": name, "description": description,
                 "enabled": True, "on_failure": "REVIEW", "parameters": {}} for key, name, description in PAYMENT_DEFAULT_CONDITIONS]
    return [{"id": f"builtin_{code.lower()}", "kind": "BUILTIN", "builtin_code": code,
             "name": BUILTIN_LABELS[code][0], "description": BUILTIN_LABELS[code][1], "enabled": True,
             "on_failure": action, "parameters": {"require_completed": True} if code == "SEMANTIC" else {}}
            for code, action in BUSINESS_DEFAULT_ACTIONS.items()]


def has_approval_conditions(rules: dict) -> bool:
    """提示项不属于同意条件；空条件列表必须明确交由人工判断。"""
    return any(item.get("enabled", True) and item.get("on_failure") != "WARN" for item in rules.get("entries", []))


def default_builtin_rules() -> dict:
    return {code: {"enabled": True, "on_failure": action} for code, action in BUILTIN_DEFAULT_ACTIONS.items()}


def builtin_policy(rules: dict, code: str) -> dict:
    policy = {"enabled": True, "on_failure": {**BUILTIN_DEFAULT_ACTIONS, **BUSINESS_DEFAULT_ACTIONS}[code]}
    if explicit_rules(rules) and code != "DUPLICATE":
        entry = next((rule for rule in rules.get("entries", []) if rule.get("kind") == "BUILTIN" and rule.get("builtin_code") == code), None)
        policy.update(entry or {"enabled": False})
    else:
        policy.update((rules.get("builtin_rules") or {}).get(code, {}))
    # 即使数据库中留有非法旧值，也不能把唯一占用约束降级为提示或关闭。
    if code == "DUPLICATE":
        policy["enabled"] = True
        if policy["on_failure"] not in {"REJECT", "REVIEW"}:
            policy["on_failure"] = "REVIEW"
    return policy


def enabled_custom_rules(rules: dict | None) -> list[dict]:
    if not configurable(rules):
        return []
    source = [rule for rule in rules.get("entries", []) if rule.get("kind") == "CUSTOM"] if explicit_rules(rules) else rules.get("custom_rules", [])
    return [deepcopy(rule) for rule in source if rule.get("enabled", True)]


def rule_evidence(rules: dict, code: str, title: str, passed: bool, detail: str,
                  severity="REVIEW", related=None, *, reliable=True, result=None, custom=None,
                  user_message: str | None = None) -> dict:
    """把事实与策略分开保存；证据不足统一转复核，绝不把识别失败当作明确违规。

    系统项保留自身强制级别；重复占票即使选择人工处理也保持 BLOCK。旧模板仍使用
    原先的金额比较、严重程度与语义复核行为，只有结构版本 2 才应用可配置策略。
    """
    state = result or ("PASS" if passed else "FAIL")
    business_codes = BUSINESS_DEFAULT_ACTIONS if explicit_rules(rules) else BUILTIN_DEFAULT_ACTIONS
    source = "CUSTOM" if custom else "BUILTIN" if code in business_codes else "SYSTEM"
    action, eligible = None, False
    if configurable(rules) and (custom or code in business_codes or code == "DUPLICATE"):
        policy = custom or builtin_policy(rules, code)
        action = policy["on_failure"]
        if policy.get("name"):
            title = policy["name"]
        if not policy.get("enabled", True) or state == "SKIPPED":
            state, passed, severity = "SKIPPED", True, "INFO"
            detail = "标准已停用或不适用；" + detail
        elif state == "UNKNOWN" or (not passed and not reliable):
            state, passed, severity = "UNKNOWN", False, "BLOCK" if code == "DUPLICATE" else "INFO" if action == "WARN" and explicit_rules(rules) else "REVIEW"
            detail += "；依据尚未完整确认，请人工核对"
        elif not passed:
            eligible = action == "REJECT"
            severity = "BLOCK" if eligible or code == "DUPLICATE" else "REVIEW" if action == "REVIEW" else "INFO"
    evidence = {"code": code, "title": title, "passed": passed, "detail": detail,
                "severity": "INFO" if passed else severity, "related_approval": related,
                "source": source, "result": state, "on_failure": action,
                "auto_reject_eligible": eligible, "reviewed": False}
    if user_message and state == "FAIL":
        evidence["user_message"] = user_message
    evidence["reviewable"] = (not passed and evidence["severity"] == "REVIEW"
        and (code == "SEMANTIC" or source == "CUSTOM" or explicit_rules(rules) and source == "BUILTIN")
        and (action != "REJECT" or state == "UNKNOWN"))
    return evidence


def no_conditions_evidence() -> dict:
    """无条件不会成为自动通过；人工确认该项后只允许财务手动同意。"""
    return {"code": "NO_CONDITIONS", "title": "无启用的同意条件，需人工审核", "passed": False,
            "detail": "当前模板未配置启用的同意条件，请财务核对原件及业务情况并填写处理意见。", "severity": "REVIEW",
            "source": "SYSTEM", "result": "UNKNOWN", "on_failure": "REVIEW", "related_approval": None,
            "auto_reject_eligible": False, "reviewable": True, "reviewed": False}


def reviewable(item: dict) -> bool:
    """兼容旧语义审核记录；拒绝项、系统项及已确认项均不允许人工覆盖。"""
    return (not item["passed"] and item["severity"] == "REVIEW"
            and item.get("reviewable", item["code"] == "SEMANTIC"))


def apply_evidence_review(evidence: list[dict], codes: list[str]) -> None:
    for item in evidence:
        if item["code"] in codes and reviewable(item):
            item.update(passed=True, severity="INFO", reviewed=True, reviewable=False, auto_reject_eligible=False)


def rejection_items(evidence: list[dict]) -> list[dict]:
    """仅明确失败且证据可靠的拒绝标准可生成自动拒绝操作。"""
    return [item for item in evidence if not item["passed"] and item.get("result") == "FAIL"
            and item.get("on_failure") == "REJECT" and item.get("auto_reject_eligible")]
