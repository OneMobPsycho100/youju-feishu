"""模板识别和实际回写共用的飞书待办匹配。"""
from collections.abc import Collection


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
