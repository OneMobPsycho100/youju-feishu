"""持久化原件：文件内容不可覆盖，临时 URL 不作为长期引用。"""
import hashlib
import os
import re
import tempfile
from pathlib import Path
from .config import settings
from .file_types import detect_file_type


def safe_name(value: str) -> str:
    """生成可落盘的名称，长中文名称按字节截断并保留常见后缀。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value).strip(" .") or "file"
    if len(name) <= 180 and len(name.encode("utf-8")) <= 240:
        return name
    stem, suffix = os.path.splitext(name)
    if len(suffix) > 16 or len(suffix.encode("utf-8")) > 32:
        stem, suffix = name, ""
    prefix = stem[:180 - len(suffix)].encode("utf-8")[:240 - len(suffix.encode("utf-8"))]
    return (prefix.decode("utf-8", errors="ignore").rstrip(" .") or "file") + suffix


def resolve(key: str) -> Path:
    root = settings().storage_root.resolve()
    target = (root / key).resolve()
    if target != root and root not in target.parents:
        raise ValueError("存储路径越界")
    return target


def save_original(data: bytes, filename: str) -> dict:
    if len(data) > settings().max_file_mb * 1024 * 1024:
        raise ValueError("附件超过允许的大小")
    digest = hashlib.sha256(data).hexdigest()
    filename = safe_name(filename)
    suffix, mime = detect_file_type(data, filename)
    if suffix and Path(filename).suffix.lower() != suffix:
        filename = str(Path(filename).with_suffix(suffix))
    key = f"originals/{digest[:2]}/{digest}/{filename}"
    path = resolve(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        # 先完整写临时文件，再原子发布硬链接。并发读取不会拿到写到一半的原件。
        temporary = tempfile.NamedTemporaryFile(dir=path.parent, delete=False)
        try:
            with temporary:
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.link(temporary.name, path)
        except FileExistsError:
            pass
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    return {"filename": filename, "storage_key": key, "sha256": digest,
            "size": len(data), "mime": mime}
