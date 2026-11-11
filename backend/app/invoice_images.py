"""共用的原件图像预处理。整份附件校验后才可向识别服务发送图像。"""
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
_pdf_lock = Lock()


class ParseRejected(ValueError):
    """原件冲突或超过处理边界，需要拆分、核对或替换原件。"""


@dataclass(frozen=True)
class InvoiceImage:
    width: int
    height: int
    data: str = field(repr=False)


def _encode_page(image: Image.Image) -> InvoiceImage:
    if image.width * image.height > MAX_PIXELS:
        raise ParseRejected("图片超过 4000 万像素，请缩小原件后重新提交")
    with ImageOps.exif_transpose(image) as oriented:
        oriented.thumbnail((2048, 2048))
        # 白底 JPEG 支持透明图片和 TIFF 等不同原件格式，同时去掉原件元数据。
        with oriented.convert("RGBA") as rgba, Image.new("RGB", oriented.size, "white") as canvas:
            with rgba.getchannel("A") as alpha, io.BytesIO() as stream:
                canvas.paste(rgba, mask=alpha)
                canvas.save(stream, format="JPEG", quality=90, optimize=True)
                return InvoiceImage(canvas.width, canvas.height, base64.b64encode(stream.getvalue()).decode("ascii"))


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
