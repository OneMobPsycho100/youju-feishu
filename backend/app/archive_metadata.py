"""审批归档补齐。只保存飞书打印资料和原件索引，不进入预审或审批回写。"""
from collections import defaultdict
from datetime import datetime, timedelta
from hashlib import sha256
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, unquote, urlsplit

from sqlalchemy import and_, func, or_, select, update

from .archive_department import ArchiveDepartment
from .db import begin_write
from .company_subjects import read_company_declaration
from .errors import DomainError
from .feishu_form import attachment_name, attachment_names, attachment_values
from .integrations import FeishuGateway, gateway
from .invoice_recognition import normalized_candidate
from .models import Approval, ApprovalTemplate, Attachment, Company, Job, User
from .payment_service import decoded, display
from .reimbursement_metadata import feishu_time, snapshot_metadata
from .storage import resolve, safe_name, save_original
from .template_transfer import ATTACHMENT_TYPES, GROUP_TYPES


ARCHIVE_SNAPSHOT_VERSION = 2
ARCHIVE_JOB_PREFIX = f"archive-metadata-v{ARCHIVE_SNAPSHOT_VERSION}:"


def _digest(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def _form(snapshot: dict) -> list:
    form = decoded(snapshot.get("form"))
    return form if isinstance(form, list) else []


def _url_id(url: str, *, reference: bool = False) -> str:
    """在本次飞书响应内用下载码区分原件。跨次同步只比较稳定标识。"""
    try:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return ""
        identity = parsed.netloc.lower() + parsed.path
        keys = {"fileid", "filetoken", "filekey", "drivetoken", "fstoken", "objectkey", "objectid", "key", "id"}
        query = parse_qsl(parsed.query)
        object_ids = sorted((key, value) for key, value in query
                            if key.lower().replace("_", "") in keys)
        if reference and not object_ids:
            # 原生审批可为多个附件返回同一路径，短效 code 在每次读取时重新签发。
            object_ids = sorted((key, value) for key, value in query if key.lower() in {"code", "token"})
        if object_ids:
            identity += ":" + _digest(object_ids)
        return identity
    except ValueError:
        return ""


def archive_sources(snapshot: dict, mapped_ids: list[str]) -> list[dict]:
    """枚举整单原件，保留表单、动态、评论顺序。相同文件引用只列一次。"""
    if "demo_files" in snapshot:
        return [{**item, "id": _digest(["demo", item["key"]])[:32], "origin": "FORM"}
                for item in snapshot["demo_files"]]

    mapped_ids = {mapped_ids} if isinstance(mapped_ids, str) else set(mapped_ids)
    sources, seen = [], set()

    def add(value, ext, origin, slot):
        items = attachment_values(value)
        names = attachment_names(ext, len(items))
        for index, (item, name) in enumerate(zip(items, names)):
            item = decoded(item)
            record = item if isinstance(item, dict) else {}
            url = str(record.get("url") or record.get("file_url") or record.get("download_url")
                      or (item if isinstance(item, str) else ""))
            path, reference = _url_id(url), _url_id(url, reference=True)
            token = str(record.get("file_token") or record.get("token") or "")
            keys = {key for key in (reference, "token:" + token if token else "") if key}
            if keys & seen:
                continue
            seen.update(keys)
            filename = attachment_name(record) or name
            if not filename:
                leaf = unquote(urlsplit(url).path.rsplit("/", 1)[-1]) if path else ""
                filename = leaf if "." in leaf else f"附件-{len(sources) + 1}"
            # 短效码只用于本次关联，稳定的表单位置标识用于发现增删和重试复用。
            identity = token or (path if path == reference and path else [path, slot, index])
            sources.append({"id": _digest(identity)[:32], "filename": safe_name(filename),
                            "origin": origin, "url": url if path else "",
                            "identity": path, "reference": reference, "file_size": record.get("file_size")})

    def visit(items, path=(), depth=0):
        if depth > 24 or not isinstance(items, list):
            return
        for index, item in enumerate(items):
            slot = (*path, index)
            if isinstance(item, list):
                visit(item, slot, depth + 1)
            elif isinstance(item, dict):
                kind, field = str(item.get("type") or "").lower(), str(item.get("id") or "")
                if kind in ATTACHMENT_TYPES or field in mapped_ids:
                    add(item.get("value"), item.get("ext"), "FORM", (*slot, field))
                elif kind in GROUP_TYPES or isinstance(item.get("children"), list):
                    visit(decoded(item.get("value")), (*slot, field), depth + 1)
                    visit(item.get("children"), (*slot, "children"), depth + 1)

    visit(_form(snapshot))
    for key, origin in (("timeline", "TIMELINE"), ("comment_list", "COMMENT")):
        for index, event in enumerate(snapshot.get(key) or []):
            if isinstance(event, dict):
                for kind in ("files", "attachments"):
                    add(event.get(kind), None, origin, (key, index, kind))
                extra = decoded(event.get("ext"))
                if isinstance(extra, dict):
                    add(extra.get("files") or extra.get("attachments"), None, origin, (key, index, "ext"))
    # 兼容旧附件识别器可读取的结构；附加到已确认来源之后，仍按文件标识去重。
    from .services import attachment_descriptors
    try:
        legacy = attachment_descriptors(snapshot, [])
    except DomainError:
        legacy = []
    for index, item in enumerate(legacy):
        add(item, None, "LOCAL", ("legacy", index))
    return sources


def _approval_end(snapshot: dict) -> datetime | None:
    """撤销快照的结束时间可能属于撤销动作，已有的最终通过时间继续保留。"""
    if snapshot.get("status") != "APPROVED" or snapshot.get("reverted") is True:
        return None
    end = feishu_time(snapshot.get("end_time"))
    start = feishu_time(snapshot.get("start_time"))
    return end if end and (not start or end >= start) else None


def _history_items(snapshot: dict, key: str) -> list[dict]:
    return [item for item in snapshot.get(key) or [] if isinstance(item, dict)]


def _revision(snapshot: dict, files: list[dict], attachment_version: str) -> str:
    # 只按稳定标识比较附件，飞书每次读取重新签发 URL 不应触发重复补齐。
    history = [[{key: value for key, value in item.items() if key not in {"files", "attachments", "ext"}}
                for item in _history_items(snapshot, group)] for group in ("task_list", "timeline", "comment_list")]
    return _digest([ARCHIVE_SNAPSHOT_VERSION, attachment_version, snapshot.get("start_time"), snapshot.get("end_time"),
                    snapshot.get("department_id"), snapshot.get("department_name"), history,
                    [(item["id"], item["origin"], item["filename"], item.get("identity"), item.get("file_size")) for item in files]])


def observe_archive(db, approval: Approval, snapshot: dict, enqueue) -> None:
    """同步终态时登记通过时间，并为发生变化的全单资料安排独立补齐。"""
    if approval.status != "APPROVED":
        return
    if end := _approval_end(snapshot):
        approval.approved_at = end
    template = db.get(ApprovalTemplate, approval.template_code)
    mapped_ids = (template.field_mapping or {}).get("attachment_ids") or []
    sources = archive_sources(snapshot, mapped_ids)
    revision = _revision(snapshot, sources, approval.attachment_version)
    old = approval.archive_snapshot or {}
    if old.get("observed_revision") != revision:
        approval.archive_snapshot = {**old, "observed_revision": revision}
        if old.get("revision") != revision:
            enqueue(db, "ARCHIVE_METADATA", {"instance_code": approval.id},
                    f"{ARCHIVE_JOB_PREFIX}{approval.id}:{approval.last_synced_at.isoformat()}")


def archived_approval_filter():
    """归档中心包含最终通过单据，以及已经通过后转为留痕的单据。"""
    return or_(Approval.status == "APPROVED", Approval.archive_status == "REVOKED")


def schedule_archive_metadata(db, enqueue) -> None:
    """分批补齐升级前的历史单据，失败任务可从已有任务中心重试。"""
    begin_write(db)
    prefix = ARCHIVE_JOB_PREFIX + Approval.id + ":"
    scheduled = select(Job.id).where(Job.kind == "ARCHIVE_METADATA",
        or_(func.substr(Job.dedupe_key, 1, func.length(prefix)) == prefix,
            and_(Job.status.in_(["PENDING", "RUNNING"]), Job.payload["instance_code"].as_string() == Approval.id))).exists()
    ids = db.scalars(select(Approval.id).where(archived_approval_filter(),
        or_(Approval.archive_snapshot["captured"].as_boolean().is_not(True),
            Approval.archive_snapshot["schema_version"].as_integer().is_not(ARCHIVE_SNAPSHOT_VERSION)), ~scheduled
    ).order_by(Approval.created_at, Approval.id).limit(20)).all()
    for instance_code in ids:
        enqueue(db, "ARCHIVE_METADATA", {"instance_code": instance_code}, f"{ARCHIVE_JOB_PREFIX}{instance_code}:history")
    db.commit()


def _actor(item: dict) -> str:
    open_id = str(item.get("open_id") or "")
    user_id = str(item.get("user_id") or "")
    return open_id or (user_id if user_id.startswith("ou_") else "user_id:" + user_id if user_id else "")


def _people(item: dict) -> list[str]:
    ids = [_actor(item)]
    ids.extend(str(value) for value in item.get("open_id_list") or [])
    if not item.get("open_id_list"):
        ids.extend(_actor({"user_id": value}) for value in item.get("user_id_list") or [])
    ids.extend(_actor(value) for value in item.get("cc_user_list") or [] if isinstance(value, dict))
    return list(dict.fromkeys(filter(None, ids)))


EVENT_LABELS = {
    "START": "发起审批", "PASS": "已通过", "APPROVED": "已通过", "AUTO_PASS": "自动通过",
    "REJECT": "已拒绝", "REJECTED": "已拒绝", "AUTO_REJECT": "自动拒绝",
    "CC": "抄送", "COPY": "抄送", "TRANSFER": "转交", "ROLLBACK": "退回",
    "CANCEL": "撤回", "REVERT": "撤销", "ADD_APPROVER_BEFORE": "前加签",
    "ADD_APPROVER_AFTER": "后加签", "COMMENT": "评论", "FINISH": "结束",
    "DONE": "已办理", "TRANSFERRED": "已转交",
}


def _opinion(value) -> str:
    value = decoded(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (display(item) for item in value)))
    if isinstance(value, dict):
        return display(value.get("content") or value)
    return ""


def _timeline(snapshot: dict, names: dict[str, str], applicant: str) -> tuple[list[dict], bool]:
    """合并飞书动态与任务结束事件，保留会签、抄送和独立意见。"""
    tasks = _history_items(snapshot, "task_list")
    by_id = {str(task.get("id")): task for task in tasks if task.get("id")}
    by_node = {str(task[key]): task.get("node_name") for task in tasks
               for key in ("node_id", "custom_node_id") if task.get(key)}
    events, covered, seen = [], set(), set()

    def add(node, actor, action, time_value, opinion):
        at = feishu_time(time_value)
        row = (str(node), str(actor), str(action), at.isoformat() if at else "", opinion)
        if row not in seen:
            seen.add(row)
            events.append(dict(zip(("node", "actor", "action", "at", "opinion"), row)))

    timeline = _history_items(snapshot, "timeline")
    for event in timeline:
        kind = str(event.get("type") or "").upper()
        task_id = str(event.get("task_id") or "")
        task = by_id.get(task_id, {})
        extra = decoded(event.get("ext"))
        node = task.get("node_name") or by_node.get(str(event.get("node_key") or ""))
        if not node and isinstance(extra, dict):
            node = extra.get("node_name")
        actor = str(event.get("user_name") or task.get("user_name") or names.get(_actor(event))
                    or names.get(_actor(task)) or ("系统" if kind.startswith("AUTO_") else "姓名未同步"))
        action = EVENT_LABELS.get(kind, kind or "审批动态")
        if kind in {"CC", "COPY"}:
            recipients = [names.get(item, "姓名未同步") for item in _people({**event, "open_id": "", "user_id": ""})]
            actor, node = "系统", node or "抄送"
            action = f"抄送 {len(recipients)} 人  {'、'.join(recipients)}" if recipients else "抄送"
        if kind == "START":
            node, actor = "发起", applicant
        if task_id and kind in {"PASS", "APPROVED", "AUTO_PASS", "REJECT", "REJECTED", "AUTO_REJECT"}:
            covered.add(task_id)
        add(node or "审批", actor, action, event.get("create_time") or task.get("end_time"), _opinion(event.get("comment")))
    if not any(event["action"] == "发起审批" for event in events) and snapshot.get("start_time"):
        add("发起", applicant, "发起审批", snapshot.get("start_time"), "")
    for task in tasks:
        state = str(task.get("status") or "").upper()
        if state in {"APPROVED", "REJECTED", "DONE", "TRANSFERRED"} and str(task.get("id")) not in covered:
            add(task.get("node_name") or "审批", names.get(_actor(task), "姓名未同步"),
                EVENT_LABELS[state],
                task.get("end_time"), "")
    for comment in _history_items(snapshot, "comment_list"):
        opinion = _opinion(comment.get("comment"))
        actor = names.get(_actor(comment), "姓名未同步")
        if opinion and not any(item["opinion"] == opinion and item["actor"] == actor for item in events):
            add("评论", actor, "评论", comment.get("create_time"), opinion)
    history_available = any(event["action"] not in {"发起审批", "评论"} for event in events)
    return sorted(events, key=lambda item: (not item["at"], item["at"])), history_available


def _read_names(db, remote, snapshot: dict) -> tuple[dict[str, str], str, ArchiveDepartment]:
    """补齐姓名和实例组织部门；部门读取失败返回状态，交由归档提交后报告任务失败。"""
    metadata = snapshot_metadata(snapshot)
    applicant_id = metadata.get("applicant_id", "")
    ids = {person for key in ("timeline", "task_list", "comment_list")
           for item in _history_items(snapshot, key) for person in _people(item)}
    if applicant_id:
        ids.add(applicant_id)
    names = {row.id: row.name for row in db.scalars(select(User).where(User.id.in_(ids)))
             if row.name and row.name != row.id} if ids else {}
    db.rollback()
    department_id = str(snapshot["department_id"]).strip() if snapshot.get("department_id") is not None else ""
    department_name = str(snapshot.get("department_name") or "").strip()
    if department_name:
        department = ArchiveDepartment(department_id, name=department_name, status="READY")
    elif not department_id:
        department = ArchiveDepartment(department_id, status="NOT_SET")
    else:
        department = ArchiveDepartment(department_id, error="审批实例所属部门名称尚未取得，请重新补齐打印资料。")
    applicant = str(snapshot.get("applicant_name") or "")
    if isinstance(remote, FeishuGateway):
        names.update(remote.archive_people(sorted(ids - names.keys())))
        if not department_name and department_id:
            department = remote.archive_department(department_id)
    return names, applicant or names.get(applicant_id, ""), department


def _related_approvals(db, values, remote, names) -> str:
    """仅补读原表单直接引用且现有应用可见的单据摘要。"""
    values = decoded(values)
    if not values:
        return ""
    items = values if isinstance(values, list) else [values]
    refs = []
    for item in items:
        record = item if isinstance(item, dict) else {}
        key = str(record.get("instance_code") or record.get("instance_id") or record.get("id")
                  or (item if isinstance(item, str) else ""))
        name = display(record) or str(record.get("approval_name") or record.get("title") or "")
        refs.append((key, name))
    labels = {"APPROVED": "已通过", "PENDING": "审批中", "REVERTED": "已撤销",
              "REJECTED": "已拒绝", "CANCELED": "已撤回", "DELETED": "已删除"}

    def summary(person, title, submitted, status):
        day = (submitted + timedelta(hours=8)).date().isoformat() if submitted else ""
        return " / ".join(filter(None, (person, title, day, labels.get(status, status))))

    local = {row.id: summary(names.get(row.applicant_id) or row.applicant, row.title, row.submitted_at, row.status)
             for row in db.scalars(select(Approval).where(Approval.id.in_([key for key, _ in refs])))}
    db.rollback()
    if isinstance(remote, FeishuGateway):
        lookup = list(dict.fromkeys(key for key, name in refs
            if key not in local and not name and re.fullmatch(r"[A-Za-z0-9_-]{20,100}", key)))
        for key in lookup[:5]:
            try:
                snapshot = remote.get_instance(key)
            except Exception:
                continue
            metadata = snapshot_metadata(snapshot)
            local[key] = summary(snapshot.get("applicant_name") or names.get(metadata.get("applicant_id")),
                snapshot.get("approval_name"), metadata.get("submitted_at"),
                "REVERTED" if snapshot.get("reverted") else snapshot.get("status"))
    return "\n".join(local.get(key) or name or key for key, name in refs if key or name)


def _declared_subjects(db, approval: Approval, mapping: dict) -> tuple[list[str], list[str]]:
    """无已归属原件时，列表可按唯一匹配的原申报主体查找整单。"""
    if approval.business_type == "PAYMENT":
        name = (approval.payment_details or {}).get("company_name")
        return [name] if name else [], []
    declaration = read_company_declaration(approval.raw_form, mapping)
    ids = set()
    if declaration.present and not declaration.issues:
        companies = list(db.scalars(select(Company).where(Company.active.is_(True))))
        matches = defaultdict(set)
        for company in companies:
            for name in (company.name, *company.aliases):
                matches[normalized_candidate("buyer_name", name)].add(company.id)
        matched_names = [matches[normalized_candidate("buyer_name", name)] for name in declaration.names]
        named_ids = set().union(*matched_names) if matched_names else set()
        tax_ids = {company.id for company in companies if company.tax_id in declaration.tax_ids}
        if (all(len(match) == 1 for match in matched_names) and len(tax_ids) == len(declaration.tax_ids)
                and not (tax_ids and named_ids and tax_ids != named_ids)):
            ids = tax_ids or named_ids
    return list(declaration.names), sorted(ids)


def _capture_files(remote, sources, audited, existing, previous, *, reuse_current):
    """优先引用已取得的同版本原件，只为未同步的来源保存补充文件。"""
    old = {item["id"]: item for item in previous.get("files", [])}
    by_key = defaultdict(list)
    for item in existing:
        by_key[item["source_key"]].append(item)
    limit_at = time.monotonic() + 150
    result = []
    for source in sources:
        entry = {key: source[key] for key in ("id", "filename", "origin")}
        key = source.get("key") or audited.get(source.get("reference"))
        candidates = by_key.get(str(key)[:200], [])
        previous_file = old.get(source["id"], {})
        candidate = candidates[0] if len(candidates) == 1 else None
        try:
            if reuse_current and candidate and resolve(candidate["storage_key"]).is_file():
                filename = source["filename"]
                if filename.startswith("附件-") or Path(filename).suffix.lower() != Path(candidate["filename"]).suffix.lower():
                    filename = candidate["filename"]
                entry.update(attachment_id=candidate["id"], filename=filename)
            elif (reuse_current and not previous_file.get("reason") and previous_file.get("storage_key")
                  and previous_file.get("source_version") == source.get("file_size")
                  and resolve(previous_file["storage_key"]).is_file()):
                entry.update({field: previous_file[field] for field in ("storage_key", "sha256", "size", "mime", "filename")})
            elif not source.get("url") and not source.get("demo_storage_key"):
                entry["reason"] = "飞书未返回可下载的附件地址"
            elif time.monotonic() > limit_at:
                entry["reason"] = "附件补齐达到单次处理时限，请重试同步打印资料"
            else:
                saved = save_original(remote.download(source), source["filename"])
                matched = next((item for item in candidates if item["sha256"] == saved["sha256"]
                                and resolve(item["storage_key"]).is_file()), None)
                if matched:
                    entry.update(attachment_id=matched["id"], filename=saved["filename"])
                else:
                    entry.update(saved)
        except (DomainError, ValueError, OSError):
            entry["reason"] = "原件无法下载或读取，请在飞书核对后重试"
        except Exception:
            entry["reason"] = "附件下载失败，请重试同步打印资料"
        result.append({**entry, "source_version": source.get("file_size")})
    return result


def backfill_archive(db, instance_code: str) -> dict:
    """重新读飞书并补齐打印资料。写入前核对版本，避免覆盖较新的归档快照。"""
    from .services import StaleSnapshotError, attachment_descriptors

    approval = db.get(Approval, instance_code)
    if not approval or not (approval.status == "APPROVED" or approval.archive_status == "REVOKED"):
        return {"skipped": True}
    previous = approval.archive_snapshot or {}
    version, attachment_version, status = approval.last_synced_at, approval.attachment_version, approval.status
    applicant_fallback = approval.applicant
    template = db.get(ApprovalTemplate, approval.template_code)
    code = approval.template_code
    mapping = (template.field_mapping or {}) if template else {}
    mapped = mapping.get("attachment_ids") or []
    declared_names, declared_ids = _declared_subjects(db, approval, mapping)
    related_values = (approval.payment_details or {}).get("related_approvals")
    existing = [{field: getattr(row, field) for field in ("id", "source_key", "storage_key", "sha256", "filename")}
                for row in db.scalars(select(Attachment).where(Attachment.approval_id == instance_code, Attachment.active.is_(True)))]
    db.rollback()
    remote = gateway(db)
    snapshot = remote.get_instance(instance_code)
    if snapshot.get("approval_code") and snapshot["approval_code"] != code:
        raise DomainError("飞书返回的审批模板与归档记录不一致")
    remote_approved = snapshot.get("status") == "APPROVED" and snapshot.get("reverted") is not True
    if (status == "APPROVED") != remote_approved:
        raise DomainError("飞书审批状态已变化，请先同步审批后重试")
    names, applicant, department = _read_names(db, remote, snapshot)
    related = _related_approvals(db, related_values, remote, names)
    if not applicant and not applicant_fallback.startswith(("ou_", "user_id:")) and applicant_fallback != snapshot.get("user_id"):
        applicant = applicant_fallback
    sources = archive_sources(snapshot, mapped)
    revision = _revision(snapshot, sources, attachment_version)
    if "demo_files" in snapshot:
        for task in _history_items(snapshot, "task_list"):
            if task.get("status") == "APPROVED":
                task["node_name"] = task.get("node_name") or "财务票据审核"
                task["end_time"] = task.get("end_time") or snapshot.get("end_time")
                names.setdefault(_actor(task), "演示审批人")
    try:
        audited = {_url_id(item.get("url", ""), reference=True): item["key"]
                   for item in attachment_descriptors(snapshot, mapped) if item.get("url")}
    except DomainError:
        audited = {}
    # 演示行的显示补齐不改变原始远端签名。正常同步已捕获的原件按本地版本复用。
    observed = previous.get("observed_revision")
    files = _capture_files(remote, sources, audited, existing, previous, reuse_current=(
        revision == observed or revision == previous.get("revision")))
    events, history_available = _timeline(snapshot, names, applicant or "姓名未同步")
    captured = {"captured": True, "schema_version": ARCHIVE_SNAPSHOT_VERSION, "revision": revision, "observed_revision": revision,
                "attachment_version": attachment_version, "applicant_name": applicant,
                "applicant_department": department.name, "applicant_department_sync": department.snapshot(),
                "history_available": history_available,
                "events": events, "related_approvals": related, "files": files,
                "declared_company_names": declared_names, "declared_company_ids": declared_ids}
    current_refs = {(item["id"], item.get("sha256")) for item in files}
    previous_refs = previous.get("history_files", []) + previous.get("files", [])
    captured["history_files"] = list({(item["id"], item["sha256"]): item for item in previous_refs
        if item.get("storage_key") and (item["id"], item["sha256"]) not in current_refs}.values())
    begin_write(db)
    current = db.get(Approval, instance_code)
    if (not current or (current.last_synced_at, current.attachment_version, current.status) !=
            (version, attachment_version, status) or current.archive_snapshot != previous):
        raise StaleSnapshotError("审批同步期间打印资料已变化，稍后重试")
    patch = {"archive_snapshot": captured}
    if not current.submitted_at and (submitted := snapshot_metadata(snapshot).get("submitted_at")):
        patch["submitted_at"] = submitted
    if end := _approval_end(snapshot):
        patch["approved_at"] = end
    db.execute(update(Approval).where(Approval.id == instance_code).values(**patch, updated_at=Approval.updated_at))
    db.commit()
    # 先保存已取得的资料，再暴露部门失败。权限问题可手动重试，网络故障沿用任务队列退避。
    department.ensure_complete()
    missing = sum(bool(item.get("reason")) for item in files)
    if missing:
        raise DomainError(f"已保存打印资料，仍有 {missing} 个附件未取得，可重试本任务")
    return {"approval_id": instance_code, "files": len(files), "history_available": history_available,
            "applicant_department_status": department.status}
