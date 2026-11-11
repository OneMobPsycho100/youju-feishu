"""审批级归档查询及原件下载，共用当前版本、补件和历史版本的投影。"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
from pathlib import Path, PurePosixPath
import tempfile
import zipfile

from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import and_, func, or_, select
from starlette.background import BackgroundTask

from .archive_department import department_pending
from .archive_metadata import ARCHIVE_SNAPSHOT_VERSION, archived_approval_filter
from .attachment_archives import archive_members, archive_state
from .business import _local_applicant_name
from .config import settings
from .db import begin_write
from .errors import DomainError
from .file_types import ARCHIVE_MIMES, INLINE_PREVIEW_MIMES
from .models import Approval, ArchiveRecord, Attachment, Company, Invoice, now
from .services import feishu_link, log
from .storage import resolve, safe_name


BEIJING = timedelta(hours=8)
MAX_ZIP_BYTES = 500 * 1024 * 1024


def month_bounds(value: str) -> tuple[datetime, datetime]:
    """北京时间月份统一为 UTC 左闭右开区间；开票日期使用本地日历边界。"""
    try:
        if len(value) != 7:
            raise ValueError
        start = datetime.fromisoformat(value + "-01")
        if start.year <= 1:
            raise ValueError
        end = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1)
        return start, end
    except (ValueError, OverflowError) as exc:
        raise DomainError("月份应为有效的 YYYY-MM") from exc


def _has_company(column, company_id: str):
    members = func.json_each(column).table_valued("key", "value")
    return select(1).select_from(members).where(members.c.value == company_id).correlate(Approval).exists()


def _archive_subject(company_id: str = ""):
    condition = ArchiveRecord.company_id == company_id if company_id else ArchiveRecord.company_id.is_not(None)
    return select(1).select_from(ArchiveRecord).join(Attachment, Attachment.id == ArchiveRecord.attachment_id).where(
        ArchiveRecord.approval_id == Approval.id, condition, Attachment.active.is_(True)).correlate(Approval).exists()


def archive_query(*, company_id="", month="", month_basis="approved", invoice_month="", scope="approved",
                  archive_status="", q="", business_type="", ids=None):
    """列表与助手按审批实例分页；对一对多关系使用 EXISTS，避免把附件数算成单数。"""
    stamp = Approval.approved_at if month_basis == "approved" else Approval.submitted_at
    statement = select(Approval).where(archived_approval_filter())
    if scope == "revoked":
        statement = statement.where(Approval.archive_status == "REVOKED", Approval.status != "APPROVED")
    elif scope == "approved":
        statement = statement.where(Approval.status == "APPROVED")
    if ids is not None:
        statement = statement.where(Approval.id.in_(ids))
    if company_id:
        statement = statement.where(or_(
            _has_company(Approval.company_ids, company_id), _archive_subject(company_id),
            Approval.payment_details["company_id"].as_string() == company_id,
            and_(func.json_array_length(Approval.company_ids) == 0, ~_archive_subject(),
                 _has_company(Approval.archive_snapshot["declared_company_ids"], company_id))))
    if month:
        start, end = month_bounds(month)
        statement = statement.where(stamp >= start - BEIJING, stamp < end - BEIJING)
    if invoice_month:
        start, end = month_bounds(invoice_month)
        statement = statement.where(select(1).select_from(Attachment).join(Invoice, Invoice.id == Attachment.invoice_id)
            .where(Attachment.approval_id == Approval.id, Attachment.active.is_(True),
                   Invoice.issue_date >= start.date(), Invoice.issue_date < end.date()).correlate(Approval).exists())
    if business_type:
        statement = statement.where(Approval.business_type == business_type)
    if archive_status == "QUARANTINED":
        statement = statement.where(or_(select(1).where(ArchiveRecord.approval_id == Approval.id,
            ArchiveRecord.status == "QUARANTINED").correlate(Approval).exists(),
            and_(func.json_array_length(Approval.company_ids) == 0, ~_archive_subject(),
                 func.coalesce(func.json_array_length(Approval.archive_snapshot["declared_company_ids"]), 0) == 0,
                 Approval.payment_details["company_id"].as_string().is_(None))))
    elif archive_status:
        statement = statement.where(Approval.archive_status == archive_status)
    if q:
        supplements = func.json_each(Approval.archive_snapshot["files"]).table_valued("key", "value")
        statement = statement.where(or_(
            Approval.serial_number.contains(q, autoescape=True), Approval.title.contains(q, autoescape=True),
            Approval.applicant.contains(q, autoescape=True), _local_applicant_name().contains(q, autoescape=True),
            Approval.archive_snapshot["applicant_name"].as_string().contains(q, autoescape=True),
            select(1).where(Attachment.approval_id == Approval.id, Attachment.filename.contains(q, autoescape=True))
                .correlate(Approval).exists(),
            select(1).select_from(supplements).where(func.json_extract(supplements.c.value, "$.filename")
                .contains(q, autoescape=True)).correlate(Approval).exists()))
    return statement.order_by(stamp.desc().nulls_last(), Approval.id)


def snapshot_current(approval: Approval) -> bool:
    data = approval.archive_snapshot or {}
    return bool(data.get("captured") and data.get("schema_version") == ARCHIVE_SNAPSHOT_VERSION
                and data.get("revision") == data.get("observed_revision")
                and data.get("attachment_version") == approval.attachment_version)


@dataclass(frozen=True)
class ArchiveFile:
    """服务端原件引用。文件系统键从可信数据读取，不进入公开接口。"""
    id: str
    filename: str
    origin: str
    status: str
    size: int | None = None
    company_id: str | None = None
    download_url: str | None = None
    reason: str = ""
    archive_path: str = ""
    manifest: dict = field(default_factory=dict)
    storage_key: str = ""
    sha256: str = ""
    mime: str = ""
    package_status: str | None = None
    archive_source: dict | None = None

    def public(self, *, check_storage=False):
        data = {key: value for key, value in self.__dict__.items() if key not in {"storage_key", "sha256", "mime"}}
        if check_storage and self.storage_key and not resolve(self.storage_key).is_file():
            data.update(status="MISSING", download_url=None, reason="存储中的原件缺失，请重新同步")
        data["preview_url"] = (data["download_url"] + "?preview=true"
                               if data["download_url"] and self.mime in INLINE_PREVIEW_MIMES else None)
        return data


def original_ref(attachment: Attachment, record: ArchiveRecord | None = None, *, origin="FORM",
                 filename="", historical=False, archive_source=None) -> ArchiveFile:
    """旧清单和新整单目录共用原件引用，并保留既有状态和清单内容。"""
    state = archive_state(attachment)
    if attachment.mime in ARCHIVE_MIMES and Path(filename).suffix.lower() != Path(attachment.filename).suffix.lower():
        filename = attachment.filename
    status = record.status if record else "SUPPLEMENT" if archive_source else "AVAILABLE"
    return ArchiveFile(id=attachment.id, filename=filename or attachment.filename, origin=origin,
        status="SUPERSEDED" if historical else status,
        size=attachment.size, company_id=record.company_id if record else None,
        download_url=f"/api/v1/archives/approvals/{attachment.approval_id}/files/{attachment.id}",
        archive_path=record.archive_path if record else "", manifest=record.manifest if record else {},
        storage_key=attachment.storage_key, sha256=attachment.sha256, mime=attachment.mime,
        reason=state.get("error", ""), package_status=state.get("status"), archive_source=archive_source)


def _supplement_ref(approval: Approval, item: dict, historical=False) -> ArchiveFile:
    file_id = item["id"]
    if item.get("sha256"):
        file_id += "-" + item["sha256"][:16]
    available = bool(item.get("storage_key"))
    filename = item["filename"]
    return ArchiveFile(id=file_id, filename=filename, origin=item["origin"], size=item.get("size"),
        status="SUPERSEDED" if historical else "SUPPLEMENT" if available else "MISSING",
        download_url=f"/api/v1/archives/approvals/{approval.id}/files/{file_id}" if available else None,
        reason=item.get("reason", ""), storage_key=item.get("storage_key", ""), sha256=item.get("sha256", ""),
        mime=item.get("mime", ""), manifest={"filename": filename, "sha256": item.get("sha256"),
            "size": item.get("size"), "instance_code": approval.id, "serial_number": approval.serial_number,
            "business_type": approval.business_type, "origin": item["origin"]} if available else {})


def approval_files(approval: Approval, attachments: list[Attachment],
                   records: list[ArchiveRecord]) -> tuple[list[ArchiveFile], list[ArchiveFile]]:
    """按来源顺序组织当前附件；旧单仍能用本地原件和归档记录作为基础。"""
    by_id = {item.id: item for item in attachments}
    by_attachment = {item.attachment_id: item for item in records}
    member_sources = archive_members(attachments)
    children = defaultdict(list)
    for member_id, (parent, path) in member_sources.items():
        if member := by_id.get(member_id):
            children[parent.id].append((member, path))
    active = {item.id for item in attachments if item.active}
    snapshot = approval.archive_snapshot or {}
    revoked = approval.status != "APPROVED" and approval.archive_status == "REVOKED"
    files, historical, listed = [], [], set()
    captured = snapshot_current(approval)
    for item in snapshot.get("files", []):
        ref = item.get("attachment_id")
        if ref in by_id and (captured and ref in active or revoked):
            files.append(original_ref(by_id[ref], by_attachment.get(ref), origin=item["origin"], filename=item["filename"]))
            listed.add(ref)
        elif ref is None:
            if captured or revoked:
                files.append(_supplement_ref(approval, item))
            elif item.get("storage_key"):
                historical.append(_supplement_ref(approval, item, True))
    for item in attachments:
        if item.id in listed or item.id in member_sources:
            continue
        target = files if item.id in active and (not captured or revoked or archive_state(item)) else historical
        target.append(original_ref(item, by_attachment.get(item.id), origin="LOCAL", historical=target is historical))

    def include_members(items, *, history=False):
        """归档快照仍索引飞书原包，包内成员跟随该原包的当前或历史版本显示。"""
        result, seen = [], set()
        for item in items:
            if item.id in seen:
                continue
            seen.add(item.id)
            result.append(item)
            for member, path in children.get(item.id, []):
                if member.id in seen or (not history and not member.active and not revoked):
                    continue
                seen.add(member.id)
                source = {"attachment_id": item.id, "filename": item.filename, "path": path}
                result.append(original_ref(member, by_attachment.get(member.id), origin="PACKAGE",
                                           historical=history, archive_source=source))
        return result

    historical.extend(_supplement_ref(approval, item, True) for item in snapshot.get("history_files", []))
    # 旧付款单在打印快照补齐前仍显示已知缺件。
    if not captured and not revoked:
        for index, issue in enumerate((approval.payment_details or {}).get("material_issues", [])):
            files.append(ArchiveFile(f"payment-missing-{index}", issue["filename"], "FORM", "MISSING",
                                     reason="付款附件尚未取得，请同步打印资料"))
    return include_members(files), include_members(historical, history=True)


def read_archive_rows(db, approvals: list[Approval]) -> dict:
    """批量取得审批投影需要的原件、清单和主体，避免逐单查询。"""
    ids = [row.id for row in approvals]
    if not ids:
        return {}
    attachments, records = defaultdict(list), defaultdict(list)
    for item in db.scalars(select(Attachment).where(Attachment.approval_id.in_(ids)).order_by(Attachment.created_at, Attachment.id)):
        attachments[item.approval_id].append(item)
    for item in db.scalars(select(ArchiveRecord).where(ArchiveRecord.approval_id.in_(ids)).order_by(ArchiveRecord.created_at)):
        records[item.approval_id].append(item)
    local_names = dict(db.execute(select(Approval.id, _local_applicant_name()).where(Approval.id.in_(ids))).all())
    subjects = {}
    for row in approvals:
        active = {item.id for item in attachments[row.id] if item.active}
        assigned = list(row.company_ids or [])
        assigned.extend(item.company_id for item in records[row.id] if item.company_id and item.attachment_id in active)
        if company_id := (row.payment_details or {}).get("company_id"):
            assigned.append(company_id)
        if not assigned:
            assigned.extend((row.archive_snapshot or {}).get("declared_company_ids", []))
        subjects[row.id] = list(dict.fromkeys(assigned))
    names = dict(db.execute(select(Company.id, Company.name).where(Company.id.in_(
        {identity for values in subjects.values() for identity in values}))).all())
    result = {}
    for row in approvals:
        files, historical = approval_files(row, attachments[row.id], records[row.id])
        snapshot = row.archive_snapshot or {}
        companies = [{"id": identity, "name": names[identity]} for identity in subjects[row.id] if identity in names]
        declared = snapshot.get("declared_company_names") or []
        payment_company = (row.payment_details or {}).get("company_name")
        if not declared and payment_company:
            declared = [payment_company]
        applicant = snapshot.get("applicant_name") or local_names.get(row.id) or row.applicant
        if applicant.startswith(("ou_", "user_id:")) or applicant == (row.applicant_id or "").removeprefix("user_id:"):
            applicant = "姓名未同步"
        summary = {"id": row.id, "serial_number": row.serial_number, "title": row.title, "business_type": row.business_type,
                   "applicant": applicant, "companies": companies, "declared_company_names": declared, "amount": row.amount,
                   "submitted_at": row.submitted_at, "approved_at": row.approved_at, "status": row.status,
                   "archive_status": row.archive_status, "archived_at": records[row.id][-1].created_at if records[row.id] else None,
                   "attachment_count": len(files), "missing_count": sum(bool(item.reason) and not item.storage_key for item in files),
                   "print_data_pending": not snapshot_current(row) or department_pending(snapshot),
                   "printable": row.status == "APPROVED"}
        result[row.id] = summary, files, historical
    return result


def list_archive_approvals(db, page=1, page_size=20, **filters):
    statement = archive_query(**filters)
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery()))
    rows = list(db.scalars(statement.offset((page - 1) * page_size).limit(page_size)))
    by_id = read_archive_rows(db, rows)
    return {"items": [by_id[row.id][0] for row in rows], "total": total, "page": page, "page_size": page_size}


def get_archived_approval(db, approval_id: str) -> Approval:
    row = db.scalar(select(Approval).where(Approval.id == approval_id, archived_approval_filter()))
    if row is None:
        raise HTTPException(404, "归档审批不存在")
    return row


def archive_detail(db, approval_id: str) -> dict:
    row = get_archived_approval(db, approval_id)
    summary, files, historical = read_archive_rows(db, [row])[row.id]
    public_files = [item.public(check_storage=True) for item in files]
    return {**summary, "files": public_files, "history_files": [item.public(check_storage=True) for item in historical],
            "missing_count": sum(bool(item["reason"]) for item in public_files),
            "history_available": bool((row.archive_snapshot or {}).get("history_available")),
            "feishu_url": feishu_link(row.id) if settings().app_mode == "live" else ""}


def archive_file(db, approval_id: str, file_id: str) -> ArchiveFile:
    row = get_archived_approval(db, approval_id)
    _, files, history = read_archive_rows(db, [row])[row.id]
    ref = next((item for item in files + history if item.id == file_id and item.storage_key), None)
    if ref is None or not resolve(ref.storage_key).is_file():
        raise HTTPException(404, "归档原件不存在")
    return ref


def ordered_approvals(db, ids: list[str], *, printable=False) -> list[Approval]:
    """按调用方的选择顺序返回审批；包含不可用单据时拒绝整个批次。"""
    by_id = {item.id: item for item in db.scalars(select(Approval).where(
        Approval.id.in_(ids), archived_approval_filter()))}
    if len(by_id) != len(ids):
        raise DomainError("部分归档审批不存在，请刷新列表")
    if printable and any(item.status != "APPROVED" for item in by_id.values()):
        raise DomainError("所选单据中有审批已变更，当前仅可打印最终通过的单据")
    return [by_id[identity] for identity in ids]


def make_archive_zip(db, user_name: str, entries: list[tuple[str, ArchiveFile]]) -> FileResponse:
    """兼容旧清单格式；单批从不可变原件生成 ZIP，返回后清理临时文件。"""
    if not entries:
        raise DomainError("所选审批暂无可下载的原件")
    if sum(item.size or 0 for _, item in entries) > MAX_ZIP_BYTES:
        raise DomainError("单次打包超过 500 MB，请分批下载")
    root = resolve("exports")
    root.mkdir(exist_ok=True, parents=True)
    handle = tempfile.NamedTemporaryFile(suffix=".zip", dir=root, delete=False)
    handle.close()
    path = Path(handle.name)
    try:
        seen, manifest = {"manifest.json"}, []
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for fallback, item in entries:
                entry = (item.archive_path or fallback).replace("\\", "/")
                if entry.startswith("/") or any(part == ".." or ":" in part for part in PurePosixPath(entry).parts):
                    raise DomainError("归档目录不合法，请联系管理员")
                while entry in seen:
                    original = PurePosixPath(entry)
                    entry = str(original.with_name(item.id + "-" + original.name))
                seen.add(entry)
                source = resolve(item.storage_key)
                if not source.is_file():
                    raise DomainError(f"原件「{item.filename}」缺失，请同步后重试")
                archive.write(source, entry)
                manifest_entry = {**item.manifest, "filename": item.filename, "sha256": item.sha256,
                                  "size": item.size, "path": entry, "status": item.status}
                if item.archive_source:
                    manifest_entry["archive_source"] = item.archive_source
                manifest.append(manifest_entry)
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        begin_write(db)
        log(db, user_name, "下载归档原件包", f"{len(entries)} 个附件")
        db.commit()
        return FileResponse(path, filename=f"invoice-archive-{now():%Y%m%d}.zip",
                            background=BackgroundTask(path.unlink, missing_ok=True))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def archive_approval_zip(db, user_name: str, ids: list[str]) -> FileResponse:
    approvals = ordered_approvals(db, ids)
    by_id = read_archive_rows(db, approvals)
    entries = [(f"{safe_name(row.serial_number)}/{safe_name(item.filename)}", item)
               for row in approvals for item in by_id[row.id][1] if item.storage_key]
    if len(entries) > 200:
        raise DomainError("一次最多打包 200 个原件，请分批下载")
    return make_archive_zip(db, user_name, entries)
