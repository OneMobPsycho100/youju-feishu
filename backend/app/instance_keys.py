"""实例密钥持久化；API 与事件服务共享配置卷，首次安装无需手工填写密钥。"""
import os
from pathlib import Path
import secrets
import time


def instance_secret(root: Path, supplied: str) -> str:
    """原部署密钥优先并持久化；文件独占创建，其他进程等待完整写入后再读取。"""
    root.mkdir(parents=True, exist_ok=True)
    path = root / "instance.key"
    provided = supplied if supplied and not supplied.startswith("demo-") else ""
    if provided and len(provided) < 32:
        raise RuntimeError("SESSION_SECRET 至少需要 32 位；也可留空由实例自动生成")
    proposed = provided or secrets.token_urlsafe(48)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(60):
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                if provided and not secrets.compare_digest(value, provided):
                    raise RuntimeError("SESSION_SECRET 与持久化实例密钥不一致，请恢复原密钥，勿直接轮换")
                return value
            time.sleep(0.05)
        raise RuntimeError("实例密钥文件不完整，请检查持久化配置卷")
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(proposed)
        target.flush()
        os.fsync(target.fileno())
    return proposed
