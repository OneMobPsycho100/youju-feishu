"""历史台账导入：先映射预览再确认，坏行不阻止其余有效行入库。"""
import csv
import io
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from .integrations import DomainError
from .db import begin_write
from .models import ImportBatch, InvoiceClaim
from .parser import normalize
from .services import canonical_invoice, log

IMPORT_COLUMNS = {"number": "发票号码", "code": "发票代码", "buyer_name": "购买方名称", "buyer_tax_id": "购买方税号",
                  "seller_name": "销售方名称", "issue_date": "开票日期", "amount": "价税合计", "source_label": "历史审批编号"}


def read_ledger(data: bytes, filename: str) -> tuple[list[str], list[dict]]:
    if len(data) > 20 * 1024 * 1024:
        raise DomainError("历史台账不得超过 20 MB")
    suffix = Path(filename).suffix.lower()
    try:
        if suffix == ".csv":
            try:
                text = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = data.decode("gb18030")
            reader = csv.DictReader(io.StringIO(text))
            columns = reader.fieldnames or []
            rows = []
            for i, row in enumerate(reader):
                if i >= 10000:
                    raise DomainError("单批最多导入 10000 行")
                rows.append({str(k): str(v or "").strip() for k, v in row.items() if k is not None})
        elif suffix == ".xlsx":
            import zipfile
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if sum(x.file_size for x in archive.infolist()) > 150 * 1024 * 1024:
                    raise DomainError("表格解压内容超过允许范围")
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False)
            sheet = workbook.active
            iterator = sheet.iter_rows(values_only=True)
            columns = [str(x or "").strip() for x in next(iterator)]
            rows = []
            for i, values in enumerate(iterator):
                if i >= 10000:
                    raise DomainError("单批最多导入 10000 行")
                row = {key: value.isoformat()[:10] if isinstance(value, (date, datetime)) else str(value or "").strip()
                       for key, value in zip(columns, values) if key}
                if any(row.values()):
                    rows.append(row)
            workbook.close()
        else:
            raise DomainError("只支持 CSV 和 XLSX 文件")
        if not columns or len(set(columns)) != len(columns) or not rows:
            raise DomainError("表头不能为空或重复，台账至少需要一行数据")
        if len(columns) > 100:
            raise DomainError("台账列数不能超过 100")
        return columns, rows
    except DomainError:
        raise
    except Exception as exc:
        raise DomainError("无法读取台账，请检查编码、表头和文件格式；发票号码应保存为文本") from exc


def preview_import(batch: ImportBatch, mapping: dict[str, str]) -> None:
    if batch.status in {"IMPORTING", "COMPLETED"}:
        raise DomainError("此批次已经确认，不能重新修改映射")
    required = {"number", "buyer_name", "buyer_tax_id", "issue_date", "amount"}
    if any(not mapping.get(key) or mapping[key] not in batch.columns for key in required):
        raise DomainError("请映射发票号码、购买方名称、购买方税号、开票日期和价税合计")
    preview, errors, seen = [], [], set()
    for index, row in enumerate(batch.rows, start=2):
        fields = {key: row.get(column, "") for key, column in mapping.items() if column}
        try:
            normalized = normalize(fields)
            if normalized["invoice_key"] in seen:
                raise ValueError("同一台账内存在重复发票")
            seen.add(normalized["invoice_key"])
            preview.append({**normalized, "row": index, "source_label": fields.get("source_label") or f"历史台账第 {index} 行"})
        except ValueError as exc:
            errors.append({"row": index, "error": str(exc)})
    batch.mapping, batch.preview, batch.errors, batch.status = mapping, preview, errors, "PREVIEWED"


def commit_import(db, batch_id: str, actor_name: str = "历史导入服务") -> dict:
    begin_write(db)
    batch = db.get(ImportBatch, batch_id)
    if batch.status == "COMPLETED":
        return {"imported": batch.imported, "errors": len(batch.errors)}
    if batch.status != "IMPORTING":
        raise DomainError("请先预览并确认导入批次")
    errors, imported = list(batch.errors), 0
    for item in batch.preview:
        try:
            with db.begin_nested():
                invoice = canonical_invoice(db, item, historical=True)
                existing = db.scalar(select(InvoiceClaim).where(InvoiceClaim.active_key == invoice.id))
                if existing:
                    raise DomainError(f"发票已被 {existing.source_label} 占用，未覆盖既有记录")
                if (invoice.buyer_tax_id != item["buyer_tax_id"] or invoice.amount != Decimal(item["amount"])
                        or invoice.issue_date.isoformat() != item["issue_date"]):
                    raise DomainError("与已建档发票的税号、日期或金额不一致")
                db.add(InvoiceClaim(invoice_id=invoice.id, status="HISTORICAL", active_key=invoice.id,
                                    source_label=item["source_label"][:150]))
                db.flush()
            imported += 1
        except (DomainError, IntegrityError) as exc:
            errors.append({"row": item["row"], "error": str(exc) if isinstance(exc, DomainError) else "并发占用冲突，未导入此行"})
    batch.errors, batch.imported, batch.status = errors, imported, "COMPLETED"
    log(db, actor_name, "导入历史发票台账", batch.filename, {"imported": imported, "errors": len(errors), "batch_id": batch_id})
    db.commit()
    return {"imported": imported, "errors": len(errors)}
