"""
凭证图片预处理（图片解码 / EXIF 纠正 / 缩放 / PDF 转图片）
    目标：
        - 把用户上传的 JPG/PNG/PDF 统一变成模型可读、体积可控的图片；
        - 去掉 EXIF/GPS 等隐私元数据；
        - 限制最长边与单张大小，控制 token 与请求体积；
        - PDF 只取前 N 页，避免成本失控。
    依赖：
        - Pillow：图片解码、EXIF 方向纠正、缩放、编码；
        - pypdfium2：PDF 渲染，自带 PDFium，不依赖 poppler/LibreOffice。
    边界：
        - 本模块只做"文件 → 图片字节"，不调用模型、不写数据库；
        - 依赖缺失或文件损坏时抛 DocumentPreprocessError，由上层降级为人工核对。
"""
import base64
import io
import logging
from dataclasses import dataclass

from PIL import Image, ImageOps

from app.config import settings



logger = logging.getLogger(__name__)



# 支持的输入类型
SUPPORTED_IMAGE_TYPES = ("image/jpeg", "image/png")
SUPPORTED_PDF_TYPE = "application/pdf"



# 预处理错误
class DocumentPreprocessError(Exception):
    """
    凭证预处理失败
        :ivar code: 机器可读错误码（unsupported_type/unreadable/too_large/dependency_missing）
    """

    def __init__(self, message: str, *, code: str = "PREPROCESS_ERROR"):
        """
        :param message: 错误说明
        :param code: 机器可读错误码
        """
        super().__init__(message)
        self.message = message      # 错误说明
        self.code = code            # 错误码



# 一张预处理后的图片
@dataclass(frozen=True)
class PreparedImage:
    """
    可直接送入多模态模型的图片
        :ivar content: 图片字节
        :ivar mime_type: 图片 MIME（image/jpeg 或 image/png）
        :ivar width: 宽（像素）
        :ivar height: 高（像素）
        :ivar page_number: PDF 页码，图片固定为 1
        :ivar source_type: 来源类型：image / pdf
    """
    content: bytes                         # 图片字节
    mime_type: str                         # 图片 MIME
    width: int                             # 宽
    height: int                            # 高
    page_number: int = 1                   # 页码
    source_type: str = "image"             # 来源类型

    def to_data_url(self) -> str:
        """
        转成 data URI，供 OpenAI 兼容的 image_url 使用
            :return: data:image/jpeg;base64,...
        """
        encoded = base64.b64encode(self.content).decode("ascii")   # base64 文本
        return f"data:{self.mime_type};base64,{encoded}"           # data URI



# 编码一张已缩放的图片，控制体积
def _encode_image(image: Image.Image, *, prefer_png: bool) -> tuple[bytes, str]:
    """
    把 Pillow 图片编码成目标格式，并在超限时逐步降低质量/尺寸
        :param image: RGB 图片
        :param prefer_png: 是否优先使用 PNG（截图/文字为主）
        :return: (图片字节, MIME)
    """
    max_bytes = int(settings.DOCUMENT_IMAGE_MAX_MB * 1024 * 1024)   # 目标字节上限
    max_edge = max(320, int(settings.DOCUMENT_IMAGE_MAX_EDGE))       # 最长边
    current = image.copy()                                           # 当前图片

    # 1.逐轮尝试：先按目标尺寸/质量编码，超限再缩小
    for attempt in range(3):
        if max(current.width, current.height) > max_edge:
            current.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)   # 等比缩小
        buffer = io.BytesIO()                                        # 输出缓冲
        if prefer_png and attempt == 0:
            current.save(buffer, format="PNG", optimize=True)        # 截图优先 PNG
            mime = "image/png"                                       # PNG MIME
        else:
            quality = 85 if attempt == 0 else 75                     # 逐轮降低质量
            current.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)   # JPEG
            mime = "image/jpeg"                                      # JPEG MIME
        data = buffer.getvalue()                                     # 图片字节
        if len(data) <= max_bytes:
            return data, mime                                        # 体积达标
        max_edge = int(max_edge * 0.8)                               # 下一轮缩小 20%

    # 2.三轮仍超限：返回最后一轮结果，由上层记录"已压缩"
    logger.warning("凭证图片压缩后仍超过目标大小 bytes=%s", len(data))
    return data, mime



# 处理单张图片
def _prepare_image(content: bytes, *, content_type: str) -> list[PreparedImage]:
    """
    解码图片、纠正 EXIF 方向、缩放并重新编码
        :param content: 原始字节
        :param content_type: 原始 MIME
        :return: 预处理后的图片列表（单张）
        :raises DocumentPreprocessError: 图片损坏
    """
    try:
        with Image.open(io.BytesIO(content)) as source:
            image = ImageOps.exif_transpose(source)                 # 按 EXIF 纠正方向
            # 透明 PNG 先铺白底，避免 convert("RGB") 把透明区域变黑
            if image.mode in ("RGBA", "LA") or "transparency" in image.info:
                rgba = image.convert("RGBA")                        # 统一 RGBA
                background = Image.new("RGB", rgba.size, (255, 255, 255))   # 白底
                background.paste(rgba, mask=rgba.split()[-1])       # 按 alpha 合成
                image = background                                  # 使用白底图
            else:
                image = image.convert("RGB")                        # 统一 RGB
            image.thumbnail(
                (settings.DOCUMENT_IMAGE_MAX_EDGE, settings.DOCUMENT_IMAGE_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )                                                       # 等比缩小，不放大
            prefer_png = content_type == "image/png"                # 截图优先 PNG
            data, mime = _encode_image(image, prefer_png=prefer_png)   # 编码
            return [PreparedImage(
                content=data,
                mime_type=mime,
                width=image.width,
                height=image.height,
                page_number=1,
                source_type="image",
            )]
    except DocumentPreprocessError:
        raise
    except Exception as exc:
        raise DocumentPreprocessError(f"图片无法解码: {exc}", code="unreadable") from exc



# 处理 PDF
def _prepare_pdf(content: bytes) -> list[PreparedImage]:
    """
    把 PDF 的前 N 页渲染成图片
        :param content: PDF 字节
        :return: 预处理后的图片列表
        :raises DocumentPreprocessError: PDF 损坏或依赖缺失
    """
    try:
        import pypdfium2 as pdfium                              # 延迟导入，便于给出明确错误
    except ImportError as exc:
        raise DocumentPreprocessError("PDF 渲染依赖未安装", code="dependency_missing") from exc

    try:
        document = pdfium.PdfDocument(content)                  # 直接从字节打开
    except Exception as exc:
        raise DocumentPreprocessError(f"PDF 无法打开: {exc}", code="unreadable") from exc

    prepared: list[PreparedImage] = []                          # 结果列表
    try:
        page_count = min(len(document), max(1, int(settings.DOCUMENT_PDF_MAX_PAGES)))   # 页数上限
        for index in range(page_count):
            page = document[index]                              # 当前页
            bitmap = page.render(scale=float(settings.DOCUMENT_PDF_RENDER_SCALE))       # 渲染
            image = bitmap.to_pil().convert("RGB")               # 转 Pillow 图片
            image.thumbnail(
                (settings.DOCUMENT_IMAGE_MAX_EDGE, settings.DOCUMENT_IMAGE_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )                                                   # 等比缩小
            data, mime = _encode_image(image, prefer_png=False)  # PDF 页面统一 JPEG
            prepared.append(PreparedImage(
                content=data,
                mime_type=mime,
                width=image.width,
                height=image.height,
                page_number=index + 1,
                source_type="pdf",
            ))
    except DocumentPreprocessError:
        raise
    except Exception as exc:
        raise DocumentPreprocessError(f"PDF 渲染失败: {exc}", code="unreadable") from exc
    finally:
        try:
            document.close()                                    # 释放 PDF 句柄
        except Exception:
            pass

    if not prepared:
        raise DocumentPreprocessError("PDF 没有可渲染页面", code="unreadable")
    return prepared



# 统一入口
def prepare_attachment(content: bytes, content_type: str) -> list[PreparedImage]:
    """
    把一张附件预处理成模型可读图片
        :param content: 文件字节
        :param content_type: 文件 MIME
        :return: 预处理后的图片列表（PDF 可能多页）
        :raises DocumentPreprocessError: 类型不支持、文件损坏或依赖缺失
    """
    if not content:
        raise DocumentPreprocessError("文件内容为空", code="unreadable")
    if content_type in SUPPORTED_IMAGE_TYPES:
        return _prepare_image(content, content_type=content_type)
    if content_type == SUPPORTED_PDF_TYPE:
        return _prepare_pdf(content)
    raise DocumentPreprocessError(f"不支持的文件类型: {content_type}", code="unsupported_type")
