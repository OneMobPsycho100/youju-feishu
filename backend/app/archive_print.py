"""审批打印任务。后台组装文件，交付时再次确认审批仍处于最终通过状态。"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import logging
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
import time

from fastapi import HTTPException
from pypdf import PdfReader, PdfWriter

from .archive_center import ArchiveFile, ordered_approvals, read_archive_rows, snapshot_current
from .archive_department import department_pending
from .archive_conversion import ConversionError, convert_attachment
from .archive_pdf import PrintForm, render_approval, render_plain
from .archive_schemas import MAX_PRINT_APPROVALS, MAX_PRINT_FILES, MAX_PRINT_PAGES, PrintIssue
from .config import settings
from .db import SessionLocal, begin_write
from .errors import DomainError
from .feishu_form import reimbursement_form
from .models import Approval, ApprovalTemplate, User, now, uid
from .services import feishu_link, log
from .storage import resolve
from .template_transfer import digest


logger = logging.getLogger(__name__)
RETENTION = timedelta(minutes=30)
MAX_INPUT_BYTES = 500 * 1024 * 1024
MAX_PDF_BYTES = 200 * 1024 * 1024
MAX_QUEUED = 8


def approval_ids(values: list[str]) -> list[str]:
    """整单打印及打包共用审批 ID 校验，去重时保留勾选顺序。"""
    ids = list(dict.fromkeys(values))
    if not 1 <= len(ids) <= MAX_PRINT_APPROVALS or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", item) for item in ids):
        raise DomainError(f"请选择 1 至 {MAX_PRINT_APPROVALS} 张有效审批单")
    return ids


def _versions(rows: list[Approval]) -> tuple:
    return tuple((item.id, item.version, item.attachment_version, item.approved_at,
                  digest(item.archive_snapshot)) for item in rows)


def _selected_files(files: list[ArchiveFile], selected_ids: list[str] | None) -> list[ArchiveFile]:
    """仅从当前原件解析选择，保留原始顺序；None 兼容旧接口的整单打印。"""
    if selected_ids is None:
        return files
    selected = set(selected_ids)
    failed_packages = {item.id for item in files if item.package_status == "FAILED"}
    # 展开成功的原包只作为分组；展开失败时不可通过勾选残留成员绕过原包错误。
    selectable = [item for item in files if item.package_status != "READY" and not (
        item.archive_source and item.archive_source["attachment_id"] in failed_packages)]
    if not selected <= {item.id for item in selectable}:
        raise DomainError("所选附件已失效或不属于对应审批的当前原件，请重新打开打印窗口")
    return [item for item in selectable if item.id in selected]


@dataclass
class PrintJob:
    """仅在当前 API 进程保存任务。重启后重新发起即可恢复打印。"""
    id: str
    owner: str
    printer: str
    ids: list[str]
    versions: tuple
    # None 为兼容旧调用的全部附件；显式映射保留每单选择，空数组表示仅打印正文。
    file_ids_by_approval: dict[str, list[str]] | None = None
    status: str = "QUEUED"
    completed: int = 0
    pages: int = 0
    issues: list[PrintIssue] = field(default_factory=list)
    error: str = ""
    expires_at: datetime | None = None
    read_until: datetime | None = None

    def dump(self):
        return {"id": self.id, "status": self.status, "total": len(self.ids), "completed": self.completed,
                "pages": self.pages, "issues": list(self.issues), "error": self.error, "expires_at": self.expires_at,
                "pdf_url": f"/api/v1/archives/print-jobs/{self.id}/pdf" if self.status == "READY" else None}


class PrintManager:
    """专用打印队列只负责本地转换，长任务不占用审批同步队列或数据库事务。"""

    def __init__(self):
        self.lock = Lock()
        self.jobs: dict[str, PrintJob] = {}
        self.executor: ThreadPoolExecutor | None = None
        self.stop: Event | None = None
        self.reaper: Thread | None = None

    def _path(self, identity: str) -> Path:
        return resolve("exports/approval-print/" + identity + ".pdf")

    def start(self):
        with self.lock:
            if self.executor is not None:
                return
            root = resolve("exports/approval-print")
            root.mkdir(parents=True, exist_ok=True)
            # 进程重启后旧的临时任务不可恢复，只清理本模块命名的打印稿。
            for path in root.iterdir():
                if path.is_file() and path.suffix in {".pdf", ".tmp"} and re.fullmatch(r"[0-9a-f]{32}", path.stem):
                    path.unlink(missing_ok=True)
            stop = Event()

            def reap():
                while not stop.wait(10):
                    with self.lock:
                        self._purge()

            self.stop = stop
            self.reaper = Thread(target=reap, daemon=True, name="archive-print-cleanup")
            self.reaper.start()
            self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="archive-print")

    def _purge(self):
        stamp = now()
        expired = [job for job in self.jobs.values() if job.expires_at and job.expires_at <= stamp
                   and (not job.read_until or job.read_until <= stamp)]
        for job in expired:
            try:
                self._path(job.id).unlink(missing_ok=True)
            except OSError:
                continue
            del self.jobs[job.id]

    def shutdown(self):
        with self.lock:
            executor, self.executor = self.executor, None
            if self.stop:
                self.stop.set()
        if self.reaper:
            self.reaper.join(2)
        if executor:
            executor.shutdown(wait=False, cancel_futures=True)

    def submit(self, db, user: User, ids: list[str], file_ids_by_approval: dict[str, list[str]] | None = None) -> dict:
        """校验每单附件范围后入队，数量及原件大小限制仅统计本次实际选择。"""
        if file_ids_by_approval is not None and set(file_ids_by_approval) != set(ids):
            raise DomainError("打印附件选择必须覆盖全部所选审批，且不能包含其他审批")
        rows = ordered_approvals(db, ids, printable=True)
        by_id = read_archive_rows(db, rows)
        selected = {row.id: _selected_files(by_id[row.id][1],
            file_ids_by_approval[row.id] if file_ids_by_approval is not None else None) for row in rows}
        files = [item for row in rows for item in selected[row.id]]
        if len(files) > MAX_PRINT_FILES or sum(item.size or 0 for item in files) > MAX_INPUT_BYTES:
            raise DomainError(f"单次打印最多 {MAX_PRINT_FILES} 个附件、500 MB 原件，请分批")
        # 保存从服务端原件解析出的副本，客户端顺序、重复 ID 或后续变更均不影响任务范围。
        selection = ({identity: [item.id for item in values] for identity, values in selected.items()}
                     if file_ids_by_approval is not None else None)
        task = PrintJob(uid(), user.id, user.name, ids, _versions(rows), file_ids_by_approval=selection)
        db.rollback()
        with self.lock:
            self._purge()
            active = [job for job in self.jobs.values() if job.status in {"QUEUED", "RUNNING"}]
            if not self.executor or len(active) >= MAX_QUEUED or any(job.owner == task.owner for job in active):
                raise HTTPException(429, "打印队列繁忙或已有任务在生成，请稍后重试")
            self.jobs[task.id] = task
            self.executor.submit(self._work, task)
            return task.dump()

    def _owned(self, identity: str, owner: str) -> PrintJob:
        task = self.jobs.get(identity)
        if task is None or task.owner != owner or task.expires_at and task.expires_at <= now():
            raise HTTPException(404, "打印任务不存在或已过期，请重新发起")
        return task

    @staticmethod
    def _verify(db, task: PrintJob) -> list[Approval]:
        approvals = ordered_approvals(db, task.ids, printable=True)
        if _versions(approvals) != task.versions:
            raise DomainError("审批内容或附件版本已更新，请重新生成打印稿")
        return approvals

    def state(self, db, identity: str, owner: str) -> dict:
        with self.lock:
            task = self._owned(identity, owner)
            data = task.dump()
        if data["status"] == "READY":
            self._verify(db, task)
        return data

    def file(self, db, identity: str, owner: str) -> Path:
        with self.lock:
            task = self._owned(identity, owner)
            if task.status != "READY":
                raise HTTPException(409, "打印稿尚未生成")
            task.read_until = now() + timedelta(minutes=1)
        self._verify(db, task)
        path = self._path(identity)
        if not path.is_file():
            raise HTTPException(404, "打印稿已过期，请重新发起")
        return path

    def _forms(self, task: PrintJob) -> list[tuple[PrintForm, list[ArchiveFile]]]:
        with SessionLocal() as db:
            owner = db.get(User, task.owner)
            if owner is None or not owner.enabled:
                raise DomainError("打印账号当前不可用")
            rows = self._verify(db, task)
            by_id = read_archive_rows(db, rows)
            forms = []
            for row in rows:
                template = db.get(ApprovalTemplate, row.template_code)
                mapping = (template.field_mapping or {}) if template else {}
                summary, files, _ = by_id[row.id]
                files = _selected_files(files, task.file_ids_by_approval[row.id]
                                        if task.file_ids_by_approval is not None else None)
                payment = {**(row.payment_details or {}), "expense_type": row.expense_type}
                forms.append((PrintForm(summary, reimbursement_form(row.raw_form, mapping), mapping,
                                       row.department, payment, row.archive_snapshot or {}, snapshot_current(row)), files))
            return forms

    def _build(self, task: PrintJob, target: Path) -> tuple[int, list[PrintIssue]]:
        forms = self._forms(task)
        issues = []
        deadline = time.monotonic() + 600
        total_size, pages = 0, 0
        printer_time = now()
        with TemporaryDirectory(prefix="approval-print-") as directory, ExitStack() as stack:
            root = Path(directory).resolve()
            writer = PdfWriter()

            def append(path: Path):
                nonlocal pages, total_size
                stream = stack.enter_context(path.open("rb"))
                document = PdfReader(stream)
                count = len(document.pages)
                total_size += path.stat().st_size
                if pages + count > MAX_PRINT_PAGES or total_size > MAX_PDF_BYTES:
                    raise DomainError(f"打印稿超过 {MAX_PRINT_PAGES} 页或 200 MB，请分批")
                for page in document.pages:
                    writer.add_page(page, excluded_keys=("/Annots", "/AA"))
                pages += count

            for index, (form, files) in enumerate(forms, 1):
                summary = form.summary
                work = root / str(index)
                work.mkdir()
                approval_pdf = work / "approval.pdf"
                render_approval(approval_pdf, form, task.printer, printer_time)
                append(approval_pdf)
                form_issues = []
                feishu_url = feishu_link(summary["id"]) if settings().app_mode == "live" else ""

                def issue(filename, reason, download=None):
                    form_issues.append(PrintIssue(approval_id=summary["id"], serial_number=summary["serial_number"],
                        filename=filename, reason=reason, download_url=download, feishu_url=feishu_url))

                # 部门待补齐与附件索引过期分别报告，避免把通讯录授权问题误写为附件缺失。
                if not form.attachments_current and (task.file_ids_by_approval is None or files):
                    scope = "本稿仅包含本次选择的附件" if task.file_ids_by_approval is not None else "本稿包含当前可取得的原件"
                    issue("整单附件索引", "整单附件范围待飞书同步，" + scope)
                if department_pending(form.snapshot):
                    reason = (form.snapshot.get("applicant_department_sync") or {}).get("error")
                    issue("所属部门", reason or "申请人所属部门待同步，请补齐打印资料")
                if not form.snapshot.get("history_available"):
                    issue("审批记录", "飞书审批记录待同步")
                failed_packages = {item.id for item in files if item.package_status == "FAILED"}
                for number, item in enumerate(files, 1):
                    if time.monotonic() > deadline or self.stop and self.stop.is_set():
                        raise DomainError("打印批次处理超时或服务正在关闭，请重新发起或分批")
                    # 展开后的成员分别转印。原包仍可下载，失败原因随打印稿提供。
                    if item.package_status:
                        if item.package_status == "FAILED":
                            issue(item.filename, item.reason, item.download_url)
                        continue
                    if item.archive_source and item.archive_source["attachment_id"] in failed_packages:
                        continue
                    attachment_dir = work / str(number)
                    attachment_dir.mkdir()
                    try:
                        converted, _ = convert_attachment(item, attachment_dir)
                    except ConversionError as exc:
                        original = item.download_url if item.storage_key and resolve(item.storage_key).is_file() else None
                        issue(item.filename, str(exc), original)
                    except Exception as exc:
                        logger.warning("archive_print_attachment job=%s error_type=%s", task.id, type(exc).__name__)
                        original = item.download_url if item.storage_key and resolve(item.storage_key).is_file() else None
                        issue(item.filename, "文件无法读取或转换，请核对原件", original)
                    else:
                        append(converted)
                if form_issues:
                    notice = work / "issues.pdf"
                    render_plain(notice, "本单未能完整打印的资料", [
                        f"申请编号  {summary['serial_number']}",
                        *[f"{item.filename}  ·  {item.reason}" for item in form_issues],
                    ])
                    append(notice)
                    issues.extend(form_issues)
                with self.lock:
                    task.completed = index
            writer.add_metadata({"/Title": "审批归档打印稿"})
            with target.open("wb") as output:
                writer.write(output)
        return pages, issues

    def _work(self, task: PrintJob):
        with self.lock:
            task.status = "RUNNING"
        path = self._path(task.id)
        partial = path.with_suffix(".tmp")
        try:
            pages, issues = self._build(task, partial)
            with SessionLocal() as db:
                begin_write(db)
                self._verify(db, task)
                log(db, task.printer, "生成审批归档打印稿", f"{len(task.ids)} 张审批单",
                    {"pages": pages, "issues": len(issues)})
                db.commit()
            partial.replace(path)
            with self.lock:
                task.status, task.pages, task.issues = "READY", pages, issues
                task.expires_at = now() + RETENTION
        except Exception as exc:
            logger.warning("archive_print job=%s error_type=%s", task.id, type(exc).__name__)
            with self.lock:
                task.status, task.expires_at = "FAILED", now() + RETENTION
                task.error = str(exc) if isinstance(exc, DomainError) else "打印稿生成失败，请稍后重试"
        finally:
            partial.unlink(missing_ok=True)


manager = PrintManager()
