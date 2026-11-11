"""适配层与配置层共用的可展示错误，避免配置读取和供应商模块循环依赖。"""


class DomainError(ValueError):
    """可向用户展示的业务错误，不包含令牌、密钥或外部请求详情。"""


class StaleSnapshotError(RuntimeError):
    """耗时工作期间快照已变化，后台可重试，HTTP 调用方收到版本冲突。"""
