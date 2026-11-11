"""
凭证预处理测试（离线，不联网）
    覆盖：图片缩放/编码、PNG 截图保留、PDF 转图片、非法类型、损坏文件。
"""
import io
import unittest

from PIL import Image

import tests  # noqa: F401  先设置测试环境变量
from app.services.document_preprocess import (
    DocumentPreprocessError,
    prepare_attachment,
)



# 构造测试图片字节
def make_image_bytes(size: tuple[int, int] = (3000, 2000), fmt: str = "JPEG") -> bytes:
    """
    生成一张纯色测试图片
        :param size: 宽高
        :param fmt: 图片格式
        :return: 图片字节
    """
    image = Image.new("RGB", size, (245, 245, 245))   # 浅灰底
    buffer = io.BytesIO()                              # 输出缓冲
    image.save(buffer, format=fmt)                     # 编码
    return buffer.getvalue()



class DocumentPreprocessTest(unittest.TestCase):
    """图片/PDF 预处理"""

    def test_jpeg_resized_and_encoded(self):
        """大图会缩到最长边上限，并输出 JPEG"""
        images = prepare_attachment(make_image_bytes((3000, 2000), "JPEG"), "image/jpeg")
        self.assertEqual(len(images), 1)
        image = images[0]
        self.assertEqual(image.mime_type, "image/jpeg")
        self.assertLessEqual(max(image.width, image.height), 1600)
        self.assertTrue(image.content)

    def test_png_screenshot_stays_png(self):
        """PNG 截图优先保持 PNG"""
        images = prepare_attachment(make_image_bytes((1200, 2000), "PNG"), "image/png")
        self.assertEqual(images[0].mime_type, "image/png")

    def test_pdf_rendered_to_image(self):
        """PDF 会渲染成图片"""
        image = Image.new("RGB", (800, 1000), (255, 255, 255))   # 单页 PDF
        buffer = io.BytesIO()
        image.save(buffer, format="PDF")
        images = prepare_attachment(buffer.getvalue(), "application/pdf")
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0].source_type, "pdf")
        self.assertEqual(images[0].page_number, 1)

    def test_unsupported_type_rejected(self):
        """不支持的类型直接拒绝"""
        with self.assertRaises(DocumentPreprocessError) as ctx:
            prepare_attachment(b"abc", "image/gif")
        self.assertEqual(ctx.exception.code, "unsupported_type")

    def test_corrupt_image_rejected(self):
        """损坏的图片标记为不可读"""
        with self.assertRaises(DocumentPreprocessError) as ctx:
            prepare_attachment(b"not-an-image", "image/jpeg")
        self.assertEqual(ctx.exception.code, "unreadable")

    def test_empty_content_rejected(self):
        """空文件直接拒绝"""
        with self.assertRaises(DocumentPreprocessError):
            prepare_attachment(b"", "image/jpeg")


if __name__ == "__main__":
    unittest.main()
