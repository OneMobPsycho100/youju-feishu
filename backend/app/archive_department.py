"""打印用申请人组织部门；来源固定为审批实例，不采用表单业务部门。"""
from dataclasses import dataclass
from typing import Literal

from .errors import DomainError


class DepartmentSyncUnavailable(Exception):
    """部门服务暂时不可用；由现有任务队列按外部服务异常退避重试。"""


@dataclass(frozen=True)
class ArchiveDepartment:
    """部门读取结果；只有实例明确没有部门时才能标记为 NOT_SET。

    department_id 是审批实例保存的部门标识；name 为其查询名称。
    FAILED 的 error 只允许包含可公开的诊断信息，retryable 区分网络故障与授权问题。
    """
    department_id: str
    name: str = ""
    status: Literal["READY", "NOT_SET", "FAILED"] = "FAILED"
    error_code: int | None = None
    error: str = ""
    retryable: bool = False

    def snapshot(self) -> dict:
        """归档 JSON 的部门同步信息；名称继续使用既有 applicant_department 字段。"""
        return {"department_id": self.department_id, "status": self.status,
                "error_code": self.error_code, "error": self.error}

    def ensure_complete(self) -> None:
        """必须在其他打印资料提交后调用，避免部门失败导致附件和审批轨迹丢失。"""
        if self.status == "FAILED":
            if self.retryable:
                raise DepartmentSyncUnavailable(self.error)
            raise DomainError(self.error)


def department_pending(snapshot: dict) -> bool:
    """兼容旧归档：旧快照没有名称时待补齐；新快照明确未设置部门时无需重试。"""
    status = (snapshot.get("applicant_department_sync") or {}).get("status")
    if status == "NOT_SET":
        return False
    return status == "FAILED" or not str(snapshot.get("applicant_department") or "").strip()


def department_label(snapshot: dict) -> str:
    """打印页眉统一显示状态，查询失败时不把旧名称误当成本次同步成功。"""
    if department_pending(snapshot):
        return "部门未同步"
    if (snapshot.get("applicant_department_sync") or {}).get("status") == "NOT_SET":
        return "未设置所属部门"
    return snapshot["applicant_department"]
