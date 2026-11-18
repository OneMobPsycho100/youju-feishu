"""PDF 本地文字层读取；不渲染图像，也不调用 OCR 或模型补读。"""
from collections.abc import Sequence
from pathlib import Path

from pypdf import PdfReader

from .invoice_images import MAX_PAGES, ParseRejected


class PdfTextMissing(ParseRejected):
    """页面没有文字层；发票入口可用原件图像兜底，普通材料仍报告正文不可读。"""


def pdf_text_page_count(reader: PdfReader, *, allow_long: bool = False) -> int:
    """校验 PDF 可读取并返回原件总页数。付款合同探查可暂缓完整读取的页数校验。"""
    count = 0 if reader.is_encrypted else len(reader.pages)
    if not count or (not allow_long and count > MAX_PAGES):
        raise ParseRejected(f"本地解析仅支持未加密且不超过 {MAX_PAGES} 页的 PDF，请拆分或人工处理")
    return count


def read_pdf_pages(reader: PdfReader, *, stop: int, max_text: int | None = None,
                   prefix: Sequence[dict[str, str]] = ()) -> list[dict[str, str]]:
    """读取第 1 至 stop 页。传入同一原件的连续前缀时，仅补读剩余页并校验合计字数。"""
    segments = list(prefix)
    characters = sum(len(segment["text"]) for segment in segments)
    for index in range(len(segments), stop):
        page = reader.pages[index]
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            text = ""
        if not text.strip():
            text = page.extract_text() or ""
        if not text.strip():
            raise PdfTextMissing(f"PDF 第 {index + 1} 页没有可读取的文字层，请提供文字版 PDF 或人工处理")
        characters += len(text)
        if max_text is not None and characters > max_text:
            raise ParseRejected(f"PDF 正文超过 {max_text} 字，请拆分或人工处理")
        segments.append({"location": f"第{index + 1}页", "text": text})
    return segments


def read_pdf_segments(path: Path, *, max_text: int | None = None) -> list[dict[str, str]]:
    """按页返回完整文字及定位；加密、超页、空文字页或超出字数上限时交人工处理。

    优先保留票面布局，遇到不兼容的文字布局时仅回退到本地普通文字提取。
    任一页无法读取时整份文件读取失败，避免把部分正文当成完整材料。
    """
    with path.open("rb") as stream:
        reader = PdfReader(stream)
        return read_pdf_pages(reader, stop=pdf_text_page_count(reader), max_text=max_text)


def read_pdf_first_page(path: Path, *, max_text: int = 60000) -> str:
    """分类专用的首页文字读取；校验文件可读性，但不限制总页数或提取后续页。"""
    with path.open("rb") as stream:
        reader = PdfReader(stream)
        pdf_text_page_count(reader, allow_long=True)
        return read_pdf_pages(reader, stop=1, max_text=max_text)[0]["text"]
