"""分类只准备首页缩略图；票面识别另行校验并准备完整原件图像。"""
import base64
import io
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock

from PIL import Image, ImageOps

MAX_PAGES = 10
MAX_PIXELS = 40_000_000
MODEL_IMAGE_LIMIT = 12 * 1024 * 1024
OCR_SAMPLE_LIMIT = 10 * 1024 * 1024
CLASSIFICATION_IMAGE_EDGE = 1024
CLASSIFICATION_IMAGE_QUALITY = 75
_pdf_lock = Lock()


class ParseRejected(ValueError):
    """原件冲突或超过处理边界，需要拆分、核对或替换原件。"""


@dataclass(frozen=True)
class InvoiceImage:
    width: int
    height: int
    data: str = field(repr=False)


def _encode_page(image: Image.Image, *, max_edge: int = 2048, quality: int = 90) -> InvoiceImage:
    if image.width * image.height > MAX_PIXELS:
        raise ParseRejected("图片超过 4000 万像素，请缩小原件后重新提交")
    with ImageOps.exif_transpose(image) as oriented:
        oriented.thumbnail((max_edge, max_edge))
        # 白底 JPEG 支持透明图片和 TIFF 等不同原件格式，同时去掉原件元数据。
        with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as canvas:
            with rgba.getchannel("A") as alpha, io.BytesIO() as stream:
                canvas.paste(rgba, mask=alpha)
                canvas.save(stream, format="JPEG", quality=quality, optimize=True)
                return InvoiceImage(canvas.width, canvas.height, base64.b64encode(stream.getvalue()).decode("ascii"))


def classification_first_page(path: Path, suffix: str) -> InvoiceImage:
    """只解码 PDF 首页或图片首帧，返回最长边 1024、质量 75 的 JPEG。

    分类不受票面识别的十页上限约束，也不创建后续页图像。返回值不能用于高清票面补读。
    空白首页直接交人工确认，避免模型把没有分类依据的页面判为普通附件。
    """
    def encode(image: Image.Image) -> InvoiceImage:
        page = _encode_page(image, max_edge=CLASSIFICATION_IMAGE_EDGE, quality=CLASSIFICATION_IMAGE_QUALITY)
        with io.BytesIO(base64.b64decode(page.data)) as stream, Image.open(stream) as preview:
            with preview.convert("L") as gray:
                low, high = gray.getextrema()
                if high - low <= 2:
                    raise ParseRejected("附件首页为空白，请核对原件或人工确认类别")
        return page

    if suffix == ".pdf":
        import pypdfium2 as pdfium
        # 与票面渲染共用 PDFium 锁；只访问第 0 页，网络请求在释放锁后执行。
        with _pdf_lock, closing(pdfium.PdfDocument(str(path))) as doc:
            if not len(doc):
                raise ParseRejected("PDF 没有可读取的首页，请核对原件")
            with closing(doc[0]) as page:
                width, height = page.get_size()
                if width <= 0 or height <= 0 or width * height * 4 > MAX_PIXELS:
                    raise ParseRejected("PDF 首页尺寸异常或超过 4000 万像素")
                with closing(page.render(scale=min(2, CLASSIFICATION_IMAGE_EDGE / max(width, height)))) as bitmap:
                    with bitmap.to_pil() as image:
                        return encode(image)
    try:
        with Image.open(path) as image:
            image.seek(0)
            return encode(image)
    except Image.DecompressionBombError as exc:
        raise ParseRejected("图片超过 4000 万像素，请缩小原件后重新提交") from exc


def invoice_page_images(path: Path, suffix: str) -> list[InvoiceImage]:
    """按原件顺序准备最多十页 JPEG，失败时整份原件均未发送。"""
    pages = []
    if suffix == ".pdf":
        import pypdfium2 as pdfium
        # PDFium 对不同文档也不能并发调用，审批和 Agent 共用此锁。网络请求在释放锁后执行。
        with _pdf_lock, closing(pdfium.PdfDocument(str(path))) as doc:
            if not 0 < len(doc) <= MAX_PAGES:
                raise ParseRejected("图像识别的 PDF 须为 1 至 10 页，请拆分附件")
            for index in range(len(doc)):
                with closing(doc[index]) as page:
                    width, height = page.get_size()
                    if width <= 0 or height <= 0 or width * height * 4 > MAX_PIXELS:
                        raise ParseRejected("PDF 页面尺寸异常或超过 4000 万像素")
                    with closing(page.render(scale=min(2, 2048 / max(width, height)))) as bitmap:
                        with bitmap.to_pil() as image:
                            pages.append(_encode_page(image))
    else:
        try:
            with Image.open(path) as image:
                frames = getattr(image, "n_frames", 1)
                if not 0 < frames <= MAX_PAGES:
                    raise ParseRejected("图片附件须为 1 至 10 页，请拆分附件")
                for index in range(frames):
                    image.seek(index)
                    pages.append(_encode_page(image))
        except Image.DecompressionBombError as exc:
            raise ParseRejected("图片超过 4000 万像素，请缩小原件后重新提交") from exc
    return pages


def model_image_blocks(pages: list[InvoiceImage]) -> list[dict]:
    """保留 Harness 请求总量上限，仅传原件图像，不传 OCR 候选。"""
    if sum(len(page.data) for page in pages) > MODEL_IMAGE_LIMIT:
        raise ParseRejected("模型识别图片总大小超过 12 MB，请拆分或压缩附件")
    return [{"type": "image", "data": page.data, "mimeType": "image/jpeg"} for page in pages]


def invoice_images(path: Path, suffix: str) -> list[dict]:
    """供独立的多模态正文读取流程使用。"""
    return model_image_blocks(invoice_page_images(path, suffix))


def sample_invoice_image(data: bytes) -> InvoiceImage:
    """仅接受一页真实的 JPEG 或 PNG，使用与业务识别相同的预处理。"""
    if len(data) > OCR_SAMPLE_LIMIT:
        raise ParseRejected("测试样票不能超过 10 MB")
    try:
        with io.BytesIO(data) as stream, Image.open(stream, formats=("JPEG", "PNG")) as image:
            if getattr(image, "n_frames", 1) != 1:
                raise ParseRejected("请选择一张单页 JPG 或 PNG 样票")
            return _encode_page(image)
    except Image.DecompressionBombError as exc:
        raise ParseRejected("测试样票超过 4000 万像素") from exc
    except ParseRejected:
        raise
    except (OSError, SyntaxError, ValueError) as exc:
        raise ParseRejected("请上传完整的 JPG 或 PNG 样票") from exc
