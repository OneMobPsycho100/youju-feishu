"""将可打印原件转换成独立 PDF 后再合并，单个坏文件不会留下半份页面。"""
from contextlib import closing
import csv
from io import StringIO
import json
import logging
from math import ceil
import os
from pathlib import Path, PurePosixPath
import posixpath
import shutil
import signal
import stat
import subprocess
from tempfile import TemporaryDirectory
import zipfile

from defusedxml import ElementTree
from PIL import Image, ImageChops, ImageOps
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DictionaryObject, StreamObject
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from .archive_center import ArchiveFile
from .archive_pdf import render_plain
from .archive_schemas import MAX_PRINT_PAGES
from .config import settings
from .file_types import ARCHIVE_TYPES, ZIP_SIGNATURES, detect_file_type, zip_document_type
from .invoice_images import MAX_PIXELS, _pdf_lock
from .parser import fields_from_xml
from .storage import resolve


logger = logging.getLogger(__name__)
IMAGE_FORMATS = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp", ".gif", ".heic", ".heif"}
OFFICE_FORMATS = {".doc", ".docx", ".rtf", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".odp"}
PRINT_TEXT_LIMIT = 180_000
ARCHIVE_EXPANSION_LIMIT = 200 * 1024 * 1024
SIGNATURE_CHECK_SCALE = 2  # 144 DPI，用于比对外观，打印稿仍使用矢量页面。
MAX_SIGNATURE_CHECK_PIXELS = 4_000_000


class ConversionError(ValueError):
    """单份附件无法打印；此错误的中文说明可直接展示。"""


def _format(path: Path, filename: str) -> str:
    with path.open("rb") as stream:
        head = stream.read(1024)
    if head.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if head.startswith(b"BM"):
        return ".bmp"
    if head[4:8] == b"ftyp" and any(brand in head[8:40] for brand in (b"heic", b"heix", b"mif1", b"hevc")):
        return ".heic"
    if head.lstrip().startswith(b"{\\rtf"):
        return ".rtf"
    if head.startswith(ZIP_SIGNATURES):
        with zipfile.ZipFile(path) as archive:
            document = zip_document_type(archive)
            return document[0] if document else ".zip"
    return detect_file_type(head, filename)[0]


def _check_zip(path: Path, *, xml=False) -> None:
    if not zipfile.is_zipfile(path):
        if xml:
            raise ConversionError("OFD 文件已损坏")
        return
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > 2500 or sum(member.file_size for member in members) > ARCHIVE_EXPANSION_LIMIT:
            raise ConversionError("附件的解压内容超过打印限制")
        for member in members:
            entry = PurePosixPath(member.filename.replace("\\", "/"))
            if (entry.is_absolute() or ".." in entry.parts or any(":" in part for part in entry.parts)
                    or stat.S_ISLNK(member.external_attr >> 16)):
                raise ConversionError("附件含不安全的归档路径")
            if xml and entry.suffix.lower() == ".xml":
                ElementTree.fromstring(archive.read(member), forbid_dtd=True)


def _run_converter(kind: str, arguments: list[str], work: Path) -> None:
    python = settings().print_office_python
    if not python or not python.is_file():
        raise ConversionError("部署环境缺少 Office/OFD 转换运行时，请下载原件打印")
    command = [str(python), str(Path(__file__).with_name("archive_convert_runner.py")), kind, *arguments]
    with subprocess.Popen(command, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                          start_new_session=os.name == "posix") as process:
        try:
            code = process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                process.kill()
            process.wait()
            raise ConversionError("附件转换超时，请下载原件打印") from None
    if code:
        raise ConversionError("附件无法转换，可能已加密、损坏或包含不兼容内容")


def _xlsx_print_tabs(source: Path) -> tuple[int, list[int]] | None:
    """读取默认打印选区。已有打印布局时，由 Calc 读取工作簿的打印范围。"""
    def local_name(name: str) -> str:
        return name.rsplit("}", 1)[-1]

    with zipfile.ZipFile(source) as archive:
        try:
            book = ElementTree.fromstring(archive.read("xl/workbook.xml"), forbid_dtd=True)
            relationships = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"), forbid_dtd=True)
        except KeyError:
            return None
        if any(local_name(node.tag) == "definedName" and node.get("name") in
               {"_xlnm.Print_Area", "_xlnm.Print_Titles"} for node in book.iter()):
            return None
        sheets = next((list(node) for node in book if local_name(node.tag) == "sheets"), [])
        targets = {node.get("Id"): node.get("Target") for node in relationships
                   if node.get("Type", "").endswith("/worksheet") and node.get("TargetMode") != "External"}
        visible, selected = [], []
        for index, sheet in enumerate(sheets):
            if sheet.get("state", "visible") != "visible":
                continue
            relation = next((value for key, value in sheet.attrib.items() if local_name(key) == "id"), "")
            target = targets.get(relation)
            if not target:
                return None
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            visible.append(index)
            try:
                with archive.open(path) as stream:
                    for _, node in ElementTree.iterparse(stream, events=("end",), forbid_dtd=True):
                        tag = local_name(node.tag)
                        if (tag == "pageSetup" and node.attrib or tag == "brk"
                                or tag == "pageSetUpPr" and node.get("fitToPage") in {"1", "true"}):
                            return None
                        if tag == "sheetView" and node.get("workbookViewId", "0") == "0":
                            if node.get("tabSelected") in {"1", "true"}:
                                selected.append(index)
                        node.clear()
            except KeyError:
                return None
    return (len(sheets), list(dict.fromkeys(selected)) or visible) if visible else None


def _external_pdf(source: Path, suffix: str, work: Path) -> Path:
    _check_zip(source, xml=suffix == ".ofd")
    # 交给转换器的路径只由程序生成；原件目录与文件名不参与命令拼接。
    named = work / ("input" + suffix)
    shutil.copyfile(source, named)
    result = work / "converted.pdf"
    if suffix == ".ofd":
        classpath = settings().print_ofd_classpath
        if not classpath or not shutil.which("java"):
            raise ConversionError("部署环境缺少 OFD 转换器，请下载原件打印")
        _run_converter("ofd", ["java", "-Djava.awt.headless=true", "-Xmx384m", "-Djavax.xml.accessExternalDTD=",
            "-Djavax.xml.accessExternalSchema=", "-cp", classpath, "OfdPrint", str(named), str(result)], work)
    else:
        if not shutil.which("soffice"):
            raise ConversionError("部署环境缺少 LibreOffice，请下载原件打印")
        arguments = [str(named), str(result)]
        if suffix == ".xlsx" and (tabs := _xlsx_print_tabs(named)):
            arguments.append(json.dumps(tabs))
        _run_converter("office", arguments, work)
    if not result.is_file() or not result.stat().st_size:
        raise ConversionError("转换器未生成可打印页面")
    return result


def _check_pages(pages: int) -> None:
    if not pages:
        raise ConversionError("附件没有可打印页面")
    if pages > MAX_PRINT_PAGES:
        raise ConversionError(f"单个附件超过 {MAX_PRINT_PAGES} 页，请下载原件分段打印")


def _write_pdf_pages(reader: PdfReader, target: Path) -> None:
    """从空白 PDF 复制页面内容，排除原文档的表单、批注及页面动作。"""
    writer = PdfWriter()
    for page in reader.pages:
        width, height = float(page.mediabox.width), float(page.mediabox.height)
        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
            raise ConversionError("PDF 页面尺寸异常")
        writer.add_page(page, excluded_keys=("/Annots", "/AA"))
    with target.open("wb") as output:
        writer.write(output)


def _is_static_signature(annotation: DictionaryObject) -> bool:
    """只接受已有普通外观流的签名字段，其他批注交给页面渲染。"""
    if annotation.get("/Subtype") != "/Widget" or annotation.get_inherited("/FT") != "/Sig":
        return False
    appearances = annotation["/AP"] if "/AP" in annotation else {}
    return "/N" in appearances and isinstance(appearances["/N"], StreamObject)


def _same_print_appearance(source: Path, candidate: Path) -> bool:
    """使用现有打印渲染方式比对页数、尺寸及逐页 RGB 像素，限制核对位图大小。"""
    import pypdfium2 as pdfium

    with (_pdf_lock, closing(pdfium.PdfDocument(str(source))) as original,
          closing(pdfium.PdfDocument(str(candidate))) as flattened):
        original.init_forms()
        if len(original) != len(flattened):
            return False
        scale = SIGNATURE_CHECK_SCALE
        for index in range(len(original)):
            with closing(original[index]) as before, closing(flattened[index]) as after:
                width, height = before.get_size()
                if ((width, height) != after.get_size() or width <= 0 or height <= 0
                        or ceil(width * scale) * ceil(height * scale) > MAX_SIGNATURE_CHECK_PIXELS):
                    return False
                with (closing(before.render(scale=scale, draw_annots=True)) as before_bitmap,
                      closing(after.render(scale=scale)) as after_bitmap,
                      before_bitmap.to_pil() as before_image, after_bitmap.to_pil() as after_image,
                      ImageChops.difference(before_image, after_image) as difference):
                    if difference.getbbox():
                        return False
    return True


def _try_flatten_signatures(source: Path, target: Path, reader: PdfReader, annotated_pages: list[int]) -> bool:
    """嵌入静态签章外观并校验最终输出。返回 False 时由调用者用原件重新渲染。"""
    try:
        if "/AcroForm" not in reader.root_object:
            return False
        form = reader.root_object["/AcroForm"]
        if "/XFA" in form or ("/NeedAppearances" in form and form["/NeedAppearances"].value):
            return False
        for index in annotated_pages:
            annotations = reader.pages[index]["/Annots"]
            if not annotations or not all(_is_static_signature(ref.get_object()) for ref in annotations):
                return False

        import pypdfium2 as pdfium
        from pypdfium2 import raw

        with TemporaryDirectory(prefix="signatures-", dir=target.parent) as directory:
            work = Path(directory)
            flattened = work / "flattened.pdf"
            candidate = work / "candidate.pdf"
            with _pdf_lock, closing(pdfium.PdfDocument(str(source))) as document:
                if len(document) != len(reader.pages):
                    return False
                for index in annotated_pages:
                    # 保留预览中的可见外观，与现有打印流程的 draw_annots 语义一致。
                    with closing(document[index]) as page:
                        if raw.FPDFPage_Flatten(page, raw.FLAT_NORMALDISPLAY) != raw.FLATTEN_SUCCESS:
                            return False
                document.save(str(flattened))
            with flattened.open("rb") as stream:
                _write_pdf_pages(PdfReader(stream), candidate)
            # PDFium 报告成功也可能遗漏某些外观，核对通过后才交付打印稿。
            if not _same_print_appearance(source, candidate):
                return False
            candidate.replace(target)
    except Exception as exc:
        logger.warning("archive_print_signature_fallback error_type=%s", type(exc).__name__)
        return False
    return True


def _normalize_pdf(source: Path, target: Path) -> int:
    """保留矢量页面，优先嵌入可校验的静态签章，其他表单和批注按可见外观渲染。"""
    with source.open("rb") as stream:
        reader = PdfReader(stream)
        if reader.is_encrypted:
            raise ConversionError("PDF 已加密，请下载原件解密后打印")
        count = len(reader.pages)
        _check_pages(count)
        annotated_pages = [index for index, page in enumerate(reader.pages) if page.get("/Annots")]
        if not annotated_pages:
            _write_pdf_pages(reader, target)
        elif not _try_flatten_signatures(source, target, reader, annotated_pages):
            import pypdfium2 as pdfium

            # 保留批注、签章和表单外观，同时不把交互动作带入新文件。
            with _pdf_lock, closing(pdfium.PdfDocument(str(source))) as document:
                document.init_forms()
                canvas = Canvas(str(target))
                for index in range(count):
                    with closing(document[index]) as page:
                        width, height = page.get_size()
                        if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
                            raise ConversionError("PDF 页面尺寸异常")
                        scale = min(300 / 72, (MAX_PIXELS / (width * height)) ** 0.5)
                        with closing(page.render(scale=scale, draw_annots=True)) as bitmap, bitmap.to_pil() as image:
                            canvas.setPageSize((width, height))
                            canvas.drawImage(ImageReader(image), 0, 0, width=width, height=height)
                            canvas.showPage()
                canvas.save()
    return count


def _image_pdf(source: Path, target: Path, suffix: str) -> int:
    if suffix in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener
        except ImportError:
            raise ConversionError("部署环境缺少 HEIC 解码器，请下载原件打印") from None
        register_heif_opener(thumbnails=False)
    canvas = Canvas(str(target))
    with Image.open(source) as image:
        # GIF 为动画时取第一帧，多页扫描件及 HEIF 的主图页逐页保留。
        frames = 1 if image.format == "GIF" else getattr(image, "n_frames", 1)
        _check_pages(frames)
        for index in range(frames):
            image.seek(index)
            if image.width * image.height > MAX_PIXELS:
                raise ConversionError("图片超过 4000 万像素，请下载原件打印")
            with ImageOps.exif_transpose(image) as oriented, oriented.convert("RGBA") as rgba:
                with Image.new("RGB", rgba.size, "white") as paper:
                    paper.paste(rgba, mask=rgba.getchannel("A"))
                    width, height = landscape(A4) if paper.width > paper.height else A4
                    scale = min((width - 20 * mm) / paper.width, (height - 20 * mm) / paper.height)
                    draw_width, draw_height = paper.width * scale, paper.height * scale
                    canvas.setPageSize((width, height))
                    canvas.drawImage(ImageReader(paper), (width - draw_width) / 2, (height - draw_height) / 2,
                                     width=draw_width, height=draw_height)
                    canvas.showPage()
    canvas.save()
    return frames


def _decode_text(data: bytes) -> str:
    encodings = ("utf-16",) if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030")
    for encoding in encodings:
        try:
            value = data.decode(encoding)
            if "\x00" not in value:
                return value
        except UnicodeError:
            continue
    raise ConversionError("附件文本编码无法识别")


def _xml_lines(data: bytes) -> tuple[str, list[str]]:
    root = ElementTree.fromstring(data, forbid_dtd=True)
    fields, _ = fields_from_xml(data)
    labels = {"number": "发票号码", "code": "发票代码", "buyer_name": "购买方", "buyer_tax_id": "购买方税号",
              "seller_name": "销售方", "issue_date": "开票日期", "amount": "价税合计"}
    lines = [f"{label}  {fields[key]}" for key, label in labels.items() if key in fields]
    if fields:
        lines.extend(["", "原件结构化字段"])
    stack = [(root, ())]
    while stack:
        node, path = stack.pop()
        node_path = (*path, node.tag.rsplit("}", 1)[-1])
        if len(node_path) > 40 or len(lines) >= 3500:
            raise ConversionError("XML 结构超过可打印范围")
        value = (node.text or "").strip()
        attributes = "  ".join(f"{key}={value}" for key, value in node.attrib.items())
        if value or attributes:
            lines.append(" / ".join(node_path) + "  " + "  ".join(filter(None, (value, attributes))))
        stack.extend((child, node_path) for child in reversed(list(node)))
    return ("XML 发票转印" if fields else "XML 结构化原件转印"), lines


def _text_pdf(source: Path, filename: str, suffix: str, target: Path) -> int:
    if source.stat().st_size > 4 * PRINT_TEXT_LIMIT:
        raise ConversionError("文本附件超过可打印范围")
    data = source.read_bytes()
    if suffix == ".xml":
        title, lines = _xml_lines(data)
    else:
        content = _decode_text(data)
        title = "CSV 原件转印" if suffix == ".csv" else "文本原件转印"
        lines = ["  |  ".join(row) for row in csv.reader(StringIO(content))] if suffix == ".csv" else content.splitlines()
    if len(lines) > 3500 or sum(len(line) for line in lines) > PRINT_TEXT_LIMIT:
        raise ConversionError("文本附件超过可打印范围")
    render_plain(target, title, [f"原件  {filename}", "", *lines])
    with target.open("rb") as stream:
        count = len(PdfReader(stream).pages)
    _check_pages(count)
    return count


def convert_attachment(item: ArchiveFile, work: Path) -> tuple[Path, int]:
    """识别真实文件格式，成功时返回完成的附件 PDF 和页数。"""
    if not item.storage_key:
        raise ConversionError(item.reason or "附件原件尚未取得")
    source = resolve(item.storage_key)
    if not source.is_file():
        raise ConversionError("存储中的原件缺失，请重新同步")
    target = work / "print.pdf"
    suffix = _format(source, item.filename)
    if suffix == ".pdf":
        count = _normalize_pdf(source, target)
    elif suffix in IMAGE_FORMATS:
        count = _image_pdf(source, target, suffix)
    elif suffix in OFFICE_FORMATS or suffix == ".ofd":
        count = _normalize_pdf(_external_pdf(source, suffix, work), target)
    elif suffix in {".xml", ".csv", ".txt"}:
        count = _text_pdf(source, item.filename, suffix, target)
    elif suffix in ARCHIVE_TYPES:
        raise ConversionError("请下载压缩包原件并解包后打印")
    else:
        raise ConversionError("该格式暂无法转换，请下载原件打印")
    return target, count
