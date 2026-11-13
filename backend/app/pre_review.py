"""模板识别和实际回写共用的飞书待办匹配。"""
from collections.abc import Collection
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Approval, ApprovalTemplate


def matches_pre_review_node(task: dict, node_ids: Collection[str]) -> bool:
    """按飞书原始 ID 或自定义 ID 匹配，节点名称只用于展示。"""
    return task.get("node_id") in node_ids or task.get("custom_node_id") in node_ids


def task_open_id(task: dict) -> str:
    """部分实例把 open_id 放在 user_id 字段，普通 user_id 不可用于回写。"""
    user_id = task.get("user_id") or ""
    return task.get("open_id") or (user_id if user_id.startswith("ou_") else "")


def select_pre_review_task(tasks: list[dict], node_ids: list[str], reviewer_open_id: str) -> tuple[dict, str]:
    """返回唯一可处理待办。没有当前待办时可展示历史状态，多个可处理待办由人工判断。"""
    scope = set(node_ids)
    matched = [task for task in tasks if matches_pre_review_node(task, scope)]
    pending = [task for task in matched if task.get("status") == "PENDING"]
    eligible = {str(task["id"]): task for task in pending
                if task.get("id") and reviewer_open_id and task_open_id(task) == reviewer_open_id}
    if len(eligible) > 1:
        return {}, "AMBIGUOUS"
    if eligible:
        return next(iter(eligible.values())), "PENDING"
    if pending or not matched:
        return {}, "MISSING"
    latest = matched[-1]
    return latest, latest.get("status") or "MISSING"


def has_pending_pre_review_task(approval: "Approval | None", template: "ApprovalTemplate | None") -> bool:
    """判断同步后是否持有唯一专用待办，不包含 AI 开关或付款处理策略。

    审批的待办字段由 select_pre_review_task 写入，已经校验节点范围及唯一性。
    不再用 node_id 直接比较配置，以兼容配置 custom_node_id、实例返回原始 node_id 的情况。
    """
    return bool(approval and template and template.active and template.node_ids and template.reviewer_open_id
                and approval.status == "PENDING" and approval.task_status == "PENDING" and approval.task_id
                and approval.assignee == template.reviewer_open_id)


def has_completed_pre_review_task(tasks: list[dict], node_ids: list[str], reviewer_open_id: str) -> bool:
    """从权威实例快照确认专用审批人已到过节点，仅用于恢复付款采集资格。"""
    return bool(reviewer_open_id and any(
        task.get("id") and task.get("status") in {"APPROVED", "DONE"}
        and matches_pre_review_node(task, node_ids) and task_open_id(task) == reviewer_open_id
        for task in tasks))
