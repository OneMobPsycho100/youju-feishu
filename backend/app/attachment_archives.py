"""压缩包原件与成员共用不可变存储，展开清单在审批同步的短事务内提交。"""
from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tempfile
import time

from .config import settings
from .file_types import ARCHIVE_TYPES
from .models import Attachment, uid
from .storage import resolve, save_original


MAX_MEMBERS = 100
MAX_BYTES = 100 * 1024 * 1024
EXPANSION_VERSION = 1
EXTRACTABLE = {ARCHIVE_TYPES[suffix]: suffix for suffix in (".zip", ".7z")}
TOO_LARGE = "压缩包解开后超过文件数量或大小限制，请拆分材料"


@dataclass
class Expansion:
    """解包结果。REFERENCE 用于仅补齐历史资料，AUDIT 成员参与正常业务审核。"""

    mode: str = "AUDIT"
    error: str = ""
    members: list[tuple[str, dict]] = field(default_factory=list)


def archive_state(attachment: Attachment) -> dict:
    return (attachment.parsed_data or {}).get("archive") or {}


def archive_members(attachments: list[Attachment]) -> dict[str, tuple[Attachment, str]]:
    """由原包清单反查成员来源。路径来自安全校验后的清单，用于展示与稳定标识。"""
    return {member["attachment_id"]: (attachment, member["path"])
            for attachment in attachments for member in archive_state(attachment).get("members", [])}


def archive_versions(attachments, *, for_audit=False) -> dict:
    """分开比较原包展示与审核输入，补齐历史预览时不使历史审核失效。"""
    result = {}
    for attachment in attachments:
        state = archive_state(attachment)
        if attachment.active and state and (not for_audit or state["mode"] == "AUDIT"):
            result[attachment.id] = (state, "" if for_audit else attachment.filename)
    return result


def has_unread_archive(attachments) -> bool:
    return any(state.get("mode") == "AUDIT" and state["status"] == "FAILED"
               for attachment in attachments if attachment.active and (state := archive_state(attachment)))


def archive_readonly(db, attachment: Attachment) -> bool:
    """原包与仅供查阅的成员可下载，票面及材料确认只适用于正常展开的成员。"""
    if archive_state(attachment):
        return True
    if not attachment.source_key.startswith("bundle:"):
        return False
    parent = db.get(Attachment, attachment.source_key.split(":")[1])
    state = archive_state(parent) if parent and parent.approval_id == attachment.approval_id and parent.active else {}
    return state.get("mode") != "AUDIT" or state.get("status") != "READY"


def attachment_key(descriptor: dict, saved: dict) -> str:
    """兼容原有飞书来源键的长度边界，文件名校正不会改变已有来源。"""
    return str(descriptor.get("key", saved["filename"]))[:200]


def stored_original(attachment: Attachment) -> dict:
    return {name: getattr(attachment, name) for name in ("filename", "storage_key", "sha256", "size", "mime")}


def archive_cache(attachments: list[Attachment]) -> dict[tuple[str, str], Expansion]:
    """提取普通字典以便在事务外复用已完整落盘的同版本成员。历史用途随原包哈希保留。"""
    by_id = {item.id: item for item in attachments}
    result = {}
    for item in attachments:
        state = archive_state(item)
        if not state:
            continue
        members = state.get("members", [])
        cached = Expansion(mode=state["mode"])
        if (state["status"] == "READY" and state.get("version") == EXPANSION_VERSION
                and all(member["attachment_id"] in by_id for member in members)):
            cached.members = [(member["path"], stored_original(by_id[member["attachment_id"]])) for member in members]
        result[item.source_key, item.sha256] = cached
    return result


class ExpansionBudget:
    """单审批共享展开数量、实际字节和耗时预算。每次处理一个包，避免占满工作进程。"""

    def __init__(self):
        self.count = MAX_MEMBERS
        self.total = MAX_BYTES
        self.seconds = 60.0
        self.single = settings().max_file_mb * 1024 * 1024

    def _consume(self, expansion: Expansion) -> Expansion:
        self.count -= len(expansion.members)
        self.total -= sum(saved["size"] for _, saved in expansion.members)
        return expansion

    def prepare(self, saved: dict, previous: Expansion | None = None, *, mode="AUDIT") -> Expansion:
        """只在完整解包后保存成员，格式问题以可读原因返回，存储故障交由任务重试。"""
        if previous:
            mode = previous.mode
        suffix = EXTRACTABLE.get(saved["mime"])
        if not suffix:
            return Expansion(mode, "此压缩格式暂不支持自动解包，请改传 ZIP 或 7z")
        if self.count <= 0 or self.total <= 0:
            return Expansion(mode, TOO_LARGE)
        if previous and previous.members:
            members = previous.members
            if (len(members) > self.count or sum(item["size"] for _, item in members) > self.total
                    or any(item["size"] > self.single for _, item in members)):
                return Expansion(mode, TOO_LARGE)
            if all(resolve(item["storage_key"]).is_file() for _, item in members):
                return self._consume(previous)
        if self.seconds <= 0:
            return Expansion(mode, "本单解包处理超时，请拆分压缩包")
        source = resolve(saved["storage_key"])
        with tempfile.TemporaryDirectory(prefix="youju-unpack-") as temporary:
            started = time.monotonic()
            try:
                output = subprocess.run(
                    [sys.executable, "-B", "-m", "app.archive_extract", str(source), temporary,
                     suffix, str(self.count), str(self.total), str(self.single)],
                    check=True, stdout=subprocess.PIPE, timeout=min(30, self.seconds),
                )
            except subprocess.TimeoutExpired:
                return Expansion(mode, "压缩包解包超时，请拆分后重新上传")
            finally:
                self.seconds -= time.monotonic() - started
            result = json.loads(output.stdout)
            if result.get("error"):
                return Expansion(mode, result["error"])
            members = []
            for index, path in enumerate(result["paths"]):
                data = (Path(temporary) / str(index)).read_bytes()
                members.append((path, save_original(data, PurePosixPath(path).name)))
            return self._consume(Expansion(mode, members=members))


def upsert_original(db, originals: dict, approval_id: str, key: str, saved: dict) -> Attachment:
    """按来源和内容复用附件 ID，文件名、MIME 等可随实际格式校正。"""
    identity = (key, saved["sha256"])
    attachment = originals.get(identity)
    if attachment is None:
        attachment = Attachment(id=uid(), approval_id=approval_id, source_key=key, **saved)
        db.add(attachment)
        originals[identity] = attachment
    else:
        attachment.filename, attachment.storage_key = saved["filename"], saved["storage_key"]
        attachment.mime, attachment.size = saved["mime"], saved["size"]
    attachment.active = True
    return attachment


def apply_expansion(db, originals: dict, parent: Attachment, expansion: Expansion) -> None:
    """完整发布成员清单。失败保留同一原包先前取得的成员和关联，交由人工核对。"""
    previous = parent.parsed_data or {}
    state = archive_state(parent)
    members = []
    if expansion.error:
        members = state.get("members", [])
        member_ids = {item["attachment_id"] for item in members}
        for item in originals.values():
            if item.id in member_ids:
                item.active = True
    else:
        for path, saved in expansion.members:
            key = f"bundle:{parent.id}:{sha256(path.encode('utf-8')).hexdigest()}"
            member = upsert_original(db, originals, parent.approval_id, key, saved)
            members.append({"attachment_id": member.id, "path": path})
    parent.parsed_data = {
        "material_type": "OTHER",
        "parse_status": "FAILED" if expansion.error else "SKIPPED",
        "file_type": Path(parent.filename).suffix.lower(),
        "error": expansion.error,
        "archive": {"status": "FAILED" if expansion.error else "READY", "mode": expansion.mode,
                    "version": EXPANSION_VERSION, "error": expansion.error, "members": members},
    }
    legacy_parse = previous.get("previous_invoice_parse") if state else previous
    if legacy_parse:
        parent.parsed_data = {**parent.parsed_data, "previous_invoice_parse": legacy_parse}
