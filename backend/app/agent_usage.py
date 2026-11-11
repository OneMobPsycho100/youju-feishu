"""将 Harness 已提交用量投影到原生界面；采用 Token Meter 的分桶和同次尝试替换规则。"""
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from .models import AgentConversation, AgentRun, AICall, now

METER_PLUGIN = "@deepseek-ai/dsh-token-meter"
MAX_SAFE = 2**53 - 1


def number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE else None


def normalize_usage(usage):
    """Harness 的 inputTokens 不含缓存读取/写入；对外 inputTokens 统一为完整计费输入。

    优先使用供应商 totalTokens - outputTokens，避免把已独立分桶的缓存重复计算。
    reasoningTokens 属于输出的一部分，绝不能再加到总量。
    """
    if not isinstance(usage, dict):
        return {}
    if usage.get("meterVersion") == 1:
        return {key: value for key, value in usage.items() if number(value) is not None}
    raw = {key: value for key, value in usage.items() if number(value) is not None}
    if "inputTokens" not in raw or "outputTokens" not in raw:
        return {}
    uncached, output = raw["inputTokens"], raw["outputTokens"]
    lower_input = uncached + raw.get("cacheReadTokens", 0) + raw.get("cacheWriteTokens", 0)
    total = raw.get("totalTokens")
    if total is not None and total < lower_input + output:
        return {}  # 不接受彼此矛盾的供应商用量。
    billed_input = total-output if total is not None else lower_input
    result = {"meterVersion": 1, "inputTokens": billed_input, "uncachedInputTokens": uncached,
              "outputTokens": output, "totalTokens": billed_input+output}
    for key in ("cacheReadTokens", "cacheWriteTokens", "reasoningTokens"):
        if key in raw:
            result[key] = raw[key]
    return result


class UsageLedger:
    """同一尝试的最终样本覆盖临时样本；新步骤和重试形成新计费尝试，事件重放不重复累计。"""
    def __init__(self):
        self.seen = set()
        self.attempt = 0
        self.samples = {}
        self.attempts = set()

    def consume(self, event):
        seq = event.get("seq")
        if not isinstance(seq, int) or seq in self.seen:
            return
        self.seen.add(seq)
        kind, data = event.get("type"), event.get("data") or {}
        if kind in {"step/start", "llm/retry-started"}:
            self.attempt += 1
        if kind == "request/header":
            self.attempts.add(self.attempt)
        if kind in {"assistant/message", "assistant/attempt"}:
            sample = normalize_usage(data.get("usage"))
            if not sample:
                # 被中断的回复有时只在 compact stream 中留下最后的用量样本。
                for part in data.get("stream", []):
                    if isinstance(part, dict) and isinstance(part.get("usage"), dict):
                        sample = normalize_usage(part["usage"]) or sample
            self.attempts.add(self.attempt)
            if sample:
                self.samples[self.attempt] = sample

    def snapshot(self):
        values = list(self.samples.values())
        result = {"meterVersion": 1, "reportedCalls": len(values), "missingCalls": len(self.attempts-set(self.samples))}
        if not values:
            return result
        for key in ("inputTokens", "uncachedInputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens"):
            if all(key in value for value in values):
                result[key] = sum(value[key] for value in values)
        return result


def usage_summary(db, user, days, all_users=False):
    """本人统计只读本人会话；管理员全局统计只返回聚合数字，不返回他人正文或会话 ID。"""
    cutoff = now()+timedelta(hours=8)-timedelta(days=days-1)
    cutoff = cutoff.replace(hour=0, minute=0, second=0, microsecond=0)-timedelta(hours=8)
    statement = select(AgentRun).where(AgentRun.started_at >= cutoff)
    if not all_users:
        statement = statement.where(AgentRun.owner_id == user.id)
    entries = [(row.started_at, row.model_name, "业务对话", row.usage, row.status) for row in db.scalars(statement)]
    call_statement = select(AICall).where(AICall.started_at >= cutoff, AICall.status != "SKIPPED")
    if not all_users:
        call_statement = call_statement.join(AgentConversation, AICall.agent_conversation_id == AgentConversation.id).where(
            AgentConversation.owner_id == user.id)
    labels = {"AUDIT": "审批审核", "CONNECTION_TEST": "连接测试", "INVOICE_EXTRACTION": "发票识别",
              "PAYMENT_MATERIAL": "付款材料读取", "AGENT_ATTACHMENT": "会话附件解析"}
    entries.extend((row.started_at, row.model_name, labels.get(row.source, row.source), row.usage, row.status)
                   for row in db.scalars(call_statement))
    totals, daily, models, sources = defaultdict(int), {}, {}, {}
    reported, missing = 0, 0
    fields = ("inputTokens", "outputTokens", "totalTokens", "uncachedInputTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens")
    coverage = defaultdict(int)
    for timestamp, model_name, source, raw, status in entries:
        usage = normalize_usage(raw)
        valid = "totalTokens" in usage
        reported += int(valid)
        missing += int(not valid or bool(raw.get("missingCalls")))
        day = (timestamp+timedelta(hours=8)).date().isoformat()
        day_row = daily.setdefault(day, {"date": day, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "calls": 0})
        model_row = models.setdefault(model_name, {"model": model_name, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0, "calls": 0})
        source_row = sources.setdefault(source, {"source": source, "totalTokens": 0, "calls": 0})
        day_row["calls"] += 1
        model_row["calls"] += 1
        source_row["calls"] += 1
        for key in fields:
            if key in usage:
                totals[key] += usage[key]
                coverage[key] += 1
                for bucket in (day_row, model_row, source_row):
                    if key in bucket:
                        bucket[key] += usage[key]
    # 缓存统计只在相同记录同时有完整输入和缓存读取时计算，不拿不完整样本伪造命中率。
    paired = [normalize_usage(item[3]) for item in entries]
    paired = [item for item in paired if "cacheReadTokens" in item and "inputTokens" in item]
    cache_input = sum(item["inputTokens"] for item in paired)
    return {"plugin": METER_PLUGIN, "timezone": "Asia/Shanghai", "days": days, "scope": "all" if all_users else "mine",
        "calls": len(entries), "reported": reported, "missing": missing, "totals": dict(totals), "coverage": dict(coverage),
        "cache_hit_rate": sum(item["cacheReadTokens"] for item in paired)/cache_input if cache_input else None,
        "daily": [daily[key] for key in sorted(daily)],
        "models": sorted(models.values(), key=lambda item: -item["totalTokens"]), "sources": list(sources.values())}


def attachment_usage(db, conversation_id):
    """按会话聚合识别调用，包含失败重试的已报告费用；不重复计入 AgentRun。"""
    usages = [normalize_usage(row.usage) for row in db.scalars(select(AICall).where(
        AICall.agent_conversation_id == conversation_id, AICall.status != "SKIPPED"))]
    result = {"reportedCalls": sum(bool(value) for value in usages), "missingCalls": sum(not value for value in usages)}
    for key in ("inputTokens", "outputTokens", "totalTokens", "cacheReadTokens", "cacheWriteTokens", "reasoningTokens", "uncachedInputTokens"):
        reported = [value[key] for value in usages if key in value]
        if reported:
            result[key] = sum(reported)
    return result
