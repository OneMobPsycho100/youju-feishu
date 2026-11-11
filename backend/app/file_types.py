"""按原件内容识别文件，避免临时下载地址和错误后缀误导解析与预览。"""
import io
import mimetypes
import zipfile
from pathlib import Path

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
INLINE_PREVIEW_MIMES = frozenset({"application/pdf", "image/png", "image/jpeg", "image/webp"})
ZIP_SIGNATURES = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
ARCHIVE_TYPES = {
    ".zip": "application/zip",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".tgz": "application/gzip",
}
ARCHIVE_MIMES = frozenset(ARCHIVE_TYPES.values())


def zip_document_type(archive: zipfile.ZipFile) -> tuple[str, str] | None:
    """区分采用 ZIP 容器的文档与普通压缩包，沿用打印支持的文档格式。"""
    names = set(archive.namelist())
    for entry, suffix, mime in (
        ("word/document.xml", ".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xl/workbook.xml", ".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("ppt/presentation.xml", ".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    ):
        if entry in names:
            return suffix, mime
    if any(name.rsplit("/", 1)[-1].lower() == "ofd.xml" for name in names):
        return ".ofd", "application/ofd"
    if "mimetype" in names:
        entry = archive.getinfo("mimetype")
        # OpenDocument 的首项是未压缩的媒体类型。限制读取，避免格式探测时提前解压正文。
        if entry.compress_type == zipfile.ZIP_STORED and entry.file_size < 200 and not entry.flag_bits & 1:
            media_types = {
                b"application/vnd.oasis.opendocument.text": ".odt",
                b"application/vnd.oasis.opendocument.spreadsheet": ".ods",
                b"application/vnd.oasis.opendocument.presentation": ".odp",
            }
            mime = archive.read(entry)
            if mime in media_types:
                return media_types[mime], mime.decode("ascii")
    return None


def detect_file_type(data: bytes, filename: str) -> tuple[str, str]:
    """返回实际扩展名与 MIME；未知内容沿用文件名，解析器继续执行格式和完整性校验。"""
    head = data[:1024]
    suffix = Path(filename).suffix.lower()
    # ZIP 可原样存放 PDF，必须先检查外层签名，再查找文件头中的 PDF 标记。
    if head.startswith(ZIP_SIGNATURES):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                document = zip_document_type(archive)
                if document:
                    return document
        except (zipfile.BadZipFile, UnicodeError, ValueError):
            pass
        return ".zip", ARCHIVE_TYPES[".zip"]
    if head.startswith(b"7z\xbc\xaf\x27\x1c"):
        return ".7z", ARCHIVE_TYPES[".7z"]
    if head.startswith(b"Rar!\x1a\x07"):
        return ".rar", ARCHIVE_TYPES[".rar"]
    if head.startswith(b"\x1f\x8b"):
        return (".tgz" if suffix == ".tgz" else ".gz"), ARCHIVE_TYPES[".gz"]
    if head[257:263] in {b"ustar\0", b"ustar "}:
        return ".tar", ARCHIVE_TYPES[".tar"]
    if b"%PDF-" in head:
        return ".pdf", "application/pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return ".webp", "image/webp"
    if head.startswith((b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+")):
        return ".tiff", "image/tiff"
    encoding = "utf-16" if head.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    if head.decode(encoding, errors="ignore").lstrip().startswith("<"):
        return ".xml", "application/xml"
    return suffix, ARCHIVE_TYPES.get(suffix) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
